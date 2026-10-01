use crate as dst_server;
use std::{
    ffi::OsString,
    io::{BufRead, Read, Write},
    path::{Path, PathBuf},
};

use anyhow::{Context, Result, ensure};
use clap::{CommandFactory, Parser, Subcommand, ValueEnum};
use dst_server::{model, rpc, settings};
use futures::{StreamExt, stream};
use serde_json::{Value, json};

#[path = "archive_cli.rs"]
mod archive_cli;
#[path = "host_cli.rs"]
mod host_cli;
#[path = "logs_cli.rs"]
mod logs_cli;

#[derive(Parser)]
#[command(version, about = "Manage Don't Starve Together rooms")]
struct Arguments {
    /// Print compact JSON, including errors.
    #[arg(long, global = true)]
    json: bool,
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Supervise every shard of one room and serve its local SDK socket.
    Agent {
        #[arg(long, default_value = "/cluster")]
        cluster: PathBuf,
        #[arg(
            long,
            default_value = "/install/bin64/dontstarve_dedicated_server_nullrenderer_x64"
        )]
        executable: PathBuf,
        #[arg(long)]
        socket: Option<PathBuf>,
        #[arg(long, default_value = "history")]
        profile: String,
        #[arg(long)]
        offline: bool,
    },
    Host(host_cli::Arguments),
    Archive(archive_cli::Arguments),
    Logs(logs_cli::Arguments),
    /// Call any room or player operation; describe lists methods and arguments.
    Call {
        method: String,
        /// JSON object, @filename, or - for stdin.
        #[arg(default_value = "{}")]
        arguments: String,
        #[arg(long, default_value = "/cluster/.dst-agent.sock")]
        socket: Vec<PathBuf>,
        #[arg(long)]
        shard: Option<String>,
        #[arg(long)]
        timeout: Option<f64>,
    },
    /// Describe the shared Rust, Python and RPC operation schema.
    Describe {
        #[arg(long)]
        shard: bool,
    },
    /// Follow bounded event batches; each batch includes its discarded count.
    Subscribe {
        #[arg(value_enum)]
        kind: StreamKind,
        #[arg(long, default_value = "/cluster/.dst-agent.sock")]
        socket: PathBuf,
    },
    /// Run trusted Lua on a shard, or read one command per line interactively.
    Console {
        #[arg(long, default_value = "/cluster/.dst-agent.sock")]
        socket: PathBuf,
        #[arg(long)]
        shard: String,
        #[arg(long, conflicts_with = "file")]
        source: Option<String>,
        #[arg(long)]
        file: Option<PathBuf>,
    },
    #[command(subcommand)]
    Scripts(Scripts),
    #[command(subcommand)]
    Annotations(Annotations),
    #[command(subcommand)]
    Config(Config),
    #[command(subcommand)]
    Template(Template),
    Completion {
        shell: clap_complete::Shell,
    },
    /// Validate native INI topology without starting or modifying a room.
    Inspect {
        directory: PathBuf,
    },
    /// Exercise a disposable room inside the P0 test container.
    Probe {
        #[arg(long)]
        cluster: PathBuf,
        #[arg(
            long,
            default_value = "/install/bin64/dontstarve_dedicated_server_nullrenderer_x64"
        )]
        executable: PathBuf,
        #[arg(long, default_value_t = 300)]
        timeout: u64,
        #[arg(long, default_value_t = 2)]
        rounds: usize,
    },
    /// Enumerate or apply fixed native snapshots before loading a disposable world.
    ProbeRecovery {
        #[arg(long)]
        cluster: PathBuf,
        #[arg(
            long,
            default_value = "/install/bin64/dontstarve_dedicated_server_nullrenderer_x64"
        )]
        executable: PathBuf,
        #[arg(long, default_value_t = 120)]
        timeout: u64,
        #[arg(long)]
        targets: Option<PathBuf>,
    },
    /// Save or inspect a synthetic native player in a disposable test room.
    ProbePlayer {
        #[arg(long)]
        cluster: PathBuf,
        #[arg(
            long,
            default_value = "/install/bin64/dontstarve_dedicated_server_nullrenderer_x64"
        )]
        executable: PathBuf,
        #[arg(long, default_value_t = 300)]
        timeout: u64,
        #[arg(long)]
        health: Option<u32>,
    },
}

