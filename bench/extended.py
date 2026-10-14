"""Extended benchmark suite: fairness, robustness and compatibility metrics beyond raw speed.

Complements bench.py (speed, CPU, memory and ratio per corpus and preset) with 91 further
metrics: speed under constrained and contended CPUs, unusual workload shapes, I/O and resource
behaviour, archive quality, correctness, compatibility and robustness (M01-M50), then Windows
file-system edge cases, archives from other systems, safety, real-world conditions, the whole
speed/size curve, more data types, operations at scale and footprint (M51-M91). Each metric ranks every
tool that supports the operation and awards medals; tools that cannot perform an operation are
listed as "n/a" rather than penalised. Metrics this machine cannot measure are reported with
the reason.

Results also feed results/ext-leaderboard.json, which keeps every competitor's best value per
metric and blazezip's latest; results/ext-latest-report.html is built from it.

usage: python bench/extended.py [--tools blazezip,7zip] [--only T11,T38] [--reps 5]
                                [--keep-best 3] [--main results/<run>] [--skip-heavy]
"""

import argparse
import ctypes
import datetime as dt
import html
import json
import os
import random
import shutil
import statistics
import subprocess
import sys
import time
import zipfile
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ext_more as em  # noqa: E402
import tools as toolsmod  # noqa: E402
from tools import Cmd  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
BENCHRUN = ROOT / "target" / "release" / "benchrun.exe"
LOGICAL = os.cpu_count() or 1


def log(*a):
    print(*a, flush=True)


def rmtree(p: Path):
    """Delete a file or tree, clearing read-only attributes (metadata tests create some)."""
    p = Path(p)
    if not p.exists() and not p.is_symlink():
        return

    for _ in range(5):
        try:
            if p.is_dir() or em.is_junction(p):
                em.remove_tree(p)  # never follows junctions; handles device and trailing-dot names
            else:
                os.chmod(p, 0o666)
                p.unlink()
            return
        except OSError:
            time.sleep(0.5)


def long(p) -> str:
    """Extended-length form of a path, so trees deeper than MAX_PATH can be created and deleted."""
    s = str(Path(p).resolve())
    return s if s.startswith("\\\\?\\") else "\\\\?\\" + s


# ---------------------------------------------------------------------------------------------
# Per-tool operations beyond create/extract
# ---------------------------------------------------------------------------------------------

def ops_for(t: toolsmod.Tool) -> dict:
    """list / test / extract_one / add / delete commands, where the tool's CLI has them."""
    exe = t.exe
    o = {}
    if t.key in ("7zip", "nanazip", "peazip"):
        q = ["-y", "-bso0", "-bsp0", "-bse0"]
        o["list"] = lambda z: Cmd([exe, "l", z, *q])
        o["test"] = lambda z: Cmd([exe, "t", z, "-mmt=on", *q])
        o["extract_one"] = lambda z, name, d: Cmd([exe, "x", z, f"-o{d}", name, *q])
        o["add"] = lambda z, f, cwd: Cmd([exe, "a", "-tzip", z, f, *q], cwd=cwd)
        o["delete"] = lambda z, name: Cmd([exe, "d", z, name, *q])
        o["update"] = lambda z, f, cwd: Cmd([exe, "u", "-tzip", z, f, *q], cwd=cwd)
        o["extract_pw"] = lambda z, d, pw: Cmd([exe, "x", z, f"-o{d}", f"-p{pw}", *q])
    elif t.key == "blazezip":
        o["list"] = lambda z: Cmd([exe, "l", z])
        o["test"] = lambda z: Cmd([exe, "t", z, "-q"])
        o["extract_one"] = lambda z, name, d: Cmd([exe, "x", z, name, "-o", d, "-q"])
        o["add"] = lambda z, f, cwd: Cmd([exe, "a", z, f, "-q"], cwd=cwd)
        o["delete"] = lambda z, name: Cmd([exe, "d", z, name, "-q"])
        o["update"] = lambda z, f, cwd: Cmd([exe, "a", z, f, "-q"], cwd=cwd)
    elif t.key == "bandizip":
        o["list"] = lambda z: Cmd([exe, "l", z])
        o["test"] = lambda z: Cmd([exe, "t", z])
        o["extract_one"] = lambda z, name, d: Cmd([exe, "x", "-y", "-aoa", f"-o:{d}", z, name])
        o["add"] = lambda z, f, cwd: Cmd([exe, "a", "-y", z, f], cwd=cwd)
        o["delete"] = lambda z, name: Cmd([exe, "d", "-y", z, name])
        o["update"] = lambda z, f, cwd: Cmd([exe, "a", "-y", z, f], cwd=cwd)
        o["extract_pw"] = lambda z, d, pw: Cmd([exe, "x", "-y", "-aoa", f"-o:{d}", f"-p:{pw}", z])
    elif t.key == "winrar":
        o["test"] = lambda z: Cmd([exe, "t", "-ibck", "-y", z])
        o["extract_one"] = lambda z, name, d: Cmd([exe, "x", "-ibck", "-y", z, name.replace("/", "\\"), d + "\\"])
        o["add"] = lambda z, f, cwd: Cmd([exe, "a", "-afzip", "-ibck", "-y", z, f], cwd=cwd)
        o["delete"] = lambda z, name: Cmd([exe, "d", "-ibck", "-y", z, name.replace("/", "\\")])
        o["update"] = lambda z, f, cwd: Cmd([exe, "u", "-afzip", "-r", "-ibck", "-y", z, f], cwd=cwd)
        o["extract_pw"] = lambda z, d, pw: Cmd([exe, "x", "-ibck", "-y", f"-p{pw}", z, d + "\\"])
    elif t.key == "winzip":
        wz = exe
        wuz = str(Path(exe).with_name("wzunzip.exe"))
        o["list"] = lambda z: Cmd([wuz, "-v", z], console=True)
        o["test"] = lambda z: Cmd([wuz, "-t", z], console=True)
        o["extract_one"] = lambda z, name, d: Cmd([wuz, "-d", "-o", "-ybc", z, d, name], console=True)
        o["add"] = lambda z, f, cwd: Cmd([wz, "-a", z, f], cwd=cwd, console=True)
        o["delete"] = lambda z, name: Cmd([wz, "-d", z, name], console=True)
        o["update"] = lambda z, f, cwd: Cmd([wz, "-u", "-r", "-p", z, f + "\\*.*"], cwd=cwd, console=True)
        o["extract_pw"] = lambda z, d, pw: Cmd([wuz, "-d", "-o", "-ybc", f"-s{pw}", z, d], console=True)
    elif t.key == "infozip":
        izip, iunzip = toolsmod.first("zip"), toolsmod.first("unzip")
        if iunzip:
            o["list"] = lambda z: Cmd([iunzip, "-l", z])
            o["test"] = lambda z: Cmd([iunzip, "-tq", z])
            o["extract_one"] = lambda z, name, d: Cmd([iunzip, "-q", "-o", z, name, "-d", d])
            o["extract_pw"] = lambda z, d, pw: Cmd([iunzip, "-q", "-o", "-P", pw, z, "-d", d])
        if izip:
            o["add"] = lambda z, f, cwd: Cmd([izip, "-q", z, f], cwd=cwd)
            o["delete"] = lambda z, name: Cmd([izip, "-q", "-d", z, name])
            o["update"] = lambda z, f, cwd: Cmd([izip, "-q", "-r", "-u", z, f], cwd=cwd)
    elif t.key == "wintar":
        o["list"] = lambda z: Cmd([exe, "-tf", z])
        # bsdtar has no test command; decoding every entry to NUL verifies the CRCs.
        o["test"] = lambda z: Cmd([exe, "-xOf", z], stdout="NUL")
        o["extract_one"] = lambda z, name, d: Cmd([exe, "-xf", z, "-C", d, name])
        o["extract_pw"] = lambda z, d, pw: Cmd([exe, "--passphrase", pw, "-xf", z, "-C", d])
    return o


# ---------------------------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------------------------

def bench_argv(cmd: Cmd, timeout=900, cpus=None, poll=False, watch=None, temp_dir=None, size_dir=None,
               interrupt_ms=None, mem_limit=None) -> list:
    argv = [str(BENCHRUN), "--timeout", str(timeout)]
    if cmd.cwd:
        argv += ["--cwd", str(cmd.cwd)]
    if cmd.stdout:
        argv += ["--stdout", str(cmd.stdout)]
    if cmd.console:
        argv += ["--console"]
    if cpus:
        argv += ["--cpus", str(cpus)]
    if poll:
        argv += ["--poll"]
    if watch:
        argv += ["--watch", str(watch)]
    if temp_dir:
        argv += ["--temp-dir", str(temp_dir)]
    if size_dir:
        argv += ["--size-dir", str(size_dir)]
    if interrupt_ms is not None:
        argv += ["--interrupt-ms", str(interrupt_ms)]
    if mem_limit:
        argv += ["--mem-limit", str(mem_limit)]
    return argv + ["--", *map(str, cmd.argv)]


def measure(cmd: Cmd, **kw) -> dict:
    r = subprocess.run(bench_argv(cmd, **kw), capture_output=True, text=True, errors="replace")
    return parse_measurement(r.stdout, r.stderr)


def parse_measurement(stdout: str, stderr: str) -> dict:
    try:
        m = json.loads(stdout.strip().splitlines()[-1])
    except Exception:
        return {"exit": -1, "error": (stdout + stderr)[-300:]}
    m["cpu_s"] = m.get("user_s", 0) + m.get("kernel_s", 0)
    return m


def ok(m) -> bool:
    return m.get("exit") == 0 and not m.get("timed_out")


class Ctx:
    def __init__(self, args, tools, reference_tools=None):
        self.args = args
        self.tools = tools
        self.work = Path(args.work) / "ext"
        self.data = self.work / "data"
        self.scratch = self.work / "scratch"
        self.corpora = Path(args.work) / "corpora"
        self.archives = Path(args.work) / "archives"
        self.tars = Path(args.work) / "tars"
        self.rows = []
        self.notes = {}
        self.run_dir = args.run_dir
        # 7-Zip checks archives and builds reference inputs even when it is not one of the tools
        # being measured (e.g. a blazezip-only run).
        self.sevenzip = next((t for t in (reference_tools or tools) if t.key == "7zip" and t.available), None)

    # ----- bookkeeping -----
    def row(self, test, metric, tool, value, unit="", better="lower", note="", ok_=True):
        r = {"test": test, "metric": metric, "tool": tool, "value": value, "unit": unit, "better": better,
             "note": note, "ok": ok_ and value is not None}
        self.rows.append(r)
        v = "n/a" if value is None else (f"{value:.4g}" if isinstance(value, float) else value)
        log(f"  {test:<5} {metric:<34} {tool:<10} {v} {unit} {note}")
        self.save()

    def na(self, test, metric, tool, why):
        self.rows.append({"test": test, "metric": metric, "tool": tool, "value": None, "unit": "",
                          "better": "lower", "note": why, "ok": False, "na": True})

    def save(self):
        self.run_dir.mkdir(parents=True, exist_ok=True)
        out = self.run_dir / "extended.json"
        tmp = out.with_suffix(".tmp")
        tmp.write_text(json.dumps({"meta": self.meta, "rows": self.rows, "unmeasurable": UNMEASURABLE}, indent=1))
        tmp.replace(out)

    # ----- helpers -----
    def zip_tools(self):
        return [t for t in self.tools if t.available and t.zip_create and t.zip_extract]

    def create_cmd(self, t, src: Path, out: Path, preset="default"):
        preset = preset if preset in t.zip_create_presets else "default"
        return t.zip_create(preset, str(out), str(src.parent), src.name)

    def timed(self, mk, reset=None, verify=None, reps=None, **kw):
        """Run `mk()` reps times (after `reset()`), return the median of the fastest keep-best runs."""
        reps = reps or self.args.reps
        runs = []
        for _ in range(reps):
            if reset:
                reset()
            m = measure(mk(), **kw)
            if not ok(m):
                return None, m.get("error") or f"exit code {m.get('exit')}" + (" (timed out)" if m.get("timed_out") else "")
            runs.append(m)
        if verify:
            err = verify()
            if err:
                return None, err
        runs.sort(key=lambda m: m["wall_s"])
        best = runs[: max(1, min(self.args.keep_best, len(runs)))]
        out = {}
        for k in runs[0]:
            vals = [m[k] for m in best if isinstance(m.get(k), (int, float)) and not isinstance(m.get(k), bool)]
            if vals:
                out[k] = statistics.median(vals)
        return out, ""

    def seven_t(self, z: Path) -> str:
        if not self.sevenzip:
            return ""
        r = subprocess.run([self.sevenzip.exe, "t", str(z), "-bso0", "-bsp0"], capture_output=True, text=True)
        return "" if r.returncode == 0 else "archive failed 7z t"

    def made(self, t, src: Path, name: str, preset="default") -> Path | None:
        """Archive of `src` made by tool `t` (cached for the run)."""
        out = self.scratch / "made" / f"{t.key}-{name}.zip"
        if out.exists():
            return out
        out.parent.mkdir(parents=True, exist_ok=True)
        m = measure(self.create_cmd(t, src, out, preset), timeout=self.args.timeout)
        return out if ok(m) and out.exists() else None


# ---------------------------------------------------------------------------------------------
# Test data
# ---------------------------------------------------------------------------------------------

def text_block(rng: random.Random, n: int) -> bytes:
    words = [b"alpha", b"beta", b"gamma", b"delta", b"archive", b"zip", b"deflate", b"window", b"entry",
             b"header", b"stream", b"block", b"thread", b"buffer", b"file", b"data", b"\n"]
    out = bytearray()
    while len(out) < n:
        out += rng.choice(words) + b" "
    return bytes(out[:n])


def gen_tiny(d: Path, n=100_000, size=1024):
    rng = random.Random(1)
    for i in range(n):
        sub = d / f"d{i // 1000:03d}"
        if i % 1000 == 0:
            sub.mkdir(parents=True, exist_ok=True)
        (sub / f"f{i:06d}.txt").write_bytes(text_block(rng, size))


def gen_empty(d: Path, n: int, per_dir=1000):
    for i in range(n):
        sub = d / f"d{i // per_dir:04d}"
        if i % per_dir == 0:
            sub.mkdir(parents=True, exist_ok=True)
        open(sub / f"e{i:07d}", "wb").close()


