//! End-to-end behaviour of the blazezip command line: archives round-trip byte-exactly through
//! every code path (small/whole/chunked/stored/zstd), stay readable by other tools, and corrupt
//! data is detected.

use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::{Duration, SystemTime};

const BZ: &str = env!("CARGO_BIN_EXE_blazezip");

fn bz(args: &[&str]) -> Output {
    Command::new(BZ).args(args).output().expect("run blazezip")
}

fn bz_ok(args: &[&str]) -> Output {
    let out = bz(args);
    assert!(
        out.status.success(),
        "blazezip {:?} failed:\n{}",
        args,
        String::from_utf8_lossy(&out.stderr)
    );
    out
}

/// A fresh, empty scratch directory unique to this test.
fn scratch(name: &str) -> PathBuf {
    static N: AtomicUsize = AtomicUsize::new(0);
    let d = std::env::temp_dir()
        .join("blazezip-tests")
        .join(format!("{}-{}-{}", name, std::process::id(), N.fetch_add(1, Ordering::Relaxed)));
    let _ = fs::remove_dir_all(&d);
    fs::create_dir_all(&d).unwrap();
    d
}

struct Rng(u64);
impl Rng {
    fn next(&mut self) -> u64 {
        self.0 ^= self.0 << 13;
        self.0 ^= self.0 >> 7;
        self.0 ^= self.0 << 17;
        self.0
    }
    fn bytes(&mut self, n: usize) -> Vec<u8> {
        (0..n).map(|_| self.next() as u8).collect()
    }
}

/// Compressible but not trivially so: words drawn from a small vocabulary.
fn text(rng: &mut Rng, n: usize) -> Vec<u8> {
    const WORDS: &[&str] = &["alpha ", "beta ", "gamma ", "delta\n", "epsilon ", "zeta ", "eta, ", "theta. "];
    let mut v = Vec::with_capacity(n + 16);
    while v.len() < n {
        v.extend_from_slice(WORDS[(rng.next() % WORDS.len() as u64) as usize].as_bytes());
    }
    v.truncate(n);
    v
}

fn write(p: &Path, data: &[u8]) {
    fs::create_dir_all(p.parent().unwrap()).unwrap();
    fs::write(p, data).unwrap();
}

/// A tree that exercises every create/extract path.
fn make_tree(root: &Path) {
    let mut rng = Rng(0x9e3779b97f4a7c15);
    write(&root.join("small.txt"), &text(&mut rng, 5_000));
    write(&root.join("empty.bin"), b"");
    write(&root.join("tiny.txt"), b"x");
    write(&root.join("nested/deeper/deepest/file.txt"), &text(&mut rng, 20_000));
    fs::create_dir_all(root.join("empty_dir/also_empty")).unwrap();
    write(&root.join("caf\u{e9}/na\u{ef}ve \u{65e5}\u{672c}.txt"), &text(&mut rng, 3_000));
    write(&root.join("random_small.bin"), &rng.bytes(300_000));
    write(&root.join("big_text.txt"), &text(&mut rng, 5_500_000));
    write(&root.join("big_random.bin"), &rng.bytes(3_000_000));
    for d in 0..12 {
        for f in 0..15 {
            let n = (rng.next() % 4000) as usize;
            write(&root.join(format!("many/d{d:02}/f{f:02}.txt")), &text(&mut rng, n));
        }
    }
}

/// Every file and directory under `root`, relative, with file contents.
fn snapshot(root: &Path) -> Vec<(String, Option<Vec<u8>>)> {
    let mut out = Vec::new();
    fn walk(base: &Path, p: &Path, out: &mut Vec<(String, Option<Vec<u8>>)>) {
        let mut items: Vec<_> = fs::read_dir(p).unwrap().map(|e| e.unwrap().path()).collect();
        items.sort();
        for i in items {
            let rel = i.strip_prefix(base).unwrap().to_string_lossy().replace('\\', "/");
            if i.is_dir() {
                out.push((rel + "/", None));
                walk(base, &i, out);
            } else {
                out.push((rel, Some(fs::read(&i).unwrap())));
            }
        }
    }
    walk(root, root, &mut out);
    out
}

fn assert_same_tree(a: &Path, b: &Path) {
    let (sa, sb) = (snapshot(a), snapshot(b));
    let names = |s: &[(String, Option<Vec<u8>>)]| s.iter().map(|x| x.0.clone()).collect::<Vec<_>>();
    assert_eq!(names(&sa), names(&sb), "entry lists differ");
    for (x, y) in sa.iter().zip(&sb) {
        assert!(x.1 == y.1, "content differs: {}", x.0);
    }
}