#[derive(Clone, Copy, ValueEnum)]
enum StreamKind {
    Logs,
    Lifecycle,
    Events,
}
impl StreamKind {
    fn name(self) -> &'static str {
        match self {
            Self::Logs => "logs",
            Self::Lifecycle => "lifecycle",
            Self::Events => "events",
        }
    }
}

#[derive(Subcommand)]
enum Scripts {
    Build {
        source: PathBuf,
        #[arg(long)]
        output: PathBuf,
    },
    Verify {
        path: PathBuf,
        #[arg(long)]
        source: Option<PathBuf>,
    },
}

#[derive(Subcommand)]
enum Annotations {
    Components {
        source: PathBuf,
        #[arg(long)]
        output: Option<PathBuf>,
    },
    Modutil {
        source: PathBuf,
        #[arg(long)]
        output: Option<PathBuf>,
    },
}

#[derive(Subcommand)]
enum Config {
    Schema {
        #[arg(default_value = "ClusterConfig")]
        model: String,
    },
    Validate {
        model: String,
        value: String,
        #[arg(long)]
        resolved: bool,
        #[arg(long)]
        secrets: bool,
    },
    Files {
        value: String,
    },
}

#[derive(Subcommand)]
enum Template {
    List,
    Build {
        name: String,
        #[arg(long, default_value_t = 1)]
        number: u16,
        #[arg(long)]
        token_file: PathBuf,
        #[arg(long)]
        cluster_key: Option<String>,
        #[arg(long)]
        settings: Option<String>,
        #[arg(long)]
        secrets: bool,
    },
}

fn input(source: &str) -> Result<Value> {
    let data = if source == "-" {
        let mut data = Vec::new();
        std::io::stdin()
            .take(1024 * 1024 + 1)
            .read_to_end(&mut data)?;
        data
    } else if let Some(path) = source.strip_prefix('@') {
        let mut data = Vec::new();
        std::fs::File::open(path)?
            .take(1024 * 1024 + 1)
            .read_to_end(&mut data)?;
        data
    } else {
        source.as_bytes().to_vec()
    };
    model::parse_json(&data).context("invalid JSON input")
}

fn emit(value: &impl serde::Serialize, compact: bool) -> Result<()> {
    writeln!(
        std::io::stdout().lock(),
        "{}",
        if compact {
            serde_json::to_string(value)?
        } else {
            serde_json::to_string_pretty(value)?
        }
    )?;
    Ok(())
}

fn write_text(output: Option<&Path>, content: &str) -> Result<()> {
    if let Some(path) = output {
        std::fs::write(path, content)?;
    } else {
        std::io::stdout().lock().write_all(content.as_bytes())?;
    }
    Ok(())
}

async fn remote(socket: &Path, envelope: model::Envelope) -> model::Result<Value> {
    let client = rpc::Client::connect(socket).await?;
    let result = client.call(envelope).await;
    let _ = client.close().await;
    result
}

pub fn main(arguments: impl IntoIterator<Item = OsString>) -> u8 {
    let arguments = match Arguments::try_parse_from(arguments) {
        Ok(arguments) => arguments,
        Err(error) => {
            let _ = error.print();
            return error.exit_code() as u8;
        }
    };
    let compact = arguments.json;
    let result = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .map_err(anyhow::Error::from)
        .and_then(|runtime| runtime.block_on(run(arguments)));
    match result {
        Ok(status) => status,
        Err(error) => {
            if compact {
                let value = error
                    .downcast_ref::<model::Error>()
                    .and_then(|error| serde_json::to_value(error).ok())
                    .unwrap_or_else(|| json!({"code":"invalid","message":format!("{error:#}")}));
                eprintln!("{}", json!({"error":value}));
            } else {
                eprintln!("{error:#}");
            }
            1
        }
    }
}

