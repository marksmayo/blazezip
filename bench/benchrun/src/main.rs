//! benchrun: run a command inside a Windows Job Object and report resource usage as JSON.
//!
//! Using a job (rather than the single child process) means CPU time and peak memory include
//! every helper process a tool spawns. The child is created suspended and resumed only after it
//! is in the job, so nothing escapes accounting.
//!
//! usage: benchrun [--timeout SECS] [--cwd DIR] [--log FILE] [--stdout FILE] [--console]
//!                 [--cpus N] [--poll] [--watch PATH] [--temp-dir DIR] [--size-dir DIR]
//!                 [--interrupt-ms MS] [--mem-limit MIB] -- program args...
//! output (stdout, one line):
//!   {"exit":0,"timed_out":false,"wall_s":1.23,"user_s":4.5,"kernel_s":0.3,
//!    "peak_job_commit":123,"peak_proc_commit":123,"peak_ws":123,"procs":1,
//!    "page_faults":0,"read_bytes":0,"write_bytes":0,"read_ops":0,"write_ops":0, ...}
//!
//! Optional measurements:
//! - `--cpus N` limits the job to N logical processors, one per physical core (fastest first).
//! - `--poll` samples the job every 10 ms for peak threads, handles and total working set.
//! - `--watch PATH` records when PATH first holds data (a non-empty file, or a non-empty dir).
//! - `--temp-dir DIR` points TEMP/TMP at DIR and records its peak size; `--size-dir DIR` records
//!   the peak size of another directory (e.g. the output directory).
//! - `--interrupt-ms MS` sends Ctrl-C MS milliseconds after `--watch` fires (or after start),
//!   and records how long the tool took to exit.
//! - `--mem-limit MIB` caps the job's committed memory; allocations beyond it fail.

use std::os::windows::io::AsRawHandle;
use std::os::windows::process::CommandExt;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};
use windows_sys::Win32::Foundation::{CloseHandle, HANDLE, INVALID_HANDLE_VALUE};
use windows_sys::Win32::System::IO::{CreateIoCompletionPort, GetQueuedCompletionStatus, OVERLAPPED};
use windows_sys::Win32::System::JobObjects::*;
use windows_sys::Win32::System::ProcessStatus::{GetProcessMemoryInfo, PROCESS_MEMORY_COUNTERS};
use windows_sys::Win32::System::Threading::CREATE_SUSPENDED;

#[link(name = "kernel32")]
extern "system" {
    fn FreeConsole() -> i32;
    fn AllocConsole() -> i32;
    fn GetConsoleWindow() -> isize;
}
#[link(name = "user32")]
extern "system" {
    fn ShowWindow(hwnd: isize, cmd: i32) -> i32;
}

fn press_enter(conin: &std::fs::File) {
    use windows_sys::Win32::System::Console::{WriteConsoleInputW, INPUT_RECORD, KEY_EVENT};
    unsafe {
        let mut recs: [INPUT_RECORD; 2] = std::mem::zeroed();
        for (i, down) in [1, 0].into_iter().enumerate() {
            recs[i].EventType = KEY_EVENT as u16;
            let k = &mut recs[i].Event.KeyEvent;
            k.bKeyDown = down;
            k.wRepeatCount = 1;
            k.wVirtualKeyCode = 0x0D; // VK_RETURN
            k.wVirtualScanCode = 0x1C;
            k.uChar.UnicodeChar = 13;
        }
        let mut n = 0u32;
        WriteConsoleInputW(conin.as_raw_handle() as HANDLE, recs.as_ptr(), 2, &mut n);
    }
}

fn attach_hidden_console() {
    unsafe {
        FreeConsole();
        AllocConsole();
        let w = GetConsoleWindow();
        if w != 0 {
            ShowWindow(w, 0); // SW_HIDE
        }
    }
}

#[repr(C)]
struct ProcessEntry32W {
    size: u32,
    usage: u32,
    pid: u32,
    heap: usize,
    module: u32,
    threads: u32,
    parent: u32,
    pri: i32,
    flags: u32,
    exe: [u16; 260],
}

#[link(name = "kernel32")]
extern "system" {
    fn GetSystemCpuSetInformation(info: *mut u8, len: u32, returned: *mut u32, process: isize, flags: u32) -> i32;
    fn CreateToolhelp32Snapshot(flags: u32, pid: u32) -> HANDLE;
    fn Process32FirstW(snap: HANDLE, e: *mut ProcessEntry32W) -> i32;
    fn Process32NextW(snap: HANDLE, e: *mut ProcessEntry32W) -> i32;
    fn OpenProcess(access: u32, inherit: i32, pid: u32) -> HANDLE;
    fn GetProcessHandleCount(p: HANDLE, count: *mut u32) -> i32;
    fn SetConsoleCtrlHandler(handler: usize, add: i32) -> i32;
    fn GenerateConsoleCtrlEvent(event: u32, group: u32) -> i32;
}

