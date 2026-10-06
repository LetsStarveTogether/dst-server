use super::*;
use crate::steam::tests::Server;
use serde_json::{Value, json};

const ID: u64 = 2189004162;

fn details(version: &str) -> Value {
    let fixture: Value = serde_json::from_str(include_str!(
        "../../../tests/fixtures/steam/published_file_details.json"
    ))
    .unwrap();
    let mut item = fixture["response"]["publishedfiledetails"][0].clone();
    item["tags"] = json!([{"tag": format!("version:{version}")}]);
    item["hcontent_file"] = json!("1");
    item
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

fn loaded(version: &str) -> BTreeMap<String, String> {
    [
        ("Master".into(), version.into()),
        ("Caves".into(), version.into()),
    ]
    .into()
}

#[tokio::test]
async fn game_declarations_define_updates_without_content_or_version_ordering() {
    for (name, local, remote, outdated) in [
        ("same_version_new_content", "1.0.50", "1.0.50", false),
        ("different", "1.0", "2.0", true),
        ("no_ordering", "2.0", "1.0", true),
        ("leading_zero", "01", "1", true),
        ("no_numeric_conversion", "1", "1.0", true),
        (
            "native_ascii_rules",
            "v1.0-a",
            " \t\n\r\x0b\x0cV1.0-A \t\n\r\x0b\x0c",
            false,
        ),
        ("preserve_unicode", "Üv1.0", "ÜV1.0", false),
        ("no_unicode_case_folding", "üv1.0", "ÜV1.0", true),
    ] {
        let server = Server::start(vec![response(vec![details(remote)])]).await;
        let report = query(
            &server.client(),
            &[(ID, loaded(local))].into(),
            &UpdateTargets::new(),
        )
        .await
        .unwrap();
        assert!(
            report.unavailable.is_empty(),
            "{name}: {:?}",
            report.unavailable
        );
        assert_eq!(report.targets.contains_key(&ID), outdated, "{name}");
        assert_eq!(report.versions[&ID].declared.as_deref(), Some(remote));
    }
}

#[tokio::test]
async fn missing_conflicting_and_unbounded_versions_are_not_update_targets() {
    let mut duplicate = details("2");
    duplicate["tags"] = json!([{"tag":"version:2"}, {"tag":"version:2"}]);
    let mut conflict = duplicate.clone();
    conflict["tags"][1]["tag"] = json!("version:3");
    let mut missing = details("2");
    missing["tags"] = json!([]);
    for (name, local, item, available) in [
        ("duplicate_same", "1", duplicate, true),
        ("conflict", "1", conflict, false),
        ("missing_remote", "1", missing, false),
        ("empty_remote", "1", details(" \t"), false),
        ("control_character", "1", details("2\n3"), false),
        (
            "oversized_remote",
            "1",
            details(&"x".repeat(super::super::MAX_VERSION_BYTES + 1)),
            false,
        ),
        ("empty_local", "", details("2"), false),
        ("invalid_local", "1\n2", details("2"), false),
    ] {
        let server = Server::start(vec![response(vec![item])]).await;
        let report = query(
            &server.client(),
            &[(ID, loaded(local))].into(),
            &UpdateTargets::new(),
        )
        .await
        .unwrap();
        assert_eq!(report.targets.contains_key(&ID), available, "{name}");
        assert_eq!(report.unavailable.contains_key(&ID), !available, "{name}");
    }
}

#[tokio::test]
async fn every_participating_shard_must_be_present_and_match() {
    let required: UpdateTargets = [(
        ID,
        UpdateTarget {
            version: "2".into(),
            shards: ["Master".into(), "Caves".into()].into(),
        },
    )]
    .into();
    let required: UpdateTargets =
        serde_json::from_slice(&serde_json::to_vec(&required).unwrap()).unwrap();
    for (name, versions, matches, comparable) in [
        ("both_match", loaded("2"), true, true),
        (
            "split_versions",
            [("Master".into(), "1".into()), ("Caves".into(), "2".into())].into(),
            false,
            true,
        ),
        (
            "missing_original_shard",
            [("Caves".into(), "2".into())].into(),
            false,
            false,
        ),
        (
            "additional_shard_differs",
            [
                ("Master".into(), "2".into()),
                ("Caves".into(), "2".into()),
                ("Island".into(), "1".into()),
            ]
            .into(),
            false,
            true,
        ),
    ] {
        let loaded = [(ID, versions)].into();
        assert_eq!(satisfied(&loaded, &required), matches, "{name}");
        let server = Server::start(vec![response(vec![details("2")])]).await;
        let report = query(&server.client(), &loaded, &required).await.unwrap();
        assert_eq!(report.verification(&required).is_ok(), comparable, "{name}");
        if comparable && !matches {
            assert!(required[&ID].shards.is_subset(&report.targets[&ID].shards));
        }
    }
    let server = Server::start(vec![response(vec![details("2")])]).await;
    let missing = query(&server.client(), &LoadedMods::new(), &required)
        .await
        .unwrap();
    assert!(missing.verification(&required).is_err());
}

#[tokio::test]
async fn retry_verification_keeps_satisfied_targets_at_the_current_publication() {
    let second = ID + 1;
    let mut required: UpdateTargets = [
        (
            ID,
            UpdateTarget {
                version: "2".into(),
                shards: loaded("2").into_keys().collect(),
            },
        ),
        (
            second,
            UpdateTarget {
                version: "2".into(),
                shards: loaded("2").into_keys().collect(),
            },
        ),
    ]
    .into();
    let mut items = vec![details(" 3 "), details("2"), details("4")];
    items[1]["publishedfileid"] = json!(second.to_string());
    items[2]["publishedfileid"] = json!((second + 1).to_string());
    let mut satisfied = loaded("3");
    satisfied.insert("Island".into(), "3".into());
    let server = Server::start(vec![response(items)]).await;
    let report = query(
        &server.client(),
        &[
            (ID, satisfied),
            (second, loaded("1")),
            (second + 1, loaded("1")),
        ]
        .into(),
        &required,
    )
    .await
    .unwrap();
    assert_eq!(
        report.targets.len(),
        2,
        "ordinary checks list only outdated Mods"
    );
    assert!(!report.targets.contains_key(&ID));
    let retry = report.verification(&required).unwrap();
    assert_eq!(retry.len(), 3);
    assert_eq!(retry[&ID].version, "3");
    assert_eq!(
        retry[&ID].shards,
        ["Master".into(), "Caves".into(), "Island".into()].into()
    );
    required.insert(ID, retry[&ID].clone());
    for loaded in [
        BTreeMap::new(),
        [
            ("Master".into(), String::new()),
            ("Caves".into(), "3".into()),
            ("Island".into(), "3".into()),
        ]
        .into(),
        [
            ("Master".into(), "4".into()),
            ("Caves".into(), "3".into()),
            ("Island".into(), "3".into()),
        ]
        .into(),
    ] {
        let mut malformed = report.clone();
        malformed.versions.get_mut(&ID).unwrap().loaded = loaded;
        assert!(malformed.verification(&required).is_err());
    }
}

#[tokio::test]
async fn batches_are_bounded_and_unavailable_items_have_reasons() {
    let mut items: Vec<_> = (1..=103)
        .map(|id| {
            let mut item = details("2");
            item["publishedfileid"] = json!(id.to_string());
            item
        })
        .collect();
    items[1] = json!({"publishedfileid":"2", "result":15});
    items[2]["banned"] = json!(true);
    items[3]["consumer_app_id"] = json!(1);
    items[102]["tags"] = json!([]);
    let server = Server::start(vec![
        response(items[..100].to_vec()),
        response(items[100..].to_vec()),
    ])
    .await;
    let report = query(
        &server.client(),
        &(1..=103).map(|id| (id, loaded("1"))).collect(),
        &UpdateTargets::new(),
    )
    .await
    .unwrap();
    assert_eq!(server.count(), 2);
    assert_eq!(
        report.unavailable.keys().copied().collect::<BTreeSet<_>>(),
        BTreeSet::from([2, 3, 4, 103])
    );
    assert_eq!(report.targets.len(), 99);
    assert!(report.targets.contains_key(&1));
    assert!(report.targets.contains_key(&102));
    assert!(report.checked_at.is_some());
    assert!(report.error.is_none());
}

#[tokio::test]
async fn transport_failures_discard_partial_batch_observations() {
    let items = (1..=100)
        .map(|id| {
            let mut item = details("2");
            item["publishedfileid"] = json!(id.to_string());
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
        &(1..=101).map(|id| (id, loaded("1"))).collect(),
        &UpdateTargets::new(),
    )
    .await
    .unwrap_err();
    assert_eq!(server.count(), 2);
    assert!(error.to_string().contains("403"));
}