fn roundtrip(name: &str, opts: &[&str]) -> (PathBuf, PathBuf) {
    let d = scratch(name);
    let src = d.join("src");
    make_tree(&src);
    let zip = d.join("out.zip");
    let mut args = vec!["c", zip.to_str().unwrap(), src.to_str().unwrap(), "-q"];
    args.extend_from_slice(opts);
    bz_ok(&args);
    let dest = d.join("dest");
    bz_ok(&["x", zip.to_str().unwrap(), "-o", dest.to_str().unwrap(), "-q"]);
    assert_same_tree(&src, &dest.join("src"));
    bz_ok(&["t", zip.to_str().unwrap(), "-q"]);
    (d, zip)
}

fn methods(zip: &Path) -> Vec<(String, String, usize)> {
    let out = bz_ok(&["l", zip.to_str().unwrap()]);
    String::from_utf8_lossy(&out.stdout)
        .lines()
        .skip(1)
        .map(|l| {
            // size, compressed, method, chunks, then the name (which may contain spaces).
            let mut rest = l.trim_start();
            let mut f = Vec::new();
            for _ in 0..4 {
                let end = rest.find(' ').unwrap();
                f.push(&rest[..end]);
                rest = rest[end..].trim_start();
            }
            (rest.to_string(), f[2].to_string(), f[3].parse().unwrap())
        })
        .collect()
}

fn method_of(zip: &Path, suffix: &str) -> (String, usize) {
    let m = methods(zip);
    let e = m.iter().find(|e| e.0.ends_with(suffix)).unwrap_or_else(|| panic!("{suffix} not listed: {m:?}"));
    (e.1.clone(), e.2)
}

fn seven_zip() -> Option<PathBuf> {
    let p = PathBuf::from(r"C:\Program Files\7-Zip\7z.exe");
    p.exists().then_some(p)
}

#[test]
fn deflate_default_roundtrips() {
    let (_, zip) = roundtrip("deflate", &[]);
    // A 5.5 MB file is split into chunks; random data is detected and stored.
    let (method, chunks) = method_of(&zip, "big_text.txt");
    assert_eq!(method, "deflate");
    assert!(chunks > 1, "{chunks} chunks");
    assert_eq!(method_of(&zip, "big_random.bin").0, "store");
    assert_eq!(method_of(&zip, "random_small.bin").0, "store");
    assert_eq!(method_of(&zip, "small.txt").0, "deflate");
}

#[test]
fn deflate_fastest_roundtrips() {
    roundtrip("deflate1", &["-1"]);
}

#[test]
fn deflate_max_roundtrips() {
    roundtrip("deflate12", &["-12"]);
}

#[test]
fn store_roundtrips() {
    let (_, zip) = roundtrip("store", &["-0"]);
    assert_eq!(method_of(&zip, "big_text.txt").0, "store");
}

#[test]
fn zstd_roundtrips() {
    let (_, zip) = roundtrip("zstd", &["--zstd"]);
    let (method, chunks) = method_of(&zip, "big_text.txt");
    assert_eq!(method, "zstd");
    assert!(chunks > 1, "{chunks} chunks");
}

#[test]
fn no_store_compresses_everything() {
    let (_, zip) = roundtrip("nostore", &["--no-store"]);
    assert_eq!(method_of(&zip, "big_random.bin").0, "deflate");
}

#[test]
fn large_unchunked_entries_roundtrip() {
    // A 16 MiB chunk keeps the 5.5 MB file as one whole entry; with -t 1 everything is serial.
    roundtrip("bigwhole", &["--chunk", "16"]);
    let d = scratch("bigwhole-threads");
    let src = d.join("src");
    let mut rng = Rng(7);
    write(&src.join("ten_mb.txt"), &text(&mut rng, 10_000_000));
    let zip = d.join("o.zip");
    bz_ok(&["c", zip.to_str().unwrap(), src.to_str().unwrap(), "--chunk", "16", "-q"]);
    let dest = d.join("dest");
    bz_ok(&["x", zip.to_str().unwrap(), "-o", dest.to_str().unwrap(), "-q", "-t", "1"]);
    assert_same_tree(&src, &dest.join("src"));
}

