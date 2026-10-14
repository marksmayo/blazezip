//! Parallel extraction.
//!
//! The archive is memory-mapped and the central directory parsed once. All directories are
//! created up front (one depth level at a time, in parallel), then files are decoded and written
//! by a pool of threads (file creation is the bottleneck for many small files on NTFS, and it
//! parallelises well). Large entries that carry a blazezip chunk index are split so every core
//! decodes part of the same file. Output is written with WriteFile from reusable per-thread
//! buffers rather than through mapped views, and file handles are closed on separate threads
//! because closing a freshly written file is where on-access antivirus scanning blocks.

use crate::codec::{self, Crc};
use crate::zipfmt::*;
use anyhow::{bail, Context, Result};
use memmap2::{Mmap, MmapMut};
use std::collections::HashSet;
use std::fs::{File, OpenOptions};
use std::io::Write;
use std::os::windows::fs::FileExt;
use std::path::{Component, Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{mpsc, Mutex, OnceLock};

pub struct ExtractOpts {
    pub threads: usize,
    /// Decode and verify only, write nothing (`blazezip t`).
    pub test_only: bool,
    pub quiet: bool,
    /// Extract only these entries (and everything under these directories); empty means all.
    pub only: Vec<String>,
}

/// Outputs at least this large are written through a memory map instead of a heap buffer.
/// (Decoding into a buffer and writing it with WriteFile was measured slower: out-of-order
/// writes beyond the valid data length serialise in NTFS, and it costs a buffer per thread.)
const MMAP_OUT_THRESHOLD: u64 = 8 << 20;

/// Large stored entries are copied with ReadFile/WriteFile through a buffer of this size.
const STREAM_STORED_THRESHOLD: u64 = 1 << 20;
const COPY_BUF: usize = 1 << 20;

/// Shared state of an entry decoded in parallel chunks.
struct Split {
    entry: usize,
    map: Option<MmapMut>,
    /// Created (and sized) up front; used for the final timestamp.
    file: Option<File>,
    chunk: u64,
    usize: u64,
    remaining: AtomicUsize,
    crcs: Vec<OnceLock<u32>>,
}

enum Job {
    Whole(usize),
    Chunk { entry: usize, index: usize, comp_off: u64, comp_len: usize, out_off: u64, out_len: usize, split: usize },
}

/// Per-worker reusable state.
#[derive(Default)]
struct Local {
    out: Vec<u8>,
    comp: Vec<u8>,
}

pub fn extract(zip: &Path, dest: &Path, opts: &ExtractOpts) -> Result<()> {
    let file = File::open(zip).with_context(|| format!("opening {}", zip.display()))?;
    let map = unsafe { Mmap::map(&file)? };
    let (mut entries, recovered) = match parse_central_directory(&map) {
        Ok(e) => (e, false),
        Err(err) => {
            // A truncated or damaged archive: salvage what the local headers still describe.
            let found = scan_local_headers(&map);
            if found.is_empty() {
                return Err(err);
            }
            eprintln!("blazezip: warning: {err:#}; recovering {} entries from local headers", found.len());
            (found, true)
        }
    };
    if !opts.only.is_empty() {
        let only: Vec<String> = opts.only.iter().map(|n| n.replace('\\', "/").trim_end_matches('/').to_string()).collect();
        entries.retain(|e| {
            let name = e.name.trim_end_matches('/');
            only.iter().any(|n| name == n || name.strip_prefix(n.as_str()).is_some_and(|r| r.starts_with('/')))
        });
        anyhow::ensure!(!entries.is_empty(), "no entries match {}", only.join(", "));
    }
    let buf: &[u8] = &map;

    if let Some(e) = entries.iter().find(|e| e.flags & 1 != 0) {
        bail!("{}: encrypted entries are not supported", e.name);
    }
    let paths: Vec<Option<PathBuf>> = if opts.test_only {
        Vec::new()
    } else {
        let paths: Vec<Option<PathBuf>> = entries.iter().map(|e| safe_path(dest, &e.name)).collect();
        make_dirs(dest, &entries, &paths, opts.threads)?;
        paths
    };
    let wanted = |i: usize| !entries[i].is_dir() && (opts.test_only || paths[i].is_some());

    // Plan jobs. Chunk-indexed entries go first, then whole entries largest first, so the big
    // ones don't become the long tail.
    let mut splits: Vec<Split> = Vec::new();
    let mut jobs: Vec<Job> = Vec::new();
    let mut whole: Vec<usize> = Vec::new();
    for (i, e) in entries.iter().enumerate() {
        if !wanted(i) {
            continue;
        }
        match &e.chunks {
            Some((chunk, sizes)) if sizes.len() > 1 && e.method != METHOD_STORE => {
                let (map, file) = if opts.test_only {
                    (None, None)
                } else {
                    let f = open_out(paths[i].as_ref().unwrap(), e.usize)?;
                    let m = unsafe { MmapMut::map_mut(&f)? };
                    (Some(m), Some(f))
                };
                let split = splits.len();
                splits.push(Split {
                    entry: i,
                    map,
                    file,
                    chunk: *chunk as u64,
                    usize: e.usize,
                    remaining: AtomicUsize::new(sizes.len()),
                    crcs: (0..sizes.len()).map(|_| OnceLock::new()).collect(),
                });
                let mut comp_off = e.data_offset(buf)?;
                let mut out_off = 0u64;
                for (index, &cl) in sizes.iter().enumerate() {
                    let out_len = (*chunk as u64).min(e.usize - out_off) as usize;
                    jobs.push(Job::Chunk { entry: i, index, comp_off, comp_len: cl as usize, out_off, out_len, split });
                    comp_off += cl as u64;
                    out_off += out_len as u64;
                }
            }
            _ => whole.push(i),
        }
    }
    whole.sort_by_key(|&i| std::cmp::Reverse(entries[i].usize));
    let n_whole = whole.len();
    let whole_bytes: u64 = whole.iter().map(|&i| entries[i].usize).sum();
    jobs.extend(whole.into_iter().map(Job::Whole));

    // Small-file extraction is bound by file creation and close, which block in the kernel;
    // more threads than cores keeps the disk and the cores busy.
    let mut threads = opts.threads.max(1);
    if !opts.test_only && n_whole > threads * 4 && whole_bytes / (n_whole as u64) < 64 << 10 {
        threads *= 2;
    }
    // Jobs run largest first, so the run takes at least as long as the biggest job; threads
    // beyond total / biggest only idle (and add CPU and memory). A file also costs about as much
    // to create and close as decoding 64 KiB.
    let cost = |j: &Job| -> u64 {
        match *j {
            Job::Chunk { out_len, .. } => out_len as u64,
            Job::Whole(i) => entries[i].usize + (64 << 10),
        }
    };
    let total_cost: u64 = jobs.iter().map(cost).sum();
    let max_cost = jobs.iter().map(cost).max().unwrap_or(1).max(1);
    let useful = total_cost.div_ceil(max_cost) as usize + 1;
    let threads = threads.min(useful).min(jobs.len().max(1));

    let use_closer = !opts.test_only && n_whole >= 256;
    let next = AtomicUsize::new(0);
    let err: Mutex<Option<anyhow::Error>> = Mutex::new(None);
    let (close_tx, close_rx) = mpsc::channel::<File>();
    // Without closer threads the receiver is dropped, so sends fail and files close in place.
    let close_rx = use_closer.then(|| Mutex::new(close_rx));
    std::thread::scope(|s| {
        // Only worth its start-up cost when there are many files to close.
        if use_closer {
            for _ in 0..opts.threads.clamp(1, 16) {
                s.spawn(|| loop {
                    let f = close_rx.as_ref().unwrap().lock().unwrap().recv();
                    match f {
                        Ok(f) => drop(f),
                        Err(_) => break,
                    }
                });
            }
        }
        for _ in 0..threads {
            let close_tx = close_tx.clone();
            let (jobs, entries, paths, splits, next, err, file) = (&jobs, &entries, &paths, &splits, &next, &err, &file);
            s.spawn(move || {
                let mut local = Local::default();
                loop {
                    let j = next.fetch_add(1, Ordering::Relaxed);
                    if j >= jobs.len() {
                        break;
                    }
                    let r = match &jobs[j] {
                        Job::Whole(i) => {
                            let path = paths.get(*i).and_then(|p| p.as_deref());
                            extract_whole(buf, file, &entries[*i], path, &mut local, &close_tx)
                        }
                        &Job::Chunk { entry, index, comp_off, comp_len, out_off, out_len, split } => extract_chunk(
                            buf,
                            &entries[entry],
                            splits,
                            split,
                            index,
                            comp_off as usize,
                            comp_len,
                            out_off,
                            out_len,
                            &mut local,
                        ),
                    };
                    if let Err(e) = r {
                        err.lock().unwrap().get_or_insert(e);
                        next.store(usize::MAX / 2, Ordering::Relaxed);
                    }
                }
            });
        }
        drop(close_tx);
    });
    if let Some(e) = err.into_inner().unwrap() {
        return Err(e);
    }
    for sp in splits {
        drop(sp.map);
        if let Some(f) = &sp.file {
            set_mtime(f, &entries[sp.entry]);
            if let Some(path) = &paths[sp.entry] {
                set_attrs(path, &entries[sp.entry]);
            }
        }
    }
    if !opts.quiet {
        let total: u64 = entries.iter().map(|e| e.usize).sum();
        eprintln!(
            "blazezip: {} {} entries ({})",
            if opts.test_only { "verified" } else { "extracted" },
            entries.len(),
            crate::create::human(total)
        );
    }
    let n = entries.len();
    std::mem::forget(entries);
    std::mem::forget(paths);
    anyhow::ensure!(!recovered, "the archive is damaged; recovered {n} entries");
    Ok(())
}

/// Create every directory the archive needs. Parents of consecutive entries are usually the
/// same, so duplicates are dropped before hashing; levels are created shallowest first, each
/// level in parallel when it is large.
fn make_dirs(dest: &Path, entries: &[CdEntry], paths: &[Option<PathBuf>], threads: usize) -> Result<()> {
    let mut dirs: HashSet<&Path> = HashSet::new();
    let mut last: Option<&Path> = None;
    for (e, p) in entries.iter().zip(paths) {
        let Some(p) = p else { continue };
        let d = if e.is_dir() { Some(p.as_path()) } else { p.parent() };
        let Some(d) = d else { continue };
        if last == Some(d) {
            continue;
        }
        last = Some(d);
        // Add the directory and any ancestors not yet seen, stopping at `dest` (which exists).
        let mut a = Some(d);
        while let Some(x) = a {
            if x == dest || !x.starts_with(dest) || !dirs.insert(x) {
                break;
            }
            a = x.parent();
        }
    }
    let mut levels: Vec<Vec<&Path>> = Vec::new();
    for d in dirs {
        let depth = d.components().count();
        if levels.len() <= depth {
            levels.resize_with(depth + 1, Vec::new);
        }
        levels[depth].push(d);
    }
    let mk = |d: &Path| -> Result<()> {
        match std::fs::create_dir(d) {
            Err(e) if e.kind() != std::io::ErrorKind::AlreadyExists => {
                Err(e).with_context(|| format!("creating {}", d.display()))
            }
            _ => Ok(()),
        }
    };
    for level in levels.iter().filter(|l| !l.is_empty()) {
        if level.len() < 64 || threads < 2 {
            level.iter().try_for_each(|d| mk(d))?;
            continue;
        }
        let next = AtomicUsize::new(0);
        let err: Mutex<Option<anyhow::Error>> = Mutex::new(None);
        std::thread::scope(|s| {
            for _ in 0..threads.min(level.len() / 16).max(1) {
                s.spawn(|| loop {
                    let k = next.fetch_add(1, Ordering::Relaxed);
                    if k >= level.len() {
                        break;
                    }
                    if let Err(e) = mk(level[k]) {
                        err.lock().unwrap().get_or_insert(e);
                        break;
                    }
                });
            }
        });
        if let Some(e) = err.into_inner().unwrap() {
            return Err(e);
        }
    }
    Ok(())
}

/// Open `path` for writing; an existing read-only file (say, from an earlier extraction of an
/// archive that records attributes) is made writable first.
fn writable<T>(path: &Path, open: impl Fn() -> std::io::Result<T>) -> std::io::Result<T> {
    match open() {
        Err(e) if e.kind() == std::io::ErrorKind::PermissionDenied => {
            let mut perm = std::fs::metadata(path)?.permissions();
            if !perm.readonly() {
                return Err(e);
            }
            #[allow(clippy::permissions_set_readonly_false)]
            perm.set_readonly(false);
            std::fs::set_permissions(path, perm)?;
            open()
        }
        r => r,
    }
}

fn create_file(path: &Path) -> Result<File> {
    writable(path, || File::create(path)).with_context(|| format!("creating {}", path.display()))
}

fn open_out(path: &Path, len: u64) -> Result<File> {
    let f = writable(path, || OpenOptions::new().read(true).write(true).create(true).truncate(true).open(path))
        .with_context(|| format!("creating {}", path.display()))?;
    f.set_len(len)?;
    Ok(f)
}

/// Grow `v` (keeping it initialised) to at least `n` bytes and return the first `n`.
fn scratch(v: &mut Vec<u8>, n: usize) -> &mut [u8] {
    if v.len() < n {
        v.resize(n, 0);
    }
    &mut v[..n]
}

fn decode(e: &CdEntry, comp: &[u8], out: &mut [u8]) -> Result<()> {
    match e.method {
        METHOD_STORE => {
            if comp.len() != out.len() {
                bail!("stored size mismatch");
            }
            out.copy_from_slice(comp);
            Ok(())
        }
        METHOD_DEFLATE => codec::inflate_into(comp, out),
        METHOD_ZSTD => codec::zstd_decompress_into(comp, out),
        m => bail!("compression method {m} is not supported"),
    }
}

/// Large stored entries are copied with ReadFile/WriteFile through a buffer instead of from the
/// archive map: page-faulting hundreds of MB of mapping from many threads at once costs several
/// times more kernel CPU than buffered reads.
fn copy_stored(zip: &File, e: &CdEntry, data_offset: u64, path: &Path, local: &mut Local) -> Result<File> {
    let mut out = create_file(path)?;
    out.set_len(e.usize)?;
    let mut crc = Crc::default();
    let b = scratch(&mut local.comp, COPY_BUF);
    let mut off = data_offset;
    let end = data_offset + e.csize;
    while off < end {
        let want = (end - off).min(b.len() as u64) as usize;
        let n = zip.seek_read(&mut b[..want], off)?;
        if n == 0 {
            bail!("{}: archive truncated", e.name);
        }
        crc.update(&b[..n]);
        out.write_all(&b[..n])?;
        off += n as u64;
    }
    check_crc(e, crc.crc)?;
    Ok(out)
}

fn extract_whole(
    buf: &[u8],
    zip: &File,
    e: &CdEntry,
    path: Option<&Path>,
    local: &mut Local,
    closer: &mpsc::Sender<File>,
) -> Result<()> {
    let data_offset = e.data_offset(buf)?;
    let start = data_offset as usize;
    let end = start
        .checked_add(e.csize as usize)
        .filter(|&x| x <= buf.len())
        .with_context(|| format!("{}: data extends past end of archive", e.name))?;
    let ctx = || format!("{}: corrupt data", e.name);
    let stored = e.method == METHOD_STORE && e.csize == e.usize;
    let finish = |f: File| {
        set_mtime(&f, e);
        if let Some(path) = path {
            set_attrs(path, e);
        }
        // With no closer threads running the send fails and the file is closed right here.
        let _ = closer.send(f);
    };

    if e.usize == 0 {
        check_crc(e, 0)?;
        if let Some(path) = path {
            finish(create_file(path)?);
        }
        return Ok(());
    }
    if let Some(path) = path {
        if stored && e.usize >= STREAM_STORED_THRESHOLD {
            return copy_stored(zip, e, data_offset, path, local).map(finish);
        }
        if !stored && e.usize >= MMAP_OUT_THRESHOLD {
            let comp = &buf[start..end];
            let f = open_out(path, e.usize)?;
            let mut m = unsafe { MmapMut::map_mut(&f)? };
            decode(e, comp, &mut m).with_context(ctx)?;
            check_crc(e, Crc::of(&m).crc)?;
            drop(m);
            finish(f);
            return Ok(());
        }
    }
    // Stored data is used (and written) straight from the archive map: no copy.
    let data: &[u8] = if stored {
        &buf[start..end]
    } else {
        let comp = &buf[start..end];
        let out = scratch(&mut local.out, e.usize as usize);
        decode(e, comp, out).with_context(ctx)?;
        out
    };
    check_crc(e, Crc::of(data).crc)?;
    if let Some(path) = path {
        let mut f = create_file(path)?;
        f.write_all(data)?;
        finish(f);
    }
    Ok(())
}

#[allow(clippy::too_many_arguments)]
fn extract_chunk(
    buf: &[u8],
    e: &CdEntry,
    splits: &[Split],
    split_idx: usize,
    index: usize,
    comp_off: usize,
    comp_len: usize,
    out_off: u64,
    out_len: usize,
    local: &mut Local,
) -> Result<()> {
    let split = &splits[split_idx];
    let comp = buf.get(comp_off..comp_off + comp_len).with_context(|| format!("{}: truncated", e.name))?;
    let last = index + 1 == split.crcs.len();
    let out: &mut [u8] = match &split.map {
        // SAFETY: every chunk job writes a disjoint range of the map.
        Some(m) => unsafe { std::slice::from_raw_parts_mut((m.as_ptr() as *mut u8).add(out_off as usize), out_len) },
        None => scratch(&mut local.out, out_len),
    };
    match e.method {
        METHOD_DEFLATE => codec::inflate_chunk_into(comp, out, last),
        METHOD_ZSTD => codec::zstd_decompress_into(comp, out),
        m => bail!("compression method {m} is not supported for chunked entries"),
    }
    .with_context(|| format!("{}: corrupt data in chunk {index}", e.name))?;
    let _ = split.crcs[index].set(Crc::of(out).crc);
    if split.remaining.fetch_sub(1, Ordering::AcqRel) == 1 {
        let mut total = Crc::default();
        for (k, c) in split.crcs.iter().enumerate() {
            let len = split.chunk.min(split.usize - k as u64 * split.chunk);
            total = total.combine(Crc { crc: *c.get().unwrap(), len });
        }
        check_crc(e, total.crc)?;
    }
    Ok(())
}

fn check_crc(e: &CdEntry, crc: u32) -> Result<()> {
    if crc != e.crc {
        bail!("{}: CRC mismatch (expected {:08x}, got {:08x})", e.name, e.crc, crc);
    }
    Ok(())
}

fn set_mtime(f: &File, e: &CdEntry) {
    use std::os::windows::fs::FileTimesExt;
    let times = match e.ntfs_times {
        Some([m, a, c]) => std::fs::FileTimes::new()
            .set_modified(system_time_from_filetime(m))
            .set_accessed(system_time_from_filetime(a))
            .set_created(system_time_from_filetime(c)),
        None => std::fs::FileTimes::new().set_modified(match e.unix_mtime {
            Some(u) => std::time::UNIX_EPOCH + std::time::Duration::from_secs(u as u64),
            None => dos_to_system_time(e.dos_time, e.dos_date),
        }),
    };
    let _ = f.set_times(times);
}

/// Restore read-only / hidden / system attributes recorded by a DOS, Windows or NTFS host.
fn set_attrs(path: &Path, e: &CdEntry) {
    const KEPT: u32 = ATTR_READONLY | ATTR_HIDDEN | ATTR_SYSTEM;
    if !matches!(e.made_by >> 8, 0 | 10 | 14) || e.ext_attrs & KEPT == 0 {
        return;
    }
    #[link(name = "kernel32")]
    extern "system" {
        fn SetFileAttributesW(path: *const u16, attrs: u32) -> i32;
    }
    use std::os::windows::ffi::OsStrExt;
    let w: Vec<u16> = path.as_os_str().encode_wide().chain([0]).collect();
    unsafe { SetFileAttributesW(w.as_ptr(), e.ext_attrs & (KEPT | ATTR_ARCHIVE)) };
}

/// Map an archive name to a path under `dest`, refusing anything that escapes it (zip-slip).
fn safe_path(dest: &Path, name: &str) -> Option<PathBuf> {
    let mut p = PathBuf::with_capacity(dest.as_os_str().len() + name.len() + 1);
    p.push(dest);
    let mut any = false;
    for part in name.split(['/', '\\']) {
        if part.is_empty() || part == "." {
            continue;
        }
        if part == ".." || part.contains(':') {
            return None;
        }
        let c = Path::new(part).components().next();
        if !matches!(c, Some(Component::Normal(_))) {
            return None;
        }
        p.push(part);
        any = true;
    }
    any.then_some(p)
}

pub fn list(zip: &Path) -> Result<()> {
    let file = File::open(zip)?;
    let map = unsafe { Mmap::map(&file)? };
    let entries = parse_central_directory(&map)?;
    let mut out = std::io::BufWriter::new(std::io::stdout().lock());
    writeln!(out, "{:>14} {:>14} {:>7}  {:>6}  name", "size", "compressed", "method", "chunks")?;
    for e in &entries {
        let other;
        let method = match e.method {
            METHOD_STORE => "store",
            METHOD_DEFLATE => "deflate",
            METHOD_ZSTD => "zstd",
            m => {
                other = m.to_string();
                &other
            }
        };
        let chunks = e.chunks.as_ref().map(|c| c.1.len()).unwrap_or(1);
        writeln!(out, "{:>14} {:>14} {:>7}  {:>6}  {}", e.usize, e.csize, method, chunks, e.name)?;
    }
    Ok(())
}
