// Fault Lines web client. One store, fed by either the live SSE feed (server.py) or a local replay of
// web/data/match-*.json. Both tabs render from the store; nothing here computes analytics, it only draws them.
const $ = (id) => document.getElementById(id);
const NS = "http://www.w3.org/2000/svg";
const S = {
  meta: null, events: [], cues: [], minutes: [], snap: null,
  follow: "home", mode: "Casual", lang: "English", fav: "", selected: "", showOpp: false,
  source: null, live: false, t: 0, speed: 10, playing: true,
  lastEventT: 0, lastEventAt: 0, delays: [], procs: [], dirty: true, lastAnalysis: 0,
};
const params = new URLSearchParams(location.search);
const LIVE = (params.get("live") ?? window.FAULTLINES_LIVE_URL ?? "").replace(/\/$/, "");

// ---------- helpers ----------
const el = (tag, attrs = {}, parent) => {
  const n = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  if (parent) parent.appendChild(n);
  return n;
};
const px = (x, y) => [x * 1.05, y * 0.68];
const clock = (t) => `${String(Math.floor(t / 60)).padStart(2, "0")}:${String(Math.floor(t % 60)).padStart(2, "0")}`;
const surname = (n) => (n || "").split(" ").slice(-1)[0];
const pct = (v) => `${Math.round(v * 100)}%`;
const xpa = (v) => `${v >= 0 ? "+" : ""}${(v ?? 0).toFixed(2)}`;
const teamColor = (t) => getComputedStyle(document.documentElement).getPropertyValue(t === "home" ? "--home" : "--away").trim();
const other = (t) => (t === "home" ? "away" : "home");
function fragColor(f) {
  const stops = [[43, 58, 74], [255, 210, 63], [255, 77, 94]];
  const u = Math.min(1, f / 0.35) * 2, i = Math.min(1, Math.floor(u)), w = u - i;
  const c = stops[i].map((v, k) => Math.round(v + (stops[i + 1][k] - v) * w));
  return `rgb(${c})`;
}
const quantile = (a, q) => (a.length ? [...a].sort((x, y) => x - y)[Math.floor((a.length - 1) * q)] : 0);
const median = (a) => quantile(a, 0.5);

// ---------- pitch ----------
function drawPitch(svg) {
  svg.innerHTML = "";
  el("rect", { x: -2, y: -2, width: 109, height: 72, fill: "var(--pitch)", rx: 1.5 }, svg);
  for (let i = 0; i < 10; i++) if (i % 2) el("rect", { x: i * 10.5, y: 0, width: 10.5, height: 68, fill: "rgba(255,255,255,.025)" }, svg);
  const g = el("g", { fill: "none", stroke: "var(--pitch-line)", "stroke-width": 0.25 }, svg);
  el("rect", { x: 0, y: 0, width: 105, height: 68 }, g);
  el("line", { x1: 52.5, y1: 0, x2: 52.5, y2: 68 }, g);
  el("circle", { cx: 52.5, cy: 34, r: 9.15 }, g);
  for (const [x, dir] of [[0, 1], [105, -1]]) {
    el("rect", { x: dir > 0 ? 0 : 105 - 16.5, y: 13.84, width: 16.5, height: 40.32 }, g);
    el("rect", { x: dir > 0 ? 0 : 105 - 5.5, y: 24.84, width: 5.5, height: 18.32 }, g);
    el("rect", { x: dir > 0 ? -1.5 : 105, y: 30.34, width: 1.5, height: 7.32 }, g);
    el("circle", { cx: x + dir * 11, cy: 34, r: 0.3, fill: "var(--pitch-line)" }, g);
  }
  return { edges: el("g", {}, svg), opp: el("g", {}, svg), trail: el("g", {}, svg), nodes: el("g", {}, svg) };
}
const pitch = drawPitch($("pitch"));
const mini = drawPitch($("pp-passes"));

