use super::*;
use std::sync::{
    Arc,
    atomic::{AtomicUsize, Ordering},
};

use proptest::prelude::*;
use serde_json::{Value, json};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::TcpListener,
    sync::mpsc,
    task::{JoinHandle, JoinSet},
};

// Real response shapes from 2026-11-11; author IDs, URLs and prose use synthetic values.
const FIXTURE: &[u8] = include_bytes!("../../tests/fixtures/steam/published_file_details.json");
const IDS: [PublishedFileId; 3] = [
    PublishedFileId(2189004162),
    PublishedFileId(1803285852),
    PublishedFileId(u64::MAX),
];

fn fixture() -> Value {
    serde_json::from_slice(FIXTURE).unwrap()
}

fn parse(value: &Value, ids: &[PublishedFileId]) -> Result<Vec<PublishedFile>, Error> {
    decode(&serde_json::to_vec(value).unwrap(), ids)
}

#[test]
fn real_response_shapes_keep_typed_metadata_and_individual_failures() {
    let files = decode(FIXTURE, &IDS).unwrap();
    assert_eq!(files.iter().map(|file| file.id).collect::<Vec<_>>(), IDS);
    let details = files[0].details.as_ref().unwrap();
    assert_eq!(details.consumer_app_id, AppId(322330));
    assert_eq!(details.creator_app_id, AppId(245850));
    assert_eq!(details.creator, SteamId(76561198000000001));
    assert_eq!(details.content_handle, UgcHandle(3558921307329919114));
    assert_eq!(details.preview_handle, UgcHandle(1479948484468384018));
    assert_eq!(details.file_size, 33439940);
    assert_eq!(details.updated_at.timestamp(), 1793675412);
    assert_eq!(details.visibility, Visibility::Public);
    assert!(!details.banned);
    assert_eq!(details.file_url, None);
    assert_eq!(details.preview_url.as_ref().unwrap().scheme(), "https");
    assert!(details.description.contains("中文 🎮"));
    assert!(
        details
            .tags
            .iter()
            .any(|tag| tag.tag == "all_clients_require_mod")
    );
    assert_eq!(files[2].details, Err(ResultCode::FILE_NOT_FOUND));
    assert_eq!(
        serde_json::from_slice::<Vec<PublishedFile>>(&serde_json::to_vec(&files).unwrap()).unwrap(),
        files
    );
}

proptest! {
    #![proptest_config(ProptestConfig::with_cases(64))]

    #[test]
    fn identifiers_preserve_every_u64_bit(value: u64) {
        let id = PublishedFileId(value);
        let encoded = serde_json::to_string(&id).unwrap();
        prop_assert_eq!(&encoded, &format!("\"{value}\""));
        prop_assert_eq!(serde_json::from_str::<PublishedFileId>(&encoded).unwrap(), id);
        prop_assert_eq!(serde_json::from_str::<PublishedFileId>(&value.to_string()).unwrap(), id);
        prop_assert_eq!(id.to_string().parse::<PublishedFileId>().unwrap(), id);
    }
}

#[test]
fn integer_fields_reject_lossy_or_malformed_values() {
    for raw in [
        "-1",
        "1.0",
        "1e3",
        "18446744073709551616",
        "null",
        "true",
        "[]",
        "{}",
        "\"\"",
        "\"-1\"",
        "\"+1\"",
        "\" 1\"",
        "\"1 \"",
        "\"1.0\"",
        "\"1e3\"",
        "\"18446744073709551616\"",
        "\"１２\"",
    ] {
        assert!(
            serde_json::from_str::<PublishedFileId>(raw).is_err(),
            "{raw}"
        );
        assert!(serde_json::from_str::<UgcHandle>(raw).is_err(), "{raw}");
        assert!(serde_json::from_str::<SteamId>(raw).is_err(), "{raw}");
    }
    for field in ["creator", "file_size", "hcontent_file", "hcontent_preview"] {
        let mut value = fixture();
        value["response"]["publishedfiledetails"][0][field] = json!(u64::MAX);
        assert!(parse(&value, &IDS).is_ok(), "{field}");
        value["response"]["publishedfiledetails"][0][field] = json!(1.5);
        assert!(
            matches!(parse(&value, &IDS), Err(Error::Decode { .. })),
            "{field}"
        );
    }
}

