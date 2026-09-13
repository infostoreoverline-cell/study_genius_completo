"""Small native REST adapters with persistent accounting, validated JSON and bounded retries."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import random
from pathlib import Path
from typing import Callable, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from .config import Settings
from .storage import Store, atomic_json, read_json

T = TypeVar("T", bound=BaseModel)
PROMPT_VERSION = "1.0.0"


class ProviderError(RuntimeError):
    pass


class ModelOutputError(ProviderError):
    pass


class TruncatedOutput(ModelOutputError):
    pass


def parse_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```") and text.endswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    result = json.loads(text)
    if not isinstance(result, dict):
        raise ValueError("La risposta deve essere un oggetto JSON")
    return result


def gemini_schema(schema: dict) -> dict:
    """Inline local refs and remove unsupported metadata/length keywords.

    Property *names* such as 'title' are data and must not be stripped. The full
    Pydantic contract is still enforced locally after every response.
    """
    defs = schema.get("$defs", {})
    def convert(node):
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            return convert(defs[node["$ref"].split("/")[-1]])
        out = {}
        for key, value in node.items():
            if key in {"$defs", "title", "default", "minLength", "maxLength"}:
                continue
            if key == "properties":
                out[key] = {name: convert(spec) for name, spec in value.items()}
            elif key in {"items", "additionalProperties"} and isinstance(value, dict):
                out[key] = convert(value)
            elif key in {"anyOf", "oneOf", "allOf"}:
                out[key] = [convert(spec) for spec in value]
            else:
                out[key] = value
        return out
    return convert(schema)


class Models:
    json_attempts = 3

    def __init__(self, settings: Settings, store: Store, job_id: str, check: Callable,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.settings, self.store, self.job_id, self.check = settings, store, job_id, check
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(300, connect=20), transport=transport)
        self.cache = store.directory(job_id) / "cache"
        self.gemini_native_schema = True

    async def close(self):
        await self.client.aclose()

    async def json(self, provider: str, task: str, system: str, prompt: str, schema: type[T],
                   images: list[Path] | None = None, validate: Callable[[T], None] | None = None,
                   max_output: int = 16000) -> T:
        model = getattr(self.settings, f"{provider}_model")
        images = images or []
        fingerprint = json.dumps({"v": PROMPT_VERSION, "provider": provider, "model": model,
            "system": system, "prompt": prompt, "schema": schema.model_json_schema(), "output": max_output,
            "images": [hashlib.sha256(p.read_bytes()).hexdigest() for p in images]}, sort_keys=True)
        cache_path = self.cache / f"{hashlib.sha256(fingerprint.encode()).hexdigest()}.json"
        pending_path = self.cache / "pending" / cache_path.name
        if cache_path.exists():
            result = schema.model_validate(read_json(cache_path))
            if validate:
                validate(result)
            return result
        corrected_prompt = prompt
        output_limit = max_output
        pending_raw = read_json(pending_path).get("raw") if pending_path.exists() else None
        for correction in range(self.json_attempts):
            self.check()
            try:
                if correction == 0 and pending_raw is not None:
                    raw = pending_raw
                else:
                    raw = await self.request(provider, task, system, corrected_prompt, schema.model_json_schema(), images, output_limit)
                    # Retain a paid response before validation. A renderer/validator fix
                    # can revalidate it on resume without another API request. This
                    # private diagnostic is never treated as a validated cache entry.
                    atomic_json(pending_path, {"raw": raw})
            except TruncatedOutput:
                if correction == self.json_attempts - 1 or output_limit >= 64000:
                    raise
                output_limit = min(output_limit * 2, 64000)
                self.store.event(self.job_id, f"{task}: risposta troncata; aumento il limite di output a {output_limit} token.")
                continue
            try:
                result = schema.model_validate(parse_json(raw))
                if validate:
                    validate(result)
                atomic_json(cache_path, result.model_dump(mode="json"))
                pending_path.unlink(missing_ok=True)
                self.check()
                return result
            except (ValueError, ValidationError) as exc:
                # ValidationError must not include complete generated content or possible secrets in logs.
                if isinstance(exc, ValidationError):
                    details = [{"loc": e["loc"], "msg": e["msg"]} for e in exc.errors(include_input=False)]
                else:
                    details = str(exc)[:1500]
                atomic_json(pending_path, {"raw": raw, "validation_errors": details})
                if correction == self.json_attempts - 1:
                    detail = json.dumps(details, ensure_ascii=False)[:1200]
                    raise ModelOutputError(f"{provider}: risposta non valida dopo {self.json_attempts} tentativi ({task}). Errori: {detail}") from None
                corrected_prompt = (prompt + "\nLa risposta precedente non rispettava il contratto. Rigenera tutto. "
                                    + "Errori da correggere: " + json.dumps(details, ensure_ascii=False))
                self.store.event(self.job_id, f"{task}: correggo la struttura della risposta ({correction + 1}/{self.json_attempts - 1}).")
        raise AssertionError("unreachable")

    async def request(self, provider, task, system, prompt, schema, images, max_output):
        key = getattr(self.settings, f"{provider}_key").get_secret_value()
        model = getattr(self.settings, f"{provider}_model")
        if not key:
            raise ProviderError(f"Inserisci la chiave API {provider} nelle impostazioni.")
        if provider == "gemini":
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
            headers = {"x-goog-api-key": key}
            parts = [{"text": prompt}]
            for p in images:
                parts.extend([{"text": f"Immagine: {p.stem}"}, {"inlineData": {
                    "mimeType": "image/jpeg" if p.suffix in (".jpg", ".jpeg") else "image/png",
                    "data": base64.b64encode(p.read_bytes()).decode()}}])
            body = {"systemInstruction": {"parts": [{"text": system}]},
                    "contents": [{"role": "user", "parts": parts}],
                    "generationConfig": {"responseMimeType": "application/json", "responseJsonSchema": gemini_schema(schema),
                                         "maxOutputTokens": max_output}}
            if not self.gemini_native_schema:
                body["generationConfig"].pop("responseJsonSchema")
                parts.append({"text": "JSON Schema da rispettare integralmente: " + json.dumps(schema, ensure_ascii=False)})
        elif provider == "deepseek":
            if images:
                raise ValueError("Le immagini di questa pipeline sono assegnate al revisore Gemini")
            url = "https://api.deepseek.com/chat/completions"
            headers = {"Authorization": f"Bearer {key}"}
            body = {"model": model, "messages": [{"role": "system", "content": system +
                    "\nRispondi in JSON conforme a questo JSON Schema: " + json.dumps(schema, ensure_ascii=False)},
                    {"role": "user", "content": prompt}], "response_format": {"type": "json_object"},
                    "max_tokens": max_output, "stream": False}
        else:
            raise ValueError("Provider non supportato")
        for retry in range(4):
            self.check()
            options = self.store.get(self.job_id)["options"]
            call_id = self.store.reserve_call(self.job_id, provider, model, task,
                                             options["max_api_calls"], options["max_total_tokens"])
            try:
                response = await self.client.post(url, headers=headers, json=body)
            except httpx.TransportError:
                self.store.finish_call(call_id, "transport_error")
                if retry == 3:
                    raise ProviderError(f"{provider}: connessione interrotta o timeout. Riprendi il lavoro per ritentare.") from None
                await self.backoff(retry, task)
                continue
            self.store.finish_call(call_id, f"http_{response.status_code}")
            if (provider == "gemini" and response.status_code == 400
                    and "responseJsonSchema" in body["generationConfig"] and retry < 3):
                # Some serving versions reject valid but complex schemas. Keep JSON mode,
                # include the full contract in the prompt, and retain identical local validation.
                self.gemini_native_schema = False
                body["generationConfig"].pop("responseJsonSchema")
                parts.append({"text": "JSON Schema da rispettare integralmente: " + json.dumps(schema, ensure_ascii=False)})
                self.store.event(self.job_id, f"{task}: adatto il formato della richiesta per questo modello.")
                continue
            if response.status_code in (408, 429, 500, 502, 503, 504):
                if retry == 3:
                    raise ProviderError(f"{provider}: servizio non disponibile o quota esaurita (HTTP {response.status_code}).")
                retry_after = response.headers.get("retry-after", "")
                await self.backoff(retry, task, min(float(retry_after), 60) if retry_after.isdigit() else None)
                continue
            if response.status_code >= 400:
                messages = {400: "richiesta non supportata: verifica modello e limiti di output",
                            401: "chiave API non valida", 403: "accesso negato: verifica chiave, progetto e modello",
                            402: "credito insufficiente", 404: "modello non disponibile: aggiorna il nome nelle impostazioni"}
                raise ProviderError(f"{provider}: {messages.get(response.status_code, 'errore del servizio')} (HTTP {response.status_code}).")
            try:
                payload = response.json()
                if provider == "gemini":
                    usage = payload.get("usageMetadata", {})
                    incoming = usage.get("promptTokenCount", 0)
                    outgoing = usage.get("candidatesTokenCount", 0) + usage.get("thoughtsTokenCount", 0)
                    total = usage.get("totalTokenCount", incoming + outgoing)
                    self.store.finish_call(call_id, "received", incoming, outgoing, total)
                    candidates = payload.get("candidates", [])
                    if not candidates:
                        raise ModelOutputError("Gemini non ha restituito candidati (blocco o risposta vuota).")
                    candidate = candidates[0]
                    reason = candidate.get("finishReason", "")
                    if reason == "MAX_TOKENS":
                        raise TruncatedOutput("Gemini: output troncato anche dopo l'aumento del limite. Dividi il materiale in progetti più piccoli.")
                    if reason != "STOP":
                        raise ModelOutputError(f"Gemini: risposta incompleta o bloccata ({reason}). Riduci le pagine per blocco.")
                    text = "".join(p.get("text", "") for p in candidate.get("content", {}).get("parts", []) if not p.get("thought"))
                else:
                    usage = payload.get("usage", {})
                    incoming, outgoing = usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
                    total = usage.get("total_tokens", incoming + outgoing)
                    self.store.finish_call(call_id, "received", incoming, outgoing, total)
                    candidate = payload["choices"][0]
                    if candidate.get("finish_reason") == "length":
                        raise TruncatedOutput("DeepSeek: output troncato anche dopo l'aumento del limite. Dividi il materiale in progetti più piccoli.")
                    if candidate.get("finish_reason") != "stop":
                        raise ModelOutputError("DeepSeek: risposta troncata o bloccata. Riprendi con un modello con output maggiore.")
                    text = candidate["message"].get("content") or ""
                if not text.strip():
                    raise ModelOutputError(f"{provider}: risposta vuota.")
                return text
            except (KeyError, IndexError, TypeError, json.JSONDecodeError):
                raise ProviderError(f"{provider}: formato della risposta API inatteso.") from None
        raise AssertionError("unreachable")

    async def backoff(self, retry, task, delay=None):
        delay = delay if delay is not None else min(2 ** (retry + 1) + random.random(), 30)
        self.store.event(self.job_id, f"{task}: attesa per limite API o errore temporaneo ({int(delay)} s).")
        # Short waits keep cancellation responsive.
        for _ in range(int(delay * 4) + 1):
            self.check()
            await asyncio.sleep(0.25)


async def available_models(settings: Settings) -> dict:
    result = {}
    async with httpx.AsyncClient(timeout=25) as client:
        for provider in ("gemini", "deepseek"):
            key = getattr(settings, f"{provider}_key").get_secret_value()
            if not key:
                result[provider] = {"ok": False, "error": "Chiave non configurata"}
                continue
            try:
                if provider == "gemini":
                    response = await client.get("https://generativelanguage.googleapis.com/v1beta/models",
                                                headers={"x-goog-api-key": key}, params={"pageSize": 1000})
                else:
                    response = await client.get("https://api.deepseek.com/models", headers={"Authorization": f"Bearer {key}"})
                if response.status_code != 200:
                    result[provider] = {"ok": False, "error": f"HTTP {response.status_code}: controlla chiave e accesso"}
                    continue
                payload = response.json()
                names = ([m["name"].removeprefix("models/") for m in payload.get("models", [])
                          if "generateContent" in m.get("supportedGenerationMethods", [])] if provider == "gemini"
                         else [m["id"] for m in payload.get("data", [])])
                selected = getattr(settings, f"{provider}_model")
                result[provider] = {"ok": selected in names, "models": names,
                                    "error": "" if selected in names else "Modello selezionato non presente nell'elenco"}
            except (httpx.HTTPError, ValueError, KeyError):
                result[provider] = {"ok": False, "error": "Impossibile raggiungere il servizio o leggere l'elenco modelli"}
    return result
