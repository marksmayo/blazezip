"""Write the README's performance section and its charts from the benchmark leaderboards.

usage: python bench/readme_perf.py

Reads results/leaderboard.json (main suite) and results/ext-leaderboard.json (extended suite),
draws SVG charts into docs/perf/, and replaces everything between the perf markers in README.md.
Medals follow the same rules as the HTML reports (report.medals and extended.rank).
"""

import datetime as dt
import json
import re
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import extended  # noqa: E402
import report  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs" / "perf"
START, END = "<!-- perf:start -->", "<!-- perf:end -->"
BZ = "blazezip"
MEDAL = {0: "🥇", 1: "🥈", 2: "🥉"}
CATEGORY_TITLES = {
    "zip-create": "Create a ZIP",
    "zip-extract": "Extract a ZIP",
    "zip-roundtrip": "Extract the tool's own ZIP",
    "native-compress": "Compress, native format",
    "native-decompress": "Decompress, native format",
}
CORPUS_ORDER = ["canterbury", "silesia", "enwik8", "linux", "binaries", "media"]
PRESET_ORDER = ["fast", "default", "max"]
METRIC_TITLES = {"wall_s": "Speed (wall)", "cpu_s": "CPU time", "peak_mem": "Peak memory", "out_size": "Size"}


def fmt(metric, row, in_size=None):
    v = row[metric]
    if metric in ("wall_s", "cpu_s"):
        return f"{v:.3g} s" if v < 100 else f"{v:.0f} s"
    if metric == "peak_mem":
        return f"{v / 2**20:.1f} MiB"
    return f"{v / in_size:.1%}" if in_size else f"{v / 2**20:.1f} MiB"


def ordinal(n):
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def load():
    main = json.loads((ROOT / "results" / "leaderboard.json").read_text(encoding="utf-8"))
    ext = json.loads((ROOT / "results" / "ext-leaderboard.json").read_text(encoding="utf-8"))
    return main, ext


def groups_of(results):
    groups = {}
    for r in results:
        if r.get("ok"):
            groups.setdefault((r["category"], r["corpus"], r["preset"]), []).append(r)
    order = list(CATEGORY_TITLES)
    return dict(sorted(groups.items(), key=lambda kv: (order.index(kv[0][0]), CORPUS_ORDER.index(kv[0][1])
                                                       if kv[0][1] in CORPUS_ORDER else 99,
                                                       PRESET_ORDER.index(kv[0][2]) if kv[0][2] in PRESET_ORDER else 9)))


def ranked(rows, metric):
    return sorted((r for r in rows if r.get(metric) is not None), key=lambda r: r[metric])


def medal_rows(table, names):
    out = sorted(table.items(), key=lambda kv: (-kv[1][0], -kv[1][1], -kv[1][2]))
    lines = ["| # | Tool | 🥇 | 🥈 | 🥉 | Total |", "|--:|---|--:|--:|--:|--:|"]
    for i, (k, (g, s, b)) in enumerate(out, 1):
        name = f"**{names.get(k, k)}**" if k == BZ else names.get(k, k)
        lines.append(f"| {i} | {name} | {g} | {s} | {b} | {g + s + b} |")
    return lines, out


# ----- charts --------------------------------------------------------------------------------

def style_axes(ax):
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.tick_params(labelsize=8)
    ax.grid(axis="x", color="#e5e7eb", linewidth=0.6)
    ax.set_axisbelow(True)


def chart_medals(main_order, ext_order, names, path):
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.8), facecolor="white")
    for ax, order, title in ((axes[0], main_order, "Main suite"), (axes[1], ext_order, "Extended suite")):
        order = order[:12][::-1]
        labels = [names.get(k, k) for k, _ in order]
        left = [0] * len(order)
        for place, color in enumerate(("#f5b301", "#a8b3c2", "#c47a3a")):
            vals = [m[place] for _, m in order]
            ax.barh(labels, vals, left=left, color=color, edgecolor="white", linewidth=0.5,
                    label=["Gold", "Silver", "Bronze"][place])
            left = [a + b for a, b in zip(left, vals)]
        for lbl in ax.get_yticklabels():
            if lbl.get_text() == names.get(BZ, BZ):
                lbl.set_fontweight("bold")
                lbl.set_color("#ff5a1f")
        ax.set_title(title, fontsize=11, loc="left")
        style_axes(ax)
    axes[0].legend(fontsize=8, frameon=False, loc="lower right")
    fig.suptitle("Medals (gold / silver / bronze) per tool", fontsize=12, x=0.01, ha="left")
    fig.tight_layout()
    fig.savefig(path, format="svg")
    plt.close(fig)


