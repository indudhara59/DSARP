"use strict";
/* DSARP web interface: plain JavaScript, no build step, no external libraries. */

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const num = (v, d = 2) => v == null || isNaN(v) ? "–" : Number(v).toFixed(d);
const int = v => v == null || isNaN(v) ? "–" : Number(v).toLocaleString("en");
const short = s => String(s || "").replace(/^org\.apache\./, "o.a.");
const when = t => t ? new Date(t * 1000).toLocaleString() : "–";
const dur = (a, b) => { if (!a) return "–"; const s = Math.round(((b || Date.now() / 1000) - a)); return s < 60 ? s + " s" : s < 3600 ? Math.floor(s / 60) + " min " + (s % 60) + " s" : Math.floor(s / 3600) + " h " + Math.floor(s % 3600 / 60) + " min"; };
const ACTIVE = ["queued", "pending", "running"];
const PAL = ["var(--c1)", "var(--c2)", "var(--c3)", "var(--c4)", "var(--c5)", "var(--c6)", "var(--c7)"];
const APPROACH = {hybrid: "var(--c1)", search_only: "var(--c4)", solver: "var(--c2)", nsga2: "var(--c3)", random: "var(--c7)", llm_only: "var(--c6)", hybrid_fixed_affinity: "var(--c5)"};
const STATUS_TEXT = {done: "done", todo: "not run", running: "running", pending: "waiting in Slurm queue", queued: "queued", failed: "failed", cancelled: "cancelled", lost: "lost (server restarted)"};

const S = {info: null, timer: null, logTimer: null, route: "", proj: null, sel: null, opts: {}, res: {}, mode: null, log: {id: null, off: 0, text: ""}, runAll: false};

async function api(path, opt = {}) {
  const o = {headers: {}, ...opt};
  if (o.body && typeof o.body !== "string") { o.body = JSON.stringify(o.body); o.headers["Content-Type"] = "application/json"; }
  const r = await fetch(path, o);
  if (r.status === 401) { location.href = "/"; throw new Error("not logged in"); }
  const ct = r.headers.get("Content-Type") || "";
  const data = ct.includes("json") ? await r.json() : await r.text();
  if (!r.ok) throw new Error(data.error || data || r.statusText);
  return data;
}
function toast(msg, bad = false) {
  const t = document.createElement("div"); t.className = "toast" + (bad ? " bad" : ""); t.textContent = msg;
  document.body.appendChild(t); setTimeout(() => t.remove(), bad ? 7000 : 3500);
}
const tag = st => `<span class="tag s-${esc(st)}">${esc(STATUS_TEXT[st] || st)}</span>`;
function stopTimers() { clearInterval(S.timer); clearInterval(S.logTimer); S.timer = S.logTimer = null; }

/* ---------- charts (HTML/SVG, no library) ---------- */
function bars(items, o = {}) {
  if (!items.length) return `<div class="muted small">No data.</div>`;
  const max = o.max ?? Math.max(...items.map(i => i.segs ? i.segs.reduce((a, s) => a + s.value, 0) : Math.abs(i.value)), 1e-9);
  const f = o.fmt || (v => int(v));
  return `<div class="bars" style="--lw:${o.lw || 170}px">` + items.map(i => {
    const segs = i.segs || [{value: i.value, color: i.color || "var(--a)", label: i.label}];
    const tot = segs.reduce((a, s) => a + s.value, 0);
    return `<div class="b" title="${esc(i.title || "")}"><span>${i.html || esc(i.label)}</span><div class="track">${segs.map(s =>
      `<i style="width:${Math.max(0, s.value / max * 100)}%;background:${s.color}" title="${esc(s.label)}: ${esc(f(s.value))}"></i>`).join("")}</div><span class="v">${f(i.total ?? tot)}</span></div>`;
  }).join("") + `</div>`;
}
function legend(items) { return `<div class="legend">${items.map(([l, c]) => `<span><i style="background:${c}"></i>${esc(l)}</span>`).join("")}</div>`; }
function scatter(pts, o) {
  if (!pts.length) return `<div class="muted small">No data.</div>`;
  const W = 560, H = 310, L = 48, B = 36, T = 34, Rr = 16;
  const xs = pts.map(p => p.x), ys = pts.map(p => p.y);
  const x0 = 0, x1 = Math.max(...xs) * 1.08 || 1, y0 = Math.min(0, ...ys), y1 = Math.max(1, ...ys) * 1.08;
  const X = x => L + (x - x0) / (x1 - x0) * (W - L - Rr), Y = y => H - B - (y - y0) / (y1 - y0) * (H - B - T);
  let s = `<svg viewBox="0 0 ${W} ${H}" width="100%" role="img" aria-label="${esc(o.title || "scatter")}">`;
  for (let k = 0; k <= 4; k++) { const yv = y0 + (y1 - y0) * k / 4, xv = x0 + (x1 - x0) * k / 4;
    s += `<line x1="${L}" x2="${W - Rr}" y1="${Y(yv)}" y2="${Y(yv)}" stroke="var(--line)"/><text x="${L - 6}" y="${Y(yv) + 4}" font-size="10.5" text-anchor="end" fill="var(--faint)">${num(yv, 2)}</text>`;
    s += `<text x="${X(xv)}" y="${H - B + 16}" font-size="10.5" text-anchor="middle" fill="var(--faint)">${num(xv, 0)}</text>`; }
  s += `<text x="${(L + W) / 2}" y="${H - 4}" font-size="11.5" text-anchor="middle" fill="var(--mute)">${esc(o.xlabel)}</text>`;
  s += `<text x="${L - 6}" y="14" font-size="11.5" fill="var(--mute)">${esc(o.ylabel)}</text>`;
  pts.forEach(p => { s += `<circle cx="${X(p.x)}" cy="${Y(p.y)}" r="${p.r || 6}" fill="${p.color}" fill-opacity=".75" stroke="${p.color}"><title>${esc(p.title)}</title></circle>`; });
  return s + `</svg>`;
}

