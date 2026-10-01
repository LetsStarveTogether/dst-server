use std::{
    future::Future,
    sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    },
    time::Duration,
};

use chrono::NaiveDate;
use dst_server::{
    external::{
        KleiClient, KleiConfig, KleiEndpoints, Platform, Region, Room, RoomQuery, VersionType,
        parse_players, parse_versions,
    },
    model::{ErrorCode, Outcome},
};
use serde_json::{Value, json};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::TcpListener,
    sync::{Semaphore, oneshot, watch},
    task::{JoinHandle, JoinSet},
};

const VERSION_HTML: &str = r#"
<h1>Don't Starve Together</h1>
<li class="cCmsRecord_row" data-rowID="2754">
  <a href="https://example.test/736959" class="cRelease"
     data-releaseID="2754" data-currentRelease>
    <span class="cUpdate_hotfix"></span>
    <h3 class="ipsType_sectionHead">
      736959 <span class="ipsBadge">Release</span>
    </h3>
    <div class="ipsDataItem_meta">Released 06/11/26</div>
  </a>
</li>
<ul class="ipsPagination"><li>Page 1 of 35</li></ul>
"#;

fn lobby_row(id: &str) -> Value {
    json!({"__rowId": id, "__addr": "127.0.0.1", "name": "DST cluster", "port": 10999,
        "host": "KU_HOST", "connected": 3, "maxconnections": 6, "v": 736959,
        "allownewplayers": true, "clanonly": false, "clienthosted": false, "dedicated": true,
        "fo": false, "lanonly": false, "mods": true, "password": false, "pvp": false,
        "serverpaused": false, "platform": 1, "session": "session-id", "guid": "guid",
        "intent": "social", "steamroom": "steam-room", "season": "dry",
        "future_field": {"integer": 9_007_199_254_740_993_i64, "value": null}})
}

fn room_row(id: &str) -> Value {
    let mut row = lobby_row(id);
    row.as_object_mut().unwrap().extend(json!({"tick": 12345, "clientmodsoff": false, "nat": 1,
        "mods_info": [["workshop-1", {"name":"Example mod"}], null],
        "players": "return-- players\n{{name='Mod Hero',netid='76561198000000002',prefab='workshop-123-character',colour='F02D0EFF',eventlevel=1}}"
    }).as_object().unwrap().clone());
    row
}

struct Request {
    method: String,
    path: String,
    headers: String,
    body: Vec<u8>,
}

struct Response {
    status: u16,
    headers: Vec<(String, String)>,
    body: Vec<u8>,
    chunked: bool,
    body_delay: Duration,
}

impl Response {
    fn json(value: Value) -> Self {
        Self {
            status: 200,
            headers: vec![],
            body: serde_json::to_vec(&value).unwrap(),
            chunked: false,
            body_delay: Duration::ZERO,
        }
    }
}

struct Server {
    base: String,
    task: JoinHandle<()>,
}

impl Drop for Server {
    fn drop(&mut self) {
        self.task.abort();
    }
}

