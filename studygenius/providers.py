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
PROMPT_VERSION = "4.0.0"


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
        self.shared_cache = store.root / "model-cache"
        # API compatibility is a property of the selected model/account, not of a
        # single project. Remember a validated fallback so every new project does
        # not pay for the same rejected probe again. Prompt versioning makes the
        # choice expire naturally when the request format changes.
        self.capability_path = store.root / "provider-capabilities.json"
        try:
            self.capabilities = read_json(self.capability_path) if self.capability_path.exists() else {}
        except (OSError, ValueError, TypeError):
            self.capabilities = {}
        gemini_key = self._capability_key("gemini", settings.gemini_model)
        deepseek_key = self._capability_key("deepseek", "responses-endpoint")
        self.gemini_native_schema = self.capabilities.get(gemini_key, True)
        self.deepseek_responses_schema = self.capabilities.get(deepseek_key, True)
        self.rate_limits = {
            "gemini": asyncio.Semaphore(settings.gemini_concurrency),
            "deepseek": asyncio.Semaphore(settings.deepseek_concurrency),
        }
        self.cache_stats = {"job_hits": 0, "shared_hits": 0, "pending_hits": 0, "writes": 0}

    @staticmethod
    def _capability_key(provider: str, model: str) -> str:
        return f"{PROMPT_VERSION}:{provider}:{model}"

    def remember_capability(self, provider: str, model: str, supported: bool):
        self.capabilities[self._capability_key(provider, model)] = supported
        atomic_json(self.capability_path, self.capabilities)

    async def close(self):
        await self.client.aclose()

    async def json(self, provider: str, task: str, system: str, prompt: str, schema: type[T],
                   images: list[Path] | None = None, validate: Callable[[T], None] | None = None,
                   max_output: int = 16000, tier: str = "quality") -> T:
        model = self.model_for(provider, tier)
        images = images or []
        contract = schema.model_json_schema()
        # Versioning deliberately invalidates responses when prompts or contracts change;
        # otherwise identical validated calls remain reusable across jobs.
        fingerprint = json.dumps({"v": PROMPT_VERSION, "provider": provider, "model": model,
            "system": system, "prompt": prompt, "schema": contract, "output": max_output,
            "images": [hashlib.sha256(p.read_bytes()).hexdigest() for p in images]}, sort_keys=True)
        digest = hashlib.sha256(fingerprint.encode()).hexdigest()
        cache_path = self.cache / f"{digest}.json"
        shared_path = self.shared_cache / digest[:2] / f"{digest}.json"
        pending_path = self.cache / "pending" / cache_path.name
        if cache_path.exists():
            result = schema.model_validate(read_json(cache_path))
            if validate:
                validate(result)
            self.cache_stats["job_hits"] += 1
            return result
        if shared_path.exists():
            try:
                result = schema.model_validate(read_json(shared_path))
                if validate:
                    validate(result)
            except (ValueError, ValidationError):
                # A stricter local validator may reject an older shared result. Leave it
                # available to the job that created it and regenerate for this context.
                pass
            else:
                atomic_json(cache_path, result.model_dump(mode="json"))
                self.cache_stats["shared_hits"] += 1
                return result
        corrected_prompt = prompt
        output_limit = max_output
        pending_raw = read_json(pending_path).get("raw") if pending_path.exists() else None
        if pending_raw is not None:
            self.cache_stats["pending_hits"] += 1
        for correction in range(self.json_attempts):
            self.check()
            try:
                if correction == 0 and pending_raw is not None:
                    raw = pending_raw
                else:
                    raw = await self.request(provider, task, system, corrected_prompt, contract,
                                             images, output_limit, model, tier)
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
                value = result.model_dump(mode="json")
                atomic_json(cache_path, value)
                atomic_json(shared_path, value)
                self.cache_stats["writes"] += 1
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

    def model_for(self, provider: str, tier: str) -> str:
        if provider == "gemini":
            return self.settings.gemini_model
        if provider == "deepseek":
            if tier not in ("fast", "quality"):
                raise ValueError("Tier del modello non supportato")
            return self.settings.deepseek_fast_model if tier == "fast" else self.settings.deepseek_model
        raise ValueError("Provider non supportato")

    def _request_payload(self, provider, system, prompt, schema, images, max_output, model, tier):
        key = getattr(self.settings, f"{provider}_key").get_secret_value()
        if not key:
            raise ProviderError(f"Inserisci la chiave API {provider} nelle impostazioni.")
        compact_contract = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
        if provider == "gemini":
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
            headers = {"x-goog-api-key": key}
            parts = [{"text": prompt}]
            for path in images:
                parts.extend([{"text": f"Immagine: {path.stem}"}, {"inlineData": {
                    "mimeType": "image/jpeg" if path.suffix.lower() in (".jpg", ".jpeg") else "image/png",
                    "data": base64.b64encode(path.read_bytes()).decode()}}])
            generation = {"responseMimeType": "application/json", "maxOutputTokens": max_output}
            native = self.gemini_native_schema
            if native:
                generation["responseJsonSchema"] = gemini_schema(schema)
            else:
                parts.append({"text": "JSON Schema obbligatorio: " + compact_contract})
            body = {"systemInstruction": {"parts": [{"text": system}]},
                    "contents": [{"role": "user", "parts": parts}], "generationConfig": generation}
            return url, headers, body, "gemini-schema" if native else "gemini-json"
        if provider == "deepseek":
            if images:
                raise ValueError("Le immagini di questa pipeline sono assegnate al revisore Gemini")
            headers = {"Authorization": f"Bearer {key}"}
            if self.deepseek_responses_schema:
                # The Responses endpoint supports native JSON Schema. This removes the
                # repeated contract from prose and gives the server a strict structure.
                body = {"model": model, "instructions": system, "input": prompt,
                        "text": {"format": {"type": "json_schema", "name": "studygenius_response",
                                             "schema": schema}},
                        "reasoning": {"effort": "none" if tier == "fast" else self.settings.deepseek_reasoning_effort},
                        "max_output_tokens": max_output, "stream": False}
                return "https://api.deepseek.com/responses", headers, body, "deepseek-responses"
            body = {"model": model, "messages": [{"role": "system", "content": system +
                    "\nRestituisci JSON conforme a questo schema: " + compact_contract},
                    {"role": "user", "content": prompt}], "response_format": {"type": "json_object"},
                    "max_tokens": max_output, "stream": False}
            return "https://api.deepseek.com/chat/completions", headers, body, "deepseek-chat"
        raise ValueError("Provider non supportato")

    async def request(self, provider, task, system, prompt, schema, images, max_output, model, tier):
        key = getattr(self.settings, f"{provider}_key").get_secret_value()
        if not key:
            raise ProviderError(f"Inserisci la chiave API {provider} nelle impostazioni.")
        for retry in range(4):
            self.check()
            url, headers, body, response_style = self._request_payload(
                provider, system, prompt, schema, images, max_output, model, tier)
            options = self.store.get(self.job_id)["options"]
            call_id = self.store.reserve_call(self.job_id, provider, model, task,
                                             options["max_api_calls"], options["max_total_tokens"])
            try:
                # Backoff happens outside the semaphore, so a throttled request does not
                # prevent an unrelated ready request from using the provider connection.
                async with self.rate_limits[provider]:
                    response = await self.client.post(url, headers=headers, json=body)
            except httpx.TransportError:
                self.store.finish_call(call_id, "transport_error")
                if retry == 3:
                    raise ProviderError(f"{provider}: connessione interrotta o timeout. Riprendi il lavoro per ritentare.") from None
                await self.backoff(retry, task)
                continue
            self.store.finish_call(call_id, f"http_{response.status_code}")
            if provider == "gemini" and response.status_code == 400 and response_style == "gemini-schema" and retry < 3:
                # Some serving versions reject valid but complex schemas. Keep JSON mode,
                # include the full contract in the prompt, and retain identical local validation.
                self.gemini_native_schema = False
                self.remember_capability("gemini", model, False)
                self.store.event(self.job_id, f"{task}: adatto il formato della richiesta per questo modello.")
                continue
            if (provider == "deepseek" and response.status_code in (400, 404, 422)
                    and response_style == "deepseek-responses" and retry < 3):
                # Older accounts/endpoints may expose only Chat Completions. Fall back
                # once, retain local Pydantic validation, and reuse that capability choice.
                self.deepseek_responses_schema = False
                self.remember_capability("deepseek", "responses-endpoint", False)
                self.store.event(self.job_id, f"{task}: endpoint Structured Outputs non disponibile; uso JSON validato localmente.")
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
                    cached = usage.get("cachedContentTokenCount", 0)
                    self.store.finish_call(call_id, "received", incoming, outgoing, total, cached)
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
                elif response_style == "deepseek-responses":
                    usage = payload.get("usage", {})
                    incoming, outgoing = usage.get("input_tokens", 0), usage.get("output_tokens", 0)
                    total = usage.get("total_tokens", incoming + outgoing)
                    cached = usage.get("input_tokens_details", {}).get("cached_tokens", 0)
                    self.store.finish_call(call_id, "received", incoming, outgoing, total, cached)
                    status = payload.get("status")
                    if status == "incomplete" and payload.get("incomplete_details", {}).get("reason") == "max_output_tokens":
                        raise TruncatedOutput("DeepSeek: output troncato; aumento il limite prima di riprovare.")
                    if status != "completed":
                        raise ModelOutputError(f"DeepSeek: risposta non completata ({status or 'stato assente'}).")
                    text = "".join(part.get("text", "") for item in payload.get("output", [])
                                   if item.get("type") == "message" for part in item.get("content", [])
                                   if part.get("type") == "output_text")
                else:
                    usage = payload.get("usage", {})
                    incoming, outgoing = usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
                    total = usage.get("total_tokens", incoming + outgoing)
                    cached = (usage.get("prompt_cache_hit_tokens", 0)
                              or usage.get("input_tokens_details", {}).get("cached_tokens", 0))
                    self.store.finish_call(call_id, "received", incoming, outgoing, total, cached)
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
                if provider == "deepseek":
                    result[provider]["fast_model"] = settings.deepseek_fast_model
                    result[provider]["fast_model_ok"] = settings.deepseek_fast_model in names
            except (httpx.HTTPError, ValueError, KeyError):
                result[provider] = {"ok": False, "error": "Impossibile raggiungere il servizio o leggere l'elenco modelli"}
    return result
