//! ZIP on-disk structures: writing local/central headers and parsing the central directory.
//!
//! Spec reference: PKWARE APPNOTE.TXT 6.3.x. Zip64 is used automatically when needed.

use anyhow::{bail, Context, Result};
use std::time::{SystemTime, UNIX_EPOCH};

pub const SIG_LOCAL: u32 = 0x0403_4b50;
pub const SIG_CENTRAL: u32 = 0x0201_4b50;
pub const SIG_EOCD: u32 = 0x0605_4b50;
pub const SIG_EOCD64: u32 = 0x0606_4b50;
pub const SIG_EOCD64_LOC: u32 = 0x0706_4b50;

pub const METHOD_STORE: u16 = 0;
pub const METHOD_DEFLATE: u16 = 8;
pub const METHOD_ZSTD: u16 = 93;

pub const FLAG_UTF8: u16 = 0x0800;

pub const EXTRA_ZIP64: u16 = 0x0001;
pub const EXTRA_EXT_TIME: u16 = 0x5455;
/// NTFS timestamps (modified, accessed, created) in 100 ns units.
pub const EXTRA_NTFS: u16 = 0x000a;
/// Size of the NTFS extra field: header, reserved word, one tag of three FILETIMEs.
const NTFS_EXTRA_LEN: u16 = 4 + 4 + 4 + 24;

/// DOS attribute bits kept in the external attributes.
pub const ATTR_READONLY: u32 = 0x01;
pub const ATTR_HIDDEN: u32 = 0x02;
pub const ATTR_SYSTEM: u32 = 0x04;
pub const ATTR_DIRECTORY: u32 = 0x10;
pub const ATTR_ARCHIVE: u32 = 0x20;

/// 100 ns intervals between 1601-01-01 (FILETIME epoch) and 1970-01-01.
const FILETIME_UNIX_OFFSET: u64 = 116_444_736_000_000_000;

pub fn filetime_from_system_time(t: SystemTime) -> u64 {
    let d = t.duration_since(UNIX_EPOCH).unwrap_or_default();
    FILETIME_UNIX_OFFSET + d.as_secs() * 10_000_000 + d.subsec_nanos() as u64 / 100
}

pub fn system_time_from_filetime(ft: u64) -> SystemTime {
    let t = ft.saturating_sub(FILETIME_UNIX_OFFSET);
    // A damaged archive can hold times far past what SystemTime represents: clamp, don't panic.
    let d = std::time::Duration::new(t / 10_000_000, (t % 10_000_000) as u32 * 100);
    UNIX_EPOCH.checked_add(d).unwrap_or_else(|| system_time_from_filetime(MAX_FILETIME))
}

/// The latest time Windows file times can hold (30828-09-14).
const MAX_FILETIME: u64 = 0x7FFF_35F4_F06C_7FFF;
/// blazezip chunk index: lets blazezip decompress a single large entry in parallel.
/// Other unzip tools ignore unknown extra fields, so archives stay fully standard.
pub const EXTRA_BLAZE_CHUNKS: u16 = 0x7a42;

/// Local header fields that need zip64 once sizes reach this value. We are conservative so a
/// compressed stream that grows slightly past its input still fits.
pub const ZIP64_LOCAL_THRESHOLD: u64 = 0xFFFF_0000;

pub fn put16(v: &mut Vec<u8>, x: u16) {
    v.extend_from_slice(&x.to_le_bytes());
}
pub fn put32(v: &mut Vec<u8>, x: u32) {
    v.extend_from_slice(&x.to_le_bytes());
}
pub fn put64(v: &mut Vec<u8>, x: u64) {
    v.extend_from_slice(&x.to_le_bytes());
}

fn rd16(b: &[u8], o: usize) -> u16 {
    u16::from_le_bytes([b[o], b[o + 1]])
}
fn rd32(b: &[u8], o: usize) -> u32 {
    u32::from_le_bytes(b[o..o + 4].try_into().unwrap())
}
fn rd64(b: &[u8], o: usize) -> u64 {
    u64::from_le_bytes(b[o..o + 8].try_into().unwrap())
}