#[test]
fn future_codes_fields_and_native_sentinels_are_preserved() {
    let mut value = fixture();
    value["future_envelope"] = json!({"anything": [1, null, "new"]});
    value["response"]["future_batch"] = json!({"anything": true});
    let rows = value["response"]["publishedfiledetails"]
        .as_array_mut()
        .unwrap();
    rows[0]["future_item"] = json!({"result": "unrelated"});
    rows[0]["visibility"] = json!(500);
    rows[0]["hcontent_file"] = json!("0");
    rows[0]["hcontent_preview"] = json!(u64::MAX.to_string());
    rows[0]["tags"][0]["future_tag"] = json!("extension");
    rows[2]["result"] = json!(9999);
    let files = parse(&value, &IDS).unwrap();
    let details = files[0].details.as_ref().unwrap();
    assert_eq!(details.visibility, Visibility::Unknown(500));
    assert_eq!(details.content_handle, UgcHandle(0));
    assert_eq!(details.preview_handle, UgcHandle(u64::MAX));
    assert_eq!(files[2].details, Err(ResultCode(9999)));
    assert_eq!(
        serde_json::from_value::<Vec<PublishedFile>>(serde_json::to_value(&files).unwrap())
            .unwrap(),
        files
    );
}

#[test]
fn batch_errors_do_not_need_success_fields() {
    for code in [0, 2, 9, 15, 9999, -1] {
        let response = json!({"response":{"result":code, "error":"private upstream text"}});
        let error = parse(&response, &IDS).unwrap_err();
        assert!(matches!(error, Error::Api(ResultCode(value)) if value == code));
        assert!(!format!("{error:?} {error}").contains("private upstream"));
    }
}

#[test]
fn successes_require_complete_metadata() {
    let original = fixture();
    let fields = original["response"]["publishedfiledetails"][0]
        .as_object()
        .unwrap()
        .keys();
    for field in fields {
        let mut value = original.clone();
        value["response"]["publishedfiledetails"][0]
            .as_object_mut()
            .unwrap()
            .remove(field);
        assert!(
            matches!(parse(&value, &IDS), Err(Error::Decode { .. })),
            "missing {field}"
        );
        let mut value = original.clone();
        value["response"]["publishedfiledetails"][0][field] = Value::Null;
        assert!(
            matches!(parse(&value, &IDS), Err(Error::Decode { .. })),
            "null {field}"
        );
    }
    for value in [
        json!({}),
        json!({"response":{}}),
        json!({"response":{"result":1}}),
        json!({"response":{"result":"1","resultcount":0,"publishedfiledetails":[]}}),
        json!({"response":{"result":1,"resultcount":1,"publishedfiledetails":[{"publishedfileid":"2189004162","result":1}]}}),
    ] {
        assert!(matches!(parse(&value, &IDS), Err(Error::Decode { .. })));
    }
}

#[test]
fn malformed_types_and_duplicate_fields_never_become_successes() {
    for (field, bad) in [
        ("creator_app_id", json!(-1)),
        ("consumer_app_id", json!(u64::MAX)),
        ("visibility", json!(-1)),
        ("banned", json!(2)),
        ("banned", json!("0")),
        ("time_updated", json!(i64::MAX)),
        ("time_created", json!("123")),
        ("file_url", json!("not an absolute URL")),
        ("subscriptions", json!(-1)),
        ("tags", json!(["tag"])),
        ("title", json!(123)),
    ] {
        let mut value = fixture();
        value["response"]["publishedfiledetails"][0][field] = bad;
        assert!(
            matches!(parse(&value, &IDS), Err(Error::Decode { .. })),
            "{field}"
        );
    }
    let raw = String::from_utf8(FIXTURE.to_vec()).unwrap();
    for (needle, replacement) in [
        ("\"result\": 1", "\"result\": 2, \"result\": 1"),
        (
            "\"resultcount\": 3",
            "\"resultcount\": 0, \"resultcount\": 3",
        ),
        (
            "\"hcontent_file\":",
            "\"hcontent_file\": \"1\", \"hcontent_file\":",
        ),
    ] {
        assert!(
            matches!(
                decode(raw.replacen(needle, replacement, 1).as_bytes(), &IDS),
                Err(Error::Decode { .. })
            ),
            "{needle}"
        );
    }
    for raw in [
        b"not JSON: private upstream text".as_slice(),
        b"[]",
        b"null",
        b"{}
{}",
    ] {
        let error = decode(raw, &IDS).unwrap_err();
        assert!(!format!("{error:?} {error}").contains("private upstream"));
    }
}

