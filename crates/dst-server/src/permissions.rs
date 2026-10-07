use anyhow::{Result, bail};
use serde_json::{Value, json};

use crate::{
    files::{StoppedRoom, edit_permission, permission_users},
    model::Request,
};

pub(crate) fn supports_offline(request: &Request) -> bool {
    matches!(
        request,
        Request::Ban { seconds: None, .. }
            | Request::Unban { .. }
            | Request::Blocklist {}
            | Request::ReadPermissions {}
            | Request::IsAdmin { .. }
            | Request::SetAdmin { .. }
            | Request::IsWhitelisted { .. }
            | Request::Whitelist { .. }
            | Request::Unwhitelist { .. }
    )
}

pub(crate) fn apply(stopped: &mut StoppedRoom<'_>, request: &Request) -> Result<Value> {
    if matches!(request, Request::ReadPermissions {}) {
        let mut result = serde_json::Map::new();
        for field in crate::settings::PERMISSION_FIELDS {
            let source = stopped
                .read_optional_bytes(format!("{field}.txt"))?
                .unwrap_or_default();
            result.insert(field.into(), json!(permission_users(&source)));
        }
        return Ok(Value::Object(result));
    }
    let file = match request {
        Request::Ban { seconds: None, .. } | Request::Unban { .. } | Request::Blocklist {} => {
            "blocklist.txt"
        }
        Request::IsAdmin { .. } | Request::SetAdmin { .. } => "adminlist.txt",
        Request::IsWhitelisted { .. } | Request::Whitelist { .. } | Request::Unwhitelist { .. } => {
            "whitelist.txt"
        }
        _ => bail!("operation requires a running game"),
    };
    let source = stopped.read_optional_bytes(file)?.unwrap_or_default();
    if matches!(request, Request::Blocklist {}) {
        let mut users = permission_users(&source);
        users.sort();
        users.dedup();
        return Ok(json!(users));
    }
    let userid = request.userid().expect("permission request has a userid");
    if matches!(
        request,
        Request::IsAdmin { .. } | Request::IsWhitelisted { .. }
    ) {
        return Ok(json!(
            permission_users(&source)
                .iter()
                .any(|entry| entry == userid)
        ));
    }
    let remove = matches!(
        request,
        Request::Unban { .. }
            | Request::Unwhitelist { .. }
            | Request::SetAdmin { enabled: false, .. }
    );
    let updated = edit_permission(&source, userid, remove);
    if updated != source {
        stopped.replace_permission(file, &updated)?;
    }
    Ok(Value::Null)
}
