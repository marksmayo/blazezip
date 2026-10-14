"""Unit tests for the extended suite's helpers: archive crafting, scoring rules, statistics,
test-data generators and the cross-run leaderboard."""

import os
import sqlite3
import subprocess
import zipfile
import zlib
from pathlib import Path

import pytest

import ext_more as em
import extended


# ----- crafted archives ------------------------------------------------------------------------

def test_build_zip_round_trips_through_zipfile(tmp_path):
    z = tmp_path / "a.zip"
    z.write_bytes(em.build_zip([em.ZEntry("a.txt", b"hello"), em.ZEntry("dir/b.bin", bytes(1000), method=0)],
                               comment=b"archive comment"))
    with zipfile.ZipFile(z) as f:
        assert f.namelist() == ["a.txt", "dir/b.bin"]
        assert f.read("a.txt") == b"hello"
        assert f.read("dir/b.bin") == bytes(1000)
        assert f.getinfo("a.txt").compress_type == zipfile.ZIP_DEFLATED
        assert f.comment == b"archive comment"
        assert f.testzip() is None


def test_build_zip_flags_non_ascii_names_as_utf8_unless_raw_bytes(tmp_path):
    z = tmp_path / "a.zip"
    sjis = "日本語.txt".encode("shift_jis")
    z.write_bytes(em.build_zip([em.ZEntry("café.txt", b"x"), em.ZEntry(sjis, b"y")]))
    with zipfile.ZipFile(z) as f:
        infos = f.infolist()
        assert infos[0].flag_bits & 0x800 and infos[0].filename == "café.txt"
        assert not infos[1].flag_bits & 0x800
    with zipfile.ZipFile(z, metadata_encoding="shift_jis") as f:
        assert f.namelist()[1] == "日本語.txt"


def test_build_zip_overrides_crc_and_declared_size(tmp_path):
    z = tmp_path / "a.zip"
    z.write_bytes(em.build_zip([em.ZEntry("bad.txt", b"payload" * 100, crc=0x12345678),
                                em.ZEntry("liar.bin", bytes(100_000), usize=1000)]))
    with zipfile.ZipFile(z) as f:
        assert f.getinfo("bad.txt").CRC == 0x12345678
        assert f.getinfo("liar.bin").file_size == 1000
        assert f.testzip() == "bad.txt"


def test_build_zip_unix_symlink_entry(tmp_path):
    z = tmp_path / "a.zip"
    z.write_bytes(em.build_zip([em.ZEntry("target.txt", b"t"),
                                em.ZEntry("link", b"target.txt", create_system=3, ext_attr=0o120777 << 16)]))
    with zipfile.ZipFile(z) as f:
        i = f.getinfo("link")
        assert i.create_system == 3 and (i.external_attr >> 16) & 0o170000 == 0o120000


def test_build_zip_entry_comments(tmp_path):
    z = tmp_path / "a.zip"
    z.write_bytes(em.build_zip([em.ZEntry("a.txt", b"a", comment=b"per-file")]))
    with zipfile.ZipFile(z) as f:
        assert f.getinfo("a.txt").comment == b"per-file"


def test_overlap_bomb_shares_one_local_header(tmp_path):
    z = tmp_path / "bomb.zip"
    z.write_bytes(em.overlap_bomb(n=50, size=1 << 20))
    assert z.stat().st_size < 20_000
    with zipfile.ZipFile(z) as f:
        infos = f.infolist()
        assert len(infos) == 50
        assert {i.header_offset for i in infos} == {0}
        assert sum(i.file_size for i in infos) == 50 << 20


# ----- scoring rules ---------------------------------------------------------------------------

def test_find_markers_searches_every_file(tmp_path):
    (tmp_path / "x").mkdir()
    (tmp_path / "x" / "renamed_aux.h").write_bytes(b"MARK-aux")
    (tmp_path / "con_.txt").write_bytes(b"MARK-con")
    found = em.find_markers(tmp_path, {"aux": b"MARK-aux", "con": b"MARK-con", "nul": b"MARK-nul"})
    assert found == {"aux", "con"}


@pytest.mark.parametrize("found,exit,score", [({"a", "b"}, 0, 2), ({"a"}, 1, 1), ({"a"}, 0, 0), (set(), 2, 1)])
def test_score_case_collision(found, exit, score):
    assert em.score_case_collision(found, {"a", "b"}, exit) == score