#[test]
fn explicit_chunk_size_is_respected() {
    // --chunk 1 always means 1 MiB chunks, however small the input.
    let (_, zip) = roundtrip("chunk1", &["--chunk", "1"]);
    assert_eq!(method_of(&zip, "big_text.txt"), ("deflate".into(), 6));
}

#[test]
fn single_thread_roundtrips() {
    roundtrip("single", &["-t", "1"]);
}

#[test]
fn thread_count_does_not_change_the_archive() {
    // Only the speed may depend on -t: the chunk plan, and so every entry's compressed bytes,
    // must not. (The listing compares sizes, methods and chunk counts; directory timestamps can
    // still settle between runs on a freshly written tree.)
    let d = scratch("threads");
    let src = d.join("src");
    make_tree(&src);
    let build = |t: Option<&str>| {
        let zip = d.join(format!("t{}.zip", t.unwrap_or("default")));
        let mut args = vec!["c", zip.to_str().unwrap(), src.to_str().unwrap(), "-q"];
        if let Some(t) = t {
            args.extend_from_slice(&["-t", t]);
        }
        bz_ok(&args);
        String::from_utf8(bz_ok(&["l", zip.to_str().unwrap()]).stdout).unwrap()
    };
    let default = build(None);
    assert!(default.lines().any(|l| l.ends_with("big_text.txt") && !l.contains(" 1  ")), "big_text.txt should be chunked");
    for t in ["1", "8", "12", "20"] {
        assert_eq!(build(Some(t)), default, "-t {t} gives a different archive");
    }
}

#[test]
fn single_file_input_roundtrips() {
    let d = scratch("single-file");
    let f = d.join("one.txt");
    let mut rng = Rng(3);
    write(&f, &text(&mut rng, 2_500_000));
    let zip = d.join("o.zip");
    bz_ok(&["c", zip.to_str().unwrap(), f.to_str().unwrap(), "-q"]);
    let dest = d.join("dest");
    bz_ok(&["x", zip.to_str().unwrap(), "-o", dest.to_str().unwrap(), "-q"]);
    assert_eq!(fs::read(dest.join("one.txt")).unwrap(), fs::read(&f).unwrap());
}

#[test]
fn modification_times_are_preserved() {
    let d = scratch("mtime");
    let src = d.join("src");
    let mut rng = Rng(5);
    write(&src.join("a.txt"), &text(&mut rng, 1000));
    write(&src.join("big.txt"), &text(&mut rng, 3_000_000));
    write(&src.join("stored.bin"), &rng.bytes(2_000_000));
    let t = SystemTime::UNIX_EPOCH + Duration::from_secs(1_600_000_000);
    for f in ["a.txt", "big.txt", "stored.bin"] {
        fs::File::options().write(true).open(src.join(f)).unwrap().set_modified(t).unwrap();
    }
    let zip = d.join("o.zip");
    bz_ok(&["c", zip.to_str().unwrap(), src.to_str().unwrap(), "-q"]);
    let dest = d.join("dest");
    bz_ok(&["x", zip.to_str().unwrap(), "-o", dest.to_str().unwrap(), "-q"]);
    for f in ["a.txt", "big.txt", "stored.bin"] {
        assert_eq!(fs::metadata(dest.join("src").join(f)).unwrap().modified().unwrap(), t, "{f}");
    }
}

#[test]
fn corrupt_data_is_detected() {
    let d = scratch("corrupt");
    let src = d.join("src");
    let mut rng = Rng(11);
    write(&src.join("a.txt"), &text(&mut rng, 200_000));
    let zip = d.join("o.zip");
    bz_ok(&["c", zip.to_str().unwrap(), src.to_str().unwrap(), "-q", "-0"]);
    let mut bytes = fs::read(&zip).unwrap();
    // Flip a byte in the middle of the stored data.
    bytes[100_000] ^= 0xff;
    fs::write(&zip, &bytes).unwrap();
    let out = bz(&["t", zip.to_str().unwrap(), "-q"]);
    assert!(!out.status.success());
    assert!(String::from_utf8_lossy(&out.stderr).contains("CRC mismatch"));
    let out = bz(&["x", zip.to_str().unwrap(), "-o", d.join("dest").to_str().unwrap(), "-q"]);
    assert!(!out.status.success());
}

