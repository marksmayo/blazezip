"""blazezip benchmark suite.

Runs every available archiver/compressor over a set of standard corpora, measuring wall time,
CPU time and peak memory (whole process tree, via a Windows Job Object), verifies every output,
awards medals per benchmark and metric, and writes a self-contained HTML report.

usage: python bench/bench.py [--quick] [--corpora silesia,linux] [--tools blazezip,7zip]
                             [--reps 10] [--work C:\\bzbench] [--no-native] [--resume DIR]
"""

import argparse
import datetime as dt
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import corpus as corpusmod  # noqa: E402
import ext_more  # noqa: E402
import leaderboard  # noqa: E402
import report  # noqa: E402
import tools as toolsmod  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
BENCHRUN = ROOT / "target" / "release" / "benchrun.exe"

ALL_CORPORA = ["canterbury", "silesia", "enwik8", "linux", "binaries", "media"]
NATIVE_CORPORA = ["silesia", "enwik8", "linux"]
QUICK_CORPORA = ["canterbury", "silesia", "linux"]


def log(*a):
    print(*a, flush=True)


def rmtree(p: Path):
    if not p.exists():
        return
    for _ in range(5):
        try:
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink()
            return
        except OSError:
            time.sleep(0.5)
    shutil.rmtree(p, ignore_errors=True)


def measure(cmd: toolsmod.Cmd, timeout: float) -> dict:
    argv = [str(BENCHRUN), "--timeout", str(timeout)]
    if cmd.cwd:
        argv += ["--cwd", str(cmd.cwd)]
    if cmd.stdout:
        argv += ["--stdout", str(cmd.stdout)]
    if cmd.console:
        argv += ["--console"]
    argv += ["--", *map(str, cmd.argv)]
    r = subprocess.run(argv, capture_output=True, text=True)
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        return {"exit": -1, "error": (r.stdout + r.stderr)[-500:]}


