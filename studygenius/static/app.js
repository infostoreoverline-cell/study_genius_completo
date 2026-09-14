"use strict";
const $ = id => document.getElementById(id);
let token = "", files = [], selectedJob = null, polling = false, lastLimitsJob = null;
const states = {ready:"Pronto",queued:"In coda",running:"In elaborazione",paused:"In pausa",failed:"Da riprendere",completed:"Completato",needs_review:"Pronto · da verificare"};
const number = n => Number(n || 0).toLocaleString("it-IT");
let toastTimer;
function toast(message) { $("toast").textContent = message; $("toast").hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => $("toast").hidden = true, 9000); }
async function api(path, options = {}) {
  const headers = {"X-StudyGenius-Token": token, ...(options.headers || {})};
  if (options.body && !(options.body instanceof FormData)) headers["Content-Type"] = "application/json";
  const response = await fetch(path, {...options, headers});
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(typeof body.detail === "string" ? body.detail : "Richiesta non riuscita. Controlla i campi inseriti.");
  return body;
}
async function guarded(button, fn) {
  button.disabled = true;
  try { await fn(); } catch (error) { toast(error.message); } finally { button.disabled = false; }
}
function addFiles(incoming) {
  for (const file of incoming) {
    if (!file.name.toLowerCase().endsWith(".pdf")) { toast("Sono accettati soltanto file PDF."); continue; }
    if (file.size > 100 * 1024 * 1024) { toast("Ogni PDF deve essere inferiore a 100 MB."); continue; }
    if (!files.some(f => f.name === file.name && f.size === file.size && f.lastModified === file.lastModified)) files.push(file);
  }
  if (files.length > 30) { files = files.slice(0,30); toast("Puoi caricare fino a 30 file."); }
  renderFiles();
}
function renderFiles() {
  $("file-list").replaceChildren();
  files.forEach((file, index) => {
    const li = document.createElement("li"), name = document.createElement("span"), size = document.createElement("span"), remove = document.createElement("button");
    name.textContent = file.name; size.textContent = (file.size / 1024 / 1024).toFixed(1) + " MB"; size.className = "file-size";
    remove.textContent = "×"; remove.type = "button"; remove.setAttribute("aria-label", "Rimuovi " + file.name);
    remove.onclick = () => { files.splice(index,1); renderFiles(); };
    li.append(name,size,remove); $("file-list").append(li);
  });
}
async function refreshHealth() {
  const health = await api("/api/health");
  const notice = $("system-notice");
  notice.hidden = health.latex;
  notice.textContent = "Per generare il PDF manca LaTeX. Installa MiKTeX con XeLaTeX, riapri l’app e riprova. La guida nella repository contiene i passaggi per Windows.";
  applySettings(health.settings);
}
function applySettings(settings) {
  $("remember-keys").checked = Boolean(settings.remembered);
  $("gemini-model").value = settings.gemini_model; $("deepseek-model").value = settings.deepseek_model;
  $("deepseek-fast-model").value = settings.deepseek_fast_model;
  $("deepseek-reasoning-effort").value = settings.deepseek_reasoning_effort;
  $("gemini-status").textContent = settings.gemini_configured ? "CHIAVE PRESENTE" : "DA CONFIGURARE";
  $("deepseek-status").textContent = settings.deepseek_configured ? "CHIAVE PRESENTE" : "DA CONFIGURARE";
}
function settingsBody(clear = false) {
  return {gemini_key:$("gemini-key").value, deepseek_key:$("deepseek-key").value, gemini_model:$("gemini-model").value.trim(), deepseek_model:$("deepseek-model").value.trim(), deepseek_fast_model:$("deepseek-fast-model").value.trim(), deepseek_reasoning_effort:$("deepseek-reasoning-effort").value, remember:$("remember-keys").checked, clear_keys:clear};
}
async function saveSettings(clear = false) {
  const value = await api("/api/settings", {method:"POST", body:JSON.stringify(settingsBody(clear))});
  $("gemini-key").value = ""; $("deepseek-key").value = ""; applySettings(value);
}
async function refreshHistory() {
  const jobs = await api("/api/jobs");
  $("history").replaceChildren();
  $("mobile-history").replaceChildren(new Option("Progetti recenti", ""));
  if (!jobs.length) { const p = document.createElement("p"); p.className = "muted small"; p.textContent = "Il tuo prossimo esame comincia qui."; $("history").append(p); }
  for (const job of jobs) {
    $("mobile-history").append(new Option(job.title, job.id, false, job.id === selectedJob));
    const button = document.createElement("button"), status = document.createElement("small");
    button.textContent = job.title; status.textContent = (job.options.mode === "demo" ? "DEMO · " : "") + states[job.status];
    button.append(status); button.className = job.id === selectedJob ? "selected" : "";
    button.onclick = () => showJob(job.id).catch(e => toast(e.message)); $("history").append(button);
  }
}
async function showJob(id) {
  selectedJob = id; localStorage.setItem("sg-last-job", id);
  $("welcome").hidden = true; $("create-view").hidden = true; $("job-view").hidden = false;
  await refreshJob(); await refreshHistory();
}
async function refreshJob() {
  if (!selectedJob) return;
  const id = selectedJob, job = await api("/api/jobs/" + id);
  if (selectedJob !== id) return;
  $("breadcrumb").textContent = job.title; $("job-title").textContent = job.title;
  $("job-mode").textContent = job.options.mode === "demo" ? "DIMOSTRAZIONE OFFLINE · NESSUNA CHIAMATA API" : "LA TUA DISPENSA";
  $("job-status").textContent = states[job.status]; $("job-stage").textContent = job.stage;
  $("job-progress").value = job.progress; $("job-percent").textContent = Math.round(job.progress * 100) + "%";
  $("job-error").textContent = job.error; $("job-error").hidden = !job.error;
  const active = ["running","queued"].includes(job.status), complete = ["completed","needs_review"].includes(job.status);
  $("pause-button").hidden = !active; $("resume-button").hidden = active || complete;
  $("save-limits").disabled = active;
  $("usage-calls").textContent = number(job.usage.calls); $("usage-tokens").textContent = number(job.usage.total_tokens);
  const cacheNote = job.usage.cached_input_tokens ? " " + number(job.usage.cached_input_tokens) + " token di input serviti dalla cache del provider." : "";
  $("usage-note").textContent = "Limite: " + number(job.options.max_api_calls) + " chiamate. " + (job.usage.uncertain_calls ? number(job.usage.uncertain_calls) + " chiamate con consumo non confermato dal provider." : "Conteggi registrati dalle risposte API, inclusi i tentativi.") + cacheNote;
  if (lastLimitsJob !== job.id) { $("job-max-calls").value = job.options.max_api_calls; $("job-max-tokens").value = job.options.max_total_tokens; lastLimitsJob = job.id; }
  $("result-card").hidden = !complete;
  if (complete) {
    $("pdf-link").href = `/api/jobs/${id}/download/dispensa.pdf`; $("source-link").href = `/api/jobs/${id}/download/sorgenti.zip`; $("report-link").href = `/api/jobs/${id}/download/qualita.md`;
    $("result-description").textContent = job.options.mode === "demo" ? "Questa è una dimostrazione con contenuti prestabiliti. Puoi usarla per verificare il formato; non misura la qualità delle risposte live dei modelli." : (job.status === "needs_review" ? "Il PDF è disponibile. Prima di studiarlo, consulta i punti rimasti da verificare." : "I controlli automatici previsti sono conclusi. Confronta comunque la dispensa con il programma ufficiale.");
    $("quality-issues").replaceChildren();
    for (const issue of job.quality?.issues || []) { const li = document.createElement("li"); li.textContent = issue; $("quality-issues").append(li); }
    $("coverage").replaceChildren(); const c = job.quality?.coverage || {};
    for (const [value,label] of [[`${c.pages_analyzed ?? "–"}/${c.pages_total ?? "–"}`,"pagine lette"],[`${c.topics_in_lessons ?? "–"}/${c.topics_total ?? "–"}`,"argomenti tracciati"],[`${c.visuals_explained ?? "–"}/${c.visuals_total ?? "–"}`,"figure spiegate"]]) {
      const item = document.createElement("div"), n = document.createElement("strong"), l = document.createElement("span"); n.textContent = value; l.textContent = label; item.append(n,l); $("coverage").append(item);
    }
  }
  if (job.outline) {
    $("outline").replaceChildren();
    job.outline.forEach((chapter,i) => { const row=document.createElement("div"), n=document.createElement("span"), content=document.createElement("div"), heading=document.createElement("h3"), desc=document.createElement("p"); row.className="outline-item"; n.textContent=String(i+1).padStart(2,"0"); heading.textContent=chapter.title; desc.textContent=chapter.objectives.join(" · "); content.append(heading,desc); row.append(n,content); $("outline").append(row); });
  } else { $("outline").textContent = "L’indice compare dopo la lettura delle fonti."; }
  $("events").replaceChildren();
  for (const event of [...job.events].reverse()) { const li=document.createElement("li"), time=document.createElement("time"), text=document.createElement("span"); time.textContent=new Date(event.time*1000).toLocaleTimeString("it-IT",{hour:"2-digit",minute:"2-digit"}); text.textContent=event.message; li.append(time,text); $("events").append(li); }
}
$("files").onchange = event => { addFiles(event.target.files); event.target.value=""; };
$("dropzone").onkeydown = event => { if (["Enter"," "].includes(event.key)) { event.preventDefault(); $("files").click(); } };
for (const type of ["dragover","dragleave","drop"]) $("dropzone").addEventListener(type, event => { event.preventDefault(); $("dropzone").classList.toggle("dragover",type==="dragover"); if(type==="drop") addFiles(event.dataTransfer.files); });
$("project-form").onsubmit = event => {
  event.preventDefault(); guarded($("create-button"), async () => {
    if (!files.length) throw new Error("Aggiungi almeno un PDF per cominciare.");
    const data = new FormData();
    const options = {title:$("title").value.trim(),exam_brief:$("exam-brief").value,review_rounds:Number($("rounds").value),max_api_calls:Number($("max-calls").value),max_total_tokens:Number($("max-tokens").value),pages_per_batch:Number($("batch-pages").value)};
    data.append("options",JSON.stringify(options)); files.forEach(file=>data.append("files",file));
    const job=await api("/api/jobs",{method:"POST",body:data}); await showJob(job.id);
    await api(`/api/jobs/${job.id}/start`,{method:"POST"}); await refreshJob();
  });
};
$("demo-button").onclick = () => guarded($("demo-button"), async()=>{ const job=await api("/api/demo",{method:"POST"}); await showJob(job.id); await api(`/api/jobs/${job.id}/start`,{method:"POST"}); await refreshJob(); });
$("new-project").onclick = () => { selectedJob=null; localStorage.removeItem("sg-last-job"); $("welcome").hidden=false; $("create-view").hidden=false; $("job-view").hidden=true; $("breadcrumb").textContent="Nuova dispensa"; refreshHistory().catch(e=>toast(e.message)); };
$("open-settings").onclick = () => guarded($("open-settings"), async()=>{ applySettings(await api("/api/settings")); $("settings-dialog").showModal(); });
$("mobile-new").onclick = () => $("new-project").click();
$("mobile-history").onchange = event => { if (event.target.value) showJob(event.target.value).catch(e => toast(e.message)); };
$("close-settings").onclick = () => $("settings-dialog").close();
$("settings-form").onsubmit = event => { event.preventDefault(); guarded(event.submitter,async()=>{ await saveSettings(); $("settings-dialog").close(); toast("Impostazioni salvate."); }); };
$("check-connections").onclick = () => guarded($("check-connections"),async()=>{
  await saveSettings(); $("connection-result").textContent="Verifica dell’accesso e dei modelli disponibili…";
  const result=await api("/api/settings/check",{method:"POST"});
  $("connection-result").textContent=Object.entries(result).map(([provider,value])=>`${provider}: ${value.ok ? "accesso riuscito; modello presente nell’elenco" : value.error}`).join("\n")+"\nLa verifica dell’elenco non esegue una generazione.";
  for(const [provider,value] of Object.entries(result)) { const list=$(provider+"-models"); list.replaceChildren(); for(const model of value.models||[]) {const option=document.createElement("option");option.value=model;list.append(option);} }
});
$("clear-keys").onclick = () => guarded($("clear-keys"),async()=>{ await saveSettings(true); $("remember-keys").checked=false; $("connection-result").textContent="Chiavi rimosse dalla sessione e dal salvataggio dell’app. Rimuovi anche eventuali chiavi impostate tramite .env."; });
$("resume-button").onclick = () => guarded($("resume-button"),async()=>{await api(`/api/jobs/${selectedJob}/start`,{method:"POST"});await refreshJob();});
$("pause-button").onclick = () => guarded($("pause-button"),async()=>{await api(`/api/jobs/${selectedJob}/pause`,{method:"POST"});toast("Pausa richiesta. Il passaggio in corso verrà chiuso prima di fermarsi.");await refreshJob();});
$("save-limits").onclick = () => guarded($("save-limits"),async()=>{await api(`/api/jobs/${selectedJob}/limits`,{method:"POST",body:JSON.stringify({max_api_calls:Number($("job-max-calls").value),max_total_tokens:Number($("job-max-tokens").value)})});toast("Limiti aggiornati. Puoi riprendere il lavoro.");await refreshJob();});
async function init(){ const session=await api("/api/session");token=session.token;await refreshHealth();await refreshHistory();const last=localStorage.getItem("sg-last-job");if(last){try{await showJob(last);}catch{localStorage.removeItem("sg-last-job");$("new-project").click();}} }
init().catch(error=>toast(error.message));
setInterval(async()=>{ if(polling||!selectedJob)return;polling=true;try{await refreshJob();await refreshHistory();}catch(error){toast(error.message);}finally{polling=false;} },2500);
