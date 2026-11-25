use std::{env, fs, path::PathBuf};

fn main() {
    println!("cargo:rustc-check-cfg=cfg(duckflight_bundled_core)");
    println!("cargo:rustc-check-cfg=cfg(duckflight_linked_acl)");
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
    if source.to_string_lossy().ends_with(".tar.gz") {
        let dir = output.parent().unwrap();
        let libraries = [
            "libduckdb_static.a",
            "libduckdb_loadable_extensions.a",
            "libcore_functions_extension.a",
        ];
        // Only extract known members from the checksum-pinned platform bundle.
        let status = std::process::Command::new("tar")
            .arg("-xzf")
            .arg(&source)
            .arg("-C")
            .arg(dir)
            .args(["core.bundle", "duckflight_acl.o"])
            .args(libraries)
            .status()
            .expect("extract DuckFlight build inputs");
        assert!(status.success(), "invalid DuckFlight build bundle");
        fs::rename(dir.join("core.bundle"), &output).expect("stage embedded core");
        println!("cargo:rustc-cfg=duckflight_linked_acl");
        let linux = env::var("CARGO_CFG_TARGET_OS").unwrap() == "linux";
        println!(
            "cargo:rustc-link-arg={}",
            dir.join("duckflight_acl.o").display()
        );
        if linux {
            println!("cargo:rustc-link-arg=-Wl,--start-group");
        }
        for library in libraries {
            println!("cargo:rustc-link-arg={}", dir.join(library).display());
        }
        if linux {
            for arg in [
                "-Wl,--end-group",
                "-lstdc++",
                "-lgcc",
                "-lm",
                "-lc",
                "-lc_nonshared",
                "-Wl,-z,defs",
            ] {
                println!("cargo:rustc-link-arg={arg}");
            }
        } else {
            println!("cargo:rustc-link-arg=-lc++");
        }
    } else {
        fs::copy(&source, &output).unwrap_or_else(|error| {
            panic!(
                "copy DuckFlight core bundle {} to {}: {error}",
                source.display(),
                output.display()
            )
        });
    }
    println!("cargo:rerun-if-changed={}", source.display());
    println!("cargo:rustc-cfg=duckflight_bundled_core");
}