function renderPitch() {
  for (const g of Object.values(pitch)) g.innerHTML = "";
  if (!S.snap) return;
  const ts = S.snap.teams[S.follow], color = teamColor(S.follow);
  const pos = Object.fromEntries(ts.players.map((p) => [p.name, p]));
  const maxW = Math.max(1, ...ts.edges.map((e) => e[2]));
  for (const [a, b, w] of ts.edges) {
    const [x1, y1] = px(pos[a].x, pos[a].y), [x2, y2] = px(pos[b].x, pos[b].y);
    el("line", { x1, y1, x2, y2, stroke: "#fff", "stroke-opacity": 0.12 + 0.6 * (w / maxW), "stroke-width": 0.15 + 1.1 * (w / maxW), "stroke-linecap": "round" }, pitch.edges);
  }
  if (S.showOpp) for (const p of S.snap.teams[other(S.follow)].players) {
    const [x, y] = px(p.x, p.y);
    el("circle", { cx: x, cy: y, r: 1.4, fill: teamColor(other(S.follow)), opacity: 0.45 }, pitch.opp);
  }
  // Ball trail: the last few on-ball actions, newest brightest
  const recent = S.events.slice(-6);
  recent.forEach((e, i) => {
    const [x1, y1] = px(e.x, e.y), [x2, y2] = e.type === "shot" ? px(e.team === "home" ? 100 : 0, 50) : px(e.ex, e.ey);
    const o = (i + 1) / recent.length;
    el("line", { x1, y1, x2, y2, stroke: e.ok || e.type === "shot" ? teamColor(e.team) : "var(--bad)", "stroke-width": 0.5, opacity: o * 0.9, "stroke-dasharray": e.ok ? "" : "1 0.8" }, pitch.trail);
    if (i === recent.length - 1) el("circle", { cx: x2, cy: y2, r: 0.9, fill: "#fff", stroke: "#000", "stroke-width": 0.2, class: "ball" }, pitch.trail);
  });
  for (const p of ts.players) {
    const [x, y] = px(p.x, p.y), r = 1.3 + Math.sqrt(p.passes) * 0.32;
    if (p.name === ts.spof) el("circle", { cx: x, cy: y, r: r + 1.1, fill: "none", stroke: "var(--accent)", "stroke-width": 0.6, class: "ring" }, pitch.nodes);
    if (p.name === S.fav) el("circle", { cx: x, cy: y, r: r + 2.1, fill: "none", stroke: "#fff", "stroke-width": 0.35, "stroke-dasharray": "1 0.7" }, pitch.nodes);
    const c = el("circle", { cx: x, cy: y, r, fill: fragColor(p.fragility), stroke: color, "stroke-width": 0.45, style: "cursor:pointer" }, pitch.nodes);
    el("title", {}, c).textContent = `${p.name} (${p.role}): ${pct(p.fragility)} of the network, ${p.passes} passes`;
    c.addEventListener("click", () => { S.selected = p.name; setTab("analysis"); });
    const t = el("text", { x, y: y + r + 2.4, "text-anchor": "middle", "font-size": 2.3, fill: p.name === S.fav ? "var(--accent)" : "#fff", "font-weight": p.name === ts.spof ? 700 : 400 }, pitch.nodes);
    t.textContent = surname(p.name);
  }
}

// ---------- charts ----------
function lineChart(svg, series, { yMax, yMin = 0, xMax = 95, marker, w = +svg.viewBox.baseVal.width, h = +svg.viewBox.baseVal.height, fmt = (v) => v } = {}) {
  svg.innerHTML = "";
  const L = 28, B = 14, X = (x) => L + (x / xMax) * (w - L - 4), Y = (y) => h - B - ((y - yMin) / (yMax - yMin || 1)) * (h - B - 6);
  for (const v of [yMin, (yMin + yMax) / 2, yMax]) {
    el("line", { x1: L, x2: w - 4, y1: Y(v), y2: Y(v), stroke: "var(--line)", "stroke-width": 1 }, svg);
    el("text", { x: L - 4, y: Y(v) + 3, "text-anchor": "end", "font-size": 9, fill: "var(--muted)" }, svg).textContent = fmt(v);
  }
  for (const m of [0, 45, 90]) el("text", { x: X(m), y: h - 2, "font-size": 9, fill: "var(--muted)", "text-anchor": "middle" }, svg).textContent = `${m}'`;
  for (const s of series) {
    if (s.pts.length < 2) continue;
    el("polyline", { points: s.pts.map(([x, y]) => `${X(x)},${Y(y)}`).join(" "), fill: "none", stroke: s.color, "stroke-width": s.width ?? 2, "stroke-dasharray": s.dash ?? "", "stroke-linejoin": "round" }, svg);
  }
  if (marker != null) el("line", { x1: X(marker), x2: X(marker), y1: 4, y2: h - B, stroke: "var(--accent)", "stroke-opacity": 0.5, "stroke-dasharray": "3 3" }, svg);
}
const minuteSeries = (fn) => S.minutes.map((s, m) => (s && m >= 5 ? [m, fn(s)] : null)).filter(Boolean);  // first minutes are noise