#[test]
fn corrupt_chunk_is_detected() {
    let d = scratch("corrupt-chunk");
    let src = d.join("src");
    let mut rng = Rng(13);
    write(&src.join("big.txt"), &text(&mut rng, 4_000_000));
    let zip = d.join("o.zip");
    bz_ok(&["c", zip.to_str().unwrap(), src.to_str().unwrap(), "-q"]);
    let mut bytes = fs::read(&zip).unwrap();
    let mid = bytes.len() / 2;
    bytes[mid] ^= 0x55;
    fs::write(&zip, &bytes).unwrap();
    for cmd in ["t", "x"] {
        let out = bz(&[cmd, zip.to_str().unwrap(), "-o", d.join("dest").to_str().unwrap(), "-q"]);
        assert!(!out.status.success(), "{cmd} accepted corrupt data");
    }
}

#[test]
fn archives_pass_7zip_integrity_test() {
    let Some(sz) = seven_zip() else { return };
    for opts in [&[][..], &["--zstd"][..], &["-0"][..], &["-12"][..]] {
        let d = scratch("7z");
        let src = d.join("src");
        make_tree(&src);
        let zip = d.join("o.zip");
        let mut args = vec!["c", zip.to_str().unwrap(), src.to_str().unwrap(), "-q"];
        args.extend_from_slice(opts);
        bz_ok(&args);
        if opts == ["--zstd"] {
            continue; // stock 7-Zip has no zstd-in-zip; NanaZip does
        }
        let out = Command::new(&sz).args(["t", zip.to_str().unwrap()]).output().unwrap();
        assert!(out.status.success(), "7z t failed for {opts:?}: {}", String::from_utf8_lossy(&out.stdout));
        // And 7-Zip's extraction matches the source.
        let dest = d.join("dest7");
        let out = Command::new(&sz)
            .args(["x", zip.to_str().unwrap(), &format!("-o{}", dest.display()), "-y"])
            .output()
            .unwrap();
        assert!(out.status.success());
        assert_same_tree(&src, &dest.join("src"));
    }
}

#[test]
fn extracts_archives_made_by_7zip() {
    let Some(sz) = seven_zip() else { return };
    let d = scratch("from7z");
    let src = d.join("src");
    make_tree(&src);
    let mut rng = Rng(17);
    write(&src.join("nine_mb.txt"), &text(&mut rng, 9_500_000));
    let zip = d.join("o.zip");
    let out = Command::new(&sz)
        .current_dir(&d)
        .args(["a", "-tzip", "-mx5", zip.to_str().unwrap(), "src"])
        .output()
        .unwrap();
    assert!(out.status.success());
    let dest = d.join("dest");
    bz_ok(&["x", zip.to_str().unwrap(), "-o", dest.to_str().unwrap(), "-q"]);
    assert_same_tree(&src, &dest.join("src"));
    bz_ok(&["t", zip.to_str().unwrap(), "-q"]);
}

#[test]
fn list_shows_every_entry() {
    let (d, zip) = roundtrip("list", &[]);
    let listed = methods(&zip).len();
    let on_disk = snapshot(&d.join("src")).len() + 1; // + the top-level "src/" directory
    assert_eq!(listed, on_disk);
}

#[test]
fn wide_directory_trees_roundtrip() {
    // More than 64 directories on one level, plus nesting, with and without files.
    let d = scratch("wide");
    let src = d.join("src");
    let mut rng = Rng(19);
    for i in 0..150 {
        let n = (rng.next() % 300) as usize;
        write(&src.join(format!("d{i:03}/sub/f.txt")), &text(&mut rng, n));
        fs::create_dir_all(src.join(format!("d{i:03}/empty"))).unwrap();
    }
    let zip = d.join("o.zip");
    bz_ok(&["c", zip.to_str().unwrap(), src.to_str().unwrap(), "-q"]);
    let dest = d.join("dest");
    bz_ok(&["x", zip.to_str().unwrap(), "-o", dest.to_str().unwrap(), "-q"]);
    assert_same_tree(&src, &dest.join("src"));
}