@pytest.mark.parametrize("new,exit,score", [(True, 0, 2), (False, 1, 1), (False, 0, 0)])
def test_score_readonly_overwrite(new, exit, score):
    assert em.score_readonly_overwrite(new, exit) == score


@pytest.mark.parametrize("written,exit,score", [(0, 1, 1), (50 << 20, 0, 1), (900 << 20, 0, 0), (1 << 30, 2, 0)])
def test_score_bomb_blocks_when_under_a_tenth_written(written, exit, score):
    assert em.score_bomb(written, 1 << 30) == score


@pytest.mark.parametrize("written,exit,score", [(1000, 0, 1), (100_000, 1, 1), (100_000, 0, 0)])
def test_score_declared_size(written, exit, score):
    assert em.score_declared_size(written, declared=1000, exit_code=exit) == score


@pytest.mark.parametrize("hung,exit,entries,score", [(False, 0, 12, 2), (False, 3, 0, 1), (True, None, 0, 0),
                                                     (False, 0, 50_000, 0)])
def test_score_junction_loop(hung, exit, entries, score):
    assert em.score_junction_loop(hung, exit, entries) == score


# ----- statistics ------------------------------------------------------------------------------

def test_hypervolume_of_pareto_points():
    # Points (wall, ratio) inside reference box (10, 100): area each point dominates, unioned.
    assert em.hypervolume([(5, 50)], (10, 100)) == pytest.approx(5 * 50)
    assert em.hypervolume([(2, 80), (5, 50)], (10, 100)) == pytest.approx(8 * 20 + 5 * 30)
    # Dominated and out-of-box points add nothing.
    assert em.hypervolume([(2, 80), (5, 50), (6, 60), (20, 10)], (10, 100)) == pytest.approx(8 * 20 + 5 * 30)
    assert em.hypervolume([], (10, 100)) == 0


def test_best_within_budget():
    pts = [(0.5, 60), (2, 40), (9, 30)]
    assert em.best_within(pts, 1) == 60
    assert em.best_within(pts, 3) == 40
    assert em.best_within(pts, 0.1) is None


def test_percentile():
    assert em.percentile(list(range(1, 101)), 99) == pytest.approx(99.01)
    assert em.percentile([5], 99) == 5


# ----- junctions -------------------------------------------------------------------------------

def test_junction_loop_is_created_and_removed_without_following(tmp_path):
    root = tmp_path / "tree"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "f.txt").write_bytes(b"f")
    em.make_junction(root / "sub" / "loop", root)
    assert em.is_junction(root / "sub" / "loop")
    assert (root / "sub" / "loop" / "sub" / "f.txt").exists()
    em.remove_tree(root)
    assert not root.exists()
    assert tmp_path.exists()


