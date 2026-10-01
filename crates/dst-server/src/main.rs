fn main() -> std::process::ExitCode {
    std::process::ExitCode::from(dst_server::cli::main(std::env::args_os()))
}