/// A minimal one-entry stored zip, for names blazezip itself would never write.
fn stored_zip(entries: &[(&str, &[u8])]) -> Vec<u8> {
    let mut out = Vec::new();
    let mut cd = Vec::new();
    for (name, data) in entries {
        let crc = crc32(data);
        let off = out.len() as u32;
        let mut h = Vec::new();
        h.extend_from_slice(&0x0403_4b50u32.to_le_bytes());
        h.extend_from_slice(&[10, 0, 0, 0, 0, 0, 0, 0, 0x21, 0]);
        h.extend_from_slice(&crc.to_le_bytes());
        h.extend_from_slice(&(data.len() as u32).to_le_bytes());
        h.extend_from_slice(&(data.len() as u32).to_le_bytes());
        h.extend_from_slice(&(name.len() as u16).to_le_bytes());
        h.extend_from_slice(&0u16.to_le_bytes());
        out.extend_from_slice(&h);
        out.extend_from_slice(name.as_bytes());
        out.extend_from_slice(data);
        cd.extend_from_slice(&0x0201_4b50u32.to_le_bytes());
        cd.extend_from_slice(&[20, 0]);
        cd.extend_from_slice(&h[4..]);
        cd.extend_from_slice(&[0; 6]); // comment length, disk, internal attributes
        cd.extend_from_slice(&0u32.to_le_bytes());
        cd.extend_from_slice(&off.to_le_bytes());
        cd.extend_from_slice(name.as_bytes());
    }
    let cd_off = out.len() as u32;
    out.extend_from_slice(&cd);
    out.extend_from_slice(&0x0605_4b50u32.to_le_bytes());
    out.extend_from_slice(&[0; 4]);
    out.extend_from_slice(&(entries.len() as u16).to_le_bytes());
    out.extend_from_slice(&(entries.len() as u16).to_le_bytes());
    out.extend_from_slice(&(cd.len() as u32).to_le_bytes());
    out.extend_from_slice(&cd_off.to_le_bytes());
    out.extend_from_slice(&[0; 2]);
    out
}

fn crc32(data: &[u8]) -> u32 {
    let mut c = !0u32;
    for &b in data {
        c ^= b as u32;
        for _ in 0..8 {
            c = if c & 1 != 0 { (c >> 1) ^ 0xEDB8_8320 } else { c >> 1 };
        }
    }
    !c
}

#[test]
fn entries_escaping_the_destination_are_skipped() {
    let d = scratch("slip");
    let zip = d.join("evil.zip");
    fs::write(
        &zip,
        stored_zip(&[("../escaped.txt", b"nope"), ("C:/abs.txt", b"nope"), ("ok/fine.txt", b"yes"), ("a/../../up.txt", b"nope")]),
    )
    .unwrap();
    let dest = d.join("dest");
    fs::create_dir_all(&dest).unwrap();
    bz_ok(&["x", zip.to_str().unwrap(), "-o", dest.to_str().unwrap(), "-q"]);
    assert_eq!(fs::read(dest.join("ok/fine.txt")).unwrap(), b"yes");
    assert!(!d.join("escaped.txt").exists());
    assert!(!d.join("up.txt").exists());
    assert_eq!(snapshot(&dest).len(), 2, "{:?}", snapshot(&dest).iter().map(|x| &x.0).collect::<Vec<_>>());
    // Testing an archive verifies every entry, whatever its name.
    bz_ok(&["t", zip.to_str().unwrap(), "-q"]);
}

#[test]
fn subsecond_times_and_attributes_are_preserved() {
    use std::os::windows::fs::MetadataExt;
    let d = scratch("attrs");
    let src = d.join("src");
    write(&src.join("timed.txt"), b"timed\n");
    write(&src.join("readonly.txt"), b"ro\n");
    let t = SystemTime::UNIX_EPOCH + Duration::from_nanos(1_600_000_001_234_567_800);
    fs::File::options().write(true).open(src.join("timed.txt")).unwrap().set_modified(t).unwrap();
    let mut perm = fs::metadata(src.join("readonly.txt")).unwrap().permissions();
    perm.set_readonly(true);
    fs::set_permissions(src.join("readonly.txt"), perm).unwrap();
    let zip = d.join("o.zip");
    bz_ok(&["c", zip.to_str().unwrap(), src.to_str().unwrap(), "-q"]);
    let dest = d.join("dest");
    for _ in 0..2 {
        // The second extraction overwrites the read-only file left by the first.
        bz_ok(&["x", zip.to_str().unwrap(), "-o", dest.to_str().unwrap(), "-q"]);
    }
    assert_eq!(fs::metadata(dest.join("src/timed.txt")).unwrap().modified().unwrap(), t);
    assert!(fs::metadata(dest.join("src/readonly.txt")).unwrap().file_attributes() & 1 != 0);
    for p in [src.join("readonly.txt"), dest.join("src/readonly.txt")] {
        let mut perm = fs::metadata(&p).unwrap().permissions();
        #[allow(clippy::permissions_set_readonly_false)]
        perm.set_readonly(false);
        fs::set_permissions(&p, perm).unwrap();
    }
}

