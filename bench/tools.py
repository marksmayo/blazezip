"""Tool adapters: how to drive each archiver/compressor from the command line.

Every tool is run with its own multithreading enabled where it has a switch for it, so each
competitor is measured at its best. Presets map each tool's native levels onto
fast / default / max.
"""

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
LOCAL = Path(os.environ.get("LOCALAPPDATA", ""))
PF = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
PF86 = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
PRESETS = ("fast", "default", "max")


def first(*cands) -> Optional[str]:
    for c in cands:
        if c is None:
            continue
        c = str(c)
        if os.path.isfile(c):
            return c
        w = shutil.which(c)
        if w:
            return w
    return None


def glob1(base: Path, pattern: str) -> Optional[Path]:
    hits = sorted(base.glob(pattern)) if base.exists() else []
    return hits[-1] if hits else None


@dataclass
class Cmd:
    argv: list
    cwd: Optional[str] = None
    stdout: Optional[str] = None  # redirect stdout into this file
    console: bool = False  # tool needs real console handles (WinZip's CLI hangs without them)


@dataclass
class Tool:
    key: str
    name: str
    color: str
    exe: Optional[str]
    note: str = ""
    # zip_create(preset, out_zip, src_parent, src_name) -> Cmd
    zip_create: Optional[Callable] = None
    zip_create_presets: tuple = PRESETS
    # zip_extract(zip, out_dir) -> Cmd
    zip_extract: Optional[Callable] = None
    # native_compress(preset, in_file, out_base) -> (Cmd, out_path)
    native_compress: Optional[Callable] = None
    native_presets: tuple = PRESETS
    native_format: str = ""
    # native_decompress(archive, out_dir, out_file) -> (Cmd, produced_file)
    native_decompress: Optional[Callable] = None
    version: str = ""
    available: bool = field(init=False, default=False)

    def __post_init__(self):
        self.available = self.exe is not None


def sevenzip_like(key, name, color, exe, note=""):
    lv = {"fast": "-mx1", "default": "-mx5", "max": "-mx9"}
    q = ["-y", "-bso0", "-bsp0", "-bse0"]
    return Tool(
        key, name, color, exe, note,
        zip_create=lambda p, out, cwd, src: Cmd([exe, "a", "-tzip", lv[p], "-mmt=on", *q, out, src], cwd=cwd),
        zip_extract=lambda z, d: Cmd([exe, "x", z, f"-o{d}", "-mmt=on", *q]),
        native_compress=lambda p, f, base: (Cmd([exe, "a", "-t7z", lv[p], "-mmt=on", *q, base + ".7z", f]), base + ".7z"),
        native_format="7z (LZMA2)",
        native_decompress=lambda a, d, name: (Cmd([exe, "x", a, f"-o{d}", "-mmt=on", *q]), os.path.join(d, name)),
    )