// ---------- live tab ----------
function renderLive() {
  const ts = S.snap?.teams[S.follow];
  const t = currentT();
  const score = { home: 0, away: 0 };
  for (const e of S.events) if (e.goal) score[e.team]++;
  $("sb-score").textContent = `${score.home} - ${score.away}`;
  $("sb-clock").textContent = clock(t);
  renderPitch();
  if (ts) {
    const spof = ts.players.find((p) => p.name === ts.spof);
    $("fl-name").textContent = ts.spof;
    const early = S.snap.minute < 15;  // the rolling window is not full yet: flag the read as provisional
    $("fl-value").textContent = pct(spof.fragility) + (early ? " · early read" : "");
    const ai = AI.ctx === aiContext() ? AI.text : "";  // never show an AI line written for another team/mode/language
    $("fl-text").textContent = (early ? `Based on the first ${Math.floor(S.snap.minute)} minutes only. ` : "") + (ai || (S.snap.explain?.[S.follow]?.[S.mode] ?? ""));
    $("fl-source").textContent = ai ? `${AI.source === "ai" ? "Microsoft Foundry · grounded" : AI.source} · ${AI.minute}'` : AI.on ? "updating…" : "template";
    const fav = ts.players.find((p) => p.name === S.fav);
    $("fav-text").textContent = fav ? `Your player ${fav.name}: ${fav.passes} passes in the last 15 min, holds ${pct(fav.fragility)} of the network, expected points added ${xpa(fav.xpa)}.` : "";
  }
  const m = t / 60;
  lineChart($("spark-conn"), ["home", "away"].map((tm) => ({ color: teamColor(tm), pts: minuteSeries((s) => s.teams[tm].efficiency) })), { yMax: 45, marker: m, xMax: 95 });
  lineChart($("spark-prog"), ["home", "away"].map((tm) => ({ color: teamColor(tm), pts: minuteSeries((s) => s.teams[tm].progressive) })), { yMax: 50, marker: m, xMax: 95 });
  $("cues").innerHTML = S.cues.slice(-10).reverse().map((c) => `<li><time>${c.match_clock}</time><span><span class="kind">${c.cue.replaceAll("_", " ")}</span> ${c.headline}</span></li>`).join("");
  $("ticker").innerHTML = S.events.slice(-14).reverse().map((e) => {
    const cls = e.goal ? "goal" : e.ok || e.type === "shot" ? "" : "lost";
    const what = e.type === "shot" ? `${e.goal ? "GOAL" : "Shot"} ${surname(e.player)} (xG ${e.xg})` : `${surname(e.player)} → ${surname(e.to)} ${e.ok ? "✓" : "✗ lost"}`;
    return `<li class="${cls}"><time>${clock(e.t)}</time><span style="color:${cls ? "" : teamColor(e.team)}">${what}</span></li>`;
  }).join("");
  $("latency").textContent = S.live
    ? `feed delay ~${Math.round(median(S.delays))} ms · processing p95 ${quantile(S.procs, 0.95).toFixed(0)} ms`
    : `replay · ${S.speed}×`;
}

let ltTimer;
function lowerThird(c) {
  $("lt-kind").textContent = `${c.match_clock} · ${c.cue.replaceAll("_", " ")}`;
  $("lt-text").textContent = c.headline;
  $("lower-third").hidden = false;
  clearTimeout(ltTimer);
  ltTimer = setTimeout(() => ($("lower-third").hidden = true), Math.max(4, c.display_seconds) * 1000);
}