impl Server {
    async fn start<F, Fut>(handler: F) -> anyhow::Result<Self>
    where
        F: Fn(Request) -> Fut + Send + Sync + 'static,
        Fut: Future<Output = Response> + Send + 'static,
    {
        let listener = TcpListener::bind("127.0.0.1:0").await?;
        let base = format!("http://{}", listener.local_addr()?);
        let handler = Arc::new(handler);
        let task = tokio::spawn(async move {
            let mut workers = JoinSet::new();
            loop {
                tokio::select! {
                    connection = listener.accept() => {
                        let (mut socket, _) = connection.unwrap();
                        let handler = handler.clone();
                        workers.spawn(async move {
                            let mut data = Vec::new();
                            let split = loop {
                                let mut chunk = [0; 1024];
                                let count = socket.read(&mut chunk).await?;
                                if count == 0 { return Ok::<_, std::io::Error>(()); }
                                data.extend_from_slice(&chunk[..count]);
                                if let Some(index) = data.windows(4).position(|bytes| bytes == b"\r\n\r\n") { break index + 4; }
                                assert!(data.len() < 64 * 1024);
                            };
                            let headers = String::from_utf8(data[..split].to_vec()).unwrap();
                            let first = headers.lines().next().unwrap().split_whitespace().collect::<Vec<_>>();
                            let method = first[0].to_owned();
                            let path = first[1].to_owned();
                            let length: usize = headers.lines().filter_map(|line| line.split_once(':'))
                                .find(|(name, _)| name.eq_ignore_ascii_case("content-length"))
                                .map_or(0, |(_, length)| length.trim().parse().unwrap());
                            while data.len() - split < length {
                                let mut chunk = [0; 1024];
                                let count = socket.read(&mut chunk).await?;
                                if count == 0 { return Ok(()); }
                                data.extend_from_slice(&chunk[..count]);
                            }
                            let response = handler(Request { method, path, headers, body: data[split..split + length].to_vec() }).await;
                            let mut headers = format!("HTTP/1.1 {} Response\r\nConnection: close\r\n", response.status);
                            for (key, value) in response.headers { headers.push_str(&format!("{key}: {value}\r\n")); }
                            headers.push_str(&if response.chunked { "Transfer-Encoding: chunked\r\n\r\n".into() } else { format!("Content-Length: {}\r\n\r\n", response.body.len()) });
                            socket.write_all(headers.as_bytes()).await?;
                            tokio::time::sleep(response.body_delay).await;
                            if response.chunked {
                                for chunk in response.body.chunks(7) {
                                    socket.write_all(format!("{:x}\r\n", chunk.len()).as_bytes()).await?;
                                    socket.write_all(chunk).await?;
                                    socket.write_all(b"\r\n").await?;
                                }
                                socket.write_all(b"0\r\n\r\n").await?;
                            } else { socket.write_all(&response.body).await?; }
                            socket.shutdown().await?;
                            Ok(())
                        });
                    }
                    result = workers.join_next(), if !workers.is_empty() => {
                        if let Some(Err(error)) = result { panic!("mock HTTP handler failed: {error}"); }
                    }
                }
            }
        });
        Ok(Self { base, task })
    }

    fn config(&self) -> KleiConfig {
        KleiConfig {
            access_token: Some("test-credential".into()),
            endpoints: KleiEndpoints {
                builds: format!("{}/builds", self.base),
                versions: format!("{}/versions", self.base),
                regions: format!("{}/regions", self.base),
                lobby: format!("{}/{{region}}-{{platform}}", self.base),
                room: format!("{}/{{region}}/room", self.base),
            },
            ..Default::default()
        }
    }
}

#[test]
fn versions_and_lua_players_preserve_source_fields() -> anyhow::Result<()> {
    let html = VERSION_HTML
        .replace("736959", "736960")
        .replace("06/11/26", "06/10/26")
        + &VERSION_HTML.replace("736959", "736958")
        + VERSION_HTML;
    let versions = parse_versions(&html)?;
    assert_eq!(
        versions
            .iter()
            .map(|version| version.number)
            .collect::<Vec<_>>(),
        [736959, 736958, 736960]
    );
    assert_eq!(versions[0].kind, VersionType::Release);
    assert_eq!(
        versions[0].date,
        NaiveDate::from_ymd_opt(2026, 6, 11).unwrap()
    );
    assert_eq!(
        (versions[0].row_id, versions[0].release_id),
        (Some(2754), Some(2754))
    );
    assert!(versions[0].is_hotfix && versions[0].is_current_release);
    for html in [
        "<li class='cCmsRecord_row'>broken</li>".into(),
        VERSION_HTML.replace("06/11/26", "02/29/26"),
        VERSION_HTML.replace("Release</span>", "unknown</span>"),
    ] {
        assert_eq!(parse_versions(&html).unwrap_err().code, ErrorCode::Protocol);
    }
    for players in [
        Value::Null,
        json!(""),
        json!(" \n"),
        json!("{}"),
        json!("return{}"),
        json!("return--comment\n{}"),
        json!([]),
    ] {
        assert!(parse_players(players)?.is_empty());
    }
    for source in [
        "returning {}",
        "return require('untrusted')",
        "return {{name=os.execute('untrusted')}}",
        "return {},{}",
        "return {}; print('untrusted')",
        "return {name='Wilson'}",
        "return {[1]={},[3]={}}",
        "return {[1]={},named={}}",
        "return {[1]={},[1]={}}",
    ] {
        assert_eq!(
            parse_players(json!(source)).unwrap_err().code,
            ErrorCode::Protocol,
            "{source}"
        );
    }
    let mut row = room_row("row-1");
    row["region"] = json!("us-east-1");
    let room: Room = serde_json::from_value(row)?;
    assert_eq!(room.players[0].prefab, "workshop-123-character");
    assert_eq!(room.players[0].netid, "76561198000000002");
    assert_eq!(
        room.lobby.extra["future_field"]["integer"],
        json!(9_007_199_254_740_993_i64)
    );
    assert!(!room.lobby.extra.contains_key("players"));
    assert_eq!(serde_json::to_value(room)?["tick"], 12345);
    Ok(())
}