def gen_deep(d: Path):
    """A tree whose paths exceed MAX_PATH (260 characters)."""
    p = d
    for i in range(14):
        p = p / f"level{i:02d}_{'x' * 16}"
    os.makedirs(long(p), exist_ok=True)
    rng = random.Random(2)
    for i in range(20):
        with open(long(p / f"deep_file_{i:02d}.txt"), "wb") as f:
            f.write(text_block(rng, 4096))


UNICODE_NAMES = [
    "café.txt", "naïve résumé.txt", "Ελληνικά.txt", "Русский.txt", "日本語のファイル.txt", "中文文件.txt",
    "한국어.txt", "עברית.txt", "العربية.txt", "हिन्दी.txt", "ไทย.txt", "emoji_😀🎉.txt", "Ünïcödé Ñame.txt",
    "math_∑∫√.txt", "quotes «»„“.txt", "ẞtraße.txt", "Tiếng Việt.txt", "Ⅻ roman.txt", "mixed_日本_Ελ_😀.txt",
]


def gen_unicode(d: Path):
    rng = random.Random(3)
    for i, n in enumerate(UNICODE_NAMES):
        sub = d / ("子目录" if i % 2 else "répertoire")
        sub.mkdir(parents=True, exist_ok=True)
        (sub / n).write_bytes(text_block(rng, 2048))


def gen_sparse(d: Path):
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "zeros.bin", "wb") as f:
        chunk = bytes(1 << 20)
        for _ in range(1024):
            f.write(chunk)
    sp = d / "sparse.bin"
    open(sp, "wb").close()
    subprocess.run(["fsutil", "sparse", "setflag", str(sp)], capture_output=True)
    with open(sp, "r+b") as f:
        f.truncate(1 << 30)


def gen_mixed(d: Path, corpora: Path):
    """A realistic home-folder mix (documents, code, binaries, media), hard-linked from the corpora."""
    picks = [("canterbury", None), ("silesia", None), ("binaries", 12), ("media", 6)]
    for name, limit in picks:
        src = corpora / name
        files = sorted(p for p in src.rglob("*") if p.is_file())
        if limit:
            files = files[:limit]
        for f in files:
            dst = d / name / f.relative_to(src)
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.link(f, dst)
    code = sorted(p for p in (corpora / "linux").rglob("*") if p.is_file())[:3000]
    for f in code:
        dst = d / "code" / f.relative_to(corpora / "linux")
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.link(f, dst)


def gen_tree1000(d: Path, corpora: Path):
    files = sorted(p for p in (corpora / "linux").rglob("*") if p.is_file())[:1000]
    for f in files:
        dst = d / f.relative_to(corpora / "linux")
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.link(f, dst)


def gen_huge(f: Path, tar: Path, target=int(4.6 * 2**30)):
    f.parent.mkdir(parents=True, exist_ok=True)
    data = tar.read_bytes()
    with open(f, "wb") as out:
        n = 0
        while n < target:
            out.write(data)
            n += len(data)


def set_times(p: Path, mtime: float):
    os.utime(p, (mtime, mtime))


FILE_ATTRIBUTE_READONLY, FILE_ATTRIBUTE_HIDDEN = 0x1, 0x2
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.GetFileAttributesW.argtypes = [wintypes.LPCWSTR]
_k32.GetFileAttributesW.restype = wintypes.DWORD
_k32.SetFileAttributesW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
_k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                             wintypes.DWORD, wintypes.HANDLE]
_k32.CreateFileW.restype = wintypes.HANDLE
_k32.CloseHandle.argtypes = [wintypes.HANDLE]


def attrs(p: Path) -> int:
    return _k32.GetFileAttributesW(str(p))


META_MTIME = 1_704_103_201.5  # 2024-01-01 10:00:01.5 UTC: odd second and a fraction


def gen_meta(d: Path):
    d.mkdir(parents=True, exist_ok=True)
    rng = random.Random(4)
    (d / "timed.txt").write_bytes(text_block(rng, 1000))
    set_times(d / "timed.txt", META_MTIME)
    (d / "readonly.txt").write_bytes(b"read only\n")
    _k32.SetFileAttributesW(str(d / "readonly.txt"), FILE_ATTRIBUTE_READONLY)
    (d / "hidden.txt").write_bytes(b"hidden\n")
    _k32.SetFileAttributesW(str(d / "hidden.txt"), FILE_ATTRIBUTE_HIDDEN)
    (d / "emptydir").mkdir(exist_ok=True)
    (d / "sub").mkdir(exist_ok=True)
    (d / "sub" / "x.txt").write_bytes(b"x\n")


def ensure(ctx: Ctx, name: str, gen) -> Path:
    p = ctx.data / name
    stamp = ctx.data / f".{name}.done"
    if stamp.exists():
        return p
    rmtree(p)
    log(f"  generating {name} ...")
    p.mkdir(parents=True, exist_ok=True)
    gen(p)
    warm_files(p)
    stamp.write_text("ok")
    return p


def warm_files(d: Path) -> int:
    """Read every file under d once, untimed. The first read of a just-written file can cost
    milliseconds (on-access antivirus scanning, for one); without this, whichever tool is measured
    first on freshly generated data pays it for everyone. Parallel, since the cost is latency."""
    from concurrent.futures import ThreadPoolExecutor

    def read(path):
        try:
            with open(path, "rb") as f:
                while f.read(1 << 20):
                    pass
        except OSError:
            pass  # unreadable on purpose (metadata tests): the tools meet it cold anyway

    paths = [os.path.join(root, f) for root, _, files in os.walk(long(d)) for f in files]
    with ThreadPoolExecutor(32) as ex:
        list(ex.map(read, paths, chunksize=256))
    return len(paths)


def drop(ctx: Ctx, name: str):
    rmtree(ctx.data / name)
    (ctx.data / f".{name}.done").unlink(missing_ok=True)


def tree_files(d: Path) -> dict:
    """Relative path -> size for every file under d (extended-length aware)."""
    out = {}
    base = long(d)
    for root, _, files in os.walk(base):
        for f in files:
            full = os.path.join(root, f)
            out[os.path.relpath(full, base).replace("\\", "/")] = os.path.getsize(full)
    return out


def find_root(dest: Path, name: str) -> Path:
    """Extracted tree root: dest/name for most tools."""
    p = dest / name
    return p if p.exists() else dest


def same_tree(src: Path, dest_root: Path) -> tuple:
    """(files matching by content, total source files)."""
    import hashlib
    a = tree_files(src)
    good = 0
    for rel in a:
        native = rel.replace("/", "\\")  # extended-length paths are not normalised
        s, d = os.path.join(long(src), native), os.path.join(long(dest_root), native)
        try:
            if os.path.getsize(d) == a[rel]:
                with open(s, "rb") as f1, open(d, "rb") as f2:
                    if hashlib.blake2b(f1.read()).digest() == hashlib.blake2b(f2.read()).digest():
                        good += 1
        except OSError:
            pass
    return good, len(a)


# ---------------------------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------------------------

UNMEASURABLE = {
    "M01": ("Cold-cache create", "needs the file cache purged between runs (administrator rights)"),
    "M02": ("Cold-cache extract", "needs the file cache purged between runs (administrator rights)"),
    "M07": ("Slow target (USB / network share)", "no USB drive or network share attached"),
    "M08": ("HDD throughput", "the machine has a single NVMe SSD"),
    "M10": ("Power-saver profile", "would change the system-wide power plan while other work is running"),
    "M28": ("Energy (joules)", "per-process energy counters need a driver (RAPL/EMI) not installed here"),
    "M47": ("Disk full mid-write", "needs a small dedicated volume (creating a VHD needs administrator rights)"),
    "M76": ("Network share (SMB)", "creating a share, even on localhost, needs administrator rights"),
    "M91": ("First run after a reboot", "needs a reboot (or a file-cache purge, which needs administrator rights) "
                                        "between runs"),
}


def t_single_thread(ctx: Ctx):
    """M03/M04: create and extract restricted to one CPU (affinity)."""
    src = ctx.corpora / "silesia"
    ref = ctx.archives / "silesia" / "_reference.zip"
    for t in ctx.zip_tools():
        out = ctx.scratch / f"{t.key}.zip"
        res, err = ctx.timed(lambda: ctx.create_cmd(t, src, out), lambda: rmtree(out), lambda: ctx.seven_t(out),
                             cpus=1, timeout=ctx.args.timeout)
        ctx.row("M03", "1-CPU create silesia: wall", t.key, res and res["wall_s"], "s", note=err)
        rmtree(out)
        dest = ctx.scratch / f"x-{t.key}"
        res, err = ctx.timed(lambda: t.zip_extract(str(ref), str(dest)),
                             lambda: (rmtree(dest), dest.mkdir(parents=True)), cpus=1, timeout=ctx.args.timeout)
        ctx.row("M04", "1-CPU extract silesia: wall", t.key, res and res["wall_s"], "s", note=err)
        rmtree(dest)


def t_scaling(ctx: Ctx):
    """M05: wall time at 1/2/4/8/all CPUs; speed-up of all CPUs over one."""
    src = ctx.corpora / "silesia"
    counts = sorted({1, 2, 4, 8, LOGICAL})
    for t in ctx.zip_tools():
        out = ctx.scratch / f"{t.key}.zip"
        walls = {}
        for n in counts:
            res, err = ctx.timed(lambda: ctx.create_cmd(t, src, out), lambda: rmtree(out), cpus=n,
                                 timeout=ctx.args.timeout, reps=min(ctx.args.reps, 3))
            walls[n] = res and res["wall_s"]
            ctx.row("M05", f"create silesia at {n} CPUs: wall", t.key, walls[n], "s", note=err)
        rmtree(out)
        sp = walls[1] / walls[LOGICAL] if walls.get(1) and walls.get(LOGICAL) else None
        ctx.row("M05", f"speed-up {LOGICAL} CPUs vs 1", t.key, sp, "x", better="higher")


def t_ttfb(ctx: Ctx):
    """M06: time until the archive file first holds data."""
    src = ctx.corpora / "silesia"
    for t in ctx.zip_tools():
        out = ctx.scratch / f"{t.key}.zip"
        res, err = ctx.timed(lambda: ctx.create_cmd(t, src, out), lambda: rmtree(out), watch=out,
                             timeout=ctx.args.timeout)
        v = res.get("first_output_s") if res else None
        ctx.row("M06", "time to first output byte", t.key, v, "s", note=err or ("" if v else "no output seen"))
        rmtree(out)


def t_contention(ctx: Ctx):
    """M09: create while every logical CPU is busy with another process."""
    src = ctx.corpora / "silesia"
    burners = [subprocess.Popen([sys.executable, "-c", "while True: pass"]) for _ in range(LOGICAL)]
    try:
        time.sleep(1)
        for t in ctx.zip_tools():
            out = ctx.scratch / f"{t.key}.zip"
            res, err = ctx.timed(lambda: ctx.create_cmd(t, src, out), lambda: rmtree(out), timeout=ctx.args.timeout,
                                 reps=min(ctx.args.reps, 3))
            ctx.row("M09", "create silesia under full CPU load: wall", t.key, res and res["wall_s"], "s", note=err)
            rmtree(out)
    finally:
        for b in burners:
            b.kill()


def create_extract(ctx: Ctx, test, label, src: Path, reps=None, timeout=None, ratio=False, extract=True,
                   check=False):
    """Create with every tool, then extract the tool's own archive; record wall/cpu/memory."""
    for t in ctx.zip_tools():
        out = ctx.scratch / f"{t.key}.zip"
        res, err = ctx.timed(lambda: ctx.create_cmd(t, src, out), lambda: rmtree(out), lambda: ctx.seven_t(out),
                             reps=reps, timeout=timeout or ctx.args.timeout)
        ctx.row(test, f"{label} create: wall", t.key, res and res["wall_s"], "s", note=err)
        ctx.row(test, f"{label} create: CPU", t.key, res and res["cpu_s"], "s", note=err)
        ctx.row(test, f"{label} create: peak memory", t.key, res and res["peak_job_commit"] / 2**20, "MiB", note=err)
        if ratio:
            insize = sum(tree_files(src).values())
            ctx.row(test, f"{label} create: size", t.key, res and out.stat().st_size / insize * 100, "%", note=err)
        if res and extract:
            dest = ctx.scratch / f"x-{t.key}"
            res2, err2 = ctx.timed(lambda: t.zip_extract(str(out), str(dest)),
                                   lambda: (rmtree(dest), dest.mkdir(parents=True)), reps=reps,
                                   timeout=timeout or ctx.args.timeout)
            if res2 and check:
                good, total = same_tree(src, find_root(dest, src.name))
                if good != total:
                    res2, err2 = None, f"{total - good} of {total} files wrong or missing"
            ctx.row(test, f"{label} extract own: wall", t.key, res2 and res2["wall_s"], "s", note=err2)
            ctx.row(test, f"{label} extract own: CPU", t.key, res2 and res2["cpu_s"], "s", note=err2)
            rmtree(dest)
        rmtree(out)


def t_tiny(ctx: Ctx):
    """M11: 100,000 files of 1 KiB."""
    src = ensure(ctx, "tiny", gen_tiny)
    create_extract(ctx, "M11", "100k x 1 KiB", src, reps=min(ctx.args.reps, 3))


def t_huge(ctx: Ctx):
    """M12 + M40b: one 4.6 GiB file (Zip64 sizes)."""
    d = ctx.data / "huge"
    f = d / "huge.bin"
    if not f.exists():
        log("  generating huge (4.6 GiB) ...")
        gen_huge(f, ctx.tars / "silesia.tar")
    for t in ctx.zip_tools():
        out = ctx.scratch / f"{t.key}.zip"
        res, err = ctx.timed(lambda: ctx.create_cmd(t, d, out), lambda: rmtree(out), lambda: ctx.seven_t(out),
                             reps=1, timeout=3600)
        ctx.row("M12", "4.6 GiB file create: wall", t.key, res and res["wall_s"], "s", note=err)
        ctx.row("M12", "4.6 GiB file create: peak memory", t.key, res and res["peak_job_commit"] / 2**20, "MiB", note=err)
        ctx.row("M40", "Zip64: >4 GiB entry passes 7z t", t.key, 1 if res else 0, "", better="higher", note=err)
        if res:
            dest = ctx.scratch / f"x-{t.key}"
            res2, err2 = ctx.timed(lambda: t.zip_extract(str(out), str(dest)),
                                   lambda: (rmtree(dest), dest.mkdir(parents=True)), reps=1, timeout=3600)
            if res2:
                got = find_root(dest, "huge") / "huge.bin"
                if not got.exists() or got.stat().st_size != f.stat().st_size:
                    res2, err2 = None, "extracted file missing or wrong size"
            ctx.row("M12", "4.6 GiB file extract own: wall", t.key, res2 and res2["wall_s"], "s", note=err2)
            rmtree(dest)
        rmtree(out)
    rmtree(d)