// ---------- analysis tab ----------
function renderAnalysis() {
  const team = S.follow, names = S.meta?.teams[team].players ?? [];
  const mins = S.minutes.map((s, m) => (s ? m : null)).filter((m) => m != null);
  const fragAt = (s, n) => s.teams[team].players.find((p) => p.name === n)?.fragility ?? 0;
  const avg = (n) => mins.reduce((a, m) => a + fragAt(S.minutes[m], n), 0) / (mins.length || 1);
  const rows = [...names].sort((a, b) => avg(b) - avg(a));
  const sel = S.selected || S.fav || S.snap?.teams[team].spof || rows[0];

  // Heat strip: players x minutes
  const W = Math.max(400, $("heat").clientWidth), rowH = 20, x0 = Math.min(160, W * 0.28), cw = (W - x0 - 4) / 95;  // pixel-true
  const heat = document.createElementNS(NS, "svg");
  heat.setAttribute("viewBox", `0 0 ${W} ${rows.length * rowH + 16}`);
  heat.style.width = "100%";
  rows.forEach((n, r) => {
    const g = el("g", { style: "cursor:pointer" }, heat);
    el("rect", { x: 0, y: r * rowH, width: W, height: rowH - 2, fill: n === sel ? "var(--panel-2)" : "transparent", rx: 3 }, g);
    el("text", { x: 4, y: r * rowH + 12, "font-size": 11, fill: n === sel ? "var(--accent)" : "var(--text)" }, g).textContent = n;
    for (const m of mins) el("rect", { x: x0 + (m - 1) * cw, y: r * rowH + 1, width: cw + 0.3, height: rowH - 4, fill: fragColor(fragAt(S.minutes[m], n)), opacity: m < 15 ? 0.35 : 1 }, g);
    g.addEventListener("click", () => { S.selected = n; S.dirty = true; S.lastAnalysis = 0; });
  });
  for (const m of [0, 15, 30, 45, 60, 75, 90]) el("text", { x: x0 + m * cw, y: rows.length * rowH + 12, "font-size": 10, fill: "var(--muted)" }, heat).textContent = `${m}'`;
  $("heat").replaceChildren(heat);

  // Player profile
  const full = mins.filter((m) => m >= 15);  // full rolling window only
  const series = full.map((m) => [m, fragAt(S.minutes[m], sel)]);
  const teamAvg = full.map((m) => [m, S.minutes[m].teams[team].players.reduce((a, p) => a + p.fragility, 0) / 11]);
  const now = S.snap?.teams[team].players.find((p) => p.name === sel);
  // Peak only over minutes with a full rolling window; earlier reads rest on too few passes
  const peak = series.filter(([m]) => m >= 15).reduce((b, p) => (p[1] > b[1] ? p : b), [0, 0]);
  $("pp-name").textContent = sel ? `${sel}${now ? ` · ${now.role}` : ""}` : "Choose a player";
  const tiles = now ? [
    [pct(now.fragility), "network held now"], peak[0] ? [pct(peak[1]), `peak (${peak[0]}')`] : ["–", "peak (from 15')"], [now.passes, "passes, last 15 min"],
    [now.betweenness.toFixed(2), "betweenness"], [xpa(now.xpa), "expected points added"],
    [S.events.filter((e) => e.player === sel && e.type === "pass").length, "passes, match"],
  ] : [];
  $("pp-tiles").innerHTML = tiles.map(([v, l]) => `<div class="tile"><b>${v}</b><span>${l}</span></div>`).join("");
  lineChart($("pp-line"), [{ pts: teamAvg, color: "#8b98a5", width: 1.5, dash: "4 3" }, { pts: series, color: "var(--accent)", width: 2.5 }],
    { yMax: Math.max(0.35, peak[1] + 0.05), fmt: (v) => pct(v), marker: currentT() / 60 });

  // Pass map + partners
  for (const g of Object.values(mini)) g.innerHTML = "";
  for (const e of S.events) if (e.player === sel && e.type === "pass") {
    const [x1, y1] = px(e.x, e.y), [x2, y2] = px(e.ex, e.ey);
    el("line", { x1, y1, x2, y2, stroke: e.ok ? "var(--good)" : "var(--bad)", "stroke-width": 0.35, opacity: 0.75 }, mini.trail);
    el("circle", { cx: x2, cy: y2, r: 0.45, fill: e.ok ? "var(--good)" : "var(--bad)" }, mini.trail);
  }
  const partners = (S.snap?.teams[team].edges ?? []).filter((e) => e[0] === sel || e[1] === sel)
    .map(([a, b, w]) => [a === sel ? b : a, w]).sort((a, b) => b[1] - a[1]).slice(0, 6);
  const top = partners[0]?.[1] || 1;
  $("pp-partners").innerHTML = partners.map(([n, w]) => `<div><span>${n}</span><i style="width:${(w / top) * 100}%"></i><b>${w}</b></div>`).join("") || `<span class="muted small">No passes in the window</span>`;

  // Team comparison
  lineChart($("tc-conn"), ["home", "away"].map((tm) => ({ color: teamColor(tm), pts: minuteSeries((s) => s.teams[tm].efficiency) })), { yMax: 45, marker: currentT() / 60 });
  lineChart($("tc-prog"), ["home", "away"].flatMap((tm) => [
    { color: teamColor(tm), pts: minuteSeries((s) => s.teams[tm].progressive) },
    { color: teamColor(tm), dash: "2 3", width: 1.5, pts: minuteSeries((s) => s.teams[tm].completion * 100) }]), { yMax: 100, marker: currentT() / 60 });
  $("an-text").textContent = S.snap?.explain?.[team]?.Analyst ?? "";
  renderChips();
  $("ask-off").hidden = AI.on;
  const tname = S.meta?.teams[team].name;
  $("moments").innerHTML = S.cues.filter((c) => c.team === tname || c.cue === "goal").slice(-12).reverse()
    .map((c) => `<li><time>${c.match_clock}</time><span><span class="kind">${c.cue.replaceAll("_", " ")}</span> ${c.headline}</span></li>`).join("");
}

// ---------- store updates ----------
function reset(meta) {
  Object.assign(S, { meta, events: [], cues: [], minutes: [], snap: null, selected: "" });
  $("follow-home").textContent = meta.teams.home.name;
  $("follow-away").textContent = meta.teams.away.name;
  $("sb-home").textContent = meta.teams.home.name;
  $("sb-away").textContent = meta.teams.away.name;
  fillFavourites();
}
function fillFavourites() {
  const keep = S.fav;
  $("favourite").innerHTML = `<option value="">(none)</option>` + (S.meta?.teams[S.follow].players ?? []).map((p) => `<option>${p}</option>`).join("");
  $("favourite").value = S.meta?.teams[S.follow].players.includes(keep) ? keep : "";
  S.fav = $("favourite").value;
}
function addSnapshot(s) {
  S.snap = s;
  const m = Math.floor(s.minute);
  if (!S.minutes[m]) S.minutes[m] = s;
}
function addCues(cues, announce) {
  for (const c of cues) { S.cues.push(c); if (announce) lowerThird(c); }
}
function currentT() {
  if (!S.live) return S.t;
  return S.lastEventT + Math.min(8, ((performance.now() - S.lastEventAt) / 1000) * S.speed);  // smooth clock between events
}

