"use strict";

// Los datos de las ideas viven en la API; el navegador solo mantiene el formulario.
const $ = (id) => document.getElementById(id);
const STATUS = {draft: "Idea", in_review: "En revisión", planned: "Planificada", done: "Completada"};
const RUN_STATE = {completed: "Completada", failed: "No completada", running: "En curso"};
const state = {ideas: [], detail: null, dirty: false, busy: false, generating: false, online: false, selectedRun: null, poll: null};
const fields = {title: $("idea-title"), description: $("idea-description"), transcription: $("transcription"), status: $("idea-status"), notes: $("idea-notes")};
const dateFormatter = new Intl.DateTimeFormat("es", {day: "numeric", month: "short", hour: "2-digit", minute: "2-digit"});
let toastTimer;

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function date(value) { return value ? dateFormatter.format(new Date(value)) : ""; }
function runs() { return state.detail?.runs || []; }
function selectedRun() { return runs().find((run) => run.id === state.selectedRun) || runs()[0]; }
function isRunning() { return runs().some((run) => run.state === "running"); }
function formData() { return Object.fromEntries(Object.entries(fields).map(([key, input]) => [key, input.value.trim()])); }

async function api(path, options = {}) {
  let response;
  try {
    response = await fetch(path, { ...options, headers: {"Content-Type": "application/json", ...options.headers} });
  } catch {
    updateConnection(false);
    throw new Error("No se puede conectar con el servidor. Comprueba que está en marcha y vuelve a intentarlo. El formulario sigue aquí.");
  }
  updateConnection(true);
  let data;
  try { data = await response.json(); } catch { data = {}; }
  if (!response.ok) {
    const message = typeof data.detail === "string" ? data.detail : response.status === 422
      ? "Revisa los campos: el nombre es obligatorio y la transcripción admite hasta 200.000 caracteres."
      : "No se ha podido completar la operación. Vuelve a intentarlo en un momento.";
    const error = new Error(message);
    error.status = response.status;
    throw error;
  }
  return data;
}

function updateConnection(online) {
  state.online = online;
  $("connection-text").textContent = online ? "Servidor conectado" : "Sin conexión";
  $("connection").classList.toggle("offline", !online);
}

function notice(message) {
  $("notice-text").textContent = message;
  $("notice").hidden = !message;
}

function toast(message) {
  clearTimeout(toastTimer);
  $("toast").textContent = message;
  $("toast").hidden = false;
  toastTimer = setTimeout(() => { $("toast").hidden = true; }, 4000);
}

function updateSaveState() {
  const saved = Boolean(state.detail) && !state.dirty;
  $("save-state").textContent = state.busy ? "Procesando…" : saved ? "✓ Cambios guardados" : state.detail ? "Cambios sin guardar" : "Borrador sin guardar";
  $("save-state").classList.toggle("saved", saved && !state.busy);
  $("character-count").textContent = `${fields.transcription.value.length.toLocaleString("es")} caracteres`;
}

function setBusy(value) {
  state.busy = value;
  const locked = value || isRunning();
  for (const input of Object.values(fields)) input.disabled = locked;
  for (const id of ["new-idea", "save-idea", "save-followup", "generate", "load-sample", "import-file", "file-input"]) $(id).disabled = locked;
  document.querySelectorAll(".idea-item").forEach((button) => { button.disabled = value; });
  $("generate").querySelector(".generate-label").textContent = state.generating || isRunning() ? "Generando…" : "Generar estimación";
  if ($("retry-estimation")) $("retry-estimation").disabled = locked;
  updateSaveState();
  if (!value) schedulePoll();
}

function setTab(name, focus = false) {
  document.querySelectorAll("[data-tab]").forEach((button) => {
    const active = button.dataset.tab === name;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
    button.tabIndex = active ? 0 : -1;
    $(button.getAttribute("aria-controls")).hidden = !active;
    if (active && focus) button.focus();
  });
}

