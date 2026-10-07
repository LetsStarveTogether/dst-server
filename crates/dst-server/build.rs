fn main() {
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