// ---------- sources ----------
function startLive() {
  stopSource();
  S.live = true;
  $("replay-controls").hidden = true;
  setBadge("live", "LIVE");
  const es = new EventSource(`${LIVE}/live`);
  S.source = { stop: () => es.close() };
  es.addEventListener("hello", (m) => {
    const d = JSON.parse(m.data);
    if (!d.meta) return;
    reset(d.meta);
    S.speed = d.speed;
    S.events = d.events ?? [];
    S.cues = d.cues ?? [];
    for (const s of d.history ?? []) addSnapshot(s);
    if (d.snapshot) addSnapshot(d.snapshot);
    const last = S.events.at(-1);
    if (last) { S.lastEventT = last.t; S.lastEventAt = performance.now(); }
    S.dirty = true;
  });
  es.addEventListener("kickoff", (m) => { reset(JSON.parse(m.data)); S.dirty = true; });
  es.addEventListener("event", (m) => {
    const d = JSON.parse(m.data);
    if (!S.meta) return;
    S.events.push(d.event);
    addCues(d.cues, true);
    if (d.snapshot) addSnapshot(d.snapshot);
    S.lastEventT = d.event.t; S.lastEventAt = performance.now();
    S.delays.push(Date.now() - d.server_ts); S.procs.push(d.process_ms);
    if (S.delays.length > 200) { S.delays.shift(); S.procs.shift(); }
    S.dirty = true;
  });
  es.onerror = () => setBadge("", "Reconnecting…");
  es.onopen = () => setBadge("live", "LIVE");
}

async function startReplay(file) {
  stopSource();
  S.live = false;
  $("replay-controls").hidden = false;
  setBadge("replay", "REPLAY");
  const data = await (await fetch(file)).json();
  reset(data.meta);
  let i = 0, c = 0, alive = true;
  const end = data.events.at(-1).t;
  $("seek").max = Math.ceil(end);
  const seekTo = (t) => {
    S.t = t; i = 0; c = 0;
    S.events = []; S.cues = []; S.minutes = []; S.snap = null;
    advance(false);
  };
  const advance = (announce) => {
    const before = i;
    while (i < data.events.length && data.events[i].t <= S.t) S.events.push(data.events[i++]);
    const newCues = [];
    while (c < data.cues.length && data.cues[c].t <= S.t) newCues.push(data.cues[c++]);
    addCues(newCues, announce);
    const m = Math.floor(S.t / 60);
    for (let k = 1; k <= Math.min(m, data.snapshots.length); k++) if (!S.minutes[k]) addSnapshot(data.snapshots[k - 1]);
    if (m >= 1) S.snap = data.snapshots[Math.min(m, data.snapshots.length) - 1];
    if (i !== before || newCues.length || !announce) S.dirty = true;
    else $("sb-clock").textContent = clock(S.t);  // between events only the clock moves
  };
  let prev = performance.now();
  const tick = (now) => {
    if (!alive) return;
    const dt = (now - prev) / 1000; prev = now;
    if (S.playing && S.t < end) {
      S.t = Math.min(end, S.t + dt * S.speed);
      $("seek").value = S.t;
      advance(true);
    }
    requestAnimationFrame(tick);
  };
  $("seek").oninput = () => seekTo(+$("seek").value);
  S.source = { stop: () => (alive = false) };
  seekTo(0);
  requestAnimationFrame(tick);
}
function stopSource() { S.source?.stop(); S.source = null; }
function setBadge(cls, text) { $("badge").className = `badge ${cls}`; $("badge").textContent = text; }