function renderIdeas() {
  const query = $("search").value.trim().toLocaleLowerCase("es");
  const filter = $("status-filter").value;
  const visible = state.ideas.filter((idea) =>
    (filter === "all" || idea.status === filter) && `${idea.title} ${idea.description}`.toLocaleLowerCase("es").includes(query));
  $("idea-count").textContent = state.ideas.length;
  const list = $("idea-list");
  list.replaceChildren();
  if (!visible.length) {
    list.append(element("p", "sidebar-empty", state.ideas.length ? "No hay ideas que coincidan con tu búsqueda." : "Tu próxima idea empieza aquí. Crea una o carga la reunión de ejemplo."));
  }
  for (const idea of visible) {
    const button = element("button", `idea-item${state.detail?.id === idea.id ? " active" : ""}`);
    button.type = "button";
    button.disabled = state.busy;
    if (state.detail?.id === idea.id) button.setAttribute("aria-current", "true");
    button.append(element("span", "idea-item-title", idea.title));
    const info = element("span", "idea-item-info");
    const status = element("span", "idea-item-status", STATUS[idea.status]);
    status.dataset.status = idea.status;
    info.append(status, element("span", "", idea.run_count ? `${idea.run_count} ${idea.run_count === 1 ? "versión" : "versiones"}` : "Sin estimar"));
    button.append(info);
    button.addEventListener("click", () => openIdea(idea.id));
    list.append(button);
  }
}

async function refreshIdeas() {
  state.ideas = await api("/api/v1/ideas");
  renderIdeas();
}

function applyDetail(detail, fillForm = true) {
  state.detail = detail;
  if (fillForm) {
    for (const [key, input] of Object.entries(fields)) input.value = detail?.[key] || (key === "status" ? "draft" : "");
    state.dirty = false;
  }
  $("page-title").textContent = detail?.title || "Dale forma a tu próxima idea";
  $("page-subtitle").textContent = detail ? "Un lugar para revisar el alcance, comparar versiones y decidir el siguiente paso." : "Empieza con una reunión. Conviértela en una estimación que puedas seguir.";
  $("updated-at").textContent = detail ? `Actualizada: ${date(detail.updated_at)}` : "Guarda la idea para comenzar su historial.";
  if (!runs().some((run) => run.id === state.selectedRun)) state.selectedRun = runs()[0]?.id || null;
  renderIdeas();
  renderEstimation();
  renderHistory();
  setBusy(state.busy);
  schedulePoll();
}

function allowDiscard() { return !state.dirty || window.confirm("Tienes cambios sin guardar. ¿Quieres descartarlos y continuar?"); }

async function openIdea(id) {
  if (state.busy || state.detail?.id === id || !allowDiscard()) return;
  notice("");
  setBusy(true);
  try {
    const detail = await api(`/api/v1/ideas/${id}`);
    state.selectedRun = null;
    applyDetail(detail);
    setTab(detail.runs.length ? "estimation" : "prepare");
  } catch (error) { notice(error.message); }
  finally { setBusy(false); }
}

function newIdea() {
  if (state.busy || !allowDiscard()) return;
  notice("");
  clearTimeout(state.poll);
  state.selectedRun = null;
  applyDetail(null);
  setTab("prepare");
  fields.title.focus();
}

function validateForm(needsTranscription = false) {
  if (!fields.title.value.trim()) {
    setTab("prepare");
    fields.title.focus();
    throw new Error("Ponle un nombre a la idea antes de guardarla.");
  }
  if (needsTranscription && !fields.transcription.value.trim()) {
    setTab("prepare");
    fields.transcription.focus();
    throw new Error("Añade la transcripción de la reunión para generar una estimación.");
  }
}

async function persistIdea() {
  const payload = formData();
  let detail;
  if (state.detail) {
    detail = await api(`/api/v1/ideas/${state.detail.id}`, {method: "PATCH", body: JSON.stringify(payload)});
  } else {
    const {status, ...initial} = payload;
    detail = await api("/api/v1/ideas", {method: "POST", body: JSON.stringify(initial)});
    // Una nueva idea nace como borrador; conserva cualquier estado elegido en el formulario.
    applyDetail(detail, false);
    if (status !== "draft") detail = await api(`/api/v1/ideas/${detail.id}`, {method: "PATCH", body: JSON.stringify({status})});
  }
  applyDetail(detail);
  await refreshIdeas();
  return detail;
}

async function saveIdea() {
  if (state.busy || isRunning()) return;
  notice("");
  try {
    validateForm();
    setBusy(true);
    await persistIdea();
    toast("Idea y seguimiento guardados.");
  } catch (error) { notice(error.message); }
  finally { setBusy(false); }
}