def chart_bars(groups, category, preset, metric, names, colors, title, path, unit="s"):
    corpora = [c for c in CORPUS_ORDER if (category, c, preset) in groups]
    if not corpora:
        return False
    cols = 3
    rows_n = (len(corpora) + cols - 1) // cols
    fig, axes = plt.subplots(rows_n, cols, figsize=(12, 3.3 * rows_n), facecolor="white", squeeze=False)
    for ax in axes.flat[len(corpora):]:
        ax.axis("off")
    for ax, corpus in zip(axes.flat, corpora):
        rs = ranked(groups[(category, corpus, preset)], metric)[::-1]
        vals = [r[metric] / (2**20 if unit == "MiB" else 1) for r in rs]
        labels = [names.get(r["tool"], r["tool"]) for r in rs]
        cols_ = ["#ff5a1f" if r["tool"] == BZ else "#cbd5e1" for r in rs]
        ax.barh(labels, vals, color=cols_)
        ax.set_xlim(0, max(vals) * 1.18)
        for y, v in enumerate(vals):
            ax.text(v, y, f" {v:.3g}", va="center", fontsize=7, color="#334155")
        ax.set_title(corpus, fontsize=10, loc="left")
        ax.set_xlabel(f"{unit} (lower is better)", fontsize=7)
        style_axes(ax)
    fig.suptitle(title, fontsize=12, x=0.01, ha="left")
    fig.tight_layout()
    fig.savefig(path, format="svg")
    plt.close(fig)
    return True


def chart_tradeoff(groups, corpora, names, path):
    fig, axes = plt.subplots(1, len(corpora), figsize=(5.5 * len(corpora), 4.4), facecolor="white", squeeze=False)
    for ax, corpus in zip(axes.flat, corpora):
        for preset, marker in (("fast", "v"), ("default", "o"), ("max", "^")):
            for r in groups.get(("zip-create", corpus, preset), []):
                if r.get("wall_s") is None or not r.get("in_size"):
                    continue
                bz = r["tool"] == BZ
                ax.scatter(r["wall_s"], r["out_size"] / r["in_size"] * 100, marker=marker, s=60 if bz else 28,
                           color="#ff5a1f" if bz else "#94a3b8", zorder=3 if bz else 2,
                           edgecolors="black" if bz else "none", linewidths=0.6)
                if preset == "default":
                    ax.annotate(names.get(r["tool"], r["tool"]), (r["wall_s"], r["out_size"] / r["in_size"] * 100),
                                fontsize=7, xytext=(4, 2), textcoords="offset points",
                                color="#ff5a1f" if bz else "#475569", fontweight="bold" if bz else "normal")
        ax.set_xscale("log")
        ax.set_xlabel("wall time, s (log scale) — left is faster", fontsize=8)
        ax.set_ylabel("archive size, % of input — lower is smaller", fontsize=8)
        ax.set_title(f"{corpus}: speed vs size (▼ fast  ● default  ▲ max)", fontsize=10, loc="left")
        style_axes(ax)
        ax.grid(axis="y", color="#e5e7eb", linewidth=0.6)
    fig.tight_layout()
    fig.savefig(path, format="svg")
    plt.close(fig)


# ----- markdown ------------------------------------------------------------------------------

def bench_label(key):
    cat, corpus, preset = key
    return f"{CATEGORY_TITLES[cat]} · {corpus}" + (f" · {preset}" if cat in ("zip-create", "native-compress") else "")


def cell(rows, metric, in_size):
    rs = ranked(rows, metric)
    if len(rs) < 2:
        return "–", None
    place = next((i for i, r in enumerate(rs) if r["tool"] == BZ), None)
    if place is None:
        return "–", None
    me = fmt(metric, rs[place], in_size)
    if place < 3:
        return f"{MEDAL[place]} {me}", place
    best = rs[0]
    return f"{ordinal(place + 1)} · {me} (best {best['_name']} {fmt(metric, best, in_size)})", place


