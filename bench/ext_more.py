"""Helpers for the extended suite's second set of metrics (M51-M91): crafted archives, scoring
rules, statistics, test-data generators, compression-level tables and a cross-run leaderboard.

Kept free of measurement code so every rule here is unit-tested (bench/tests/test_ext_more.py).
"""

import json
import os
import random
import shutil
import sqlite3
import struct
import subprocess
import zlib
from dataclasses import dataclass
from pathlib import Path

from tools import Cmd

# ---------------------------------------------------------------------------------------------
# Crafted archives
# ---------------------------------------------------------------------------------------------

DOS_TIME = (10 << 11) | (0 << 5) | 0  # 10:00:00
DOS_DATE = ((2024 - 1980) << 9) | (1 << 5) | 1  # 2024-01-01


@dataclass
class ZEntry:
    """One entry of a hand-built archive. `name` as bytes is written raw (no UTF-8 flag)."""
    name: object
    data: bytes = b""
    method: int = 8  # 0 store, 8 deflate
    crc: int | None = None  # override the stored CRC-32
    usize: int | None = None  # override the declared uncompressed size
    create_system: int = 0  # 0 MS-DOS, 3 Unix
    ext_attr: int = 0
    comment: bytes = b""


def _deflate(data: bytes) -> bytes:
    c = zlib.compressobj(9, zlib.DEFLATED, -15)
    return c.compress(data) + c.flush()


def build_zip(entries, comment: bytes = b"") -> bytes:
    out = bytearray()
    central = bytearray()
    for e in entries:
        if isinstance(e.name, str):
            name = e.name.encode("utf-8")
            flags = 0x800 if not e.name.isascii() else 0
        else:
            name, flags = e.name, 0
        payload = _deflate(e.data) if e.method == 8 else e.data
        crc = zlib.crc32(e.data) if e.crc is None else e.crc
        usize = len(e.data) if e.usize is None else e.usize
        offset = len(out)
        out += struct.pack("<IHHHHHIIIHH", 0x04034B50, 20, flags, e.method, DOS_TIME, DOS_DATE, crc, len(payload),
                           usize, len(name), 0)
        out += name + payload
        central += struct.pack("<IHHHHHHIIIHHHHHII", 0x02014B50, (e.create_system << 8) | 20, 20, flags, e.method,
                               DOS_TIME, DOS_DATE, crc, len(payload), usize, len(name), 0, len(e.comment), 0, 0,
                               e.ext_attr, offset)
        central += name + e.comment
    cd_offset = len(out)
    out += central
    out += struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, len(entries), len(entries), len(central), cd_offset,
                       len(comment)) + comment
    return bytes(out)


def overlap_bomb(n=1000, size=1 << 20) -> bytes:
    """`n` central-directory entries that all point at one local header holding `size` zeros:
    a few KB that expand to n * size bytes when every entry is extracted (an overlapping-entry bomb)."""
    data = bytes(size)
    payload = _deflate(data)
    crc = zlib.crc32(data)
    out = bytearray(struct.pack("<IHHHHHIIIHH", 0x04034B50, 20, 0, 8, DOS_TIME, DOS_DATE, crc, len(payload), size,
                                5, 0))
    out += b"0.bin" + payload
    central = bytearray()
    for i in range(n):
        name = f"{i}.bin".encode()
        central += struct.pack("<IHHHHHHIIIHHHHHII", 0x02014B50, 20, 20, 0, 8, DOS_TIME, DOS_DATE, crc, len(payload),
                               size, len(name), 0, 0, 0, 0, 0, 0)
        central += name
    cd_offset = len(out)
    out += central
    out += struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, n, n, len(central), cd_offset, 0)
    return bytes(out)


# ---------------------------------------------------------------------------------------------
# Paths, junctions, trees
# ---------------------------------------------------------------------------------------------

def long(p) -> str:
    s = str(Path(p).resolve())
    return s if s.startswith("\\\\?\\") else "\\\\?\\" + s


def make_junction(link: Path, target: Path):
    """Directory junction (needs no administrator rights, unlike a symbolic link)."""
    r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(Path(target).resolve())], capture_output=True,
                       text=True)
    if r.returncode != 0:
        raise OSError(f"mklink /J failed: {r.stdout}{r.stderr}")


def is_junction(p: Path) -> bool:
    return os.path.isjunction(p)