#[test]
fn typed_booleans_and_visibility_roundtrip() {
    for (wire, expected) in [
        (json!(0), false),
        (json!(1), true),
        (json!(false), false),
        (json!(true), true),
    ] {
        let mut value = fixture();
        value["response"]["publishedfiledetails"][0]["banned"] = wire;
        assert_eq!(
            parse(&value, &IDS).unwrap()[0]
                .details
                .as_ref()
                .unwrap()
                .banned,
            expected
        );
    }
    for (raw, expected) in [
        (0, Visibility::Public),
        (1, Visibility::FriendsOnly),
        (2, Visibility::Private),
        (3, Visibility::Unlisted),
        (u32::MAX, Visibility::Unknown(u32::MAX)),
    ] {
        assert_eq!(
            serde_json::from_value::<Visibility>(json!(raw)).unwrap(),
            expected
        );
        assert_eq!(serde_json::to_value(expected).unwrap(), json!(raw));
    }
}

#[test]
fn responses_are_correlated_to_request_order_including_repeated_ids() {
    let order = [IDS[2], IDS[0], IDS[1]];
    let files = decode(FIXTURE, &order).unwrap();
    assert_eq!(files.iter().map(|file| file.id).collect::<Vec<_>>(), order);
    assert_eq!(files[0].details, Err(ResultCode::FILE_NOT_FOUND));
    let mut value = fixture();
    let row = value["response"]["publishedfiledetails"][0].clone();
    value["response"]["publishedfiledetails"] = json!([row, row]);
    value["response"]["resultcount"] = json!(2);
    let files = parse(&value, &[IDS[0], IDS[0]]).unwrap();
    assert_eq!(files.len(), 2);
    assert_eq!(files[0], files[1]);
    assert!(matches!(
        parse(&value, &[IDS[0], IDS[1]]),
        Err(Error::MissingItem(id)) if id == IDS[1]
    ));
}

#[test]
fn missing_extra_unexpected_and_duplicate_results_fail_the_batch() {
    for mode in [
        "declared_count",
        "missing",
        "extra",
        "unexpected",
        "duplicate",
    ] {
        let mut value = fixture();
        match mode {
            "declared_count" => value["response"]["resultcount"] = json!(2),
            "missing" => {
                value["response"]["publishedfiledetails"]
                    .as_array_mut()
                    .unwrap()
                    .pop();
            }
            "extra" => {
                value["response"]["publishedfiledetails"]
                    .as_array_mut()
                    .unwrap()
                    .push(json!({"publishedfileid":"123", "result":9}));
            }
            "unexpected" => {
                value["response"]["publishedfiledetails"][0]["publishedfileid"] = json!("123")
            }
            "duplicate" => {
                value["response"]["publishedfiledetails"][1]["publishedfileid"] =
                    json!(IDS[0].0.to_string())
            }
            _ => unreachable!(),
        }
        assert!(
            matches!(
                parse(&value, &IDS),
                Err(Error::ItemCountMismatch { .. } | Error::MissingItem(_))
            ),
            "{mode}"
        );
    }
}

pub(crate) fn response(status: u16, headers: &str, body: &[u8]) -> Vec<u8> {
    let mut bytes = format!(
        "HTTP/1.1 {status} Response\r\nConnection: close\r\nContent-Length: {}\r\n{headers}\r\n",
        body.len()
    )
    .into_bytes();
    bytes.extend_from_slice(body);
    bytes
}

