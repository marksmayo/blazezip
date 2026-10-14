# Third-party notices

blazezip is MIT-licensed (see `LICENSE`). Its release binary includes the following components.

| Component | Use | Licence |
|---|---|---|
| [libdeflate](https://github.com/ebiggers/libdeflate) (vendored in `third_party/libdeflate`, with a small sync-flush addition) | Deflate compression and decompression, CRC-32 | MIT — copyright 2016 Eric Biggers, 2024 Google LLC; full text in `third_party/libdeflate/COPYING` |
| [Zstandard](https://github.com/facebook/zstd) via the [`zstd`](https://crates.io/crates/zstd), `zstd-safe` and `zstd-sys` crates | Zstandard (ZIP method 93) | BSD-3-Clause |
| [`crc32fast`](https://crates.io/crates/crc32fast) | combining CRC-32s of parallel chunks | MIT or Apache-2.0 |
| [`memmap2`](https://crates.io/crates/memmap2) | memory-mapped archive and output files | MIT or Apache-2.0 |
| [`anyhow`](https://crates.io/crates/anyhow) | error handling | MIT or Apache-2.0 |

The benchmark harness (`bench/`, not part of the release binary) also uses
[`windows-sys`](https://crates.io/crates/windows-sys) (MIT or Apache-2.0) and, when run, downloads
the public benchmark corpora and third-party archivers listed in the README. Those tools and
corpora are not redistributed in this repository.
