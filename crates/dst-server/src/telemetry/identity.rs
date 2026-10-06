//! Resource facts come from this artifact and its selected native installation.

use serde_json::{Map, Value, json};
use std::{io::Read, os::unix::fs::OpenOptionsExt, path::Path};

pub(super) fn apply(resource: &mut Map<String, Value>, executable: Option<&Path>) {
    resource
        .entry("service.name")
        .or_insert_with(|| json!("dst-server"));
    resource.insert("service.version".into(), json!(env!("CARGO_PKG_VERSION")));
    resource.remove("dst.build.revision");
    if !env!("DST_BUILD_REVISION").is_empty() {
        resource.insert(
            "dst.build.revision".into(),
            json!(env!("DST_BUILD_REVISION")),
        );
    }
    resource.insert("dst.build.dirty".into(), json!(env!("DST_BUILD_DIRTY")));
    resource.insert(
        "dst.game.version".into(),
        json!(
            executable
                .and_then(game_version)
                .unwrap_or_else(|| "unknown".into())
        ),
    );
}

fn game_version(executable: &Path) -> Option<String> {
    let executable = executable.canonicalize().ok()?;
    let bin = executable.parent()?;
    if bin.file_name()? != "bin64" {
        return None;
    }
    let file = std::fs::OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NONBLOCK)
        .open(bin.parent()?.join("version.txt"))
        .ok()?;
    if !file.metadata().ok()?.is_file() {
        return None;
    }
    let mut bytes = Vec::new();
    file.take(129).read_to_end(&mut bytes).ok()?;
    if bytes.len() > 128 {
        return None;
    }
    let value = std::str::from_utf8(&bytes)
        .ok()?
        .trim_end_matches(['\r', '\n']);
    (!value.is_empty() && value.bytes().all(|byte| byte.is_ascii_digit())).then(|| value.into())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{deployment::SYSTEMD_UNIT_ENV, telemetry::TelemetryConfig};

    #[test]
    fn only_the_agent_uses_its_quadlet_unit_name() -> anyhow::Result<()> {
        let parent = tempfile::tempdir()?;
        let game = parent.path().join("game");
        let lookup = |key: &str| (key == SYSTEMD_UNIT_ENV).then(|| "bot.service".to_owned());
        for name in ["first-room", "second-room"] {
            let directory = parent.path().join(name);
            std::fs::create_dir(&directory)?;
            let mut config = TelemetryConfig::from_lookup(lookup)?;
            assert_eq!(config.bind_room(&game, &directory), name);
            assert_eq!(config.resource["dst.cluster.name"], name);
        }
        let mut agent = TelemetryConfig::for_agent(lookup)?;
        assert_eq!(agent.bind_room(&game, parent.path()), "bot");
        Ok(())
    }

    #[test]
    fn game_identity_uses_the_canonical_installation_and_bounded_digits() {
        let directory = tempfile::tempdir().unwrap();
        let bin = directory.path().join("bin64");
        std::fs::create_dir(&bin).unwrap();
        let game = bin.join("game");
        std::fs::write(&game, b"").unwrap();
        let linked = directory.path().join("mounted-game");
        std::os::unix::fs::symlink(&game, &linked).unwrap();
        let version = directory.path().join("version.txt");
        assert_eq!(game_version(&linked), None);
        for (contents, expected) in [
            ("762172\r\n".to_owned(), Some("762172".into())),
            ("8".repeat(128), Some("8".repeat(128))),
            ("8".repeat(129), None),
            (format!("{}\r\n", "8".repeat(126)), Some("8".repeat(126))),
            (format!("{}\r\n", "8".repeat(127)), None),
            (String::new(), None),
            ("1\n2".into(), None),
            ("１２３".into(), None),
            (" 123".into(), None),
        ] {
            std::fs::write(&version, contents).unwrap();
            assert_eq!(game_version(&linked), expected);
        }
        std::fs::write(&version, [0xff]).unwrap();
        assert_eq!(game_version(&linked), None);
        std::fs::remove_file(&version).unwrap();
        std::fs::create_dir(&version).unwrap();
        assert_eq!(game_version(&linked), None);
        let mut resource = json!({"dst.build.revision":"old-image","dst.build.dirty":"false","dst.game.version":"old-game","service.version":"old-sdk"}).as_object().unwrap().clone();
        apply(&mut resource, Some(&linked));
        assert_eq!(resource["dst.game.version"], "unknown");
        assert_ne!(
            resource.get("dst.build.revision"),
            Some(&json!("old-image"))
        );
        assert_eq!(resource["service.version"], env!("CARGO_PKG_VERSION"));
        std::fs::remove_dir(&version).unwrap();
        let contents = directory.path().join("selected-version");
        std::fs::write(&contents, "762173\n").unwrap();
        std::os::unix::fs::symlink(&contents, &version).unwrap();
        assert_eq!(game_version(&linked), Some("762173".into()));
    }
}
