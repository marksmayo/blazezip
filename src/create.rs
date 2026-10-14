//! Parallel archive creation.
//!
//! Every input file becomes part of a job. Runs of small files are batched into one job; big
//! files are split into independent chunks so a single huge file still uses every core. Workers
//! compress jobs out of order; a single writer thread emits them in archive order, bounded by an
//! in-flight window (in jobs and in bytes) so memory stays proportional to the thread count, not
//! the input size. Output buffers are pooled and recycled by the writer.

use crate::codec::{self, Crc};
use crate::zipfmt::*;
use anyhow::{Context, Result};
use std::collections::HashMap;
use std::fs::{File, OpenOptions};
use std::io::{BufWriter, Read, Write};
use std::os::windows::fs::{FileExt, OpenOptionsExt};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{mpsc, Condvar, Mutex};
use std::time::SystemTime;

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Method {
    Store,
    Deflate,
    Zstd,
}

pub struct CreateOpts {
    pub method: Method,
    pub level: i32,
    pub threads: usize,
    /// The thread count was not given explicitly: small inputs may use more workers.
    pub auto_threads: bool,
    /// Uncompressed bytes per chunk for large files. Files at or below this are one job.
    pub chunk_size: usize,
    /// The chunk size was not given explicitly: small inputs may use smaller chunks.
    pub auto_chunk: bool,
    pub store_incompressible: bool,
    pub quiet: bool,
}

struct Entry {
    name: String,
    path: PathBuf,
    is_dir: bool,
    size: u64,
    mtime: SystemTime,
    /// Modified, accessed and created FILETIMEs, and DOS attributes, as the file system reports them.
    times: [u64; 3],
    attrs: u32,
    /// Decided up front for chunked files (sampled), per file for whole files.
    force_store: bool,
    chunk_size: usize,
    n_chunks: usize,
}

/// A run of `count` whole entries starting at `entry`, or one chunk of a large entry.
struct Job {
    entry: usize,
    count: usize,
    chunk: Option<usize>,
    offset: u64,
    len: usize,
}

/// One entry's (or one chunk's) bytes within a job's output buffer.
struct Part {
    len: usize,
    crc: Crc,
    method: u16,
    /// The file could not be read (locked by another process, access denied, deleted since the
    /// tree was listed): it is left out of the archive and reported.
    skipped: bool,
}

struct Done {
    job: usize,
    data: Vec<u8>,
    parts: Vec<Part>,
}

/// Default worker threads when creating Deflate archives. Deflate compression is CPU-bound:
/// past about 8 threads, hyperthread siblings and efficiency cores add much more CPU time than
/// they save in wall time.
pub const CREATE_THREADS: usize = 8;

/// Deflate inputs smaller than this are only a few rounds of chunks, where a few more workers
/// shorten the last round by more than they cost. Only from SMALL_INPUT_MIN_LEVEL up: at the
/// faster levels the rounds are too short for that to outweigh the extra CPU and memory.
const SMALL_INPUT: u64 = 32 << 20;
const SMALL_INPUT_THREADS: usize = 12;
const SMALL_INPUT_MIN_LEVEL: i32 = 7;

/// Small files are grouped into jobs of up to this many bytes / entries.
const BATCH_BYTES: u64 = 256 << 10;
const BATCH_ENTRIES: usize = 64;

/// Whole files at least this large are sampled for compressibility before compressing them.
const SAMPLE_WHOLE_MIN: u64 = 256 << 10;

/// Extensions of formats that are already compressed; such files are sampled before compressing.
const COMPRESSED_EXTS: &[&str] = &[
    "jpg", "jpeg", "png", "gif", "webp", "heic", "avif", "mp4", "m4v", "mov", "mkv", "webm", "avi", "mp3", "m4a",
    "aac", "ogg", "opus", "flac", "zip", "7z", "rar", "gz", "tgz", "bz2", "xz", "zst", "lz4", "br", "cab", "jar",
    "apk", "docx", "xlsx", "pptx", "odt", "epub", "pdf", "woff", "woff2",
];

const FILE_FLAG_SEQUENTIAL_SCAN: u32 = 0x0800_0000;

pub fn create(out_path: &Path, inputs: &[PathBuf], opts: &CreateOpts) -> Result<()> {
    let skipped = build(out_path, inputs, opts, None)?;
    ensure_none_skipped(skipped)
}

fn ensure_none_skipped(skipped: usize) -> Result<()> {
    // The archive is complete and valid, but it is missing files, so say so with the exit code.
    anyhow::ensure!(skipped == 0, "{skipped} file(s) could not be read and were left out of the archive");
    Ok(())
}

/// Add `inputs` to an existing archive (replacing entries of the same name), or create it.
/// Existing entries are copied as they are, compressed data included.
pub fn update(archive: &Path, inputs: &[PathBuf], opts: &CreateOpts) -> Result<()> {
    if !archive.exists() {
        return create(archive, inputs, opts);
    }
    rewrite(archive, opts, |entries| Ok(entries), inputs)
}

