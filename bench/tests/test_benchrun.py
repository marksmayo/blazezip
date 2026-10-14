"""benchrun options used by the extended suite."""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BENCHRUN = ROOT / "target" / "release" / "benchrun.exe"


def run(*args):
    r = subprocess.run([str(BENCHRUN), *args], capture_output=True, text=True)
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_mem_limit_stops_a_job_that_needs_more():
    m = run("--mem-limit", "64", "--", sys.executable, "-c", "b = bytearray(300 << 20); b[::4096] = b'x' * len(b[::4096])")
    assert m["exit"] != 0
    assert m["mem_limit_mib"] == 64
    # (peak_job_commit still counts the refused request, so it is not checked here)


def test_mem_limit_lets_a_small_job_finish():
    m = run("--mem-limit", "256", "--", sys.executable, "-c", "b = bytearray(10 << 20)")
    assert m["exit"] == 0
