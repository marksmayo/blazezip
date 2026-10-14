"""Render benchmark results as a single self-contained HTML report (no external assets).

Medals are computed in the page from the raw results, so the medal table can be filtered by
category and metric without regenerating anything. A Python copy of the medal logic is used for
the console summary.
"""

import html
import json
from pathlib import Path

METRICS = {
    "wall_s": "Speed",
    "cpu_s": "CPU time",
    "peak_mem": "Memory",
    "out_size": "Size",
}
CATEGORY_METRICS = {
    "zip-create": ["wall_s", "cpu_s", "peak_mem", "out_size"],
    "zip-extract": ["wall_s", "cpu_s", "peak_mem"],
    "zip-roundtrip": ["wall_s", "cpu_s", "peak_mem"],
    "native-compress": ["wall_s", "cpu_s", "peak_mem", "out_size"],
    "native-decompress": ["wall_s", "cpu_s", "peak_mem"],
}


def medals(results, category=None, metric=None):
    """{tool: [gold, silver, bronze]} over every benchmark x metric."""
    groups = {}
    for r in results:
        if r.get("ok"):
            groups.setdefault((r["category"], r["corpus"], r["preset"]), []).append(r)
    table = {}
    for (cat, _, _), rows in groups.items():
        if category and cat != category:
            continue
        for m in CATEGORY_METRICS[cat]:
            if metric and m != metric:
                continue
            ranked = sorted((r for r in rows if r.get(m) is not None), key=lambda r: r[m])
            if len(ranked) < 2:
                continue
            for place, r in enumerate(ranked[:3]):
                table.setdefault(r["tool"], [0, 0, 0])[place] += 1
    return table


def write(data: dict, out: Path):
    tpl = TEMPLATE.replace("/*__DATA__*/null", json.dumps(data).replace("</", "<\\/"))
    out.write_text(tpl, encoding="utf-8")
    # Variant for publishing as a hosted page: the host supplies the document skeleton.
    body = tpl.split("<head>", 1)[1]
    for tag in ("</head>", "<body>", "</body>", "</html>",
                '<meta charset="utf-8">', '<meta name="viewport" content="width=device-width, initial-scale=1">'):
        body = body.replace(tag, "")
    out.with_name("report-artifact.html").write_text(body.strip() + "\n", encoding="utf-8")
    table = medals(data["results"])
    names = {t["key"]: t["name"] for t in data["tools"]}
    print("\nMedal table (all benchmarks, all metrics):")
    for k, (g, s, b) in sorted(table.items(), key=lambda kv: (-kv[1][0], -kv[1][1], -kv[1][2])):
        print(f"  {names.get(k, k):<24} gold {g:3d}  silver {s:3d}  bronze {b:3d}")


TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>blazezip Benchmark Report</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wght@500;700;800&family=JetBrains+Mono:wght@400;600&display=swap">
<style>
/* Layout: podium hero -> filterable medal table -> grid of per-benchmark bar cards -> method notes */
:root {
  --font-display: "Archivo", system-ui, "Segoe UI", sans-serif;
  --font-body: system-ui, -apple-system, "Segoe UI", sans-serif;
  --font-data: "JetBrains Mono", ui-monospace, Consolas, monospace;
  color-scheme: light;
  --page: #f6f5f2;
  --surface: #fcfcfb;
  --surface-2: #f0efec;
  --ink: #0b0b0b;
  --ink-2: #52514e;
  --muted: #7a7873;
  --grid: #e1e0d9;
  --border: rgba(11,11,11,0.10);
  --bar: #9ec5f4;
  --bar-hi: #eb6834;
  --accent: #eb6834;
  --gold: #d4a017; --gold-ink: #3a2c00;
  --silver: #b4b7bd; --silver-ink: #23262b;
  --bronze: #c27a45; --bronze-ink: #2e1606;
  --bad: #d03b3b;
  --shadow: 0 1px 2px rgba(0,0,0,.04), 0 8px 24px rgba(0,0,0,.05);
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --page: #0d0d0d; --surface: #1a1a19; --surface-2: #232321;
    --ink: #ffffff; --ink-2: #c3c2b7; --muted: #94928b;
    --grid: #2c2c2a; --border: rgba(255,255,255,0.10);
    --bar: #3d6fae; --bar-hi: #f07a45; --accent: #f07a45;
    --shadow: none;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --page: #0d0d0d; --surface: #1a1a19; --surface-2: #232321;
  --ink: #ffffff; --ink-2: #c3c2b7; --muted: #94928b;
  --grid: #2c2c2a; --border: rgba(255,255,255,0.10);
  --bar: #3d6fae; --bar-hi: #f07a45; --accent: #f07a45;
  --shadow: none;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--page); color: var(--ink);
  font: 15px/1.5 var(--font-body);
}
h1, h2, h3, .step .who, .block, .big-medal { font-family: var(--font-display); text-wrap: balance; }
.val, .medal-table td.n, #tip .kv b, .chip, .tally { font-family: var(--font-data); font-size: 12px; }
.wrap { margin: 0 auto; padding: 32px clamp(16px, 2.5vw, 48px) 80px; }
header { display: flex; justify-content: space-between; gap: 24px; align-items: flex-start; flex-wrap: wrap; }
h1 { font-size: 34px; line-height: 1.1; margin: 0 0 6px; letter-spacing: -0.02em; }
h1 .flame { color: var(--accent); }
h2 { font-size: 22px; margin: 48px 0 6px; letter-spacing: -0.01em; }
h3 { font-size: 16px; margin: 0; }
.sub { color: var(--ink-2); margin: 0; }
.chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 14px; }
.chip { background: var(--surface); border: 1px solid var(--border); border-radius: 999px; padding: 3px 10px; font-size: 12.5px; color: var(--ink-2); }
.theme-btn { background: var(--surface); color: var(--ink); border: 1px solid var(--border); border-radius: 8px; padding: 6px 12px; cursor: pointer; font: inherit; font-size: 13px; }
.card { background: var(--surface); border: 1px solid var(--border); border-radius: 14px; box-shadow: var(--shadow); }
.section-note { color: var(--ink-2); margin: 0 0 16px; max-width: 760px; }

/* podium */
.podium-card { margin-top: 28px; padding: 28px 20px 14px; overflow: hidden; }
.podium-head { display: flex; justify-content: space-between; align-items: baseline; flex-wrap: wrap; gap: 8px; padding: 0 8px; }
/* Every product on one staircase, best on the left; step heights follow medal points. */
.podium { display: grid; grid-template-columns: repeat(var(--n, 14), minmax(0, 1fr)); align-items: end; gap: 10px; margin-top: 18px; }
@media (max-width: 1000px) { .podium { grid-template-columns: repeat(auto-fill, minmax(96px, 1fr)); row-gap: 18px; } }
.step { text-align: center; min-width: 0; display: flex; flex-direction: column; align-items: center; }
.step .who { font-weight: 700; font-size: clamp(12.5px, 1.05vw, 18px); margin: 8px 0 2px; letter-spacing: -0.01em; line-height: 1.2; min-height: 2.4em; display: flex; align-items: flex-end; justify-content: center; }
.step.p1 .who { font-size: clamp(14px, 1.3vw, 22px); }
.step .tally { color: var(--ink-2); font-size: 12px; margin-bottom: 8px; font-variant-numeric: tabular-nums; white-space: nowrap; }
.step .tally i { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin: 0 3px 0 5px; }
.step .tally i.g { background: var(--gold); } .step .tally i.s { background: var(--silver); } .step .tally i.b { background: var(--bronze); }
.block { width: 100%; border-radius: 10px 10px 0 0; display: flex; align-items: flex-start; justify-content: center; padding-top: 10px; font-size: clamp(15px, 1.6vw, 32px); font-weight: 800; background: var(--surface-2); color: var(--ink-2); }
.p1 .block { background: linear-gradient(180deg, var(--gold), color-mix(in srgb, var(--gold) 70%, var(--surface))); color: var(--gold-ink); }
.p2 .block { background: linear-gradient(180deg, var(--silver), color-mix(in srgb, var(--silver) 70%, var(--surface))); color: var(--silver-ink); }
.p3 .block { background: linear-gradient(180deg, var(--bronze), color-mix(in srgb, var(--bronze) 70%, var(--surface))); color: var(--bronze-ink); }
.step .events { color: var(--ink-2); font-size: 12.5px; margin-top: 6px; font-variant-numeric: tabular-nums; white-space: nowrap; }
.big-medal.mx { background: var(--surface-2); color: var(--ink-2); width: 40px; height: 40px; font-size: 16px; }
.big-medal { display: inline-grid; place-items: center; width: 54px; height: 54px; border-radius: 50%; font-weight: 800; font-size: 22px; box-shadow: inset 0 -3px 0 rgba(0,0,0,.18), 0 0 0 4px var(--surface); }

