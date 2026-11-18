"use strict";
// MyVideoGen web UI. Plain JavaScript, no build step. Talks to webui.py's JSON API.

const $app = document.getElementById("app");
const S = {
  view: null, status: null,
  slug: null, project: null, draft: null, dirty: false, tab: "script",
  job: null, jobLines: [], logJobId: null, activitySig: "",
};
const SOURCES = { nasa: "NASA images", stock_video: "Stock video", stock_photo: "Stock photo",
  archive: "Archive & museums", ai: "AI-generated" };
const STEP_LABELS = {
  empty: "Not started", writing: "Writing script", review: "Needs review", voiceover: "Needs voiceover",
  visuals: "Needs visuals", render: "Ready to render", upload: "Ready to upload", uploaded: "Uploaded",
};
const STEPS = [["review", "Script"], ["voiceover", "Voiceover"], ["visuals", "Visuals"], ["render", "Render"], ["upload", "Upload"]];
const TONES = {
  natural: "Natural: the ElevenLabs audio as delivered",
  soft: "Soft: tames harsh “s” sounds and the 3 kHz edge, adds a little body",
  warm: "Warm: a stronger version of soft, for bright or thin voices",
};
const WPM = 150;

// ---------------------------------------------------------------- helpers
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else if (k === "value") el.value = v;
    else if (k === "checked") el.checked = v;
    else if (k === "html") el.innerHTML = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid == null || kid === false) continue;
    el.append(kid instanceof Node ? kid : String(kid));
  }
  return el;
}

async function api(method, url, body, isForm) {
  const opts = { method, headers: { "X-Requested-By": "myvideogen" } };
  if (body !== undefined) {
    if (isForm) opts.body = body;
    else { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  }
  const r = await fetch(url, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `${r.status} ${r.statusText}`);
  return data;
}

let toastTimer = null;
function toast(msg, bad) {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.className = "toast" + (bad ? " bad" : "");
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, bad ? 9000 : 3500);
}

function ago(ts) {
  if (!ts) return "";
  const s = Date.now() / 1000 - ts;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return new Date(ts * 1000).toLocaleDateString();
}

function words(text) { return (text || "").trim().split(/\s+/).filter(Boolean).length; }
function sceneStats(text) {
  const w = words(text), c = (text || "").trim().length;
  return `${w} words · ${c} characters · ~${Math.round(w * 60 / WPM)} s`;
}
function active(job) { return job && (job.status === "queued" || job.status === "running"); }
function chip(text, cls) { return h("span", { class: "chip" + (cls ? " " + cls : "") }, text); }

// ---------------------------------------------------------------- job following
const followers = new Map();
function follow(job, onUpdate, onDone) {
  if (followers.has(job.id)) return;
  const f = { lines: [], timer: null, stopped: false };
  followers.set(job.id, f);
  const tick = async () => {
    if (f.stopped) return;
    try {
      const j = await api("GET", `/api/jobs/${job.id}?since=${f.lines.length}`);
      f.lines.push(...(j.lines || []));
      if (onUpdate) onUpdate(j, f.lines);
      if (active(j)) { f.timer = setTimeout(tick, 1000); return; }
      followers.delete(job.id);
      if (j.status === "failed") toast(`${j.label} failed: ${j.error}`, true);
      else toast(`${j.label}: done`);
      if (onDone) onDone(j, f.lines);
    } catch (e) {
      if (String(e.message).includes("Unknown job")) { followers.delete(job.id); return; }
      f.timer = setTimeout(tick, 2500);
    }
  };
  tick();
}
function stopFollowers() {
  for (const f of followers.values()) { f.stopped = true; clearTimeout(f.timer); }
  followers.clear();
}

async function pollActivity() {
  try {
    const jobs = await api("GET", "/api/jobs?active=1");
    const el = document.getElementById("activity");
    if (jobs.length) {
      const j = jobs.find(x => x.status === "running") || jobs[0];
      el.replaceChildren(h("span", { class: "spinner" }),
        h("span", {}, `${j.label}${j.project ? " · " + j.project : ""}${jobs.length > 1 ? ` (+${jobs.length - 1} waiting)` : ""}`));
      el.hidden = false;
    } else el.hidden = true;
    if (S.view === "project" && S.slug) {
      const mine = jobs.find(x => x.project === S.slug);
      if (mine && !followers.has(mine.id)) watchProjectJob(mine);
    }
    const sig = jobs.map(x => x.id + x.status).join(",");
    if (S.view === "projects" && sig !== S.activitySig) refreshProjectCards();
    S.activitySig = sig;
  } catch (e) { /* server restarting */ }
  setTimeout(pollActivity, 2500);
}

// ---------------------------------------------------------------- routing
let currentHash = location.hash;
window.addEventListener("hashchange", route);
window.addEventListener("beforeunload", e => { if (S.dirty) { e.preventDefault(); e.returnValue = ""; } });