/// Affinity mask of `n` logical processors (group 0): one per physical core, most performant
/// cores first, then hyperthread siblings.
fn cpu_mask(n: usize) -> usize {
    let mut len = 0u32;
    unsafe { GetSystemCpuSetInformation(std::ptr::null_mut(), 0, &mut len, 0, 0) };
    let mut buf = vec![0u8; len as usize];
    if unsafe { GetSystemCpuSetInformation(buf.as_mut_ptr(), len, &mut len, 0, 0) } == 0 {
        return usize::MAX;
    }
    // SYSTEM_CPU_SET_INFORMATION: Size u32, Type u32, Id u32, Group u16, LogicalProcessorIndex u8,
    // CoreIndex u8, LastLevelCacheIndex u8, NumaNodeIndex u8, EfficiencyClass u8, ...
    let mut cpus = Vec::new();
    let mut off = 0usize;
    while off + 20 <= len as usize {
        let b = &buf[off..];
        let size = u32::from_le_bytes(b[0..4].try_into().unwrap()) as usize;
        if size == 0 {
            break;
        }
        if u32::from_le_bytes(b[4..8].try_into().unwrap()) == 0 && u16::from_le_bytes([b[12], b[13]]) == 0 {
            cpus.push((b[18], b[15], b[14])); // (efficiency class, core, logical processor)
        }
        off += size;
    }
    cpus.sort_by(|a, b| b.0.cmp(&a.0).then(a.1.cmp(&b.1)).then(a.2.cmp(&b.2)));
    let (mut first, mut rest) = (Vec::new(), Vec::new());
    for (i, c) in cpus.iter().enumerate() {
        if i == 0 || cpus[i - 1].1 != c.1 {
            first.push(c.2)
        } else {
            rest.push(c.2)
        }
    }
    first.extend(rest);
    first.iter().take(n.max(1)).fold(0usize, |m, &l| m | (1usize << l))
}

fn dir_size(p: &std::path::Path) -> u64 {
    let Ok(rd) = std::fs::read_dir(p) else { return 0 };
    rd.flatten()
        .map(|e| match e.file_type() {
            Ok(t) if t.is_dir() => dir_size(&e.path()),
            _ => e.metadata().map(|m| m.len()).unwrap_or(0),
        })
        .sum()
}

fn has_data(p: &std::path::Path) -> bool {
    match std::fs::metadata(p) {
        Ok(m) if m.is_dir() => std::fs::read_dir(p).map(|mut r| r.next().is_some()).unwrap_or(false),
        Ok(m) => m.len() > 0,
        Err(_) => false,
    }
}

/// Peaks sampled while the job runs.
#[derive(Default)]
struct Peaks {
    threads: u64,
    handles: u64,
    ws: u64,
    temp: u64,
    size_dir: u64,
    first_output_s: Option<f64>,
    interrupted_s: Option<f64>,
}

fn job_pids(job: HANDLE) -> Vec<u32> {
    // JOBOBJECT_BASIC_PROCESS_ID_LIST: two u32 counts, then the ids as ULONG_PTRs.
    let mut buf = vec![0usize; 2050];
    let ok = unsafe {
        QueryInformationJobObject(job, JobObjectBasicProcessIdList, buf.as_mut_ptr().cast(), (buf.len() * 8) as u32, std::ptr::null_mut())
    };
    if ok == 0 {
        return Vec::new();
    }
    let n = (buf[0] >> 32) as usize;
    buf[1..1 + n.min(2048)].iter().map(|&p| p as u32).collect()
}