#[tokio::test]
async fn endpoints_preserve_strict_data_and_successful_empty_results() -> anyhow::Result<()> {
    let server = Server::start(|request| async move {
        match request.path.as_str() {
            "/builds" => Response::json(json!({"release": [736958, "736959"]})),
            "/versions" => Response {
                body: VERSION_HTML.as_bytes().to_vec(),
                ..Response::json(Value::Null)
            },
            "/regions" => Response::json(
                json!({"LobbyRegions": [{"Region":"us-east-1"}, {"Region":"eu-central-1"}]}),
            ),
            "/us-east-1-Steam" => Response::json(json!({"GET": [lobby_row("row-1")]})),
            "/us-east-1/room" => {
                assert_eq!(request.method, "POST");
                assert!(
                    request
                        .headers
                        .to_ascii_lowercase()
                        .contains("content-type: application/json")
                );
                let payload: Value = serde_json::from_slice(&request.body).unwrap();
                assert_eq!(payload["__gameId"], "DontStarveTogether");
                assert_eq!(payload["__token"], "test-credential");
                let rows = if payload["query"]["__rowId"] == "missing" {
                    vec![]
                } else {
                    vec![room_row("row-1")]
                };
                Response::json(json!({"GET": rows}))
            }
            _ => Response::json(json!({"GET": []})),
        }
    })
    .await?;
    let client = KleiClient::new(server.config())?;
    assert_eq!(client.get_latest_build("release").await?, 736959);
    assert_eq!(
        client.get_latest_build("missing").await.unwrap_err().code,
        ErrorCode::Protocol
    );
    assert_eq!(client.get_versions().await?[0].number, 736959);
    assert_eq!(client.get_regions().await?, ["us-east-1", "eu-central-1"]);
    let lobby = client
        .lobby(Region::UsEast, Platform::Steam)
        .await?
        .remove(0);
    assert_eq!(lobby.region, Region::UsEast);
    assert_eq!(lobby.connect_code(), "c_connect('127.0.0.1', 10999)");
    let room = client.room("row-1", Region::UsEast).await?.unwrap();
    assert_eq!(room.players[0].prefab, "workshop-123-character");
    assert_eq!(room.mods_info.unwrap()[1], Value::Null);
    assert!(client.room("missing", Region::UsEast).await?.is_none());
    assert!(
        client
            .lobby(Region::ApEast, Platform::Steam)
            .await?
            .is_empty()
    );
    Ok(())
}

#[tokio::test]
async fn failed_and_redirected_queries_never_become_empty_successes() -> anyhow::Result<()> {
    let count = Arc::new(AtomicUsize::new(0));
    let server = Server::start({
        let count = count.clone();
        move |request| {
            count.fetch_add(1, Ordering::SeqCst);
            async move {
                let mut response = Response::json(json!({"GET": []}));
                if request.path.contains("room") {
                    response.status = 307;
                    response
                        .headers
                        .push(("Location".into(), "/stolen-token".into()));
                } else {
                    response.status = 503;
                }
                response
            }
        }
    })
    .await?;
    let client = KleiClient::new(server.config())?;
    let error = client.room("row", Region::UsEast).await.unwrap_err();
    assert_eq!(error.code, ErrorCode::Transport);
    assert_eq!(error.details["status"], 307);
    assert!(!error.message.contains("test-credential"));
    let error = client
        .lobby(Region::UsEast, Platform::Steam)
        .await
        .unwrap_err();
    assert_eq!(error.details["status"], 503);
    assert_eq!(count.load(Ordering::SeqCst), 2);
    assert!(
        "us-east-1.klei.com.attacker.invalid/path"
            .parse::<Region>()
            .is_err()
    );
    assert!("moon-base-1".parse::<Region>().is_err());
    let mut config = server.config();
    config.access_token = None;
    let client = KleiClient::new(config)?;
    assert_eq!(
        client.get_rooms(Vec::new()).await.unwrap_err().code,
        ErrorCode::Invalid
    );
    assert_eq!(count.load(Ordering::SeqCst), 2);
    Ok(())
}