def discover() -> list:
    tools = []

    bz = first(ROOT / "target" / "release" / "blazezip.exe")
    # ZIP "max" is level 9, the strongest greedy/lazy level. Levels 10-12 (near-optimal parsing)
    # gain under 1% ratio for 4-8x the CPU, so they stay available on the CLI but are not the
    # benchmarked preset.
    blv = {"fast": ["-1"], "default": [], "max": ["-9"]}
    zlv = {"fast": ["-1"], "default": [], "max": ["-19"]}
    tools.append(Tool(
        "blazezip", "blazezip", "#ff5a1f", bz, "parallel Deflate (libdeflate); --zstd for native",
        zip_create=lambda p, out, cwd, src: Cmd([bz, "c", out, src, *blv[p], "-q"], cwd=cwd),
        zip_extract=lambda z, d: Cmd([bz, "x", z, "-o", d, "-q"]),
        native_compress=lambda p, f, base: (Cmd([bz, "c", base + ".zip", f, "--zstd", *zlv[p], "-q"]), base + ".zip"),
        native_format="zip + Zstandard",
        native_decompress=lambda a, d, name: (Cmd([bz, "x", a, "-o", d, "-q"]), os.path.join(d, name)),
    ))

    pz = first(TOOLS / "pzip" / "pzip.exe", Path.home() / "go" / "bin" / "pzip.exe")
    puz = first(TOOLS / "pzip" / "punzip.exe", Path.home() / "go" / "bin" / "punzip.exe")
    tools.append(Tool(
        "pzip", "pzip / punzip", "#8b5cf6", pz, "Go, parallel zip; single compression level",
        zip_create=lambda p, out, cwd, src: Cmd([pz, out, src], cwd=cwd),
        zip_create_presets=("default",),
        zip_extract=(lambda z, d: Cmd([puz, "-d", d, z])) if puz else None,
    ))

    tools.append(sevenzip_like("7zip", "7-Zip", "#2563eb", first(PF / "7-Zip" / "7z.exe", "7z")))
    tools.append(sevenzip_like("nanazip", "NanaZip", "#0ea5e9",
                               first(LOCAL / "Microsoft" / "WindowsApps" / "NanaZipC.exe", "NanaZipC")))
    tools.append(sevenzip_like("peazip", "PeaZip", "#16a34a",
                               first(PF / "PeaZip" / "res" / "bin" / "7z" / "7z.exe",
                                     LOCAL / "Programs" / "PeaZip" / "res" / "bin" / "7z" / "7z.exe"),
                               "runs PeaZip's bundled 7z engine (PeaZip's GUI front-end has no batch CLI)"))

    bzx = first(PF / "Bandizip" / "bz.exe")
    bandl = {"fast": "-l:1", "default": "-l:5", "max": "-l:9"}
    tools.append(Tool(
        "bandizip", "Bandizip", "#f59e0b", bzx, "bz.exe console tool",
        zip_create=lambda p, out, cwd, src: Cmd([bzx, "c", "-y", "-r", "-fmt:zip", bandl[p], out, src], cwd=cwd),
        zip_extract=lambda z, d: Cmd([bzx, "x", "-y", "-aoa", f"-o:{d}", z]),
    ))

    wr = first(PF / "WinRAR" / "WinRAR.exe", TOOLS / "winrar" / "WinRAR.exe")
    rar = first(PF / "WinRAR" / "Rar.exe", TOOLS / "winrar" / "Rar.exe")
    rlv = {"fast": "-m1", "default": "-m3", "max": "-m5"}
    tools.append(Tool(
        "winrar", "WinRAR", "#7c3aed", wr, "WinRAR.exe -afzip for ZIP, Rar.exe for RAR5",
        zip_create=lambda p, out, cwd, src: Cmd([wr, "a", "-afzip", rlv[p], "-r", "-ibck", "-y", out, src], cwd=cwd),
        zip_extract=lambda z, d: Cmd([wr, "x", "-ibck", "-y", "-o+", z, d + "\\"]),
        native_compress=(lambda p, f, base: (Cmd([rar, "a", rlv[p], "-ep1", "-y", "-idq", base + ".rar", f]), base + ".rar")) if rar else None,
        native_format="RAR5",
        native_decompress=(lambda a, d, name: (Cmd([rar, "x", "-y", "-idq", a, d + "\\"]), os.path.join(d, name))) if rar else None,
    ))

    wz = first(PF / "WinZip" / "wzzip.exe", PF86 / "WinZip" / "wzzip.exe")
    wuz = first(PF / "WinZip" / "wzunzip.exe", PF86 / "WinZip" / "wzunzip.exe")
    wlv = {"fast": "-ef", "default": "-en", "max": "-ex"}
    tools.append(Tool(
        "winzip", "WinZip", "#dc2626", wz, "WinZip Command Line Support Add-On",
        zip_create=lambda p, out, cwd, src: Cmd([wz, "-a", "-r", "-p", wlv[p], out, src + "\\*.*"], cwd=cwd, console=True),
        zip_extract=(lambda z, d: Cmd([wuz, "-d", "-o", "-ybc", z, d], console=True)) if wuz else None,
    ))

    izip = first("zip")
    iunzip = first("unzip")
    ilv = {"fast": "-1", "default": "-6", "max": "-9"}
    tools.append(Tool(
        "infozip", "Info-ZIP zip/unzip", "#64748b", izip or iunzip, "classic single-threaded reference",
        zip_create=(lambda p, out, cwd, src: Cmd([izip, "-r", "-q", ilv[p], out, src], cwd=cwd)) if izip else None,
        zip_extract=(lambda z, d: Cmd([iunzip, "-q", "-o", z, "-d", d])) if iunzip else None,
    ))

    wtar = first(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "tar.exe")
    tools.append(Tool(
        "wintar", "Windows built-in (tar)", "#94a3b8", wtar, "bsdtar shipped with Windows; baseline",
        zip_create=lambda p, out, cwd, src: Cmd([wtar, "-a", "-c", "-f", out, src], cwd=cwd),
        zip_create_presets=("default",),
        zip_extract=lambda z, d: Cmd([wtar, "-x", "-f", z, "-C", d]),
    ))

    zs = first(glob1(LOCAL / "Microsoft" / "WinGet" / "Packages", "Meta.Zstandard*/zstd-*/zstd.exe"), "zstd")
    zslv = {"fast": "-1", "default": "-3", "max": "-19"}
    tools.append(Tool(
        "zstd", "zstd", "#0d9488", zs, "multithreaded with -T0",
        native_compress=lambda p, f, base: (Cmd([zs, zslv[p], "-T0", "-q", "-f", f, "-o", base + ".zst"]), base + ".zst"),
        native_format="Zstandard",
        native_decompress=lambda a, d, name: (Cmd([zs, "-d", "-q", "-f", a, "-o", os.path.join(d, name)]), os.path.join(d, name)),
    ))

    pg = first(TOOLS / "pigz" / "pigz.exe")
    pglv = {"fast": "-1", "default": "-6", "max": "-9"}
    tools.append(Tool(
        "pigz", "pigz", "#db2777", pg, "parallel gzip (Cygwin build)",
        native_compress=lambda p, f, base: (Cmd([pg, pglv[p], "-c", f], stdout=base + ".gz"), base + ".gz"),
        native_format="gzip",
        native_decompress=lambda a, d, name: (Cmd([pg, "-d", "-c", a], stdout=os.path.join(d, name)), os.path.join(d, name)),
    ))

    l4 = first(TOOLS / "lz4" / "lz4.exe")
    l4lv = {"fast": "--fast=3", "default": "-1", "max": "-12"}
    tools.append(Tool(
        "lz4", "LZ4", "#ca8a04", l4, "multithreaded with -T0",
        native_compress=lambda p, f, base: (Cmd([l4, l4lv[p], "-T0", "-q", "-f", f, base + ".lz4"]), base + ".lz4"),
        native_format="LZ4 frame",
        native_decompress=lambda a, d, name: (Cmd([l4, "-d", "-q", "-f", a, os.path.join(d, name)]), os.path.join(d, name)),
    ))

    lf = first(TOOLS / "lzfse" / "lzfse.exe")
    tools.append(Tool(
        "lzfse", "LZFSE", "#4f46e5", lf, "Apple reference implementation (built from source); single level",
        native_compress=lambda p, f, base: (Cmd([lf, "-encode", "-i", f, "-o", base + ".lzfse"]), base + ".lzfse"),
        native_presets=("default",),
        native_format="LZFSE",
        native_decompress=lambda a, d, name: (Cmd([lf, "-decode", "-i", a, "-o", os.path.join(d, name)]), os.path.join(d, name)),
    ))

    for t in tools:
        t.version = probe_version(t)
    return tools