// ---------- UI wiring ----------
function setTab(name) {
  for (const t of ["live", "analysis"]) {
    $(`tab-${t}`).setAttribute("aria-selected", t === name);
    $(`panel-${t}`).hidden = t !== name;
  }
  S.dirty = true; S.lastAnalysis = 0;
}
$("tab-live").onclick = () => setTab("live");
$("tab-analysis").onclick = () => setTab("analysis");
for (const t of ["home", "away"]) $(`follow-${t}`).onclick = () => {
  S.follow = t; S.selected = "";
  $("follow-home").setAttribute("aria-checked", t === "home"); $("follow-away").setAttribute("aria-checked", t === "away");
  fillFavourites(); S.dirty = true; S.lastAnalysis = 0;
  showPlan(); renderSquad(); ASSESS.key = ""; ASSESS.last = 0;
};
for (const m of ["Casual", "Analyst"]) $(`mode-${m.toLowerCase()}`).onclick = () => {
  S.mode = m;
  $("mode-casual").setAttribute("aria-checked", m === "Casual"); $("mode-analyst").setAttribute("aria-checked", m === "Analyst");
  S.dirty = true;
};
$("favourite").onchange = (e) => { S.fav = e.target.value; S.selected = S.fav; S.dirty = true; S.lastAnalysis = 0; };
$("show-opp").onchange = (e) => { S.showOpp = e.target.checked; S.dirty = true; };
$("speed").onchange = (e) => { S.speed = +e.target.value; };
$("play").onclick = () => { S.playing = !S.playing; $("play").textContent = S.playing ? "⏸" : "▶"; };
$("source").onchange = (e) => (e.target.value === "live" ? startLive() : startReplay(e.target.value));

function frame(now) {
  const analysisOpen = !$("panel-analysis").hidden;
  if (S.dirty) {  // redraw only on new data
    renderLive();
    if (analysisOpen && now - S.lastAnalysis > 400) { renderAnalysis(); S.lastAnalysis = now; }
    else if (analysisOpen) S.pending = true;
    S.dirty = false;
  } else if (S.pending && now - S.lastAnalysis > 400) { renderAnalysis(); S.lastAnalysis = now; S.pending = false; }
  if (S.live) $("sb-clock").textContent = clock(currentT());  // keep the live clock ticking between events
  refreshAI(now);
  refreshAssess(now);
  requestAnimationFrame(frame);
}

// ---------- AI explanations (Microsoft Foundry via the live server; grounded, cached per match minute) ----------
const AI = { on: false, ctx: "", text: "", source: "", minute: 0, busy: false, last: 0, asked: "" };
const aiContext = () => [S.live ? "live" : S.meta?.seed, S.follow, S.mode, S.lang, S.fav].join("|");
async function refreshAI(now) {
  const minute = Math.floor(currentT() / 60);
  if (!AI.on || !S.meta || AI.busy || minute < 1 || now - AI.last < 1500) return;
  const ctx = aiContext(), ask = `${ctx}|${minute}`;
  if (ask === AI.asked) return;
  AI.busy = true; AI.last = now; AI.asked = ask;
  try {
    const q = new URLSearchParams({ seed: S.live ? "live" : S.meta.seed, minute, team: S.follow, mode: S.mode, lang: S.lang, fav: S.fav });
    const r = await fetch(`${LIVE}/explain?${q}`, { signal: AbortSignal.timeout(20000) });
    if (r.ok && aiContext() === ctx) Object.assign(AI, { ctx, ...(({ text, source, minute }) => ({ text, source, minute }))(await r.json()) });
    S.dirty = true;
  } catch { /* keep the template; try again next minute */ } finally { AI.busy = false; }
}
$("lang").onchange = (e) => { S.lang = e.target.value; S.dirty = true; };

// ---------- Ask the analyst (agents run server-side; text is inserted with textContent only) ----------
const askHistory = [];
const node = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };
function renderChips() {
  const team = S.meta?.teams[S.follow].name, fl = S.snap?.teams[S.follow].spof, sel = S.selected || S.fav || fl;
  if (!team) return;
  const chips = ["Is our game plan working?", "Who should I take off?", sel && `How is ${sel} doing against the plan?`,
    "Where should we attack, and where are we losing the ball?", fl && `What happens if ${fl} is marked out of the game?`,
    "Who is going to win from here?"].filter(Boolean);
  if ($("ask-chips").dataset.key === chips.join("|")) return;
  $("ask-chips").dataset.key = chips.join("|");
  $("ask-chips").replaceChildren(...chips.map((c) => { const b = node("button", "", c); b.type = "button"; b.onclick = () => { $("ask-q").value = c; askAnalyst(); }; return b; }));
}
async function askAnalyst() {
  const question = $("ask-q").value.trim();
  if (question.length < 3 || !S.meta) return;
  if (!AI.on) { $("ask-off").hidden = false; return; }
  const minute = Math.max(1, Math.floor(currentT() / 60));
  const entry = node("div", "qa");
  entry.append(node("div", "q", `${clock(minute * 60)} · ${question}`), node("div", "a", "Routing → analysing → verifying…"));
  $("ask-log").append(entry);
  $("ask-go").disabled = true; $("ask-q").value = "";
  try {
    const r = await fetch(`${LIVE}/ask`, { method: "POST", headers: { "content-type": "application/json" }, signal: AbortSignal.timeout(60000),
      body: JSON.stringify({ question, minute, seed: S.live ? "live" : S.meta.seed, lang: S.lang, history: askHistory.slice(-6),
        plan_team: S.follow, plan: PLAN[S.follow]?.custom ? PLAN[S.follow].plan : null }) });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
    const a = entry.querySelector(".a"); a.textContent = d.answer; a.dir = "auto";
    const total = d.trace.reduce((s, x) => s + (x.ms || 0), 0);
    entry.append(node("div", "src", `${d.source} · ${d.model_calls} model calls · ${(total / 1000).toFixed(1)} s`));
    const det = node("details"); det.append(node("summary", "", "How the agents answered"));
    const ol = node("ol");
    for (const s of d.trace) ol.append(node("li", "", `${s.agent}${s.tool ? ` → ${s.tool}(${Object.values(s.args || {}).join(", ")})` : ""}${s.detail ? `: ${s.detail}` : ""} · ${s.ms} ms`));
    det.append(ol); entry.append(det);
    askHistory.push({ q: question, a: d.answer });
  } catch (err) {
    entry.querySelector(".a").replaceWith(node("div", "a err", `Couldn't answer: ${err.message}`));
  } finally { $("ask-go").disabled = false; }
}
$("ask-form").onsubmit = (e) => { e.preventDefault(); askAnalyst(); };