/* medal chips */
.m { display: inline-grid; place-items: center; width: 22px; height: 22px; border-radius: 50%; font-size: 11.5px; font-weight: 800; flex: none; box-shadow: inset 0 -2px 0 rgba(0,0,0,.18); }
.m1 { background: var(--gold); color: var(--gold-ink); }
.m2 { background: var(--silver); color: var(--silver-ink); }
.m3 { background: var(--bronze); color: var(--bronze-ink); }

/* filters */
.filters { display: flex; flex-wrap: wrap; gap: 10px 18px; margin: 14px 0; align-items: center; }
.seg { display: inline-flex; background: var(--surface-2); border-radius: 9px; padding: 3px; gap: 2px; flex-wrap: wrap; }
.seg button { border: 0; background: transparent; color: var(--ink-2); padding: 5px 11px; border-radius: 7px; font: inherit; font-size: 13px; cursor: pointer; }
.seg button[aria-pressed="true"] { background: var(--surface); color: var(--ink); box-shadow: 0 0 0 1px var(--border); font-weight: 600; }
.flabel { font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: .06em; }

/* medal table */
table { border-collapse: collapse; width: 100%; }
.medal-table th, .medal-table td { padding: 10px 14px; text-align: left; border-bottom: 1px solid var(--grid); }
.medal-table th { font-size: 12px; color: var(--muted); font-weight: 600; text-transform: uppercase; letter-spacing: .05em; }
.medal-table td.n, .medal-table th.n { text-align: right; font-variant-numeric: tabular-nums; }
.medal-table tr:last-child td { border-bottom: 0; }
.medal-table tr.hi td { background: color-mix(in srgb, var(--accent) 8%, transparent); }
.tool-cell { display: flex; flex-direction: column; }
.tool-cell small { color: var(--muted); font-size: 12px; }
.rank { color: var(--muted); font-variant-numeric: tabular-nums; width: 40px; }
.stack { display: flex; height: 8px; border-radius: 4px; overflow: hidden; gap: 2px; min-width: 60px; background: var(--surface-2); }
.stack span { display: block; height: 100%; }
.table-scroll { overflow-x: auto; }

/* benchmark cards */
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); gap: 14px; }
.bench { padding: 16px 16px 12px; }
.bench-head { display: flex; justify-content: space-between; gap: 8px; align-items: baseline; margin-bottom: 10px; }
.bench-head .meta { font-size: 12px; color: var(--muted); white-space: nowrap; }
.row { display: grid; grid-template-columns: 22px minmax(90px, 132px) 1fr; align-items: center; gap: 8px; padding: 3px 0; position: relative; }
.row .name { font-size: 13px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; color: var(--ink-2); }
.row.me .name { color: var(--ink); font-weight: 700; }
.track { position: relative; height: 18px; display: flex; align-items: center; }
.fill { height: 14px; border-radius: 0 4px 4px 0; background: var(--bar); min-width: 2px; }
.row.me .fill { background: var(--bar-hi); }
.val { font-size: 12px; color: var(--ink-2); margin-left: 6px; white-space: nowrap; font-variant-numeric: tabular-nums; }
.row:hover .name { color: var(--ink); }
.row .slot { width: 22px; height: 22px; }
.fail { font-size: 12px; color: var(--muted); margin-top: 8px; }
.fail b { color: var(--bad); font-weight: 600; }
.cat-title { display: flex; align-items: baseline; gap: 10px; margin: 34px 0 10px; }
.cat-title h3 { font-size: 18px; }
.cat-title span { color: var(--muted); font-size: 13px; }

/* tooltip */
#tip { position: fixed; pointer-events: none; z-index: 10; background: var(--surface); color: var(--ink); border: 1px solid var(--border); border-radius: 10px; padding: 10px 12px; font-size: 12.5px; box-shadow: 0 8px 30px rgba(0,0,0,.18); opacity: 0; transition: opacity .08s; min-width: 200px; }
#tip .t { font-weight: 700; margin-bottom: 4px; }
#tip .kv { display: flex; justify-content: space-between; gap: 18px; color: var(--ink-2); font-variant-numeric: tabular-nums; }
#tip .kv b { color: var(--ink); font-weight: 600; }