def remove_tree(p: Path):
    """Delete a tree without following junctions or links, clearing read-only attributes."""
    p = Path(p)
    if not os.path.lexists(p):
        return
    lp = long(p) if not os.path.islink(p) and not is_junction(p) else str(p)
    if is_junction(p) or os.path.islink(p):
        os.rmdir(p) if os.path.isdir(p) else os.unlink(p)
        return
    if not os.path.isdir(lp):
        os.chmod(lp, 0o666)
        os.unlink(lp)
        return
    for entry in os.scandir(lp):
        full = entry.path
        if entry.is_junction() or entry.is_symlink():
            try:
                os.rmdir(full)
            except OSError:
                os.unlink(full)
        elif entry.is_dir(follow_symlinks=False):
            remove_tree(Path(full))
        else:
            try:
                os.chmod(full, 0o666)
                os.unlink(full)
            except OSError:
                pass
    os.chmod(lp, 0o777)
    os.rmdir(lp)


def walk_files(root: Path):
    """Every file under root (extended-length aware, junctions not followed)."""
    for dirpath, dirs, files in os.walk(long(root)):
        dirs[:] = [d for d in dirs if not os.path.isjunction(os.path.join(dirpath, d))]
        for f in files:
            yield os.path.join(dirpath, f)


def find_markers(root: Path, markers: dict) -> set:
    """Keys of `markers` whose bytes appear in some file under root (whatever the file was named)."""
    found = set()
    for f in walk_files(root):
        try:
            with open(f, "rb") as fh:
                data = fh.read(1 << 20)
        except OSError:
            continue
        for k, m in markers.items():
            if m in data:
                found.add(k)
    return found


def tree_bytes(root: Path) -> int:
    total = 0
    for f in walk_files(root):
        try:
            total += os.path.getsize(f)
        except OSError:
            pass
    return total


# ---------------------------------------------------------------------------------------------
# Scoring rules
# ---------------------------------------------------------------------------------------------

def failed(exit_code) -> bool:
    return exit_code not in (0, None)


def score_case_collision(found: set, expected: set, exit_code) -> int:
    """2: both same-name-but-case files kept; 1: one lost but the tool said so; 0: silently lost."""
    if expected <= found:
        return 2
    return 1 if failed(exit_code) else 0


def score_readonly_overwrite(new_content: bool, exit_code) -> int:
    """2: the read-only file was replaced; 1: kept but reported; 0: kept with exit 0."""
    if new_content:
        return 2
    return 1 if failed(exit_code) else 0


def score_bomb(written: int, full: int) -> int:
    """1 when the tool wrote under a tenth of what the bomb expands to."""
    return int(written < full / 10)


def score_declared_size(written: int, declared: int, exit_code) -> int:
    """1 when the tool stopped at the declared size or reported the mismatch."""
    return int(failed(exit_code) or written <= declared)


def score_junction_loop(hung: bool, exit_code, entries: int) -> int:
    """2: finished with a sane archive; 1: stopped with an error; 0: hung or recursed."""
    if hung or entries >= 10_000:
        return 0
    return 2 if exit_code == 0 else 1


# ---------------------------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------------------------

def hypervolume(points, ref) -> float:
    """Area of the (wall, ratio) region dominated by `points` inside the box up to `ref`
    (both minimised). Larger means a better speed/size trade-off curve."""
    pts = sorted(p for p in points if p[0] < ref[0] and p[1] < ref[1])
    area, best = 0.0, ref[1]
    for wall, ratio in pts:
        if ratio < best:
            area += (ref[0] - wall) * (best - ratio)
            best = ratio
    return area


def best_within(points, budget):
    r = [ratio for wall, ratio in points if wall <= budget]
    return min(r) if r else None


def percentile(xs, p):
    xs = sorted(xs)
    if len(xs) == 1:
        return xs[0]
    k = (len(xs) - 1) * p / 100
    i = int(k)
    j = min(i + 1, len(xs) - 1)
    return xs[i] + (xs[j] - xs[i]) * (k - i)


# ---------------------------------------------------------------------------------------------
# Test data
# ---------------------------------------------------------------------------------------------