/// Remove entries (and everything under directory entries) by name.
pub fn delete(archive: &Path, names: &[String], opts: &CreateOpts) -> Result<()> {
    let names: Vec<String> = names.iter().map(|n| n.replace('\\', "/").trim_end_matches('/').to_string()).collect();
    rewrite(
        archive,
        opts,
        |entries| {
            let before = entries.len();
            let kept: Vec<CdEntry> = entries
                .into_iter()
                .filter(|e| {
                    let name = e.name.trim_end_matches('/');
                    !names.iter().any(|n| name == n || name.strip_prefix(n.as_str()).is_some_and(|r| r.starts_with('/')))
                })
                .collect();
            anyhow::ensure!(kept.len() < before, "no entries match {}", names.join(", "));
            Ok(kept)
        },
        &[],
    )
}

/// Write a new archive from (a selection of) `archive`'s entries plus `inputs`, then replace it.
fn rewrite(
    archive: &Path,
    opts: &CreateOpts,
    select: impl FnOnce(Vec<CdEntry>) -> Result<Vec<CdEntry>>,
    inputs: &[PathBuf],
) -> Result<()> {
    let mut tmp = archive.as_os_str().to_owned();
    tmp.push(".tmp");
    let tmp = PathBuf::from(tmp);
    let skipped = {
        let file = File::open(archive).with_context(|| format!("opening {}", archive.display()))?;
        let map = unsafe { memmap2::Mmap::map(&file)? };
        let entries = parse_central_directory(&map)?;
        if let Some(e) = entries.iter().find(|e| e.flags & 1 != 0) {
            anyhow::bail!("{}: encrypted entries are not supported", e.name);
        }
        let kept = select(entries)?;
        build(&tmp, inputs, opts, Some((&map, &kept)))?
    };
    std::fs::rename(&tmp, archive).with_context(|| format!("replacing {}", archive.display()))?;
    ensure_none_skipped(skipped)
}

/// Write an archive of `base`'s entries (copied verbatim, minus any that `inputs` replace)
/// followed by `inputs`. Returns how many input files could not be read.
fn build(out_path: &Path, inputs: &[PathBuf], opts: &CreateOpts, base: Option<(&[u8], &[CdEntry])>) -> Result<usize> {
    let mut entries = if inputs.is_empty() { Vec::new() } else { gather(inputs, opts.threads.max(1))? };
    let (base_buf, kept): (&[u8], Vec<&CdEntry>) = match base {
        Some((buf, old)) => {
            let new: std::collections::HashSet<&str> =
                entries.iter().map(|e| e.name.trim_end_matches('/')).collect();
            (buf, old.iter().filter(|e| !new.contains(e.name.trim_end_matches('/'))).collect())
        }
        None => (&[], Vec::new()),
    };
    let logical = std::thread::available_parallelism().map(|n| n.get()).unwrap_or(4);
    let tuned;
    // Not at near-optimal levels (10+): there, smaller chunks cost ratio that matters more.
    let opts = if opts.auto_chunk && !(opts.method == Method::Deflate && opts.level >= 10) {
        tuned = CreateOpts { chunk_size: auto_chunk_size(&entries, opts, logical), ..*opts };
        &tuned
    } else {
        opts
    };
    let jobs = plan(&mut entries, opts);
    let compress_bytes: u64 = match opts.method {
        Method::Store => 0,
        _ => entries.iter().filter(|e| !e.force_store).map(|e| e.size).sum(),
    };
    let threads = worker_threads(opts, compress_bytes, jobs.len(), logical);
    let window = threads * 3 / 2 + 2;
    let max_jobs = window * 4;
    let job_bytes = |j: &Job| -> u64 {
        match j.chunk {
            Some(_) => j.len as u64,
            None => entries[j.entry..j.entry + j.count].iter().map(|e| e.size).sum(),
        }
    };
    let mut cum = Vec::with_capacity(jobs.len() + 1);
    cum.push(0u64);
    for j in &jobs {
        cum.push(cum.last().unwrap() + job_bytes(j));
    }
    let gate = Gate {
        written: AtomicUsize::new(0),
        waiters: AtomicUsize::new(0),
        abort: AtomicBool::new(false),
        lock: Mutex::new(()),
        cv: Condvar::new(),
        budget: window as u64 * jobs.iter().map(|j| j.len as u64).max().unwrap_or(0).max(BATCH_BYTES),
        max_jobs,
        cum,
    };

    let out = create_output(out_path).with_context(|| format!("creating {}", out_path.display()))?;
    // Until the archive is complete, closing the handle deletes the file: an error, a crash or
    // Ctrl-C never leaves a partial archive behind.
    set_delete_on_close(&out, true);
    let total_in: u64 = entries.iter().map(|e| e.size).sum();
    let kept_bytes: u64 = kept.iter().map(|e| e.csize + 128).sum();
    preallocate(&out, total_in + kept_bytes + entries.len() as u64 * 128);
    let next_job = AtomicUsize::new(0);
    let pool = Pool::default();
    let (tx, rx) = mpsc::sync_channel::<Result<Done>>(window);

    let (written_len, skipped) = std::thread::scope(|s| -> Result<(u64, usize)> {
        for _ in 0..threads {
            let tx = tx.clone();
            let (entries, jobs, next_job, gate, pool) = (&entries, &jobs, &next_job, &gate, &pool);
            s.spawn(move || {
                let mut local = Local::default();
                loop {
                    let i = next_job.fetch_add(1, Ordering::Relaxed);
                    if i >= jobs.len() {
                        break;
                    }
                    gate.wait_for(i);
                    if gate.abort.load(Ordering::Relaxed) {
                        break;
                    }
                    let r = run_job(i, &jobs[i], entries, opts, &mut local, pool);
                    let failed = r.is_err();
                    if tx.send(r).is_err() || failed {
                        next_job.store(usize::MAX / 2, Ordering::Relaxed);
                        break;
                    }
                }
            });
        }
        drop(tx);
        let r = write_archive(out, &entries, &jobs, rx, &gate, &pool, base_buf, &kept);
        if r.is_err() {
            // Unblock any workers parked on the window so the scope can join.
            next_job.store(usize::MAX / 2, Ordering::Relaxed);
            gate.cancel();
        }
        r
    })?;
    if !opts.quiet {
        eprintln!(
            "blazezip: {} entries, {} -> {} ({:.1}%)",
            entries.len() - skipped + kept.len(),
            human(total_in),
            human(written_len),
            if total_in > 0 { written_len as f64 * 100.0 / total_in as f64 } else { 100.0 }
        );
    }
    // The process exits right after this; freeing tens of thousands of paths is wasted work.
    std::mem::forget(entries);
    std::mem::forget(jobs);
    Ok(skipped)
}