class Runner:
    def __init__(self, args, tools, corpora):
        self.args = args
        self.tools = tools
        self.corpora = corpora
        self.work = Path(args.work)
        self.out = self.work / "scratch"
        self.results = []
        self.done = set()
        self.manifests = {}
        self.sevenzip = next((t for t in tools if t.key == "7zip" and t.available), None)

    # ---------- bookkeeping ----------
    def key(self, cat, corpus, preset, tool):
        return f"{cat}|{corpus}|{preset}|{tool}"

    def record(self, row):
        row["version"] = next((t.version for t in self.tools if t.key == row["tool"]), "")
        self.results.append(row)
        self.save()
        self.board.add(row, self.args.run_dir.name)
        self.board.save()

    def save(self):
        self.args.run_dir.mkdir(parents=True, exist_ok=True)
        # Write-then-rename: a failed write (e.g. a full disk) must not truncate earlier results.
        out = self.args.run_dir / "results.json"
        tmp = out.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.payload(), indent=1))
        tmp.replace(out)

    def payload(self):
        return {
            "meta": self.meta,
            "corpora": self.corpus_info,
            "tools": tool_dicts(self.tools),
            "results": self.results,
        }

    # ---------- measurement ----------
    def run_reps(self, make_cmd, prepare, verify, timeout):
        """Run a command `reps` times (fewer when it's slow); return median metrics.

        prepare() runs (unmeasured) before every rep; verify() checks the first rep's output.
        """
        ext_more.wait_for_memory(2048, log=log)  # don't measure a machine that is short of memory
        runs = []
        err = ""
        for rep in range(self.args.reps):
            prepare()
            m = measure(make_cmd(), timeout)
            if m.get("timed_out"):
                return None, "timed out"
            if m.get("exit") not in (0, 1):
                err = m.get("error") or f"exit code {m.get('exit')}"
                return None, err
            if rep == 0:
                ok, why = verify()
                if not ok:
                    return None, why
            runs.append(m)
            if self.args.long_run and m["wall_s"] > self.args.long_run:
                break
        all_runs = runs
        if self.args.keep_best and len(runs) > self.args.keep_best:
            # Discard the slowest runs: on a busy machine they measure interference, not the tool.
            runs = sorted(runs, key=lambda r: r["wall_s"])[: self.args.keep_best]
        med = lambda k: statistics.median(r[k] for r in runs)
        return {
            "wall_s": med("wall_s"),
            "cpu_s": statistics.median(r["user_s"] + r["kernel_s"] for r in runs),
            "user_s": med("user_s"),
            "kernel_s": med("kernel_s"),
            "peak_mem": med("peak_job_commit"),
            "peak_ws": med("peak_ws"),
            "procs": runs[0]["procs"],
            "reps": len(runs),
            "runs_measured": len(all_runs),
            "all_wall": [r["wall_s"] for r in all_runs],
            "min_wall": min(r["wall_s"] for r in runs),
            "max_wall": max(r["wall_s"] for r in runs),
        }, ""

    def verify_tree(self, dest: Path, corpus: str):
        man = self.manifests[corpus]
        root = dest / corpus if (dest / corpus).is_dir() else dest
        got = {}
        for p in root.rglob("*"):
            if p.is_file():
                got[str(p.relative_to(root)).replace("\\", "/").lower()] = p
        if len(got) != len(man):
            return False, f"extracted {len(got)} files, expected {len(man)}"
        for rel, (size, h) in man.items():
            p = got.get(rel)
            if p is None:
                return False, f"missing {rel}"
            if p.stat().st_size != size:
                return False, f"content mismatch in {rel}"
        # Hash in parallel: hashlib releases the GIL, and the Linux corpus has ~70k files.
        rels = list(man)
        with ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as ex:
            hashes = ex.map(lambda r: corpusmod.file_hash(got[r]), rels, chunksize=64)
            for rel, h in zip(rels, hashes):
                if h != man[rel][1]:
                    return False, f"content mismatch in {rel}"
        return True, ""

    def verify_zip(self, z: Path):
        if not z.exists() or z.stat().st_size == 0:
            return False, "no archive produced"
        if not self.sevenzip:
            return True, ""
        r = subprocess.run([self.sevenzip.exe, "t", str(z), "-bso0", "-bsp0"], capture_output=True, text=True)
        return (r.returncode == 0, "" if r.returncode == 0 else "archive failed integrity test (7z t)")

    def row(self, cat, corpus, preset, tool, res, err, **extra):
        r = {"category": cat, "corpus": corpus, "preset": preset, "tool": tool.key,
             "ok": res is not None, "error": err, **(res or {}), **extra}
        self.record(r)
        status = (f"{res['wall_s']:8.2f}s  cpu {res['cpu_s']:7.2f}s  mem {res['peak_mem'] / 2**20:7.1f} MiB"
                  + (f"  ratio {extra['ratio'] * 100:5.1f}%" if extra.get("ratio") else "")) if res else f"FAILED: {err}"
        log(f"  {cat:<16} {corpus:<10} {preset:<8} {tool.name:<24} {status}")

    # ---------- benchmarks ----------
    def bench_zip_create(self, corpus: str, path: Path, size: int):
        arch_dir = self.work / "archives" / corpus
        arch_dir.mkdir(parents=True, exist_ok=True)
        for preset in toolsmod.PRESETS:
            for t in self.tools:
                if not (t.available and t.zip_create and preset in t.zip_create_presets):
                    continue
                k = self.key("zip-create", corpus, preset, t.key)
                if k in self.done:
                    continue
                out = self.out / f"{t.key}-{preset}.zip"
                self.out.mkdir(parents=True, exist_ok=True)
                res, err = self.run_reps(
                    lambda: t.zip_create(preset, str(out), str(path.parent), path.name),
                    lambda: rmtree(out),
                    lambda: self.verify_zip(out),
                    self.args.timeout,
                )
                extra = {}
                if res:
                    extra = {"out_size": out.stat().st_size, "in_size": size, "ratio": out.stat().st_size / size}
                    if preset == "default":
                        shutil.copyfile(out, arch_dir / f"{t.key}.zip")
                self.row("zip-create", corpus, preset, t, res, err, **extra)
                rmtree(out)

    def reference_zip(self, corpus: str, path: Path) -> Path:
        ref = self.work / "archives" / corpus / "_reference.zip"
        if not ref.exists():
            ref.parent.mkdir(parents=True, exist_ok=True)
            if self.sevenzip:
                subprocess.run([self.sevenzip.exe, "a", "-tzip", "-mx5", str(ref), path.name, "-bso0", "-bsp0"],
                               cwd=path.parent, check=True)
            else:
                subprocess.run(["zip", "-r", "-q", "-6", str(ref), path.name], cwd=path.parent, check=True)
        return ref

    def bench_zip_extract(self, corpus: str, path: Path, size: int):
        ref = self.reference_zip(corpus, path)
        for cat, archive_for in (
            ("zip-extract", lambda t: ref),
            ("zip-roundtrip", lambda t: self.work / "archives" / corpus / f"{t.key}.zip"),
        ):
            for t in self.tools:
                if not (t.available and t.zip_extract):
                    continue
                k = self.key(cat, corpus, "default", t.key)
                if k in self.done:
                    continue
                z = archive_for(t)
                if not z.exists():
                    continue
                dest = self.out / f"x-{t.key}"
                res, err = self.run_reps(
                    lambda: t.zip_extract(str(z), str(dest)),
                    lambda: (rmtree(dest), dest.mkdir(parents=True)),
                    lambda: self.verify_tree(dest, corpus),
                    self.args.timeout,
                )
                self.row(cat, corpus, "default", t, res, err, in_size=size)
                rmtree(dest)
                if cat == "zip-roundtrip":
                    z.unlink(missing_ok=True)  # only needed for this test; the reference zip is kept

    def cached_hash(self, src: Path) -> str:
        """Hash of a native-benchmark source file, cached across runs by size and mtime."""
        cache = self.work / "manifests" / "native-hashes.json"
        data = json.loads(cache.read_text()) if cache.exists() else {}
        st = src.stat()
        key = f"{src}|{st.st_size}|{st.st_mtime_ns}"
        if key not in data:
            data[key] = corpusmod.file_hash(src)
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(data, indent=1))
        return data[key]

    def bench_native(self, corpus: str, path: Path, size: int):
        if path.is_dir() and len(list(path.iterdir())) == 1 and corpus == "enwik8":
            src = next(path.iterdir())
        else:
            src = corpusmod.make_tar(path, self.work / "tars" / f"{corpus}.tar")
        src_size = src.stat().st_size
        src_hash = self.cached_hash(src)
        keep = {}
        for preset in toolsmod.PRESETS:
            for t in self.tools:
                if not (t.available and t.native_compress and preset in t.native_presets):
                    continue
                k = self.key("native-compress", corpus, preset, t.key)
                base = str(self.out / f"n-{t.key}-{preset}")
                cmd, produced = t.native_compress(preset, str(src), base)
                if k not in self.done:
                    self.out.mkdir(parents=True, exist_ok=True)
                    res, err = self.run_reps(
                        lambda: t.native_compress(preset, str(src), base)[0],
                        lambda: rmtree(Path(produced)),
                        lambda: (Path(produced).exists() and Path(produced).stat().st_size > 0, "no output"),
                        self.args.timeout,
                    )
                    extra = {}
                    if res:
                        osz = Path(produced).stat().st_size
                        extra = {"out_size": osz, "in_size": src_size, "ratio": osz / src_size, "format": t.native_format}
                    self.row("native-compress", corpus, preset, t, res, err, **extra)
                if Path(produced).exists():
                    keep[(t.key, preset)] = produced
        for (tkey, preset), produced in keep.items():
            t = next(x for x in self.tools if x.key == tkey)
            if t.native_decompress and preset == "default":
                k = self.key("native-decompress", corpus, preset, t.key)
                if k not in self.done:
                    dest = self.out / f"nd-{t.key}"
                    cmd, outfile = t.native_decompress(produced, str(dest), src.name)

                    def check():
                        p = Path(outfile)
                        if not p.exists():
                            return False, "no output"
                        return (corpusmod.file_hash(p) == src_hash, "decompressed data differs")

                    res, err = self.run_reps(
                        lambda: t.native_decompress(produced, str(dest), src.name)[0],
                        lambda: (rmtree(dest), dest.mkdir(parents=True)),
                        check,
                        self.args.timeout,
                    )
                    self.row("native-decompress", corpus, preset, t, res, err, in_size=src_size, format=t.native_format)
                    rmtree(dest)
        for produced in keep.values():
            rmtree(Path(produced))

    def run(self):
        self.meta = system_info(self.args)
        self.corpus_info = {}
        paths = corpusmod.ensure_corpora(self.work, self.corpora)
        for name, path in paths.items():
            n, size = corpusmod.stats(path)
            self.corpus_info[name] = {"files": n, "bytes": size, "desc": corpusmod.CORPORA[name]}
        self.board.set_corpora(self.corpus_info)
        self.board.save()
        self.save()
        for name, path in paths.items():
            info = self.corpus_info[name]
            log(f"\n=== {name}: {info['files']} files, {info['bytes'] / 2**20:.1f} MiB ===")
            mf = self.work / "manifests" / f"{name}.json"
            if mf.exists():
                self.manifests[name] = {k: tuple(v) for k, v in json.loads(mf.read_text()).items()}
            else:
                self.manifests[name] = corpusmod.manifest(path)
                mf.parent.mkdir(parents=True, exist_ok=True)
                mf.write_text(json.dumps(self.manifests[name]))
            if "zip" in self.args.categories:
                self.bench_zip_create(name, path, info["bytes"])
                self.bench_zip_extract(name, path, info["bytes"])
            if "native" in self.args.categories and name in NATIVE_CORPORA:
                self.bench_native(name, path, info["bytes"])
            # Refresh the leaderboard report after each corpus so progress can be followed.
            self.board.data["meta"] = {**self.meta, "in_progress": True}
            self.board.save()
            write_reports(self.board, self.board.data["meta"], self.tools, self.args.run_dir)
            # pzip leaves a temp directory behind on every run.
            for d in Path(tempfile.gettempdir()).glob("pzip-overflow*"):
                rmtree(d)
            log(f"corpus done: {name}")
        self.meta["finished"] = dt.datetime.now().isoformat(timespec="seconds")
        self.save()
        rmtree(self.out)