def roundtrip_names(ctx: Ctx, test, label, src: Path):
    """Create + extract own; score 1 if every file comes back with the same name and content."""
    for t in ctx.zip_tools():
        out = ctx.scratch / f"{t.key}.zip"
        dest = ctx.scratch / f"x-{t.key}"
        rmtree(out), rmtree(dest)
        dest.mkdir(parents=True)
        m1 = measure(ctx.create_cmd(t, src, out), timeout=ctx.args.timeout)
        note, score, wall = "", 0, None
        if not ok(m1) or not out.exists():
            note = f"create failed (exit {m1.get('exit')})"
        else:
            m2 = measure(t.zip_extract(str(out), str(dest)), timeout=ctx.args.timeout)
            good, total = same_tree(src, find_root(dest, src.name))
            score = 1 if good == total and total else 0
            wall = m1["wall_s"] + m2.get("wall_s", 0)
            note = "" if score else f"{total - good} of {total} files wrong or missing" + (
                "" if ok(m2) else f"; extract exit {m2.get('exit')}")
        ctx.row(test, f"{label}: round-trip intact", t.key, score, "", better="higher", note=note)
        rmtree(out), rmtree(dest)


def t_deep(ctx: Ctx):
    """M13: paths longer than 260 characters."""
    roundtrip_names(ctx, "M13", "paths > 260 chars", ensure(ctx, "deep", gen_deep))


def t_unicode(ctx: Ctx):
    """M14 + M41: non-ASCII names, and whether the archive marks them as UTF-8."""
    src = ensure(ctx, "unicode", gen_unicode)
    roundtrip_names(ctx, "M14", "Unicode names", src)
    for t in ctx.zip_tools():
        out = ctx.scratch / f"{t.key}-u.zip"
        rmtree(out)
        m = measure(ctx.create_cmd(t, src, out), timeout=ctx.args.timeout)
        if not ok(m) or not out.exists():
            ctx.row("M41", "UTF-8 names flagged", t.key, 0, "", better="higher", note="create failed")
            continue
        flagged = total = 0
        with zipfile.ZipFile(out) as z:
            for i in z.infolist():
                if any(ord(c) > 127 for c in i.filename) or not i.filename.isascii():
                    total += 1
                    has_up = b"\x75\x70" in i.extra  # Info-ZIP Unicode Path extra field (0x7075)
                    if i.flag_bits & 0x800 or has_up:
                        flagged += 1
        ctx.row("M41", "UTF-8 names flagged", t.key, flagged / total if total else 0, "", better="higher",
                note=f"{flagged}/{total} non-ASCII entries")
        rmtree(out)


def t_sparse(ctx: Ctx):
    """M15: 1 GiB of zeros plus a 1 GiB sparse file."""
    src = ensure(ctx, "sparse", gen_sparse)
    create_extract(ctx, "M15", "zeros + sparse 2 GiB", src, reps=min(ctx.args.reps, 3), ratio=True)


def t_mixed(ctx: Ctx):
    """M16: mixed documents / code / binaries / media."""
    src = ensure(ctx, "mixed", lambda d: gen_mixed(d, ctx.corpora))
    create_extract(ctx, "M16", "mixed tree", src, ratio=True)


def t_ops(ctx: Ctx):
    """M18-M22: add, delete, extract one, list, test."""
    sil = ctx.corpora / "silesia"
    linux_ref = ctx.archives / "linux" / "_reference.zip"
    sil_ref = ctx.archives / "silesia" / "_reference.zip"
    tiny = ensure(ctx, "tiny", gen_tiny)
    tiny_ref = ctx.scratch / "made" / "tiny-reference.zip"
    if not tiny_ref.exists() and ctx.sevenzip:
        tiny_ref.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([ctx.sevenzip.exe, "a", "-tzip", "-mx1", str(tiny_ref), "tiny", "-bso0", "-bsp0"],
                       cwd=tiny.parent, check=True)
    extra = ctx.scratch / "addme"
    extra.mkdir(parents=True, exist_ok=True)
    (extra / "added.txt").write_bytes(text_block(random.Random(5), 1 << 20))
    for t in [t for t in ctx.tools if t.available and t.zip_create]:
        o = ops_for(t)
        own = ctx.made(t, sil, "silesia")
        work = ctx.scratch / f"{t.key}-work.zip"
        for test, metric, key in (("M18", "add 1 MiB file to silesia archive: wall", "add"),
                                  ("M19", "delete 1 entry from silesia archive: wall", "delete")):
            if key not in o:
                ctx.na(test, metric, t.key, "no CLI command")
                continue
            if not own:
                ctx.row(test, metric, t.key, None, "s", note="could not create the archive")
                continue
            mk = (lambda: o["add"](str(work), "added.txt", str(extra))) if key == "add" else \
                (lambda: o["delete"](str(work), "silesia/dickens"))
            res, err = ctx.timed(mk, lambda: shutil.copyfile(own, work), timeout=ctx.args.timeout)
            if res:
                with zipfile.ZipFile(work) as z:
                    names = z.namelist()
                if key == "add" and not any(n.endswith("added.txt") for n in names):
                    res, err = None, "entry was not added"
                if key == "delete" and "silesia/dickens" in names:
                    res, err = None, "entry was not deleted"
            ctx.row(test, metric, t.key, res and res["wall_s"], "s", note=err)
            rmtree(work)
        if "extract_one" in o:
            dest = ctx.scratch / f"x1-{t.key}"
            res, err = ctx.timed(lambda: o["extract_one"](str(linux_ref), "linux/kernel/fork.c", str(dest)),
                                 lambda: (rmtree(dest), dest.mkdir(parents=True)), timeout=ctx.args.timeout)
            if res and not any(p.name == "fork.c" for p in dest.rglob("fork.c")):
                res, err = None, "file not extracted"
            ctx.row("M20", "extract 1 file from 23k-entry zip: wall", t.key, res and res["wall_s"], "s", note=err)
            rmtree(dest)
        else:
            ctx.na("M20", "extract 1 file from 23k-entry zip: wall", t.key, "no CLI command")
        if "list" in o and tiny_ref.exists():
            res, err = ctx.timed(lambda: o["list"](str(tiny_ref)), timeout=ctx.args.timeout)
            ctx.row("M21", "list 100k-entry zip: wall", t.key, res and res["wall_s"], "s", note=err)
        else:
            ctx.na("M21", "list 100k-entry zip: wall", t.key, "no CLI command")
        if "test" in o:
            res, err = ctx.timed(lambda: o["test"](str(sil_ref)), timeout=ctx.args.timeout)
            ctx.row("M22", "test silesia zip: wall", t.key, res and res["wall_s"], "s", note=err)
            ctx.row("M22", "test silesia zip: CPU", t.key, res and res["cpu_s"], "s", note=err)
        else:
            ctx.na("M22", "test silesia zip: wall", t.key, "no CLI command")
    rmtree(extra)


def t_resources(ctx: Ctx):
    """M23-M27, M29: resource profile of creating the silesia archive."""
    src = ctx.corpora / "silesia"
    for t in ctx.zip_tools():
        outdir = ctx.scratch / "res-out"
        out = outdir / f"{t.key}.zip"
        tmp = ctx.scratch / "res-tmp"

        def reset():
            rmtree(outdir), rmtree(tmp)
            outdir.mkdir(parents=True)
            tmp.mkdir(parents=True)

        res, err = ctx.timed(lambda: ctx.create_cmd(t, src, out), reset, poll=True, temp_dir=tmp, size_dir=outdir,
                             timeout=ctx.args.timeout, reps=min(ctx.args.reps, 3))
        g = (lambda k, s=1: res[k] / s if res and k in res else None)
        ctx.row("M23", "peak working set (all processes)", t.key, g("peak_job_ws", 2**20), "MiB", note=err)
        ctx.row("M24", "bytes read", t.key, g("read_bytes", 2**20), "MiB", note=err)
        ctx.row("M25", "bytes written", t.key, g("write_bytes", 2**20), "MiB", note=err)
        extra = None
        if res and out.exists():
            extra = max(res.get("peak_temp_bytes", 0), res.get("peak_size_dir_bytes", 0) - out.stat().st_size) / 2**20
        ctx.row("M26", "peak extra disk (temp + scratch files)", t.key, extra, "MiB", note=err)
        ctx.row("M27", "peak threads", t.key, g("peak_threads"), "", note=err)
        ctx.row("M27", "peak handles", t.key, g("peak_handles"), "", note=err)
        ctx.row("M29", "page faults", t.key, g("page_faults"), "", note=err)
        rmtree(outdir), rmtree(tmp)


def t_startup(ctx: Ctx):
    """M30: create and extract an archive of one small file (start-up overhead)."""
    src = ctx.scratch / "one"
    src.mkdir(parents=True, exist_ok=True)
    (src / "hello.txt").write_bytes(b"hello\n")
    for t in ctx.zip_tools():
        out = ctx.scratch / f"{t.key}.zip"
        res, err = ctx.timed(lambda: ctx.create_cmd(t, src, out), lambda: rmtree(out))
        ctx.row("M30", "create 1-file archive: wall", t.key, res and res["wall_s"], "s", note=err)
        if res:
            dest = ctx.scratch / f"x-{t.key}"
            res2, err2 = ctx.timed(lambda: t.zip_extract(str(out), str(dest)),
                                   lambda: (rmtree(dest), dest.mkdir(parents=True)))
            ctx.row("M30", "extract 1-file archive: wall", t.key, res2 and res2["wall_s"], "s", note=err2)
            rmtree(dest)
        rmtree(out)
    rmtree(src)


def t_neutral_decode(ctx: Ctx):
    """M34: cost for a neutral reader (7-Zip on one CPU) to decode each tool's archive."""
    if not ctx.sevenzip:
        return
    src = ctx.corpora / "silesia"
    for t in [t for t in ctx.tools if t.available and t.zip_create]:
        z = ctx.made(t, src, "silesia")
        if not z:
            ctx.row("M34", "7-Zip 1-CPU decode of its silesia zip: CPU", t.key, None, "s", note="create failed")
            continue
        res, err = ctx.timed(lambda: Cmd([ctx.sevenzip.exe, "t", str(z), "-bso0", "-bsp0"]), cpus=1)
        ctx.row("M34", "7-Zip 1-CPU decode of its silesia zip: CPU", t.key, res and res["cpu_s"], "s", note=err)


def t_overhead(ctx: Ctx):
    """M35: archive bytes per entry for 10,000 empty files."""
    src = ensure(ctx, "empty10k", lambda d: gen_empty(d, 10_000))
    for t in [t for t in ctx.tools if t.available and t.zip_create]:
        out = ctx.scratch / f"{t.key}.zip"
        rmtree(out)
        m = measure(ctx.create_cmd(t, src, out), timeout=ctx.args.timeout)
        v = out.stat().st_size / 10_000 if ok(m) and out.exists() else None
        ctx.row("M35", "bytes per entry (10k empty files)", t.key, v, "B", note="" if v else f"exit {m.get('exit')}")
        rmtree(out)


def t_determinism(ctx: Ctx):
    """M36/M37: same bytes when run twice, and when run on 1 CPU vs all."""
    src = ctx.corpora / "canterbury"
    for t in [t for t in ctx.tools if t.available and t.zip_create]:
        outs = []
        for i, cpus in enumerate((None, None, 1)):
            out = ctx.scratch / f"{t.key}-det{i}.zip"
            rmtree(out)
            m = measure(ctx.create_cmd(t, src, out), cpus=cpus, timeout=ctx.args.timeout)
            outs.append(out.read_bytes() if ok(m) and out.exists() else None)
            rmtree(out)
            time.sleep(1.1)  # a tool that stamps the current time would now differ
        if outs[0] is None:
            ctx.row("M36", "identical output on repeat", t.key, None, note="create failed")
            continue
        ctx.row("M36", "identical output on repeat", t.key, int(outs[0] == outs[1]), "", better="higher")
        ctx.row("M37", "identical output on 1 CPU vs all", t.key, int(outs[0] == outs[2]), "", better="higher")