/// Worker threads when -t is not given. Deflate stays at CREATE_THREADS (see there);
/// Zstandard is fast enough per byte that every logical CPU still pays off in wall time.
pub fn default_threads(method: Method, logical: usize) -> usize {
    match method {
        Method::Zstd => logical,
        _ => logical.min(CREATE_THREADS),
    }
}

/// Stored data is bound by I/O, which two threads keep busy; compression needs CPU in
/// proportion to the bytes it has to compress. Idle extra threads only add overhead.
fn worker_threads(opts: &CreateOpts, compress_bytes: u64, jobs: usize, logical: usize) -> usize {
    let mut threads = opts.threads.max(1);
    if opts.auto_threads
        && opts.method == Method::Deflate
        && opts.level >= SMALL_INPUT_MIN_LEVEL
        && compress_bytes < SMALL_INPUT
    {
        threads = threads.max(logical.min(SMALL_INPUT_THREADS));
    }
    let wanted = 2 + (compress_bytes >> 20) as usize;
    threads.min(wanted).min(jobs).max(1)
}

/// With little input there may be fewer chunks than threads. Shrink the chunk size (down to
/// 128 KiB) so there are about four jobs per default thread; large inputs keep the configured
/// size. Planned for the method's default thread count rather than the actual one, so -t never
/// changes the bytes.
fn auto_chunk_size(entries: &[Entry], opts: &CreateOpts, logical: usize) -> usize {
    let total: u64 = entries.iter().map(|e| e.size).sum();
    let target = (total / (default_threads(opts.method, logical) as u64 * 4)) as usize;
    target.next_multiple_of(64 << 10).clamp(128 << 10, opts.chunk_size.max(128 << 10))
}

/// Workers may run job `i` only while it is within `max_jobs` jobs and `budget` bytes of the
/// writer. The fast path is a single atomic load; the mutex is only taken to sleep.
struct Gate {
    written: AtomicUsize,
    waiters: AtomicUsize,
    abort: AtomicBool,
    lock: Mutex<()>,
    cv: Condvar,
    /// cum[i] = uncompressed bytes in jobs 0..i.
    cum: Vec<u64>,
    budget: u64,
    max_jobs: usize,
}

impl Gate {
    fn allowed(&self, i: usize) -> bool {
        if self.abort.load(Ordering::SeqCst) {
            return true;
        }
        let w = self.written.load(Ordering::SeqCst).min(i);
        i < w + self.max_jobs && self.cum[i] - self.cum[w] <= self.budget
    }

    fn wait_for(&self, i: usize) {
        if self.allowed(i) {
            return;
        }
        self.waiters.fetch_add(1, Ordering::SeqCst);
        let mut g = self.lock.lock().unwrap();
        while !self.allowed(i) {
            g = self.cv.wait(g).unwrap();
        }
        drop(g);
        self.waiters.fetch_sub(1, Ordering::SeqCst);
    }

    fn advance(&self, written: usize) {
        self.written.store(written, Ordering::SeqCst);
        if self.waiters.load(Ordering::SeqCst) > 0 {
            let _g = self.lock.lock().unwrap();
            self.cv.notify_all();
        }
    }

    fn cancel(&self) {
        self.abort.store(true, Ordering::SeqCst);
        let _g = self.lock.lock().unwrap();
        self.cv.notify_all();
    }
}

/// Output buffers recycled from the writer back to the workers, so large allocations (and the
/// page faults that come with fresh ones) happen once rather than per job.
#[derive(Default)]
struct Pool(Mutex<Vec<Vec<u8>>>);

impl Pool {
    fn get(&self) -> Vec<u8> {
        self.0.lock().unwrap().pop().unwrap_or_default()
    }
    fn put(&self, mut v: Vec<u8>) {
        v.clear();
        self.0.lock().unwrap().push(v);
    }
}