/// Convert a timestamp to MS-DOS (time, date). DOS time has 2-second resolution and no zone;
/// we store UTC and also emit the extended-timestamp extra field with the exact unix time.
pub fn dos_datetime(t: SystemTime) -> (u16, u16, u32) {
    let secs = t.duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0);
    let unix = secs.min(u32::MAX as u64) as u32;
    let days = (secs / 86400) as i64;
    let rem = secs % 86400;
    let (y, m, d) = civil_from_days(days);
    if y < 1980 {
        return (0, (1 << 5) | 1, unix);
    }
    let time = ((rem / 3600) << 11) as u16 | (((rem % 3600) / 60) << 5) as u16 | ((rem % 60) / 2) as u16;
    let date = (((y - 1980) as u16) << 9) | ((m as u16) << 5) | d as u16;
    (time, date, unix)
}

pub fn dos_to_system_time(time: u16, date: u16) -> SystemTime {
    let y = 1980 + (date >> 9) as i64;
    let m = ((date >> 5) & 0xf).clamp(1, 12) as u32;
    let d = (date & 0x1f).max(1) as u32;
    let days = days_from_civil(y, m, d);
    let secs = days * 86400 + (time >> 11) as i64 * 3600 + ((time >> 5) & 0x3f) as i64 * 60 + (time & 0x1f) as i64 * 2;
    UNIX_EPOCH + std::time::Duration::from_secs(secs.max(0) as u64)
}

// Howard Hinnant's civil date algorithms.
fn civil_from_days(z: i64) -> (i64, u32, u32) {
    let z = z + 719468;
    let era = z.div_euclid(146097);
    let doe = z.rem_euclid(146097);
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    (if m <= 2 { y + 1 } else { y }, m, d)
}

fn days_from_civil(y: i64, m: u32, d: u32) -> i64 {
    let y = if m <= 2 { y - 1 } else { y };
    let era = y.div_euclid(400);
    let yoe = y.rem_euclid(400);
    let mp = if m > 2 { m - 3 } else { m + 9 } as i64;
    let doy = (153 * mp + 2) / 5 + d as i64 - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146097 + doe - 719468
}

/// Everything needed to emit both the local and central header of one entry.
#[derive(Clone, Debug)]
pub struct EntryMeta<'a> {
    pub name: &'a str,
    pub is_dir: bool,
    pub method: u16,
    pub crc: u32,
    pub csize: u64,
    pub usize: u64,
    pub dos_time: u16,
    pub dos_date: u16,
    /// Modified, accessed and created times as FILETIMEs (100 ns since 1601).
    pub times: [u64; 3],
    /// DOS attribute bits (read-only, hidden, system, archive).
    pub attrs: u32,
    /// "Version made by" and external attributes to keep verbatim (entries copied from an
    /// existing archive, which may come from another host system).
    pub ext: Option<(u16, u32)>,
    pub local_offset: u64,
    /// (uncompressed chunk size, compressed size of each chunk) when chunked.
    pub chunks: Option<(u32, Vec<u32>)>,
}

