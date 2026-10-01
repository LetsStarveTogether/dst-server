fn main() {
    println!("cargo:rerun-if-changed=schema/room.capnp");
    println!("cargo:rerun-if-changed=../../resources/lua");
    capnpc::CompilerCommand::new()
        .src_prefix("schema")
        .file("schema/room.capnp")
        .run()
        .expect("compile the local RPC schema with capnp");
}