async fn run(arguments: Arguments) -> Result<u8> {
    let compact = arguments.json;
    match arguments.command {
        Command::Agent {
            cluster,
            executable,
            socket,
            profile,
            offline,
        } => {
            let mut driver = dst_server::driver::DriverOptions {
                profile,
                ..Default::default()
            };
            if offline {
                driver.extra_args.push("-offline".into());
            }
            return tokio::task::LocalSet::new()
                .run_until(dst_server::agent::run(dst_server::agent::Options {
                    cluster,
                    executable,
                    socket,
                    driver,
                }))
                .await;
        }
        Command::Host(arguments) => {
            return host_cli::run(arguments, compact)
                .await
                .map(|success| if success { 0 } else { 1 });
        }
        Command::Archive(arguments) => {
            return archive_cli::run(arguments, compact)
                .await
                .map(|success| if success { 0 } else { 1 });
        }
        Command::Logs(arguments) => {
            return logs_cli::run(arguments, compact)
                .await
                .map(|success| if success { 0 } else { 1 });
        }
        Command::Call {
            method,
            arguments,
            socket,
            shard,
            timeout,
        } => {
            let request = serde_json::from_value::<model::Request>(
                json!({"method":method,"arguments":input(&arguments)?}),
            )?;
            let envelope = model::Envelope {
                target: shard.map_or(model::Target::Room, model::Target::Shard),
                request,
                timeout,
            };
            envelope.validate()?;
            ensure!(
                socket.len() <= 1024,
                "at most 1024 target sockets are allowed"
            );
            let results: Vec<_> = stream::iter(socket.into_iter().map(|socket| {
                let envelope = envelope.clone();
                async move {
                    let result = remote(&socket, envelope).await;
                    (socket, result)
                }
            }))
            .buffered(8)
            .collect()
            .await;
            let success = results.iter().all(|(_, result)| result.is_ok());
            let values: Vec<_> = results
                .into_iter()
                .map(|(socket, result)| match result {
                    Ok(value) => json!({"socket":socket,"result":{"ok":true,"value":value}}),
                    Err(error) => json!({"socket":socket,"result":{"ok":false,"error":error}}),
                })
                .collect();
            emit(&values, compact)?;
            return Ok(if success { 0 } else { 1 });
        }
        Command::Describe { shard } => emit(
            &model::describe(if shard {
                model::Scope::Shard
            } else {
                model::Scope::Room
            }),
            compact,
        )?,
        Command::Subscribe { kind, socket } => {
            let mut interrupt =
                tokio::signal::unix::signal(tokio::signal::unix::SignalKind::interrupt())?;
            let mut terminate =
                tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;
            let client = rpc::Client::connect(socket).await?;
            let subscription = client.subscribe(kind.name()).await?;
            let result: Result<()> = async {
                loop {
                    let batch = tokio::select! {
                        batch = subscription.next(512) => batch?,
                        _ = interrupt.recv() => break,
                        _ = terminate.recv() => break,
                    };
                    emit(&batch, compact)?;
                    if batch.closed {
                        break;
                    }
                }
                Ok(())
            }
            .await;
            let subscription_closed = subscription.close().await;
            let client_closed = client.close().await;
            result?;
            subscription_closed?;
            client_closed?;
        }
        Command::Console {
            socket,
            shard,
            source,
            file,
        } => {
            let source = source.or(file.map(std::fs::read_to_string).transpose()?);
            let client = rpc::Client::connect(socket).await?;
            let lines: Box<dyn Iterator<Item = std::io::Result<String>>> =
                if let Some(source) = source {
                    Box::new(std::iter::once(Ok(source)))
                } else {
                    Box::new(std::io::stdin().lock().lines())
                };
            let mut success = true;
            for source in lines {
                let envelope = model::Envelope::new(
                    model::Target::Shard(shard.clone()),
                    model::Request::Execute { source: source? },
                )?;
                match client.call(envelope).await {
                    Ok(value) => emit(&value, compact)?,
                    Err(error) => {
                        emit(&json!({"error":error}), compact)?;
                        success = false;
                    }
                }
            }
            client.close().await?;
            return Ok(if success { 0 } else { 1 });
        }
        Command::Scripts(command) => match command {
            Scripts::Build { source, output } => {
                emit(&dst_server::scripts::build_bundle(source, output)?, compact)?
            }
            Scripts::Verify { path, source } => emit(
                &dst_server::scripts::verify_bundle(path, source.as_deref())?,
                compact,
            )?,
        },
        Command::Annotations(command) => match command {
            Annotations::Components { source, output } => write_text(
                output.as_deref(),
                &dst_server::annotations::generate_components(source)?,
            )?,
            Annotations::Modutil { source, output } => write_text(
                output.as_deref(),
                &dst_server::annotations::generate_modutil(source)?,
            )?,
        },
        Command::Config(command) => match command {
            Config::Schema { model } if model == "Room" => {
                emit(&dst_server::rooms::schema(), compact)?
            }
            Config::Schema { model } => emit(settings::schema(&model)?, compact)?,
            Config::Validate {
                model,
                value,
                resolved,
                secrets,
            } => {
                let value = input(&value)?;
                let value = if model == "Room" {
                    let room = dst_server::rooms::Room::from_value(value)?;
                    if resolved {
                        room.resolved()
                    } else {
                        room.as_value()
                    }
                } else {
                    settings::validate(&model, &value)?;
                    if resolved {
                        settings::resolved(&model, &value)
                    } else {
                        value
                    }
                };
                emit(
                    &if secrets {
                        value
                    } else {
                        settings::redact(value)
                    },
                    compact,
                )?;
            }
            Config::Files { value } => emit(
                &settings::ClusterConfig::from_value(input(&value)?)?.files()?,
                compact,
            )?,
        },
        Command::Template(command) => match command {
            Template::List => emit(&dst_server::rooms::template_names(), compact)?,
            Template::Build {
                name,
                number,
                token_file,
                cluster_key,
                settings,
                secrets,
            } => {
                let token = std::fs::read_to_string(token_file)?;
                let settings = settings
                    .map(|source| input(&source).and_then(settings::ClusterSettings::from_value))
                    .transpose()?;
                let configuration = dst_server::rooms::build_template(
                    &name,
                    number,
                    token.trim(),
                    cluster_key.as_deref(),
                    settings.as_ref(),
                )?;
                if secrets {
                    emit(configuration.as_value(), compact)?;
                } else {
                    emit(&configuration, compact)?;
                }
            }
        },
        Command::Completion { shell } => clap_complete::generate(
            shell,
            &mut Arguments::command(),
            "dst-server",
            &mut std::io::stdout(),
        ),
        Command::Inspect { directory } => {
            println!(
                "{}",
                serde_json::to_string_pretty(&dst_server::configuration::discover(directory)?)?
            );
        }
        Command::Probe {
            cluster,
            executable,
            timeout,
            rounds,
        } => {
            let report = dst_server::probe::run(&cluster, &executable, timeout, rounds).await?;
            println!("{}", serde_json::to_string_pretty(&report)?);
        }
        Command::ProbeRecovery {
            cluster,
            executable,
            timeout,
            targets,
        } => {
            let targets = targets
                .map(|path| -> Result<_> {
                    let mut bytes = Vec::new();
                    std::fs::File::open(path)?
                        .take(1024 * 1024 + 1)
                        .read_to_end(&mut bytes)?;
                    Ok(model::parse_json(&bytes)?)
                })
                .transpose()?;
            let report =
                dst_server::probe::recovery(&cluster, &executable, timeout, targets.as_ref())
                    .await?;
            println!("{}", serde_json::to_string_pretty(&report)?);
        }
        Command::ProbePlayer {
            cluster,
            executable,
            timeout,
            health,
        } => {
            let report =
                dst_server::probe::player_fixture(&cluster, &executable, timeout, health).await?;
            println!("{}", serde_json::to_string_pretty(&report)?);
        }
    }
    Ok(0)
}