impl EntryMeta<'_> {
    fn version_needed(&self, zip64: bool) -> u16 {
        match self.method {
            METHOD_ZSTD => 63,
            _ if zip64 => 45,
            METHOD_DEFLATE => 20,
            _ if self.is_dir => 20,
            _ => 10,
        }
    }

    fn flags(&self) -> u16 {
        if self.name.is_ascii() {
            0
        } else {
            FLAG_UTF8
        }
    }

    pub fn local_needs_zip64(&self) -> bool {
        self.usize >= ZIP64_LOCAL_THRESHOLD
    }

    /// Append the local file header to `v`. The CRC field sits 14 bytes in, so a writer can
    /// patch CRC/sizes later for streamed entries (see `local_patch`).
    pub fn local_header(&self, v: &mut Vec<u8>) {
        let z64 = self.local_needs_zip64();
        v.reserve(30 + self.name.len() + 29);
        put32(v, SIG_LOCAL);
        put16(v, self.version_needed(z64));
        put16(v, self.flags());
        put16(v, self.method);
        put16(v, self.dos_time);
        put16(v, self.dos_date);
        put32(v, self.crc);
        if z64 {
            put32(v, u32::MAX);
            put32(v, u32::MAX);
        } else {
            put32(v, self.csize as u32);
            put32(v, self.usize as u32);
        }
        put16(v, self.name.len() as u16);
        // Timestamps live only in the central directory, as 7-Zip and Windows also do.
        put16(v, if z64 { 20 } else { 0 });
        v.extend_from_slice(self.name.as_bytes());
        if z64 {
            put16(v, EXTRA_ZIP64);
            put16(v, 16);
            put64(v, self.usize);
            put64(v, self.csize);
        }
    }

    /// Bytes to overwrite at local_offset+14 once CRC and compressed size are known.
    pub fn local_patch(&self) -> (u64, Vec<u8>) {
        let mut v = Vec::new();
        put32(&mut v, self.crc);
        // With zip64 the size fields stay 0xFFFFFFFF; the real sizes live in the extra field.
        if !self.local_needs_zip64() {
            put32(&mut v, self.csize as u32);
            put32(&mut v, self.usize as u32);
        }
        (14, v)
    }

    /// Offset (relative to local header start) and bytes of the zip64 csize field, if present.
    pub fn local_zip64_csize_patch(&self) -> Option<(u64, [u8; 8])> {
        if self.local_needs_zip64() {
            let off = 30 + self.name.len() as u64 + 4 + 8;
            Some((off, self.csize.to_le_bytes()))
        } else {
            None
        }
    }

    pub fn central_header(&self, out: &mut Vec<u8>) {
        let need_u = self.usize >= u32::MAX as u64;
        let need_c = self.csize >= u32::MAX as u64;
        let need_o = self.local_offset >= u32::MAX as u64;
        let z64 = need_u || need_c || need_o;
        let n64 = (need_u as u16 + need_c as u16 + need_o as u16) * 8;
        let extra_len = NTFS_EXTRA_LEN
            + if z64 { 4 + n64 } else { 0 }
            + self.chunks.as_ref().map_or(0, |c| 9 + 4 * c.1.len() as u16);
        out.reserve(46 + self.name.len() + extra_len as usize);
        put32(out, SIG_CENTRAL);
        put16(out, self.ext.map_or(0x003F, |e| e.0)); // made by: MS-DOS/Windows attrs, spec 6.3
        put16(out, self.version_needed(z64));
        put16(out, self.flags());
        put16(out, self.method);
        put16(out, self.dos_time);
        put16(out, self.dos_date);
        put32(out, self.crc);
        put32(out, if need_c { u32::MAX } else { self.csize as u32 });
        put32(out, if need_u { u32::MAX } else { self.usize as u32 });
        put16(out, self.name.len() as u16);
        put16(out, extra_len);
        put16(out, 0); // comment
        put16(out, 0); // disk
        put16(out, 0); // internal attrs
        put32(
            out,
            self.ext.map_or(
                self.attrs & (ATTR_READONLY | ATTR_HIDDEN | ATTR_SYSTEM | ATTR_ARCHIVE) | if self.is_dir { ATTR_DIRECTORY } else { 0 },
                |e| e.1,
            ),
        );
        put32(out, if need_o { u32::MAX } else { self.local_offset as u32 });
        out.extend_from_slice(self.name.as_bytes());
        put16(out, EXTRA_NTFS);
        put16(out, NTFS_EXTRA_LEN - 4);
        put32(out, 0); // reserved
        put16(out, 1); // tag 1: times
        put16(out, 24);
        for t in self.times {
            put64(out, t);
        }
        if z64 {
            put16(out, EXTRA_ZIP64);
            put16(out, n64);
            if need_u {
                put64(out, self.usize);
            }
            if need_c {
                put64(out, self.csize);
            }
            if need_o {
                put64(out, self.local_offset);
            }
        }
        if let Some((chunk, sizes)) = &self.chunks {
            put16(out, EXTRA_BLAZE_CHUNKS);
            put16(out, (5 + 4 * sizes.len()) as u16);
            out.push(1);
            put32(out, *chunk);
            for s in sizes {
                put32(out, *s);
            }
        }
    }
}

/// Max chunks representable in one extra field (65535 bytes total).
pub const MAX_CHUNKS: usize = (65535 - 4 - 5) / 4;

pub fn end_of_central_directory(out: &mut Vec<u8>, entries: u64, cd_offset: u64, cd_size: u64) {
    let z64 = entries >= 0xFFFF || cd_offset >= u32::MAX as u64 || cd_size >= u32::MAX as u64;
    if z64 {
        let eocd64_off = cd_offset + cd_size;
        put32(out, SIG_EOCD64);
        put64(out, 44);
        put16(out, 0x003F);
        put16(out, 45);
        put32(out, 0);
        put32(out, 0);
        put64(out, entries);
        put64(out, entries);
        put64(out, cd_size);
        put64(out, cd_offset);
        put32(out, SIG_EOCD64_LOC);
        put32(out, 0);
        put64(out, eocd64_off);
        put32(out, 1);
    }
    put32(out, SIG_EOCD);
    put16(out, 0);
    put16(out, 0);
    let e16 = if z64 { 0xFFFF } else { entries as u16 };
    put16(out, e16);
    put16(out, e16);
    put32(out, if z64 { u32::MAX } else { cd_size as u32 });
    put32(out, if z64 { u32::MAX } else { cd_offset as u32 });
    put16(out, 0);
}

