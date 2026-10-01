//! Native building blocks for the DST room agent.

pub mod agent;
pub mod annotations;
pub mod archive;
pub mod cli;
pub mod configuration;
pub mod deployment;
pub mod driver;
pub mod events;
pub mod external;
pub mod files;
pub mod host;
pub mod host_operations;
pub mod logs;
pub mod lua;
pub mod model;
pub mod mods;
pub mod observability;
pub mod policy;
pub mod preloader;
pub mod probe;
pub mod process;
pub mod recovery;
pub mod room;
pub mod rooms;
pub mod rpc;
pub mod scripts;
pub mod settings;
pub mod telemetry;

#[allow(clippy::all, dead_code)]
pub(crate) mod room_capnp {
    include!(concat!(env!("OUT_DIR"), "/room_capnp.rs"));
}