/// Per-worker state: a reusable read buffer and the last opened chunked input.
#[derive(Default)]
struct Local {
    raw: Vec<u8>,
    file: Option<(usize, File)>,
}

fn open_input(path: &Path) -> std::io::Result<File> {
    OpenOptions::new().read(true).custom_flags(FILE_FLAG_SEQUENTIAL_SCAN).open(path)
}

/// Reserve disk space for the archive up front so NTFS doesn't extend the allocation on every
/// write. Best effort: the hint is an upper estimate and is released at close.
fn create_output(path: &Path) -> std::io::Result<File> {
    use std::os::windows::fs::OpenOptionsExt;
    const DELETE: u32 = 0x0001_0000;
    const GENERIC_READ: u32 = 0x8000_0000;
    const GENERIC_WRITE: u32 = 0x4000_0000;
    OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(true)
        .access_mode(GENERIC_READ | GENERIC_WRITE | DELETE)
        .open(path)
}

/// SetFileInformationByHandle with a plain-data info value; best effort.
fn set_file_info<T>(f: &File, class: i32, info: &T) {
    use std::os::windows::io::AsRawHandle;
    #[link(name = "kernel32")]
    extern "system" {
        fn SetFileInformationByHandle(h: *mut std::ffi::c_void, class: i32, info: *const std::ffi::c_void, size: u32) -> i32;
    }
    unsafe {
        SetFileInformationByHandle(f.as_raw_handle(), class, (info as *const T).cast(), std::mem::size_of::<T>() as u32);
    }
}

fn set_delete_on_close(f: &File, delete: bool) {
    const FILE_DISPOSITION_INFO: i32 = 4;
    set_file_info(f, FILE_DISPOSITION_INFO, &(delete as u8));
}

/// Errors that mean "this one file cannot be read right now" rather than a failed archive.
fn is_unreadable(e: &std::io::Error) -> bool {
    const ERROR_SHARING_VIOLATION: i32 = 32;
    const ERROR_LOCK_VIOLATION: i32 = 33;
    matches!(e.kind(), std::io::ErrorKind::PermissionDenied | std::io::ErrorKind::NotFound)
        || matches!(e.raw_os_error(), Some(ERROR_SHARING_VIOLATION | ERROR_LOCK_VIOLATION))
}

fn preallocate(f: &File, bytes: u64) {
    const FILE_ALLOCATION_INFO: i32 = 5;
    set_file_info(f, FILE_ALLOCATION_INFO, &(bytes as i64));
}

fn gather(inputs: &[PathBuf], threads: usize) -> Result<Vec<Entry>> {
    let mut entries = Vec::new();
    for input in inputs {
        let meta = std::fs::metadata(input).with_context(|| format!("reading {}", input.display()))?;
        // Archive names are relative to the input's parent, like `zip -r out.zip dir`.
        let base = input.parent().map(Path::to_path_buf).unwrap_or_default();
        if meta.is_dir() {
            let listing = list_tree(input, threads)?;
            let root_name = zip_name(input.strip_prefix(&base).unwrap_or(input));
            if !root_name.is_empty() {
                entries.push(new_entry(root_name + "/", input.clone(), true, &meta));
            }
            // Depth-first, directory listing order: the same order a recursive walk produces.
            let mut stack = vec![(input.clone(), 0usize)];
            while let Some((dir, k)) = stack.pop() {
                let children = &listing[&dir];
                if k >= children.len() {
                    continue;
                }
                stack.push((dir.clone(), k + 1));
                let (path, is_dir, m) = &children[k];
                let rel = path.strip_prefix(&base).unwrap_or(path);
                let mut name = zip_name(rel);
                if name.is_empty() {
                    continue;
                }
                if *is_dir {
                    name.push('/');
                }
                entries.push(new_entry(name, path.clone(), *is_dir, m));
                if *is_dir {
                    stack.push((path.clone(), 0));
                }
            }
        } else {
            let name = zip_name(Path::new(input.file_name().context("bad input path")?));
            entries.push(new_entry(name, input.clone(), false, &meta));
        }
    }
    Ok(entries)
}

type Listing = HashMap<PathBuf, Vec<(PathBuf, bool, std::fs::Metadata)>>;