/// Threads, handles and working set summed over the job's processes.
fn sample(job: HANDLE) -> (u64, u64, u64) {
    let pids = job_pids(job);
    let (mut threads, mut handles, mut ws) = (0u64, 0u64, 0u64);
    unsafe {
        let snap = CreateToolhelp32Snapshot(2, 0); // TH32CS_SNAPPROCESS
        if snap != INVALID_HANDLE_VALUE {
            let mut e: ProcessEntry32W = std::mem::zeroed();
            e.size = std::mem::size_of::<ProcessEntry32W>() as u32;
            let mut ok = Process32FirstW(snap, &mut e);
            while ok != 0 {
                if pids.contains(&e.pid) {
                    threads += e.threads as u64;
                }
                ok = Process32NextW(snap, &mut e);
            }
            CloseHandle(snap);
        }
        for &pid in &pids {
            let h = OpenProcess(0x1000 | 0x0010, 0, pid); // QUERY_LIMITED_INFORMATION | VM_READ
            if h.is_null() {
                continue;
            }
            let mut n = 0u32;
            if GetProcessHandleCount(h, &mut n) != 0 {
                handles += n as u64;
            }
            let mut pmc: PROCESS_MEMORY_COUNTERS = std::mem::zeroed();
            pmc.cb = std::mem::size_of::<PROCESS_MEMORY_COUNTERS>() as u32;
            if GetProcessMemoryInfo(h, &mut pmc, pmc.cb) != 0 {
                ws += pmc.WorkingSetSize as u64;
            }
            CloseHandle(h);
        }
    }
    (threads, handles, ws)
}

#[link(name = "ntdll")]
extern "system" {
    fn NtResumeProcess(h: HANDLE) -> i32;
}