pub(crate) struct Server {
    endpoint: String,
    requests: mpsc::UnboundedReceiver<String>,
    count: Arc<AtomicUsize>,
    task: JoinHandle<()>,
}

impl Drop for Server {
    fn drop(&mut self) {
        self.task.abort();
    }
}

impl Server {
    pub(crate) async fn start(replies: Vec<Vec<u8>>) -> Self {
        assert!(!replies.is_empty());
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let endpoint = format!(
            "http://{}/ISteamRemoteStorage/GetPublishedFileDetails/v1/",
            listener.local_addr().unwrap()
        );
        let count = Arc::new(AtomicUsize::new(0));
        let requests_count = count.clone();
        let replies = Arc::new(replies);
        let (send, requests) = mpsc::unbounded_channel();
        let task = tokio::spawn(async move {
            let mut workers = JoinSet::new();
            loop {
                tokio::select! {
                    connection = listener.accept() => {
                        let (mut socket, _) = connection.unwrap();
                        let count = requests_count.clone();
                        let replies = replies.clone();
                        let send = send.clone();
                        workers.spawn(async move {
                            let mut bytes = Vec::new();
                            let mut buffer = [0_u8; 2048];
                            loop {
                                let read = socket.read(&mut buffer).await.unwrap();
                                if read == 0 { return; }
                                bytes.extend_from_slice(&buffer[..read]);
                                assert!(bytes.len() < 64 * 1024);
                                let Some(split) = bytes.windows(4).position(|part| part == b"\r\n\r\n") else { continue; };
                                let header = std::str::from_utf8(&bytes[..split]).unwrap();
                                let length: usize = header.lines().filter_map(|line| line.split_once(':'))
                                    .find(|(name, _)| name.eq_ignore_ascii_case("content-length"))
                                    .map(|(_, value)| value.trim().parse().unwrap()).unwrap_or(0);
                                if bytes.len() < split + 4 + length { continue; }
                                let index = count.fetch_add(1, Ordering::SeqCst);
                                send.send(String::from_utf8(bytes).unwrap()).unwrap();
                                let reply = &replies[index.min(replies.len() - 1)];
                                let _ = socket.write_all(reply).await;
                                if reply.is_empty() { std::future::pending::<()>().await; }
                                return;
                            }
                        });
                    }
                    result = workers.join_next(), if !workers.is_empty() => {
                        result.unwrap().unwrap();
                    }
                }
            }
        });
        Self {
            endpoint,
            requests,
            count,
            task,
        }
    }

    pub(crate) fn client(&self) -> Client {
        let mut client = Client::new().unwrap();
        client.endpoint = self.endpoint.clone();
        client
    }

    pub(crate) fn count(&self) -> usize {
        self.count.load(Ordering::SeqCst)
    }
}

#[tokio::test]
async fn request_uses_anonymous_indexed_form_and_exact_ids() {
    let mut server = Server::start(vec![response(200, "", FIXTURE)]).await;
    let files = server.client().published_file_details(&IDS).await.unwrap();
    assert_eq!(files.len(), 3);
    let request = server.requests.recv().await.unwrap();
    let (headers, body) = request.split_once("\r\n\r\n").unwrap();
    assert!(
        headers.starts_with("POST /ISteamRemoteStorage/GetPublishedFileDetails/v1/ HTTP/1.1\r\n")
    );
    let headers = headers.to_ascii_lowercase();
    assert!(headers.contains("content-type: application/x-www-form-urlencoded"));
    assert!(!headers.contains("authorization:"));
    assert!(!headers.contains("x-webapi-key:"));
    let url = Url::parse(&format!("http://localhost/?{body}")).unwrap();
    let params: HashMap<_, _> = url.query_pairs().into_owned().collect();
    assert_eq!(params.len(), 4);
    assert_eq!(params["itemcount"], "3");
    for (index, id) in IDS.iter().enumerate() {
        assert_eq!(
            params[&format!("publishedfileids[{index}]")],
            id.to_string()
        );
    }
    assert_eq!(server.count(), 1);
}