/// List every directory under `root` (files and directories only; links are skipped, as
/// before). Directories are read in parallel once there is more than a handful to do.
fn list_tree(root: &Path, threads: usize) -> Result<Listing> {
    fn read(dir: &Path) -> Result<Vec<(PathBuf, bool, std::fs::Metadata)>> {
        let mut v = Vec::new();
        for de in std::fs::read_dir(dir).with_context(|| format!("reading {}", dir.display()))? {
            let de = de?;
            let ft = de.file_type()?;
            if !(ft.is_dir() || ft.is_file()) {
                continue;
            }
            v.push((de.path(), ft.is_dir(), de.metadata()?));
        }
        Ok(v)
    }
    let mut listing = Listing::new();
    let mut queue = vec![root.to_path_buf()];
    // Small trees: stay serial and skip the thread start-up cost.
    while let Some(d) = queue.pop() {
        let v = read(&d)?;
        queue.extend(v.iter().filter(|c| c.1).map(|c| c.0.clone()));
        listing.insert(d, v);
        if queue.len() >= 4 {
            break;
        }
    }
    if queue.is_empty() {
        return Ok(listing);
    }
    struct Shared {
        queue: Vec<PathBuf>,
        active: usize,
        listing: Listing,
        err: Option<anyhow::Error>,
    }
    let shared = Mutex::new(Shared { queue, active: 0, listing, err: None });
    let cv = Condvar::new();
    std::thread::scope(|s| {
        for _ in 0..threads.clamp(1, 8) {
            s.spawn(|| loop {
                let dir = {
                    let mut g = shared.lock().unwrap();
                    loop {
                        if g.err.is_some() {
                            return;
                        }
                        if let Some(d) = g.queue.pop() {
                            g.active += 1;
                            break d;
                        }
                        if g.active == 0 {
                            cv.notify_all();
                            return;
                        }
                        g = cv.wait(g).unwrap();
                    }
                };
                let r = read(&dir);
                let mut g = shared.lock().unwrap();
                g.active -= 1;
                match r {
                    Ok(v) => {
                        let before = g.queue.len();
                        g.queue.extend(v.iter().filter(|c| c.1).map(|c| c.0.clone()));
                        g.listing.insert(dir, v);
                        if g.queue.len() > before || g.active == 0 {
                            cv.notify_all();
                        }
                    }
                    Err(e) => {
                        g.err = Some(e);
                        cv.notify_all();
                    }
                }
            });
        }
    });
    let s = shared.into_inner().unwrap();
    match s.err {
        Some(e) => Err(e),
        None => Ok(s.listing),
    }
}

fn new_entry(name: String, path: PathBuf, is_dir: bool, m: &std::fs::Metadata) -> Entry {
    use std::os::windows::fs::MetadataExt;
    Entry {
        name,
        path,
        is_dir,
        size: if is_dir { 0 } else { m.len() },
        mtime: m.modified().unwrap_or(SystemTime::UNIX_EPOCH),
        // The access slot gets the modification time: Windows updates last-access times when
        // files are read (blazezip included), which would make repeat runs differ.
        times: [m.last_write_time(), m.last_write_time(), m.creation_time()],
        attrs: m.file_attributes(),
        force_store: false,
        chunk_size: 0,
        n_chunks: 1,
    }
}

fn zip_name(rel: &Path) -> String {
    let mut name = String::new();
    for c in rel.components() {
        if let std::path::Component::Normal(s) = c {
            if !name.is_empty() {
                name.push('/');
            }
            name.push_str(&s.to_string_lossy());
        }
    }
    name
}

fn plan(entries: &mut [Entry], opts: &CreateOpts) -> Vec<Job> {
    let mut jobs: Vec<Job> = Vec::with_capacity(entries.len() / 4 + 1);
    let mut to_sample = Vec::new();
    // Bytes in the batch job currently being filled, if the last job is an open batch.
    let mut batch: Option<u64> = None;
    for (i, e) in entries.iter_mut().enumerate() {
        let mut chunk = opts.chunk_size.max(64 * 1024);
        let n = (e.size as usize).div_ceil(chunk).max(1);
        if n > MAX_CHUNKS {
            chunk = (e.size as usize).div_ceil(MAX_CHUNKS).next_multiple_of(1 << 20);
        }
        e.chunk_size = chunk;
        if e.is_dir || e.size as usize <= chunk {
            match batch {
                Some(b) if b + e.size <= BATCH_BYTES && jobs.last().unwrap().count < BATCH_ENTRIES => {
                    jobs.last_mut().unwrap().count += 1;
                    batch = Some(b + e.size);
                }
                _ => {
                    jobs.push(Job { entry: i, count: 1, chunk: None, offset: 0, len: e.size as usize });
                    batch = (e.size < BATCH_BYTES).then_some(e.size);
                }
            }
        } else {
            batch = None;
            if opts.method != Method::Store && opts.store_incompressible {
                to_sample.push(i);
            }
            let mut off = 0u64;
            let mut index = 0;
            while off < e.size {
                let len = chunk.min((e.size - off) as usize);
                jobs.push(Job { entry: i, count: 1, chunk: Some(index), offset: off, len });
                off += len as u64;
                index += 1;
            }
            e.n_chunks = index;
        }
    }
    sample_all(entries, &to_sample, opts);
    jobs
}

const SAMPLE: usize = 128 * 1024;

/// Sample every large file in parallel: each needs two reads and two fast compressions, which
/// otherwise run serially before the first job starts.
fn sample_all(entries: &mut [Entry], which: &[usize], opts: &CreateOpts) {
    if which.is_empty() {
        return;
    }
    let zstd = opts.method == Method::Zstd;
    let next = AtomicUsize::new(0);
    let results: Vec<AtomicBool> = which.iter().map(|_| AtomicBool::new(false)).collect();
    let threads = opts.threads.max(1).min(which.len());
    {
        let entries = &*entries;
        let sample = || loop {
            let k = next.fetch_add(1, Ordering::Relaxed);
            if k >= which.len() {
                break;
            }
            let e = &entries[which[k]];
            let r = sample_file_incompressible(&e.path, e.size, zstd).unwrap_or(false);
            results[k].store(r, Ordering::Relaxed);
        };
        if threads == 1 {
            sample();
        } else {
            std::thread::scope(|s| {
                for _ in 0..threads {
                    s.spawn(sample);
                }
            });
        }
    }
    for (k, &i) in which.iter().enumerate() {
        entries[i].force_store = results[k].load(Ordering::Relaxed);
    }
}

