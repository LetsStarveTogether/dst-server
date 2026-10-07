use super::*;
use crate::steam::tests::Server;
use serde_json::{Value, json};

fn details() -> Value {
    let fixture: Value = serde_json::from_str(include_str!(
        "../../../tests/fixtures/steam/published_file_details.json"
    ))
    .unwrap();
    fixture["response"]["publishedfiledetails"][0].clone()
}

fn response(items: Vec<Value>) -> Vec<u8> {
    let body = serde_json::to_vec(&json!({"response": {
        "result": 1, "resultcount": items.len(), "publishedfiledetails": items
    }}))
    .unwrap();
    let mut response = format!(
        "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
        body.len()
    )
    .into_bytes();
    response.extend(body);
    response
}

fn installed() -> Version {
    let remote: steam::PublishedFileDetails = serde_json::from_value(details()).unwrap();
    Version {
        content: remote.content_handle,
        updated_at: remote.updated_at,
    }
}

#[test]
fn content_handles_are_compared_as_identities_and_older_metadata_cannot_trigger_updates() {
    let local = installed();
    let mut remote: steam::PublishedFileDetails = serde_json::from_value(details()).unwrap();
    assert!(!changed(&local, &remote));
    remote.updated_at += chrono::TimeDelta::seconds(1);
    assert!(
        !changed(&local, &remote),
        "a title or description edit does not change content"
    );
    remote.content_handle = steam::UgcHandle(1);
    assert!(
        changed(&local, &remote),
        "smaller content handles can be newer content"
    );
    remote.updated_at = local.updated_at;
    assert!(
        changed(&local, &remote),
        "two uploads can occur within the same second"
    );
    remote.updated_at -= chrono::TimeDelta::seconds(1);
    assert!(
        !changed(&local, &remote),
        "an older response must not trigger a downgrade"
    );
    for sentinel in [0, u64::MAX] {
        remote.content_handle = steam::UgcHandle(sentinel);
        remote.updated_at = local.updated_at;
        assert!(!changed(&local, &remote));
        remote.updated_at += chrono::TimeDelta::seconds(1);
        assert!(changed(&local, &remote));
    }
}

#[tokio::test]
async fn batches_are_bounded_and_missing_private_banned_or_foreign_items_are_not_updates() {
    let mut items: Vec<_> = (1..=103)
        .map(|id| {
            let mut item = details();
            item["publishedfileid"] = json!(id.to_string());
            item["hcontent_file"] = json!("1");
            item
        })
        .collect();
    items[1] = json!({"publishedfileid":"2", "result":15});
    items[2]["banned"] = json!(true);
    items[3]["consumer_app_id"] = json!(1);
    let server = Server::start(vec![
        response(items[..100].to_vec()),
        response(items[100..].to_vec()),
    ])
    .await;
    let ids = (1..=103).collect();
    let versions = (1..=102).map(|id| (id, installed())).collect();
    let report = query(&server.client(), &ids, &versions).await.unwrap();
    assert_eq!(server.count(), 2);
    assert_eq!(report.unavailable, BTreeSet::from([2, 3, 4, 103]));
    assert_eq!(report.outdated.len(), 99);
    assert!(report.outdated.contains(&1));
    assert!(report.outdated.contains(&102));
    assert!(report.checked_at.is_some());
    assert!(report.error.is_none());
}

#[tokio::test]
async fn no_enabled_workshop_mods_need_no_manifest_or_network() {
    let server = Server::start(vec![Vec::new()]).await;
    let report = query(&server.client(), &BTreeSet::new(), &BTreeMap::new())
        .await
        .unwrap();
    assert_eq!(server.count(), 0);
    assert!(report.outdated.is_empty());
    assert!(report.unavailable.is_empty());
}

#[tokio::test]
async fn transport_failures_discard_partial_batch_observations() {
    let items = (1..=100)
        .map(|id| {
            let mut item = details();
            item["publishedfileid"] = json!(id.to_string());
            item["hcontent_file"] = json!("1");
            item
        })
        .collect();
    let server = Server::start(vec![
        response(items),
        b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".to_vec(),
    ])
    .await;
    let error = query(
        &server.client(),
        &(1..=101).collect(),
        &(1..=101).map(|id| (id, installed())).collect(),
    )
    .await
    .unwrap_err();
    assert_eq!(server.count(), 2);
    assert!(error.to_string().contains("403"));
}