// ---------- Game plan + squad assessment (server-side, deterministic; plan text is inserted with textContent only) ----------
const PLAN = { home: null, away: null };  // per team: { text, plan, source, custom }
const ASSESS = { key: "", busy: false, last: 0, data: null };
const SERVER = { on: false };
const saved = {
  get: (k) => { try { return JSON.parse(localStorage.getItem(k)); } catch { return null; } },
  set: (k, v) => { try { v ? localStorage.setItem(k, JSON.stringify(v)) : localStorage.removeItem(k); } catch { /* private mode */ } },
};
const label = (k) => k.replaceAll("_", " ");
async function loadPlan(team, demo = false) {
  const mine = !demo && saved.get(`fl-plan-${team}`);
  if (mine?.plan) PLAN[team] = mine;
  else {
    try {
      const d = await (await fetch(`${LIVE}/plan?team=${team}`, { signal: AbortSignal.timeout(8000) })).json();
      PLAN[team] = { text: d.text, plan: d.plan, source: d.source, custom: false };
    } catch { return; }
    if (demo) saved.set(`fl-plan-${team}`, null);
  }
  if (team === S.follow) { showPlan(); ASSESS.key = ""; }
}
function showPlan() {
  const p = PLAN[S.follow];
  $("plan-text").value = p?.text ?? "";
  $("plan-src").textContent = p ? `${p.custom ? "Your plan" : "Demo plan"} · read by ${p.source}` : "";
  const rows = [];
  for (const pr of p?.plan.principles ?? []) {
    const d = node("div"); d.append(node("b", "", "Team"), node("span", "", pr.text), ...Object.keys(pr.metrics).map((k) => node("span", "tag", label(k)))); rows.push(d);
  }
  for (const [name, du] of Object.entries(p?.plan.duties ?? {})) {
    const d = node("div"); d.append(node("b", "", `${name} · relevance ${du.relevance}/5`), node("span", "", du.duty), ...Object.keys(du.metrics).map((k) => node("span", "tag", label(k)))); rows.push(d);
  }
  $("plan-read").replaceChildren(...rows);
}
async function applyPlan() {
  const text = $("plan-text").value.trim(), team = S.follow;
  if (text.length < 10) { $("plan-src").textContent = "Write at least a sentence of instructions."; return; }
  $("plan-apply").disabled = true; $("plan-src").textContent = "Reading your plan…";
  try {
    const r = await fetch(`${LIVE}/plan`, { method: "POST", headers: { "content-type": "application/json" }, signal: AbortSignal.timeout(70000),
      body: JSON.stringify({ text, team }) });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
    PLAN[team] = { text, plan: d.plan, source: d.source, custom: true };
    saved.set(`fl-plan-${team}`, PLAN[team]);
    if (team === S.follow) { showPlan(); ASSESS.key = ""; ASSESS.last = 0; }
  } catch (err) { $("plan-src").textContent = `Couldn't read the plan: ${err.message}`; } finally { $("plan-apply").disabled = false; }
}
$("plan-apply").onclick = applyPlan;
$("plan-reset").onclick = () => loadPlan(S.follow, true);
$("plan-file").onchange = async (e) => { const f = e.target.files[0]; if (f) $("plan-text").value = (await f.text()).slice(0, 6000); e.target.value = ""; };