#[tokio::test]
async fn explicit_proxy_receives_https_tunnels_and_preserves_safe_errors() {
    const SECRET: &str = "private-proxy-password";
    let mut proxy = Server::start(vec![response(407, "", SECRET.as_bytes())]).await;
    let mut url = Url::parse(&proxy.endpoint).unwrap();
    url.set_username("user").unwrap();
    url.set_password(Some(SECRET)).unwrap();
    let mut client = Client::with_proxy(Some(url.as_str())).unwrap();
    client.endpoint =
        "https://steam.invalid/ISteamRemoteStorage/GetPublishedFileDetails/v1/".into();
    let error = client.published_file_details(&IDS).await.unwrap_err();
    assert!(matches!(error, Error::Transport(_)));
    assert_eq!(proxy.count(), MAX_ATTEMPTS);
    for _ in 0..MAX_ATTEMPTS {
        let request = proxy.requests.recv().await.unwrap();
        assert!(request.starts_with("CONNECT steam.invalid:443 HTTP/1.1\r\n"));
        assert!(
            request
                .to_ascii_lowercase()
                .contains("proxy-authorization: basic ")
        );
    }
    let mut source: Option<&dyn std::error::Error> = Some(&error);
    while let Some(error) = source {
        let text = format!("{error} {error:?}");
        assert!(!text.contains(SECRET));
        assert!(!text.contains("steam.invalid"));
        source = error.source();
    }
}

#[test]
fn invalid_proxy_configuration_does_not_expose_credentials() {
    let error =
        Client::with_proxy(Some("http://user:private-proxy-password@[invalid]")).unwrap_err();
    assert!(matches!(
        error,
        Error::Transport(TransportError::Configuration)
    ));
    assert!(!format!("{error} {error:?}").contains("private-proxy-password"));
}

#[tokio::test]
async fn invalid_batches_are_rejected_before_network_io() {
    let server = Server::start(vec![response(200, "", FIXTURE)]).await;
    let client = server.client();
    assert!(matches!(
        client.published_file_details(&[]).await,
        Err(Error::EmptyBatch)
    ));
    assert!(matches!(
        client.published_file_details(&[PublishedFileId(0)]).await,
        Err(Error::InvalidPublishedFileId(PublishedFileId(0)))
    ));
    assert!(matches!(
        client
            .published_file_details(&[IDS[0]; MAX_PUBLISHED_FILES + 1])
            .await,
        Err(Error::BatchTooLarge { .. })
    ));
    assert_eq!(server.count(), 0);
}

#[tokio::test]
async fn the_maximum_batch_is_accepted_and_encoded_completely() {
    let mut value = fixture();
    let row = value["response"]["publishedfiledetails"][0].clone();
    value["response"]["publishedfiledetails"] = json!(vec![row; MAX_PUBLISHED_FILES]);
    value["response"]["resultcount"] = json!(MAX_PUBLISHED_FILES);
    let mut server = Server::start(vec![response(
        200,
        "",
        &serde_json::to_vec(&value).unwrap(),
    )])
    .await;
    let files = server
        .client()
        .published_file_details(&[IDS[0]; MAX_PUBLISHED_FILES])
        .await
        .unwrap();
    assert_eq!(files.len(), MAX_PUBLISHED_FILES);
    assert!(
        files
            .iter()
            .all(|file| file.id == IDS[0] && file.details.is_ok())
    );
    let request = server.requests.recv().await.unwrap();
    let body = request.split_once("\r\n\r\n").unwrap().1;
    let url = Url::parse(&format!("http://localhost/?{body}")).unwrap();
    let params: HashMap<_, _> = url.query_pairs().into_owned().collect();
    assert_eq!(params["itemcount"], "100");
    assert_eq!(params["publishedfileids[99]"], IDS[0].to_string());
    assert_eq!(params.len(), 101);
}

