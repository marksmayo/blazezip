//! Build the vendored libdeflate for the benchmarks (same sources and flags as the main crate).

fn main() {
    let dir = "../../third_party/libdeflate";
    let mut b = cc::Build::new();
    for f in [
        "lib/x86/cpu_features.c",
        "lib/arm/cpu_features.c",
        "lib/utils.c",
        "lib/crc32.c",
        "lib/deflate_compress.c",
        "lib/deflate_decompress.c",
    ] {
        b.file(format!("{dir}/{f}"));
    }
    b.include(dir).warnings(false).opt_level(3).define("NDEBUG", None).compile("deflate");
    println!("cargo:rerun-if-changed=build.rs");
    println!("cargo:rerun-if-changed={dir}/lib");
    println!("cargo:rerun-if-changed={dir}/libdeflate.h");
}