def gen_logs(d: Path, mb: int):
    """Application logs as JSON lines, 8 MiB per file (very compressible, typical archiving input)."""
    d = Path(d)
    d.mkdir(parents=True, exist_ok=True)
    rng = random.Random(11)
    services = ["api", "auth", "billing", "search", "worker", "gateway"]
    paths = ["/v1/items", "/v1/items/{id}", "/v1/users/{id}", "/login", "/search", "/health", "/v2/orders"]
    msgs = ["request completed", "cache miss", "retrying upstream", "slow query", "token refreshed",
            "validation failed", "queue drained"]
    agents = ["Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "okhttp/4.12.0", "curl/8.5.0", "python-requests/2.32"]
    ts = 1_704_067_200.0
    target, written, n = mb << 20, 0, 0
    f = None
    while written < target:
        if f is None or f.tell() >= 8 << 20:
            if f:
                f.close()
            f = open(d / f"app-{n:03d}.jsonl", "w", encoding="utf-8", newline="\n")
            n += 1
        ts += rng.expovariate(50)
        rec = {"ts": f"{ts:.3f}", "level": rng.choices(["INFO", "WARN", "ERROR", "DEBUG"], [80, 10, 3, 7])[0],
               "service": rng.choice(services), "path": rng.choice(paths).replace("{id}", str(rng.randrange(10**6))),
               "status": rng.choices([200, 201, 304, 400, 401, 404, 500], [70, 5, 10, 5, 3, 5, 2])[0],
               "ms": round(rng.lognormvariate(3, 1), 2), "req": f"{rng.getrandbits(64):016x}",
               "user": rng.randrange(50_000), "ua": rng.choice(agents), "msg": rng.choice(msgs)}
        line = json.dumps(rec) + "\n"
        f.write(line)
        written += len(line)
    f.close()


def gen_sqlite(d: Path, mb: int):
    """A SQLite database with a table and an index (page-structured, partly empty pages)."""
    d = Path(d)
    d.mkdir(parents=True, exist_ok=True)
    db = d / "app.db"
    db.unlink(missing_ok=True)
    rng = random.Random(12)
    kinds = ["view", "click", "purchase", "signup", "logout", "search"]
    c = sqlite3.connect(db)
    try:
        c.execute("create table events(id integer primary key, ts real, user integer, kind text, payload text)")
        c.execute("create index events_user on events(user, ts)")
        i = 0
        while db.stat().st_size < mb << 20:
            rows = []
            for _ in range(5000):
                i += 1
                rows.append((i, 1.7e9 + i * 0.37, rng.randrange(100_000), rng.choice(kinds),
                             json.dumps({"ref": f"{rng.getrandbits(48):012x}", "qty": rng.randrange(1, 9),
                                         "note": rng.choice(["", "gift", "express", "returning customer"])})))
            c.executemany("insert into events values (?,?,?,?,?)", rows)
            c.commit()
    finally:
        c.close()  # "with connect()" only commits; an open handle would block exclusive readers


def gen_office(out: Path, srcdir: Path):
    """.docx (a Deflate-compressed ZIP container) per text file, plus PDFs with Flate streams:
    already-compressed documents, where re-compressing gains little."""
    out = Path(out)
    from xml.sax.saxutils import escape
    import zipfile
    texts = sorted(p for p in Path(srcdir).rglob("*") if p.is_file())
    for i, t in enumerate(texts):
        body = t.read_bytes().decode("latin-1")
        paras = "".join(f"<w:p><w:r><w:t>{escape(line)}</w:t></w:r></w:p>" for line in body.splitlines()[:20000])
        with zipfile.ZipFile(out / f"doc{i:03d}.docx", "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/'
                       'package/2006/content-types"><Default Extension="xml" ContentType="application/xml"/></Types>')
            z.writestr("word/document.xml", '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats'
                       f'.org/wordprocessingml/2006/main"><w:body>{paras}</w:body></w:document>')
        stream = zlib.compress(body.encode("latin-1"), 6)
        pdf = (b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
               b"3 0 obj<</Type/Page/Parent 2 0 R/Contents 4 0 R>>endobj\n"
               + f"4 0 obj<</Length {len(stream)}/Filter/FlateDecode>>stream\n".encode() + stream
               + b"\nendstream endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n")
        (out / f"doc{i:03d}.pdf").write_bytes(pdf)


def gen_git_repo(repo: Path, files, base: Path):
    """A working tree plus its packed .git directory (source checkout as people archive it)."""
    repo = Path(repo)
    for f in files:
        dst = repo / Path(f).relative_to(base)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(f, dst)
    env = {**os.environ, "GIT_AUTHOR_NAME": "bench", "GIT_AUTHOR_EMAIL": "bench@example.com",
           "GIT_COMMITTER_NAME": "bench", "GIT_COMMITTER_EMAIL": "bench@example.com",
           "GIT_AUTHOR_DATE": "2024-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2024-01-01T00:00:00Z"}
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "snapshot"], ["gc", "-q"]):
        subprocess.run(["git", "-c", "core.autocrlf=false", "-C", str(repo), *args], env=env, check=True,
                       capture_output=True)


def footprint(exe: str) -> int:
    """Bytes of a CLI tool: the executable plus the DLLs beside it. Tools that live in a shared
    system folder (System32, or a bin folder with many unrelated programs) count the exe alone."""
    p = Path(exe)
    d = p.parent
    shared = d.name.lower() == "system32" or sum(1 for _ in d.glob("*.exe")) > 20
    if shared:
        return p.stat().st_size
    return p.stat().st_size + sum(f.stat().st_size for f in d.glob("*.dll"))


# ---------------------------------------------------------------------------------------------
# Every compression level each tool offers for ZIP: level -> fn(out_zip, src_parent, src_name) -> Cmd
# ---------------------------------------------------------------------------------------------