/* info */
.two { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
.info { padding: 18px 20px; }
.info h3 { margin-bottom: 8px; }
.info ul { margin: 0; padding-left: 18px; color: var(--ink-2); }
.info li { margin: 4px 0; }
.tools-table td, .tools-table th { padding: 7px 10px; border-bottom: 1px solid var(--grid); font-size: 13px; text-align: left; vertical-align: top; }
.tools-table th { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .05em; }
.na { color: var(--muted); }
.ok-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: #0ca30c; margin-right: 6px; }
.no-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: var(--muted); margin-right: 6px; }
footer { margin-top: 48px; color: var(--muted); font-size: 12.5px; }

@media (max-width: 720px) {
  h1 { font-size: 27px; }
  .podium { gap: 8px; }
  .two { grid-template-columns: 1fr; }
  .grid { grid-template-columns: 1fr; }
  .hide-sm { display: none; }
  .medal-table th, .medal-table td { padding: 8px 8px; }
}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div>
      <h1><span class="flame">blaze</span>zip benchmark</h1>
      <p class="sub" id="subtitle"></p>
      <p class="sub" id="basis" style="font-size:13px"></p>
      <div class="chips" id="chips"></div>
    </div>
    <button class="theme-btn" id="themeBtn" type="button">Toggle theme</button>
  </header>

  <section class="card podium-card" aria-labelledby="podTitle">
    <div class="podium-head">
      <h3 id="podTitle">Overall podium</h3>
      <span class="sub" style="font-size:13px" id="podNote"></span>
    </div>
    <div class="podium" id="podium"></div>
  </section>

  <h2>Medal table</h2>
  <p class="section-note">Every benchmark awards gold, silver and bronze for each metric it measures: <b>speed</b> (wall-clock time), <b>CPU time</b> (total core-seconds, a proxy for energy), <b>memory</b> (peak committed memory of the whole process tree) and, for compression, <b>size</b> (smallest output). Ranking is by golds, then silvers, then bronzes.</p>
  <div class="filters">
    <span class="flabel">Category</span><div class="seg" id="fCat"></div>
    <span class="flabel">Metric</span><div class="seg" id="fMetric"></div>
  </div>
  <div class="card table-scroll"><table class="medal-table" id="medalTable"></table></div>

  <h2>Benchmarks</h2>
  <p class="section-note">Each card is one benchmark. Bars show the selected metric (shorter is better); blazezip is highlighted. Hover a row for every measurement. Failed or unverified runs earn nothing and are listed under the card.</p>
  <div class="filters">
    <span class="flabel">Show</span><div class="seg" id="fBarMetric"></div>
    <span class="flabel">Corpus</span><div class="seg" id="fCorpus"></div>
  </div>
  <div id="benches"></div>

  <h2>Contenders &amp; method</h2>
  <div class="card table-scroll" style="margin-bottom:14px"><table class="tools-table" id="toolsTable"></table></div>
  <div class="two">
    <div class="card info"><h3>Corpora</h3><ul id="corpusList"></ul></div>
    <div class="card info"><h3>How it was measured</h3><ul>
      <li>Each command runs inside a Windows <b>Job Object</b>, so CPU time and peak memory include every helper process a tool launches.</li>
      <li>Every tool runs with its multithreading switch on (if it has one). Presets map native levels to fast / default / max.</li>
      <li>Each result is the median of <span id="repsN"></span> runs of that test; hover a result to see its run count and its fastest and slowest run.</li>
      <li><b>Verified outputs only:</b> created ZIPs must pass <code>7z t</code>; every extraction is hash-checked file-by-file against the corpus; native decompression must reproduce the input byte-for-byte.</li>
      <li><b>ZIP extract</b> uses one neutral reference archive (7-Zip, default level) for everyone. <b>Round-trip</b> extracts each tool's own default archive.</li>
      <li><b>Native</b> benchmarks compress one file (a .tar of the corpus, or enwik8) in each tool's own best format, which lets stream compressors like zstd, pigz, LZ4 and LZFSE compete.</li>
      <li>Windows Defender real-time protection stays on, as on a typical PC. It scans every newly written file, which costs every tool roughly the same fixed time per file and dominates extraction of many-file archives such as the Linux source.</li>
    </ul></div>
  </div>
  <footer id="footer"></footer>