async function route() {
  const parts = location.hash.replace(/^#\/?/, "").split("/").map(decodeURIComponent);
  const leavingProject = S.view === "project" && !(parts[0] === "p" && parts[1] === S.slug);
  if (leavingProject && S.dirty && !confirm("You have unsaved script changes. Leave without saving them?")) {
    history.replaceState(null, "", currentHash);
    return;
  }
  currentHash = location.hash;
  if (leavingProject) { S.dirty = false; hideSaveBar(); }
  document.querySelectorAll("[data-nav]").forEach(a => a.classList.toggle("active",
    a.dataset.nav === (parts[0] === "queue" ? "queue" : parts[0] === "settings" ? "settings" : "projects")));
  try {
    if (!S.status) S.status = await api("GET", "/api/status");
    if (parts[0] === "p" && parts[1]) await showProject(parts[1], parts[2] || "script");
    else if (parts[0] === "queue") { stopFollowers(); S.view = "queue"; await showQueue(); }
    else if (parts[0] === "settings") { stopFollowers(); S.view = "settings"; await showSettings(); }
    else { stopFollowers(); S.view = "projects"; await showProjects(); }
  } catch (e) {
    $app.replaceChildren(h("div", { class: "panel callout bad" }, h("h2", {}, "Something went wrong"), h("p", {}, e.message)));
  }
}

// ---------------------------------------------------------------- videos list
async function showProjects() {
  S.slug = null; S.project = null; S.draft = null;
  const box = h("div", { class: "panel stack" });
  const draw = () => {
    const mode = S.newMode || "claude";
    const seg = (k, label) => h("button", { type: "button", class: mode === k ? "active" : "",
      onclick: () => { S.newMode = k; draw(); } }, label);
    box.replaceChildren(
      h("div", { class: "row between" }, h("h2", { style: "margin:0" }, "New video"),
        h("div", { class: "seg" }, seg("claude", "Claude writes the script"), seg("manual", "I have the script"))),
      mode === "manual" ? manualForm() : claudeForm());
  };
  draw();
  $app.replaceChildren(box, h("div", { id: "perf" }), h("h2", {}, "Videos"),
    h("div", { id: "cards", class: "cards" }, h("p", { class: "muted" }, "Loading…")));
  await refreshProjectCards();
}

function claudeForm() {
  const topic = h("input", { class: "grow", placeholder: "Topic, e.g. Why do stars twinkle?", required: true, maxlength: 200 });
  const minutes = h("input", { type: "number", min: 1, max: 30, step: 0.5, value: 6, style: "width:5.5rem" });
  return h("form", { class: "stack", onsubmit: async e => {
    e.preventDefault();
    try {
      const r = await api("POST", "/api/projects", { topic: topic.value, minutes: Number(minutes.value) });
      location.hash = `#/p/${r.slug}`;
    } catch (err) { toast(err.message, true); }
  } },
    h("div", { class: "row" }, topic, h("label", { class: "row" }, minutes, h("span", { class: "muted small" }, "minutes")),
      h("button", { class: "primary", type: "submit" }, "Write script")),
    h("p", { class: "muted small" }, "Claude writes the script and fact-checks it with web searches (a few cents). Nothing is narrated or spent on ElevenLabs until you approve the script."));
}

// Rough preview of how a pasted script will be split (the server does the real split)
function manualStats(text) {
  const w = words(text);
  if (!w) return "Separate scenes with a blank line, or start lines with “Scene 1:”, “Scene 2:”…";
  const body = text.split("\n").filter(l => !/^\s*[\[(]?\s*visual\s*[:\-–—]/i.test(l)).join("\n");
  const blocks = body.split(/\n\s*\n|\n(?=\s*[\[(]?\s*scene\s*\d)/i).filter(b => b.trim()).length;
  const scenes = blocks > 1 ? `${blocks} scenes` : w > 50 ? `about ${Math.max(2, Math.round(w / 30))} scenes (split automatically into about two sentences each)` : "1 scene";
  return `${w} words · ${body.trim().length} characters · ~${(w / WPM).toFixed(1)} min · ${scenes}`;
}

function manualForm() {
  const title = h("input", { placeholder: "Title, e.g. The day the Moon saved Earth", required: true, maxlength: 100, style: "width:100%" });
  const stats = h("div", { class: "muted small" }, manualStats(""));
  const text = h("textarea", { rows: 12, required: true, placeholder:
    "Scene 1: Four billion years ago, a day on Earth lasted just six hours.\nVisual: nasa: early Earth\n\n" +
    "Scene 2: The Moon was much closer then, and its pull raised enormous tides.\nVisual: giant ocean waves\n\n" +
    "…or paste the whole speech as one block and it is split into scenes for you.",
    oninput: () => { stats.textContent = manualStats(text.value); } });
  const suggest = h("input", { type: "checkbox", checked: true });
  const check = h("input", { type: "checkbox", checked: true });
  return h("form", { class: "stack", onsubmit: async e => {
    e.preventDefault();
    try {
      const r = await api("POST", "/api/projects", { mode: "manual", title: title.value, script: text.value,
        suggest: suggest.checked, factcheck: check.checked });
      location.hash = `#/p/${r.slug}`;
    } catch (err) { toast(err.message, true); }
  } },
    h("label", {}, h("span", {}, "Title"), title),
    h("label", {}, h("span", {}, "Narration, exactly as it should be spoken"), text),
    stats,
    h("p", { class: "muted small" }, "Optional: under a scene, add a line “Visual: ocean waves” to choose its visual search ",
      "(“Visual: nasa: Saturn rings” for NASA images, “Visual: photo: old map” for a still photo)."),
    h("label", { class: "row" }, suggest, h("span", { style: "margin:0" },
      "Let Claude choose visuals for the other scenes and write the description, tags and thumbnail text (about a cent; your narration is not changed)")),
    h("label", { class: "row" }, check, h("span", { style: "margin:0" },
      "Fact-check it with web searches (a few cents). You review the findings before anything is narrated.")),
    h("div", { class: "row" }, h("button", { class: "primary", type: "submit" }, "Create video")));
}

function perfPanel(projects) {
  const uploaded = projects.filter(p => p.youtube || p.short_youtube);
  if (!uploaded.length) return null;
  const fetched = uploaded.map(p => p.stats && p.stats.fetched).filter(Boolean).sort().pop();
  const rows = uploaded.map(p => ({ p, v: (p.stats && p.stats.video) || {}, s: (p.stats && p.stats.short) || {} }))
    .sort((a, b) => (b.v.views || 0) - (a.v.views || 0));
  const num = x => (x == null ? "–" : Number(x).toLocaleString());
  const dur = s => `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, "0")}`;
  const btn = h("button", { class: "small", onclick: async () => {
    try {
      const job = await api("POST", "/api/stats/refresh");
      btn.disabled = true; btn.textContent = "Refreshing…";
      follow(job, null, () => refreshProjectCards());
    } catch (e) { toast(e.message, true); }
  } }, "Refresh statistics");
  return h("div", { class: "panel" },
    h("div", { class: "row between" }, h("h2", { style: "margin:0" }, "How your videos are doing"),
      h("div", { class: "row" }, h("span", { class: "muted small" }, fetched ? `Updated ${ago(Date.parse(fetched) / 1000)}` : "Not fetched yet"), btn)),
    h("div", { class: "tablewrap" }, h("table", { class: "stats" },
      h("tr", {}, ["Video", "Views", "Watch time", "Average view", "Likes", "Short views"].map(t => h("th", {}, t))),
      rows.map(({ p, v, s }) => h("tr", {},
        h("td", {}, h("a", { href: `#/p/${p.slug}` }, p.title)),
        h("td", {}, p.youtube ? num(v.views) : "–"),
        h("td", {}, v.minutes_watched == null ? "–" : `${(v.minutes_watched / 60).toFixed(1)} h`),
        h("td", {}, v.avg_view_seconds == null ? "–"
          : `${dur(v.avg_view_seconds)}${v.avg_view_percent != null ? ` (${Math.round(v.avg_view_percent)}%)` : ""}`),
        h("td", {}, p.youtube ? num(v.likes) : "–"),
        h("td", {}, p.short_youtube ? num(s.views) : "–"))))),
    h("p", { class: "muted small" }, "Views and likes are live. Watch time and average view come from YouTube Analytics, which lags a day or two."));
}

async function refreshProjectCards() {
  const box = document.getElementById("cards");
  if (!box) return;
  const projects = await api("GET", "/api/projects");
  const perf = document.getElementById("perf");
  const panel = perfPanel(projects);
  if (perf) perf.replaceChildren(...(panel ? [panel] : []));
  if (!projects.length) {
    box.replaceChildren(h("p", { class: "muted" }, "No videos yet. Start one above, or from the topic queue."));
    return;
  }
  box.replaceChildren(...projects.map(p => h("a", { class: "card", href: `#/p/${p.slug}` },
    h("div", { class: "thumb" }, p.thumbnail_url ? h("img", { src: p.thumbnail_url, alt: "" }) : "No thumbnail yet"),
    h("div", { class: "body" },
      h("div", { class: "title" }, p.title),
      h("div", { class: "row" },
        p.job ? h("span", { class: "chip accent" }, h("span", { class: "spinner", style: "display:inline-block;width:10px;height:10px;vertical-align:-1px;margin-right:4px" }), p.job.label)
          : chip(STEP_LABELS[p.step] || p.step, p.step === "uploaded" ? "ok" : p.step === "review" || p.step === "voiceover" ? "warn" : "accent"),
        p.scenes ? chip(`${p.scenes} scenes · ~${p.minutes} min`) : null),
      h("div", { class: "muted small" }, `Updated ${ago(p.updated)}`)))));
}

// ---------------------------------------------------------------- project view
async function showProject(slug, tab) {
  const same = S.view === "project" && S.slug === slug && S.project;
  S.view = "project"; S.tab = tab;
  if (!same) {
    stopFollowers();
    S.slug = slug; S.project = null; S.draft = null; S.dirty = false; S.job = null; S.jobLines = []; S.logJobId = null;
    $app.replaceChildren(h("p", { class: "muted" }, "Loading…"));
    await loadProject(false);
  } else renderProject();
}

async function loadProject(keepDraft) {
  const p = await api("GET", `/api/projects/${encodeURIComponent(S.slug)}`);
  S.project = p;
  if (!(keepDraft && S.dirty)) makeDraft();
  renderProject();
  if (p.job && !followers.has(p.job.id)) watchProjectJob(p.job);
}

function makeDraft() {
  const sc = S.project && S.project.script;
  S.draft = sc ? JSON.parse(JSON.stringify(sc)) : null;
  if (S.draft) {
    S.draft.scenes.forEach((s, i) => { s._orig = i; });
    S.draft._thumbScene = Number.isInteger(S.draft.thumbnail_scene) ? S.draft.scenes[S.draft.thumbnail_scene] : null;
  }
  S.dirty = false;
  hideSaveBar();
}

function markDirty() {
  if (!S.dirty) { S.dirty = true; showSaveBar(); }
}

function showSaveBar() {
  let bar = document.getElementById("savebar");
  if (!bar) {
    bar = h("div", { id: "savebar", class: "savebar" },
      h("span", { class: "muted" }, "Unsaved changes"),
      h("button", { onclick: () => { if (confirm("Discard your unsaved changes?")) { makeDraft(); renderProject(); } } }, "Discard"),
      h("button", { class: "primary", onclick: () => saveDraft() }, "Save"));
    document.body.append(bar);
  }
}
function hideSaveBar() { const bar = document.getElementById("savebar"); if (bar) bar.remove(); }

async function saveDraft() {
  const d = S.draft;
  const script = {
    title: d.title || "", description: d.description || "", tags: d.tags || [], thumbnail_text: d.thumbnail_text || "",
    scenes: d.scenes.map(s => ({ narration: s.narration, visual: { source: s.visual.source, query: s.visual.query } })),
  };
  const ti = d._thumbScene ? d.scenes.indexOf(d._thumbScene) : -1;
  if (ti >= 0) script.thumbnail_scene = ti;
  const origin = d.scenes.map(s => (s._orig == null ? null : s._orig));
  try {
    S.project = await api("PUT", `/api/projects/${encodeURIComponent(S.slug)}/script`, { script, origin });
    makeDraft();
    renderProject();
    toast("Saved");
    return true;
  } catch (e) { toast(e.message, true); return false; }
}

// What is left on the ElevenLabs plan, as a sentence for cost confirmations ("" if it cannot be read)
async function usageNote(chars) {
  try {
    const u = await api("GET", "/api/elevenlabs");
    if (u.error || !u.limit) return "";
    const resets = u.resets ? ` (resets ${new Date(u.resets * 1000).toLocaleDateString()})` : "";
    const over = chars > u.left ? "\n\nThat is more than you have left: ElevenLabs will refuse the rest or bill extra, depending on your plan." : "";
    return `\n\nYou have ${u.left.toLocaleString()} of ${u.limit.toLocaleString()} characters left this month${resets}.${over}`;
  } catch (e) { return ""; }
}

async function startJob(action, extra, confirmText, chars) {
  if (confirmText && !confirm(confirmText + (chars ? await usageNote(chars) : ""))) return;
  try {
    if (S.dirty && !(await saveDraft())) return;
    const job = await api("POST", `/api/projects/${encodeURIComponent(S.slug)}/jobs`, Object.assign({ action }, extra || {}));
    S.project.job = job;
    watchProjectJob(job);
    renderProject();
  } catch (e) { toast(e.message, true); }
}

function watchProjectJob(job) {
  S.job = job; S.jobLines = []; S.logJobId = job.id;
  follow(job, (j, lines) => {
    S.job = j; S.jobLines = lines;
    updateJobBanner(); updateJobLog();
  }, async j => {
    S.job = null;
    if (S.view === "project" && S.slug === j.project) {
      const rewrites = j.action === "create" || j.action === "rewrite" || j.action === "factcheck";
      if (rewrites && S.dirty) toast("The script changed on the server; your unsaved edits were replaced.", true);
      await loadProject(!rewrites);
    }
  });
}

function updateJobBanner() {
  const el = document.getElementById("jobbanner");
  if (!el) return;
  const j = S.job;
  if (!active(j)) { el.replaceChildren(); el.hidden = true; return; }
  el.hidden = false;
  el.replaceChildren(h("div", { class: "panel jobbar" }, h("span", { class: "spinner" }),
    h("div", { class: "grow" }, h("strong", {}, j.status === "queued" ? `Waiting: ${j.label}` : j.label),
      h("div", { class: "muted small" }, (S.jobLines[S.jobLines.length - 1] || j.last_line || "Starting…").replace(/^\[[\d:]+\]\s*/, ""))),
    h("a", { href: `#/p/${S.slug}/log` }, "Log")));
}

function renderProject() {
  const p = S.project;
  if (!p) return;
  const tabs = [["script", "Script"], ["visuals", "Visuals"], ["video", "Video"], ["short", "Short"], ["log", "Log"]];
  $app.replaceChildren(
    h("div", { class: "row between" },
      h("div", {}, h("h1", {}, (S.draft && S.draft.title) || p.title),
        h("div", { class: "muted small" }, p.topic ? `Topic: ${p.topic}` : "", p.scenes ? ` · ${p.scenes} scenes · ${p.words} words · ~${p.minutes} min` : "")),
      p.youtube ? h("a", { class: "btn", href: p.youtube.url, target: "_blank", rel: "noopener" }, "Open on YouTube") : null),
    stepsBar(p),
    h("div", { id: "jobbanner", hidden: true }),
    nextStep(p),
    h("nav", { class: "tabs" }, tabs.map(([k, label]) => h("a", { href: `#/p/${p.slug}/${k}`, class: S.tab === k ? "active" : "" }, label))),
    h("div", { id: "tabbody" }, S.tab === "visuals" ? visualsTab(p) : S.tab === "video" ? videoTab(p)
      : S.tab === "short" ? shortTab(p) : S.tab === "log" ? logTab(p) : scriptTab(p)));
  updateJobBanner();
  if (S.dirty) showSaveBar();
}

function stepsBar(p) {
  const order = STEPS.map(s => s[0]);
  const cur = p.step === "uploaded" ? order.length : order.indexOf(p.step);
  return h("div", { class: "steps" }, STEPS.map(([k, label], i) =>
    h("span", { class: "step" + (cur > i ? " done" : cur === i ? " current" : "") }, (cur > i ? "✓ " : "") + label)));
}

function nextStep(p) {
  if (active(S.job) || p.job) return null;
  const box = (cls, text, ...actions) => h("div", { class: "panel callout row between " + (cls || "") }, h("div", { class: "grow" }, text), ...actions);
  const failures = p.attempts && p.attempts.errors && p.attempts.errors.length
    ? h("div", { class: "muted small" }, `Last unattended run failed: ${p.attempts.errors[p.attempts.errors.length - 1]}`) : null;
  switch (p.step) {
    case "empty":
      if (p.source === "manual") {
        return box("bad", "Preparing your script failed or was interrupted (see the Log tab). Your text is saved in the project as my_script.txt.",
          h("button", { class: "primary", onclick: async () => {
            try { watchProjectJob(await api("POST", `/api/projects/${encodeURIComponent(p.slug)}/prepare`)); renderProject(); }
            catch (e) { toast(e.message, true); }
          } }, "Try again"));
      }
      return box("bad", h("div", {}, "The script has not been written yet (the last attempt failed or was interrupted).", failures),
        h("button", { class: "primary", onclick: async () => {
          try {
            const r = await api("POST", "/api/projects", { topic: p.topic, minutes: 6 });
            location.hash = `#/p/${r.slug}`; await loadProject(false);
          } catch (e) { toast(e.message, true); }
        } }, "Write script"));
    case "review":
      return box("", "Review the script and the fact-check below. When you are happy with it, generate the voiceover.",
        h("button", { class: "primary", onclick: () => startJob("voiceover", null,
          `ElevenLabs will narrate ${p.voice_todo.length} scene(s), ${p.voice_todo_chars} characters. Continue?`, p.voice_todo_chars) },
          `Approve and generate voiceover (${p.voice_todo_chars} characters)`));
    case "voiceover":
      return box("warn", `${p.voice_todo.length} scene(s) have new or changed narration (${p.voice_todo_chars} characters).`,
        h("button", { class: "primary", onclick: () => startJob("voiceover", null,
          `ElevenLabs will narrate ${p.voice_todo.length} scene(s), ${p.voice_todo_chars} characters. Continue?`, p.voice_todo_chars) },
          `Generate voiceover (${p.voice_todo_chars} characters)`));
    case "visuals":
      return box("", `${p.visuals_ready} of ${p.scenes} scenes have a visual.`,
        h("button", { class: "primary", onclick: () => startJob("visuals") }, "Fetch visuals"));
    case "render":
      return box("", p.video === "missing" ? "Everything is ready to render." : "The video is out of date with your latest changes.",
        h("button", { class: "primary", onclick: () => startJob("render") }, "Render video"));
    case "upload":
      return box("", "The video is ready. Watch it, then upload it.", h("a", { class: "btn", href: `#/p/${p.slug}/video` }, "Watch and upload"));
    default:
      return null;
  }
}

// ---------------------------------------------------------------- script tab
function scriptTab(p) {
  const d = S.draft;
  if (!d) return h("p", { class: "muted" }, p.step === "writing" ? "Claude is writing the script. It appears here when the fact-check is done." : "No script yet.");
  return h("div", {},
    factcheckPanel(p),
    metaPanel(d),
    h("div", { class: "row between", style: "margin:1.25rem 0 .5rem" },
      h("h2", { style: "margin:0" }, "Scenes"),
      h("span", { class: "muted small", id: "totals" }, totalsText())),
    h("div", { id: "scenes" }, d.scenes.map((s, i) => sceneEditor(s, i))),
    h("div", { class: "panel row", style: "margin-top:1rem" },
      h("button", { onclick: () => startJob("factcheck", null, "Run the fact-check again on the saved script? This uses Claude and web searches (a few cents).") }, "Fact-check again"),
      h("button", { class: "danger", onclick: () => {
        const m = prompt("Rewrite the whole script from scratch? Current narration clips and visuals move to the project's .trash folder.\n\nTarget length in minutes:", "6");
        if (m) startJob("rewrite", { minutes: Number(m) });
      } }, "Rewrite script from scratch")));
}

function totalsText() {
  const all = S.draft.scenes.map(s => s.narration).join(" ");
  const w = words(all);
  return `${S.draft.scenes.length} scenes · ${w} words · ${all.trim().length} characters · ~${(w / WPM).toFixed(1)} min`;
}
function updateTotals() { const el = document.getElementById("totals"); if (el) el.textContent = totalsText(); }

function factcheckPanel(p) {
  const fc = p.factcheck;
  if (!fc) return h("div", { class: "panel muted" }, "No fact-check for this script yet.");
  const issues = fc.issues || [];
  const draftIndex = orig => S.draft.scenes.findIndex(s => s._orig === orig);
  return h("details", { class: "panel", open: issues.length && p.step === "review" ? true : null },
    h("summary", {}, h("strong", {}, "Fact-check: "),
      issues.length ? chip(`${issues.length} issue(s)`, "warn") : chip("no issues", "ok"), " ",
      chip(`${(fc.claims_to_verify || []).length} claim(s) to double-check`), " ",
      chip(`${fc.searches || 0} web search(es)`)),
    h("div", { class: "stack", style: "margin-top:.75rem" },
      issues.map(issue => {
        const k = draftIndex(issue.scene);
        const applied = k >= 0 && S.draft.scenes[k].narration.trim() === (issue.fixed_narration || "").trim();
        return h("div", { class: "issue" },
          h("div", {}, h("strong", {}, k >= 0 ? `Scene ${k + 1}: ` : "Removed scene: "), issue.problem),
          issue.fixed_narration ? h("div", { class: "fix" }, issue.fixed_narration) : null,
          h("div", { class: "row" },
            issue.source ? (/^https?:\/\//.test(issue.source)
              ? h("a", { href: issue.source, target: "_blank", rel: "noopener", class: "small" }, "Source")
              : h("span", { class: "muted small" }, `Source: ${issue.source}`)) : null,
            k >= 0 && issue.fixed_narration ? (applied ? chip("applied", "ok") : h("button", { class: "small", onclick: () => {
              S.draft.scenes[k].narration = issue.fixed_narration;
              markDirty(); renderProject();
            } }, "Use this fix")) : null));
      }),
      (fc.claims_to_verify || []).length ? h("div", {}, h("h3", {}, "Claims to double-check yourself"),
        h("ul", {}, fc.claims_to_verify.map(c => h("li", {}, c)))) : null,
      p.factcheck_notes ? h("details", {}, h("summary", { class: "small" }, "Full fact-checker notes"), h("pre", {}, p.factcheck_notes)) : null));
}

function metaPanel(d) {
  const counter = h("span", { class: "muted small" }, `${(d.title || "").length}/100`);
  return h("div", { class: "panel" }, h("div", { class: "field-grid" },
    h("label", { style: "grid-column:1/-1" }, h("span", {}, "Title ", counter),
      h("input", { value: d.title || "", maxlength: 100, style: "width:100%", oninput: e => {
        d.title = e.target.value; counter.textContent = `${d.title.length}/100`; markDirty();
      } })),
    h("label", {}, h("span", {}, "Thumbnail text (2-5 words)"),
      h("input", { value: d.thumbnail_text || "", maxlength: 60, style: "width:100%", oninput: e => { d.thumbnail_text = e.target.value; markDirty(); } })),
    h("label", {}, h("span", {}, "Tags (comma separated)"),
      h("input", { value: (d.tags || []).join(", "), style: "width:100%", oninput: e => {
        d.tags = e.target.value.split(",").map(t => t.trim()).filter(Boolean); markDirty();
      } })),
    h("label", { style: "grid-column:1/-1" }, h("span", {}, "Description"),
      h("textarea", { rows: 5, oninput: e => { d.description = e.target.value; markDirty(); } }, d.description || ""))));
}

function audioState(s) {
  const info = s._orig != null ? S.project.scene_info[s._orig] : null;
  if (!info || !info.audio.url) return { state: "new", info };
  return { state: info.audio.text === s.narration.trim() ? "voiced" : "changed", info };
}
function audioChip(state) {
  return state === "voiced" ? chip("voiced", "ok") : state === "changed" ? chip("changed: will be re-voiced", "warn") : chip("not voiced yet");
}

function renderScenes() {
  const box = document.getElementById("scenes");
  if (box) box.replaceChildren(...S.draft.scenes.map((s, i) => sceneEditor(s, i)));
  updateTotals();
}

function sceneEditor(s, i) {
  const d = S.draft;
  const n = d.scenes.length;
  const { state, info } = audioState(s);
  const chipBox = h("span", {}, audioChip(state));
  const stats = h("span", { class: "muted small" }, sceneStats(s.narration));
  const move = delta => { const j = i + delta; [d.scenes[i], d.scenes[j]] = [d.scenes[j], d.scenes[i]]; markDirty(); renderScenes(); };
  const ta = h("textarea", { rows: 3, oninput: e => {
    s.narration = e.target.value; markDirty();
    stats.textContent = sceneStats(s.narration);
    chipBox.replaceChildren(audioChip(audioState(s).state));
    updateTotals();
  } }, s.narration);
  const split = () => {
    const pos = ta.selectionStart, before = s.narration.slice(0, pos).trim(), after = s.narration.slice(pos).trim();
    if (!before || !after) { toast("Click in the text where the new scene should start, then press ✂"); return; }
    s.narration = before;
    d.scenes.splice(i + 1, 0, { narration: after, visual: { source: s.visual.source, query: s.visual.query }, _orig: null });
    markDirty(); renderScenes();
  };
  const join = () => {
    const next = d.scenes[i + 1];
    if (audioState(next).state !== "new" && !confirm(`Join scene ${i + 2} onto scene ${i + 1}? Scene ${i + 2}'s narration clip and visual move to the project's .trash folder when you save, and the joined scene is narrated again.`)) return;
    s.narration = `${s.narration.trim()} ${next.narration.trim()}`.trim();
    d.scenes.splice(i + 1, 1); markDirty(); renderScenes();
  };
  return h("div", { class: "scene" },
    h("div", { class: "head" },
      h("span", { class: "num" }, `Scene ${i + 1}`), chipBox,
      info && info.audio.url ? h("audio", { controls: true, preload: "none", src: info.audio.url }) : null,
      h("div", { class: "tools" },
        state === "voiced" ? h("button", { class: "small", title: "Generate this clip again with the same text", onclick: () =>
          startJob("revoice", { scene: i }, `Re-voice scene ${i + 1}? ElevenLabs will narrate ${s.narration.trim().length} characters again.`,
            s.narration.trim().length) }, "Re-voice") : null,
        h("button", { class: "small", title: "Move up", disabled: i === 0, onclick: () => move(-1) }, "↑"),
        h("button", { class: "small", title: "Move down", disabled: i === n - 1, onclick: () => move(1) }, "↓"),
        h("button", { class: "small", title: "Split this scene in two where the text cursor is", onclick: split }, "✂"),
        h("button", { class: "small", title: "Join the next scene onto this one", disabled: i === n - 1, onclick: join }, "⤓"),
        h("button", { class: "small", title: "Add a scene below", onclick: () => {
          d.scenes.splice(i + 1, 0, { narration: "", visual: { source: s.visual.source, query: "" }, _orig: null });
          markDirty(); renderScenes();
        } }, "+"),
        h("button", { class: "small danger", title: "Delete scene", disabled: n === 1, onclick: () => {
          if (state !== "new" && !confirm(`Delete scene ${i + 1}? Its narration clip and visual move to the project's .trash folder when you save.`)) return;
          d.scenes.splice(i, 1); markDirty(); renderScenes();
        } }, "✕"))),
    ta,
    h("div", { class: "visual-row" }, stats),
    h("div", { class: "visual-row" },
      h("select", { onchange: e => { s.visual.source = e.target.value; markDirty(); } },
        Object.entries(SOURCES).map(([k, label]) => h("option", { value: k, selected: s.visual.source === k ? true : null }, label))),
      h("input", { value: s.visual.query, placeholder: "Visual search words, e.g. aurora night sky", maxlength: 100,
        oninput: e => { s.visual.query = e.target.value; markDirty(); } })));
}

// ---------------------------------------------------------------- visuals tab
function visualsTab(p) {
  const d = S.draft;
  if (!d) return h("p", { class: "muted" }, "No script yet.");
  const providers = (S.status && S.status.stock_providers) || [];
  const missing = p.scenes - p.visuals_ready;
  return h("div", {},
    h("div", { class: "panel row between" },
      h("div", {}, h("strong", {}, `${p.visuals_ready} of ${p.scenes} scenes have a visual. `),
        h("span", { class: "muted small" }, providers.length ? `Stock footage from ${providers.join(" and ")}.` : "No stock footage key is set, so every scene uses NASA images.")),
      missing ? h("button", { class: "primary", onclick: () => startJob("visuals") }, `Fetch ${missing} missing visual(s)`) : null),
    S.dirty ? h("div", { class: "panel callout warn" }, "You have unsaved changes. Saving happens automatically when you search for or upload a visual.") : null,
    h("div", { class: "vgrid" }, d.scenes.map((s, i) => visualCard(s, i))));
}

function visualCard(s, i) {
  const d = S.draft;
  const info = s._orig != null ? S.project.scene_info[s._orig] : null;
  const a = info && info.asset;
  const isThumb = d._thumbScene === s;
  const file = h("input", { type: "file", accept: "image/*,video/*", hidden: true, onchange: async e => {
    const f = e.target.files[0];
    if (!f) return;
    const credit = prompt("Credit line for the video description (leave empty if none is needed):", "");
    if (credit === null) return;
    try {
      if (S.dirty && !(await saveDraft())) return;
      const fd = new FormData();
      fd.append("file", f); fd.append("credit", credit);
      toast("Uploading…");
      S.project = await api("POST", `/api/projects/${encodeURIComponent(S.slug)}/scenes/${i}/visual`, fd, true);
      makeDraft(); renderProject(); toast(`Scene ${i + 1} now uses your file`);
    } catch (err) { toast(err.message, true); }
  } });
  let media;
  if (!a || !a.url) media = h("div", { class: "media" }, "No visual yet");
  else if (a.kind === "video") media = h("div", { class: "media" }, h("video", { src: a.url, muted: true, loop: true, controls: true, preload: "metadata" }));
  else media = h("div", { class: "media" }, h("img", { src: a.url, alt: "", loading: "lazy" }));
  return h("div", { class: "vcard" }, media,
    h("div", { class: "body" },
      h("div", { class: "row between" }, h("strong", {}, `Scene ${i + 1}`),
        h("span", {}, a ? chip(a.source === "custom" ? "your file" : a.kind) : null, isThumb ? chip("thumbnail", "accent") : null)),
      h("div", { class: "narr" }, s.narration),
      a && a.credit ? h("div", { class: "muted small" }, a.credit) : null,
      h("div", { class: "row" },
        h("select", { onchange: e => { s.visual.source = e.target.value; markDirty(); } },
          Object.entries(SOURCES).map(([k, label]) => h("option", { value: k, selected: s.visual.source === k ? true : null }, label))),
        h("input", { class: "grow", value: s.visual.query, maxlength: 100, oninput: e => { s.visual.query = e.target.value; markDirty(); } })),
      h("div", { class: "row" },
        h("button", { class: "small primary", title: "Browse search results with previews and pick one",
          onclick: () => openPicker(i) }, "Choose…"),
        h("button", { class: "small", title: "Search with the words above and swap in the next result you have not skipped",
          onclick: () => startJob("find_visual", { scene: i }) }, a ? "Next result" : "Find visual"),
        h("button", { class: "small", onclick: () => file.click() }, "Upload my own"), file,
        a ? h("button", { class: "small", disabled: isThumb, onclick: () => {
          d._thumbScene = s; markDirty(); renderProject();
        } }, "Use for thumbnail") : null)));
}

// ---------------------------------------------------------------- visual picker
async function openPicker(i) {
  if (S.dirty && !(await saveDraft())) return;   // scene numbers must match the saved script
  const s = S.draft.scenes[i];
  const slug = S.slug;
  let page = 1, sceneSeconds = null, closed = false;
  const q = h("input", { class: "grow", value: s.visual.query, maxlength: 100, placeholder: "Search words, e.g. molten lava" });
  const src = h("select", {}, Object.entries(SOURCES).map(([k, label]) => h("option", { value: k, selected: s.visual.source === k ? true : null }, label)));
  const grid = h("div", { class: "pgrid" });
  const status = h("div", { class: "muted small" });
  const more = h("button", { hidden: true, onclick: () => search(page + 1) }, "More results");
  const overlay = h("div", { class: "modal-back", onclick: e => { if (e.target === overlay) close(); } });
  const onKey = e => { if (e.key === "Escape") close(); };
  function close() { closed = true; overlay.remove(); document.removeEventListener("keydown", onKey); }

  async function search(p) {
    status.replaceChildren(h("span", { class: "row" }, h("span", { class: "spinner" }), "Searching…"));
    more.hidden = true;
    try {
      const r = await api("GET", `/api/projects/${encodeURIComponent(slug)}/scenes/${i}/candidates?` +
        new URLSearchParams({ q: q.value, source: src.value, page: p }));
      if (closed) return;
      page = p; sceneSeconds = r.scene_seconds;
      if (p === 1) grid.replaceChildren();
      r.results.forEach(c => grid.append(card(c)));
      const shown = grid.children.length;
      const notes = [];
      if (src.value !== "nasa" && !r.stock_providers.length) notes.push("No stock footage key is set, so stock searches return nothing (see the README).");
      notes.push(...r.errors);
      status.replaceChildren(
        shown ? `${shown} result(s). Click one to use it for scene ${i + 1}.` : "Nothing found. Try other or fewer words.",
        sceneSeconds ? ` The scene lasts ${Math.round(sceneSeconds)} s; shorter clips loop.` : "",
        ...notes.map(n => h("div", { class: "warn-text" }, n)));
      more.hidden = r.results.length === 0;
    } catch (e) { if (!closed) status.replaceChildren(h("span", { class: "warn-text" }, e.message)); }
  }

  function card(c) {
    const media = h("div", { class: "pmedia" }, c.thumb ? h("img", { src: c.thumb, alt: "", loading: "lazy", referrerpolicy: "no-referrer" }) : "No preview");
    if (c.kind === "video" && c.preview) {
      let vid = null;
      media.addEventListener("mouseenter", () => {
        vid = h("video", { src: c.preview, muted: true, autoplay: true, loop: true, playsinline: true });
        vid.muted = true;
        media.append(vid);
      });
      media.addEventListener("mouseleave", () => { if (vid) { vid.remove(); vid = null; } });
    }
    const short = c.kind === "video" && sceneSeconds && c.duration && c.duration < sceneSeconds;
    return h("button", { class: "pcard" + (c.current ? " current" : ""), title: c.credit, onclick: () => pick(c) },
      media,
      h("div", { class: "pinfo" },
        chip(c.provider),
        c.kind === "video" ? chip(c.duration ? `${c.duration} s${short ? " · loops" : ""}` : "video", short ? "warn" : "") : chip("photo"),
        c.current ? chip("current", "ok") : null,
        c.used_in ? chip(`in scene ${c.used_in}`, "warn") : null,
        c.rejected && !c.current ? chip("skipped before") : null),
      h("div", { class: "pcredit muted small" }, c.title || c.credit));
  }

  function pick(c) {
    if (c.current) { toast("That is already this scene's visual"); return; }
    if (c.used_in && !confirm(`Scene ${c.used_in} already uses this. Use it for scene ${i + 1} too?`)) return;
    close();
    startJob("pick_visual", { scene: i, candidate: c.id, query: q.value.trim() });
  }

  overlay.append(h("div", { class: "modal panel", role: "dialog", "aria-label": `Choose a visual for scene ${i + 1}` },
    h("div", { class: "row between" }, h("h2", { style: "margin:0" }, `Choose a visual for scene ${i + 1}`),
      h("button", { class: "small", onclick: close }, "Close")),
    h("div", { class: "narr muted small" }, s.narration),
    h("form", { class: "row", onsubmit: e => { e.preventDefault(); search(1); } },
      src, q, h("button", { class: "primary", type: "submit" }, "Search")),
    status, grid, h("div", { class: "row", style: "justify-content:center" }, more)));
  document.body.append(overlay);
  document.addEventListener("keydown", onKey);
  q.focus();
  search(1);
}

// ---------------------------------------------------------------- video tab
function videoTab(p) {
  const st = S.status || { keys: {}, settings: {} };
  const voiceText = p.voice_todo.length ? `${p.voice_todo.length} scene(s) need narration (${p.voice_todo_chars} characters)` : "Up to date";
  const videoText = { ready: "Up to date", outdated: "Out of date with your latest changes", missing: "Not rendered yet" }[p.video];
  return h("div", {},
    h("div", { class: "panel" },
      h("table", { class: "kv" },
        h("tr", {}, h("td", {}, "Voiceover"), h("td", {}, voiceText)),
        h("tr", {}, h("td", {}, "Visuals"), h("td", {}, `${p.visuals_ready} of ${p.scenes} scenes`)),
        h("tr", {}, h("td", {}, "Video"), h("td", {}, videoText)),
        h("tr", {}, h("td", {}, "Music"), h("td", {}, musicPicker(p))),
        h("tr", {}, h("td", {}, "Sound"), h("td", {}, `Voice tone ${st.settings.VOICE_TONE || "soft"}, `,
          `music level ${Math.round((st.settings.MUSIC_VOLUME || 0) * 100)}% (${Math.round(-20 * Math.log10(st.settings.MUSIC_VOLUME || 0.1))} dB below the voice)`,
          " · ", h("a", { href: "#/settings" }, "change")))),
      h("div", { class: "row", style: "margin-top:.75rem" },
        h("button", { class: "primary", disabled: p.voice_todo.length > 0, onclick: () => startJob("render") },
          p.video === "missing" ? "Render video" : "Render again"),
        h("button", { disabled: p.voice_todo.length === p.scenes, title: "Mix the first 45 seconds with the current sound settings",
          onclick: () => startJob("preview", { seconds: 45 }) }, "Preview sound (45 s)"),
        p.preview_url ? h("audio", { controls: true, src: p.preview_url, preload: "none" }) : null),
      h("p", { class: "muted small" }, "Rendering reuses every scene that did not change, so a re-render after small edits only redoes the affected scenes, then joins and mixes.")),
    p.final_url ? h("div", { class: "panel" },
      h("video", { class: "player", controls: true, preload: "metadata", src: p.final_url }),
      h("div", { class: "row", style: "margin-top:.5rem" },
        h("a", { class: "btn small", href: p.final_url, download: `${p.slug}.mp4` }, "Download MP4"),
        p.captions_url ? h("a", { class: "btn small", href: p.captions_url, download: `${p.slug}.srt` }, "Download captions") : null)) : null,
    thumbnailPanel(p),
    uploadPanel(p, st, "video"));
}

function trackName(path) { return String(path).split("/").pop(); }

function musicPicker(p) {
  const m = p.music || {};
  const names = ((S.status && S.status.music_files) || []).map(trackName);
  const explicit = m.choice && !m.auto ? m.choice : "";
  const defaultLabel = explicit
    ? `Default (${m.default})`
    : `Default: ${m.track || "no music"}${m.auto ? " (picked for this video)" : ""}`;
  const select = h("select", { onchange: async e => {
    try {
      S.project = await api("PUT", `/api/projects/${encodeURIComponent(p.slug)}/music`, { music: e.target.value || null });
      renderProject();
      toast("Music changed. Render again to hear it; only the sound is re-mixed, which takes about a minute.");
    } catch (err) { toast(err.message, true); renderProject(); }
  } },
    h("option", { value: "" }, defaultLabel),
    h("option", { value: "none", selected: explicit === "none" ? true : null }, "No music"),
    names.map(n => h("option", { value: n, selected: explicit === n ? true : null }, n)));
  return h("div", { class: "row" }, select,
    m.track ? h("audio", { controls: true, preload: "none", src: `/music/${encodeURIComponent(m.track)}`, class: "mini" }) : null,
    h("a", { class: "small", href: "#/settings" }, "Music library"));
}

function thumbnailPanel(p) {
  const d = S.draft;
  if (!d) return null;
  return h("div", { class: "panel row", style: "align-items:flex-start" },
    p.thumbnail_url ? h("img", { class: "thumbimg", src: p.thumbnail_url, alt: "Thumbnail" }) : h("div", { class: "muted" }, "The thumbnail is made when the video renders."),
    h("div", { class: "stack grow" },
      h("h2", {}, "Thumbnail"),
      h("label", {}, h("span", {}, "Text"), h("input", { value: d.thumbnail_text || "", maxlength: 60, style: "width:100%",
        oninput: e => { d.thumbnail_text = e.target.value; markDirty(); } })),
      h("p", { class: "muted small" }, "Pick the background scene with “Use for thumbnail” on the Visuals tab."),
      p.thumbnail_outdated ? chip("older than the script", "warn") : null,
      h("button", { disabled: p.video === "missing", onclick: () => startJob("thumbnail") }, "Save and update thumbnail")));
}

function uploadPanel(p, st, kind) {
  const isShort = kind === "short";
  const yt = isShort ? p.short.youtube : p.youtube;
  const ready = isShort ? p.short.state === "ready" : p.video === "ready";
  const what = isShort ? "the Short" : "the video";
  if (yt) {
    return h("div", { class: "panel" }, h("h2", {}, isShort ? "Short on YouTube" : "YouTube"),
      h("p", {}, "Uploaded ", ago(Date.parse(yt.uploaded_at) / 1000), ": ", h("a", { href: yt.url, target: "_blank", rel: "noopener" }, yt.url)),
      h("p", { class: "muted small" }, `To upload a new version, delete ${isShort ? "short/youtube.json" : "youtube.json"} from the project folder (and the old one in YouTube Studio).`));
  }
  const privacy = h("select", {}, ["private", "unlisted", "public"].map(v => h("option", { value: v }, v)));
  const when = h("input", { type: "datetime-local" });
  const synthetic = h("input", { type: "checkbox" });
  const warn = !st.keys.youtube_client ? "client_secret.json is missing, so uploading is not set up (see the README)."
    : !st.keys.youtube_login ? "Sign in to YouTube once from a terminal with: .venv/bin/python pipeline.py --login" : null;
  return h("div", { class: "panel stack" }, h("h2", {}, isShort ? "Upload the Short" : "Upload to YouTube"),
    warn ? h("div", { class: "callout warn panel" }, warn) : null,
    isShort && !p.youtube ? h("p", { class: "muted small" }, "Tip: upload the full video first, so the Short's description can link to it.") : null,
    h("div", { class: "field-grid" },
      h("label", {}, h("span", {}, "Privacy"), privacy),
      h("label", {}, h("span", {}, "Schedule (optional, your local time)"), when),
      h("label", { class: "row" }, synthetic, h("span", { style: "margin:0" }, "Contains realistic AI-generated visuals"))),
    h("p", { class: "muted small" }, "Scheduled uploads stay private and go public at that time. Watch the result in YouTube Studio before it goes live."),
    h("button", { class: "primary", disabled: !ready || !!warn, onclick: () => {
      const extra = { privacy: privacy.value, synthetic: synthetic.checked };
      if (when.value) extra.publish_at = new Date(when.value).toISOString();
      startJob(isShort ? "upload_short" : "upload", extra,
        `Upload ${isShort ? "the Short for " : ""}“${p.title}” to YouTube as ${extra.publish_at ? "private, scheduled" : privacy.value}?`);
    } }, ready ? "Upload" : isShort ? "Make the Short first" : "Render the video first"));
}

// ---------------------------------------------------------------- short tab
function shortTab(p) {
  const sh = p.short;
  if (!sh || !p.script) return h("p", { class: "muted" }, "No script yet.");
  const st = S.status || { keys: {}, settings: {} };
  const scenes = p.script.scenes;   // the saved script: scene numbers must match the server's
  const secs = sh.scene_seconds;
  const v = { first: sh.first, last: sh.last, title: sh.title };
  const label = i => `${i + 1}. ${scenes[i].narration.slice(0, 60)}${scenes[i].narration.length > 60 ? "…" : ""}`;
  const total = h("strong", {});
  const note = h("span", { class: "small" });
  const update = () => {
    const t = secs.slice(v.first, v.last + 1).reduce((a, b) => a + b, 0);
    total.textContent = `${Math.round(t)} s`;
    note.className = "small " + (t > sh.max_seconds ? "warn-text" : "muted");
    note.textContent = t > sh.max_seconds ? ` YouTube Shorts can be at most ${sh.max_seconds / 60} minutes; pick fewer scenes.`
      : t > sh.target_seconds ? " Longer than a minute is allowed, but under 60 s usually does better." : " Good length for a Short.";
  };
  const firstSel = h("select", { onchange: e => { v.first = Number(e.target.value); if (v.last < v.first) { v.last = v.first; lastSel.value = v.last; } update(); } },
    scenes.map((_, i) => h("option", { value: i, selected: i === v.first ? true : null }, label(i))));
  const lastSel = h("select", { onchange: e => { v.last = Number(e.target.value); if (v.first > v.last) { v.first = v.last; firstSel.value = v.first; } update(); } },
    scenes.map((_, i) => h("option", { value: i, selected: i === v.last ? true : null }, label(i))));
  const title = h("input", { value: v.title, maxlength: 90, style: "width:100%", oninput: e => { v.title = e.target.value; } });
  update();
  const saveShort = async () => {
    if (v.first === sh.first && v.last === sh.last && v.title === sh.title) return true;
    try { S.project = await api("PUT", `/api/projects/${encodeURIComponent(p.slug)}/short`, v); return true; }
    catch (e) { toast(e.message, true); return false; }
  };
  const stateText = { ready: "Up to date", outdated: "Out of date with your latest changes", missing: "Not made yet" }[sh.state];
  return h("div", {},
    h("div", { class: "panel stack" },
      h("h2", {}, "Short (vertical, up to a minute)"),
      h("p", { class: "muted small" }, "Made from a run of scenes in this video: the same narration and visuals, cropped to 9:16, with large captions and the title at the top. It costs nothing extra: no new narration."),
      h("div", { class: "field-grid" },
        h("label", {}, h("span", {}, "From scene"), firstSel),
        h("label", {}, h("span", {}, "To scene"), lastSel),
        h("label", { style: "grid-column:1/-1" }, h("span", {}, "Title shown at the top (and used on YouTube)"), title)),
      h("div", {}, "Length: ", total, note),
      h("div", { class: "row" },
        h("button", { class: "primary", disabled: p.voice_todo.length > 0, onclick: async () => {
          if (await saveShort()) startJob("short");
        } }, sh.state === "missing" ? "Make the Short" : "Save and make again"),
        h("button", { onclick: async () => { if (await saveShort()) { renderProject(); toast("Saved"); } } }, "Save"),
        h("span", { class: "muted small" }, stateText),
        p.voice_todo.length ? h("span", { class: "warn-text small" }, "Generate the voiceover first.") : null)),
    sh.url ? h("div", { class: "panel" },
      h("video", { class: "player vertical", controls: true, preload: "metadata", src: sh.url }),
      h("div", { class: "row", style: "margin-top:.5rem" },
        h("a", { class: "btn small", href: sh.url, download: `${p.slug}-short.mp4` }, "Download MP4"))) : null,
    uploadPanel(p, st, "short"));
}

// ---------------------------------------------------------------- log tab
function logTab(p) {
  const jobs = p.jobs || [];
  const pick = jobs.find(j => j.id === S.logJobId) || jobs[0];
  const pre = h("pre", { id: "joblog" }, "");
  if (pick) {
    if (S.job && pick.id === S.job.id) pre.textContent = S.jobLines.join("\n");
    else api("GET", `/api/jobs/${pick.id}?since=0`).then(j => { pre.textContent = (j.lines || []).join("\n") + (j.error ? `\n\nError: ${j.error}` : ""); }).catch(() => {});
  }
  return h("div", {},
    jobs.length ? h("div", { class: "panel" }, h("h2", {}, "Jobs since the web UI started"),
      h("div", { class: "row" }, jobs.map(j => h("button", { class: "small" + (pick && j.id === pick.id ? " primary" : ""),
        onclick: () => { S.logJobId = j.id; renderProject(); } },
        `${j.label} · ${j.status}`))),
      pre) : h("p", { class: "muted" }, "No jobs for this video since the web UI started."),
    p.activity ? h("details", { class: "panel" }, h("summary", {}, "Earlier activity (activity.log)"), h("pre", {}, p.activity)) : null);
}

function updateJobLog() {
  const pre = document.getElementById("joblog");
  if (pre && S.job && S.logJobId === S.job.id) {
    pre.textContent = S.jobLines.join("\n");
    pre.scrollTop = pre.scrollHeight;
  }
}

// ---------------------------------------------------------------- topic queue
async function showQueue() {
  renderQueue(await api("GET", "/api/queue"));
}

function renderQueue(q) {
  const minutes = h("input", { type: "number", min: 1, max: 30, step: 0.5, value: 6, style: "width:5.5rem" });
  const add = h("input", { class: "grow", placeholder: "New topic", maxlength: 200 });
  const save = async topics => { try { renderQueue(await api("PUT", "/api/queue", { topics })); } catch (e) { toast(e.message, true); } };
  const topics = q.topics.slice();
  $app.replaceChildren(
    h("h1", {}, "Topic queue"),
    h("p", { class: "muted" }, "Scheduled runs (pipeline.py --topics-file topics.txt --yes) take the top topic. Here you can reorder the queue, or start a video from any topic and review it before it is narrated."),
    h("form", { class: "panel row", onsubmit: e => { e.preventDefault(); if (add.value.trim()) save(topics.concat([add.value.trim()])); } },
      add, h("button", { class: "primary", type: "submit" }, "Add to queue"),
      h("label", { class: "row" }, h("span", { class: "muted small", style: "margin:0" }, "Length for new videos"), minutes)),
    h("div", { class: "panel" }, topics.length ? topics.map((t, i) => h("div", { class: "queue-item" },
      h("span", { class: "muted small" }, `${i + 1}.`), h("span", { class: "t" }, t),
      q.existing[t] ? h("a", { class: "btn small", href: `#/p/${q.existing[t]}` }, "Open video")
        : h("button", { class: "small", onclick: async () => {
          try {
            const r = await api("POST", "/api/projects", { topic: t, minutes: Number(minutes.value), from_queue: true });
            location.hash = `#/p/${r.slug}`;
          } catch (e) { toast(e.message, true); }
        } }, "Start video"),
      h("button", { class: "small", disabled: i === 0, onclick: () => { [topics[i - 1], topics[i]] = [topics[i], topics[i - 1]]; save(topics); } }, "↑"),
      h("button", { class: "small", disabled: i === topics.length - 1, onclick: () => { [topics[i + 1], topics[i]] = [topics[i], topics[i + 1]]; save(topics); } }, "↓"),
      h("button", { class: "small danger", onclick: () => { if (confirm(`Remove “${t}” from the queue?`)) save(topics.filter((_, k) => k !== i)); } }, "✕")))
      : h("p", { class: "muted" }, "The queue is empty.")),
    q.failed.length ? h("div", { class: "panel" }, h("h2", {}, "Failed topics"),
      q.failed.map(f => h("div", { class: "queue-item" }, h("span", { class: "muted small" }, f.date), h("span", { class: "t" }, f.topic, h("div", { class: "muted small" }, f.note)),
        h("button", { class: "small", onclick: async () => { try { renderQueue(await api("POST", "/api/queue/requeue", { topic: f.topic })); } catch (e) { toast(e.message, true); } } }, "Back to queue")))) : null,
    q.done.length ? h("details", { class: "panel" }, h("summary", {}, `Done (${q.done.length})`),
      q.done.map(f => h("div", { class: "queue-item" }, h("span", { class: "muted small" }, f.date), h("span", { class: "t" }, f.topic)))) : null);
}

// ---------------------------------------------------------------- settings
async function showSettings() {
  S.status = await api("GET", "/api/status");
  S.music = await api("GET", "/api/music");
  const projects = await api("GET", "/api/projects");
  renderSettings(projects.filter(p => p.voice_todo.length < p.scenes));
}

function renderSettings(voiced) {
  const st = S.status, v = st.settings;
  const vals = Object.assign({}, v);
  const dbText = x => `${Math.round(x * 100)}% · ${Math.round(-20 * Math.log10(x))} dB below the voice`;
  const level = h("span", { class: "muted small" }, dbText(v.MUSIC_VOLUME));
  const num = (key, min, max, step) => h("input", { type: "number", min, max, step, value: v[key], style: "width:6rem",
    oninput: e => { vals[key] = Number(e.target.value); } });
  const previewProject = h("select", {}, voiced.map(p => h("option", { value: p.slug }, p.title)));
  const previewPlayer = h("div", {});
  const save = async () => {
    try { S.status = await api("PUT", "/api/settings", vals); toast("Settings saved to .env"); return true; }
    catch (e) { toast(e.message, true); return false; }
  };
  $app.replaceChildren(
    h("h1", {}, "Settings"),
    st.problems.length ? h("div", { class: "panel callout bad" }, h("strong", {}, "Problems: "), st.problems.join("; ")) : null,
    h("div", { class: "panel stack" }, h("h2", {}, "Sound"),
      h("div", { class: "field-grid" },
        h("label", {}, h("span", {}, "Background music for videos without their own choice"),
          h("select", { onchange: e => { vals.MUSIC_PATH = e.target.value; } },
            h("option", { value: "" }, "No music"),
            st.music_files.length ? h("option", { value: st.music_library, selected: v.MUSIC_PATH === st.music_library ? true : null },
              `A different track for each video (${st.music_files.length} in ${st.music_library}/)`) : null,
            st.music_files.map(f => h("option", { value: f, selected: f === v.MUSIC_PATH ? true : null }, `Always ${trackName(f)}`)))),
        h("label", {}, h("span", {}, "Music level ", level),
          h("input", { type: "range", min: 0.01, max: 0.3, step: 0.01, value: v.MUSIC_VOLUME, style: "width:100%",
            oninput: e => { vals.MUSIC_VOLUME = Number(e.target.value); level.textContent = dbText(vals.MUSIC_VOLUME); } })),
        h("label", { class: "row" }, h("input", { type: "checkbox", checked: v.MUSIC_DUCKING, onchange: e => { vals.MUSIC_DUCKING = e.target.checked; } }),
          h("span", { style: "margin:0" }, "Lower the music while the narrator speaks")),
        h("label", {}, h("span", {}, "Voice tone"),
          h("select", { onchange: e => { vals.VOICE_TONE = e.target.value; } },
            st.voice_tones.map(t => h("option", { value: t, selected: t === v.VOICE_TONE ? true : null }, TONES[t] || t))))),
      h("p", { class: "muted small" }, "The music level is measured against the narration, so the same setting sounds the same with any track. 10% is a typical documentary bed; try 5% for very quiet. Sound settings apply at the final mix, so changing them only needs a re-render, never new narration."),
      voiced.length ? h("div", { class: "row" }, h("span", { class: "muted small" }, "Try it on"), previewProject,
        h("button", { onclick: async () => {
          if (!(await save())) return;
          try {
            const slug = previewProject.value;
            const job = await api("POST", `/api/projects/${encodeURIComponent(slug)}/jobs`, { action: "preview", seconds: 45 });
            previewPlayer.replaceChildren(h("span", { class: "row muted small" }, h("span", { class: "spinner" }), "Mixing a 45 second preview…"));
            follow(job, null, async j => {
              if (j.status !== "done") { previewPlayer.replaceChildren(); return; }
              const p = await api("GET", `/api/projects/${encodeURIComponent(slug)}`);
              previewPlayer.replaceChildren(h("audio", { controls: true, autoplay: true, src: p.preview_url }));
            });
          } catch (e) { toast(e.message, true); }
        } }, "Save and preview 45 s"), previewPlayer) : h("p", { class: "muted small" }, "Previews need a video with a voiceover.")),
    musicLibraryPanel(voiced),
    h("div", { class: "panel stack" }, h("h2", {}, "Voice generation (ElevenLabs)"),
      h("div", { class: "field-grid" },
        h("label", {}, h("span", {}, "Stability (0-1, higher is calmer and steadier)"), num("VOICE_STABILITY", 0, 1, 0.05)),
        h("label", {}, h("span", {}, "Similarity (0-1, lower can sound smoother)"), num("VOICE_SIMILARITY", 0, 1, 0.05)),
        h("label", {}, h("span", {}, "Style exaggeration (0-1, 0 is most neutral)"), num("VOICE_STYLE", 0, 1, 0.05)),
        h("label", {}, h("span", {}, "Speed (0.7-1.2)"), num("VOICE_SPEED", 0.7, 1.2, 0.05)),
        h("label", { class: "row" }, h("input", { type: "checkbox", checked: v.VOICE_SPEAKER_BOOST, onchange: e => { vals.VOICE_SPEAKER_BOOST = e.target.checked; } }),
          h("span", { style: "margin:0" }, "Speaker boost (off can sound less harsh)"))),
      h("p", { class: "muted small" }, "These only affect narration generated after you save. To hear them on an existing scene, use Re-voice on the Script tab (it costs that scene's characters).")),
    h("div", { class: "panel stack" }, h("h2", {}, "Video"),
      h("label", {}, h("span", {}, "Cross-fade between scenes in seconds (0 = hard cuts)"), num("CROSSFADE", 0, 2, 0.1))),
    providersPanel(vals),
    h("div", { class: "row" }, h("button", { class: "primary", onclick: async () => { if (await save()) renderSettings(voiced); } }, "Save settings")),
    statusPanel(st));
}

// Visual providers: switch on/off and order (first = tried first). Keys stay in .env.
function providersPanel(vals) {
  const list = (S.status.providers || []).map(p => Object.assign({}, p));
  const box = h("div", {});
  const sync = () => { vals.VISUAL_PROVIDERS = list.filter(p => p.enabled).map(p => p.id); };
  const draw = () => {
    box.replaceChildren(...list.map((p, i) => h("div", { class: "queue-item" },
      h("input", { type: "checkbox", checked: p.enabled, onchange: e => { p.enabled = e.target.checked; sync(); draw(); } }),
      h("span", { class: "t" }, h("strong", {}, p.name), " ", h("span", { class: "muted small" }, `${p.about} · ${p.kinds.join(", ")}`),
        h("div", { class: "small" }, !p.key ? chip("no key needed", "ok") : p.key_set ? chip(`${p.key} set`, "ok")
          : chip(`add ${p.key} to .env`, p.enabled ? "warn" : ""))),
      h("button", { class: "small", disabled: i === 0, title: "Try earlier", onclick: () => { [list[i - 1], list[i]] = [list[i], list[i - 1]]; sync(); draw(); } }, "↑"),
      h("button", { class: "small", disabled: i === list.length - 1, title: "Try later", onclick: () => { [list[i + 1], list[i]] = [list[i], list[i + 1]]; sync(); draw(); } }, "↓"))));
  };
  draw();
  return h("div", { class: "panel stack" }, h("h2", {}, "Visual sources"),
    h("p", { class: "muted small" }, "Ticked sources are searched in this order when a scene's visual is fetched automatically; the picker shows results from all of them. ",
      "Providers that need a key are skipped until the key is in .env (restart the web UI after adding one)."),
    box,
    h("label", { class: "row" }, h("input", { type: "checkbox", checked: vals.ALLOW_SHARE_ALIKE, onchange: e => { vals.ALLOW_SHARE_ALIKE = e.target.checked; } }),
      h("span", { style: "margin:0" }, "Also use CC BY-SA (share-alike) images and footage from Wikimedia Commons and Openverse. ",
        h("span", { class: "muted small" }, "Many more results, but share-alike can require your video to carry the same licence. Public domain, CC0 and CC BY are always allowed; everything is credited in the description."))));
}

function musicLibraryPanel(voiced) {
  const lib = S.music || { tracks: [], library: "music" };
  const file = h("input", { type: "file", accept: "audio/*", hidden: true, onchange: async e => {
    const f = e.target.files[0];
    if (!f) return;
    try {
      const fd = new FormData();
      fd.append("file", f);
      toast("Adding the track…");
      S.music = await api("POST", "/api/music", fd, true);
      S.status = await api("GET", "/api/status");
      renderSettings(voiced);
      toast(`${f.name} added to ${S.music.library}/`);
    } catch (err) { toast(err.message, true); }
  } });
  return h("div", { class: "panel stack" }, h("h2", {}, "Music library"),
    h("p", { class: "muted small" }, `Tracks in ${lib.library}/. Each video can use its own track (Video tab), or take the default above. `,
      "With “a different track for each video”, a new video gets the track used least so far and keeps it. Use only music you have the rights to, such as the YouTube Audio Library."),
    lib.tracks.length ? h("div", {}, lib.tracks.map(t => h("div", { class: "queue-item" },
      h("span", { class: "t" }, t.name, h("div", { class: "muted small" }, t.videos ? `chosen by ${t.videos} video(s)` : "not chosen by any video yet")),
      h("audio", { controls: true, preload: "none", src: t.url, class: "mini" })))) : h("p", { class: "muted" }, "No tracks yet."),
    h("div", { class: "row" }, h("button", { onclick: () => file.click() }, "Add a track…"), file));
}

function usageCell() {
  const cell = h("span", { class: "muted small" }, "Checking…");
  api("GET", "/api/elevenlabs?refresh=1").then(u => {
    if (u.error) { cell.replaceChildren(chip("unknown", "warn"), " ", h("span", { class: "muted small" }, u.error)); return; }
    const pct = u.limit ? Math.round(100 * u.left / u.limit) : 0;
    const resets = u.resets ? `, resets ${new Date(u.resets * 1000).toLocaleDateString()}` : "";
    cell.replaceChildren(chip(`${u.left.toLocaleString()} of ${u.limit.toLocaleString()} left`, pct < 15 ? "bad" : pct < 35 ? "warn" : "ok"),
      h("span", { class: "muted small" }, ` (${u.used.toLocaleString()} used this month${resets}; a 6-minute video is about 5,500)`));
  }).catch(e => cell.replaceChildren(chip("unknown", "warn"), " ", e.message));
  return cell;
}

function statusPanel(st) {
  const yes = ok => (ok ? chip("set", "ok") : chip("missing", "bad"));
  return h("div", { class: "panel", style: "margin-top:1rem" }, h("h2", {}, "Setup"),
    h("table", { class: "kv" },
      h("tr", {}, h("td", {}, "Claude API key"), h("td", {}, yes(st.keys.anthropic))),
      h("tr", {}, h("td", {}, "ElevenLabs key and voice"), h("td", {}, yes(st.keys.elevenlabs && st.keys.voice_id))),
      h("tr", {}, h("td", {}, "ElevenLabs characters"), h("td", {}, usageCell())),
      h("tr", {}, h("td", {}, "Stock footage"), h("td", {}, st.stock_providers.length ? st.stock_providers.join(", ") : chip("none: NASA images only", "warn"))),
      h("tr", {}, h("td", {}, "YouTube"), h("td", {}, st.keys.youtube_client ? (st.keys.youtube_login ? chip("signed in", "ok") : chip("not signed in: run pipeline.py --login", "warn")) : chip("client_secret.json missing", "bad"))),
      h("tr", {}, h("td", {}, "Models"), h("td", {}, `writer ${st.models.writer}, fact-check ${st.models.factcheck}, voice ${st.models.voice}`)),
      h("tr", {}, h("td", {}, "FFmpeg"), h("td", {}, st.ffmpeg || chip("not found", "bad"), " ", st.xfade ? chip("cross-fades", "ok") : chip("hard cuts only", "warn"))),
      h("tr", {}, h("td", {}, "Projects folder"), h("td", {}, st.projects_dir))),
    h("p", { class: "muted small" }, "API keys and models are changed in the .env file; restart the web UI afterwards."));
}

// ---------------------------------------------------------------- start
route();
pollActivity();