async function generate() {
  if (state.busy || isRunning()) return;
  notice("");
  let id;
  try {
    validateForm(true);
    setBusy(true);
    const detail = await persistIdea();
    id = detail.id;
    state.generating = true;
    setTab("estimation");
    $("generation-progress").hidden = false;
    $("estimation-empty").hidden = true;
    $("generate").querySelector(".generate-label").textContent = "Generando…";
    const result = await api(`/api/v1/ideas/${id}/estimate`, {method: "POST"});
    state.selectedRun = null;
    applyDetail(result);
    toast("Estimación guardada. Ya puedes revisarla.");
  } catch (error) {
    notice(error.message);
    if (id) {
      try { state.selectedRun = null; applyDetail(await api(`/api/v1/ideas/${id}`)); } catch { /* Conserva los datos visibles y el aviso de conexión. */ }
    }
  } finally {
    state.generating = false;
    if (id) { try { await refreshIdeas(); } catch (error) { notice(error.message); } }
    setBusy(false);
    renderEstimation();
  }
}

function schedulePoll() {
  clearTimeout(state.poll);
  if (!isRunning() || state.busy) return;
  const id = state.detail.id;
  state.poll = setTimeout(async () => {
    if (state.detail?.id !== id || state.busy) return;
    try {
      const detail = await api(`/api/v1/ideas/${id}`);
      if (state.detail?.id !== id || state.busy) return;
      notice("");
      applyDetail(detail, false);
      if (!isRunning()) {
        await refreshIdeas();
        toast(detail.runs[0]?.state === "completed" ? "La estimación ya está disponible." : "La estimación no se completó. Puedes volver a intentarlo.");
      }
    } catch (error) {
      notice(error.message);
      schedulePoll();
    }
  }, 3000);
}

function renderEstimation() {
  const all = runs();
  const run = selectedRun();
  $("run-count").textContent = all.length;
  $("generation-progress").hidden = !(state.generating || isRunning());
  $("estimation-empty").hidden = Boolean(run) || state.generating || isRunning();
  $("estimation-content").hidden = !run;
  if (!run) return;
  const select = $("version-select");
  select.replaceChildren();
  all.forEach((item, index) => {
    const option = element("option", "", `Versión ${all.length - index} · ${date(item.started_at)}${item.state !== "completed" ? " · " + RUN_STATE[item.state] : ""}`);
    option.value = item.id;
    option.selected = item.id === run.id;
    select.append(option);
  });
  $("result-badge").textContent = RUN_STATE[run.state];
  $("result-badge").dataset.state = run.state;
  $("result-meta").textContent = [run.provider, run.model, date(run.finished_at || run.started_at)].filter(Boolean).join(" · ");
  $("copy-estimation").disabled = run.state !== "completed";
  $("download-estimation").disabled = run.state !== "completed";
  const body = $("result-body");
  body.replaceChildren();
  if (run.state === "completed") renderMarkdown(body, run.estimation || "");
  else if (run.state === "failed") {
    body.append(element("h2", "", "Esta versión no pudo completarse"), element("p", "", run.error || "Se ha producido un error al generar la estimación."));
    const retry = element("button", "button button-primary", "Volver a intentar");
    retry.id = "retry-estimation";
    retry.type = "button";
    retry.disabled = state.busy || isRunning();
    retry.addEventListener("click", generate);
    body.append(retry);
  } else body.append(element("p", "", "La estimación está en curso. El resultado aparecerá aquí al terminar."));
  $("snapshot-text").textContent = run.transcription;
}

function renderHistory() {
  const list = $("history-list");
  list.replaceChildren();
  if (!runs().length) list.append(element("p", "history-empty", "Cuando generes una estimación, aparecerá aquí con su fecha y la transcripción utilizada."));
  runs().forEach((run, index) => {
    const button = element("button", "history-item");
    button.type = "button";
    button.dataset.state = run.state;
    button.append(element("span", "history-dot", run.state === "completed" ? "✓" : run.state === "failed" ? "!" : "…"));
    const content = element("span", "");
    const badge = element("span", "status-badge", RUN_STATE[run.state]);
    badge.dataset.state = run.state;
    content.append(element("span", "history-title", `Versión ${runs().length - index}`), element("span", "history-time", date(run.started_at)), badge);
    button.append(content, element("span", "history-arrow", "↗"));
    button.addEventListener("click", () => { state.selectedRun = run.id; renderEstimation(); setTab("estimation", true); });
    list.append(button);
  });
}

