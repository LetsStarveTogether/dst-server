//! Command line adapter for native host operations.

use std::{collections::BTreeSet, path::PathBuf};

use crate::{
    host::Host,
    host_operations::HostOperation,
    rooms::{self, Room},
};
use anyhow::{Context, Result, ensure};
use clap::{Args, Subcommand};
use serde_json::Value;

#[derive(Args)]
pub struct Arguments {
    #[arg(long, default_value = "/srv/dst", global = true)]
    root: PathBuf,
    #[arg(long, default_value = "/etc/containers/systemd", global = true)]
    quadlet_dir: PathBuf,
    #[arg(long, default_value = "systemctl", global = true)]
    systemctl: PathBuf,
    #[arg(long, global = true)]
    user: bool,
    #[command(subcommand)]
    command: Command,
}

#[derive(Args)]
struct Selection {
    /// Room numbers, comma-separated lists or ascending ranges (000-099).
    #[arg(long = "room", conflicts_with = "all")]
    rooms: Vec<String>,
    #[arg(long)]
    all: bool,
    #[arg(long)]
    template: Option<String>,
}

#[derive(Args)]
struct Lifecycle {
    #[command(flatten)]
    selection: Selection,
    #[arg(long)]
    no_wait: bool,
    #[arg(long, default_value_t = 900.0)]
    timeout: f64,
}

#[derive(Subcommand)]
enum Command {
    /// List discovered room services, preserving individual failures.
    List,
    /// Show a room or a value addressed by JSON Pointer; secrets are redacted.
    Show {
        number: u16,
        #[arg(long)]
        field: Option<String>,
    },
    Schema,
    /// Create one room from its complete JSON definition, @file, or stdin (-).
    Create {
        value: String,
    },
    /// Create rooms from a JSON array of complete room definitions.
    Provision {
        value: String,
    },
    /// Create packaged LST fleet rooms without overwriting existing rooms.
    Fleet {
        #[arg(long = "room", conflicts_with = "all")]
        rooms: Vec<String>,
        #[arg(long)]
        all: bool,
        #[arg(long)]
        token_file: PathBuf,
        #[arg(long)]
        cluster_key: Option<String>,
        #[arg(long)]
        image: Option<String>,
        #[arg(long)]
        userns: Option<String>,
        #[arg(long)]
        volume_idmap: Option<String>,
    },
    Edit {
        #[command(flatten)]
        selection: Selection,
        /// Assignment in the form /json/pointer=JSON.
        #[arg(long = "set")]
        changes: Vec<String>,
        #[arg(long = "unset")]
        unset: Vec<String>,
    },
    Start(Lifecycle),
    Stop(Lifecycle),
    Restart(Lifecycle),
    Wait {
        #[command(flatten)]
        selection: Selection,
        #[arg(long, default_value_t = 900.0)]
        timeout: f64,
    },
    Status {
        #[command(flatten)]
        selection: Selection,
        #[arg(long)]
        service_only: bool,
    },
    Diagnose(Selection),
    /// Run a shared HostOperation JSON request against selected rooms.
    Run {
        request: String,
        #[command(flatten)]
        selection: Selection,
    },
}

fn numbers(values: &[String]) -> Result<Vec<u16>> {
    let mut result = BTreeSet::new();
    for value in values.iter().flat_map(|value| value.split(',')) {
        let bounds: Vec<_> = value.split('-').collect();
        ensure!(
            (1..=2).contains(&bounds.len())
                && bounds.iter().all(|bound| !bound.is_empty()
                    && bound.len() <= 3
                    && bound.bytes().all(|byte| byte.is_ascii_digit())),
            "invalid room selection"
        );
        let start: u16 = bounds[0].parse()?;
        let end: u16 = bounds[bounds.len() - 1].parse()?;
        ensure!(
            start <= end && end <= rooms::MAX_ROOM_SLOT,
            "room numbers must be between 000 and 299 in ascending ranges"
        );
        result.extend(start..=end);
    }
    Ok(result.into_iter().collect())
}

async fn select(host: &Host, selection: &Selection) -> Result<Vec<u16>> {
    ensure!(
        selection.all || !selection.rooms.is_empty() || selection.template.is_some(),
        "select --room, --template or --all explicitly"
    );
    let mut selected = Vec::new();
    for number in if selection.rooms.is_empty() {
        host.rooms.numbers()?
    } else {
        numbers(&selection.rooms)?
    } {
        if let Some(template) = &selection.template
            && host.load(number).await?.template.as_ref() != Some(template)
        {
            continue;
        }
        selected.push(number);
    }
    ensure!(!selected.is_empty(), "no rooms matched the selection");
    Ok(selected)
}

