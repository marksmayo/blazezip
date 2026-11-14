//! Benchmarks for blazezip's portable core: the compression back-ends (libdeflate and zstd),
//! CRC-32, and ZIP header encoding/parsing.
//!
//! blazezip is a Windows-only binary crate, but these modules have no Windows dependencies, so
//! the `blazezip-bench` crate compiles them straight from `src/` and they can run anywhere
//! (including CodSpeed's Linux CI runners).
//!
//! Run locally with `cargo bench -p blazezip-bench`.

use blazezip_bench::codec::{self, Crc};
use blazezip_bench::zipfmt;
use divan::{black_box, Bencher};
use std::time::{Duration, UNIX_EPOCH};
use zipfmt::{EntryMeta, METHOD_DEFLATE};

fn main() {
    divan::main();
}

const SIZE: usize = 256 << 10;

/// Deterministic text-like data (compressible, roughly like source code or logs).
fn text_data(len: usize) -> Vec<u8> {
    const WORDS: &[&str] = &[
        "the", "archive", "entry", "deflate", "zstd", "header", "central", "directory", "chunk", "thread", "fn",
        "let", "mut", "return", "struct", "impl", "0x7a42", "{", "}", "(", ");", "\n", "    ", "// ", "compress",
        "offset", "size", "crc32", "buffer", "file",
    ];
    let mut out = Vec::with_capacity(len + 16);
    let mut x: u32 = 0x1234_5678;
    while out.len() < len {
        x ^= x << 13;
        x ^= x >> 17;
        x ^= x << 5;
        out.extend_from_slice(WORDS[(x % WORDS.len() as u32) as usize].as_bytes());
        out.push(b' ');
    }
    out.truncate(len);
    out
}

/// Deterministic pseudo-random bytes (incompressible).
fn random_data(len: usize) -> Vec<u8> {
    let mut x: u64 = 0x9E37_79B9_7F4A_7C15;
    (0..len)
        .map(|_| {
            x ^= x << 13;
            x ^= x >> 7;
            x ^= x << 17;
            (x >> 32) as u8
        })
        .collect()
}

// ---------------------------------------------------------------------------------------------
// CRC-32
// ---------------------------------------------------------------------------------------------

mod crc {
    use super::*;

    #[divan::bench]
    fn libdeflate_crc32(bencher: Bencher) {
        let data = text_data(SIZE);
        bencher.bench_local(|| Crc::of(black_box(&data)));
    }

    #[divan::bench]
    fn combine(bencher: Bencher) {
        let data = text_data(SIZE);
        let parts: Vec<Crc> = data.chunks(SIZE / 16).map(Crc::of).collect();
        bencher.bench_local(|| black_box(&parts).iter().fold(Crc::default(), |acc, &c| acc.combine(c)));
    }
}

// ---------------------------------------------------------------------------------------------
// Deflate (libdeflate)
// ---------------------------------------------------------------------------------------------

mod deflate {
    use super::*;

    #[divan::bench(args = [1, 6, 12])]
    fn compress_text(bencher: Bencher, level: i32) {
        let data = text_data(SIZE);
        let mut out = Vec::new();
        codec::deflate_append(&data, level, true, &mut out); // warm the per-thread compressor
        bencher.bench_local(|| {
            out.clear();
            codec::deflate_append(black_box(&data), level, true, &mut out)
        });
    }

    #[divan::bench]
    fn compress_random(bencher: Bencher) {
        let data = random_data(SIZE);
        let mut out = Vec::new();
        codec::deflate_append(&data, 6, true, &mut out);
        bencher.bench_local(|| {
            out.clear();
            codec::deflate_append(black_box(&data), 6, true, &mut out)
        });
    }

    /// Non-final chunk ending in a sync flush, as used for large files split across threads.
    #[divan::bench]
    fn compress_sync_chunk(bencher: Bencher) {
        let data = text_data(SIZE);
        let mut out = Vec::new();
        codec::deflate_append(&data, 6, false, &mut out);
        bencher.bench_local(|| {
            out.clear();
            codec::deflate_append(black_box(&data), 6, false, &mut out)
        });
    }

    #[divan::bench]
    fn decompress(bencher: Bencher) {
        let data = text_data(SIZE);
        let mut comp = Vec::new();
        codec::deflate_append(&data, 6, true, &mut comp);
        let mut out = vec![0u8; data.len()];
        codec::inflate_into(&comp, &mut out).unwrap();
        bencher.bench_local(|| codec::inflate_into(black_box(&comp), &mut out).unwrap());
    }

    #[divan::bench]
    fn decompress_sync_chunk(bencher: Bencher) {
        let data = text_data(SIZE);
        let mut comp = Vec::new();
        codec::deflate_append(&data, 6, false, &mut comp);
        let mut out = vec![0u8; data.len()];
        codec::inflate_chunk_into(&comp, &mut out, false).unwrap();
        bencher.bench_local(|| codec::inflate_chunk_into(black_box(&comp), &mut out, false).unwrap());
    }