/// A parsed central directory entry plus the resolved start of its compressed data.
#[derive(Debug, Clone)]
pub struct CdEntry {
    pub name: String,
    pub method: u16,
    pub flags: u16,
    pub crc: u32,
    pub csize: u64,
    pub usize: u64,
    pub dos_time: u16,
    pub dos_date: u16,
    pub unix_mtime: Option<u32>,
    /// Modified, accessed and created FILETIMEs from an NTFS extra field.
    pub ntfs_times: Option<[u64; 3]>,
    /// "Version made by" (host system in the high byte) and external attributes.
    pub made_by: u16,
    pub ext_attrs: u32,
    pub local_offset: u64,
    pub chunks: Option<(u32, Vec<u32>)>,
}

impl CdEntry {
    pub fn is_dir(&self) -> bool {
        self.name.ends_with('/') || self.name.ends_with('\\')
    }

    /// Start of the entry's compressed data. Reading the local header is a random access into
    /// the archive, so this is done lazily by whichever thread decodes the entry.
    pub fn data_offset(&self, buf: &[u8]) -> Result<u64> {
        let lo = self.local_offset as usize;
        if lo.checked_add(30).is_none_or(|end| end > buf.len()) || rd32(buf, lo) != SIG_LOCAL {
            bail!("bad local header for {}", self.name);
        }
        Ok(self.local_offset + 30 + rd16(buf, lo + 26) as u64 + rd16(buf, lo + 28) as u64)
    }
}

impl CdEntry {
    /// Times as FILETIMEs, from the NTFS field, else the Unix mtime, else the DOS time.
    pub fn filetimes(&self) -> [u64; 3] {
        self.ntfs_times.unwrap_or_else(|| {
            let t = match self.unix_mtime {
                Some(u) => UNIX_EPOCH + std::time::Duration::from_secs(u as u64),
                None => dos_to_system_time(self.dos_time, self.dos_date),
            };
            let ft = filetime_from_system_time(t);
            [ft, ft, ft]
        })
    }

    /// Headers for writing this entry, unchanged, into a new archive at `local_offset`.
    pub fn to_meta(&self, local_offset: u64) -> EntryMeta<'_> {
        EntryMeta {
            name: &self.name,
            is_dir: self.is_dir(),
            method: self.method,
            crc: self.crc,
            csize: self.csize,
            usize: self.usize,
            dos_time: self.dos_time,
            dos_date: self.dos_date,
            times: self.filetimes(),
            attrs: self.ext_attrs,
            ext: Some((self.made_by, self.ext_attrs)),
            local_offset,
            chunks: self.chunks.clone(),
        }
    }
}

/// Rebuild the entry list of an archive whose central directory is missing or damaged (say, a
/// truncated download) by walking the local headers from the start. Stops at the first entry
/// whose sizes are unknown (data descriptor) or whose data runs past the end of the file.
pub fn scan_local_headers(buf: &[u8]) -> Vec<CdEntry> {
    let mut out = Vec::new();
    let mut p = 0usize;
    while p + 30 <= buf.len() && rd32(buf, p) == SIG_LOCAL {
        let flags = rd16(buf, p + 6);
        let nlen = rd16(buf, p + 26) as usize;
        let xlen = rd16(buf, p + 28) as usize;
        if flags & 0x0009 != 0 || p + 30 + nlen + xlen > buf.len() {
            break; // encrypted, or sizes only in a trailing data descriptor
        }
        let mut csize = rd32(buf, p + 18) as u64;
        let mut usize_ = rd32(buf, p + 22) as u64;
        let name_bytes = &buf[p + 30..p + 30 + nlen];
        let extra = &buf[p + 30 + nlen..p + 30 + nlen + xlen];
        let mut unix_mtime = None;
        let mut x = 0;
        while x + 4 <= extra.len() {
            let id = rd16(extra, x);
            let len = rd16(extra, x + 2) as usize;
            let body = &extra[x + 4..(x + 4 + len).min(extra.len())];
            match id {
                EXTRA_ZIP64 if body.len() >= 16 => {
                    usize_ = rd64(body, 0);
                    csize = rd64(body, 8);
                }
                EXTRA_EXT_TIME if body.len() >= 5 && body[0] & 1 != 0 => unix_mtime = Some(rd32(body, 1)),
                _ => {}
            }
            x += 4 + len;
        }
        let data = (p + 30 + nlen + xlen) as u64;
        let Some(end) = data.checked_add(csize).filter(|&e| e <= buf.len() as u64) else { break };
        out.push(CdEntry {
            name: if flags & FLAG_UTF8 != 0 {
                String::from_utf8_lossy(name_bytes).into_owned()
            } else {
                decode_cp437(name_bytes)
            },
            method: rd16(buf, p + 8),
            flags,
            crc: rd32(buf, p + 14),
            csize,
            usize: usize_,
            dos_time: rd16(buf, p + 10),
            dos_date: rd16(buf, p + 12),
            unix_mtime,
            ntfs_times: None,
            made_by: 0,
            ext_attrs: 0,
            local_offset: p as u64,
            chunks: None,
        });
        p = end as usize;
    }
    out
}