def tool_dicts(tools):
    return [
        {"key": t.key, "name": t.name, "color": t.color, "available": t.available,
         "version": t.version, "note": t.note,
         "zip": bool(t.zip_create or t.zip_extract), "native_format": t.native_format}
        for t in tools
    ]


def write_reports(board, meta, tools, run_dir=None):
    """The headline report is the leaderboard (competitors' best runs vs blazezip's latest)."""
    payload = board.payload(meta, tool_dicts(tools))
    out = (run_dir or ROOT / "results") / "report.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    report.write(payload, out)
    results = ROOT / "results"
    shutil.copyfile(out, results / "latest-report.html")
    if out.parent != results:
        shutil.copyfile(out.with_name("report-artifact.html"), results / "report-artifact.html")
    return results / "latest-report.html"


def bench_build_env() -> dict:
    """Build environment for benchmark binaries.

    The benchmark only ever runs on this machine, so blazezip may target its CPU (x86-64-v3:
    AVX2/BMI2) when the CPU supports it, and the C dependencies (libdeflate, zstd) are compiled
    with clang-cl when it is installed. Release builds outside the benchmark stay portable.
    """
    env = os.environ.copy()
    flags = ["-C", "target-feature=+crt-static"]
    try:
        import ctypes
        PF_AVX2_INSTRUCTIONS_AVAILABLE = 40
        if ctypes.windll.kernel32.IsProcessorFeaturePresent(PF_AVX2_INSTRUCTIONS_AVAILABLE):
            flags += ["-C", "target-cpu=x86-64-v3"]
    except Exception:
        pass
    env["CARGO_ENCODED_RUSTFLAGS"] = "".join(flags)
    llvm = Path(r"C:\Program Files\LLVM\bin\clang-cl.exe")
    clang = shutil.which("clang-cl") or (str(llvm) if llvm.exists() else None)
    if clang and "CC" not in env:
        env["CC_x86_64_pc_windows_msvc"] = clang
    return env


