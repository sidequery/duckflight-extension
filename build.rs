use std::{env, fs, path::PathBuf};

fn main() {
    println!("cargo:rustc-check-cfg=cfg(duckflight_bundled_core)");
    println!("cargo:rerun-if-env-changed=DUCKFLIGHT_CORE_BUNDLE_PATH");

    let Some(source) = env::var_os("DUCKFLIGHT_CORE_BUNDLE_PATH") else {
        return;
    };
    let source = PathBuf::from(source);
    if !source.is_file() {
        panic!("DuckFlight core bundle is missing: {}", source.display());
    }
    let output = PathBuf::from(env::var_os("OUT_DIR").expect("Cargo must provide OUT_DIR"))
        .join("duckflight_core.bundle");
    fs::copy(&source, &output).unwrap_or_else(|error| {
        panic!(
            "copy DuckFlight core bundle {} to {}: {error}",
            source.display(),
            output.display()
        )
    });
    println!("cargo:rerun-if-changed={}", source.display());
    println!("cargo:rustc-cfg=duckflight_bundled_core");
}