pub fn parse_central_directory(buf: &[u8]) -> Result<Vec<CdEntry>> {
    if buf.len() < 22 {
        bail!("file too small to be a zip archive");
    }
    let search_start = buf.len().saturating_sub(22 + 65535);
    let sig = SIG_EOCD.to_le_bytes();
    let eocd = buf[search_start..buf.len() - 18]
        .windows(4)
        .rposition(|w| w == sig)
        .map(|i| search_start + i)
        .context("end of central directory not found (not a zip file?)")?;
    let mut entries = rd16(buf, eocd + 10) as u64;
    let mut cd_size = rd32(buf, eocd + 12) as u64;
    let mut cd_off = rd32(buf, eocd + 16) as u64;
    if (entries == 0xFFFF || cd_size == u32::MAX as u64 || cd_off == u32::MAX as u64)
        && eocd >= 20
        && rd32(buf, eocd - 20) == SIG_EOCD64_LOC
    {
        let e64 = rd64(buf, eocd - 20 + 8);
        if e64.checked_add(56).is_none_or(|end| end > buf.len() as u64) || rd32(buf, e64 as usize) != SIG_EOCD64 {
            bail!("corrupt zip64 end of central directory");
        }
        let e64 = e64 as usize;
        entries = rd64(buf, e64 + 32);
        cd_size = rd64(buf, e64 + 40);
        cd_off = rd64(buf, e64 + 48);
    }
    let Some(end) = cd_off.checked_add(cd_size).filter(|&e| e <= buf.len() as u64) else {
        bail!("central directory extends past end of file");
    };
    let end = end as usize;
    // A damaged count must not reserve more than the directory could possibly hold.
    let mut out = Vec::with_capacity((entries as usize).min(cd_size as usize / 46));
    let mut p = cd_off as usize;
    while p + 46 <= end && rd32(buf, p) == SIG_CENTRAL {
        let made_by = rd16(buf, p + 4);
        let flags = rd16(buf, p + 8);
        let method = rd16(buf, p + 10);
        let dos_time = rd16(buf, p + 12);
        let dos_date = rd16(buf, p + 14);
        let crc = rd32(buf, p + 16);
        let mut csize = rd32(buf, p + 20) as u64;
        let mut usize_ = rd32(buf, p + 24) as u64;
        let nlen = rd16(buf, p + 28) as usize;
        let xlen = rd16(buf, p + 30) as usize;
        let clen = rd16(buf, p + 32) as usize;
        let ext_attrs = rd32(buf, p + 38);
        let mut local_offset = rd32(buf, p + 42) as u64;
        if p + 46 + nlen + xlen + clen > end {
            bail!("corrupt central directory entry at offset {p}");
        }
        let name_bytes = &buf[p + 46..p + 46 + nlen];
        let name = if flags & FLAG_UTF8 != 0 {
            String::from_utf8_lossy(name_bytes).into_owned()
        } else {
            decode_cp437(name_bytes)
        };
        let extra = &buf[p + 46 + nlen..p + 46 + nlen + xlen];
        let mut unix_mtime = None;
        let mut ntfs_times = None;
        let mut chunks = None;
        let mut x = 0;
        while x + 4 <= extra.len() {
            let id = rd16(extra, x);
            let len = rd16(extra, x + 2) as usize;
            let body = &extra[x + 4..(x + 4 + len).min(extra.len())];
            match id {
                EXTRA_ZIP64 => {
                    let mut q = 0;
                    if usize_ == u32::MAX as u64 && q + 8 <= body.len() {
                        usize_ = rd64(body, q);
                        q += 8;
                    }
                    if csize == u32::MAX as u64 && q + 8 <= body.len() {
                        csize = rd64(body, q);
                        q += 8;
                    }
                    if local_offset == u32::MAX as u64 && q + 8 <= body.len() {
                        local_offset = rd64(body, q);
                    }
                }
                EXTRA_EXT_TIME if body.len() >= 5 && body[0] & 1 != 0 => {
                    unix_mtime = Some(rd32(body, 1));
                }
                EXTRA_NTFS => {
                    // Reserved word, then (tag, size, data) attributes; tag 1 holds the times.
                    let mut q = 4;
                    while q + 4 <= body.len() {
                        let (tag, size) = (rd16(body, q), rd16(body, q + 2) as usize);
                        if tag == 1 && size >= 24 && q + 4 + 24 <= body.len() {
                            ntfs_times = Some([rd64(body, q + 4), rd64(body, q + 12), rd64(body, q + 20)]);
                        }
                        q += 4 + size;
                    }
                }
                EXTRA_BLAZE_CHUNKS if body.len() >= 5 && body[0] == 1 => {
                    let chunk = rd32(body, 1);
                    let sizes: Vec<u32> = body[5..].chunks_exact(4).map(|c| u32::from_le_bytes(c.try_into().unwrap())).collect();
                    chunks = Some((chunk, sizes));
                }
                _ => {}
            }
            x += 4 + len;
        }
        out.push(CdEntry {
            name,
            method,
            flags,
            crc,
            csize,
            usize: usize_,
            dos_time,
            dos_date,
            unix_mtime,
            ntfs_times,
            made_by,
            ext_attrs,
            local_offset,
            chunks,
        });
        p += 46 + nlen + xlen + clen;
    }
    Ok(out)
}