/* ---------- router ---------- */
async function route() {
  stopTimers();
  const h = location.hash.replace(/^#\/?/, "");
  const p = h.split("/").map(decodeURIComponent);
  $$("#nav a").forEach(a => a.classList.toggle("on", a.dataset.r === (p[0] === "jobs" ? "jobs" : p[0] === "settings" ? "settings" : "projects")));
  try {
    if (!S.info) { S.info = await api("/api/info"); }
    $("#mode").textContent = "mode: " + S.info.settings.mode; $("#ver").textContent = "v" + S.info.version;
    if (!p[0]) return await viewProjects();
    if (p[0] === "new") return viewNew();
    if (p[0] === "edit") return viewNew(p[1]);
    if (p[0] === "settings") return viewSettings();
    if (p[0] === "jobs") return p[1] ? viewJob(p[1]) : viewJobs();
    if (p[0] === "p") {
      if (S.proj?.name !== p[1]) { S.sel = null; S.opts = {}; S.res = {}; S.runAll = false; }
      if (p[2] === "results") return viewResults(p[1]);
      if (p[2] === "group") return viewGroup(p[1], p[3]);
      if (p[2] === "jobs") return viewProjectJobs(p[1]);
      return viewPipeline(p[1]);
    }
    $("#view").innerHTML = `<div class="empty">Page not found.</div>`;
  } catch (e) { $("#view").innerHTML = `<div class="empty err">${esc(e.message)}</div>`; }
}
addEventListener("hashchange", route);

/* ---------- projects ---------- */
async function viewProjects() {
  const ps = await api("/api/projects");
  const order = S.info.stages.map(s => s.id);
  $("#view").innerHTML = `<div class="row" style="margin-bottom:18px"><div><h1>Projects</h1><div class="muted small">One project per repository config in <code>${esc(S.info.settings.configs_dir)}/</code>. Results go to <code>${esc(S.info.settings.results_dir)}/&lt;name&gt;</code>.</div></div><span class="sp"></span><a class="btn primary" href="#/new" style="background:var(--ink);color:var(--bg)">+ New project</a></div>
  ${ps.length ? `<div class="grid g3">${ps.map(p => {
    const done = order.filter(s => p.stages[s] === "done").length;
    const act = order.find(s => ACTIVE.includes(p.stages[s]));
    return `<a class="card proj" href="#/p/${encodeURIComponent(p.name)}"><div class="row"><h2 style="margin:0">${esc(p.name)}</h2><span class="sp"></span>${act ? tag(p.stages[act]) : ""}</div>
    <div class="muted small" style="margin-top:4px">${esc(p.repo_url || p.error || "")}</div>
    <div class="small"><span class="faint">commit</span> <span class="mono">${esc(String(p.commit || "–").slice(0, 10))}</span> · <span class="faint">build</span> ${esc(p.build || "auto")}</div>
    <div class="dots">${order.map(s => `<i class="${esc(p.stages[s])}" title="${esc(s)}: ${esc(STATUS_TEXT[p.stages[s]])}"></i>`).join("")}</div>
    <div class="small muted" style="margin-top:6px">${done} of ${order.length} stages done${act ? " · " + esc(act) + " " + esc(STATUS_TEXT[p.stages[act]]) : ""}</div></a>`; }).join("")}</div>`
    : `<div class="empty">No configs yet. <a href="#/new">Create the first project</a>.</div>`}`;
  S.timer = setInterval(() => { if (location.hash.replace(/^#\/?/, "") === "") viewProjects().catch(() => {}); }, 10000);
}

function viewNew(edit) {
  const load = edit ? api("/api/projects/" + encodeURIComponent(edit)) : Promise.resolve(null);
  load.then(pr => {
    const c = pr?.config || {};
    const dets = (c.detectors && c.detectors.length) ? c.detectors : [{tool: "arcan", path: ""}, {tool: "designite", path: ""}];
    $("#view").innerHTML = `<h1>${edit ? "Edit " + esc(edit) : "New project"}</h1>
    <p class="muted">A project is one repository at one commit, plus the folders where Arcan and Designite wrote their CSV files for that commit. Paths are relative to the data folder (<code>${esc(S.info.settings.data_root || "$DSARP_DATA")}</code>) or absolute.</p>
    <form id="newf" class="card" style="max-width:820px">
      <div class="grid g2"><div><label class="f">Name</label><input name="name" required pattern="[A-Za-z0-9][A-Za-z0-9_.-]*" value="${esc(c.name || "")}" ${edit ? "readonly" : ""} placeholder="karaf"><div class="help">Used for the results folder.</div></div>
      <div><label class="f">Build</label><select name="build">${["auto", "maven", "gradle"].map(b => `<option${(c.build || "auto") === b ? " selected" : ""}>${b}</option>`).join("")}</select></div></div>
      <label class="f">Repository URL</label><input name="repo_url" required value="${esc(c.repo_url || "")}" placeholder="https://github.com/apache/karaf">
      <label class="f">Commit hash</label><input name="commit" required class="mono" value="${esc(c.commit || "")}" placeholder="5f5677d7395170208907f2f1655ae9fd9b3bff9e"><div class="help">Arcan and Designite must have analysed exactly this commit.</div>
      <label class="f">Detector output folders</label><div id="dets"></div><button type="button" data-act="adddet" style="margin-top:6px">+ Add folder</button>
      <div class="help">Designite run per module? Add one row per module and name the module.</div>
      <label class="f">Protected API (optional)</label><input name="protected_api" value="${esc((c.protected_api || []).join(", "))}" placeholder="org.apache.tika.parser.Parser, org.example.api.*"><div class="help">Classes the planner must never move or change, comma-separated.</div>
      <div class="row" style="margin-top:18px"><button class="primary" type="submit">${edit ? "Save" : "Create project"}</button><a href="${edit ? "#/p/" + encodeURIComponent(edit) : "#/"}">Cancel</a><span id="nerr" class="err"></span></div>
    </form>`;
    const addRow = d => { const r = document.createElement("div"); r.className = "row det"; r.style.margin = "6px 0";
      r.innerHTML = `<select data-k="tool" style="width:130px"><option${d.tool === "arcan" ? " selected" : ""}>arcan</option><option${d.tool === "designite" ? " selected" : ""}>designite</option></select>
      <input data-k="path" placeholder="${d.tool === "designite" ? "Designite/Karaf/Main" : "Arcan/Karaf"}" value="${esc(String(d.path || "").replace(/^.*?\/(Arcan|Designite)\//, "$1/"))}" style="flex:1;width:auto"><input data-k="module" placeholder="module (optional)" value="${esc(d.module || "")}" style="width:170px"><button type="button" data-act="rmdet">Remove</button>`;
      $("#dets").appendChild(r); };
    dets.forEach(addRow); S.addDet = () => addRow({tool: "designite", path: ""});
    $("#newf").addEventListener("submit", async ev => {
      ev.preventDefault(); const f = new FormData(ev.target);
      const body = {name: f.get("name"), repo_url: f.get("repo_url"), commit: f.get("commit"), build: f.get("build"), overwrite: !!edit,
        protected_api: String(f.get("protected_api") || "").split(",").map(s => s.trim()).filter(Boolean),
        detectors: $$(".det").map(r => ({tool: $("[data-k=tool]", r).value, path: $("[data-k=path]", r).value, module: $("[data-k=module]", r).value})).filter(d => d.path)};
      try { await api("/api/projects", {method: "POST", body}); toast("Saved " + body.name); location.hash = "#/p/" + encodeURIComponent(body.name); }
      catch (e) { $("#nerr").textContent = e.message; }
    });
  }).catch(e => { $("#view").innerHTML = `<div class="empty err">${esc(e.message)}</div>`; });
}

/* ---------- project header ---------- */
function header(pr, tab) {
  const c = pr.config || {}, n = encodeURIComponent(pr.name);
  return `<div class="row"><div><div class="small faint"><a href="#/">Projects</a> / ${esc(pr.name)}</div><h1>${esc(pr.name)}</h1>
  <div class="small muted">${c.repo_url ? `<a href="${esc(c.repo_url)}" target="_blank" rel="noopener">${esc(c.repo_url)}</a>` : ""} · commit <span class="mono">${esc(String(c.commit || "").slice(0, 12))}</span> · config <code>${esc(pr.config_file)}</code> · results <code>${esc(pr.results)}</code></div></div>
  <span class="sp"></span><a class="btn" href="#/edit/${n}">Edit config</a></div>
  <div class="tabs"><a href="#/p/${n}" class="${tab === "pipe" ? "on" : ""}">Pipeline</a><a href="#/p/${n}/results" class="${tab === "res" ? "on" : ""}">Results</a><a href="#/p/${n}/jobs" class="${tab === "jobs" ? "on" : ""}">Jobs</a></div>`;
}

/* ---------- pipeline flowchart ---------- */
const POS = {check: [100, 66], checkout: [100, 200], ingest: [100, 334], model: [310, 267], triage: [520, 190], learn: [520, 380],
  plan: [735, 267], explain: [955, 86], verify: [955, 267], evaluate: [955, 448], report: [1165, 267]};
const EDGES = [["checkout", "model"], ["ingest", "model"], ["ingest", "triage"], ["model", "triage"], ["model", "learn"], ["checkout", "learn", 1],
  ["triage", "plan"], ["learn", "plan", 1], ["plan", "explain"], ["plan", "verify"], ["triage", "evaluate"], ["learn", "evaluate", 1], ["plan", "report"], ["verify", "report", 1]];
const COL = {done: ["var(--t-bg)", "var(--t)", "var(--t-ink)"], running: ["var(--b-bg)", "var(--b)", "var(--b-ink)"], pending: ["var(--w-bg)", "var(--w)", "var(--w-ink)"],
  queued: ["var(--w-bg)", "var(--w)", "var(--w-ink)"], failed: ["var(--r-bg)", "var(--r)", "var(--r-ink)"], lost: ["var(--r-bg)", "var(--r)", "var(--r-ink)"],
  cancelled: ["var(--soft)", "var(--faint)", "var(--mute)"], todo: ["var(--card)", "var(--line)", "var(--ink)"]};
function flowSVG(stages) {
  const by = Object.fromEntries(stages.map(s => [s.id, s]));
  const size = id => id === "plan" ? [196, 210] : [178, 86];
  let s = `<svg viewBox="0 0 1270 520" role="img" aria-label="Pipeline flowchart"><defs><marker id="ah" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M1 1L9 5L1 9" fill="none" stroke="context-stroke" stroke-width="1.6"/></marker></defs>`;
  for (const [a, b, opt] of EDGES) {
    const [ax, ay] = POS[a], [bx, by_] = POS[b], [aw] = size(a), [bw] = size(b);
    const x1 = ax + aw / 2, x2 = bx - bw / 2 - 4, mx = (x1 + x2) / 2;
    const src = by[a]?.status, dst = by[b]?.status;
    const col = src === "done" ? "var(--t)" : "var(--faint)";
    const live = src === "done" && ACTIVE.includes(dst);
    s += `<path d="M${x1} ${ay} C${mx} ${ay} ${mx} ${by_} ${x2} ${by_}" fill="none" stroke="${live ? "var(--b)" : col}" stroke-width="${src === "done" ? 2 : 1.4}" ${opt ? 'stroke-dasharray="5 5"' : ""} class="${live ? "live" : ""}" marker-end="url(#ah)"/>`;
  }
  for (const st of stages) {
    const [cx, cy] = POS[st.id], [w, h] = size(st.id), [fill, stroke, ink] = COL[st.status] || COL.todo;
    const x = cx - w / 2, y = cy - h / 2, sel = S.sel === st.id;
    const icon = {done: "✓", running: "●", pending: "◷", queued: "◷", failed: "✕", lost: "!", cancelled: "–", todo: st.ready ? "▷" : ""}[st.status] || "";
    s += `<g class="node ${esc(st.status)} ${sel ? "sel" : ""}" data-act="sel" data-stage="${st.id}" tabindex="0" role="button" aria-label="${esc(st.label)}: ${esc(STATUS_TEXT[st.status])}">
      <rect class="box" x="${x}" y="${y}" width="${w}" height="${h}" rx="12" fill="${fill}" stroke="${sel ? "var(--ink)" : stroke}" stroke-width="${sel ? 3 : 1.4}"/>
      <text x="${x + 12}" y="${y + 22}" font-size="14" fill="${ink}" opacity=".75" style="font-family:var(--mono)">stage ${esc(st.number)}</text>
      <text x="${x + w - 12}" y="${y + 24}" font-size="18" text-anchor="end" fill="${ink}">${icon}</text>
      <text x="${x + 12}" y="${y + 49}" font-size="19" font-weight="600" fill="${ink}">${esc(st.label)}</text>
      <text x="${x + 12}" y="${y + 73}" font-size="15" fill="${ink}" opacity=".85">${esc(STATUS_TEXT[st.status])}${st.job && ACTIVE.includes(st.status) && st.job.started ? " · " + dur(st.job.started) : ""}</text>`;
    if (st.inner.length) st.inner.forEach((t, i) => { s += `<rect x="${x + 12}" y="${y + 88 + i * 29}" width="${w - 24}" height="24" rx="6" fill="var(--card)" opacity=".7"/><text x="${x + 22}" y="${y + 105 + i * 29}" font-size="14.5" fill="${ink}">${esc(t)}</text>`; });
    s += `</g>`;
  }
  return s + `</svg>`;
}

async function viewPipeline(name) {
  const pr = await api("/api/projects/" + encodeURIComponent(name));
  S.proj = pr; S.mode = S.mode || S.info.settings.mode;
  if (!S.sel) S.sel = (pr.stages.find(s => ACTIVE.includes(s.status)) || pr.stages.find(s => s.status !== "done" && s.ready) || pr.stages[0]).id;
  $("#view").innerHTML = header(pr, "pipe") + `
  <div class="row" style="margin-bottom:12px"><span class="muted small">Run on</span>
    <select id="modeSel" style="width:auto">${S.info.modes.map(m => `<option value="${m}"${m === S.mode ? " selected" : ""}>${{local: "this machine (local)", container: "this machine, in the container", slurm: "Slurm job (HPC)"}[m]}</option>`).join("")}</select>
    <span class="sp"></span><button data-act="runall" class="primary">Run pipeline…</button></div>
  <div id="runall"></div>
  <div class="pipe"><div><div class="card flow" id="flow"></div>
  ${legend([["done", "var(--t)"], ["running", "var(--b)"], ["waiting", "var(--w)"], ["failed", "var(--r)"], ["not run", "var(--line)"]])}
  <div class="small muted" style="margin-top:6px">Click a stage to configure and run it. Dashed arrows are optional inputs (used when present).</div></div>
  <div class="panel" id="panel"></div></div>`;
  $("#modeSel").addEventListener("change", e => { S.mode = e.target.value; renderPanel(true); if (S.runAll) renderRunAll(); });
  drawPipeline(true);
  S.timer = setInterval(refreshPipeline, 3000);
}
async function refreshPipeline() {
  if (!location.hash.startsWith("#/p/") || location.hash.split("/").length > 3) return;
  try { S.proj = await api("/api/projects/" + encodeURIComponent(S.proj.name)); drawPipeline(false); } catch (e) { /* keep last view */ }
}
function drawPipeline(full) {
  $("#flow").innerHTML = flowSVG(S.proj.stages);
  renderPanel(full);
}
function stageOf(id) { return S.proj.stages.find(s => s.id === id); }

function optInput(st, o) {
  const v = S.opts[st.id]?.[o.name] ?? o.default;
  const id = `o-${st.id}-${o.name}`;
  if (o.kind === "bool") return `<label class="row small" style="margin:8px 0"><input type="checkbox" id="${id}" data-opt="${o.name}"${v ? " checked" : ""}> <span><code>${esc(o.flag)}</code> ${esc(o.help)}</span></label>`;
  const inp = o.kind === "choice" ? `<select id="${id}" data-opt="${o.name}">${o.choices.map(c => `<option${c === v ? " selected" : ""}>${esc(c)}</option>`).join("")}</select>`
    : `<input id="${id}" data-opt="${o.name}" ${o.kind === "text" ? "" : `type="number" step="${o.kind === "int" ? 1 : "any"}"`} value="${esc(v ?? "")}" ${o.min != null ? `min="${o.min}"` : ""}>`;
  return `<label class="f" for="${id}"><code>${esc(o.flag)}</code> ${esc(o.help)}</label>${inp}`;
}
function renderPanel(full) {
  const st = stageOf(S.sel); if (!st) return;
  const p = $("#panel");
  if (full || !$("#pform", p) || p.dataset.stage !== st.id || p.dataset.mode !== S.mode) {
    p.dataset.stage = st.id; p.dataset.mode = S.mode;
    const r = {...st.resources, ...(S.res[st.id] || {})};
    p.innerHTML = `<div class="card"><div class="row"><span class="tag pu mono">stage ${esc(st.number)}</span><h2 style="margin:0">${esc(st.label)}</h2><span class="sp"></span><span id="pstat"></span></div>
    <p class="muted small" style="margin:6px 0 0">${esc(st.summary)}</p>
    <div class="small" style="margin-top:8px"><span class="faint">needs</span> ${st.requires.length ? st.requires.map(x => `<a href="#" data-act="sel" data-stage="${x}">${esc(x)}</a>`).join(", ") : "nothing"}${st.optional_requires.length ? ` <span class="faint">· uses if present</span> ${st.optional_requires.map(esc).join(", ")}` : ""}</div>
    <div class="small"><span class="faint">writes</span> ${st.outputs.length ? st.outputs.map(o => `<code>${esc(o)}</code>`).join(" ") : "nothing (prints a check)"}</div>
    <form id="pform">${st.options.map(o => optInput(st, o)).join("")}
    ${S.mode === "slurm" ? `<details style="margin-top:12px" open><summary>Slurm resources</summary><div class="grid g3" style="gap:8px">
      <div><label class="f">CPUs</label><input data-res="cpus" type="number" min="1" max="128" value="${esc(r.cpus)}"></div>
      <div><label class="f">Memory</label><input data-res="mem" value="${esc(r.mem)}"></div>
      <div><label class="f">Time limit</label><input data-res="time" value="${esc(r.time)}"></div></div></details>` : ""}
    <label class="f">Command</label><pre id="pcmd" style="white-space:pre-wrap">${esc(st.command)}</pre>
    <div class="row" style="margin-top:12px"><button class="primary" type="submit" id="prun">Run ${esc(st.label)}</button><span id="pwarn" class="small" style="color:var(--w)"></span></div></form></div>
    <div class="card" style="margin-top:12px" id="pjob"></div>`;
    $("#pform").addEventListener("input", onForm); $("#pform").addEventListener("change", onForm);
    $("#pform").addEventListener("submit", e => { e.preventDefault(); runStages([st.id]); });
    S.log = {id: null, off: 0, text: ""}; onForm();
  }
  $("#pstat").innerHTML = tag(st.status);
  $("#pwarn").textContent = st.missing.length ? `Not done yet: ${st.missing.join(", ")}. It will probably fail.` : "";
  $("#prun").disabled = ACTIVE.includes(st.status);
  renderJob(st);
}
let previewT = null;
function onForm() {
  const st = stageOf(S.sel); const vals = {}; const res = {};
  $$("#pform [data-opt]").forEach(i => { vals[i.dataset.opt] = i.type === "checkbox" ? i.checked : i.value; });
  $$("#pform [data-res]").forEach(i => { res[i.dataset.res] = i.value; });
  S.opts[st.id] = vals; if (Object.keys(res).length) S.res[st.id] = res;
  clearTimeout(previewT);
  previewT = setTimeout(async () => {
    try { const r = await api(`/api/projects/${encodeURIComponent(S.proj.name)}/preview`, {method: "POST", body: {stage: st.id, options: vals}});
      $("#pcmd").textContent = r.command; $("#pcmd").style.color = ""; }
    catch (e) { $("#pcmd").textContent = e.message; $("#pcmd").style.color = "var(--r)"; }
  }, 250);
}
function renderJob(st) {
  const box = $("#pjob"), j = st.job;
  if (!j) { box.innerHTML = `<div class="muted small">This stage hasn't been run from the web interface yet.${st.outputs_state.exists ? " Its outputs exist already (run from the command line?)." : ""}</div>`; stopLog(); return; }
  if (S.log.id !== j.id || !$("#plog", box)) {
    box.innerHTML = `<div class="row"><h3 style="margin:0">Last run</h3><span id="jst"></span><span class="sp"></span><a class="small" href="#/jobs/${esc(j.id)}">job ${esc(j.id)}</a></div>
      <div class="small muted" id="jmeta" style="margin:6px 0"></div><div id="jerr" class="err"></div>
      <div class="row" style="margin:6px 0"><button class="danger" data-act="cancel" data-job="${esc(j.id)}" id="jcancel">Cancel</button><label class="small row"><input type="checkbox" id="follow" checked> follow log</label></div>
      <pre class="log" id="plog"></pre>`;
    S.log = {id: j.id, off: 0, text: ""}; pollLog(true);
  }
  $("#jst").innerHTML = tag(j.status);
  $("#jmeta").innerHTML = `${esc(j.mode)}${j.slurm_id ? " · Slurm " + esc(j.slurm_id) : ""} · started ${when(j.started || j.created)} · ${ACTIVE.includes(j.status) ? "running for " + dur(j.started) : "took " + dur(j.started, j.ended)}${j.returncode != null ? " · exit " + j.returncode : ""}`;
  $("#jerr").textContent = j.error || "";
  $("#jcancel").style.display = ACTIVE.includes(j.status) ? "" : "none";
  if (ACTIVE.includes(j.status) && !S.logTimer) S.logTimer = setInterval(() => pollLog(false), 2000);
  if (!ACTIVE.includes(j.status) && S.logTimer) { stopLog(); pollLog(false); }
}
function stopLog() { clearInterval(S.logTimer); S.logTimer = null; }
async function pollLog(first) {
  const id = S.log.id; if (!id || (S.log.busy && !first)) return;
  S.log.busy = true;
  try {
    const r = await api(`/api/jobs/${encodeURIComponent(id)}/log?offset=${S.log.off}`);
    if (id !== S.log.id) return;
    if (r.text) { S.log.text = (first ? "" : S.log.text) + r.text; if (S.log.text.length > 300000) S.log.text = S.log.text.slice(-300000); }
    S.log.off = r.offset;
    const pre = $("#plog"); if (!pre) return;
    pre.textContent = S.log.text || (ACTIVE.includes(r.status) ? "Waiting for output…" : "(no output)");
    if ($("#follow")?.checked) pre.scrollTop = pre.scrollHeight;
  } catch (e) { /* ignore */ } finally { if (id === S.log.id) S.log.busy = false; }
}
async function runStages(ids) {
  const body = {stages: ids, mode: S.mode, options: {}, resources: {}};
  ids.forEach(i => { if (S.opts[i]) body.options[i] = S.opts[i]; if (S.res[i]) body.resources[i] = S.res[i]; });
  try {
    const r = await api(`/api/projects/${encodeURIComponent(S.proj.name)}/run`, {method: "POST", body});
    toast(r.jobs.length > 1 ? `Started ${r.jobs.length} stages in a chain` : `Started ${r.jobs[0].stage}` + (r.jobs[0].slurm_id ? ` (Slurm ${r.jobs[0].slurm_id})` : ""));
    const failed = r.jobs.find(j => j.status === "failed"); if (failed) toast(failed.error, true);
    S.runAll = false; $("#runall").innerHTML = ""; S.log.id = null; await refreshPipeline(); renderPanel(true);
  } catch (e) { toast(e.message, true); }
}
function renderRunAll() {
  const box = $("#runall");
  if (!S.runAll) { box.innerHTML = ""; return; }
  const skip = new Set(["check", "explain", "evaluate"]);
  const sts = S.proj.stages;
  box.innerHTML = `<div class="card" style="margin-bottom:14px"><h2>Run several stages in a chain</h2>
    <p class="small muted" style="margin-top:0">Each stage starts when the previous one finished successfully (${S.mode === "slurm" ? "Slurm <code>--dependency=afterok</code>" : "one after the other on this machine"}). Options are taken from each stage's form (defaults if you haven't opened it). <code>explain</code> is off because <code>plan --explain</code> already writes the report.</p>
    <div class="row">${sts.map(s => `<label class="chip" style="cursor:pointer"><input type="checkbox" data-ra="${s.id}" ${s.status !== "done" && !skip.has(s.id) ? "checked" : ""}> ${esc(s.label)} ${s.status === "done" ? '<span class="faint">(done)</span>' : ""}</label>`).join("")}</div>
    <div class="row" style="margin-top:10px"><button class="primary" data-act="runallgo">Start chain</button><button data-act="runallx">Close</button><button data-act="raall">Select all</button><button data-act="ranone">None</button></div></div>`;
}

/* ---------- results dashboard ---------- */
async function viewResults(name) {
  const [pr, ov] = await Promise.all([api("/api/projects/" + encodeURIComponent(name)), api(`/api/projects/${encodeURIComponent(name)}/overview`)]);
  S.proj = pr; const n = encodeURIComponent(name), A = ov.available || {};
  let h = header(pr, "res");
  if (!A.ingest) { $("#view").innerHTML = h + `<div class="empty">No results yet. Run the pipeline first (<a href="#/p/${n}">Pipeline</a>).</div>`; return; }
  const ver = ov.verify || [], tri = ov.triage || {}, pl = ov.plans || {};
  const compiles = ver.filter(v => v.compiles).length, removed = ver.filter(v => v.target_removed).length;
  h += `<div class="row" style="margin-bottom:14px"><span class="muted small">${ov.repo ? `${esc(ov.repo.name)} @ <span class="mono">${esc(String(ov.repo.commit).slice(0, 12))}</span>` : ""}</span><span class="sp"></span>
    ${A.report ? `<a class="btn" href="/api/projects/${n}/file/report.html" target="_blank" rel="noopener">Open report.html</a>` : ""}
    ${A.report_md ? `<a class="btn" href="/api/projects/${n}/file/plans/report.md" target="_blank" rel="noopener">report.md</a>` : ""}</div>
  <div class="kpis">
    <div class="kpi"><b>${int(ov.smells?.total)}</b><span>smells reported</span></div>
    <div class="kpi"><b>${int(tri.decisions?.keep)}</b><span>kept after triage</span></div>
    <div class="kpi"><b>${int(tri.groups)}</b><span>groups</span></div>
    <div class="kpi"><b>${int(pl.planned)}</b><span>groups with a plan</span></div>
    <div class="kpi"><b>${ver.length ? compiles + " / " + ver.length : "–"}</b><span>verified plans compile</span></div>
    <div class="kpi"><b>${ver.length ? removed + " / " + ver.length : "–"}</b><span>smell removed in real code</span></div></div>`;
  const funnel = [["reported", ov.smells?.total], ["kept", tri.decisions?.keep], ["groups", tri.groups], ["planned", pl.planned], ["verified, compiles", ver.length ? compiles : null]].filter(x => x[1] != null);
  h += `<div class="grid g2 sec"><div class="card"><h2>Funnel</h2>${bars(funnel.map((f, i) => ({label: f[0], value: f[1], color: PAL[i % PAL.length]})), {lw: 130})}</div>`;
  const types = [...new Set((ov.smells?.by_tool_type || []).map(r => r.type))];
  const tools = [...new Set((ov.smells?.by_tool_type || []).map(r => r.tool))];
  h += `<div class="card"><h2>Reported smells by type</h2>${bars(types.map(t => ({label: t.replace(/_/g, " "), value: 0, segs: tools.map((tl, k) => ({label: tl, value: (ov.smells.by_tool_type.find(r => r.type === t && r.tool === tl) || {}).n || 0, color: PAL[k]}))})), {lw: 170})}${legend(tools.map((t, k) => [t, PAL[k]]))}</div></div>`;
  if (A.triage) {
    h += `<div class="grid g2 sec"><div class="card"><h2>Why smells were dropped</h2>${bars((tri.by_reason || []).map(r => ({label: `${r.tool}: ${r.reason}`, value: r.n, color: "var(--c3)", title: r.reason})), {lw: 260})}</div>
    <div class="card"><h2>Groups by type</h2>${bars(Object.entries(tri.groups_by_type || {}).sort((a, b) => b[1] - a[1]).map(([k, v]) => ({label: k.replace(/_/g, " ").replace(":", " · "), value: v, color: "var(--c1)"})), {lw: 230})}</div></div>`;
  }
  if (A.model) {
    const m = ov.model;
    h += `<div class="sec"><h2>Program model <small>stage 2</small></h2><div class="kpis">${[["types", m.types], ["dependencies", m.dependencies], ["members", m.members], ["packages", m.packages], ["build modules", m.build_modules], ["public API types", m.api_types], ["history commits", m.history_commits], ["co-change pairs", m.cochange_pairs]].map(([l, v]) => `<div class="kpi"><b>${int(v)}</b><span>${l}</span></div>`).join("")}</div></div>`;
  }
  if (A.learn) {
    const L = ov.learn, ev = L.evaluation || {};
    const names = {learned: "learned", fixed_weights: "fixed weights", structural_only: "structure only"};
    h += `<div class="grid g2 sec"><div class="card"><h2>Learned affinity weights <small class="muted">${int(L.examples)} examples</small></h2>${bars((L.features || []).map((f, i) => ({label: f.replace(/_/g, " "), value: L.coef[i], color: L.coef[i] >= 0 ? "var(--c1)" : "var(--c3)"})), {fmt: v => num(v), lw: 140})}</div>
    <div class="card"><h2>Does learning help? <small>5-fold top-1 accuracy</small></h2>${bars(Object.entries(ev).map(([k, v], i) => ({label: names[k] || k, value: v.top1, color: i ? "var(--c7)" : "var(--c1)"})), {max: 1, fmt: v => num(v, 3), lw: 140})}
    <div class="small muted" style="margin-top:8px">On real historical moves only: ${Object.entries(ev).map(([k, v]) => `${names[k] || k} ${num(v.move_top1, 2)}`).join(" · ")}</div></div></div>`;
  }
  if (A.plan && pl.by_kind) {
    const kinds = Object.entries(pl.by_kind).sort((a, b) => b[1].groups - a[1].groups);
    h += `<div class="card sec"><h2>Recommendations per smell type <small>groups planned in this run</small></h2>${bars(kinds.map(([k, v]) => ({
      label: k.replace(/_/g, " ").replace(":class", " (class)"), value: 0, total: `${v.full + v.partial}/${v.groups}`,
      segs: [{value: v.full, color: "var(--c2)", label: "smell removed"}, {value: v.partial, color: "var(--c5)", label: "partly removed"},
             {value: v.none, color: "var(--c7)", label: "no safe plan"}]})), {lw: 230, fmt: v => v})}
      ${legend([["smell removed", "var(--c2)"], ["partly removed", "var(--c5)"], ["no safe plan (reason on the group page)", "var(--c7)"]])}</div>`;
  }
  if (A.plan) {
    const rows = (pl.groups || []).filter(r => r.recommended);
    const planNames = [...new Set(rows.map(r => r.recommended))], pc = Object.fromEntries(planNames.map((p, i) => [p, PAL[i % PAL.length]]));
    h += `<div class="grid g2 sec"><div class="card"><h2>Score of the recommended plan per group</h2>${bars(rows.map(r => ({html: `<a href="#/p/${n}/group/${esc(r.group_id)}" class="mono">${esc(r.group_id)}</a>`, value: Math.max(0, r.score || 0), total: r.score, color: pc[r.recommended], title: r.recommended})), {max: 1, fmt: v => num(v, 3), lw: 150})}${legend(planNames.map(p => [p, pc[p]]))}</div>
    <div class="card"><h2>Benefit vs change cost</h2>${scatter(rows.map(r => ({x: r.cost || 0, y: r.benefit || 0, r: 4 + Math.min(10, r.operations), color: (r.new_smells || 0) > 0 ? "var(--c3)" : "var(--c2)", title: `${r.group_id}: benefit ${num(r.benefit)}, cost ${num(r.cost, 1)}, ${r.operations} operations, new smells ${r.new_smells}`})), {xlabel: "change cost", ylabel: "benefit (1 = smell gone)"})}
    ${legend([["no new smells", "var(--c2)"], ["creates new smells", "var(--c3)"]])}<div class="small muted">Dot size = number of operations.</div></div></div>`;
  }
  if (A.plan) {
    const vb = Object.fromEntries(ver.map(v => [v.group_id, v]));
    h += `<div class="card sec"><h2>All groups <small>click a row for the step-by-step recommendation</small></h2><div style="overflow:auto"><table><tr><th>rank</th><th>group</th><th>type</th><th>priority</th><th>recommended</th><th>steps</th><th>score</th><th>new smells</th><th>verified</th></tr>
    ${(pl.groups || []).map(r => { const v = vb[r.group_id]; return `<tr class="click" data-act="go" data-href="#/p/${n}/group/${esc(r.group_id)}"><td>${int(r.rank)}</td><td class="mono">${esc(r.group_id)}</td><td>${esc((r.kind || r.type || "").replace(/_/g, " ").replace(":class", " (class)"))}</td><td>${num(r.priority, 3)}</td>
      <td>${r.recommended ? `<span class="tag pu">${esc(r.recommended)}</span>` : `<span class="small muted" title="${esc(r.skipped || "")}">${esc(String(r.skipped || "no plan").slice(0, 70))}</span>`}</td><td>${r.recommended ? r.operations : ""}</td><td>${r.recommended ? num(r.score, 3) : ""}</td><td>${r.recommended ? num(r.new_smells, 0) : ""}</td>
      <td>${v ? (v.compiles ? `<span class="tag s-done">compiles</span>` : `<span class="tag s-failed">fails</span>`) + (v.target_removed ? ` <span class="tag s-done">removed</span>` : "") : ""}</td></tr>`; }).join("")}</table></div></div>`;
  }
  if (ver.length) {
    h += `<div class="grid g2 sec"><div class="card"><h2>Verification: size of each patch</h2>${bars(ver.map(v => ({html: `<a class="mono" href="#/p/${n}/group/${esc(v.group_id)}">${esc(v.group_id)}</a>`, value: v.patch_lines || 0, color: v.compiles ? "var(--c2)" : "var(--c3)"})), {lw: 150})}<div class="small muted">Patch lines; green = compiles.</div></div>
    <div class="card"><h2>Verification: steps kept by delta debugging</h2>${bars(ver.map(v => ({html: `<span class="mono">${esc(v.group_id)}</span>`, value: v.operations || 0, total: `${v.kept}/${v.operations}`, segs: [{value: v.kept || 0, color: "var(--c2)", label: "kept"}, {value: (v.operations || 0) - (v.kept || 0), color: "var(--c3)", label: "dropped"}]})), {lw: 150, fmt: v => v})}${legend([["kept", "var(--c2)"], ["dropped", "var(--c3)"]])}</div></div>`;
  }
  h += compareSection(ov, n);
  $("#view").innerHTML = h;
}


/* ---------- optional comparison with other approaches (hidden by default) ---------- */
const OWN = "hybrid";
const ALABEL = {hybrid: "DSARP (hybrid)", solver: "solver only", search_only: "search only", nsga2: "NSGA-II", random: "random search",
  llm_only: "LLM only", hybrid_fixed_affinity: "DSARP, fixed affinity (ablation)"};
const store = {get(k, d) { try { const v = localStorage.getItem("dsarp." + k); return v == null ? d : JSON.parse(v); } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem("dsarp." + k, JSON.stringify(v)); } catch (e) { /* storage unavailable */ } }};
S.compare = store.get("compare", false);
S.cmpSel = store.get("cmpSel", null);
function compareSection(ov, n) {
  if (!ov.available?.evaluate) return "";
  const E = ov.evaluation, all = (E.summary || []).map(r => r.approach).filter(a => a !== OWN);
  if (!S.compare) {
    return `<div class="card sec row"><div><h2 style="margin:0">Compare with other approaches</h2><div class="small muted">Results of the same groups with ${all.map(a => esc(ALABEL[a] || a)).join(", ")}. Hidden so the page shows only DSARP's recommendations.</div></div><span class="sp"></span><button data-act="cmp">Show comparison</button></div>`;
  }
  const sel = new Set(S.cmpSel || all);
  const aps = [OWN, ...all.filter(a => sel.has(a))];
  const sm = aps.map(a => (E.summary || []).find(r => r.approach === a)).filter(Boolean);
  const lab = a => ALABEL[a] || a.replace(/_/g, " ");
  let h = `<div class="sec"><div class="row"><h2 style="margin:0">Comparison with other approaches</h2><span class="sp"></span><button data-act="cmp">Hide comparison</button></div>
  <div class="row" style="margin:10px 0"><span class="small muted">Compare DSARP with:</span>${all.map(a => `<label class="chip" style="cursor:pointer"><input type="checkbox" data-act="cmpap" data-ap="${esc(a)}"${sel.has(a) ? " checked" : ""}> ${esc(lab(a))}</label>`).join("")}</div>
  <div class="small muted" style="margin-bottom:10px">Same groups, same whole-repo simulation, same score. Only DSARP (hybrid) produces the recommendations above; the others are baselines for the evaluation.</div>`;
  h += `<div class="grid g2"><div class="card"><h2>Mean score per approach</h2>${bars(sm.map(r => ({label: lab(r.approach), value: r.mean_score, color: r.approach === OWN ? "var(--c1)" : "var(--c7)"})), {fmt: v => num(v, 3), lw: 210})}</div>
  <div class="card"><h2>New smells per plan <small class="muted">lower is better</small></h2>${bars(sm.map(r => ({label: lab(r.approach), value: r.mean_new_smells || 0, color: r.approach === OWN ? "var(--c1)" : "var(--c3)"})), {fmt: v => num(v, 2), lw: 210})}</div></div>`;
  const gids = [...new Set((E.per_group || []).map(r => r.group_id))];
  if (gids.length) {
    const sc = (g, a) => (E.per_group.find(r => r.group_id === g && r.approach === a) || {}).score;
    const cell = (v, own) => { if (v == null) return `<td class="faint">–</td>`; const a = Math.max(0, Math.min(1, v)); return `<td style="background:color-mix(in srgb, var(--t) ${Math.round(a * 55)}%, transparent)${own ? ";font-weight:600" : ""}">${num(v, 2)}</td>`; };
    const vs = a => { let w = 0, l = 0; gids.forEach(g => { const d = (sc(g, OWN) ?? 0) - (sc(g, a) ?? 0); if (d > 1e-3) w++; else if (d < -1e-3) l++; }); return [w, l, gids.length - w - l]; };
    h += `<div class="card sec"><h2>DSARP against each approach <small>groups where DSARP scores higher / lower / equal</small></h2>${bars(all.filter(a => sel.has(a)).map(a => { const [w, l, t] = vs(a);
      return {label: "vs " + lab(a), value: 0, total: `${w} / ${l} / ${t}`, segs: [{value: w, color: "var(--c2)", label: "DSARP better"}, {value: t, color: "var(--c7)", label: "equal"}, {value: l, color: "var(--c3)", label: "DSARP worse"}]}; }), {lw: 230, fmt: v => v})}
      ${legend([["DSARP better", "var(--c2)"], ["equal", "var(--c7)"], ["DSARP worse", "var(--c3)"]])}</div>`;
    h += `<div class="card sec"><h2>Score per group</h2><div style="overflow:auto"><table class="heat"><tr><th>group</th>${aps.map(a => `<th>${esc(lab(a))}</th>`).join("")}</tr>${gids.map(g => `<tr><td class="mono" style="text-align:left"><a href="#/p/${n}/group/${esc(g)}">${esc(g)}</a></td>${aps.map(a => cell(sc(g, a), a === OWN)).join("")}</tr>`).join("")}</table></div></div>`;
  }
  return h + `</div>`;
}

/* ---------- group detail ---------- */
function targetText(t) {
  const parts = [["core_packages_on_cycle", "packages on cycle"], ["core_classes_on_cycle", "classes on cycle"],
    ["dependencies_to_cut", "dependencies to cut"], ["classes", "classes"], ["sets", "unrelated sets"], ["lcc", "LCC"],
    ["methods", "scattered methods"], ["fan_in", "fan-in"], ["fan_out", "fan-out"]];
  const out = parts.filter(([k]) => t && t[k] != null).map(([k, l]) => `<b>${esc(t[k])}</b> ${l}`);
  if (t && t.less_stable) out.push(`<b>${t.less_stable.length}</b> less stable dependencies`);
  return out.join(", ") || "–";
}
function md(text) {
  const lines = esc(text).split("\n"); let out = "", inList = false, inTable = false;
  const inline = s => s.replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/`([^`]+)`/g, "<code>$1</code>");
  for (const l of lines) {
    if (/^\|/.test(l)) { if (/^\|[-| ]+\|$/.test(l)) continue; if (!inTable) { out += "<table>"; inTable = true; } out += "<tr>" + l.split("|").slice(1, -1).map(c => `<td>${inline(c.trim())}</td>`).join("") + "</tr>"; continue; }
    if (inTable) { out += "</table>"; inTable = false; }
    if (/^\s*- /.test(l)) { if (!inList) { out += "<ul>"; inList = true; } out += `<li${/^\s{2,}/.test(l) ? ' style="margin-left:18px"' : ""}>${inline(l.replace(/^\s*- /, ""))}</li>`; continue; }
    if (inList) { out += "</ul>"; inList = false; }
    const m = l.match(/^(#{2,4}) (.*)/); if (m) { out += `<h${m[1].length}>${inline(m[2])}</h${m[1].length}>`; continue; }
    if (l.trim()) out += `<p>${inline(l)}</p>`;
  }
  return out + (inList ? "</ul>" : "") + (inTable ? "</table>" : "");
}
function diff(text) {
  return text.split("\n").map(l => { const c = l.startsWith("diff --git") ? "hd" : /^(\+\+\+|---)/.test(l) ? "hk" : l.startsWith("+") ? "add" : l.startsWith("-") ? "del" : l.startsWith("@@") || /^(rename|similarity|index)/.test(l) ? "hk" : "";
    return c ? `<span class="${c}">${esc(l)}</span>` : esc(l); }).join("\n");
}
async function viewGroup(name, gid) {
  const [pr, g] = await Promise.all([api("/api/projects/" + encodeURIComponent(name)), api(`/api/projects/${encodeURIComponent(name)}/group/${encodeURIComponent(gid)}`)]);
  const n = encodeURIComponent(name), P = g.plan || {}, T = g.triage || {}, V = g.verification;
  const plans = (P.plans || []).filter(p => p.name !== "no_change");
  const rec = plans.find(p => p.name === P.recommended);
  let pk = []; try { pk = JSON.parse(T.packages || T.members || "[]"); } catch (e) { pk = []; }
  let h = header(pr, "res") + `<div class="small"><a href="#/p/${n}/results">Results</a> / ${esc(gid)}</div>
  <div class="row" style="margin:6px 0 4px"><h1 class="mono" style="font-size:20px">${esc(gid)}</h1><span class="tag pu">${esc((P.type || T.type || "").replace(/_/g, " "))}</span>${P.rank ? `<span class="chip">rank <b>${P.rank}</b></span>` : ""}</div>
  <div class="small muted">${pk.map(x => `<code>${esc(x)}</code>`).join(" ")}</div>
  <div style="margin-top:10px">${[["severity", num(T.severity)], ["PageRank", num(T.pagerank, 3)], ["changes", T.changes], ["class edges", T.class_edges], ["smells merged", T.smells], ["tools", T.tools], ["priority", num(T.priority, 3)]].filter(x => x[1] != null && x[1] !== "–").map(([k, v]) => `<span class="chip">${k} <b>${esc(v)}</b></span>`).join("")}</div>`;
  if (!plans.length || !plans.some(p => (p.operations || []).length)) { h += `<div class="empty sec">No plan for this group. ${esc(P.skipped || P.no_plan_reason || "")}</div>`; $("#view").innerHTML = h; return; }
  if (!P.recommended && (P.no_plan_reason || P.skipped)) h += `<div class="card sec small"><b>No plan recommended.</b> ${esc(P.no_plan_reason || P.skipped)}</div>`;
  h += `<div class="grid g2 sec"><div class="card"><h2>Plans compared</h2><table><tr><th>plan</th><th>steps</th><th>cost</th><th>benefit</th><th>new</th><th>score</th></tr>${plans.map(p => `<tr${p.name === P.recommended ? ' style="background:var(--a-bg)"' : ""}><td>${esc(p.name)} ${p.name === P.recommended ? '<span class="tag pu">recommended</span>' : ""}</td><td>${(p.operations || []).length}</td><td>${num(p.change_cost, 1)}</td><td>${num(p.benefit)}</td><td>${num(p.new_smells, 0)}</td><td><b>${num(p.score, 3)}</b></td></tr>`).join("")}</table></div>
  <div class="card"><h2>Score breakdown</h2>${(() => { const w = S.info && P ? ({new_smell: .25, side_removed: .05, cost: .3}) : {}; return plans.filter(p => p.score != null).map(p => {
    const parts = [["benefit", p.benefit || 0, "var(--c2)"], ["other smells removed", w.side_removed * (p.other_smells_removed || 0), "var(--c4)"], ["new smells", -w.new_smell * (p.new_smells || 0), "var(--c3)"], ["cost", -w.cost * (p.cost_normalised || 0), "var(--c5)"]];
    return `<div style="margin:8px 0"><div class="row small"><b>${esc(p.name)}</b><span class="sp"></span><b>${num(p.score, 3)}</b></div><div class="small muted">${parts.filter(x => Math.abs(x[1]) > 1e-6).map(x => `<span style="color:${x[2]}">${x[0]} ${x[1] > 0 ? "+" : ""}${num(x[1], 3)}</span>`).join(" · ")}</div>
    <div class="track" style="height:10px;margin-top:3px"><i style="width:${Math.max(0, p.score) * 100}%;background:${(p.new_smells || 0) > 0 ? "var(--c3)" : "var(--c2)"}"></i></div></div>`; }).join(""); })()}
    <div class="small muted">score = benefit − 0.25·new smells + 0.05·other removed − 0.30·normalised cost − 0.01·made public</div></div></div>`;
  if (rec) {
    const sim = rec.simulation || {}, b = sim.target_before || {}, a = sim.target_after || {};
    h += `<div class="sec"><h2>Recommended steps <small>${esc(rec.name)} · ${(rec.operations || []).length} steps · ${esc(rec.backend || "")}</small></h2>
    <div class="chips"><span class="chip">before: ${targetText(b)}</span><span class="chip">after: ${targetText(a)} ${sim.target_removed ? '<span class="tag s-done">removed</span>' : ""}</span>
    ${(sim.new_smells || []).map(s => `<span class="chip" style="background:var(--r-bg);color:var(--r-ink)">new: ${esc(s.type)} ${esc(short(s.central || ""))}</span>`).join("")}</div>
    ${(rec.operations || []).map((o, i) => { const ck = o.check || {}; return `<div class="step"><div class="row"><b>Step ${i + 1}</b><span class="tag pu">${esc((o.op || "").replace(/_/g, " "))}</span>${ck.status ? `<span class="tag ${ck.status === "ok" ? "s-done" : ck.status === "blocked" ? "s-failed" : "s-pending"}">${esc(ck.status)}</span>` : ""}${o.affinity != null ? `<span class="small muted">affinity ${num(o.affinity)}</span>` : ""}${o.module ? `<span class="small muted">module ${esc(o.module)}</span>` : ""}</div>
      <div class="ft">${esc(o.class || (o.members ? o.members.map(x => x.split("#")[1]).join(", ") : "") || o.member || o.element || o.provider || o.client || "")}</div>
      ${o.from_package || o.to_package ? `<div class="ft"><span class="faint">from</span> ${esc(o.from_package || "")}<span class="arrow">→</span><span class="faint">to</span> <b>${esc(o.to_package || "")}</b>${o.new_package ? ' <span class="tag s-pending">new package</span>' : ""}</div>` : ""}
      ${o.from_class || o.to_class ? `<div class="ft"><span class="faint">from class</span> ${esc(o.from_class || "")}<span class="arrow">→</span><span class="faint">to class</span> <b>${esc(o.to_class || "")}</b></div>` : ""}
      ${o.op === "extract_interface" ? `<div class="ft"><span class="faint">client</span> ${esc(o.client)}<span class="arrow">→</span><span class="faint">interface for</span> <b>${esc(o.provider)}</b></div>` : ""}
      ${o.reason ? `<div class="small muted">${esc(o.reason)}</div>` : ""}
      ${(o.tests || []).length ? `<div class="small muted">tests moved with it: ${o.tests.map(t => `<code>${esc(short(t))}</code>`).join(" ")}</div>` : ""}
      ${(ck.edits || []).length ? `<ul class="small" style="margin:6px 0 0;padding-left:18px">${ck.edits.map(e => `<li>${esc((e.edit || "").replace(/_/g, " "))}${e.count ? ` in ${e.count}` : ""}${e.new_file ? ` → <code>${esc(e.new_file)}</code>` : ""}${e.classes ? `<div class="small muted">${e.classes.map(c => `<code>${esc(short(c))}</code>`).join(" ")}</div>` : ""}</li>`).join("")}</ul>` : ""}
      ${(ck.notes || []).map(x => `<div class="small" style="color:var(--w);margin-top:4px">${esc(x)}</div>`).join("")}</div>`; }).join("")}</div>`;
  }
  if (P.search) {
    const sr = P.search;
    h += `<div class="card sec"><h2>Tree search</h2><div>${[["iterations", sr.iterations], ["plans simulated", sr.evaluations], ["seconds", num(sr.seconds, 1)], ["best score", num(sr.best_score, 3)], ["LLM calls", sr.llm_calls]].map(([k, v]) => `<span class="chip">${k} <b>${esc(v)}</b></span>`).join("")}</div>
    ${bars((sr.trace || []).map((t, i) => ({label: t.event === "seed" ? `seed ${i + 1} (${t.operations} ops)` : `search (${int(t.iterations)} it.)`, value: Math.max(0, t.score ?? t.best ?? 0), total: t.score ?? t.best, color: t.event === "seed" ? "var(--c7)" : "var(--c1)"})), {max: 1, fmt: v => num(v, 3), lw: 150})}</div>`;
  }
  if (V) {
    h += `<div class="card sec"><h2>Verification on the real code</h2><div>${[["compiles", V.compiles ? "yes" : "no"], ["compile runs", V.compile_runs], ["steps kept", `${V.kept}/${V.operations}`], ["new errors", V.new_errors_full_plan], ["smell removed", V.target_removed ? "yes" : "no"], ["matches simulation", V.prediction_matches ? "yes" : "no"], ["files", V.files_changed], ["patch lines", V.patch_lines], ["seconds", num(V.seconds, 1)]].map(([k, v]) => `<span class="chip">${k} <b>${esc(v)}</b></span>`).join("")}</div>
    ${V.smells_before ? `<div class="small muted">smells in the repo before → after: ${Object.keys(V.smells_before).map(k => `${esc(k.replace(/_/g, " "))} ${V.smells_before[k]} → ${(V.smells_after || {})[k] ?? "?"}`).join(" · ")}</div>` : ""}
    ${(V.dropped_operations || []).length ? `<div class="small" style="color:var(--w)">Dropped by delta debugging: ${V.dropped_operations.map(esc).join(", ")}</div>` : ""}</div>`;
  }
  if (g.patch) {
    const files = [...g.patch.matchAll(/^diff --git a\/(\S+) b\/(\S+)/gm)].map(m => m[2]);
    h += `<div class="card sec"><div class="row"><h2 style="margin:0">plan.patch</h2><span class="small muted">${files.length} files · apply with <code>git apply plan.patch</code> at the same commit</span><span class="sp"></span><a class="btn" href="/api/projects/${n}/file/verify/${esc(gid)}/plan.patch" target="_blank" rel="noopener">Raw</a></div>
    <details style="margin:8px 0"><summary>${files.length} changed files</summary><ul class="small mono">${files.map(f => `<li>${esc(f)}</li>`).join("")}</ul></details><pre class="diff">${diff(g.patch)}</pre>${g.patch_truncated ? '<div class="small muted">Truncated; open the raw file for the rest.</div>' : ""}</div>`;
  }
  if (g.recipe) h += `<div class="card sec"><h2>OpenRewrite recipe</h2><pre>${esc(g.recipe)}</pre></div>`;
  if ((g.new_errors || []).length) h += `<div class="card sec"><h2>New compiler errors</h2><table><tr>${Object.keys(g.new_errors[0]).map(k => `<th>${esc(k)}</th>`).join("")}</tr>${g.new_errors.map(r => `<tr>${Object.values(r).map(v => `<td class="small">${esc(v)}</td>`).join("")}</tr>`).join("")}</table></div>`;
  if (g.report_md) h += `<div class="card sec md"><h2>From report.md</h2>${md(g.report_md)}</div>`;
  $("#view").innerHTML = h;
}

/* ---------- jobs ---------- */
function jobsTable(js, showProject = true) {
  if (!js.length) return `<div class="empty">No jobs yet.</div>`;
  return `<div class="card" style="padding:4px 8px;overflow:auto"><table><tr><th>job</th>${showProject ? "<th>project</th>" : ""}<th>stage</th><th>mode</th><th>status</th><th>Slurm id</th><th>started</th><th>duration</th><th></th></tr>
  ${js.map(j => `<tr class="click" data-act="go" data-href="#/jobs/${esc(j.id)}"><td class="mono small">${esc(j.id)}</td>${showProject ? `<td>${esc(j.project)}</td>` : ""}<td>${esc(j.stage)}${j.chain ? ` <span class="faint small">chain ${esc(j.chain)}</span>` : ""}</td><td>${esc(j.mode)}</td><td>${tag(j.status)}</td><td class="mono small">${esc(j.slurm_id || "")}</td><td class="small">${when(j.started || j.created)}</td><td class="small">${j.started ? dur(j.started, ACTIVE.includes(j.status) ? null : j.ended) : "–"}</td>
  <td>${ACTIVE.includes(j.status) ? `<button class="danger" data-act="cancel" data-job="${esc(j.id)}">Cancel</button>` : ""}</td></tr>`).join("")}</table></div>`;
}
async function viewJobs() {
  const js = await api("/api/jobs");
  $("#view").innerHTML = `<h1>Jobs</h1><p class="muted small">All stages started from this web interface. Slurm jobs keep running when the server stops; restart it in the same folder to follow them again.</p>${jobsTable(js)}`;
  S.timer = setInterval(() => { if (location.hash === "#/jobs") viewJobs().catch(() => {}); }, 5000);
}
async function viewProjectJobs(name) {
  const [pr, js] = await Promise.all([api("/api/projects/" + encodeURIComponent(name)), api(`/api/projects/${encodeURIComponent(name)}/jobs`)]);
  $("#view").innerHTML = header(pr, "jobs") + jobsTable(js, false);
  S.timer = setInterval(() => { if (location.hash.endsWith("/jobs")) viewProjectJobs(name).catch(() => {}); }, 5000);
}
async function viewJob(id) {
  const j = await api("/api/jobs/" + encodeURIComponent(id));
  let script = "";
  if (j.mode === "slurm") { try { script = await api(`/api/jobs/${encodeURIComponent(id)}/script`); } catch (e) { script = ""; } }
  $("#view").innerHTML = `<div class="small"><a href="#/jobs">Jobs</a> / ${esc(j.id)}</div><div class="row" style="margin:6px 0 14px"><h1>${esc(j.project)} · ${esc(j.stage)}</h1><span id="jst">${tag(j.status)}</span><span class="sp"></span>
    <a class="btn" href="#/p/${encodeURIComponent(j.project)}">Open pipeline</a>${ACTIVE.includes(j.status) ? `<button class="danger" data-act="cancel" data-job="${esc(j.id)}">Cancel</button>` : ""}</div>
    <div>${[["mode", j.mode], ["Slurm id", j.slurm_id], ["created", when(j.created)], ["started", when(j.started)], ["ended", when(j.ended)], ["exit code", j.returncode], ["chain", j.chain], ["after", j.after]].filter(x => x[1] != null && x[1] !== "–").map(([k, v]) => `<span class="chip">${k} <b>${esc(v)}</b></span>`).join("")}</div>
    ${j.error ? `<div class="err" style="margin:6px 0">${esc(j.error)}</div>` : ""}
    <label class="f">Command</label><pre>${esc(j.command)}</pre>
    ${script ? `<details style="margin-top:10px"><summary>Slurm script</summary><pre>${esc(script)}</pre></details>` : ""}
    <div class="row" style="margin-top:14px"><h2 style="margin:0">Log</h2><span class="sp"></span><label class="small row"><input type="checkbox" id="follow" checked> follow</label></div><pre class="log" id="plog" style="max-height:640px"></pre>`;
  S.log = {id: j.id, off: 0, text: ""}; await pollLog(true);
  S.timer = setInterval(async () => { if (!location.hash.startsWith("#/jobs/")) return; await pollLog(false);
    try { const k = await api("/api/jobs/" + encodeURIComponent(id)); $("#jst").innerHTML = tag(k.status); if (!ACTIVE.includes(k.status)) stopTimers(); } catch (e) {} }, 2000);
}

/* ---------- settings ---------- */
function viewSettings() {
  const s = S.info.settings;
  const f = (k, label, help, attrs = "") => `<div><label class="f">${label}</label><input name="${k}" value="${esc(s[k])}" ${attrs}><div class="help">${help}</div></div>`;
  $("#view").innerHTML = `<h1>Settings</h1><p class="muted small">Server folder <code>${esc(S.info.root)}</code> · state <code>${esc(S.info.state)}</code></p>
  <form id="setf" class="card" style="max-width:820px"><h2>Where stages run</h2>
    ${S.info.modes.map(m => `<label class="row" style="margin:8px 0;align-items:flex-start;flex-wrap:nowrap"><input type="radio" name="mode" value="${m}"${s.mode === m ? " checked" : ""} style="margin-top:4px"><span><b>${{local: "Local", container: "Container", slurm: "Slurm"}[m]}</b><br><span class="small muted">${{local: "Runs the dsarp command on this machine. Good for a laptop with dsarp installed.", container: "Runs apptainer exec on this machine with the image below. Don't use this on an HPC login node for heavy stages.", slurm: "Submits each stage with sbatch to the compute nodes (Noctua 2). Chains use afterok dependencies. Recommended on the cluster."}[m]}</span></span></label>`).join("")}
    <div class="grid g2">${f("dsarp_cmd", "dsarp command (local mode)", "For example <code>dsarp</code> or <code>python3 -m dsarp.cli</code>.")}${f("sif", "Container image", "Used in container and Slurm mode, e.g. <code>/scratch/hpc-prf-…/dsarp.sif</code>. Empty in Slurm mode = run <code>dsarp</code> directly.")}
    ${f("account", "Slurm account (-A)", "Your compute project, e.g. <code>hpc-prf-dssecs</code>.")}${f("partition", "Slurm partition", "normal, largemem, …")}
    ${f("modules", "Modules to load in jobs", "e.g. <code>tools Apptainer</code>; empty = none.")}${f("bind", "Apptainer bind path", "Added to APPTAINER_BIND so the container sees it, e.g. <code>/scratch</code>.")}</div>
    <h2 style="margin-top:20px">Folders</h2><div class="grid g2">${f("configs_dir", "Configs", "Folder with the repository YAML files.")}${f("results_dir", "Results", "One sub-folder per project.")}
    ${f("workdir", "Checkouts", "Where repositories are cloned.")}${f("data_root", "Detector data", "Folder that detector paths in the configs are relative to (empty = $DSARP_DATA).")}</div>
    <div class="row" style="margin-top:18px"><button class="primary" type="submit">Save settings</button><span id="serr" class="err"></span></div></form>`;
  $("#setf").addEventListener("submit", async e => {
    e.preventDefault(); const body = Object.fromEntries(new FormData(e.target).entries());
    try { const r = await api("/api/settings", {method: "POST", body}); S.info.settings = r; S.mode = r.mode; $("#mode").textContent = "mode: " + r.mode; toast("Settings saved"); }
    catch (er) { $("#serr").textContent = er.message; }
  });
}

/* ---------- actions (no inline handlers: the page has a strict CSP) ---------- */
document.addEventListener("click", async e => {
  const el = e.target.closest("[data-act]"); if (!el) return;
  const act = el.dataset.act;
  if (act === "sel") { e.preventDefault(); S.sel = el.dataset.stage; S.log.id = null; stopLog(); drawPipeline(true); return; }
  if (act === "go") { if (e.target.closest("a,button")) return; location.hash = el.dataset.href; return; }
  if (act === "cancel") { e.stopPropagation(); try { await api(`/api/jobs/${encodeURIComponent(el.dataset.job)}/cancel`, {method: "POST", body: {}}); toast("Cancelled"); route(); } catch (er) { toast(er.message, true); } return; }
  if (act === "cmp") { S.compare = !S.compare; store.set("compare", S.compare); route(); return; }
  if (act === "cmpap") { const sel = new Set($$("[data-act=cmpap]").filter(c => c.checked).map(c => c.dataset.ap)); S.cmpSel = [...sel]; store.set("cmpSel", S.cmpSel); route(); return; }
  if (act === "adddet") { S.addDet(); return; }
  if (act === "rmdet") { el.closest(".det").remove(); return; }
  if (act === "runall") { S.runAll = !S.runAll; renderRunAll(); return; }
  if (act === "runallx") { S.runAll = false; renderRunAll(); return; }
  if (act === "raall" || act === "ranone") { $$("[data-ra]").forEach(c => c.checked = act === "raall"); return; }
  if (act === "runallgo") { const ids = $$("[data-ra]").filter(c => c.checked).map(c => c.dataset.ra); if (!ids.length) return toast("Select at least one stage", true); runStages(ids); }
});
document.addEventListener("keydown", e => { if ((e.key === "Enter" || e.key === " ") && e.target.matches("g.node")) { e.preventDefault(); e.target.dispatchEvent(new MouseEvent("click", {bubbles: true})); } });
route();