def probe_version(t: Tool) -> str:
    """Short version label, e.g. "7-Zip 26.04" or "v1.5.7"."""
    import re
    raw = _probe_version(t)
    raw = re.split(r"\s+:\s+|\(c\)|Copyright|, by |\*\*\*\s*$", raw.strip("* "))[0]
    return re.sub(r"\s+", " ", raw).strip(" ,-*")[:40]


def _probe_version(t: Tool) -> str:
    if not t.exe:
        return ""
    try:
        if t.key in ("7zip", "nanazip", "peazip"):
            out = subprocess.run([t.exe], capture_output=True, text=True, timeout=20).stdout
            line = next((l for l in out.splitlines() if l.strip()), "")
            return line.strip()[:60]
        args = {"blazezip": None, "zstd": ["-V"], "pigz": ["--version"], "lz4": ["-V"],
                "infozip": ["-v"], "wintar": ["--version"], "bandizip": [], "pzip": ["--version"]}.get(t.key)
        if args is None:
            if t.key == "blazezip":
                return "0.1.0 (this repo)"
            return file_version(t.exe)
        r = subprocess.run([t.exe, *args], capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL)
        out = (r.stdout + r.stderr).strip().splitlines()
        line = next((l for l in out if any(ch.isdigit() for ch in l)), out[0] if out else "")
        return line.strip()[:60]
    except Exception:
        return file_version(t.exe)


def file_version(exe: str) -> str:
    try:
        ps = f"(Get-Item '{exe}').VersionInfo.ProductVersion"
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=30)
        return r.stdout.strip()[:40]
    except Exception:
        return ""
