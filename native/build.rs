//! Exposes build identity to the Python-visible `native_info()` diagnostics.
//!
//! Which linker/toolchain built the extension matters on Windows (MSVC vs
//! GNU), and the stark difference between a stray debug build and the shipped
//! release build must be visible from inside the running app.

fn main() {
    if let Ok(target) = std::env::var("TARGET") {
        println!("cargo:rustc-env=JARVIS_NATIVE_TARGET={target}");
    }
    if let Ok(profile) = std::env::var("PROFILE") {
        println!("cargo:rustc-env=JARVIS_NATIVE_PROFILE={profile}");
    }
    if let Ok(opt) = std::env::var("OPT_LEVEL") {
        println!("cargo:rustc-env=JARVIS_NATIVE_OPT_LEVEL={opt}");
    }
}