const CP437_HI: [char; 128] = [
    'Ç', 'ü', 'é', 'â', 'ä', 'à', 'å', 'ç', 'ê', 'ë', 'è', 'ï', 'î', 'ì', 'Ä', 'Å',
    'É', 'æ', 'Æ', 'ô', 'ö', 'ò', 'û', 'ù', 'ÿ', 'Ö', 'Ü', '¢', '£', '¥', '₧', 'ƒ',
    'á', 'í', 'ó', 'ú', 'ñ', 'Ñ', 'ª', 'º', '¿', '⌐', '¬', '½', '¼', '¡', '«', '»',
    '░', '▒', '▓', '│', '┤', '╡', '╢', '╖', '╕', '╣', '║', '╗', '╝', '╜', '╛', '┐',
    '└', '┴', '┬', '├', '─', '┼', '╞', '╟', '╚', '╔', '╩', '╦', '╠', '═', '╬', '╧',
    '╨', '╤', '╥', '╙', '╘', '╒', '╓', '╫', '╪', '┘', '┌', '█', '▄', '▌', '▐', '▀',
    'α', 'ß', 'Γ', 'π', 'Σ', 'σ', 'µ', 'τ', 'Φ', 'Θ', 'Ω', 'δ', '∞', 'φ', 'ε', '∩',
    '≡', '±', '≥', '≤', '⌠', '⌡', '÷', '≈', '°', '∙', '·', '√', 'ⁿ', '²', '■', '\u{a0}',
];

fn decode_cp437(b: &[u8]) -> String {
    if b.is_ascii() {
        return String::from_utf8_lossy(b).into_owned();
    }
    b.iter().map(|&c| if c < 128 { c as char } else { CP437_HI[(c - 128) as usize] }).collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn dos_roundtrip() {
        let t = UNIX_EPOCH + std::time::Duration::from_secs(1_700_000_000);
        let (tm, dt, _) = dos_datetime(t);
        assert_eq!(dos_to_system_time(tm, dt), t);
    }

    #[test]
    fn out_of_range_file_times_are_clamped() {
        let max = system_time_from_filetime(MAX_FILETIME);
        assert_eq!(system_time_from_filetime(u64::MAX), max);
        assert_eq!(filetime_from_system_time(max), MAX_FILETIME);
    }

    #[test]
    fn cp437_high_half() {
        assert_eq!(decode_cp437(&[0x80, b'a', 0x9b, 0xff]), "Ça¢\u{a0}");
        assert_eq!(decode_cp437(&[0xe1]), "ß");
    }
}
