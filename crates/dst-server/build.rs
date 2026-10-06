mod build_metadata;

fn main() {
    for name in ["SOURCE_REVISION", "SOURCE_DIRTY"] {
        println!("cargo:rerun-if-env-changed={name}");
    }
    let (revision, dirty) = build_metadata::resolve(
        &std::env::var("SOURCE_REVISION").unwrap_or_default(),
        &std::env::var("SOURCE_DIRTY").unwrap_or_else(|_| "unknown".into()),
    )
    .expect("invalid build identity");
    println!("cargo:rustc-env=DST_BUILD_REVISION={revision}");
    println!("cargo:rustc-env=DST_BUILD_DIRTY={dirty}");
    pkg_config::Config::new()
        .atleast_version("3.7.0")
        .probe("libarchive")
        .expect("libarchive development files are required");
    println!("cargo:rerun-if-changed=schema/room.capnp");
    println!("cargo:rerun-if-changed=../../resources/lua");
    capnpc::CompilerCommand::new()
        .src_prefix("schema")
        .file("schema/room.capnp")
        .run()
        .expect("compile the local RPC schema with capnp");
}