def readers(ctx: Ctx) -> dict:
    """Independent ZIP readers: name -> function(zip) -> error string ('' when it reads cleanly)."""
    r = {}
    if ctx.sevenzip:
        r["7-Zip"] = lambda z: "" if subprocess.run([ctx.sevenzip.exe, "t", z, "-bso0", "-bsp0"],
                                                     capture_output=True).returncode == 0 else "7z t failed"
    unzip = toolsmod.first("unzip")
    if unzip:
        r["Info-ZIP unzip"] = lambda z: "" if subprocess.run([unzip, "-tqq", z], capture_output=True).returncode == 0 \
            else "unzip -t failed"

    def py(z):
        try:
            with zipfile.ZipFile(z) as f:
                bad = f.testzip()
                return f"bad entry {bad}" if bad else ""
        except Exception as e:
            return str(e)[:80]
    r["Python zipfile"] = py
    pwsh = toolsmod.first("pwsh") or toolsmod.first("powershell")
    if pwsh:
        def net(z):
            lit = z.replace("'", "''")
            script = ("$ErrorActionPreference='Stop'; Add-Type -AssemblyName System.IO.Compression.FileSystem;"
                      f"$z=[IO.Compression.ZipFile]::OpenRead('{lit}');"
                      "foreach($e in $z.Entries){ if(-not $e.FullName.EndsWith('/')){"
                      "$s=$e.Open(); $s.CopyTo([IO.Stream]::Null); $s.Dispose() } }; $z.Dispose()")
            r_ = subprocess.run([pwsh, "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True)
            return "" if r_.returncode == 0 else ".NET read failed: " + (r_.stderr.strip().splitlines() or [""])[0][:60]
        r[".NET ZipFile"] = net
    wtar = toolsmod.first(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "tar.exe")
    if wtar:
        r["Windows tar"] = lambda z: "" if subprocess.run([wtar, "-xOf", z], stdout=subprocess.DEVNULL,
                                                          stderr=subprocess.DEVNULL).returncode == 0 else "tar failed"
    return r


def t_compat(ctx: Ctx):
    """M38: how many independent readers read each tool's archives (silesia + Unicode tree)."""
    rd = readers(ctx)
    srcs = [ctx.corpora / "silesia", ensure(ctx, "unicode", gen_unicode)]
    for t in [t for t in ctx.tools if t.available and t.zip_create]:
        passed, total, fails = 0, 0, []
        for src in srcs:
            z = ctx.made(t, src, src.name)
            for name, f in rd.items():
                total += 1
                err = f(str(z)) if z else "create failed"
                if err:
                    fails.append(f"{name} ({src.name})")
                else:
                    passed += 1
        ctx.row("M38", f"readers passing (of {total})", t.key, passed, "", better="higher",
                note="fails: " + ", ".join(fails) if fails else "")


def t_metadata(ctx: Ctx):
    """M39: metadata surviving create + extract with the same tool."""
    src = ensure(ctx, "meta", gen_meta)
    for t in ctx.zip_tools():
        out = ctx.scratch / f"{t.key}.zip"
        dest = ctx.scratch / f"x-{t.key}"
        rmtree(out), rmtree(dest)
        dest.mkdir(parents=True)
        m = measure(ctx.create_cmd(t, src, out), timeout=ctx.args.timeout)
        checks = {}
        if ok(m) and out.exists():
            measure(t.zip_extract(str(out), str(dest)), timeout=ctx.args.timeout)
            root = find_root(dest, src.name)
            f = root / "timed.txt"
            mt = f.stat().st_mtime if f.exists() else None
            checks["mtime within 2 s"] = mt is not None and abs(mt - META_MTIME) <= 2
            checks["mtime within 1 s"] = mt is not None and abs(mt - META_MTIME) <= 1
            checks["mtime exact (1 ms)"] = mt is not None and abs(mt - META_MTIME) <= 0.001
            ro, hid = root / "readonly.txt", root / "hidden.txt"
            checks["read-only kept"] = ro.exists() and bool(attrs(ro) & FILE_ATTRIBUTE_READONLY)
            checks["hidden kept"] = hid.exists() and bool(attrs(hid) & FILE_ATTRIBUTE_HIDDEN)
            checks["empty dir kept"] = (root / "emptydir").is_dir()
        score = sum(checks.values())
        missing = [k for k, v in checks.items() if not v]
        ctx.row("M39", "metadata checks passed (of 6)", t.key, score, "", better="higher",
                note=("lost: " + ", ".join(missing)) if missing else ("" if checks else "create failed"))
        rmtree(out), rmtree(dest)
    log("  (symlinks not tested: creating them needs Developer Mode or administrator rights)")


def t_zip64_count(ctx: Ctx):
    """M40: more than 65,535 entries."""
    src = ensure(ctx, "many70k", lambda d: gen_empty(d, 70_000))
    for t in [t for t in ctx.tools if t.available and t.zip_create]:
        out = ctx.scratch / f"{t.key}.zip"
        rmtree(out)
        m = measure(ctx.create_cmd(t, src, out), timeout=ctx.args.timeout)
        note = ""
        score = 0
        if ok(m) and out.exists():
            err = ctx.seven_t(out)
            try:
                with zipfile.ZipFile(out) as z:
                    n = sum(1 for i in z.infolist() if not i.is_dir())
            except Exception as e:
                n, err = 0, err or str(e)[:60]
            score = int(not err and n == 70_000)
            note = err or ("" if score else f"{n} file entries")
        else:
            note = f"create exit {m.get('exit')}"
        ctx.row("M40", "Zip64: 70k entries valid", t.key, score, "", better="higher", note=note)
        rmtree(out)


def lock_file(p: Path):
    GENERIC_READ, OPEN_EXISTING = 0x80000000, 3
    h = _k32.CreateFileW(str(p), GENERIC_READ, 0, None, OPEN_EXISTING, 0, None)
    return h


def t_locked(ctx: Ctx):
    """M42: one input file is locked (opened exclusively by another process) during create."""
    src = ctx.scratch / "lockedtree"
    rmtree(src)
    src.mkdir(parents=True)
    rng = random.Random(6)
    for i in range(10):
        (src / f"f{i}.txt").write_bytes(text_block(rng, 10_000))
    h = lock_file(src / "f5.txt")
    try:
        for t in [t for t in ctx.tools if t.available and t.zip_create]:
            out = ctx.scratch / f"{t.key}.zip"
            rmtree(out)
            m = measure(ctx.create_cmd(t, src, out), timeout=120)
            names = []
            if out.exists():
                try:
                    with zipfile.ZipFile(out) as z:
                        names = [n for n in z.namelist() if not n.endswith("/")]
                except Exception:
                    pass
            others = sum(1 for n in names if not n.endswith("f5.txt"))
            if m.get("timed_out"):
                score, note = 0, "hung"
            elif others == 9 and m.get("exit") != 0:
                score, note = 2, "archived the rest and reported the error"
            elif m.get("exit") != 0:
                score, note = 1, "stopped with an error"
            elif others == 9 and not any(n.endswith("f5.txt") for n in names):
                score, note = 0, "silently skipped the locked file (exit 0)"
            else:
                score, note = 0, f"exit 0 with {len(names)} entries"
            ctx.row("M42", "locked input handling (0-2)", t.key, score, "", better="higher", note=note)
            rmtree(out)
    finally:
        _k32.CloseHandle(h)
        rmtree(src)


def data_ranges(z: Path):
    out = []
    with open(z, "rb") as f, zipfile.ZipFile(z) as zf:
        for i in zf.infolist():
            if i.compress_size < 16:
                continue
            f.seek(i.header_offset + 26)
            n, e = int.from_bytes(f.read(2), "little"), int.from_bytes(f.read(2), "little")
            start = i.header_offset + 30 + n + e
            out.append((start, i.compress_size))
    return out


def t_corruption(ctx: Ctx, trials=20):
    """M43: share of single-byte corruptions in entry data that the tool's own test detects."""
    src = ctx.corpora / "canterbury"
    for t in [t for t in ctx.tools if t.available and t.zip_create]:
        o = ops_for(t)
        z = ctx.made(t, src, "canterbury")
        if not z:
            ctx.row("M43", "corruptions detected", t.key, None, note="create failed")
            continue
        ranges = data_ranges(z)
        rng = random.Random(7)
        good = z.read_bytes()
        bad = ctx.scratch / f"{t.key}-bad.zip"
        detected = hung = 0
        for _ in range(trials):
            start, n = rng.choice(ranges)
            pos = start + rng.randrange(n)
            b = bytearray(good)
            b[pos] ^= 1 << rng.randrange(8)
            bad.write_bytes(b)
            if "test" in o:
                m = measure(o["test"](str(bad)), timeout=120)
            else:
                dest = ctx.scratch / f"xc-{t.key}"
                rmtree(dest)
                dest.mkdir(parents=True)
                m = measure(t.zip_extract(str(bad), str(dest)), timeout=120)
                rmtree(dest)
            detected += em.corruption_detected(m)
            hung += bool(m.get("timed_out"))
        ctx.row("M43", "corruptions detected", t.key, detected / trials * 100, "%", better="higher",
                note=("via test" if "test" in o else "via extract") + (f"; hung on {hung}" if hung else ""))
        rmtree(bad)


def t_truncated(ctx: Ctx):
    """M44: files recovered from an archive truncated to half its size."""
    src = ensure(ctx, "tree1000", lambda d: gen_tree1000(d, ctx.corpora))
    for t in ctx.zip_tools():
        z = ctx.made(t, src, "tree1000")
        if not z:
            ctx.row("M44", "files recovered from half an archive", t.key, None, note="create failed")
            continue
        cut = ctx.scratch / f"{t.key}-cut.zip"
        data = z.read_bytes()
        cut.write_bytes(data[: len(data) // 2])
        dest = ctx.scratch / f"xt-{t.key}"
        rmtree(dest)
        dest.mkdir(parents=True)
        m = measure(t.zip_extract(str(cut), str(dest)), timeout=300)
        good, total = same_tree(src, find_root(dest, "tree1000"))
        ctx.row("M44", "files recovered from half an archive", t.key, good / total * 100, "%", better="higher",
                note=f"{good}/{total}; exit {m.get('exit')}")
        rmtree(dest), rmtree(cut)


def t_zipslip(ctx: Ctx):
    """M45: entries that try to escape the destination (../ and absolute paths)."""
    zone = ctx.scratch / "slipzone"
    rmtree(zone)
    zone.mkdir(parents=True)
    evil = ctx.scratch / "evil.zip"
    abs_target = (zone / "abs_escape.txt").as_posix()
    with zipfile.ZipFile(evil, "w") as z:
        z.writestr("ok.txt", b"fine\n")
        z.writestr("../dotdot_escape.txt", b"escaped\n")
        z.writestr("a/../../nested_escape.txt", b"escaped\n")
        zi = zipfile.ZipInfo("..\\backslash_escape.txt")
        z.writestr(zi, b"escaped\n")
        z.writestr(abs_target, b"escaped\n")
    for t in ctx.zip_tools():
        dest = zone / "dest" / "inner"
        rmtree(zone / "dest")
        for p in zone.glob("*escape*"):
            rmtree(p)
        dest.mkdir(parents=True)
        measure(t.zip_extract(str(evil), str(dest)), timeout=60)
        escaped = [p.name for p in zone.rglob("*escape*") if dest not in p.parents]
        ctx.row("M45", "zip-slip entries blocked (of 4)", t.key, 4 - len(escaped), "", better="higher",
                note=("escaped: " + ", ".join(escaped)) if escaped else "")
    rmtree(zone), rmtree(evil)


def t_exit_codes(ctx: Ctx):
    """M46: non-zero exit codes for missing input, missing archive and a corrupt archive."""
    junk = ctx.scratch / "junk.zip"
    junk.write_bytes(b"PK\x03\x04" + os.urandom(4000))
    missing_src = ctx.scratch / "does-not-exist"
    for t in ctx.zip_tools():
        score, fails = 0, []
        out = ctx.scratch / f"{t.key}.zip"
        rmtree(out)
        m = measure(ctx.create_cmd(t, missing_src, out), timeout=60)
        if m.get("exit") != 0:
            score += 1
        else:
            fails.append("missing input")
        rmtree(out)
        dest = ctx.scratch / f"xe-{t.key}"
        for label, z in (("missing archive", ctx.scratch / "nope.zip"), ("corrupt archive", junk)):
            rmtree(dest)
            dest.mkdir(parents=True)
            m = measure(t.zip_extract(str(z), str(dest)), timeout=60)
            if m.get("exit") != 0:
                score += 1
            else:
                fails.append(label)
        rmtree(dest)
        ctx.row("M46", "failures reported by exit code (of 3)", t.key, score, "", better="higher",
                note=("exit 0 on: " + ", ".join(fails)) if fails else "")
    rmtree(junk)


def t_million(ctx: Ctx):
    """M48: 1,000,000 empty files."""
    src = ensure(ctx, "million", lambda d: gen_empty(d, 1_000_000))
    for t in [t for t in ctx.tools if t.available and t.zip_create]:
        out = ctx.scratch / f"{t.key}.zip"
        rmtree(out)
        m = measure(ctx.create_cmd(t, src, out), timeout=1800)
        good = ok(m) and out.exists()
        note = "" if good else ("timed out after 30 min" if m.get("timed_out") else f"exit {m.get('exit')}")
        ctx.row("M48", "1M entries create: peak memory", t.key, m["peak_job_commit"] / 2**20 if good else None,
                "MiB", note=note)
        ctx.row("M48", "1M entries create: wall", t.key, m["wall_s"] if good else None, "s", note=note)
        rmtree(out)
    million_ops(ctx, src)
    if not ctx.args.keep_data:
        drop(ctx, "million")


def million_ops(ctx: Ctx, src: Path, n=1_000_000):
    """M86-M88: list a 1,000,000-entry archive, its peak memory, and extracting its last entry."""
    if not ctx.sevenzip:
        return
    ref = ctx.scratch / "million-ref.zip"
    rmtree(ref)
    subprocess.run([ctx.sevenzip.exe, "a", "-tzip", "-mx0", str(ref), src.name, "-bso0", "-bsp0"], cwd=src.parent,
                   capture_output=True)
    if not ref.exists():
        log("  (could not make the 1M-entry reference archive)")
        return
    last = f"{src.name}/d{(n - 1) // 1000:04d}/e{n - 1:07d}"
    for t in [t for t in ctx.tools if t.available and (t.zip_create or t.zip_extract)]:
        o = ops_for(t)
        if "list" in o:
            m = measure(o["list"](str(ref)), timeout=900)
            good = ok(m)
            ctx.row("M86", "list a 1M-entry zip: wall", t.key, m["wall_s"] if good else None, "s",
                    note="" if good else exit_note(m))
            ctx.row("M88", "list a 1M-entry zip: peak memory", t.key, m["peak_job_commit"] / 2**20 if good else None,
                    "MiB", note="" if good else exit_note(m))
        else:
            ctx.na("M86", "list a 1M-entry zip: wall", t.key, "no CLI command")
        if "extract_one" in o:
            dest = ctx.scratch / f"x1m-{t.key}"
            rmtree(dest)
            dest.mkdir(parents=True)
            m = measure(o["extract_one"](str(ref), last, str(dest)), timeout=900)
            good = ok(m) and bool(files_named(dest, last.rsplit("/", 1)[1]))
            ctx.row("M87", "extract the last entry of a 1M-entry zip: wall", t.key, m["wall_s"] if good else None, "s",
                    note="" if good else ("file not extracted; " + exit_note(m)))
            rmtree(dest)
        else:
            ctx.na("M87", "extract the last entry of a 1M-entry zip: wall", t.key, "no CLI command")
    rmtree(ref)


def t_interrupt(ctx: Ctx):
    """M49: Ctrl-C half-way through: does the tool stop promptly and clean up?"""
    src = ctx.corpora / "silesia"
    for t in ctx.zip_tools():
        outdir = ctx.scratch / "int-out"
        tmp = ctx.scratch / "int-tmp"
        rmtree(outdir), rmtree(tmp)
        outdir.mkdir(parents=True)
        tmp.mkdir(parents=True)
        out = outdir / f"{t.key}.zip"
        # Use the slowest preset so every tool is still running when Ctrl-C arrives.
        full = measure(ctx.create_cmd(t, src, out, "max"), timeout=ctx.args.timeout)
        rmtree(out)
        delay = int(max(0.05, full.get("wall_s", 1) * 0.4) * 1000)
        m = measure(ctx.create_cmd(t, src, out, "max"), timeout=ctx.args.timeout, temp_dir=tmp, interrupt_ms=delay)
        stopped = m.get("exit_after_interrupt_s") is not None and m["exit_after_interrupt_s"] < 3 and m.get("exit") != 0
        leftovers = [p.name for p in outdir.iterdir()] + [p.name for p in tmp.rglob("*") if p.is_file()]
        score = int(stopped) + int(stopped and not leftovers)
        note = ("stopped in %.2fs" % m["exit_after_interrupt_s"]) if stopped else "kept running"
        if leftovers:
            note += "; left " + ", ".join(leftovers[:3])
        ctx.row("M49", "Ctrl-C: stops (1) and cleans up (2)", t.key, score, "", better="higher", note=note)
        rmtree(outdir), rmtree(tmp)


def derived(ctx: Ctx):
    """M17, M31-M33, M50: computed from the main suite's results."""
    p = ctx.args.main / "results.json" if ctx.args.main else None
    if not p or not p.exists():
        log("  (no main results given; skipping derived metrics)")
        return
    rows = [r for r in json.loads(p.read_text())["results"] if r.get("ok")]
    zc = [r for r in rows if r["category"] == "zip-create"]
    corpora = sorted({r["corpus"] for r in zc})
    tools = sorted({r["tool"] for r in zc})
    # M17: store detection on already-compressed media (size and time at default).
    for r in zc:
        if r["corpus"] == "media" and r["preset"] == "default":
            ctx.row("M17", "media (incompressible) default: wall", r["tool"], r["wall_s"], "s")
            ctx.row("M17", "media (incompressible) default: size", r["tool"], r["ratio"] * 100, "%")
    # M31/M32: Pareto efficiency. For each corpus, the fraction of other tools' preset points a tool's
    # best point dominates; M33: compression gain per CPU-second.
    for t in tools:
        dominated, comparisons, gains = 0, 0, []
        for c in corpora:
            mine = [r for r in zc if r["corpus"] == c and r["tool"] == t]
            others = [r for r in zc if r["corpus"] == c and r["tool"] != t]
            for o in others:
                comparisons += 1
                if any(m["wall_s"] <= o["wall_s"] and m["ratio"] <= o["ratio"] for m in mine):
                    dominated += 1
            for m in mine:
                if m.get("in_size") and m["cpu_s"] > 0:
                    gains.append((1 - m["ratio"]) * m["in_size"] / 2**20 / m["cpu_s"])
        if comparisons:
            ctx.row("M31", "share of rival points beaten on both size and time", t, dominated / comparisons * 100, "%",
                    better="higher")
        if gains:
            ctx.row("M33", "MiB saved per CPU-second (median)", t, statistics.median(gains), "MiB/s", better="higher")
    # M32: speed at matched ratio: wall of each tool's point closest to the 7-Zip default ratio, per corpus.
    for c in corpora:
        ref = next((r for r in zc if r["corpus"] == c and r["tool"] == "7zip" and r["preset"] == "default"), None)
        if not ref:
            continue
        for t in tools:
            pts = [r for r in zc if r["corpus"] == c and r["tool"] == t and r["ratio"] <= ref["ratio"] * 1.01]
            if pts:
                best = min(pts, key=lambda r: r["wall_s"])
                ctx.row("M32", f"{c}: fastest wall within 1% of 7-Zip default size", t, best["wall_s"], "s")
    # M50: run-to-run variability (coefficient of variation of all measured runs).
    for t in tools:
        cvs = []
        for r in rows:
            w = r.get("all_wall") or []
            if r["tool"] == t and len(w) >= 3 and statistics.mean(w) > 0:
                cvs.append(statistics.stdev(w) / statistics.mean(w) * 100)
        if cvs:
            ctx.row("M50", "run-to-run variation (median CV)", t, statistics.median(cvs), "%")


# ---------------------------------------------------------------------------------------------
# Second set (M51-M91): Windows file-system edge cases, foreign archives, safety, real-world
# conditions, the full speed/size curve, more data types, operations at scale and footprint.
# ---------------------------------------------------------------------------------------------

def extract_tools(ctx: Ctx):
    return [t for t in ctx.tools if t.available and t.zip_extract]


def create_tools(ctx: Ctx):
    return [t for t in ctx.tools if t.available and t.zip_create]


def extract_crafted(ctx: Ctx, t, data: bytes, label: str, timeout=120, prepare=None):
    """Write a hand-built archive, extract it with `t` into a fresh folder; returns (measurement, dest)."""
    z = ctx.scratch / f"{label}.zip"
    z.write_bytes(data)
    dest = ctx.scratch / f"x-{label}-{t.key}"
    rmtree(dest)
    dest.mkdir(parents=True)
    if prepare:
        prepare(dest)
    return measure(t.zip_extract(str(z), str(dest)), timeout=timeout), dest


def exit_note(m) -> str:
    return "hung (killed)" if m.get("timed_out") else f"exit {m.get('exit')}"


def files_named(root: Path, name: str):
    return [f for f in em.walk_files(root) if os.path.basename(f) == name]


RESERVED = {"aux": "aux.h", "con": "con.txt", "nul": "nul", "com1": "com1.log", "prn": "prn/inside.txt",
            "dot": "trailing.", "space": "trailing "}


def t_reserved(ctx: Ctx):
    """M51: entries named like Windows devices (aux.h, con.txt, nul ...) or ending in a dot or space."""
    markers = {k: f"MARK-{k}-reserved".encode() for k in RESERVED}
    data = em.build_zip([em.ZEntry("ok.txt", b"fine")] + [em.ZEntry(n, markers[k]) for k, n in RESERVED.items()])
    for t in extract_tools(ctx):
        m, dest = extract_crafted(ctx, t, data, "reserved")
        found = set() if m.get("timed_out") else em.find_markers(dest, markers)
        lost = [RESERVED[k] for k in RESERVED if k not in found]
        ctx.row("M51", f"reserved-name entries recovered (of {len(RESERVED)})", t.key, len(found), "", better="higher",
                note=("lost: " + ", ".join(repr(n) for n in lost) + "; " if lost else "") + exit_note(m))
        rmtree(dest)


def t_junction_loop(ctx: Ctx):
    """M52: a directory junction that points back at its own parent."""
    src = ctx.scratch / "jloop"
    rmtree(src)
    (src / "sub").mkdir(parents=True)
    (src / "a.txt").write_bytes(b"a")
    (src / "sub" / "b.txt").write_bytes(b"b")
    em.make_junction(src / "sub" / "loop", src)
    try:
        for t in create_tools(ctx):
            out = ctx.scratch / f"{t.key}-jl.zip"
            rmtree(out)
            m = measure(ctx.create_cmd(t, src, out), timeout=120)
            entries = 0
            if out.exists():
                try:
                    with zipfile.ZipFile(out) as z:
                        entries = len(z.infolist())
                except Exception:
                    pass
            score = em.score_junction_loop(bool(m.get("timed_out")), m.get("exit"), entries)
            ctx.row("M52", "junction loop handled (0-2)", t.key, score, "", better="higher",
                    note=f"{exit_note(m)}, {entries} entries")
            rmtree(out)
    finally:
        rmtree(src)


def t_links(ctx: Ctx):
    """M53: a hard-linked pair of files and a junction to a folder outside the tree."""
    base = ctx.scratch / "links"
    rmtree(base)
    src, outside = base / "linktree", base / "outside"
    src.mkdir(parents=True)
    outside.mkdir()
    payload = random.Random(8).randbytes(1 << 20)
    (src / "data.bin").write_bytes(payload)
    os.link(src / "data.bin", src / "data_link.bin")
    (outside / "t.txt").write_bytes(b"MARK-junction-target")
    em.make_junction(src / "jdir", outside)
    try:
        for t in ctx.zip_tools():
            out, dest = ctx.scratch / f"{t.key}-links.zip", ctx.scratch / f"x-links-{t.key}"
            rmtree(out), rmtree(dest)
            dest.mkdir(parents=True)
            m = measure(ctx.create_cmd(t, src, out), timeout=120)
            score, notes = 0, []
            if ok(m) and out.exists():
                measure(t.zip_extract(str(out), str(dest)), timeout=120)
                root = find_root(dest, "linktree")
                pair = all((root / n).is_file() and (root / n).read_bytes() == payload
                           for n in ("data.bin", "data_link.bin"))
                j = root / "jdir"
                target = em.is_junction(j) or os.path.islink(j) or (
                    (j / "t.txt").is_file() and (j / "t.txt").read_bytes() == b"MARK-junction-target")
                score = int(pair) + int(target)
                notes += [] if pair else ["hard-linked pair not restored"]
                notes += [] if target else ["junction contents lost"]
            else:
                notes.append(f"create {exit_note(m)}")
            ctx.row("M53", "links handled (of 2)", t.key, score, "", better="higher", note="; ".join(notes))
            rmtree(out), rmtree(dest)
    finally:
        rmtree(base)


def t_motw(ctx: Ctx):
    """M54: Mark of the Web: files extracted from a downloaded archive inherit its Zone.Identifier."""
    data = em.build_zip([em.ZEntry("readme.txt", b"hello"), em.ZEntry("run.js", b"WScript.Echo(1)"),
                         em.ZEntry("doc/report.docm", b"macro document")])
    z = ctx.scratch / "downloaded.zip"
    for t in extract_tools(ctx):
        z.write_bytes(data)
        with open(str(z) + ":Zone.Identifier", "w") as f:
            f.write("[ZoneTransfer]\r\nZoneId=3\r\nHostUrl=https://example.com/downloaded.zip\r\n")
        dest = ctx.scratch / f"x-motw-{t.key}"
        rmtree(dest)
        dest.mkdir(parents=True)
        m = measure(t.zip_extract(str(z), str(dest)), timeout=120)
        files = list(em.walk_files(dest))

        def marked(f):
            try:
                with open(f + ":Zone.Identifier", "rb") as s:
                    return b"ZoneId=3" in s.read()
            except OSError:
                return False
        n = sum(map(marked, files))
        ctx.row("M54", "Mark of the Web passed on (% of files)", t.key, n / len(files) * 100 if files else 0, "%",
                better="higher", note=f"{n}/{len(files)} files; {exit_note(m)}")
        rmtree(dest)
    rmtree(z)


def t_case(ctx: Ctx):
    """M55: two entries whose names differ only in case (Readme.txt, README.TXT)."""
    data = em.build_zip([em.ZEntry("Readme.txt", b"MARK-lower-case"), em.ZEntry("README.TXT", b"MARK-upper-case")])
    words = {2: "kept both", 1: "lost one but reported it", 0: "lost one silently"}
    for t in extract_tools(ctx):
        m, dest = extract_crafted(ctx, t, data, "case")
        found = em.find_markers(dest, {"a": b"MARK-lower-case", "b": b"MARK-upper-case"})
        score = 0 if m.get("timed_out") else em.score_case_collision(found, {"a", "b"}, m.get("exit"))
        ctx.row("M55", "case-only name clash (0-2)", t.key, score, "", better="higher",
                note=f"{words[score]}; {exit_note(m)}")
        rmtree(dest)


def t_readonly_dest(ctx: Ctx):
    """M56: extracting over a read-only file of the same name."""
    data = em.build_zip([em.ZEntry("doc/file.txt", b"MARK-new-content")])

    def prepare(dest):
        (dest / "doc").mkdir()
        f = dest / "doc" / "file.txt"
        f.write_bytes(b"old content")
        _k32.SetFileAttributesW(str(f), FILE_ATTRIBUTE_READONLY)
    words = {2: "replaced", 1: "kept old file, reported", 0: "kept old file silently"}
    for t in extract_tools(ctx):
        m, dest = extract_crafted(ctx, t, data, "rodest", prepare=prepare)
        new = em.find_markers(dest, {"n": b"MARK-new-content"}) == {"n"}
        score = 0 if m.get("timed_out") else em.score_readonly_overwrite(new, m.get("exit"))
        ctx.row("M56", "overwrite a read-only file (0-2)", t.key, score, "", better="higher",
                note=f"{words[score]}; {exit_note(m)}")
        rmtree(dest)


def t_denied_dest(ctx: Ctx):
    """M57: extracting into a folder the user may not write to."""
    data = em.build_zip([em.ZEntry("a.txt", b"a" * 1000), em.ZEntry("sub/b.txt", b"b" * 1000)])
    z = ctx.scratch / "denied.zip"
    z.write_bytes(data)
    for t in extract_tools(ctx):
        dest = ctx.scratch / f"denied-{t.key}"
        rmtree(dest)
        dest.mkdir(parents=True)
        subprocess.run(["icacls", str(dest), "/deny", "*S-1-1-0:(OI)(CI)(W,AD,WD)"], capture_output=True)
        try:
            m = measure(t.zip_extract(str(z), str(dest)), timeout=60)
        finally:
            subprocess.run(["icacls", str(dest), "/remove:d", "*S-1-1-0"], capture_output=True)
        score = int(em.failed(m.get("exit")) and not m.get("timed_out"))
        ctx.row("M57", "unwritable destination reported", t.key, score, "", better="higher",
                note=exit_note(m) + ("" if score else " (failure not reported)" if not m.get("timed_out") else ""))
        rmtree(dest)
    rmtree(z)


def t_changing(ctx: Ctx):
    """M58: a file that keeps growing while it is being archived (a live log)."""
    import threading
    src = ctx.scratch / "growing"
    rmtree(src)
    src.mkdir(parents=True)
    rng = random.Random(9)
    for i in range(5):
        (src / f"static{i}.txt").write_bytes(text_block(rng, 200_000))
    grow = src / "grow.log"
    chunk = text_block(rng, 1 << 20)
    for t in create_tools(ctx):
        grow.write_bytes(chunk * 32)
        out = ctx.scratch / f"{t.key}-grow.zip"
        rmtree(out)
        stop = threading.Event()

        def writer():
            with open(grow, "ab") as f:
                for _ in range(1024):  # at most 1 GiB more
                    if stop.is_set():
                        break
                    f.write(chunk)
                    f.flush()
                    time.sleep(0.01)
        th = threading.Thread(target=writer)
        th.start()
        time.sleep(0.2)
        try:
            m = measure(ctx.create_cmd(t, src, out), timeout=300)
        finally:
            stop.set()
            th.join()
        valid = out.exists() and ctx.seven_t(out) == ""
        reported = em.failed(m.get("exit"))
        ctx.row("M58", "growing file: valid archive (1) + change reported (1)", t.key, int(valid) + int(reported), "",
                better="higher", note=f"{'valid' if valid else 'no valid archive'}; {exit_note(m)}")
        rmtree(out)
    rmtree(src)


T_DIR1, T_DIR2, T_FILE = 1_577_836_800.0, 1_622_505_600.0, 1_640_995_200.0  # 2020-01-01, 2021-06-01, 2022-01-01


def t_dir_times(ctx: Ctx):
    """M59: folder modification times survive create + extract."""
    src = ctx.scratch / "dirtimes" / "top"
    rmtree(src.parent)
    (src / "nested").mkdir(parents=True)
    (src / "nested" / "f.txt").write_bytes(b"f")
    os.utime(src / "nested" / "f.txt", (T_FILE, T_FILE))
    os.utime(src / "nested", (T_DIR2, T_DIR2))
    os.utime(src, (T_DIR1, T_DIR1))
    for t in ctx.zip_tools():
        out, dest = ctx.scratch / f"{t.key}-dt.zip", ctx.scratch / f"x-dt-{t.key}"
        rmtree(out), rmtree(dest)
        dest.mkdir(parents=True)
        m = measure(ctx.create_cmd(t, src, out), timeout=120)
        score, lost = 0, []
        if ok(m) and out.exists():
            measure(t.zip_extract(str(out), str(dest)), timeout=120)
            root = find_root(dest, "top")
            for p, want in ((root, T_DIR1), (root / "nested", T_DIR2)):
                if p.is_dir() and abs(p.stat().st_mtime - want) <= 2:
                    score += 1
                else:
                    lost.append(p.name)
        ctx.row("M59", "folder times kept (of 2)", t.key, score, "", better="higher",
                note=("lost: " + ", ".join(lost)) if lost else ("" if ok(m) else f"create {exit_note(m)}"))
        rmtree(out), rmtree(dest)
    rmtree(src.parent)


DATES = {"y1975.txt": 170_812_800.0, "y2040.txt": 2_208_988_800.0, "y2100.txt": 4_102_444_800.0}


def t_dates(ctx: Ctx):
    """M60: modification times outside the DOS range (before 1980) and far in the future (2040, 2100)."""
    src = ctx.scratch / "dates"
    rmtree(src)
    src.mkdir(parents=True)
    for n, ts in DATES.items():
        (src / n).write_bytes(n.encode())
        os.utime(src / n, (ts, ts))
    for t in ctx.zip_tools():
        out, dest = ctx.scratch / f"{t.key}-dates.zip", ctx.scratch / f"x-dates-{t.key}"
        rmtree(out), rmtree(dest)
        dest.mkdir(parents=True)
        m = measure(ctx.create_cmd(t, src, out), timeout=120)
        kept = []
        if ok(m) and out.exists():
            measure(t.zip_extract(str(out), str(dest)), timeout=120)
            root = find_root(dest, "dates")
            kept = [n for n, ts in DATES.items() if (root / n).exists() and abs((root / n).stat().st_mtime - ts) <= 2]
        lost = [n.split(".")[0][1:] for n in DATES if n not in kept]
        ctx.row("M60", f"unusual dates kept (of {len(DATES)})", t.key, len(kept), "", better="higher",
                note=("lost: " + ", ".join(lost)) if lost else "")
        rmtree(out), rmtree(dest)
    rmtree(src)


LEGACY = {"sjis": ("日本語のファイル.txt", "shift_jis"), "cp866": ("Русский.txt", "cp866"), "cp437": ("café.txt", "cp437")}


def t_legacy_names(ctx: Ctx):
    """M61: archives from older Japanese/Russian systems (names in a local code page, no UTF-8 flag)."""
    data = em.build_zip([em.ZEntry(name.encode(enc), f"MARK-{k}".encode()) for k, (name, enc) in LEGACY.items()])
    for t in extract_tools(ctx):
        m, dest = extract_crafted(ctx, t, data, "legacy")
        right = [k for k, (name, _) in LEGACY.items()
                 if any(open(f, "rb").read() == f"MARK-{k}".encode() for f in files_named(dest, name))]
        wrong = [LEGACY[k][1] for k in LEGACY if k not in right]
        ctx.row("M61", f"legacy-encoded names read correctly (of {len(LEGACY)})", t.key, len(right), "",
                better="higher", note=("garbled: " + ", ".join(wrong)) if wrong else "")
        rmtree(dest)


def t_macos(ctx: Ctx):
    """M62: an archive made on macOS: is the __MACOSX/._ resource-fork clutter left out?"""
    apple = b"\x00\x05\x16\x07\x00\x02\x00\x00Mac OS X        " + bytes(64)
    data = em.build_zip([em.ZEntry("photos/photo.txt", b"MARK-photo"),
                         em.ZEntry("__MACOSX/photos/._photo.txt", apple), em.ZEntry("photos/.DS_Store", b"\x00\x00\x00\x01Bud1")])
    for t in extract_tools(ctx):
        m, dest = extract_crafted(ctx, t, data, "macos")
        files = list(em.walk_files(dest))
        clutter = [f for f in files if "__MACOSX" in f or os.path.basename(f).startswith("._")]
        real = em.find_markers(dest, {"p": b"MARK-photo"}) == {"p"}
        ctx.row("M62", "macOS resource-fork clutter skipped", t.key, int(real and not clutter), "", better="higher",
                note=f"{len(clutter)} clutter files" if clutter else ("" if real else "photo.txt missing"))
        rmtree(dest)


def t_unix_archive(ctx: Ctx):
    """M63: an archive made on Linux, with Unix permissions and a symbolic link entry."""
    data = em.build_zip([
        em.ZEntry("app/run.sh", b"#!/bin/sh\necho MARK-run\n", create_system=3, ext_attr=0o100755 << 16),
        em.ZEntry("app/readme.txt", b"MARK-readme", create_system=3, ext_attr=0o100644 << 16),
        em.ZEntry("app/latest", b"run.sh", create_system=3, ext_attr=0o120777 << 16)])
    for t in extract_tools(ctx):
        m, dest = extract_crafted(ctx, t, data, "unix")
        files_ok = em.find_markers(dest, {"r": b"MARK-run", "m": b"MARK-readme"}) == {"r", "m"}
        links = [Path(f) for f in em.walk_files(dest) if os.path.basename(f) == "latest"]
        link = find_root(dest, "app") / "latest"
        link_ok = os.path.islink(link) or any(b"MARK-run" in p.read_bytes() for p in links if p.is_file())
        ctx.row("M63", "Unix archive: files (1) + symlink as link or copy (1)", t.key, int(files_ok) + int(link_ok), "",
                better="higher", note="" if link_ok else ("symlink became a text file" if links else "symlink dropped"))
        rmtree(dest)


METHODS = ["Deflate64", "BZip2", "LZMA", "PPMd", "XZ", "ZSTD"]


def t_methods(ctx: Ctx):
    """M64: extracting entries compressed with Deflate64, BZip2, LZMA, PPMd, XZ and Zstandard."""
    src = ctx.scratch / "methsrc"
    rmtree(src)
    src.mkdir(parents=True)
    for f in sorted((ctx.corpora / "canterbury").rglob("*"))[:2]:
        if f.is_file():
            shutil.copyfile(f, src / f.name)
    made = {}
    for meth in METHODS:
        z = ctx.scratch / f"meth-{meth}.zip"
        rmtree(z)
        if ctx.sevenzip:
            subprocess.run([ctx.sevenzip.exe, "a", "-tzip", f"-mm={meth}", str(z), "methsrc", "-bso0", "-bsp0", "-bse0"],
                           cwd=ctx.scratch, capture_output=True)
        if (not z.exists() or ctx.seven_t(z)) and meth == "ZSTD":
            bz = next((t for t in ctx.tools if t.key == "blazezip" and t.available), None)
            if bz:
                subprocess.run([bz.exe, "c", str(z), "methsrc", "--zstd", "-q"], cwd=ctx.scratch, capture_output=True)
        if z.exists():
            made[meth] = z
        else:
            log(f"  (could not make a {meth} test archive)")
    for t in extract_tools(ctx):
        good = []
        for meth, z in made.items():
            dest = ctx.scratch / f"x-meth-{t.key}"
            rmtree(dest)
            dest.mkdir(parents=True)
            measure(t.zip_extract(str(z), str(dest)), timeout=120)
            g, total = same_tree(src, find_root(dest, "methsrc"))
            if g == total:
                good.append(meth)
            rmtree(dest)
        bad = [m for m in made if m not in good]
        ctx.row("M64", f"compression methods extracted (of {len(made)})", t.key, len(good), "", better="higher",
                note=("fails: " + ", ".join(bad)) if bad else "")
    for z in made.values():
        rmtree(z)
    rmtree(src)


def t_normalization(ctx: Ctx):
    """M65: two names that differ only in Unicode normalisation (café as NFC and as NFD)."""
    import unicodedata
    data = em.build_zip([em.ZEntry(unicodedata.normalize("NFC", "café.txt"), b"MARK-nfc"),
                         em.ZEntry(unicodedata.normalize("NFD", "café.txt"), b"MARK-nfd")])
    for t in extract_tools(ctx):
        m, dest = extract_crafted(ctx, t, data, "nfc")
        found = em.find_markers(dest, {"c": b"MARK-nfc", "d": b"MARK-nfd"})
        ctx.row("M65", "NFC/NFD names both kept", t.key, int(found == {"c", "d"}), "", better="higher",
                note=f"{len(found)} of 2 kept")
        rmtree(dest)


def t_comments(ctx: Ctx):
    """M66: archive and file comments kept when an entry is added or deleted."""
    base = em.build_zip([em.ZEntry("a.txt", b"a" * 1000, comment=b"file comment"), em.ZEntry("b.txt", b"b" * 1000)],
                        comment=b"ARCHIVE COMMENT")
    extra = ctx.scratch / "cm-add"
    extra.mkdir(parents=True, exist_ok=True)
    (extra / "added.txt").write_bytes(b"added")

    def kept(z):
        try:
            with zipfile.ZipFile(z) as f:
                return f.comment == b"ARCHIVE COMMENT" and f.getinfo("a.txt").comment == b"file comment"
        except Exception:
            return False
    for t in create_tools(ctx):
        o = ops_for(t)
        if "add" not in o or "delete" not in o:
            ctx.na("M66", "comments kept through add and delete (of 2)", t.key, "no add/delete command")
            continue
        work = ctx.scratch / f"{t.key}-cm.zip"
        score, lost = 0, []
        for label, mk in (("add", lambda: o["add"](str(work), "added.txt", str(extra))),
                          ("delete", lambda: o["delete"](str(work), "b.txt"))):
            work.write_bytes(base)
            measure(mk(), timeout=60)
            if kept(work):
                score += 1
            else:
                lost.append(label)
        ctx.row("M66", "comments kept through add and delete (of 2)", t.key, score, "", better="higher",
                note=("lost on " + ", ".join(lost)) if lost else "")
        rmtree(work)
    rmtree(extra)


def t_overlap_bomb(ctx: Ctx):
    """M67: an overlapping-entry zip bomb (4 KB that expands to 1 GiB)."""
    data = em.overlap_bomb(1024, 1 << 20)
    for t in extract_tools(ctx):
        m, dest = extract_crafted(ctx, t, data, "bomb", timeout=300)
        written = em.tree_bytes(dest)
        ctx.row("M67", "overlapping zip bomb blocked", t.key, em.score_bomb(written, 1 << 30), "", better="higher",
                note=f"wrote {written >> 20} MiB; {exit_note(m)}")
        rmtree(dest)


def t_size_lie(ctx: Ctx):
    """M68: an entry that inflates to far more than its declared size (8 MiB declared as 1000 bytes)."""
    data = em.build_zip([em.ZEntry("liar.bin", bytes(8 << 20), usize=1000)])
    for t in extract_tools(ctx):
        m, dest = extract_crafted(ctx, t, data, "liar")
        written = max((os.path.getsize(f) for f in files_named(dest, "liar.bin")), default=0)
        score = 0 if m.get("timed_out") else em.score_declared_size(written, 1000, m.get("exit"))
        ctx.row("M68", "size larger than declared: stopped or reported", t.key, score, "", better="higher",
                note=f"wrote {written} bytes; {exit_note(m)}")
        rmtree(dest)


def t_bad_crc(ctx: Ctx):
    """M69: an entry whose data does not match its CRC-32, found while extracting."""
    data = em.build_zip([em.ZEntry("good.txt", b"good" * 1000), em.ZEntry("bad.txt", b"payload" * 1000, crc=0x1234)])
    for t in extract_tools(ctx):
        m, dest = extract_crafted(ctx, t, data, "badcrc")
        score = int(em.failed(m.get("exit")) and not m.get("timed_out"))
        ctx.row("M69", "CRC mismatch reported on extract", t.key, score, "", better="higher", note=exit_note(m))
        rmtree(dest)


def t_encryption(ctx: Ctx):
    """M70: password-protected ZIPs (ZipCrypto and AES-256): extract with the password, and fail
    cleanly with a wrong one."""
    if not ctx.sevenzip:
        return
    src = ctx.scratch / "secret"
    rmtree(src)
    src.mkdir(parents=True)
    rng = random.Random(10)
    for i in range(3):
        (src / f"s{i}.txt").write_bytes(text_block(rng, 100_000))
    made = {}
    for label, mode in (("ZipCrypto", "ZipCrypto"), ("AES-256", "AES256")):
        z = ctx.scratch / f"enc-{mode}.zip"
        rmtree(z)
        subprocess.run([ctx.sevenzip.exe, "a", "-tzip", "-pSecret", f"-mem={mode}", str(z), "secret", "-bso0", "-bsp0"],
                       cwd=ctx.scratch, capture_output=True)
        if z.exists():
            made[label] = z
    for t in extract_tools(ctx):
        o = ops_for(t)
        if "extract_pw" not in o:
            ctx.na("M70", f"encrypted zips extracted with the password (of {len(made)})", t.key, "no encryption support")
            ctx.na("M70", f"wrong password: clean failure (of {len(made)})", t.key, "no encryption support")
            continue
        right, clean, notes = 0, 0, []
        for label, z in made.items():
            dest = ctx.scratch / f"x-enc-{t.key}"
            rmtree(dest)
            dest.mkdir(parents=True)
            measure(o["extract_pw"](str(z), str(dest), "Secret"), timeout=60)
            g, total = same_tree(src, find_root(dest, "secret"))
            if g == total:
                right += 1
            else:
                notes.append(f"{label} not extracted")
            rmtree(dest)
            dest.mkdir(parents=True)
            m = measure(o["extract_pw"](str(z), str(dest), "WrongPassword"), timeout=60)
            left = [f for f in em.walk_files(dest) if os.path.getsize(f) > 0]
            if em.failed(m.get("exit")) and not m.get("timed_out") and not left:
                clean += 1
            else:
                notes.append(f"{label} wrong password: {exit_note(m)}" + (f", left {len(left)} files" if left else ""))
            rmtree(dest)
        ctx.row("M70", f"encrypted zips extracted with the password (of {len(made)})", t.key, right, "",
                better="higher", note="; ".join(n for n in notes if "not extracted" in n))
        ctx.row("M70", f"wrong password: clean failure (of {len(made)})", t.key, clean, "", better="higher",
                note="; ".join(n for n in notes if "wrong password" in n))
    for z in made.values():
        rmtree(z)
    rmtree(src)


def t_av(ctx: Ctx):
    """M71: create and extract 1,000 files in a folder that Microsoft Defender scans in real time."""
    r = subprocess.run(["powershell", "-NoProfile", "-Command", "(Get-MpComputerStatus).RealTimeProtectionEnabled"],
                       capture_output=True, text=True)
    rtp = r.stdout.strip() or "unknown"
    log(f"  Defender real-time protection: {rtp}")
    base = Path.home() / "bzbench-av"
    rmtree(base)
    base.mkdir(parents=True)
    try:
        src = base / "tree1000"
        shutil.copytree(ensure(ctx, "tree1000", lambda d: gen_tree1000(d, ctx.corpora)), src)
        ref = base / "ref.zip"
        if ctx.sevenzip:
            subprocess.run([ctx.sevenzip.exe, "a", "-tzip", "-mx1", str(ref), "tree1000", "-bso0", "-bsp0"], cwd=base,
                           capture_output=True)
        note = f"real-time protection: {rtp}"
        for t in ctx.zip_tools():
            out = base / f"{t.key}.zip"
            res, err = ctx.timed(lambda: ctx.create_cmd(t, src, out), lambda: rmtree(out),
                                 reps=min(ctx.args.reps, 3), timeout=ctx.args.timeout)
            ctx.row("M71", "create 1,000 files, Defender-scanned folder: wall", t.key, res and res["wall_s"], "s",
                    note=err or note)
            rmtree(out)
            if ref.exists():
                dest = base / f"x-{t.key}"
                res, err = ctx.timed(lambda: t.zip_extract(str(ref), str(dest)),
                                     lambda: (rmtree(dest), dest.mkdir(parents=True)), reps=min(ctx.args.reps, 3),
                                     timeout=ctx.args.timeout)
                ctx.row("M71", "extract 1,000 files, Defender-scanned folder: wall", t.key, res and res["wall_s"], "s",
                        note=err or note)
                rmtree(dest)
    finally:
        rmtree(base)


PROBE = r'''
import json, os, sys, time
stop, out = sys.argv[1], sys.argv[2]
samples = []
while not os.path.exists(stop):
    t0 = time.perf_counter()
    time.sleep(0.01)
    t1 = time.perf_counter()
    samples.append((t1, (t1 - t0 - 0.01) * 1000))
json.dump(samples, open(out, "w"))
'''


def probe_during(ctx: Ctx, cmd, timeout):
    """Run `cmd` while a normal-priority process times 10 ms sleeps; return (measurement, p99 lateness ms)."""
    script, stop, out = ctx.scratch / "probe.py", ctx.scratch / "probe.stop", ctx.scratch / "probe.json"
    script.write_text(PROBE)
    stop.unlink(missing_ok=True)
    p = subprocess.Popen([sys.executable, str(script), str(stop), str(out)])
    time.sleep(1.0)
    t0 = time.perf_counter()
    m = measure(cmd, timeout=timeout)
    t1 = time.perf_counter()
    stop.write_text("stop")
    p.wait(timeout=30)
    samples = [late for ts, late in json.loads(out.read_text()) if t0 <= ts <= t1]
    stop.unlink(missing_ok=True)
    return m, (em.percentile(samples, 99) if samples else None)


def t_responsiveness(ctx: Ctx):
    """M72 + M77: how much a tool delays other programs (p99 lateness of a 10 ms timer) while it
    creates the silesia archive and while it extracts the 23k-file linux archive."""
    sil = ctx.corpora / "silesia"
    linux_ref = ctx.archives / "linux" / "_reference.zip"
    for t in ctx.zip_tools():
        out = ctx.scratch / f"{t.key}-resp.zip"
        p99s = []
        for _ in range(3):
            rmtree(out)
            m, p99 = probe_during(ctx, ctx.create_cmd(t, sil, out), ctx.args.timeout)
            if ok(m) and p99 is not None:
                p99s.append(p99)
        rmtree(out)
        ctx.row("M72", "timer lateness p99 while creating silesia", t.key, statistics.median(p99s) if p99s else None,
                "ms", note="" if p99s else "create failed")
        dest = ctx.scratch / f"x-resp-{t.key}"
        p99s = []
        for _ in range(3):
            rmtree(dest)
            dest.mkdir(parents=True)
            m, p99 = probe_during(ctx, t.zip_extract(str(linux_ref), str(dest)), ctx.args.timeout)
            if p99 is not None and not m.get("timed_out"):
                p99s.append(p99)
        rmtree(dest)
        ctx.row("M77", "timer lateness p99 while extracting linux", t.key, statistics.median(p99s) if p99s else None,
                "ms", note="" if p99s else "extract failed")


def measure_together(cmds, timeout):
    """Start several measured commands at once; returns their measurements."""
    procs = [subprocess.Popen(bench_argv(c, timeout=timeout), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                              errors="replace") for c in cmds]
    return [parse_measurement(*p.communicate()) for p in procs]


def t_two_jobs(ctx: Ctx):
    """M73: two silesia archives created at the same time (two scripts or users sharing the PC)."""
    src = ctx.corpora / "silesia"
    for t in ctx.zip_tools():
        outs = [ctx.scratch / f"{t.key}-pair{i}.zip" for i in range(2)]
        walls, err = [], ""
        for _ in range(min(ctx.args.reps, 3)):
            for o in outs:
                rmtree(o)
            ms = measure_together([ctx.create_cmd(t, src, o) for o in outs], ctx.args.timeout)
            if not all(ok(m) for m in ms) or any(ctx.seven_t(o) for o in outs):
                err = "a job failed"
                break
            walls.append(max(m["wall_s"] for m in ms))
        ctx.row("M73", "two creates at once: wall until both finish", t.key,
                statistics.median(walls) if walls and not err else None, "s", note=err)
        for o in outs:
            rmtree(o)


def t_mem_cap(ctx: Ctx):
    """M74: create silesia at the max preset with memory capped at 256 MiB (small VM or container)."""
    src = ctx.corpora / "silesia"
    for t in ctx.zip_tools():
        out = ctx.scratch / f"{t.key}-cap.zip"
        res, err = ctx.timed(lambda: ctx.create_cmd(t, src, out, "max"), lambda: rmtree(out), lambda: ctx.seven_t(out),
                             reps=1, timeout=ctx.args.timeout, mem_limit=256)
        ctx.row("M74", "completes within a 256 MiB memory cap", t.key, int(res is not None), "", better="higher",
                note=err)
        ctx.row("M74", "max-preset create under a 256 MiB cap: wall", t.key, res and res["wall_s"], "s", note=err)
        rmtree(out)


def t_overwrite(ctx: Ctx):
    """M75: extract silesia over an existing copy of the same files."""
    ref = ctx.archives / "silesia" / "_reference.zip"
    for t in ctx.zip_tools():
        dest = ctx.scratch / f"x-ow-{t.key}"
        rmtree(dest)
        dest.mkdir(parents=True)
        measure(t.zip_extract(str(ref), str(dest)), timeout=ctx.args.timeout)
        res, err = ctx.timed(lambda: t.zip_extract(str(ref), str(dest)), timeout=ctx.args.timeout)
        if res:
            good, total = same_tree(ctx.corpora / "silesia", find_root(dest, "silesia"))
            if good != total:
                res, err = None, f"{total - good} of {total} files wrong after overwrite"
        ctx.row("M75", "extract over existing files: wall", t.key, res and res["wall_s"], "s", note=err)
        rmtree(dest)


BUDGETS = (1, 3, 10)


def t_levels(ctx: Ctx):
    """M78 + M79: every compression level of each tool on silesia: the area under each tool's
    speed/size curve, and the smallest archive each can make within 1, 3 and 10 seconds."""
    import math
    src = ctx.corpora / "silesia"
    insize = sum(tree_files(src).values())
    curves = {}
    for t in create_tools(ctx):
        levels = em.levels_for(t) or {"default": lambda out, cwd, s, t=t: ctx.create_cmd(t, src, Path(out))}
        pts = []
        for lv, mk in levels.items():
            out = ctx.scratch / f"{t.key}-lv.zip"
            res, err = ctx.timed(lambda: mk(str(out), str(src.parent), src.name), lambda: rmtree(out),
                                 lambda: ctx.seven_t(out), reps=min(ctx.args.reps, 2), timeout=ctx.args.timeout)
            if res:
                ratio = out.stat().st_size / insize * 100
                pts.append((res["wall_s"], ratio))
                r = {"test": "M78", "metric": f"silesia at level {lv}", "tool": t.key, "value": ratio, "unit": "%",
                     "better": "lower", "note": f"{res['wall_s']:.3f} s", "ok": True, "info": True, "wall_s": res["wall_s"]}
                ctx.rows.append(r)
                log(f"  M78   level {lv:<3} {t.key:<10} {ratio:.2f}% in {res['wall_s']:.3f} s")
            rmtree(out)
        curves[t.key] = pts
    allw = [w for pts in curves.values() for w, _ in pts]
    if not allw:
        return
    ref = (math.log10(max(allw) * 1.25), 100.0)
    for k, pts in curves.items():
        hv = em.hypervolume([(math.log10(w), r) for w, r in pts], ref) if pts else None
        ctx.row("M78", "speed/size curve area (log time x % saved)", k, hv, "", better="higher",
                note=f"{len(pts)} levels")
        for b in BUDGETS:
            best = em.best_within(pts, b)
            ctx.row("M79", f"smallest archive within {b} s", k, best, "%",
                    note="" if best is not None else "no level finishes in time")
    ctx.save()


def t_logs(ctx: Ctx):
    """M80: 256 MiB of JSON-lines application logs."""
    src = ensure(ctx, "logs", lambda d: em.gen_logs(d, 256))
    create_extract(ctx, "M80", "JSON logs 256 MiB", src, reps=min(ctx.args.reps, 3), ratio=True)


def t_sqlite(ctx: Ctx):
    """M81: a 128 MiB SQLite database."""
    src = ensure(ctx, "sqlite", lambda d: em.gen_sqlite(d, 128))
    create_extract(ctx, "M81", "SQLite 128 MiB", src, reps=min(ctx.args.reps, 3), ratio=True)


def t_gitrepo(ctx: Ctx):
    """M82: a source checkout with its .git folder (3,000 kernel files)."""
    linux = ctx.corpora / "linux"
    files = sorted(p for p in linux.rglob("*") if p.is_file())[:3000]
    src = ensure(ctx, "gitrepo", lambda d: em.gen_git_repo(d, files, linux))
    create_extract(ctx, "M82", "git checkout + .git", src, reps=min(ctx.args.reps, 3), ratio=True)


def t_office(ctx: Ctx):
    """M83: 400 Word documents and 400 PDFs (already compressed inside)."""
    def gen(d):
        stage = ctx.scratch / "office-src"
        rmtree(stage)
        stage.mkdir(parents=True)
        for i, f in enumerate(sorted(p for p in (ctx.corpora / "linux").rglob("*.c"))[:400]):
            os.link(f, stage / f"{i:04d}.txt")
        em.gen_office(d, stage)
        rmtree(stage)
    src = ensure(ctx, "office", gen)
    create_extract(ctx, "M83", "docx + pdf", src, reps=min(ctx.args.reps, 3), ratio=True)


def tiny_reference(ctx: Ctx) -> Path:
    tiny = ensure(ctx, "tiny", gen_tiny)
    ref = ctx.scratch / "made" / "tiny-reference.zip"
    if not ref.exists() and ctx.sevenzip:
        ref.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([ctx.sevenzip.exe, "a", "-tzip", "-mx1", str(ref), "tiny", "-bso0", "-bsp0"],
                       cwd=tiny.parent, check=True)
    return ref


def t_add_big(ctx: Ctx):
    """M84: add one 1 MiB file to a 100,000-entry archive."""
    ref = tiny_reference(ctx)
    extra = ctx.scratch / "addbig"
    extra.mkdir(parents=True, exist_ok=True)
    (extra / "added.txt").write_bytes(text_block(random.Random(5), 1 << 20))
    for t in create_tools(ctx):
        o = ops_for(t)
        if "add" not in o:
            ctx.na("M84", "add 1 file to a 100k-entry zip: wall", t.key, "no CLI command")
            continue
        work = ctx.scratch / f"{t.key}-big.zip"
        res, err = ctx.timed(lambda: o["add"](str(work), "added.txt", str(extra)), lambda: shutil.copyfile(ref, work),
                             reps=min(ctx.args.reps, 3), timeout=ctx.args.timeout)
        if res:
            with zipfile.ZipFile(work) as z:
                if len(z.infolist()) < 100_001:
                    res, err = None, "entries lost or not added"
        ctx.row("M84", "add 1 file to a 100k-entry zip: wall", t.key, res and res["wall_s"], "s", note=err)
        rmtree(work)
    rmtree(extra)


def t_update(ctx: Ctx):
    """M85: refresh an archive of 1,000 files after 10 of them changed."""
    base = ctx.scratch / "upd"
    rmtree(base)
    base.mkdir(parents=True)
    src = base / "tree1000"
    shutil.copytree(ensure(ctx, "tree1000", lambda d: gen_tree1000(d, ctx.corpora)), src)
    files = sorted(p for p in src.rglob("*") if p.is_file())[:10]
    originals = {p: p.read_bytes() for p in files}
    try:
        for t in create_tools(ctx):
            o = ops_for(t)
            if "update" not in o:
                ctx.na("M85", "update 10 changed of 1,000 files: wall", t.key, "no update command")
                continue
            for p, b in originals.items():
                p.write_bytes(b)
            own, work = base / f"{t.key}-base.zip", base / f"{t.key}-work.zip"
            rmtree(own)
            m = measure(ctx.create_cmd(t, src, own), timeout=ctx.args.timeout)
            if not ok(m) or not own.exists():
                ctx.row("M85", "update 10 changed of 1,000 files: wall", t.key, None, "s", note="create failed")
                continue
            time.sleep(2.1)  # DOS times have 2-second steps
            for p, b in originals.items():
                p.write_bytes(b + b"\nMARK-updated\n")
            res, err = ctx.timed(lambda: o["update"](str(work), "tree1000", str(base)),
                                 lambda: shutil.copyfile(own, work), reps=min(ctx.args.reps, 3),
                                 timeout=ctx.args.timeout)
            if res:
                rels = {p.relative_to(base).as_posix() for p in files}
                with zipfile.ZipFile(work) as z:
                    fresh = sum(1 for i in z.infolist() if i.filename in rels and z.read(i).endswith(b"MARK-updated\n"))
                    n = sum(1 for i in z.infolist() if not i.is_dir())
                if fresh != len(rels) or n != 1000:
                    res, err = None, f"{fresh}/{len(rels)} changes stored, {n} files"
            ctx.row("M85", "update 10 changed of 1,000 files: wall", t.key, res and res["wall_s"], "s", note=err)
            rmtree(own), rmtree(work)
    finally:
        rmtree(base)


def t_eocd(ctx: Ctx):
    """M89: recovery when only the end-of-central-directory record is damaged."""
    src = ensure(ctx, "tree1000", lambda d: gen_tree1000(d, ctx.corpora))
    for t in ctx.zip_tools():
        z = ctx.made(t, src, "tree1000")
        if not z:
            ctx.row("M89", "files recovered with a damaged end record", t.key, None, "%", better="higher",
                    note="create failed")
            continue
        data = bytearray(z.read_bytes())
        for sig in (b"PK\x05\x06", b"PK\x06\x07", b"PK\x06\x06"):
            i = data.rfind(sig)
            if i >= 0:
                data[i:i + 4] = b"\0\0\0\0"
        bad = ctx.scratch / f"{t.key}-eocd.zip"
        bad.write_bytes(data)
        dest = ctx.scratch / f"x-eocd-{t.key}"
        rmtree(dest)
        dest.mkdir(parents=True)
        m = measure(t.zip_extract(str(bad), str(dest)), timeout=300)
        good, total = same_tree(src, find_root(dest, "tree1000"))
        ctx.row("M89", "files recovered with a damaged end record", t.key, good / total * 100, "%", better="higher",
                note=f"{good}/{total}; {exit_note(m)}")
        rmtree(dest), rmtree(bad)


def t_footprint(ctx: Ctx):
    """M90: disk footprint of each command-line tool (executable plus the DLLs beside it)."""
    for t in ctx.tools:
        if t.available and t.exe:
            ctx.row("M90", "command-line tool size", t.key, em.footprint(t.exe) / 2**20, "MiB",
                    note=Path(t.exe).name)


TESTS = [
    ("T03", t_single_thread), ("T05", t_scaling), ("T06", t_ttfb), ("T09", t_contention), ("T11", t_tiny),
    ("T13", t_deep), ("T14", t_unicode), ("T15", t_sparse), ("T16", t_mixed), ("T18", t_ops), ("T23", t_resources),
    ("T30", t_startup), ("T34", t_neutral_decode), ("T35", t_overhead), ("T36", t_determinism), ("T38", t_compat),
    ("T39", t_metadata), ("T40", t_zip64_count), ("T42", t_locked), ("T43", t_corruption), ("T44", t_truncated),
    ("T45", t_zipslip), ("T46", t_exit_codes), ("T49", t_interrupt),
    ("T51", t_reserved), ("T52", t_junction_loop), ("T53", t_links), ("T54", t_motw), ("T55", t_case),
    ("T56", t_readonly_dest), ("T57", t_denied_dest), ("T58", t_changing), ("T59", t_dir_times), ("T60", t_dates),
    ("T61", t_legacy_names), ("T62", t_macos), ("T63", t_unix_archive), ("T64", t_methods), ("T65", t_normalization),
    ("T66", t_comments), ("T67", t_overlap_bomb), ("T68", t_size_lie), ("T69", t_bad_crc), ("T70", t_encryption),
    ("T71", t_av), ("T72", t_responsiveness), ("T73", t_two_jobs), ("T74", t_mem_cap), ("T75", t_overwrite),
    ("T78", t_levels), ("T80", t_logs), ("T81", t_sqlite), ("T82", t_gitrepo), ("T83", t_office), ("T84", t_add_big),
    ("T85", t_update), ("T89", t_eocd), ("T90", t_footprint),
    ("T17", derived), ("T12", t_huge), ("T48", t_million),
]
HEAVY = {"T12", "T48"}


# ---------------------------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------------------------

def rank(rows):
    """Medals per (test, metric): standard competition ranking, ties share a place."""
    groups = {}
    for r in rows:
        if r.get("ok") and r["value"] is not None and not r.get("info"):
            groups.setdefault((r["test"], r["metric"]), []).append(r)
    for (test, metric), rs in groups.items():
        if len({r["value"] for r in rs}) == 1:
            continue  # every tool tied: the metric tells them apart in nothing, so no medals
        rev = rs[0]["better"] == "higher"
        rs.sort(key=lambda r: r["value"], reverse=rev)
        place = 0
        for i, r in enumerate(rs):
            if i == 0 or r["value"] != rs[i - 1]["value"]:
                place = i + 1
            r["place"] = place
    return groups


def write_report(path: Path, data: dict, tools: list):
    rows = data["rows"]
    groups = rank(rows)
    names = {t["key"]: t["name"] for t in tools}
    colors = {t["key"]: t["color"] for t in tools}
    tally = {}
    for r in rows:
        if r.get("place") in (1, 2, 3):
            tally.setdefault(r["tool"], [0, 0, 0])[r["place"] - 1] += 1
    medal = {1: "🥇", 2: "🥈", 3: "🥉"}
    e = html.escape
    out = ["<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>",
           "<title>Extended Benchmark</title><style>",
           ":root{--bg:#fff;--fg:#111;--mut:#666;--line:#e5e5e5;--card:#fafafa}",
           "@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#111;--fg:#eee;--mut:#999;--line:#333;--card:#1a1a1a}}",
           "body{background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,sans-serif;margin:0;padding:16px;max-width:1100px;margin:auto}",
           "table{border-collapse:collapse;width:100%;margin:6px 0 18px}td,th{border-bottom:1px solid var(--line);padding:4px 8px;text-align:left}",
           "th{color:var(--mut);font-weight:600}.num{text-align:right;font-variant-numeric:tabular-nums}.mut{color:var(--mut)}",
           ".wrap{overflow-x:auto}.sw{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px}",
           "h2{margin-top:28px}</style></head><body>",
           "<h1>Extended benchmark</h1>",
           f"<p class='mut'>{e(data['meta'].get('started', ''))} · {e(data['meta'].get('machine', ''))}</p>",
           "<h2>Medal table</h2><div class='wrap'><table><tr><th>Tool</th><th class='num'>🥇</th><th class='num'>🥈</th><th class='num'>🥉</th></tr>"]
    for k, (g, s, b) in sorted(tally.items(), key=lambda kv: (-kv[1][0], -kv[1][1], -kv[1][2])):
        out.append(f"<tr><td><span class='sw' style='background:{colors.get(k, '#888')}'></span>{e(names.get(k, k))}</td>"
                   f"<td class='num'>{g}</td><td class='num'>{s}</td><td class='num'>{b}</td></tr>")
    out.append("</table></div>")
    by_test = {}
    for r in rows:
        by_test.setdefault(r["test"], {}).setdefault(r["metric"], []).append(r)
    for test in sorted(by_test):
        out.append(f"<h2>{e(test)}</h2>")
        for metric, rs in by_test[test].items():
            unit = next((r["unit"] for r in rs if r["unit"]), "")
            arrow = "higher is better" if rs[0]["better"] == "higher" else "lower is better"
            out.append(f"<h3>{e(metric)} <span class='mut'>({e(unit + ', ' if unit else '')}{arrow})</span></h3>")
            out.append("<div class='wrap'><table><tr><th>Tool</th><th class='num'>Value</th><th></th><th>Note</th></tr>")
            rs = sorted(rs, key=lambda r: (r.get("place") or 999, r["tool"]))
            for r in rs:
                v = "n/a" if r["value"] is None else (f"{r['value']:.4g}" if isinstance(r["value"], float) else str(r["value"]))
                out.append(f"<tr><td><span class='sw' style='background:{colors.get(r['tool'], '#888')}'></span>{e(names.get(r['tool'], r['tool']))}</td>"
                           f"<td class='num'>{e(v)}</td><td>{medal.get(r.get('place'), '')}</td><td class='mut'>{e(r.get('note', ''))}</td></tr>")
            out.append("</table></div>")
    out.append("<h2>Not measurable on this machine</h2><table><tr><th>Metric</th><th>Why</th></tr>")
    for k, (name, why) in data.get("unmeasurable", {}).items():
        out.append(f"<tr><td>{e(k)} {e(name)}</td><td class='mut'>{e(why)}</td></tr>")
    out.append("</table></body></html>")
    path.write_text("\n".join(out), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tools", help="comma-separated tool keys")
    ap.add_argument("--only", help="comma-separated test ids (e.g. T11,T38)")
    ap.add_argument("--skip-heavy", action="store_true", help="skip the 4.6 GiB file and 1M-entry tests")
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument("--keep-best", type=int, default=3)
    ap.add_argument("--timeout", type=float, default=900)
    ap.add_argument("--work", default=r"C:\bzbench")
    ap.add_argument("--main", type=Path, help="main-suite run directory for derived metrics")
    ap.add_argument("--keep-data", action="store_true", help="keep generated test trees afterwards")
    ap.add_argument("--report-only", type=Path, help="rebuild the HTML report of a finished run")
    ap.add_argument("--no-board", action="store_true", help="trial run: leave the cross-run leaderboard alone")
    ap.add_argument("--import-runs", nargs="+", type=Path, metavar="DIR",
                    help="merge earlier ext-* run directories into the leaderboard, rebuild its report and exit")
    args = ap.parse_args()

    tools = toolsmod.discover()
    if args.import_runs:
        board = em.ExtBoard(ROOT / "results" / "ext-leaderboard.json")
        board.set_versions({t.key: t.version for t in tools if t.available})
        import_runs(board, args.import_runs)
        board.save()
        merged = {"meta": {"started": "leaderboard", "machine": ""}, "rows": board.rows(), "unmeasurable": UNMEASURABLE}
        write_report(ROOT / "results" / "ext-latest-report.html", merged, [vars_tool(t) for t in tools])
        return
    if args.report_only:
        data = json.loads((args.report_only / "extended.json").read_text())
        write_report(args.report_only / "extended.html", data, [vars_tool(t) for t in tools])
        return
    if args.tools:
        want = set(args.tools.split(","))
        tools = [t for t in tools if t.key in want]
    tools = [t for t in tools if t.available]
    args.run_dir = ROOT / "results" / ("ext-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S"))
    if args.main is None:
        runs = sorted(p for p in (ROOT / "results").glob("2*") if (p / "results.json").exists())
        args.main = runs[-1] if runs else None
    log("building blazezip + benchrun (release) ...")
    import bench
    subprocess.run(["cargo", "build", "--release", "--workspace"], cwd=ROOT, check=True, env=bench.bench_build_env())

    ctx = Ctx(args, tools, reference_tools=toolsmod.discover())
    ctx.meta = {"started": dt.datetime.now().isoformat(timespec="seconds"),
                "machine": bench.machine_summary() if hasattr(bench, "machine_summary") else os.environ.get("COMPUTERNAME", ""),
                "reps": args.reps, "keep_best": args.keep_best, "main": str(args.main or "")}
    ctx.scratch.mkdir(parents=True, exist_ok=True)
    only = set(args.only.split(",")) if args.only else None
    try:
        for tid, fn in TESTS:
            if only and tid not in only:
                continue
            if args.skip_heavy and tid in HEAVY:
                continue
            em.wait_for_memory(2048, log=log)
            log(f"=== {tid}: {(fn.__doc__ or '').strip().splitlines()[0]}")
            try:
                fn(ctx)
            except Exception as ex:  # one broken test must not lose the rest
                log(f"  test {tid} crashed: {ex!r}")
    finally:
        ctx.meta["finished"] = dt.datetime.now().isoformat(timespec="seconds")
        ctx.save()
        all_tools = [vars_tool(t) for t in toolsmod.discover()]
        data = json.loads((args.run_dir / "extended.json").read_text())
        write_report(args.run_dir / "extended.html", data, all_tools)
        if not args.no_board:
            update_board(data, args.run_dir.name, [t for t in tools if t.available], all_tools)
        rmtree(ctx.scratch)
        if not args.keep_data:
            for name in ("tiny", "deep", "unicode", "sparse", "mixed", "empty10k", "many70k", "meta", "tree1000",
                         "million", "huge"):
                drop(ctx, name)
        log(f"report: {args.run_dir / 'extended.html'}")


def import_runs(board, dirs):
    """Merge earlier runs into the board, oldest first (run directories sort by their timestamp)."""
    for d in sorted(map(Path, dirs), key=lambda p: p.name):
        data = json.loads((d / "extended.json").read_text())
        for r in data["rows"]:
            board.add(r, d.name)


def update_board(data: dict, run_id: str, tools, all_tools):
    """Merge this run into the cross-run board and rebuild the leaderboard report from it."""
    board = em.ExtBoard(ROOT / "results" / "ext-leaderboard.json")
    board.set_versions({t.key: t.version for t in tools})
    for r in data["rows"]:
        board.add(r, run_id)
    board.save()
    merged = {"meta": {**data["meta"], "started": f"leaderboard (latest run {run_id})"}, "rows": board.rows(),
              "unmeasurable": UNMEASURABLE}
    write_report(ROOT / "results" / "ext-latest-report.html", merged, all_tools)
    log(f"leaderboard report: {ROOT / 'results' / 'ext-latest-report.html'}")


def vars_tool(t):
    return {"key": t.key, "name": t.name, "color": t.color}


if __name__ == "__main__":
    main()