async function refreshAssess(now) {
  const p = PLAN[S.follow], minute = Math.floor(currentT() / 60);
  if (!SERVER.on || $("panel-analysis").hidden || !S.meta || !p || ASSESS.busy || minute < 5 || now - ASSESS.last < 3000) return;
  const key = [S.live ? "live" : S.meta.seed, S.follow, minute, p.custom, p.text].join("|");
  if (key === ASSESS.key) return;
  ASSESS.busy = true; ASSESS.last = now;
  try {
    const r = await fetch(`${LIVE}/assess`, { method: "POST", headers: { "content-type": "application/json" }, signal: AbortSignal.timeout(15000),
      body: JSON.stringify({ seed: S.live ? "live" : S.meta.seed, minute, plan_team: S.follow, plan: p.custom ? p.plan : null }) });
    if (r.ok) { ASSESS.data = await r.json(); ASSESS.key = key; renderSquad(); }
  } catch { /* try again in a few seconds */ } finally { ASSESS.busy = false; }
}
function renderSquad() {
  const d = ASSESS.data?.team === S.follow ? ASSESS.data : null;
  if (!d) { $("exec").replaceChildren(); $("squad").replaceChildren(); $("squad-detail").replaceChildren(); return; }
  const tone = (s) => (s === "working" ? "good" : s === "not working" ? "bad" : "warn");
  const overall = node("div"); overall.append(node("b", "", d.execution.overall_plan_execution ?? "–"), node("span", "", `overall plan execution at ${d.minute}'`));
  $("exec").replaceChildren(overall, ...d.execution.principles.map((p) => {
    const x = node("div"); x.append(node("b", "", p.score), node("span", `tag ${tone(p.status)}`, p.status), node("div", "muted", p.principle)); return x;
  }));
  const sel = d.players.find((r) => r.player === S.selected) ?? d.players[0];
  const head = node("tr"); for (const h of ["Player", "Relevance", "Plan fit", "Last 15", "Form", "Load vs plan", "Flags"]) head.append(node("th", "", h));
  const thead = node("thead"); thead.append(head);
  const tbody = node("tbody");
  const fitTone = (v) => (v >= 60 ? "var(--good)" : v < 40 ? "var(--bad)" : "var(--accent)");
  const flagTag = (f) => { const short = f.split(":")[0]; return node("span", `tag ${short === "on plan" ? "good" : short === "off plan" || short === "fading" ? "bad" : "warn"}`, short); };
  for (const r of d.players) {
    const tr = node("tr", r === sel ? "sel" : "");
    const fit = node("div", "fit"), bar = node("i"); bar.style.width = `${r.plan_fit}%`; bar.style.background = fitTone(r.plan_fit); fit.append(bar, node("span", "", r.plan_fit));
    const flags = node("td"); flags.append(...r.flags.map(flagTag));
    const cells = [node("td", "", `${r.player} · ${r.role}`), node("td", "", r.plan_relevance), node("td"), node("td", "", r.plan_fit_last_15),
      node("td", "", r.form), node("td", "", `${r.load_vs_plan} (${r.load_share} vs ${r.plan_expected_share})`), flags];
    cells[2].append(fit); tr.append(...cells);
    tr.onclick = () => { S.selected = r.player; S.dirty = true; S.lastAnalysis = 0; renderSquad(); };
    tbody.append(tr);
  }
  $("squad").replaceChildren(thead, tbody);
  const det = $("squad-detail"); det.replaceChildren(node("b", "", `${sel.player} (${sel.role}): `), node("span", "", sel.duty));
  const list = (title, items) => { if (!items.length) return; det.append(node("div", "muted small", title)); const ul = node("ul"); ul.append(...items.map((t) => node("li", "", t))); det.append(ul); };
  list("Strengths", sel.strengths); list("Concerns", sel.concerns);
  list("Judged on (weights from role + plan)", [Object.entries(sel.weighted_metrics).map(([k, w]) => `${label(k)} ×${w}`).join(" · ")]);
  for (const f of sel.flags.filter((f) => f.includes(":"))) det.append(node("div", "small", `⚠ ${f}`));
}

async function liveAvailable() {
  try {
    const r = await fetch(`${LIVE}/health`, { signal: AbortSignal.timeout(2500) });
    const h = r.ok ? await r.json() : null;
    SERVER.on = Boolean(h?.ok);
    AI.on = Boolean(h?.ai);
    $("lang-wrap").hidden = !AI.on;
    return Boolean(h?.ok);
  } catch { return false; }
}

(async function init() {
  const [index, live] = await Promise.all([fetch("data/index.json").then((r) => r.json()), liveAvailable()]);
  $("squad-off").hidden = live;
  $("plan-apply").disabled = !live;
  if (live) { loadPlan("home"); loadPlan("away"); }
  $("source").innerHTML = (live ? `<option value="live">Live feed</option>` : "") +
    index.map((m) => `<option value="${m.file}">Replay: match ${m.seed} (${m.home} ${m.final} ${m.away})</option>`).join("");
  const want = params.get("match");
  const pick = want ? index.find((m) => String(m.seed) === want)?.file : null;
  if (live && !pick) startLive();
  else { $("source").value = pick ?? index[0].file; startReplay($("source").value); }
  requestAnimationFrame(frame);
})();