</div>
<div id="tip" role="tooltip"></div>

<script>
const DATA = /*__DATA__*/null;
const METRICS = {wall_s: "Speed", cpu_s: "CPU time", peak_mem: "Memory", out_size: "Size"};
const CAT_METRICS = {
  "zip-create": ["wall_s","cpu_s","peak_mem","out_size"],
  "zip-extract": ["wall_s","cpu_s","peak_mem"],
  "zip-roundtrip": ["wall_s","cpu_s","peak_mem"],
  "native-compress": ["wall_s","cpu_s","peak_mem","out_size"],
  "native-decompress": ["wall_s","cpu_s","peak_mem"],
};
const CAT_LABEL = {
  "zip-create": "ZIP create", "zip-extract": "ZIP extract", "zip-roundtrip": "ZIP round-trip",
  "native-compress": "Native compress", "native-decompress": "Native decompress",
};
const CAT_DESC = {
  "zip-create": "Folder → .zip (Deflate), at each tool's fast / default / max preset",
  "zip-extract": "Extract the same standard reference .zip",
  "zip-roundtrip": "Extract the archive each tool made itself (default preset)",
  "native-compress": "One large file → each tool's own best format",
  "native-decompress": "Decompress each tool's own default-preset output",
};
const tools = Object.fromEntries(DATA.tools.map(t => [t.key, t]));
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const fmtBytes = b => { if (b == null) return "–"; const u=["B","KB","MB","GB"]; let i=0; while (b>=1024 && i<u.length-1) {b/=1024;i++;} return (b>=100||i===0?b.toFixed(0):b.toFixed(1))+" "+u[i]; };
const fmtTime = s => s == null ? "–" : s < 1 ? (s*1000).toFixed(0)+" ms" : s < 100 ? s.toFixed(2)+" s" : s.toFixed(0)+" s";
const fmt = (m, v) => m === "peak_mem" || m === "out_size" ? fmtBytes(v) : fmtTime(v);

// ---------- medals ----------
function groups() {
  const g = new Map();
  for (const r of DATA.results) {
    if (!r.ok) continue;
    const k = r.category + "|" + r.corpus + "|" + r.preset;
    if (!g.has(k)) g.set(k, []);
    g.get(k).push(r);
  }
  return g;
}
const GROUPS = groups();
function rankOf(rows, m) {
  // Standard competition ranking; exact ties share a place.
  const s = rows.filter(r => r[m] != null).sort((a, b) => a[m] - b[m]);
  const out = new Map(); let place = 0;
  s.forEach((r, i) => { if (i === 0 || r[m] !== s[i-1][m]) place = i; out.set(r.tool, place); });
  return {out, n: s.length};
}
function medalTable(cat, metric) {
  const t = {};
  for (const [k, rows] of GROUPS) {
    const c = k.split("|")[0];
    if (cat !== "all" && c !== cat) continue;
    for (const m of CAT_METRICS[c]) {
      if (metric !== "all" && m !== metric) continue;
      const {out, n} = rankOf(rows, m);
      if (n < 2) continue;
      for (const [tool, p] of out) if (p < 3) (t[tool] ??= [0,0,0])[p]++;
    }
  }
  for (const r of DATA.results) if (r.ok && !(r.tool in t) && (cat === "all" || r.category === cat)) t[r.tool] = [0,0,0];
  return Object.entries(t).map(([tool, [g,s,b]]) => ({tool, g, s, b, total: g+s+b, pts: 3*g+2*s+b}))
    .sort((a, b) => b.g - a.g || b.s - a.s || b.b - a.b || b.pts - a.pts);
}

