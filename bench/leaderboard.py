"""Persistent leaderboard across benchmark runs.

Rules:
  * Competitors keep their best-ever value per test and metric (each metric independently), so
    blazezip is always compared against every other tool's best showing.
  * blazezip always uses its latest result: it is the thing under development.
  * A record is discarded when the tool's version changes (a new release gets re-benchmarked from
    scratch) or when the corpus changes size (different input data).
  * A failed run never replaces a successful record.
  * A result measured with more runs per test replaces one measured with fewer.
"""

import json
from pathlib import Path

METRICS = ("wall_s", "cpu_s", "peak_mem", "out_size")
SUBJECT = "blazezip"


def key(r):
    return f"{r['category']}|{r['corpus']}|{r['preset']}|{r['tool']}"


class Leaderboard:
    def __init__(self, path: Path):
        self.path = path
        self.data = {"versions": {}, "records": {}, "meta": {}, "corpora": {}, "tools": {}}
        if path.exists():
            self.data.update(json.loads(path.read_text()))

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1))
        tmp.replace(self.path)

    def set_tool_versions(self, tools):
        """Drop records of any tool whose installed version differs from the recorded one."""
        for t in tools:
            if not t["available"]:
                continue
            old = self.data["versions"].get(t["key"])
            if old is not None and old != t["version"]:
                print(f"  leaderboard: {t['name']} changed {old!r} -> {t['version']!r}; discarding its old results")
                self.data["records"] = {k: v for k, v in self.data["records"].items() if v["tool"] != t["key"]}
            self.data["versions"][t["key"]] = t["version"]
            self.data["tools"][t["key"]] = t

    def set_corpora(self, corpora: dict):
        for name, info in corpora.items():
            old = self.data["corpora"].get(name)
            if old and old.get("bytes") != info.get("bytes"):
                print(f"  leaderboard: corpus {name} changed; discarding its results")
                self.data["records"] = {k: v for k, v in self.data["records"].items() if v["corpus"] != name}
            self.data["corpora"][name] = info

    def add(self, row: dict, run_id: str):
        row = {**row, "run": run_id}
        k = key(row)
        cur = self.data["records"].get(k)
        if row["tool"] == SUBJECT:
            # Latest wins, including a failure: a regression must show up.
            row["best_of"] = 1
            self.data["records"][k] = row
            return
        if not row.get("ok"):
            if cur is None:
                self.data["records"][k] = {**row, "best_of": 0}
            return
        # A result measured with more runs supersedes one with fewer (e.g. 10-run vs 3-run medians).
        if cur is None or not cur.get("ok") or row.get("reps", 0) > cur.get("reps", 0):
            self.data["records"][k] = {**row, "best_of": 1, "best_runs": {m: run_id for m in METRICS if row.get(m) is not None}}
            return
        if row.get("reps", 0) < cur.get("reps", 0):
            return  # fewer runs than the record: not comparable
        merged = dict(cur)
        merged["best_of"] = cur.get("best_of", 1) + 1
        runs = dict(cur.get("best_runs", {}))
        for m in METRICS:
            if row.get(m) is not None and (cur.get(m) is None or row[m] < cur[m]):
                merged[m] = row[m]
                runs[m] = run_id
                if m == "out_size" and row.get("ratio") is not None:
                    merged["ratio"] = row["ratio"]
        merged["best_runs"] = runs
        self.data["records"][k] = merged

    def payload(self, meta: dict, tools: list):
        """Report payload: the same shape as a single run's results.json."""
        known = {t["key"]: t for t in self.data["tools"].values()}
        for t in tools:
            known[t["key"]] = t
        return {
            "meta": {**self.data.get("meta", {}), **meta, "leaderboard": True},
            "corpora": self.data["corpora"],
            "tools": list(known.values()),
            "results": list(self.data["records"].values()),
        }
