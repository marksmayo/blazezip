"""Benchmark corpora: download/prepare standard test data and build manifests for verification.

Corpora (all deterministic):
  silesia      Silesia compression corpus - the de-facto standard (12 files, 211 MB, mixed types)
  canterbury   Canterbury large corpus (3 files, 11 MB) - small-archive / startup-latency case
  enwik8       Hutter Prize Wikipedia dump (1 file, 100 MB) - one big file, tests intra-file parallelism
  linux        Linux 6.6 source subset (~25k files) - many small real-world source files
  binaries     Real Windows executables and DLLs (Rust toolchain) - typical "program folder"
  media        Incompressible data (pre-compressed media stand-in) - tests store/skip detection
"""

import hashlib
import io
import os
import random
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

# Silesia's home server (sun.aei.polsl.pl) is very slow; this GitHub mirror has one zip per file.
# Extracted sizes are checked against the published originals below.
SILESIA_MIRROR = "https://raw.githubusercontent.com/MiloszKrajewski/SilesiaCorpus/master/{}.zip"
SILESIA_SIZES = {
    "dickens": 10192446, "mozilla": 51220480, "mr": 9970564, "nci": 33553445, "ooffice": 6152192,
    "osdb": 10085684, "reymont": 6627202, "samba": 21606400, "sao": 7251944, "webster": 41458703,
    "x-ray": 8474240, "xml": 5345280,
}

DOWNLOADS = {
    "cantrbry-large.zip": "https://corpus.canterbury.ac.nz/resources/large.zip",
    "enwik8.zip": "http://mattmahoney.net/dc/enwik8.zip",
    "linux.tar.xz": "https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-6.6.tar.xz",
}

LINUX_DIRS = ("linux-6.6/kernel/", "linux-6.6/fs/", "linux-6.6/net/", "linux-6.6/include/",
              "linux-6.6/arch/x86/", "linux-6.6/Documentation/", "linux-6.6/lib/", "linux-6.6/mm/")

CORPORA = {
    "silesia": "Silesia corpus — the standard compression benchmark (12 mixed files, 211 MB)",
    "canterbury": "Canterbury large corpus (3 files, 11 MB) — small-archive latency",
    "enwik8": "enwik8 Wikipedia dump (1 file, 100 MB) — single large file",
    "linux": "Linux 6.6 source subset — tens of thousands of small source files",
    "binaries": "Windows executables & DLLs (Rust toolchain) — a typical program folder",
    "media": "Incompressible media-like data (32 files, 320 MB)",
}


def download(dl: Path, name: str, url: str = None):
    dest = dl / name
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    print(f"  downloading {name} ...", flush=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(url or DOWNLOADS[name]) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f, 1 << 20)
    tmp.rename(dest)
    return dest


def unzip_to(zpath: Path, dest: Path):
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zpath) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            name = Path(info.filename).name  # flatten (silesia/canterbury zips are flat anyway)
            with z.open(info) as src, open(dest / name, "wb") as out:
                shutil.copyfileobj(src, out, 1 << 20)


def prep_linux(dl: Path, dest: Path):
    src = download(dl, "linux.tar.xz")
    dest.mkdir(parents=True)
    seen = set()
    n = 0
    with tarfile.open(src, "r:xz") as t:
        for m in t:
            if not m.isfile() or not m.name.startswith(LINUX_DIRS):
                continue
            rel = m.name[len("linux-6.6/"):]
            key = rel.lower()
            if key in seen:  # case-only collisions can't coexist on NTFS
                continue
            seen.add(key)
            out = dest / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            with t.extractfile(m) as f, open(out, "wb") as o:
                shutil.copyfileobj(f, o)
            n += 1
    print(f"  linux: {n} files")


def prep_binaries(dest: Path):
    tc = Path.home() / ".rustup" / "toolchains"
    roots = sorted(tc.glob("stable-*")) or sorted(tc.iterdir())
    root = roots[0]
    dest.mkdir(parents=True)
    budget = 400 << 20
    used = 0
    files = sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in (".exe", ".dll", ".rlib", ".lib", ".pdb"))
    for p in files:
        sz = p.stat().st_size
        if used + sz > budget:
            continue
        out = dest / p.relative_to(root)
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(p, out)
        used += sz
    print(f"  binaries: {used >> 20} MiB")


def prep_media(dest: Path):
    dest.mkdir(parents=True)
    rng = random.Random(42)
    for i in range(32):
        size = 10 << 20
        with open(dest / f"IMG_{1000 + i}.jpg", "wb") as f:
            # A JPEG-ish header followed by high-entropy data, like real photos/videos.
            f.write(b"\xff\xd8\xff\xe0\x00\x10JFIF\x00")
            f.write(rng.randbytes(size - 11))


def ensure_corpora(work: Path, names):
    dl = work / "dl"
    dl.mkdir(parents=True, exist_ok=True)
    cdir = work / "corpora"
    for name in names:
        dest = cdir / name
        if dest.exists():
            continue
        print(f"preparing corpus {name}", flush=True)
        tmp = cdir / (name + ".tmp")
        shutil.rmtree(tmp, ignore_errors=True)
        if name == "silesia":
            for f in SILESIA_SIZES:
                unzip_to(download(dl, f"silesia/{f}.zip", SILESIA_MIRROR.format(f)), tmp)
            for f, size in SILESIA_SIZES.items():
                got = (tmp / f).stat().st_size if (tmp / f).exists() else None
                if got != size:
                    raise SystemExit(f"silesia/{f}: size {got} != published {size}")
        elif name == "canterbury":
            unzip_to(download(dl, "cantrbry-large.zip"), tmp)
        elif name == "enwik8":
            unzip_to(download(dl, "enwik8.zip"), tmp)
        elif name == "linux":
            prep_linux(dl, tmp)
        elif name == "binaries":
            prep_binaries(tmp)
        elif name == "media":
            prep_media(tmp)
        else:
            raise SystemExit(f"unknown corpus {name}")
        tmp.rename(dest)
    return {n: cdir / n for n in names}


def make_tar(corpus_dir: Path, tar_path: Path):
    """Single-file input for stream compressors (zstd, pigz, lz4, lzfse ...)."""
    if tar_path.exists():
        return tar_path
    tar_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = tar_path.with_suffix(".tmp")
    with tarfile.open(tmp, "w", format=tarfile.GNU_FORMAT) as t:
        for p in sorted(corpus_dir.rglob("*")):
            if p.is_file():
                t.add(p, arcname=str(p.relative_to(corpus_dir.parent)).replace("\\", "/"), recursive=False)
    tmp.rename(tar_path)
    return tar_path


def file_hash(p: Path) -> str:
    h = hashlib.blake2b(digest_size=16)
    with open(p, "rb") as f:
        while chunk := f.read(1 << 22):
            h.update(chunk)
    return h.hexdigest()


def manifest(root: Path) -> dict:
    """{relative path (lowercase, '/'): (size, hash)} for every file under root."""
    out = {}
    for p in root.rglob("*"):
        if p.is_file():
            out[str(p.relative_to(root)).replace("\\", "/").lower()] = (p.stat().st_size, file_hash(p))
    return out


def stats(root: Path):
    files = [p for p in root.rglob("*") if p.is_file()]
    return len(files), sum(p.stat().st_size for p in files)
