mod codec;
mod create;
mod extract;
mod libdeflate;
mod zipfmt;

use anyhow::{bail, Context, Result};
use create::{CreateOpts, Method};
use extract::ExtractOpts;
use std::path::PathBuf;

const USAGE: &str = "\
blazezip - a very fast, parallel ZIP archiver

USAGE:
  blazezip c <archive.zip> <inputs...> [options]   create an archive (replacing any existing one)
  blazezip a <archive.zip> <inputs...> [options]   add to an archive (create it if missing)
  blazezip d <archive.zip> <names...>              delete entries (and directories' contents)
  blazezip x <archive.zip> [names...] [-o <dir>]   extract all entries, or only those named
  blazezip t <archive.zip>                         test (decode + verify CRCs)
  blazezip l <archive.zip>                         list entries

OPTIONS:
  -0 .. -12        compression level (default 6; 0 = store; deflate 10-12 = libdeflate max)
  --zstd           use Zstandard (zip method 93) instead of Deflate; levels 1-22, default 3
  -t, --threads N  worker threads (default: 8 for Deflate create, else all logical CPUs)
  --chunk MiB      chunk size for splitting large files (default 1 for deflate, 4 for zstd)
  --no-store       always compress, even data detected as incompressible
  -o, --out DIR    extraction directory (default: current directory)
  -q, --quiet      no summary output
  -V, --version    print the version

EXIT CODES:
  0  success
  1  error. Also returned when the job finished but not cleanly: input files that could not be
     read were left out of the archive (and named on stderr), or a damaged archive was only
     partly recovered. Nothing is ever left half-written: a failed create leaves no archive.
";

fn main() {
    disable_power_throttling();
    match run() {
        // Exit straight away rather than unwinding main's frame and running teardown.
        Ok(()) => std::process::exit(0),
        Err(e) => {
            eprintln!("blazezip: error: {e:#}");
            std::process::exit(1);
        }
    }
}

/// Windows 11 may run windowless processes under EcoQoS (efficiency cores, lower clocks).
/// blazezip is a foreground batch job, so opt out of execution-speed throttling.
fn disable_power_throttling() {
    #[repr(C)]
    struct PowerThrottlingState {
        version: u32,
        control_mask: u32,
        state_mask: u32,
    }
    #[link(name = "kernel32")]
    extern "system" {
        fn GetCurrentProcess() -> *mut std::ffi::c_void;
        fn SetProcessInformation(p: *mut std::ffi::c_void, class: i32, info: *const PowerThrottlingState, size: u32) -> i32;
    }
    const PROCESS_POWER_THROTTLING: i32 = 4;
    const EXECUTION_SPEED: u32 = 1;
    let s = PowerThrottlingState { version: 1, control_mask: EXECUTION_SPEED, state_mask: 0 };
    unsafe {
        SetProcessInformation(GetCurrentProcess(), PROCESS_POWER_THROTTLING, &s, std::mem::size_of::<PowerThrottlingState>() as u32);
    }
}

fn run() -> Result<()> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.is_empty() || matches!(args[0].as_str(), "-h" | "--help" | "help") {
        print!("{USAGE}");
        return Ok(());
    }
    if matches!(args[0].as_str(), "-V" | "--version") {
        println!("blazezip {}", env!("CARGO_PKG_VERSION"));
        return Ok(());
    }
    let cmd = args[0].as_str();
    let mut level: Option<i32> = None;
    let mut zstd = false;
    let logical = std::thread::available_parallelism().map(|n| n.get()).unwrap_or(4);
    let mut threads_arg: Option<usize> = None;
    let mut chunk_mib: Option<usize> = None;
    let mut store_incompressible = true;
    let mut out_dir = PathBuf::from(".");
    let mut quiet = false;
    let mut positional: Vec<PathBuf> = Vec::new();

    let mut it = args[1..].iter();
    while let Some(a) = it.next() {
        let mut val = |name: &str| it.next().cloned().with_context(|| format!("{name} needs a value"));
        match a.as_str() {
            "--zstd" => zstd = true,
            "-t" | "--threads" => threads_arg = Some(val(a)?.parse().context("bad thread count")?),
            "--chunk" => chunk_mib = Some(val(a)?.parse().context("bad chunk size")?),
            "--no-store" => store_incompressible = false,
            "-o" | "--out" => out_dir = PathBuf::from(val(a)?),
            "-q" | "--quiet" => quiet = true,
            s if s.starts_with("-l") && s.len() > 2 => level = Some(s[2..].parse().context("bad level")?),
            s if s.starts_with('-') && s[1..].chars().all(|c| c.is_ascii_digit()) && s.len() > 1 => {
                level = Some(s[1..].parse()?)
            }
            s if s.starts_with('-') && s.len() > 1 => bail!("unknown option {s}\n\n{USAGE}"),
            _ => positional.push(PathBuf::from(a)),
        }
    }

    match cmd {
        "c" | "create" | "a" | "add" | "u" | "update" => {
            if positional.len() < 2 {
                bail!("usage: blazezip c <archive.zip> <inputs...>");
            }
            let level = level.unwrap_or(if zstd { 3 } else { 6 });
            let method = if level == 0 {
                Method::Store
            } else if zstd {
                Method::Zstd
            } else {
                Method::Deflate
            };
            let chunk = chunk_mib.unwrap_or(if zstd { 4 } else { 1 }) << 20;
            let auto_chunk = chunk_mib.is_none();
            let threads = threads_arg.unwrap_or(create::default_threads(method, logical)).max(1);
            let auto_threads = threads_arg.is_none();
            let opts =
                CreateOpts { method, level, threads, auto_threads, chunk_size: chunk, auto_chunk, store_incompressible, quiet };
            if matches!(cmd, "c" | "create") {
                create::create(&positional[0], &positional[1..], &opts)
            } else {
                create::update(&positional[0], &positional[1..], &opts)
            }
        }
        "d" | "delete" => {
            if positional.len() < 2 {
                bail!("usage: blazezip d <archive.zip> <names...>");
            }
            let names: Vec<String> = positional[1..].iter().map(|p| p.to_string_lossy().into_owned()).collect();
            let opts = CreateOpts {
                method: Method::Deflate,
                level: 6,
                threads: 1,
                auto_threads: false,
                chunk_size: 1 << 20,
                auto_chunk: false,
                store_incompressible: true,
                quiet,
            };
            create::delete(&positional[0], &names, &opts)
        }
        "x" | "e" | "extract" | "t" | "test" => {
            let zip = positional.first().context("missing archive")?;
            let test_only = matches!(cmd, "t" | "test");
            if !test_only {
                std::fs::create_dir_all(&out_dir)?;
            }
            let threads = threads_arg.unwrap_or(logical).max(1);
            let only = positional[1..].iter().map(|p| p.to_string_lossy().into_owned()).collect();
            extract::extract(zip, &out_dir, &ExtractOpts { threads, test_only, quiet, only })
        }
        "l" | "list" => extract::list(positional.first().context("missing archive")?),
        _ => bail!("unknown command {cmd}\n\n{USAGE}"),
    }
}
