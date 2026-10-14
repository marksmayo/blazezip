//! Build the vendored libdeflate (with blazezip's sync-flush extension).
//!
//! On MSVC targets libdeflate is compiled with clang-cl when it is available: MSVC has no
//! per-function target attribute, so it cannot build libdeflate's BMI2 decompressor, and clang
//! generally produces faster code for it. Set CC to override, or BLAZEZIP_NO_CLANG=1 to force cl.

use std::path::PathBuf;

fn clang_cl() -> Option<PathBuf> {
    if std::env::var_os("BLAZEZIP_NO_CLANG").is_some() {
        return None;
    }
    let candidates = std::env::var_os("PATH")
        .into_iter()
        .flat_map(|p| std::env::split_paths(&p).collect::<Vec<_>>())
        .chain([PathBuf::from(r"C:\Program Files\LLVM\bin")]);
    candidates.map(|d| d.join("clang-cl.exe")).find(|p| p.is_file())
}

fn main() {
    let dir = "third_party/libdeflate";
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
    let msvc = std::env::var("CARGO_CFG_TARGET_ENV").as_deref() == Ok("msvc");
    if msvc && std::env::var_os("CC").is_none() {
        if let Some(clang) = clang_cl() {
            b.compiler(clang);
        }
    }
    b.include(dir).warnings(false).opt_level(3).define("NDEBUG", None).compile("deflate");
    println!("cargo:rerun-if-changed=build.rs");
    println!("cargo:rerun-if-changed={dir}/lib");
    println!("cargo:rerun-if-changed={dir}/libdeflate.h");
    println!("cargo:rerun-if-env-changed=CC");
    println!("cargo:rerun-if-env-changed=BLAZEZIP_NO_CLANG");
}