fn emit_batch(values: &[Value], compact: bool) -> Result<bool> {
    super::emit(&values, compact)?;
    Ok(values.iter().all(|value| value["result"]["ok"] == true))
}

pub async fn run(arguments: Arguments, compact: bool) -> Result<bool> {
    let mut host = Host::new(arguments.root, arguments.quadlet_dir);
    host.systemd.executable = arguments.systemctl;
    host.systemd.user = arguments.user;
    let (selection, operation) = match arguments.command {
        Command::List => {
            return emit_batch(
                &host
                    .batch(
                        &host.rooms.numbers()?,
                        &HostOperation::Status { game: false },
                    )
                    .await?,
                compact,
            );
        }
        Command::Show { number, field } => {
            let room = host.load(number).await?;
            let value = if let Some(pointer) = field {
                room.get(&pointer)?
            } else {
                serde_json::to_value(room)?
            };
            super::emit(&value, compact)?;
            return Ok(true);
        }
        Command::Schema => {
            super::emit(&rooms::schema(), compact)?;
            return Ok(true);
        }
        Command::Create { value } => {
            super::emit(
                &host
                    .create(&Room::from_value(super::input(&value)?)?)
                    .await?,
                compact,
            )?;
            return Ok(true);
        }
        Command::Provision { value } => {
            let definitions: Vec<Room> = serde_json::from_value(super::input(&value)?)?;
            ensure!(
                !definitions.is_empty(),
                "provide at least one room definition"
            );
            return emit_batch(&host.provision(&definitions).await?, compact);
        }
        Command::Fleet {
            rooms: selected,
            all,
            token_file,
            cluster_key,
            image,
            userns,
            volume_idmap,
        } => {
            let token = std::fs::read_to_string(token_file).context("reading token file")?;
            ensure!(!token.trim().is_empty(), "token file is empty");
            let selected = if all {
                rooms::room_numbers()
            } else {
                numbers(&selected)?
            };
            ensure!(!selected.is_empty(), "select --room or --all explicitly");
            let mut definitions = Vec::new();
            for number in selected {
                let mut definition =
                    rooms::fleet_room(number, token.trim(), cluster_key.as_deref())?;
                if let Some(image) = &image {
                    definition.deployment.image = image.clone();
                }
                definition.deployment.userns = userns.clone();
                definition.deployment.volume_idmap = volume_idmap.clone();
                definitions.push(definition);
            }
            return emit_batch(&host.provision(&definitions).await?, compact);
        }
        Command::Edit {
            selection,
            changes,
            unset,
        } => {
            let changes = changes
                .iter()
                .map(|assignment| {
                    let (pointer, value) = assignment
                        .split_once('=')
                        .context("--set requires /json/pointer=JSON")?;
                    Ok((
                        pointer.to_owned(),
                        crate::model::parse_json(value.as_bytes())
                            .context("invalid JSON in --set")?,
                    ))
                })
                .collect::<Result<Vec<_>>>()?;
            (selection, HostOperation::Edit { changes, unset })
        }
        Command::Start(args) => (
            args.selection,
            HostOperation::Start {
                wait: !args.no_wait,
                timeout: args.timeout,
            },
        ),
        Command::Stop(args) => (
            args.selection,
            HostOperation::Stop {
                wait: !args.no_wait,
                timeout: args.timeout,
            },
        ),
        Command::Restart(args) => (
            args.selection,
            HostOperation::Restart {
                wait: !args.no_wait,
                timeout: args.timeout,
            },
        ),
        Command::Wait { selection, timeout } => (selection, HostOperation::WaitReady { timeout }),
        Command::Status {
            selection,
            service_only,
        } => (
            selection,
            HostOperation::Status {
                game: !service_only,
            },
        ),
        Command::Diagnose(selection) => (selection, HostOperation::Diagnose),
        Command::Run { request, selection } => {
            (selection, serde_json::from_value(super::input(&request)?)?)
        }
    };
    operation.validate()?;
    let selected = select(&host, &selection).await?;
    emit_batch(&host.batch(&selected, &operation).await?, compact)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn selections_are_bounded_and_deduplicated() {
        assert_eq!(
            numbers(&["002-004,003".into(), "001".into()]).unwrap(),
            [1, 2, 3, 4]
        );
        for input in ["", "-1", "1-", "4-2", "1-300", "1-2-3", "1,a", "0001", "1,"] {
            assert!(numbers(&[input.into()]).is_err(), "{input}");
        }
    }
}