// ---------- header ----------
const M = DATA.meta || {};
document.getElementById("subtitle").textContent =
  (() => { const n = new Set(DATA.results.map(r=>r.corpus)).size;
    const when = M.started ? new Date(M.started).toLocaleString(undefined, {day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit"}) : "";
    return `${DATA.results.filter(r=>r.ok).length} verified runs across ${n} ${n === 1 ? "corpus" : "corpora"} · ${when}`; })();
if (M.leaderboard) document.getElementById("basis").textContent =
  "Every competitor is shown at its best-ever result for each test and metric; blazezip is shown at its latest run.";
document.getElementById("chips").innerHTML = [
  M.cpu, M.cores && `${M.cores} cores / ${M.threads} threads`, M.ram_gb && `${M.ram_gb} GB RAM`, M.os, M.disk,
  M.quick ? "quick mode" : null,
].filter(Boolean).map(c => `<span class="chip">${esc(c)}</span>`).join("");
(() => { const r = DATA.results.filter(x => x.ok).map(x => x.reps || 1);
  const lo = Math.min(...r), hi = Math.max(...r);
  document.getElementById("repsN").textContent = !r.length ? "10" : lo === hi ? String(lo) : `${lo} to ${hi}`; })();
document.getElementById("footer").textContent = `Generated by bench/bench.py · started ${M.started || "?"} · finished ${M.finished || "(in progress)"}`;

// ---------- podium ----------
function medalChip(p) { return `<span class="m m${p+1}" aria-label="${["gold","silver","bronze"][p]}">${p+1}</span>`; }
(function podium() {
  const all = medalTable("all", "all");
  let benches = 0, scored = 0;
  for (const [k, rows] of GROUPS) {
    const ms = CAT_METRICS[k.split("|")[0]].filter(m => rows.filter(r => r[m] != null).length >= 2);
    if (ms.length) { benches++; scored += ms.length; }
  }
  document.getElementById("podNote").textContent =
    `All categories · ${new Set(DATA.results.filter(r => r.ok).map(r => r.tool)).size} products tested · ${scored} metrics scored across ${benches} benchmarks`;
  // Events a product competed in: each benchmark x metric where it has a result and a rival does too.
  const events = {};
  for (const [k, rows] of GROUPS)
    for (const m of CAT_METRICS[k.split("|")[0]]) {
      const have = rows.filter(r => r[m] != null);
      if (have.length >= 2) for (const r of have) events[r.tool] = (events[r.tool] || 0) + 1;
    }
  // Best on the left, each step lower than the one before it (ranking is by golds, then silvers,
  // then bronzes, so heights follow the placing rather than a points total).
  const ord = n => n + (n % 100 >= 11 && n % 100 <= 13 ? "th" : ({1: "st", 2: "nd", 3: "rd"}[n % 10] || "th"));
  const pod = document.getElementById("podium");
  pod.style.setProperty("--n", all.length);
  pod.innerHTML = all.map((e, i) => {
    const name = esc(tools[e.tool]?.name || e.tool);
    return `<div class="step${i < 3 ? " p" + (i + 1) : ""}">
      <div class="big-medal ${i < 3 ? "m" + (i + 1) : "mx"}">${i + 1}</div>
      <div class="who" title="${name}">${name}</div>
      <div class="tally" aria-label="${e.g} gold, ${e.s} silver, ${e.b} bronze"><i class="g"></i>${e.g}<i class="s"></i>${e.s}<i class="b"></i>${e.b}</div>
      <div class="block" style="height:calc(1.25em + 20px + ${Math.round(170 * (all.length - 1 - i) / Math.max(1, all.length - 1))}px)">${ord(i + 1)}</div>
      <div class="events">${events[e.tool] || 0} events</div></div>`;
  }).join("");
})();

// ---------- segmented controls ----------
function seg(id, options, initial, onChange) {
  const el = document.getElementById(id); let cur = initial;
  el.innerHTML = options.map(([v, l]) => `<button type="button" data-v="${esc(v)}" aria-pressed="${v===cur}">${esc(l)}</button>`).join("");
  el.addEventListener("click", e => {
    const b = e.target.closest("button"); if (!b) return;
    cur = b.dataset.v;
    el.querySelectorAll("button").forEach(x => x.setAttribute("aria-pressed", x.dataset.v === cur));
    onChange(cur);
  });
  return () => cur;
}
const cats = [...new Set(DATA.results.map(r => r.category))].sort((a,b)=>Object.keys(CAT_LABEL).indexOf(a)-Object.keys(CAT_LABEL).indexOf(b));
const state = {cat: "all", metric: "all", bar: "wall_s", corpus: "all"};
seg("fCat", [["all","All"], ...cats.map(c => [c, CAT_LABEL[c]])], "all", v => { state.cat = v; renderMedals(); });
seg("fMetric", [["all","All"], ...Object.entries(METRICS)], "all", v => { state.metric = v; renderMedals(); });
seg("fBarMetric", Object.entries(METRICS), "wall_s", v => { state.bar = v; renderBenches(); });
const corpora = Object.keys(DATA.corpora || {});
seg("fCorpus", [["all","All"], ...corpora.map(c => [c, c])], "all", v => { state.corpus = v; renderBenches(); });

function renderMedals() {
  const rows = medalTable(state.cat, state.metric);
  const max = Math.max(1, ...rows.map(r => r.total));
  document.getElementById("medalTable").innerHTML = `
    <thead><tr><th class="n">#</th><th>Tool</th><th class="n" title="Gold">${medalChip(0)}</th><th class="n" title="Silver">${medalChip(1)}</th><th class="n" title="Bronze">${medalChip(2)}</th><th class="n">Total</th><th class="hide-sm">Share</th></tr></thead>
    <tbody>${rows.map((r, i) => `<tr class="${r.tool === "blazezip" ? "hi" : ""}">
      <td class="n rank">${i+1}</td>
      <td><div class="tool-cell"><b>${esc(tools[r.tool]?.name || r.tool)}</b><small>${esc(tools[r.tool]?.version || "")}</small></div></td>
      <td class="n"><b>${r.g}</b></td><td class="n">${r.s}</td><td class="n">${r.b}</td><td class="n">${r.total}</td>
      <td class="hide-sm" style="width:30%"><div class="stack" style="width:${Math.max(4, r.total/max*100)}%" title="${r.g} gold, ${r.s} silver, ${r.b} bronze">
        ${r.g ? `<span style="flex:${r.g};background:var(--gold)"></span>` : ""}${r.s ? `<span style="flex:${r.s};background:var(--silver)"></span>` : ""}${r.b ? `<span style="flex:${r.b};background:var(--bronze)"></span>` : ""}
      </div></td></tr>`).join("")}</tbody>`;
}

// ---------- benchmark cards ----------
const tip = document.getElementById("tip");
function showTip(ev, r) {
  const rows = [
    ["Wall time (median)", fmtTime(r.wall_s)],
    ...(r.min_wall != null && r.reps > 1 ? [["Wall time range", fmtTime(r.min_wall) + " – " + fmtTime(r.max_wall)]] : []),
    ["CPU time", fmtTime(r.cpu_s)],
    ["Avg cores busy", r.wall_s ? (r.cpu_s / r.wall_s).toFixed(1) + "×" : "–"],
    ["Peak memory", fmtBytes(r.peak_mem)], ["Peak working set", fmtBytes(r.peak_ws)],
  ];
  if (r.out_size != null) rows.push(["Output", fmtBytes(r.out_size)], ["Ratio", (r.ratio*100).toFixed(1) + "%"]);
  if (r.in_size && r.wall_s) rows.push(["Throughput", fmtBytes(r.in_size / r.wall_s) + "/s"]);
  if (r.format) rows.push(["Format", r.format]);
  rows.push(["Repetitions", r.reps], ["Processes", r.procs]);
  if (r.version) rows.push(["Version", r.version]);
  if (r.tool === "blazezip") rows.push(["Source", "latest run" + (r.run ? " (" + r.run + ")" : "")]);
  else if (r.best_of) rows.push(["Source", r.best_of > 1 ? `best of ${r.best_of} runs` : "1 run"]);
  tip.innerHTML = `<div class="t">${esc(tools[r.tool]?.name || r.tool)}</div>` + rows.map(([k,v]) => `<div class="kv"><span>${k}</span><b>${esc(v)}</b></div>`).join("");
  moveTip(ev); tip.style.opacity = 1;
}
function moveTip(ev) {
  const w = tip.offsetWidth, h = tip.offsetHeight;
  let x = ev.clientX + 14, y = ev.clientY + 14;
  if (x + w > innerWidth - 8) x = ev.clientX - w - 14;
  if (y + h > innerHeight - 8) y = ev.clientY - h - 14;
  tip.style.left = x + "px"; tip.style.top = y + "px";
}
function renderBenches() {
  const byCat = {};
  for (const [k, rows] of GROUPS) {
    const [cat, corpus, preset] = k.split("|");
    if (state.corpus !== "all" && corpus !== state.corpus) continue;
    (byCat[cat] ??= []).push({cat, corpus, preset, rows});
  }
  const presetOrder = {fast: 0, default: 1, max: 2};
  let out = "";
  for (const cat of cats) {
    const list = (byCat[cat] || []).sort((a, b) => corpora.indexOf(a.corpus) - corpora.indexOf(b.corpus) || presetOrder[a.preset] - presetOrder[b.preset]);
    if (!list.length) continue;
    out += `<div class="cat-title"><h3>${CAT_LABEL[cat]}</h3><span>${CAT_DESC[cat]}</span></div><div class="grid">`;
    for (const b of list) {
      let m = state.bar;
      const metricOk = CAT_METRICS[cat].includes(m);
      if (!metricOk) m = "wall_s";
      const {out: ranks} = rankOf(b.rows, m);
      const sorted = b.rows.filter(r => r[m] != null).sort((x, y) => x[m] - y[m]);
      const max = Math.max(...sorted.map(r => r[m]));
      const fails = DATA.results.filter(r => !r.ok && r.category === cat && r.corpus === b.corpus && r.preset === b.preset);
      const ci = DATA.corpora[b.corpus] || {};
      out += `<div class="card bench"><div class="bench-head"><h3>${esc(b.corpus)}${cat.includes("create") || cat === "native-compress" ? ` · ${esc(b.preset)}` : ""}</h3>
        <span class="meta">${metricOk ? METRICS[m] : "Speed (no size for this test)"} · ${ci.files ?? "?"} files · ${fmtBytes(ci.bytes)}</span></div>`;
      for (const r of sorted) {
        const p = ranks.get(r.tool);
        const idx = DATA.results.indexOf(r);
        out += `<div class="row ${r.tool === "blazezip" ? "me" : ""}" data-i="${idx}">
          ${p < 3 ? medalChip(p) : `<span class="slot"></span>`}
          <span class="name">${esc(tools[r.tool]?.name || r.tool)}</span>
          <div class="track"><div class="fill" style="width:${Math.max(0.5, r[m] / max * 78)}%"></div><span class="val">${fmt(m, r[m])}${m === "out_size" ? ` · ${(r.ratio*100).toFixed(1)}%` : ""}</span></div></div>`;
      }
      if (fails.length) out += `<div class="fail">${fails.map(f => `<b>✕</b> ${esc(tools[f.tool]?.name || f.tool)}: ${esc(f.error)}`).join("<br>")}</div>`;
      out += `</div>`;
    }
    out += `</div>`;
  }
  const el = document.getElementById("benches");
  el.innerHTML = out || `<p class="section-note">No results yet.</p>`;
}
document.getElementById("benches").addEventListener("mouseover", e => {
  const row = e.target.closest(".row"); if (!row) return;
  showTip(e, DATA.results[+row.dataset.i]);
});
document.getElementById("benches").addEventListener("mousemove", e => { if (e.target.closest(".row")) moveTip(e); });
document.getElementById("benches").addEventListener("mouseout", e => { if (!e.relatedTarget?.closest?.(".row")) tip.style.opacity = 0; });

// ---------- contenders ----------
(function contenders() {
  const counts = {};
  for (const r of DATA.results) if (r.ok) counts[r.tool] = (counts[r.tool] || 0) + 1;
  document.getElementById("toolsTable").innerHTML = `<thead><tr><th>Tool</th><th>Version</th><th>ZIP</th><th>Native format</th><th class="hide-sm">Notes</th><th>Runs</th></tr></thead><tbody>` +
    DATA.tools.map(t => `<tr><td>${t.available ? '<span class="ok-dot"></span>' : '<span class="no-dot"></span>'}<b>${esc(t.name)}</b></td>
      <td>${esc(t.version) || '<span class="na">–</span>'}</td><td>${t.zip ? "yes" : '<span class="na">–</span>'}</td>
      <td>${esc(t.native_format) || '<span class="na">–</span>'}</td><td class="hide-sm">${esc(t.available ? t.note : (t.note || "not installed"))}</td>
      <td>${counts[t.key] || 0}</td></tr>`).join("") + `</tbody>`;
  document.getElementById("corpusList").innerHTML = Object.entries(DATA.corpora || {}).map(([k, c]) =>
    `<li><b>${esc(k)}</b>: ${esc(c.desc)} <span class="na">(${c.files} files, ${fmtBytes(c.bytes)})</span></li>`).join("");
})();

// ---------- theme ----------
document.getElementById("themeBtn").addEventListener("click", () => {
  const root = document.documentElement;
  const dark = root.dataset.theme ? root.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  root.dataset.theme = dark ? "light" : "dark";
});

renderMedals();
renderBenches();
</script>
</body>
</html>
"""