/// Compress a sample from the start and middle of a large file at the fastest level; if it
/// barely shrinks (already-compressed media, archives, encrypted data) store the file instead.
fn sample_file_incompressible(path: &Path, size: u64, zstd: bool) -> Result<bool> {
    let f = File::open(path)?;
    let mut buf = vec![0u8; SAMPLE];
    let mut total_in = 0;
    let mut total_out = 0;
    for off in [0, size / 2] {
        let n = read_at(&f, off, &mut buf)?;
        total_in += n;
        total_out += codec::quick_compressed_size(&buf[..n], zstd);
    }
    Ok(total_in > 0 && total_out as f64 > total_in as f64 * 0.97)
}

/// The same test on a file already in memory.
fn sample_buf_incompressible(raw: &[u8], zstd: bool) -> bool {
    let (total_in, total_out) = if raw.len() <= 2 * SAMPLE {
        (raw.len(), codec::quick_compressed_size(raw, zstd))
    } else {
        let mid = raw.len() / 2;
        let (a, b) = (&raw[..SAMPLE], &raw[mid..mid + SAMPLE]);
        (2 * SAMPLE, codec::quick_compressed_size(a, zstd) + codec::quick_compressed_size(b, zstd))
    };
    total_in > 0 && total_out as f64 > total_in as f64 * 0.97
}

fn has_compressed_ext(path: &Path) -> bool {
    path.extension()
        .and_then(|x| x.to_str())
        .is_some_and(|x| COMPRESSED_EXTS.iter().any(|c| c.eq_ignore_ascii_case(x)))
}

fn read_at(f: &File, mut off: u64, buf: &mut [u8]) -> std::io::Result<usize> {
    let mut n = 0;
    while n < buf.len() {
        let r = f.seek_read(&mut buf[n..], off)?;
        if r == 0 {
            break;
        }
        n += r;
        off += r as u64;
    }
    Ok(n)
}

/// Grow `v` (keeping it initialised) to at least `n` bytes and return the first `n`.
fn scratch(v: &mut Vec<u8>, n: usize) -> &mut [u8] {
    if v.len() < n {
        v.resize(n, 0);
    }
    &mut v[..n]
}

fn run_job(idx: usize, job: &Job, entries: &[Entry], opts: &CreateOpts, local: &mut Local, pool: &Pool) -> Result<Done> {
    let mut data = pool.get();
    let mut parts = Vec::with_capacity(job.count);
    match job.chunk {
        None => {
            for e in &entries[job.entry..job.entry + job.count] {
                parts.push(encode_whole(e, opts, &mut local.raw, &mut data)?);
            }
        }
        Some(index) => {
            let e = &entries[job.entry];
            if local.file.as_ref().map(|f| f.0) != Some(job.entry) {
                let f = open_input(&e.path).with_context(|| format!("opening {}", e.path.display()))?;
                local.file = Some((job.entry, f));
            }
            let f = &local.file.as_ref().unwrap().1;
            let raw = scratch(&mut local.raw, job.len);
            let n = read_at(f, job.offset, raw)?;
            anyhow::ensure!(n == job.len, "{} changed while archiving", e.path.display());
            let crc = Crc::of(raw);
            let last = index + 1 == e.n_chunks;
            let method = if opts.method == Method::Store || e.force_store {
                data.extend_from_slice(raw);
                METHOD_STORE
            } else if opts.method == Method::Zstd {
                codec::zstd_append(raw, opts.level, &mut data)?;
                METHOD_ZSTD
            } else {
                codec::deflate_append(raw, opts.level, last, &mut data);
                METHOD_DEFLATE
            };
            parts.push(Part { len: data.len(), crc, method, skipped: false });
            if last {
                local.file = None;
            }
        }
    }
    Ok(Done { job: idx, data, parts })
}

/// Read and encode one whole (unchunked) entry, appending its bytes to `out`.
fn encode_whole(e: &Entry, opts: &CreateOpts, raw: &mut Vec<u8>, out: &mut Vec<u8>) -> Result<Part> {
    if e.is_dir || e.size == 0 {
        return Ok(Part { len: 0, crc: Crc::default(), method: METHOD_STORE, skipped: false });
    }
    let size = e.size as usize;
    let raw = scratch(raw, size);
    let mut read = || -> std::io::Result<usize> {
        let mut f = open_input(&e.path)?;
        let mut n = 0;
        while n < size {
            match f.read(&mut raw[n..])? {
                0 => break,
                r => n += r,
            }
        }
        Ok(n)
    };
    match read() {
        Ok(n) => anyhow::ensure!(n == size, "{} changed while archiving", e.path.display()),
        Err(err) if is_unreadable(&err) => {
            eprintln!("blazezip: warning: skipping {}: {err}", e.path.display());
            return Ok(Part { len: 0, crc: Crc::default(), method: METHOD_STORE, skipped: true });
        }
        Err(err) => return Err(err).with_context(|| format!("reading {}", e.path.display())),
    }
    let crc = Crc::of(raw);
    let start = out.len();
    let zstd = opts.method == Method::Zstd;
    let store = opts.method == Method::Store
        || (opts.store_incompressible
            && (e.size >= SAMPLE_WHOLE_MIN || has_compressed_ext(&e.path))
            && sample_buf_incompressible(raw, zstd));
    if !store {
        let (n, method) = match opts.method {
            Method::Deflate => (codec::deflate_append(raw, opts.level, true, out), METHOD_DEFLATE),
            Method::Zstd => (codec::zstd_append(raw, opts.level, out)?, METHOD_ZSTD),
            Method::Store => unreachable!(),
        };
        if !(opts.store_incompressible && n >= size) {
            return Ok(Part { len: n, crc, method, skipped: false });
        }
        out.truncate(start);
    }
    out.extend_from_slice(raw);
    Ok(Part { len: size, crc, method: METHOD_STORE, skipped: false })
}

