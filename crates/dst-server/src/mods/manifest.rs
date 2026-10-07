//! Read Steam's installed Workshop records; cached remote details are not installation evidence.

use std::collections::BTreeMap;

use anyhow::{Context, Result, ensure};
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};

use crate::{files, steam::UgcHandle};

pub(super) const PATH: &str = "mods/ugc/appworkshop_322330.acf";

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub(crate) struct Version {
    pub content: UgcHandle,
    pub updated_at: DateTime<Utc>,
}

#[derive(Deserialize)]
struct Workshop {
    appid: u32,
    #[serde(default, rename = "WorkshopItemsInstalled")]
    installed: BTreeMap<String, Item>,
}

#[derive(Deserialize)]
struct Item {
    manifest: u64,
    timeupdated: i64,
}

pub(super) fn parse(source: &str) -> Result<BTreeMap<u64, Version>> {
    ensure!(
        source.len() <= files::MAX_FILE_BYTES,
        "Workshop manifest exceeds the byte limit"
    );
    // Bound recursive parsing before the VDF library builds its tree.
    let mut bytes = source.bytes().peekable();
    let mut depth = 0_usize;
    let mut quoted = false;
    while let Some(byte) = bytes.next() {
        if quoted {
            match byte {
                b'\\' => {
                    bytes.next();
                }
                b'"' => quoted = false,
                _ => {}
            }
        } else {
            match byte {
                b'"' => quoted = true,
                b'/' if bytes.peek() == Some(&b'/') => {
                    for byte in bytes.by_ref() {
                        if byte == b'\n' {
                            break;
                        }
                    }
                }
                b'{' => {
                    depth += 1;
                    ensure!(depth <= 16, "Workshop manifest nesting exceeds the limit");
                }
                b'}' => {
                    depth = depth
                        .checked_sub(1)
                        .context("invalid Workshop manifest nesting")?
                }
                b'#' => anyhow::bail!("Workshop manifest file references are not supported"),
                _ => {}
            }
        }
    }
    ensure!(!quoted && depth == 0, "incomplete Workshop manifest");
    let (workshop, key): (Workshop, _) = keyvalues_serde::from_str_with_key(source)
        .map_err(|_| anyhow::anyhow!("invalid Workshop installation manifest"))?;
    ensure!(
        key == "AppWorkshop" && workshop.appid == 322330,
        "Workshop manifest is for another application"
    );
    workshop
        .installed
        .into_iter()
        .map(|(id, item)| {
            let id = crate::settings::workshop_id(&id)?;
            let updated_at = DateTime::from_timestamp(item.timeupdated, 0)
                .filter(|_| item.timeupdated >= 0)
                .context("invalid installed Workshop timestamp")?;
            Ok((
                id,
                Version {
                    content: UgcHandle(item.manifest),
                    updated_at,
                },
            ))
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    const SOURCE: &str = r#""AppWorkshop" {
        "appid" "322330"
        "WorkshopItemsInstalled" {
            "2189004162" { "manifest" "3558921307329919114" "timeupdated" "1793675412" }
        }
        "WorkshopItemDetails" {
            "2189004162" { "latest_manifest" "99" "latest_timeupdated" "1793675413" }
            "1803285852" { "manifest" "88" "timeupdated" "1793675413" }
        }
    }"#;

    #[test]
    fn only_installed_content_is_evidence_and_64_bit_handles_stay_exact() {
        let items = parse(SOURCE).unwrap();
        assert_eq!(items.len(), 1);
        assert_eq!(items[&2189004162].content, UgcHandle(3558921307329919114));
        assert_eq!(items[&2189004162].updated_at.timestamp(), 1793675412);
        assert!(
            parse(r#""AppWorkshop" { "appid" "322330" }"#)
                .unwrap()
                .is_empty()
        );
    }

    #[test]
    fn malformed_and_unbounded_manifests_are_rejected() {
        for source in [
            SOURCE.replace("AppWorkshop", "Other"),
            SOURCE.replace("322330", "1"),
            SOURCE.replace("2189004162", "01"),
            SOURCE.replace("1793675412", "-1"),
            SOURCE.replace("3558921307329919114", "18446744073709551616"),
            SOURCE[..SOURCE.len() - 1].to_owned(),
            format!("#include \"other.acf\"\n{SOURCE}"),
            format!("{}{}", "\"nested\" {".repeat(100), "}".repeat(100)),
            " ".repeat(files::MAX_FILE_BYTES + 1),
        ] {
            assert!(parse(&source).is_err());
        }
        assert!(parse(&format!("// ignored {{\n{SOURCE}")).is_ok());
    }
}