def levels_for(t) -> dict:
    exe = t.exe
    if not exe or not t.zip_create:
        return {}
    if t.key in ("7zip", "nanazip", "peazip"):
        q = ["-y", "-bso0", "-bsp0", "-bse0"]
        return {str(i): (lambda out, cwd, src, i=i: Cmd([exe, "a", "-tzip", f"-mx{i}", "-mmt=on", *q, out, src], cwd=cwd))
                for i in range(10)}
    if t.key == "blazezip":
        return {str(i): (lambda out, cwd, src, i=i: Cmd([exe, "c", out, src, f"-{i}", "-q"], cwd=cwd)) for i in range(13)}
    if t.key == "bandizip":
        return {str(i): (lambda out, cwd, src, i=i: Cmd([exe, "c", "-y", "-r", "-fmt:zip", f"-l:{i}", out, src], cwd=cwd))
                for i in range(10)}
    if t.key == "winrar":
        return {str(i): (lambda out, cwd, src, i=i: Cmd([exe, "a", "-afzip", f"-m{i}", "-r", "-ibck", "-y", out, src],
                                                        cwd=cwd)) for i in range(6)}
    if t.key == "winzip":
        return {k: (lambda out, cwd, src, f=f: Cmd([exe, "-a", "-r", "-p", f, out, src + "\\*.*"], cwd=cwd, console=True))
                for k, f in (("0", "-e0"), ("s", "-es"), ("f", "-ef"), ("n", "-en"), ("x", "-ex"))}
    if t.key == "infozip":
        return {str(i): (lambda out, cwd, src, i=i: Cmd([exe, "-r", "-q", f"-{i}", out, src], cwd=cwd)) for i in range(10)}
    if t.key == "wintar":
        return {str(i): (lambda out, cwd, src, i=i: Cmd([exe, "-a", "-c", "--options", f"zip:compression-level={i}",
                                                         "-f", out, src], cwd=cwd)) for i in range(10)}
    return {}


# ---------------------------------------------------------------------------------------------
# Cross-run leaderboard for the extended suite
# ---------------------------------------------------------------------------------------------

SUBJECT = "blazezip"


class ExtBoard:
    """Same rules as the main leaderboard: competitors keep their best value per metric, blazezip
    keeps its latest (failures included), a failure never replaces a competitor's success, and a
    tool's records are dropped when its version changes."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.data = {"versions": {}, "records": {}}
        if self.path.exists():
            self.data.update(json.loads(self.path.read_text()))

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1))
        tmp.replace(self.path)

    def set_versions(self, versions: dict):
        for tool, v in versions.items():
            old = self.data["versions"].get(tool)
            if old is not None and old != v:
                self.data["records"] = {k: r for k, r in self.data["records"].items() if r["tool"] != tool}
            self.data["versions"][tool] = v

    def add(self, row: dict, run_id: str):
        row = {**row, "run": run_id}
        row.pop("place", None)
        k = f"{row['test']}|{row['metric']}|{row['tool']}"
        cur = self.data["records"].get(k)
        if cur is None:
            self.data["records"][k] = row
            return
        if row["tool"] == SUBJECT:
            if run_id >= cur.get("run", ""):  # run ids are timestamps: an older run never wins
                self.data["records"][k] = row
            return
        if not row.get("ok"):
            return
        if not cur.get("ok"):
            self.data["records"][k] = row
            return
        better = row["value"] > cur["value"] if row["better"] == "higher" else row["value"] < cur["value"]
        if better:
            self.data["records"][k] = row

    def rows(self) -> list:
        return list(self.data["records"].values())


# ---------------------------------------------------------------------------------------------
# Memory pressure: pause instead of measuring a starved machine
# ---------------------------------------------------------------------------------------------

def free_memory_mib() -> int:
    """Available physical memory (GlobalMemoryStatusEx ullAvailPhys), in MiB."""
    import ctypes

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

    s = MEMORYSTATUSEX()
    s.dwLength = ctypes.sizeof(s)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(s))
    return s.ullAvailPhys >> 20


def wait_for_memory(min_mib=2048, get_free=free_memory_mib, sleep=None, log=print, pause_s=25 * 60):
    """Block while less than `min_mib` is available, re-checking every 25 minutes."""
    import time
    sleep = sleep or time.sleep
    while (free := get_free()) < min_mib:
        log(f"  low memory ({free} MiB available, want {min_mib}); waiting 25 min ...")
        sleep(pause_s)


def corruption_detected(m: dict) -> bool:
    """A corrupted archive counts as detected only when the tool exits with an error by itself;
    being killed at the time limit (e.g. waiting at a prompt) is not detection."""
    return failed(m.get("exit")) and not m.get("timed_out")