def main_section(main, names, chart_paths):
    groups = groups_of(main["records"].values())
    for rows in groups.values():
        for r in rows:
            r["_name"] = names.get(r["tool"], r["tool"])
    lines = []
    wins = {m: [0, 0] for m in METRIC_TITLES}
    table = ["| Benchmark | " + " | ".join(METRIC_TITLES.values()) + " |", "|---|" + "---|" * len(METRIC_TITLES)]
    for key, rows in groups.items():
        cat = key[0]
        in_size = next((r.get("in_size") for r in rows if r.get("in_size")), None)
        cells = []
        for m in METRIC_TITLES:
            if m not in report.CATEGORY_METRICS[cat]:
                cells.append("")
                continue
            c, place = cell(rows, m, in_size)
            if place is not None:
                wins[m][1] += 1
                wins[m][0] += place == 0
            cells.append(c)
        table.append(f"| {bench_label(key)} | " + " | ".join(cells) + " |")
    lines.append("Gold medals won by blazezip, by metric: " + ", ".join(
        f"**{METRIC_TITLES[m]} {g}/{n}**" for m, (g, n) in wins.items()) + ".")
    lines += ["", "Each cell is blazezip's result and its place; where it is not in the top three, the best "
              "tool's result is shown for comparison. Sizes are the archive as a share of the input.", ""]
    lines += table
    lines += ["", "<details><summary><b>Every tool, every benchmark (full main-suite results)</b></summary>", ""]
    for key, rows in groups.items():
        in_size = next((r.get("in_size") for r in rows if r.get("in_size")), None)
        metrics = report.CATEGORY_METRICS[key[0]]
        lines += [f"**{bench_label(key)}**", "",
                  "| Tool | " + " | ".join(METRIC_TITLES[m] for m in metrics) + " |",
                  "|---|" + "--:|" * len(metrics)]
        places = {m: {r["tool"]: i for i, r in enumerate(ranked(rows, m))} for m in metrics}
        for r in ranked(rows, "wall_s"):
            vals = []
            for m in metrics:
                if r.get(m) is None:
                    vals.append("–")
                    continue
                p = places[m].get(r["tool"])
                vals.append(f"{MEDAL.get(p, '')} {fmt(m, r, in_size)}".strip())
            name = f"**{r['_name']}**" if r["tool"] == BZ else r["_name"]
            lines.append(f"| {name} | " + " | ".join(vals) + " |")
        lines.append("")
    lines += ["</details>", ""]
    return lines


def ext_section(ext, names):
    rows = [dict(r) for r in ext["records"].values()]
    groups = extended.rank(rows)
    tally = {}
    for r in rows:
        if r.get("place") in (1, 2, 3):
            tally.setdefault(r["tool"], [0, 0, 0])[r["place"] - 1] += 1
    medal_lines, order = medal_rows(tally, names)
    mine = sorted((r for r in rows if r["tool"] == BZ and r.get("place")), key=lambda r: (r["test"], r["metric"]))

    def val(r):
        v = r["value"]
        if isinstance(v, float):
            v = f"{v:.4g}"
        return f"{v} {r.get('unit') or ''}".strip()

    lines = ["The extended suite covers behaviour as well as speed: scaling, start-up time, very large and very "
             "many files, Unicode and legacy names, metadata, corrupt and hostile archives, locked files, "
             "interruption, memory caps and more. Each (test, metric) pair awards medals; ties share a place.", ""]
    lines += medal_lines
    golds = [r for r in mine if r["place"] == 1]
    lines += ["", f"blazezip places first in **{len(golds)}** of the {len(mine)} ranked metrics it takes part in.", "",
              "<details><summary><b>Every extended-suite metric, with blazezip's place</b></summary>", "",
              "| Test | Metric | blazezip | Place | Best other tool |", "|---|---|--:|:-:|---|"]
    for r in mine:
        others = [o for o in groups[(r["test"], r["metric"])] if o["tool"] != BZ]
        best = others[0] if others else None
        place = MEDAL.get(r["place"] - 1, ordinal(r["place"]))
        lines.append(f"| {r['test']} | {r['metric']} | {val(r)} | {place} | "
                     + (f"{names.get(best['tool'], best['tool'])} {val(best)}" if best else "–") + " |")
    lines += ["", "</details>", ""]
    return lines, order