def test_remove_tree_keeps_junction_target(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_bytes(b"k")
    tree = tmp_path / "tree"
    tree.mkdir()
    em.make_junction(tree / "j", outside)
    em.remove_tree(tree)
    assert (outside / "keep.txt").exists()


# ----- generators ------------------------------------------------------------------------------

def test_gen_logs_is_deterministic_json_lines(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    em.gen_logs(a, 1)
    em.gen_logs(b, 1)
    fa = sorted(a.rglob("*.jsonl"))
    assert fa and sum(f.stat().st_size for f in fa) >= 1 << 20
    assert [f.read_bytes() for f in fa] == [f.read_bytes() for f in sorted(b.rglob("*.jsonl"))]
    import json
    json.loads(fa[0].read_text().splitlines()[0])


def test_gen_sqlite_makes_a_database(tmp_path):
    em.gen_sqlite(tmp_path, 2)
    db = tmp_path / "app.db"
    assert db.stat().st_size >= 2 << 20
    with sqlite3.connect(db) as c:
        assert c.execute("select count(*) from events").fetchone()[0] > 1000


def test_gen_office_makes_zip_containers(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    for i in range(3):
        (src / f"t{i}.txt").write_bytes(b"lorem ipsum " * 5000)
    out = tmp_path / "docs"
    out.mkdir()
    em.gen_office(out, src)
    docs = sorted(out.glob("*.docx"))
    assert len(docs) == 3
    with zipfile.ZipFile(docs[0]) as z:
        assert "word/document.xml" in z.namelist()
    pdfs = sorted(out.glob("*.pdf"))
    assert pdfs and pdfs[0].read_bytes().startswith(b"%PDF-")


def test_gen_git_repo_commits_the_tree(tmp_path):
    src = tmp_path / "src"
    (src / "d").mkdir(parents=True)
    (src / "d" / "a.c").write_bytes(b"int main(){}\n")
    repo = tmp_path / "repo"
    repo.mkdir()
    em.gen_git_repo(repo, [src / "d" / "a.c"], src)
    assert (repo / ".git").is_dir()
    log = subprocess.run(["git", "-C", str(repo), "log", "--oneline"], capture_output=True, text=True).stdout
    assert log.strip()


def test_footprint_counts_exe_and_local_dlls(tmp_path):
    (tmp_path / "tool.exe").write_bytes(bytes(1000))
    (tmp_path / "helper.dll").write_bytes(bytes(500))
    (tmp_path / "readme.txt").write_bytes(bytes(9999))
    assert em.footprint(str(tmp_path / "tool.exe")) == 1500


def test_footprint_of_a_system32_tool_is_the_exe_alone(tmp_path):
    exe = Path(os.environ["SystemRoot"]) / "System32" / "tar.exe"
    assert em.footprint(str(exe)) == exe.stat().st_size


# ----- levels ----------------------------------------------------------------------------------

def test_levels_for_known_tools():
    import tools as toolsmod
    ts = {t.key: t for t in toolsmod.discover()}
    lv = em.levels_for(ts["7zip"])
    assert list(lv) == [str(i) for i in range(10)]
    cmd = lv["9"]("out.zip", "C:\\x", "src")
    assert "-mx9" in cmd.argv
    assert list(em.levels_for(ts["blazezip"])) == [str(i) for i in range(0, 13)]
    assert em.levels_for(ts["pzip"]) == {}


# ----- ranking and the cross-run board --------------------------------------------------------

def row(tool, value, better="lower", test="M90", metric="m", ok=True):
    return {"test": test, "metric": metric, "tool": tool, "value": value, "unit": "", "better": better,
            "note": "", "ok": ok and value is not None}


def test_rank_awards_no_medals_when_every_tool_ties():
    rows = [row("a", 0, "higher"), row("b", 0, "higher"), row("c", 0, "higher")]
    extended.rank(rows)
    assert all(r.get("place") is None for r in rows)


def test_rank_still_shares_places_on_partial_ties():
    rows = [row("a", 1, "higher"), row("b", 1, "higher"), row("c", 0, "higher")]
    extended.rank(rows)
    assert [r["place"] for r in rows] == [1, 1, 3]


def test_board_keeps_competitors_best_and_subject_latest(tmp_path):
    b = em.ExtBoard(tmp_path / "board.json")
    b.add(row("7zip", 2.0), "r1")
    b.add(row("7zip", 3.0), "r2")
    b.add(row("7zip", None, ok=False), "r3")
    b.add(row("blazezip", 1.0), "r1")
    b.add(row("blazezip", 1.5), "r2")
    b.add(row("winrar", 5, "higher"), "r1")
    b.add(row("winrar", 7, "higher"), "r2")
    got = {r["tool"]: r for r in b.rows()}
    assert got["7zip"]["value"] == 2.0 and got["7zip"]["run"] == "r1"
    assert got["blazezip"]["value"] == 1.5
    assert got["winrar"]["value"] == 7


def test_board_records_subject_failures_and_persists(tmp_path):
    p = tmp_path / "board.json"
    b = em.ExtBoard(p)
    b.add(row("blazezip", 1.0), "r1")
    b.add(row("blazezip", None, ok=False), "r2")
    b.save()
    got = em.ExtBoard(p).rows()
    assert got[0]["value"] is None and got[0]["run"] == "r2"


def test_board_drops_a_tool_whose_version_changed(tmp_path):
    b = em.ExtBoard(tmp_path / "board.json")
    b.set_versions({"7zip": "26.04"})
    b.add(row("7zip", 1.0), "r1")
    b.set_versions({"7zip": "26.05"})
    assert b.rows() == []


def test_board_keeps_not_applicable_rows(tmp_path):
    b = em.ExtBoard(tmp_path / "board.json")
    r = row("wintar", None, ok=False)
    r["na"] = True
    b.add(r, "r1")
    assert b.rows()[0]["na"]


def test_rank_skips_informational_rows():
    rows = [row("a", 1), row("b", 2)]
    for r in rows:
        r["info"] = True
    extended.rank(rows)
    assert all(r.get("place") is None for r in rows)


def test_ops_have_update_and_password_extract_where_the_cli_allows():
    import tools as toolsmod
    ts = {t.key: t for t in toolsmod.discover()}
    seven = extended.ops_for(ts["7zip"])
    assert "-pSecret" in seven["extract_pw"]("a.zip", "out", "Secret").argv
    assert seven["update"]("a.zip", "tree", "C:\\").argv[1] == "u"
    assert "extract_pw" not in extended.ops_for(ts["blazezip"])
    assert "update" not in extended.ops_for(ts["wintar"])


def test_wait_for_memory_sleeps_25_min_until_enough_is_free():
    free = iter([900, 1500, 4000])
    slept = []
    em.wait_for_memory(2048, get_free=lambda: next(free), sleep=slept.append, log=lambda *a: None)
    assert slept == [25 * 60, 25 * 60]


def test_wait_for_memory_returns_at_once_when_enough_is_free():
    slept = []
    em.wait_for_memory(2048, get_free=lambda: 8000, sleep=slept.append, log=lambda *a: None)
    assert slept == []


def test_free_memory_mib_reads_the_system():
    assert 0 < em.free_memory_mib() < 1 << 24


def test_gen_sqlite_leaves_the_database_closed(tmp_path):
    em.gen_sqlite(tmp_path, 1)
    # Another program must be able to open it exclusively (no handle left open by the generator).
    import ctypes
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateFileW.restype = ctypes.c_void_p
    h = k.CreateFileW(str(tmp_path / "app.db"), 0x80000000, 0, None, 3, 0, None)
    assert h not in (None, ctypes.c_void_p(-1).value)
    k.CloseHandle(ctypes.c_void_p(h))


def test_import_runs_merges_earlier_runs_oldest_first(tmp_path):
    import json
    for name, val in (("ext-20261011-000000", 2.0), ("ext-20261012-000000", 9.0)):
        d = tmp_path / name
        d.mkdir()
        (d / "extended.json").write_text(json.dumps({"meta": {}, "rows": [row("7zip", val), row("blazezip", val)]}))
    b = em.ExtBoard(tmp_path / "board.json")
    extended.import_runs(b, [tmp_path / "ext-20261012-000000", tmp_path / "ext-20261011-000000"])
    got = {r["tool"]: r for r in b.rows()}
    assert got["7zip"]["value"] == 2.0  # competitor: best of both
    assert got["blazezip"]["value"] == 9.0 and got["blazezip"]["run"] == "ext-20261012-000000"  # subject: newest


def test_board_never_replaces_the_subjects_newer_run_with_an_older_one(tmp_path):
    b = em.ExtBoard(tmp_path / "board.json")
    b.add(row("blazezip", 1.0), "ext-20261012-013200")
    b.add(row("blazezip", 5.0), "ext-20261011-204731")
    assert b.rows()[0]["value"] == 1.0


@pytest.mark.parametrize("m,detected", [({"exit": 2}, True), ({"exit": 0}, False),
                                        ({"exit": -1, "timed_out": True}, False)])
def test_corruption_counts_only_a_clean_failure_as_detected(m, detected):
    assert em.corruption_detected(m) is detected


def test_ctx_keeps_7zip_as_reference_when_runs_are_limited_to_other_tools():
    import argparse
    import tools as toolsmod
    all_tools = toolsmod.discover()
    only_bz = [t for t in all_tools if t.key == "blazezip"]
    args = argparse.Namespace(work=r"C:\bzbench", run_dir=None)
    ctx = extended.Ctx(args, only_bz, reference_tools=all_tools)
    assert ctx.sevenzip is not None and ctx.sevenzip.key == "7zip"
    assert [t.key for t in ctx.tools] == ["blazezip"]


# ----- generated data is warm before it is measured ----------------------------------------------

def test_warm_files_reads_every_generated_file(tmp_path):
    for i in range(30):
        (tmp_path / f"d{i % 3}").mkdir(exist_ok=True)
        (tmp_path / f"d{i % 3}" / f"f{i}.txt").write_bytes(b"x" * i)
    assert extended.warm_files(tmp_path) == 30


def test_ensure_warms_freshly_generated_data(tmp_path, monkeypatch):
    # The first read of a just-written file can cost milliseconds (on-access scanning); it must
    # not be charged to whichever tool happens to be measured first.
    warmed = []
    monkeypatch.setattr(extended, "warm_files", lambda p: warmed.append(p) or 0)
    ctx = type("Ctx", (), {"data": tmp_path})()
    p = extended.ensure(ctx, "gen", lambda d: (d / "a.txt").write_text("a"))
    assert warmed == [p]
    extended.ensure(ctx, "gen", lambda d: None)  # already generated: nothing to warm again
    assert warmed == [p]