#[tokio::test]
async fn body_limits_cover_chunking_gzip_and_time_after_headers() -> anyhow::Result<()> {
    const COMPRESSED: &[u8] = &[
        31, 139, 8, 0, 0, 0, 0, 0, 2, 255, 171, 86, 114, 119, 13, 81, 178, 138, 142, 213, 81, 42,
        72, 76, 73, 201, 204, 75, 87, 178, 82, 170, 24, 5, 163, 96, 20, 140, 88, 160, 84, 11, 0, 5,
        190, 154, 236, 23, 4, 0, 0,
    ];
    for mode in ["length", "chunked", "gzip", "timeout"] {
        let server = Server::start(move |_| async move {
            let mut response = Response::json(json!({"GET": [], "padding": "x".repeat(1024)}));
            if mode == "gzip" {
                response.body = COMPRESSED.to_vec();
                response
                    .headers
                    .push(("Content-Encoding".into(), "gzip".into()));
            }
            response.chunked = mode != "length";
            if mode == "timeout" {
                response.body = b"{\"GET\":[]}".to_vec();
                response.body_delay = Duration::from_millis(200);
            }
            response
        })
        .await?;
        let mut config = server.config();
        config.max_response_bytes = 64;
        config.request_timeout = Duration::from_millis(50);
        let client = KleiClient::new(config)?;
        let error = client
            .lobby(Region::UsEast, Platform::Steam)
            .await
            .unwrap_err();
        assert_eq!(
            error.code,
            if mode == "timeout" {
                ErrorCode::Timeout
            } else {
                ErrorCode::Overflow
            },
            "{mode}: {error}"
        );
    }
    Ok(())
}

#[tokio::test]
async fn room_batches_bound_pending_work_and_keep_order_with_partial_failures() -> anyhow::Result<()>
{
    let release = Arc::new(Semaphore::new(0));
    let active = Arc::new(AtomicUsize::new(0));
    let maximum = Arc::new(AtomicUsize::new(0));
    let consumed = Arc::new(AtomicUsize::new(0));
    let (ready_tx, ready_rx) = oneshot::channel();
    let ready = Arc::new(std::sync::Mutex::new(Some(ready_tx)));
    let (last_tx, last_rx) = watch::channel(false);
    let server = Server::start({
        let release = release.clone();
        let active = active.clone();
        let maximum = maximum.clone();
        move |request| {
            let release = release.clone();
            let active = active.clone();
            let maximum = maximum.clone();
            let ready = ready.clone();
            let last_tx = last_tx.clone();
            let mut last_rx = last_rx.clone();
            async move {
                let now = active.fetch_add(1, Ordering::SeqCst) + 1;
                maximum.fetch_max(now, Ordering::SeqCst);
                if now == 3
                    && let Some(ready) = ready.lock().unwrap().take()
                {
                    let _ = ready.send(());
                }
                let _permit = release.acquire().await.unwrap();
                let payload: Value = serde_json::from_slice(&request.body).unwrap();
                let id = payload["query"]["__rowId"].as_str().unwrap();
                if id == "0" {
                    last_rx.wait_for(|last| *last).await.unwrap();
                }
                if id == "19" {
                    last_tx.send_replace(true);
                }
                let mut response = Response::json(json!({"GET": [room_row(id)]}));
                if id == "5" {
                    response.status = 503;
                }
                if id == "17" {
                    let mut row = room_row(id);
                    row["tick"] = json!("invalid");
                    response = Response::json(json!({"GET": [row]}));
                }
                if id == "18" {
                    response = Response::json(json!({"GET": []}));
                }
                active.fetch_sub(1, Ordering::SeqCst);
                response
            }
        }
    })
    .await?;
    let mut config = server.config();
    config.room_concurrency = 3;
    let client = Arc::new(KleiClient::new(config)?);
    let task = tokio::spawn({
        let client = client.clone();
        let consumed = consumed.clone();
        async move {
            client
                .get_rooms((0..20).map(move |index| {
                    consumed.fetch_add(1, Ordering::SeqCst);
                    RoomQuery {
                        row_id: index.to_string(),
                        region: Region::UsEast,
                    }
                }))
                .await
        }
    });
    tokio::time::timeout(Duration::from_secs(2), ready_rx).await??;
    assert_eq!(consumed.load(Ordering::SeqCst), 3);
    release.add_permits(3);
    let results = tokio::time::timeout(Duration::from_secs(5), task).await???;
    assert_eq!(maximum.load(Ordering::SeqCst), 3);
    assert_eq!(
        results
            .iter()
            .map(|result| result.query.row_id.clone())
            .collect::<Vec<_>>(),
        (0..20).map(|index| index.to_string()).collect::<Vec<_>>()
    );
    for (index, result) in results.into_iter().enumerate() {
        match (index, result.result) {
            (5, Outcome::Failure { error }) => assert_eq!(error.code, ErrorCode::Transport),
            (17, Outcome::Failure { error }) => assert_eq!(error.code, ErrorCode::Protocol),
            (18, Outcome::Success { value: None }) => {}
            (index, Outcome::Success { value: Some(room) }) => {
                assert_eq!(room.lobby.row_id, index.to_string())
            }
            (index, value) => panic!("unexpected result {index}: {value:?}"),
        }
    }
    Ok(())
}