def build():
    main, ext = load()
    names = {k: t["name"] for k, t in main["tools"].items()}
    colors = {k: t["color"] for k, t in main["tools"].items()}
    DOCS.mkdir(parents=True, exist_ok=True)
    meta = main.get("meta", {})
    main_rows = [r for r in main["records"].values()]
    main_medals, main_order = medal_rows(report.medals(main_rows), names)
    ext_lines, ext_order = ext_section(ext, names)
    chart_medals(main_order, ext_order, names, DOCS / "medals.svg")
    groups = groups_of(main_rows)
    charts = []
    for cat, preset, metric, title, name, unit in (
        ("zip-create", "default", "wall_s", "Create a ZIP (default level): wall time", "zip-create.svg", "s"),
        ("zip-extract", "default", "wall_s", "Extract a ZIP: wall time", "zip-extract.svg", "s"),
        ("native-compress", "default", "wall_s", "Compress in each tool's native format (default level): wall time",
         "native-compress.svg", "s"),
        ("native-decompress", "default", "wall_s", "Decompress native format: wall time", "native-decompress.svg", "s"),
        ("zip-create", "default", "peak_mem", "Create a ZIP (default level): peak memory", "zip-create-memory.svg", "MiB"),
    ):
        if chart_bars(groups, cat, preset, metric, names, colors, title, DOCS / name, unit):
            charts.append((title, name))
    chart_tradeoff(groups, ["silesia", "linux"], names, DOCS / "speed-vs-size.svg")

    corp = main.get("corpora", {})
    lines = [START, "", "## Performance", "",
             f"_Measured on {meta.get('cpu', '?')} ({meta.get('cores', '?')} cores, {meta.get('threads', '?')} threads), "
             f"{meta.get('ram_gb', '?')} GB RAM, {meta.get('disk', '?')}, {meta.get('os', '?')}, with Windows Defender "
             f"real-time protection on. Every test runs {meta.get('reps', 10)} times under a Windows Job Object; the "
             "median of the fastest five runs is reported. Competitors keep their best result across recent runs, "
             "blazezip shows its latest. Tool versions are listed below. Generated by `python bench/readme_perf.py` on "
             f"{dt.date.today().isoformat()}._", "",
             "![Medals per tool](docs/perf/medals.svg)", "",
             "### Main suite: medal table", "",
             "Every benchmark awards gold, silver and bronze for each metric: wall time, CPU time, peak memory and "
             "(when creating) archive size.", ""]
    lines += main_medals
    lines += ["", "### Where blazezip wins", ""]
    lines += main_section(main, names, charts)
    lines += ["### Charts", "", "blazezip is highlighted in orange.", ""]
    lines += ["![Speed versus size](docs/perf/speed-vs-size.svg)", ""]
    for title, name in charts:
        lines += [f"![{title}](docs/perf/{name})", ""]
    lines += ["### Extended suite", ""]
    lines += ext_lines
    lines += ["### Corpora and tool versions", "", "| Corpus | Files | Size | Description |", "|---|--:|--:|---|"]
    for k in CORPUS_ORDER:
        if k in corp:
            c = corp[k]
            lines.append(f"| {k} | {c['files']:,} | {c['bytes'] / 1e6:,.0f} MB | {c.get('desc', '')} |")
    lines += ["", "| Tool | Version | Native format |", "|---|---|---|"]
    for k, t in main["tools"].items():
        if t.get("available"):
            lines.append(f"| {t['name']} | {t.get('version') or '–'} | {t.get('native_format') or '–'} |")
    lines += ["", END]
    block = "\n".join(lines)

    readme = ROOT / "README.md"
    text = readme.read_text(encoding="utf-8")
    if START in text:
        text = re.sub(re.escape(START) + ".*?" + re.escape(END), lambda _: block, text, flags=re.S)
    else:
        anchor = "\n## Benchmarks"
        text = text.replace(anchor, "\n" + block + "\n" + anchor, 1) if anchor in text else text + "\n" + block + "\n"
    readme.write_text(text, encoding="utf-8")
    return block


if __name__ == "__main__":
    b = build()
    print(f"README performance section: {len(b.splitlines())} lines; charts in {DOCS}")