    /// Incompressibility probe run on every file before choosing store vs compress.
    #[divan::bench(args = [false, true])]
    fn quick_compressed_size(bencher: Bencher, zstd: bool) {
        let data = text_data(64 << 10);
        codec::quick_compressed_size(&data, zstd);
        bencher.bench_local(|| codec::quick_compressed_size(black_box(&data), zstd));
    }
}

// ---------------------------------------------------------------------------------------------
// Zstandard
// ---------------------------------------------------------------------------------------------

mod zstd {
    use super::*;

    #[divan::bench(args = [1, 3, 9])]
    fn compress_text(bencher: Bencher, level: i32) {
        let data = text_data(SIZE);
        let mut out = Vec::new();
        codec::zstd_append(&data, level, &mut out).unwrap();
        bencher.bench_local(|| {
            out.clear();
            codec::zstd_append(black_box(&data), level, &mut out).unwrap()
        });
    }

    #[divan::bench]
    fn decompress(bencher: Bencher) {
        let data = text_data(SIZE);
        let mut comp = Vec::new();
        codec::zstd_append(&data, 3, &mut comp).unwrap();
        let mut out = vec![0u8; data.len()];
        codec::zstd_decompress_into(&comp, &mut out).unwrap();
        bencher.bench_local(|| codec::zstd_decompress_into(black_box(&comp), &mut out).unwrap());
    }
}

// ---------------------------------------------------------------------------------------------
// ZIP format: headers, central directory
// ---------------------------------------------------------------------------------------------

mod zip_format {
    use super::*;

    const ENTRIES: usize = 1000;

    fn names() -> Vec<String> {
        (0..ENTRIES).map(|i| format!("project/src/module_{:02}/file_{i:04}.rs", i % 37)).collect()
    }

    fn meta(name: &str, i: usize, local_offset: u64) -> EntryMeta<'_> {
        let ft = zipfmt::filetime_from_system_time(UNIX_EPOCH + Duration::from_secs(1_700_000_000 + i as u64));
        EntryMeta {
            name,
            is_dir: false,
            method: METHOD_DEFLATE,
            crc: 0xDEAD_BEEF ^ i as u32,
            csize: 0,
            usize: 1000 + i as u64,
            dos_time: 0x6000,
            dos_date: 0x5761,
            times: [ft, ft, ft],
            attrs: zipfmt::ATTR_ARCHIVE,
            ext: None,
            local_offset,
            chunks: None,
        }
    }

    /// A complete archive with `ENTRIES` empty-data entries: local headers, central directory
    /// and end-of-central-directory record.
    fn build_archive(names: &[String]) -> Vec<u8> {
        let mut buf = Vec::new();
        let mut offsets = Vec::with_capacity(names.len());
        for (i, n) in names.iter().enumerate() {
            offsets.push(buf.len() as u64);
            meta(n, i, 0).local_header(&mut buf);
        }
        let cd_offset = buf.len() as u64;
        for (i, n) in names.iter().enumerate() {
            meta(n, i, offsets[i]).central_header(&mut buf);
        }
        let cd_size = buf.len() as u64 - cd_offset;
        zipfmt::end_of_central_directory(&mut buf, names.len() as u64, cd_offset, cd_size);
        buf
    }

    #[divan::bench]
    fn write_central_directory(bencher: Bencher) {
        let names = names();
        let metas: Vec<EntryMeta> = names.iter().enumerate().map(|(i, n)| meta(n, i, i as u64 * 100)).collect();
        let mut out = Vec::new();
        bencher.bench_local(|| {
            out.clear();
            for m in black_box(&metas) {
                m.central_header(&mut out);
            }
            let cd_size = out.len() as u64;
            zipfmt::end_of_central_directory(&mut out, metas.len() as u64, 0, cd_size);
            out.len()
        });
    }

    #[divan::bench]
    fn parse_central_directory(bencher: Bencher) {
        let archive = build_archive(&names());
        assert_eq!(zipfmt::parse_central_directory(&archive).unwrap().len(), ENTRIES);
        bencher.bench_local(|| zipfmt::parse_central_directory(black_box(&archive)).unwrap());
    }

    #[divan::bench]
    fn scan_local_headers(bencher: Bencher) {
        let archive = build_archive(&names());
        assert_eq!(zipfmt::scan_local_headers(&archive).len(), ENTRIES);
        bencher.bench_local(|| zipfmt::scan_local_headers(black_box(&archive)));
    }

    #[divan::bench]
    fn dos_datetime_roundtrip(bencher: Bencher) {
        let times: Vec<_> = (0..ENTRIES as u64).map(|i| UNIX_EPOCH + Duration::from_secs(1_000_000_000 + i * 86_399)).collect();
        bencher.bench_local(|| {
            black_box(&times)
                .iter()
                .map(|&t| {
                    let (tm, dt, _) = zipfmt::dos_datetime(t);
                    zipfmt::dos_to_system_time(tm, dt)
                })
                .fold(0u64, |acc, t| acc ^ t.duration_since(UNIX_EPOCH).unwrap().as_secs())
        });
    }
}