#[allow(clippy::too_many_arguments)]
fn write_archive<'a>(
    out: File,
    entries: &'a [Entry],
    jobs: &[Job],
    rx: mpsc::Receiver<Result<Done>>,
    gate: &Gate,
    pool: &Pool,
    base: &[u8],
    kept: &[&'a CdEntry],
) -> Result<(u64, usize)> {
    let mut w = BufWriter::with_capacity(512 << 10, out);
    let mut pos: u64 = 0;
    let ring_cap = gate.max_jobs;
    let mut ring: Vec<Option<Done>> = (0..ring_cap).map(|_| None).collect();
    let mut next = 0usize;
    let mut metas: Vec<EntryMeta> = Vec::with_capacity(entries.len());
    let mut cur: Option<(EntryMeta, Crc)> = None;
    let mut patches: Vec<(u64, Vec<u8>)> = Vec::new();
    let mut hdr = Vec::with_capacity(512);
    let mut skipped = 0usize;

    // Entries kept from an existing archive go first, compressed data copied unchanged.
    for e in kept {
        let data = e.data_offset(base)? as usize;
        let bytes = data
            .checked_add(e.csize as usize)
            .and_then(|end| base.get(data..end))
            .with_context(|| format!("{}: data extends past end of archive", e.name))?;
        let meta = e.to_meta(pos);
        hdr.clear();
        meta.local_header(&mut hdr);
        w.write_all(&hdr)?;
        w.write_all(bytes)?;
        pos += (hdr.len() + bytes.len()) as u64;
        metas.push(meta);
    }

    while next < jobs.len() {
        let d = rx.recv().context("worker thread exited unexpectedly")??;
        let slot = d.job % ring_cap;
        ring[slot] = Some(d);
        while let Some(d) = ring[next % ring_cap].take() {
            debug_assert_eq!(d.job, next);
            let job = &jobs[next];
            let mut off = 0;
            for (k, part) in d.parts.iter().enumerate() {
                let bytes = &d.data[off..off + part.len];
                off += part.len;
                match job.chunk {
                    None if part.skipped => skipped += 1,
                    None => {
                        let e = &entries[job.entry + k];
                        let (dos_time, dos_date, _) = dos_datetime(e.mtime);
                        let meta = EntryMeta {
                            name: &e.name,
                            is_dir: e.is_dir,
                            method: part.method,
                            crc: part.crc.crc,
                            csize: part.len as u64,
                            usize: e.size,
                            dos_time,
                            dos_date,
                            times: e.times,
                            attrs: e.attrs,
                            ext: None,
                            local_offset: pos,
                            chunks: None,
                        };
                        hdr.clear();
                        meta.local_header(&mut hdr);
                        w.write_all(&hdr)?;
                        w.write_all(bytes)?;
                        pos += (hdr.len() + bytes.len()) as u64;
                        metas.push(meta);
                    }
                    Some(index) => {
                        let e = &entries[job.entry];
                        if index == 0 {
                            let (dos_time, dos_date, _) = dos_datetime(e.mtime);
                            let meta = EntryMeta {
                                name: &e.name,
                                is_dir: false,
                                method: part.method,
                                crc: 0,
                                csize: 0,
                                usize: e.size,
                                dos_time,
                                dos_date,
                                times: e.times,
                                attrs: e.attrs,
                                ext: None,
                                local_offset: pos,
                                chunks: (part.method != METHOD_STORE).then(|| (e.chunk_size as u32, Vec::new())),
                            };
                            hdr.clear();
                            meta.local_header(&mut hdr);
                            w.write_all(&hdr)?;
                            pos += hdr.len() as u64;
                            cur = Some((meta, Crc::default()));
                        }
                        let (meta, crc) = cur.as_mut().unwrap();
                        w.write_all(bytes)?;
                        pos += bytes.len() as u64;
                        meta.csize += bytes.len() as u64;
                        if let Some((_, sizes)) = meta.chunks.as_mut() {
                            sizes.push(bytes.len() as u32);
                        }
                        *crc = crc.combine(part.crc);
                        if index + 1 == e.n_chunks {
                            let (mut meta, crc) = cur.take().unwrap();
                            meta.crc = crc.crc;
                            // CRC and sizes are patched into the local header once everything
                            // has been written, so the stream is never flushed mid-way.
                            let (o, bytes) = meta.local_patch();
                            patches.push((meta.local_offset + o, bytes));
                            if let Some((o, bytes)) = meta.local_zip64_csize_patch() {
                                patches.push((meta.local_offset + o, bytes.to_vec()));
                            }
                            metas.push(meta);
                        }
                    }
                }
            }
            pool.put(d.data);
            next += 1;
            gate.advance(next);
        }
    }

    let cd_offset = pos;
    let mut cd = Vec::with_capacity(metas.len() * 96);
    for m in &metas {
        m.central_header(&mut cd);
    }
    let cd_size = cd.len() as u64;
    end_of_central_directory(&mut cd, metas.len() as u64, cd_offset, cd_size);
    w.write_all(&cd)?;
    let end = cd_offset + cd.len() as u64;
    let f = w.into_inner().map_err(|e| e.into_error())?;
    for (off, bytes) in &patches {
        f.seek_write(bytes, *off)?;
    }
    f.set_len(end)?;
    set_delete_on_close(&f, false);
    std::mem::forget(metas);
    Ok((end, skipped))
}