#[tokio::test]
async fn read_only_post_retries_temporary_http_failures_with_the_same_form() {
    let mut server = Server::start(vec![
        response(503, "Retry-After: 0\r\n", b"upstream private text"),
        response(429, "Retry-After: 0\r\n", b"rate limited"),
        response(200, "", FIXTURE),
    ])
    .await;
    assert!(server.client().published_file_details(&IDS).await.is_ok());
    assert_eq!(server.count(), 3);
    let first = server.requests.recv().await.unwrap();
    for _ in 0..2 {
        let next = server.requests.recv().await.unwrap();
        assert_eq!(
            next.split_once("\r\n\r\n").unwrap().1,
            first.split_once("\r\n\r\n").unwrap().1
        );
    }
}

#[tokio::test]
async fn repeated_temporary_failures_exhaust_the_attempt_budget() {
    let server = Server::start(vec![response(
        503,
        "Retry-After: 0\r\n",
        b"private upstream text",
    )])
    .await;
    let error = server
        .client()
        .published_file_details(&IDS)
        .await
        .unwrap_err();
    assert!(matches!(
        error,
        Error::Http {
            status: StatusCode::SERVICE_UNAVAILABLE,
            ..
        }
    ));
    assert_eq!(server.count(), MAX_ATTEMPTS);
    assert!(!format!("{error} {error:?}").contains("private upstream"));
}

#[tokio::test]
async fn permanent_failures_and_redirects_are_not_retried_or_followed() {
    for status in [302, 400, 401, 403, 404] {
        let server = Server::start(vec![response(
            status,
            "Location: http://127.0.0.1:1/leak\r\n",
            b"private upstream text",
        )])
        .await;
        let error = server
            .client()
            .published_file_details(&IDS)
            .await
            .unwrap_err();
        assert!(matches!(error, Error::Http { status: actual, .. } if actual.as_u16() == status));
        assert_eq!(server.count(), 1);
        assert!(!format!("{error:?} {error}").contains("private upstream"));
        assert!(!format!("{error:?} {error}").contains("/leak"));
    }
}

#[tokio::test]
async fn malformed_responses_and_steam_failures_are_not_retried() {
    for body in [
        b"private upstream text".as_slice(),
        br#"{"response":{"result":15}}"#,
        br#"{"response":{}}"#,
    ] {
        let server = Server::start(vec![response(200, "", body)]).await;
        assert!(server.client().published_file_details(&IDS).await.is_err());
        assert_eq!(server.count(), 1);
    }
    let server = Server::start(vec![response(200, "", FIXTURE)]).await;
    assert!(
        server.client().published_file_details(&IDS).await.unwrap()[2]
            .details
            .is_err()
    );
    assert_eq!(server.count(), 1);
}

#[tokio::test]
async fn retry_after_beyond_the_deadline_returns_without_early_replay() {
    let future = Utc::now() + chrono::Duration::hours(1);
    for after in [
        "60".to_owned(),
        u64::MAX.to_string(),
        future.format("%a, %d %b %Y %H:%M:%S GMT").to_string(),
        future.format("%A, %d-%b-%y %H:%M:%S GMT").to_string(),
        future.format("%a %b %e %H:%M:%S %Y").to_string(),
    ] {
        let server = Server::start(vec![response(
            429,
            &format!("Retry-After: {after}\r\n"),
            b"",
        )])
        .await;
        let error = server
            .client()
            .published_file_details(&IDS)
            .await
            .unwrap_err();
        assert!(
            matches!(error, Error::Http { retry_after: Some(delay), .. } if delay >= OPERATION_TIMEOUT)
        );
        assert_eq!(server.count(), 1);
    }
}

#[tokio::test]
async fn expired_retry_after_dates_retry_without_delay() {
    for after in [
        "Sun, 06 Nov 1994 08:49:37 GMT",
        "Sunday, 06-Nov-94 08:49:37 GMT",
        "Sun Nov  6 08:49:37 1994",
    ] {
        let server = Server::start(vec![response(
            503,
            &format!("Retry-After: {after}\r\n"),
            b"",
        )])
        .await;
        let error = server
            .client()
            .published_file_details(&IDS)
            .await
            .unwrap_err();
        assert!(
            matches!(
                error,
                Error::Http {
                    retry_after: Some(Duration::ZERO),
                    ..
                }
            ),
            "{after}: {error:?}"
        );
        assert_eq!(server.count(), MAX_ATTEMPTS);
    }
}