def system_info(args):
    def ps(q):
        try:
            return subprocess.run(["powershell", "-NoProfile", "-Command", q], capture_output=True, text=True,
                                  timeout=30).stdout.strip()
        except Exception:
            return ""

    return {
        "started": dt.datetime.now().isoformat(timespec="seconds"),
        "cpu": ps("(Get-CimInstance Win32_Processor | Select-Object -First 1).Name"),
        "cores": ps("(Get-CimInstance Win32_Processor | Measure-Object NumberOfCores -Sum).Sum"),
        "threads": os.cpu_count(),
        "ram_gb": round(float(ps("(Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory") or 0) / 2**30, 1),
        "os": f"{platform.system()} {platform.release()} ({platform.version()})",
        "disk": ps(f"(Get-Volume -DriveLetter '{str(Path(args.work).resolve())[0]}' | Get-Partition | Get-Disk).FriendlyName"),
        "reps": args.reps,
        "quick": args.quick,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--work", default=r"C:\bzbench", help="working directory for corpora and scratch output")
    ap.add_argument("--corpora", help="comma-separated subset of: " + ",".join(ALL_CORPORA))
    ap.add_argument("--tools", help="comma-separated subset of tool keys")
    ap.add_argument("--categories", default="zip,native", help="zip,native")
    ap.add_argument("--no-native", action="store_true")
    ap.add_argument("--reps", type=int, default=10, help="runs per test (median is reported)")
    ap.add_argument("--keep-best", type=int, default=None, metavar="K",
                    help="report the median of only the K fastest runs (discard the slowest)")
    ap.add_argument("--long-run", type=float, default=None,
                    help="optional: stop repeating a test once one run exceeds this many seconds")
    ap.add_argument("--timeout", type=float, default=900.0)
    ap.add_argument("--quick", action="store_true", help="fewer corpora (still --reps runs per test)")
    ap.add_argument("--resume", help="results directory of an interrupted run to continue")
    ap.add_argument("--report", action="store_true", help="rebuild the leaderboard report and exit")
    ap.add_argument("--import-runs", nargs="+", metavar="DIR",
                    help="feed earlier results directories into the leaderboard (oldest first) and exit")
    args = ap.parse_args()
    board = leaderboard.Leaderboard(ROOT / "results" / "leaderboard.json")

    if args.report or args.import_runs:
        tools = toolsmod.discover()
        board.set_tool_versions(tool_dicts(tools))
        versions = {t.key: t.version for t in tools}
        for d in map(Path, args.import_runs or []):
            prev = json.loads((d / "results.json").read_text())
            board.set_corpora(prev.get("corpora", {}))
            for row in prev["results"]:
                # Rows from before versions were recorded belong to the currently installed version.
                row.setdefault("version", versions.get(row["tool"], ""))
                board.add(row, d.name)
            board.data["meta"] = prev.get("meta", board.data.get("meta", {}))
            log(f"imported {len(prev['results'])} results from {d}")
        board.save()
        log(f"report: {write_reports(board, board.data.get('meta', {}), tools)}")
        return

    corpora = args.corpora.split(",") if args.corpora else (QUICK_CORPORA if args.quick else ALL_CORPORA)
    args.categories = set(args.categories.split(","))
    if args.no_native:
        args.categories.discard("native")

    log("building blazezip + benchrun (release) ...")
    subprocess.run(["cargo", "build", "--release", "--workspace"], cwd=ROOT, check=True, env=bench_build_env())

    tools = toolsmod.discover()
    if args.tools:
        want = set(args.tools.split(","))
        tools = [t for t in tools if t.key in want]
    log("\nTools:")
    for t in tools:
        log(f"  {'OK ' if t.available else '-- '} {t.name:<24} {t.version or t.note}")

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    args.run_dir = Path(args.resume) if args.resume else ROOT / "results" / stamp
    board.set_tool_versions(tool_dicts(tools))
    board.save()
    r = Runner(args, tools, corpora)
    r.board = board
    if args.resume and (args.run_dir / "results.json").exists():
        prev = json.loads((args.run_dir / "results.json").read_text())
        r.results = [x for x in prev["results"] if x["ok"]]
        r.done = {r.key(x["category"], x["corpus"], x["preset"], x["tool"]) for x in r.results}
        log(f"resuming: {len(r.done)} results already done")
    r.run()

    board.data["meta"] = r.meta
    board.save()
    log(f"report: {write_reports(board, r.meta, tools, args.run_dir)}")


if __name__ == "__main__":
    main()
