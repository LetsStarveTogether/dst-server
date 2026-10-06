use std::{fs, sync::Arc};

use dst_server::{driver::DriverOptions, room::Room, rpc::EventHub};
use tempfile::TempDir;

pub(super) fn open(
    telemetry: dst_server::telemetry::TelemetryConfig,
) -> anyhow::Result<(TempDir, Arc<Room>)> {
    let directory = tempfile::tempdir()?;
    fs::write(directory.path().join("cluster.ini"), "")?;
    fs::create_dir(directory.path().join("Master"))?;
    fs::write(
        directory.path().join("Master/server.ini"),
        "[SHARD]\nis_master=true\n",
    )?;
    let room = Room::open(
        directory.path(),
        "/unused-game",
        DriverOptions::default(),
        EventHub::default(),
        telemetry,
    )?;
    Ok((directory, room))
}