#[tokio::test]
async fn retry_after_delays_the_next_attempt() {
    let server = Server::start(vec![
        response(429, "Retry-After: 1\r\n", b""),
        response(200, "", FIXTURE),
    ])
    .await;
    let started = Instant::now();
    assert!(server.client().published_file_details(&IDS).await.is_ok());
    assert!(started.elapsed() >= Duration::from_secs(1));
    assert_eq!(server.count(), 2);
}

#[tokio::test]
async fn truncated_bodies_retry_without_accepting_partial_metadata() {
    let mut truncated = response(200, "", FIXTURE);
    truncated.truncate(truncated.len() - 32);
    let server = Server::start(vec![truncated.clone(), response(200, "", FIXTURE)]).await;
    server.client().published_file_details(&IDS).await.unwrap();
    assert_eq!(server.count(), 2);

    let server = Server::start(vec![truncated]).await;
    assert!(matches!(
        server.client().published_file_details(&IDS).await,
        Err(Error::Transport(TransportError::ResponseBody))
    ));
    assert_eq!(server.count(), MAX_ATTEMPTS);
}

#[tokio::test]
async fn chunked_responses_preserve_split_utf8_characters() {
    let body = serde_json::to_vec(&fixture()).unwrap();
    let mut reply =
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n".to_vec();
    for chunk in body.chunks(3) {
        reply.extend_from_slice(format!("{:x}\r\n", chunk.len()).as_bytes());
        reply.extend_from_slice(chunk);
        reply.extend_from_slice(b"\r\n");
    }
    reply.extend_from_slice(b"0\r\n\r\n");
    let server = Server::start(vec![reply]).await;
    assert_eq!(
        server.client().published_file_details(&IDS).await.unwrap(),
        decode(FIXTURE, &IDS).unwrap()
    );
}

#[tokio::test]
async fn declared_and_streamed_oversize_bodies_are_rejected_without_retry() {
    use std::io::Write;

    let declared = format!(
        "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
        MAX_RESPONSE_BYTES + 1
    )
    .into_bytes();
    let mut chunked =
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n".to_vec();
    for length in [MAX_RESPONSE_BYTES, 1] {
        chunked.extend_from_slice(format!("{length:x}\r\n").as_bytes());
        chunked.resize(chunked.len() + length, b' ');
        chunked.extend_from_slice(b"\r\n");
    }
    chunked.extend_from_slice(b"0\r\n\r\n");
    let mut encoder = flate2::write::GzEncoder::new(Vec::new(), flate2::Compression::fast());
    encoder
        .write_all(&vec![b' '; MAX_RESPONSE_BYTES + 1])
        .unwrap();
    let compressed = response(
        200,
        "Content-Encoding: gzip\r\n",
        &encoder.finish().unwrap(),
    );
    for reply in [declared, chunked, compressed] {
        let server = Server::start(vec![reply]).await;
        assert!(matches!(
            server.client().published_file_details(&IDS).await,
            Err(Error::ResponseTooLarge)
        ));
        assert_eq!(server.count(), 1);
    }
}

#[tokio::test]
async fn the_exact_response_size_limit_is_accepted() {
    let mut body = FIXTURE.to_vec();
    body.resize(MAX_RESPONSE_BYTES, b' ');
    let server = Server::start(vec![response(200, "", &body)]).await;
    assert!(server.client().published_file_details(&IDS).await.is_ok());
}

#[tokio::test]
async fn cancelling_a_read_stops_its_retry_work() {
    let mut server = Server::start(vec![vec![], response(200, "", FIXTURE)]).await;
    let client = server.client();
    let running = client.clone();
    let task = tokio::spawn(async move { running.published_file_details(&IDS).await });
    server.requests.recv().await.unwrap();
    task.abort();
    assert!(task.await.unwrap_err().is_cancelled());
    assert!(client.published_file_details(&IDS).await.is_ok());
    assert_eq!(server.count(), 2);
}

