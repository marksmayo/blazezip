//! blazezip's platform-independent core, compiled straight from the main crate's sources so it
//! can be benchmarked on any OS (blazezip itself is a Windows-only binary).
//!
//! Run the benchmarks with `cargo bench -p blazezip-bench`.

#![allow(dead_code)]

#[path = "../../../src/codec.rs"]
pub mod codec;
#[path = "../../../src/libdeflate.rs"]
pub mod libdeflate;
#[path = "../../../src/zipfmt.rs"]
pub mod zipfmt;