#[tokio::test]
async fn discovery_preserves_unavailable_lobbies() -> anyhow::Result<()> {
    let server = Server::start(|request| async move {
        if request.path == "/us-east-1-Steam" {
            Response::json(json!({"GET":[lobby_row("row-1")]}))
        } else if request.path.ends_with("/room") {
            Response::json(json!({"GET":[room_row("row-1")]}))
        } else {
            Response {
                status: 503,
                ..Response::json(Value::Null)
            }
        }
    })
    .await?;
    let client = KleiClient::new(server.config())?;
    let result = client.discover_rooms().await?;
    assert_eq!(result.lobbies.len(), 20);
    assert_eq!(
        result
            .lobbies
            .iter()
            .filter(|item| matches!(item.result, Outcome::Failure { .. }))
            .count(),
        19
    );
    assert_eq!(result.rooms.len(), 1);
    assert!(
        matches!(&result.rooms[0].result, Outcome::Success { value: Some(room) } if room.lobby.row_id == "row-1")
    );
    Ok(())
}

#[tokio::test]
async fn cancelling_a_batch_stops_consuming_its_input() -> anyhow::Result<()> {
    let release = Arc::new(Semaphore::new(0));
    let consumed = Arc::new(AtomicUsize::new(0));
    let (received, mut arrivals) = watch::channel(0);
    let server = Server::start({
        let release = release.clone();
        move |_| {
            let release = release.clone();
            let received = received.clone();
            async move {
                received.send_modify(|count| *count += 1);
                let _permit = release.acquire().await.unwrap();
                Response::json(json!({"GET": []}))
            }
        }
    })
    .await?;
    let mut config = server.config();
    config.room_concurrency = 3;
    let client = KleiClient::new(config)?;
    let task = tokio::spawn({
        let consumed = consumed.clone();
        async move {
            client
                .get_rooms((0..200).map(move |index| {
                    consumed.fetch_add(1, Ordering::SeqCst);
                    RoomQuery {
                        row_id: index.to_string(),
                        region: Region::UsEast,
                    }
                }))
                .await
        }
    });
    tokio::time::timeout(
        Duration::from_secs(2),
        arrivals.wait_for(|count| *count == 3),
    )
    .await??;
    assert_eq!(consumed.load(Ordering::SeqCst), 3);
    task.abort();
    assert!(task.await.unwrap_err().is_cancelled());
    release.add_permits(3);
    tokio::time::sleep(Duration::from_millis(50)).await;
    assert_eq!(consumed.load(Ordering::SeqCst), 3);
    assert_eq!(*arrivals.borrow(), 3);
    Ok(())
}

#[tokio::test]
async fn ambient_proxy_is_ignored_in_an_isolated_process() -> anyhow::Result<()> {
    const MARKER: &str = "DST_KLEI_PROXY_TEST_ENDPOINT";
    if let Ok(endpoint) = std::env::var(MARKER) {
        let mut config = KleiConfig::default();
        config.endpoints.builds = endpoint;
        assert_eq!(
            KleiClient::new(config)?.get_latest_build("release").await?,
            736959
        );
        return Ok(());
    }
    let server = Server::start(|_| async { Response::json(json!({"release": [736959]})) }).await?;
    let output = tokio::process::Command::new(std::env::current_exe()?)
        .args([
            "--exact",
            "ambient_proxy_is_ignored_in_an_isolated_process",
            "--nocapture",
        ])
        .env(MARKER, format!("{}/builds", server.base))
        .env("HTTP_PROXY", "http://127.0.0.1:1")
        .env("http_proxy", "http://127.0.0.1:1")
        .env("HTTPS_PROXY", "http://127.0.0.1:1")
        .env("https_proxy", "http://127.0.0.1:1")
        .env("ALL_PROXY", "http://127.0.0.1:1")
        .env("all_proxy", "http://127.0.0.1:1")
        .env("NO_PROXY", "")
        .env("no_proxy", "")
        .kill_on_drop(true)
        .output()
        .await?;
    assert!(
        output.status.success(),
        "{}{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    Ok(())
}