#[tokio::test]
async fn the_overall_deadline_bounds_a_stalled_request() {
    let mut server = Server::start(vec![vec![]]).await;
    let client = server.client();
    let task = tokio::spawn(async move { client.published_file_details(&IDS).await });
    server.requests.recv().await.unwrap();
    tokio::time::pause();
    tokio::time::advance(OPERATION_TIMEOUT + Duration::from_secs(1)).await;
    assert!(matches!(task.await.unwrap(), Err(Error::Timeout)));
    assert_eq!(server.count(), 1);
}

#[tokio::test]
async fn http2_errors_discard_peer_text_from_every_error_output() {
    const MARKER: &str = "private-upstream-debug-text";
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let endpoint = format!("http://{}/", listener.local_addr().unwrap());
    let server = tokio::spawn(async move {
        for _ in 0..=MAX_ATTEMPTS {
            let (mut socket, _) = listener.accept().await.unwrap();
            let mut preface = [0; 24];
            socket.read_exact(&mut preface).await.unwrap();
            assert_eq!(&preface, b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n");
            socket
                .write_all(&[0, 0, 0, 4, 0, 0, 0, 0, 0])
                .await
                .unwrap();
            loop {
                let mut header = [0; 9];
                socket.read_exact(&mut header).await.unwrap();
                let length = u32::from_be_bytes([0, header[0], header[1], header[2]]) as usize;
                let mut payload = vec![0; length];
                socket.read_exact(&mut payload).await.unwrap();
                if header[3] == 4 && header[4] & 1 == 0 {
                    socket
                        .write_all(&[0, 0, 0, 4, 1, 0, 0, 0, 0])
                        .await
                        .unwrap();
                }
                if header[3] == 1 {
                    // Send GOAWAY with debug data once the request HEADERS arrive.
                    let mut goaway = vec![0, 0, (8 + MARKER.len()) as u8, 7, 0, 0, 0, 0, 0];
                    goaway.extend_from_slice(&0_u32.to_be_bytes());
                    goaway.extend_from_slice(&2_u32.to_be_bytes());
                    goaway.extend_from_slice(MARKER.as_bytes());
                    socket.write_all(&goaway).await.unwrap();
                    let mut remainder = [0; 4096];
                    while socket.read(&mut remainder).await.unwrap_or(0) != 0 {}
                    break;
                }
            }
        }
    });
    let client = Client {
        http: reqwest::Client::builder()
            .http2_prior_knowledge()
            .timeout(REQUEST_TIMEOUT)
            .retry(reqwest::retry::never())
            .build()
            .unwrap(),
        endpoint,
    };
    let raw = client.http.post(&client.endpoint).send().await.unwrap_err();
    assert!(format!("{raw:?}").contains(MARKER));
    let error = client.published_file_details(&IDS).await.unwrap_err();
    assert!(matches!(error, Error::Transport(TransportError::Request)));
    tokio::time::timeout(REQUEST_TIMEOUT, server)
        .await
        .unwrap()
        .unwrap();
    let mut source: Option<&dyn std::error::Error> = Some(&error);
    while let Some(error) = source {
        assert!(!format!("{error} {error:?}").contains(MARKER));
        source = error.source();
    }
}

#[tokio::test(start_paused = true)]
async fn invalid_request_configuration_fails_without_retry() {
    let server = Server::start(vec![response(200, "", FIXTURE)]).await;
    let mut client = server.client();
    client.endpoint = "http://[invalid-request-config]".into();
    let started = Instant::now();
    assert!(matches!(
        client.published_file_details(&IDS).await,
        Err(Error::Transport(TransportError::Configuration))
    ));
    assert_eq!(started.elapsed(), Duration::ZERO);
    assert_eq!(server.count(), 0);
}

#[tokio::test]
async fn connection_failures_keep_their_category_without_request_details() {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let mut client = Client::new().unwrap();
    client.endpoint = format!(
        "http://{}/private-request-details",
        listener.local_addr().unwrap()
    );
    drop(listener);
    let error = client.published_file_details(&IDS).await.unwrap_err();
    assert!(matches!(
        error,
        Error::Transport(TransportError::Connection)
    ));
    assert!(!format!("{error} {error:?}").contains("private-request-details"));
}
