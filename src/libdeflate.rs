//! Minimal safe wrapper over the vendored libdeflate (see third_party/libdeflate and build.rs).

use std::ffi::c_void;

#[repr(C)]
struct RawCompressor {
    _p: [u8; 0],
}
#[repr(C)]
struct RawDecompressor {
    _p: [u8; 0],
}

extern "C" {
    fn libdeflate_alloc_compressor(level: i32) -> *mut RawCompressor;
    fn libdeflate_deflate_compress(c: *mut RawCompressor, inp: *const c_void, n: usize, out: *mut c_void, avail: usize) -> usize;
    fn libdeflate_deflate_compress_sync(c: *mut RawCompressor, inp: *const c_void, n: usize, out: *mut c_void, avail: usize) -> usize;
    fn libdeflate_deflate_compress_bound(c: *mut RawCompressor, n: usize) -> usize;
    fn libdeflate_free_compressor(c: *mut RawCompressor);
    fn libdeflate_alloc_decompressor() -> *mut RawDecompressor;
    fn libdeflate_deflate_decompress(
        d: *mut RawDecompressor,
        inp: *const c_void,
        n: usize,
        out: *mut c_void,
        avail: usize,
        actual: *mut usize,
    ) -> i32;
    fn libdeflate_free_decompressor(d: *mut RawDecompressor);
    fn libdeflate_crc32(crc: u32, buf: *const c_void, len: usize) -> u32;
}

/// Update a running CRC-32 (start from 0). libdeflate picks the fastest kernel for the CPU
/// (VPCLMULQDQ/AVX2 on recent x86), roughly twice crc32fast's PCLMUL path.
pub fn crc32(crc: u32, data: &[u8]) -> u32 {
    unsafe { libdeflate_crc32(crc, data.as_ptr().cast(), data.len()) }
}

pub struct Compressor(*mut RawCompressor);

impl Compressor {
    pub fn new(level: i32) -> Self {
        let p = unsafe { libdeflate_alloc_compressor(level.clamp(0, 12)) };
        assert!(!p.is_null(), "libdeflate: out of memory");
        Compressor(p)
    }

    /// Worst-case output size, plus room for the sync-flush trailer.
    pub fn bound(&mut self, n: usize) -> usize {
        unsafe { libdeflate_deflate_compress_bound(self.0, n) + 16 }
    }

    /// Append the raw deflate stream for `input` to `out`, writing straight into its spare
    /// capacity (no zeroing). `sync` ends with an empty stored block instead of a final block,
    /// so outputs concatenate. Returns the number of bytes appended.
    pub fn compress_append(&mut self, input: &[u8], out: &mut Vec<u8>, sync: bool) -> Option<usize> {
        let bound = self.bound(input.len());
        out.reserve(bound);
        let dst = out.spare_capacity_mut().as_mut_ptr().cast::<c_void>();
        let f = if sync { libdeflate_deflate_compress_sync } else { libdeflate_deflate_compress };
        let n = unsafe { f(self.0, input.as_ptr().cast(), input.len(), dst, bound) };
        if n == 0 {
            return None;
        }
        // SAFETY: libdeflate initialised the first n bytes of the spare capacity.
        unsafe { out.set_len(out.len() + n) };
        Some(n)
    }
}

impl Drop for Compressor {
    fn drop(&mut self) {
        unsafe { libdeflate_free_compressor(self.0) }
    }
}

pub struct Decompressor(*mut RawDecompressor);

impl Decompressor {
    pub fn new() -> Self {
        let p = unsafe { libdeflate_alloc_decompressor() };
        assert!(!p.is_null(), "libdeflate: out of memory");
        Decompressor(p)
    }

    /// Decode a complete raw deflate stream into exactly `out.len()` bytes.
    pub fn decompress(&mut self, input: &[u8], out: &mut [u8]) -> Result<(), &'static str> {
        let mut actual = 0usize;
        let r = unsafe {
            libdeflate_deflate_decompress(self.0, input.as_ptr().cast(), input.len(), out.as_mut_ptr().cast(), out.len(), &mut actual)
        };
        match r {
            0 if actual == out.len() => Ok(()),
            0 => Err("size mismatch"),
            1 => Err("corrupt deflate data"),
            2 => Err("size mismatch"),
            _ => Err("output larger than expected"),
        }
    }
}

impl Drop for Decompressor {
    fn drop(&mut self) {
        unsafe { libdeflate_free_decompressor(self.0) }
    }
}