/// winnt.h; windows-sys files it under a feature this crate does not otherwise need.
const JOB_OBJECT_MSG_ACTIVE_PROCESS_ZERO: u32 = 4;

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let mut timeout = 3600.0f64;
    let mut cwd: Option<String> = None;
    let mut log: Option<String> = None;
    let mut stdout_file: Option<String> = None;
    let mut console = false;
    let mut cpus: Option<usize> = None;
    let mut poll = false;
    let mut watch: Option<String> = None;
    let mut temp_dir: Option<String> = None;
    let mut size_dir: Option<String> = None;
    let mut interrupt_ms: Option<u64> = None;
    let mut mem_limit: Option<usize> = None;
    let mut i = 0;
    while i < args.len() && args[i] != "--" {
        match args[i].as_str() {
            "--timeout" => {
                timeout = args[i + 1].parse().expect("bad timeout");
                i += 1;
            }
            "--cwd" => {
                cwd = Some(args[i + 1].clone());
                i += 1;
            }
            "--stdout" => {
                stdout_file = Some(args[i + 1].clone());
                i += 1;
            }
            "--console" => console = true,
            "--poll" => poll = true,
            "--cpus" => {
                cpus = Some(args[i + 1].parse().expect("bad cpu count"));
                i += 1;
            }
            "--watch" => {
                watch = Some(args[i + 1].clone());
                i += 1;
            }
            "--temp-dir" => {
                temp_dir = Some(args[i + 1].clone());
                i += 1;
            }
            "--size-dir" => {
                size_dir = Some(args[i + 1].clone());
                i += 1;
            }
            "--interrupt-ms" => {
                interrupt_ms = Some(args[i + 1].parse().expect("bad interrupt delay"));
                i += 1;
            }
            "--mem-limit" => {
                mem_limit = Some(args[i + 1].parse().expect("bad memory limit"));
                i += 1;
            }
            "--log" => {
                log = Some(args[i + 1].clone());
                i += 1;
            }
            a => panic!("unknown option {a}"),
        }
        i += 1;
    }
    let cmd = &args[i + 1..];
    assert!(!cmd.is_empty(), "usage: benchrun [opts] -- program args...");

    unsafe {
        let job = CreateJobObjectW(std::ptr::null(), std::ptr::null());
        let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        if let Some(n) = cpus {
            info.BasicLimitInformation.LimitFlags |= JOB_OBJECT_LIMIT_AFFINITY;
            info.BasicLimitInformation.Affinity = cpu_mask(n);
        }
        if let Some(mib) = mem_limit {
            info.BasicLimitInformation.LimitFlags |= JOB_OBJECT_LIMIT_JOB_MEMORY;
            info.JobMemoryLimit = mib << 20;
        }
        SetInformationJobObject(
            job,
            JobObjectExtendedLimitInformation,
            &info as *const _ as *const _,
            std::mem::size_of_val(&info) as u32,
        );
        // The job posts JOB_OBJECT_MSG_ACTIVE_PROCESS_ZERO to this port the moment its last
        // process exits, so the clock stops exactly then instead of at the next poll.
        let port = CreateIoCompletionPort(INVALID_HANDLE_VALUE, std::ptr::null_mut(), 0, 1);
        let assoc = JOBOBJECT_ASSOCIATE_COMPLETION_PORT { CompletionKey: job as *mut _, CompletionPort: port };
        SetInformationJobObject(
            job,
            JobObjectAssociateCompletionPortInformation,
            &assoc as *const _ as *const _,
            std::mem::size_of_val(&assoc) as u32,
        );

        let mut c = Command::new(&cmd[0]);
        c.args(&cmd[1..]).creation_flags(CREATE_SUSPENDED).stdin(Stdio::null());
        if let Some(d) = &cwd {
            c.current_dir(d);
        }
        if let Some(t) = &temp_dir {
            std::fs::create_dir_all(t).expect("temp dir");
            c.env("TEMP", t).env("TMP", t);
        }
        if interrupt_ms.is_some() {
            // Ctrl-C goes to every process on a console: share a hidden one with the child and
            // ignore the event here.
            if !console {
                attach_hidden_console();
            }
            // Processes started in a new process group inherit "Ctrl-C disabled"; re-enable it
            // so the child (which inherits this setting) handles Ctrl-C normally.
            SetConsoleCtrlHandler(0, 0);
        }
        match &log {
            Some(p) => {
                let f = std::fs::File::create(p).expect("log file");
                c.stdout(f.try_clone().unwrap()).stderr(f);
            }
            None => {
                c.stdout(Stdio::null()).stderr(Stdio::null());
            }
        }
        if let Some(p) = &stdout_file {
            c.stdout(std::fs::File::create(p).expect("stdout file"));
        }
        if console {
            // Some tools (WinZip's wzzip) hang unless their standard handles are a real console.
            // Give the child a hidden console of our own and hand it the console handles.
            attach_hidden_console();
            let rw = |p: &str| std::fs::OpenOptions::new().read(true).write(true).open(p).expect(p);
            c.stdin(rw("CONIN$"));
            if log.is_none() && stdout_file.is_none() {
                c.stdout(rw("CONOUT$")).stderr(rw("CONOUT$"));
            }
            // Answer "press any key" pauses (e.g. WinZip's add-on) by queueing an Enter key
            // every 100 ms; a prompt that flushes earlier input still gets the next one.
            let conin = rw("CONIN$");
            std::thread::spawn(move || loop {
                press_enter(&conin);
                std::thread::sleep(Duration::from_millis(100));
            });
        }
        let start = Instant::now();
        let mut child = match c.spawn() {
            Ok(ch) => ch,
            Err(e) => {
                println!("{{\"exit\":-1,\"error\":{:?}}}", e.to_string());
                std::process::exit(2);
            }
        };
        let ph = child.as_raw_handle() as HANDLE;
        if interrupt_ms.is_some() {
            // Only now: a process that ignores Ctrl-C passes that on to children it creates.
            SetConsoleCtrlHandler(0, 1);
        }
        AssignProcessToJobObject(job, ph);
        NtResumeProcess(ph);

        let peaks = std::sync::Arc::new(std::sync::Mutex::new(Peaks::default()));
        let done = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
        let watching = poll || watch.is_some() || temp_dir.is_some() || size_dir.is_some() || interrupt_ms.is_some();
        let monitor = watching.then(|| {
            let (peaks, done) = (peaks.clone(), done.clone());
            let (watch, temp_dir, size_dir) = (watch.clone(), temp_dir.clone(), size_dir.clone());
            let job_raw = job as usize;
            std::thread::spawn(move || {
                let job = job_raw as HANDLE;
                let mut last_slow: Option<Instant> = None;
                while !done.load(std::sync::atomic::Ordering::Relaxed) {
                    let now = start.elapsed().as_secs_f64();
                    let mut p = peaks.lock().unwrap();
                    if let Some(w) = &watch {
                        if p.first_output_s.is_none() && has_data(std::path::Path::new(w)) {
                            p.first_output_s = Some(now);
                        }
                    }
                    if let (Some(ms), None) = (interrupt_ms, p.interrupted_s) {
                        let from = if watch.is_some() { p.first_output_s } else { Some(0.0) };
                        if from.is_some_and(|f| now >= f + ms as f64 / 1000.0) {
                            GenerateConsoleCtrlEvent(0, 0); // CTRL_C_EVENT to the whole console
                            p.interrupted_s = Some(now);
                        }
                    }
                    if last_slow.is_none_or(|t| t.elapsed() >= Duration::from_millis(10)) {
                        last_slow = Some(Instant::now());
                        if poll {
                            let (t, h, ws) = sample(job);
                            p.threads = p.threads.max(t);
                            p.handles = p.handles.max(h);
                            p.ws = p.ws.max(ws);
                        }
                        if let Some(d) = &temp_dir {
                            p.temp = p.temp.max(dir_size(std::path::Path::new(d)));
                        }
                        if let Some(d) = &size_dir {
                            p.size_dir = p.size_dir.max(dir_size(std::path::Path::new(d)));
                        }
                    }
                    drop(p);
                    std::thread::sleep(Duration::from_millis(1));
                }
            })
        });

        let deadline = Duration::from_secs_f64(timeout);
        let mut timed_out = false;
        // Wait for every process in the job (the tool and any helpers it launched) to exit.
        loop {
            let left = deadline.saturating_sub(start.elapsed());
            let (mut code, mut key, mut ov) = (0u32, 0usize, std::ptr::null_mut::<OVERLAPPED>());
            let ok = GetQueuedCompletionStatus(port, &mut code, &mut key, &mut ov, left.as_millis().min(u32::MAX as u128 - 1) as u32);
            if ok == 0 && ov.is_null() {
                timed_out = true;
                TerminateJobObject(job, 1);
                break;
            }
            if code == JOB_OBJECT_MSG_ACTIVE_PROCESS_ZERO {
                break;
            }
        }
        let wall = start.elapsed().as_secs_f64();
        done.store(true, std::sync::atomic::Ordering::Relaxed);
        if let Some(m) = monitor {
            let _ = m.join();
        }
        let peaks = peaks.lock().unwrap();
        let status = if timed_out { let _ = child.wait(); -1 } else { child.wait().map(|s| s.code().unwrap_or(-1)).unwrap_or(-1) };

        let mut io: JOBOBJECT_BASIC_AND_IO_ACCOUNTING_INFORMATION = std::mem::zeroed();
        QueryInformationJobObject(
            job,
            JobObjectBasicAndIoAccountingInformation,
            &mut io as *mut _ as *mut _,
            std::mem::size_of_val(&io) as u32,
            std::ptr::null_mut(),
        );
        let acct = io.BasicInfo;
        let mut ext: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
        QueryInformationJobObject(
            job,
            JobObjectExtendedLimitInformation,
            &mut ext as *mut _ as *mut _,
            std::mem::size_of_val(&ext) as u32,
            std::ptr::null_mut(),
        );
        let mut pmc: PROCESS_MEMORY_COUNTERS = std::mem::zeroed();
        pmc.cb = std::mem::size_of::<PROCESS_MEMORY_COUNTERS>() as u32;
        GetProcessMemoryInfo(ph, &mut pmc, pmc.cb);

        let opt = |v: Option<f64>| v.map(|v| format!("{v:.6}")).unwrap_or_else(|| "null".into());
        let mut extra = format!(
            ",\"page_faults\":{},\"read_bytes\":{},\"write_bytes\":{},\"read_ops\":{},\"write_ops\":{}",
            acct.TotalPageFaultCount,
            io.IoInfo.ReadTransferCount,
            io.IoInfo.WriteTransferCount,
            io.IoInfo.ReadOperationCount,
            io.IoInfo.WriteOperationCount
        );
        if poll {
            extra += &format!(",\"peak_threads\":{},\"peak_handles\":{},\"peak_job_ws\":{}", peaks.threads, peaks.handles, peaks.ws);
        }
        if watch.is_some() {
            extra += &format!(",\"first_output_s\":{}", opt(peaks.first_output_s));
        }
        if temp_dir.is_some() {
            extra += &format!(",\"peak_temp_bytes\":{}", peaks.temp);
        }
        if size_dir.is_some() {
            extra += &format!(",\"peak_size_dir_bytes\":{}", peaks.size_dir);
        }
        if let Some(mib) = mem_limit {
            extra += &format!(",\"mem_limit_mib\":{mib}");
        }
        if interrupt_ms.is_some() {
            extra += &format!(
                ",\"interrupted_s\":{},\"exit_after_interrupt_s\":{}",
                opt(peaks.interrupted_s),
                opt(peaks.interrupted_s.map(|t| wall - t))
            );
        }
        println!(
            "{{\"exit\":{},\"timed_out\":{},\"wall_s\":{:.6},\"user_s\":{:.6},\"kernel_s\":{:.6},\"peak_job_commit\":{},\"peak_proc_commit\":{},\"peak_ws\":{},\"procs\":{}{}}}",
            status,
            timed_out,
            wall,
            acct.TotalUserTime as f64 / 1e7,
            acct.TotalKernelTime as f64 / 1e7,
            ext.PeakJobMemoryUsed,
            ext.PeakProcessMemoryUsed,
            pmc.PeakWorkingSetSize,
            acct.TotalProcesses,
            extra
        );
        CloseHandle(port);
        CloseHandle(job);
    }
}