pub fn human(n: u64) -> String {
    let units = ["B", "KiB", "MiB", "GiB", "TiB"];
    let mut v = n as f64;
    let mut u = 0;
    while v >= 1024.0 && u + 1 < units.len() {
        v /= 1024.0;
        u += 1;
    }
    format!("{v:.1} {}", units[u])
}

#[cfg(test)]
mod tests {
    use super::*;

    fn opts(threads: usize, auto_threads: bool, method: Method) -> CreateOpts {
        CreateOpts {
            method,
            level: 6,
            threads,
            auto_threads,
            chunk_size: 1 << 20,
            auto_chunk: true,
            store_incompressible: true,
            quiet: true,
        }
    }

    fn file(size: u64) -> Entry {
        let m = std::fs::metadata(std::env::current_exe().unwrap()).unwrap();
        Entry { size, ..new_entry("f".into(), PathBuf::new(), false, &m) }
    }

    #[test]
    fn auto_chunk_size_does_not_depend_on_threads() {
        let entries = [file(4_600_000), file(4_000_000), file(2_400_000)];
        for method in [Method::Deflate, Method::Zstd] {
            let base = auto_chunk_size(&entries, &opts(8, true, method), 20);
            for t in [1, 4, 12, 20] {
                assert_eq!(auto_chunk_size(&entries, &opts(t, true, method), 20), base, "{method:?} threads {t}");
            }
        }
    }

    #[test]
    fn chunks_are_planned_for_the_methods_default_thread_count() {
        // Four jobs per default thread: 8 for Deflate, every logical CPU for Zstandard.
        let entries = [file(95 << 20)];
        let big = |method| CreateOpts { chunk_size: 4 << 20, ..opts(1, false, method) };
        assert_eq!(default_threads(Method::Deflate, 20), CREATE_THREADS);
        assert_eq!(default_threads(Method::Zstd, 20), 20);
        assert_eq!(auto_chunk_size(&entries, &big(Method::Deflate), 20), 48 << 16);
        assert_eq!(auto_chunk_size(&entries, &big(Method::Zstd), 20), 19 << 16);
    }

    #[test]
    fn small_deflate_inputs_get_extra_workers_by_default_at_high_levels() {
        let o = CreateOpts { level: 9, ..opts(CREATE_THREADS, true, Method::Deflate) };
        assert_eq!(worker_threads(&o, 11 << 20, 28, 20), SMALL_INPUT_THREADS);
        assert_eq!(worker_threads(&CreateOpts { level: SMALL_INPUT_MIN_LEVEL, ..o }, 11 << 20, 28, 20), SMALL_INPUT_THREADS);
        // Never more than the machine has, or than there are jobs.
        assert_eq!(worker_threads(&o, 11 << 20, 28, 10), 10);
        assert_eq!(worker_threads(&o, 11 << 20, 5, 20), 5);
    }

    #[test]
    fn fast_levels_keep_the_default_workers() {
        // Below level 7 the extra workers cost CPU and memory without a useful gain in wall time.
        for level in [1, 6] {
            let o = CreateOpts { level, ..opts(CREATE_THREADS, true, Method::Deflate) };
            assert_eq!(worker_threads(&o, 11 << 20, 28, 20), CREATE_THREADS, "level {level}");
        }
    }

    #[test]
    fn large_inputs_and_explicit_thread_counts_are_kept() {
        assert_eq!(worker_threads(&opts(CREATE_THREADS, true, Method::Deflate), 200 << 20, 400, 20), CREATE_THREADS);
        assert_eq!(worker_threads(&opts(8, false, Method::Deflate), 11 << 20, 28, 20), 8);
        assert_eq!(worker_threads(&opts(3, false, Method::Deflate), 11 << 20, 28, 20), 3);
    }

    #[test]
    fn workers_still_follow_the_bytes_to_compress() {
        // Stored data is I/O-bound: two threads plus one per MiB that needs compressing.
        assert_eq!(worker_threads(&opts(CREATE_THREADS, true, Method::Deflate), 1 << 20, 50, 20), 3);
        assert_eq!(worker_threads(&opts(20, true, Method::Zstd), 0, 50, 20), 2);
    }
}
