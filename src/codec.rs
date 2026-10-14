//! Compression back-ends, with per-thread reusable contexts.
//!
//! - Deflate: libdeflate (the fastest deflate implementation available), vendored with a small
//!   extension so chunks of a large file can end in a sync flush instead of a final block. The
//!   chunks then concatenate into one valid deflate stream, and each can still be decoded alone.
//! - Zstandard (method 93): each chunk is its own frame; concatenated frames are a valid stream.

use crate::libdeflate::{Compressor, Decompressor};
use anyhow::{bail, Result};
use std::cell::RefCell;

thread_local! {
    static LIBDEFLATE: RefCell<Option<(i32, Compressor)>> = const { RefCell::new(None) };
    /// Separate level-1 compressor for incompressibility sampling, so sampling never forces the
    /// main compressor to be reallocated at a different level.
    static LIBDEFLATE_SAMPLE: RefCell<Option<Compressor>> = const { RefCell::new(None) };
    static LIBINFLATE: RefCell<Option<Decompressor>> = const { RefCell::new(None) };
    static ZSTD_C: RefCell<Option<(i32, zstd::bulk::Compressor<'static>)>> = const { RefCell::new(None) };
    static ZSTD_SAMPLE: RefCell<Option<zstd::bulk::Compressor<'static>>> = const { RefCell::new(None) };
    static ZSTD_D: RefCell<Option<zstd::bulk::Decompressor<'static>>> = const { RefCell::new(None) };
    /// Scratch for zstd output and for completing sync-flushed chunks before inflating them.
    static SCRATCH: RefCell<Vec<u8>> = const { RefCell::new(Vec::new()) };
}

/// A CRC-32 together with the number of bytes it covers, so CRCs of consecutive pieces computed
/// on different threads can be combined.
#[derive(Clone, Copy, Debug, Default)]
pub struct Crc {
    pub crc: u32,
    pub len: u64,
}

impl Crc {
    pub fn of(data: &[u8]) -> Self {
        Crc { crc: crate::libdeflate::crc32(0, data), len: data.len() as u64 }
    }

    pub fn update(&mut self, data: &[u8]) {
        self.crc = crate::libdeflate::crc32(self.crc, data);
        self.len += data.len() as u64;
    }

    /// CRC of `self`'s bytes followed by `next`'s bytes.
    pub fn combine(self, next: Crc) -> Crc {
        if self.len == 0 {
            return next;
        }
        let mut h = crc32fast::Hasher::new_with_initial_len(self.crc, self.len);
        h.combine(&crc32fast::Hasher::new_with_initial_len(next.crc, next.len));
        Crc { crc: h.finalize(), len: self.len + next.len }
    }
}

/// Append raw deflate of `raw` to `out`; returns the bytes appended. Non-final chunks end with
/// a sync flush (byte-aligned, empty stored block, no back-references into the next chunk); a
/// whole file or the final chunk sets BFINAL.
pub fn deflate_append(raw: &[u8], level: i32, last: bool, out: &mut Vec<u8>) -> usize {
    LIBDEFLATE.with(|c| {
        let mut c = c.borrow_mut();
        if c.as_ref().map(|(l, _)| *l) != Some(level) {
            *c = Some((level, Compressor::new(level)));
        }
        // Non-final chunks are always >= 64 KiB (see create::plan), well above libdeflate's
        // tiny-input passthrough, so compress_sync never refuses them.
        c.as_mut().unwrap().1.compress_append(raw, out, !last).expect("deflate bound too small")
    })
}

/// Size of `raw` compressed at the fastest setting, used to detect incompressible data cheaply.
pub fn quick_compressed_size(raw: &[u8], zstd: bool) -> usize {
    SCRATCH.with(|s| {
        let mut s = s.borrow_mut();
        s.clear();
        if zstd {
            ZSTD_SAMPLE.with(|c| {
                let mut c = c.borrow_mut();
                if c.is_none() {
                    *c = zstd::bulk::Compressor::new(1).ok();
                }
                s.reserve(zstd::zstd_safe::compress_bound(raw.len()));
                match c.as_mut() {
                    Some(c) => c.compress_to_buffer(raw, &mut *s).unwrap_or(raw.len()),
                    None => raw.len(),
                }
            })
        } else {
            LIBDEFLATE_SAMPLE.with(|c| {
                let mut c = c.borrow_mut();
                c.get_or_insert_with(|| Compressor::new(1)).compress_append(raw, &mut s, false).unwrap_or(raw.len())
            })
        }
    })
}

/// Append a zstd frame of `raw` to `out`; returns the bytes appended.
pub fn zstd_append(raw: &[u8], level: i32, out: &mut Vec<u8>) -> Result<usize> {
    ZSTD_C.with(|c| {
        let mut c = c.borrow_mut();
        if c.as_ref().map(|(l, _)| *l) != Some(level) {
            *c = Some((level, zstd::bulk::Compressor::new(level)?));
        }
        SCRATCH.with(|s| {
            let mut s = s.borrow_mut();
            s.clear();
            s.reserve(zstd::zstd_safe::compress_bound(raw.len()));
            let n = c.as_mut().unwrap().1.compress_to_buffer(raw, &mut *s)?;
            out.extend_from_slice(&s[..n]);
            Ok(n)
        })
    })
}

/// Decompress a complete deflate stream (or a full-flushed chunk, see below) into `out`, which
/// must be exactly the uncompressed size.
pub fn inflate_into(comp: &[u8], out: &mut [u8]) -> Result<()> {
    LIBINFLATE.with(|d| {
        let mut d = d.borrow_mut();
        match d.get_or_insert_with(Decompressor::new).decompress(comp, out) {
            Ok(()) => Ok(()),
            Err(e) => bail!("{e}"),
        }
    })
}

/// A non-final chunk ends byte-aligned after an empty stored block. Appending an empty final
/// fixed-Huffman block (bits 1,01 then end-of-block = `03 00`) turns it into a complete stream
/// libdeflate can decode on its own, straight into its slice of the output.
pub fn inflate_chunk_into(comp: &[u8], out: &mut [u8], last: bool) -> Result<()> {
    if last {
        return inflate_into(comp, out);
    }
    SCRATCH.with(|s| {
        let mut buf = s.borrow_mut();
        buf.clear();
        buf.extend_from_slice(comp);
        buf.extend_from_slice(&[0x03, 0x00]);
        inflate_into(&buf, out)
    })
}

pub fn zstd_decompress_into(comp: &[u8], out: &mut [u8]) -> Result<()> {
    ZSTD_D.with(|d| {
        let mut d = d.borrow_mut();
        if d.is_none() {
            *d = Some(zstd::bulk::Decompressor::new()?);
        }
        let n = d.as_mut().unwrap().decompress_to_buffer(comp, out)?;
        if n != out.len() {
            bail!("size mismatch: expected {} got {}", out.len(), n);
        }
        Ok(())
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn crc_matches_reference_and_combines() {
        let data: Vec<u8> = (0..100_000u32).map(|i| (i * 7 + i / 13) as u8).collect();
        let whole = Crc::of(&data);
        assert_eq!(whole.crc, crc32fast::hash(&data));
        let (a, b) = data.split_at(31_337);
        let joined = Crc::of(a).combine(Crc::of(b));
        assert_eq!(joined.crc, whole.crc);
        assert_eq!(joined.len, data.len() as u64);
        assert_eq!(Crc::default().combine(whole).crc, whole.crc);
    }

    #[test]
    fn deflate_appends_after_existing_bytes() {
        let raw = b"hello hello hello hello hello hello".repeat(1000);
        let mut out = b"prefix".to_vec();
        let n = deflate_append(&raw, 6, true, &mut out);
        assert_eq!(out.len(), 6 + n);
        assert_eq!(&out[..6], b"prefix");
        let mut back = vec![0u8; raw.len()];
        inflate_into(&out[6..], &mut back).unwrap();
        assert_eq!(back, raw);
    }
}

#[cfg(test)]
mod bench {
    #[test]
    #[ignore]
    fn crc_throughput() {
        let data: Vec<u8> = (0..64u32 << 20).map(|i| (i.wrapping_mul(2654435761) >> 13) as u8).collect();
        for _ in 0..2 {
            let t = std::time::Instant::now();
            let a = super::Crc::of(&data).crc;
            let t1 = t.elapsed();
            let t = std::time::Instant::now();
            let b = crc32fast::hash(&data);
            let t2 = t.elapsed();
            assert_eq!(a, b);
            eprintln!("libdeflate {:.1} GB/s, crc32fast {:.1} GB/s", 64.0 / 1024.0 / t1.as_secs_f64(), 64.0 / 1024.0 / t2.as_secs_f64());
        }
    }
}