#[test]
fn reading_the_inputs_does_not_change_the_next_archive() {
    // Windows may update a file's last-access time whenever it is read (including by blazezip
    // itself), so storing it would make two runs over unchanged files differ.
    let d = scratch("repro");
    let src = d.join("src");
    write(&src.join("a.txt"), b"same bytes
");
    let (z1, z2) = (d.join("1.zip"), d.join("2.zip"));
    bz_ok(&["c", z1.to_str().unwrap(), src.to_str().unwrap(), "-q"]);
    let later = SystemTime::now() + Duration::from_secs(7200);
    fs::File::options()
        .write(true)
        .open(src.join("a.txt"))
        .unwrap()
        .set_times(fs::FileTimes::new().set_accessed(later))
        .unwrap();
    bz_ok(&["c", z2.to_str().unwrap(), src.to_str().unwrap(), "-q"]);
    assert_eq!(fs::read(&z1).unwrap(), fs::read(&z2).unwrap());
}

#[test]
fn named_entries_extract_alone() {
    let (d, zip) = roundtrip("only", &[]);
    let dest = d.join("only");
    bz_ok(&["x", zip.to_str().unwrap(), "src/small.txt", "src/nested", "-o", dest.to_str().unwrap(), "-q"]);
    let got: Vec<String> = snapshot(&dest).into_iter().filter(|e| e.1.is_some()).map(|e| e.0).collect();
    assert_eq!(got, ["src/nested/deeper/deepest/file.txt", "src/small.txt"]);
    assert!(!bz(&["x", zip.to_str().unwrap(), "src/nope", "-o", dest.to_str().unwrap(), "-q"]).status.success());
}

#[test]
fn add_and_delete_update_an_archive() {
    let (d, zip) = roundtrip("update", &[]);
    let z = zip.to_str().unwrap();
    let extra = d.join("extra");
    write(&extra.join("new.txt"), &text(&mut Rng(9), 50_000));
    bz_ok(&["a", z, extra.to_str().unwrap(), "-q"]);
    bz_ok(&["d", z, "src/small.txt", "src/nested", "-q"]);
    bz_ok(&["t", z, "-q"]);
    let dest = d.join("dest2");
    bz_ok(&["x", z, "-o", dest.to_str().unwrap(), "-q"]);
    // Expected: the original tree minus the deleted entries, plus the added one.
    fs::remove_file(d.join("src/small.txt")).unwrap();
    fs::remove_dir_all(d.join("src/nested")).unwrap();
    assert_same_tree(&d.join("src"), &dest.join("src"));
    assert_eq!(fs::read(dest.join("extra/new.txt")).unwrap(), fs::read(extra.join("new.txt")).unwrap());
    // Copied entries keep their chunk index, so they still decode in parallel.
    assert!(method_of(&zip, "src/big_text.txt").1 > 1);
    assert!(!bz(&["d", z, "src/does-not-exist", "-q"]).status.success());
    if let Some(sz) = seven_zip() {
        assert!(Command::new(sz).args(["t", z]).output().unwrap().status.success());
    }
}

#[test]
fn truncated_archive_is_partly_recovered() {
    let d = scratch("trunc");
    let src = d.join("src");
    let mut rng = Rng(11);
    for i in 0..40 {
        write(&src.join(format!("f{i:02}.txt")), &text(&mut rng, 20_000));
    }
    let zip = d.join("o.zip");
    bz_ok(&["c", zip.to_str().unwrap(), src.to_str().unwrap(), "-q"]);
    let data = fs::read(&zip).unwrap();
    fs::write(&zip, &data[..data.len() / 2]).unwrap();
    let dest = d.join("dest");
    let out = bz(&["x", zip.to_str().unwrap(), "-o", dest.to_str().unwrap(), "-q"]);
    assert!(!out.status.success(), "a damaged archive must not report success");
    let recovered: Vec<_> = fs::read_dir(dest.join("src")).unwrap().collect();
    assert!(recovered.len() >= 10, "recovered only {}", recovered.len());
    for e in recovered {
        let p = e.unwrap().path();
        assert_eq!(fs::read(&p).unwrap(), fs::read(src.join(p.file_name().unwrap())).unwrap());
    }
}

#[test]
fn unreadable_files_are_skipped_and_reported() {
    use std::os::windows::fs::OpenOptionsExt;
    let d = scratch("locked");
    let src = d.join("src");
    for i in 0..5 {
        write(&src.join(format!("f{i}.txt")), b"content\n");
    }
    // No sharing: blazezip cannot open this file while the handle is held.
    let _lock = fs::OpenOptions::new().read(true).share_mode(0).open(src.join("f2.txt")).unwrap();
    let zip = d.join("o.zip");
    let out = bz(&["c", zip.to_str().unwrap(), src.to_str().unwrap(), "-q"]);
    assert!(!out.status.success());
    assert!(String::from_utf8_lossy(&out.stderr).contains("f2.txt"));
    let names: Vec<String> = methods(&zip).into_iter().map(|m| m.0).collect();
    assert_eq!(names, ["src/", "src/f0.txt", "src/f1.txt", "src/f3.txt", "src/f4.txt"]);
}

#[test]
fn failed_create_leaves_no_partial_archive() {
    let d = scratch("nopartial");
    let zip = d.join("o.zip");
    assert!(!bz(&["c", zip.to_str().unwrap(), d.join("missing").to_str().unwrap(), "-q"]).status.success());
    assert!(!zip.exists());
}

#[test]
fn version_flag_prints_the_version() {
    for flag in ["--version", "-V"] {
        let out = bz_ok(&[flag]);
        assert_eq!(String::from_utf8_lossy(&out.stdout).trim(), format!("blazezip {}", env!("CARGO_PKG_VERSION")));
    }
}

#[test]
fn help_documents_exit_codes() {
    let help = String::from_utf8(bz_ok(&["--help"]).stdout).unwrap();
    assert!(help.contains("EXIT CODES"), "{help}");
    assert!(help.contains("--version"), "{help}");
}

/// Damaged or hostile archives must fail with an error (exit 1), never crash: blazezip is built
/// with panic = "abort", so an out-of-bounds read while parsing would kill the process instead.
#[test]
fn corrupted_archives_never_crash() {
    let d = scratch("fuzz");
    let src = d.join("src");
    let mut rng = Rng(0x2545f4914f6cdd1d);
    write(&src.join("a.txt"), &text(&mut rng, 3_000));
    write(&src.join("dir/b.txt"), &text(&mut rng, 40_000));
    write(&src.join("big.txt"), &text(&mut rng, 600_000)); // chunked: carries the chunk index
    write(&src.join("rand.bin"), &rng.bytes(5_000));
    let zip = d.join("ok.zip");
    bz_ok(&["c", zip.to_str().unwrap(), src.to_str().unwrap(), "--chunk", "1", "-q"]);
    let good = fs::read(&zip).unwrap();
    // The central directory and end records sit at the end; aim most damage there.
    let tail = good.len().saturating_sub(600);
    let bad = d.join("bad.zip");
    let out = d.join("out");
    let mut crashes = Vec::new();
    for case in 0..400 {
        let mut b = good.clone();
        match case % 4 {
            0 | 1 => {
                for _ in 0..1 + rng.next() % 4 {
                    let i = tail + (rng.next() as usize % (b.len() - tail));
                    b[i] = rng.next() as u8;
                }
            }
            2 => {
                let i = rng.next() as usize % b.len();
                b[i] = rng.next() as u8;
                let j = tail + (rng.next() as usize % (b.len() - tail));
                b[j] ^= 0xff;
            }
            _ => b.truncate(rng.next() as usize % b.len()),
        }
        fs::write(&bad, &b).unwrap();
        let cmds: &[&[&str]] = &[&["l"], &["t", "-q"], &["x", "-q", "-o", out.to_str().unwrap()]];
        for cmd in cmds {
            let mut args = vec![cmd[0], bad.to_str().unwrap()];
            args.extend_from_slice(&cmd[1..]);
            let code = bz(&args).status.code();
            if !matches!(code, Some(0) | Some(1)) {
                let keep = d.join(format!("crash-{case}.zip"));
                fs::write(&keep, &b).unwrap();
                crashes.push(format!("case {case} {:?}: exit {code:?} (input kept as {})", cmd[0], keep.display()));
            }
        }
        let _ = fs::remove_dir_all(&out);
    }
    assert!(crashes.is_empty(), "{} crashes, e.g.:\n{}", crashes.len(), crashes[..crashes.len().min(10)].join("\n"));
}