// Markdown básico con nodos DOM: ni la transcripción ni la respuesta ejecutan HTML.
function inline(parent, text) {
  const pattern = /(\*\*([^*]+)\*\*|`([^`]+)`)/g;
  let position = 0;
  for (const match of text.matchAll(pattern)) {
    parent.append(document.createTextNode(text.slice(position, match.index)));
    parent.append(element(match[2] ? "strong" : "code", "", match[2] || match[3]));
    position = match.index + match[0].length;
  }
  parent.append(document.createTextNode(text.slice(position)));
}

function renderMarkdown(parent, source) {
  const lines = source.replace(/\r\n/g, "\n").split("\n");
  const tableCells = (line) => line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((cell) => cell.trim());
  let i = 0;
  while (i < lines.length) {
    const line = lines[i].trim();
    if (!line) { i++; continue; }
    if (line.startsWith("```")) {
      const codeLines = [];
      i++;
      while (i < lines.length && !lines[i].trim().startsWith("```")) codeLines.push(lines[i++]);
      const pre = element("pre");
      pre.append(element("code", "", codeLines.join("\n")));
      parent.append(pre);
      i++;
      continue;
    }
    const heading = /^(#{1,6})\s+(.+)$/.exec(line);
    if (heading) {
      const node = element(`h${heading[1].length}`);
      inline(node, heading[2]);
      parent.append(node);
      i++;
      continue;
    }
    if (/^(---+|\*\*\*+)\s*$/.test(line)) { parent.append(element("hr")); i++; continue; }
    if (line.includes("|") && i + 1 < lines.length && tableCells(lines[i + 1]).every((cell) => /^:?-{3,}:?$/.test(cell))) {
      const wrapper = element("div", "table-wrap");
      const table = element("table");
      const header = element("thead");
      const headerRow = element("tr");
      tableCells(line).forEach((cell) => { const th = element("th"); th.scope = "col"; inline(th, cell); headerRow.append(th); });
      header.append(headerRow);
      table.append(header);
      const body = element("tbody");
      i += 2;
      while (i < lines.length && lines[i].trim().includes("|")) {
        const row = element("tr");
        tableCells(lines[i++]).forEach((cell) => { const td = element("td"); inline(td, cell); row.append(td); });
        body.append(row);
      }
      table.append(body);
      wrapper.append(table);
      parent.append(wrapper);
      continue;
    }
    const listMatch = /^([-*+]\s+|\d+[.)]\s+)(.+)$/.exec(line);
    if (listMatch) {
      const ordered = /^\d/.test(listMatch[1]);
      const list = element(ordered ? "ol" : "ul");
      if (ordered) list.start = parseInt(listMatch[1], 10);
      while (i < lines.length) {
        const item = /^([-*+]\s+|\d+[.)]\s+)(.+)$/.exec(lines[i].trim());
        if (!item || /^\d/.test(item[1]) !== ordered) break;
        const li = element("li");
        inline(li, item[2]);
        list.append(li);
        i++;
      }
      parent.append(list);
      continue;
    }
    const paragraph = element("p");
    inline(paragraph, line);
    parent.append(paragraph);
    i++;
  }
}

async function loadSample() {
  if (state.busy || !allowDiscard()) return;
  notice("");
  setBusy(true);
  try {
    const sample = await api("/api/v1/sample-transcription");
    state.selectedRun = null;
    applyDetail(null);
    fields.title.value = sample.title;
    fields.description.value = sample.description;
    fields.transcription.value = sample.transcription;
    state.dirty = true;
    updateSaveState();
    setTab("prepare");
    toast("Reunión de ejemplo cargada. Pulsa «Generar estimación» para probarla.");
  } catch (error) { notice(error.message); }
  finally { setBusy(false); }
}

async function importFile(event) {
  const file = event.target.files[0];
  event.target.value = "";
  if (!file || state.busy) return;
  notice("");
  if (!/\.(txt|md)$/i.test(file.name)) { notice("Selecciona un archivo de texto (.txt) o Markdown (.md)."); return; }
  if (file.size > 1000000) { notice("El archivo es demasiado grande. Importa hasta 200.000 caracteres."); return; }
  if (fields.transcription.value.trim() && !window.confirm("¿Quieres sustituir la transcripción actual por la del archivo?")) return;
  setBusy(true);
  try {
    const text = await file.text();
    if (text.length > 200000) throw new Error("El archivo supera el límite de 200.000 caracteres.");
    if (text.includes("\u0000")) throw new Error("El archivo no parece ser texto UTF-8. Guárdalo como .txt o .md e inténtalo de nuevo.");
    fields.transcription.value = text;
    state.dirty = true;
    toast(`Transcripción importada: ${file.name}`);
  } catch (error) { notice(error.message || "No se ha podido leer el archivo."); }
  finally { setBusy(false); }
}

async function copyEstimation() {
  const text = selectedRun()?.estimation;
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
    toast("Estimación copiada.");
  } catch { notice("El navegador no permite copiar automáticamente. Puedes seleccionar el texto o descargar la estimación."); }
}

function downloadEstimation() {
  const run = selectedRun();
  if (!run?.estimation) return;
  const slug = (state.detail?.title || "idea").normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 80) || "idea";
  const url = URL.createObjectURL(new Blob([run.estimation + "\n"], {type: "text/markdown;charset=utf-8"}));
  const link = element("a");
  link.href = url;
  link.download = `${slug}-estimacion-v${runs().length - runs().indexOf(run)}.md`;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

for (const field of Object.values(fields)) field.addEventListener("input", () => { state.dirty = true; updateSaveState(); });
$("search").addEventListener("input", renderIdeas);
$("status-filter").addEventListener("change", renderIdeas);
$("new-idea").addEventListener("click", newIdea);
$("save-idea").addEventListener("click", saveIdea);
$("save-followup").addEventListener("click", saveIdea);
$("generate").addEventListener("click", generate);
$("load-sample").addEventListener("click", loadSample);
$("import-file").addEventListener("click", () => $("file-input").click());
$("file-input").addEventListener("change", importFile);
$("dismiss-notice").addEventListener("click", () => notice(""));
$("go-prepare").addEventListener("click", () => setTab("prepare", true));
$("go-followup").addEventListener("click", () => setTab("followup", true));
$("copy-estimation").addEventListener("click", copyEstimation);
$("download-estimation").addEventListener("click", downloadEstimation);
$("version-select").addEventListener("change", (event) => { state.selectedRun = event.target.value; renderEstimation(); });
document.querySelectorAll("[data-tab]").forEach((button) => {
  button.addEventListener("click", () => setTab(button.dataset.tab));
  button.addEventListener("keydown", (event) => {
    const names = ["prepare", "estimation", "followup"];
    let index = names.indexOf(button.dataset.tab);
    if (event.key === "ArrowRight") index = (index + 1) % names.length;
    else if (event.key === "ArrowLeft") index = (index + names.length - 1) % names.length;
    else if (event.key === "Home") index = 0;
    else if (event.key === "End") index = names.length - 1;
    else return;
    event.preventDefault();
    setTab(names[index], true);
  });
});
window.addEventListener("beforeunload", (event) => { if (state.dirty) { event.preventDefault(); event.returnValue = ""; } });

async function start() {
  setBusy(true);
  const results = await Promise.allSettled([api("/api/v1/meta"), refreshIdeas()]);
  const metadata = results[0];
  if (metadata.status === "fulfilled") {
    $("reference-count").textContent = `${metadata.value.reference_count} ejemplos de estimaciones`;
    $("model-name").textContent = `${metadata.value.provider} · ${metadata.value.model}`;
    state.online = results[1].status === "fulfilled";
  }
  updateConnection(state.online);
  if (!state.online) {
    notice(results.find((result) => result.status === "rejected")?.reason.message || "No se ha podido cargar la configuración.");
    if (results[1].status === "rejected") $("idea-list").replaceChildren(element("p", "sidebar-empty", "No se pudieron cargar las ideas. Recarga la página cuando el servidor esté disponible."));
  }
  setBusy(false);
}

start();
