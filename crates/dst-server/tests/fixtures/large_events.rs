use dst_server::{
    driver::{DriverEvent, DriverEventKind},
    events,
    telemetry::LogEvent,
};
use serde_json::{Value, json};

pub fn records() -> Vec<LogEvent> {
    fn frame(name: &str, data: Value) -> Value {
        json!({
            "nonce":"01ARZ3NDEKTSV4RRFFQ69G5FAV","generation":1,
            "session_id":null,"seq":1,"event":name,
            "tick":dst_server::model::MAX_SAFE_INTEGER,"monotonic_ms":1,"cycle":null,"data":data,
        })
    }

    let mut presence = frame(
        "dst.server.presence",
        json!({
            "reason":"periodic","players":[],"clients":[],
            "world":{"season":"autumn","phase":"day","moonphase":null,"nightmarephase":null,
                "israining":false,"issnowing":false,"isacidraining":null,"islunarhailing":null,
                "lunar_rifts_enabled":true,"shadow_rifts_enabled":false}
        }),
    );
    // A quoted ID and its comma cost three bytes. The final ID has no comma.
    let budget = events::MAX_EVENT_BYTES - presence.to_string().len() + 1;
    let clients = budget.div_ceil(128 + 3);
    presence["data"]["clients"] = (0..clients)
        .map(|index| {
            let id_width = budget / clients + usize::from(index < budget % clients) - 3;
            format!("KU_{index:0width$}", width = id_width - 3)
        })
        .collect::<Vec<_>>()
        .into();

    let mut dice = frame(
        "dst.player.dice_rolled",
        json!({
            "name":"中文🌲", "prefab":null, "userid":null, "max":6, "rolls":[]
        }),
    );
    let remaining = events::MAX_EVENT_BYTES - dice.to_string().len();
    dice["data"]["rolls"] = json!(vec![1; remaining.div_ceil(2)]);

    let mut records = Vec::new();
    for frame in [presence, dice] {
        let source = serde_json::to_vec(&frame).unwrap();
        assert!((events::MAX_EVENT_BYTES - 1..=events::MAX_EVENT_BYTES).contains(&source.len()));
        let record = from_source(&source);
        assert_eq!(record.body, frame["data"]);
        records.push(record);
    }
    records.extend(schema_records());
    if let Some(path) = std::env::var_os("DST_TEST_EVENT_CAPTURE") {
        for line in std::fs::read_to_string(path).unwrap().lines() {
            records.push(from_source(line.as_bytes()));
        }
    }
    records
}

fn from_source(source: &[u8]) -> LogEvent {
    let game = events::parse(source).unwrap();
    LogEvent::from_driver(
        DriverEvent {
            id: ulid::Ulid::generate().to_string(),
            shard: "洞穴".into(),
            nonce: game.nonce.clone(),
            generation: Some(game.generation.0),
            observed_timestamp_ns: 1_791_013_234_567_890_123,
            kind: DriverEventKind::Telemetry(Box::new(game)),
        },
        "合成房间",
    )
    .unwrap()
}

fn schema_records() -> Vec<LogEvent> {
    use std::{path::Path, process::Command};

    let root = Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .parent()
        .unwrap();
    let output = Command::new("luajit")
        .current_dir(root)
        .args([
            "tests/lua/event_contract.lua",
            ".",
            "dst-scripts/scripts",
            "play",
        ])
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let records: Vec<_> = output
        .stdout
        .split(|byte| *byte == b'\n')
        .filter_map(|line| line.strip_prefix(b"DST_OTEL|"))
        .map(from_source)
        .collect();
    assert!(!records.is_empty(), "Lua fixture produced no native frames");
    records
}

pub async fn dense_context() -> (serde_json::Map<String, Value>, String, String) {
    use dst_server::{driver::DriverOptions, room::Room, rpc::EventHub};
    let directory = tempfile::tempdir().unwrap();
    let cluster = "c".repeat(255);
    let shard = "s".repeat(255);
    let resource = |count| (0..count).map(|i| (format!("k{i:04}"), json!(0))).collect();
    let open = |count: usize| {
        let path = directory.path().join(count.to_string()).join(&cluster);
        std::fs::create_dir_all(path.join(&shard)).unwrap();
        std::fs::write(path.join("cluster.ini"), "").unwrap();
        std::fs::write(
            path.join(&shard).join("server.ini"),
            "[SHARD]\nis_master=true\n",
        )
        .unwrap();
        Room::open(
            path,
            "/unused-game",
            DriverOptions::default(),
            EventHub::default(),
            dst_server::telemetry::TelemetryConfig {
                resource: resource(count),
                ..Default::default()
            },
        )
    };
    let (mut low, mut high) = (0_usize, 4096);
    while low + 1 < high {
        let middle = (low + high) / 2;
        match open(middle) {
            Ok(room) => {
                room.close_telemetry().await.unwrap();
                low = middle;
            }
            Err(e) => {
                assert_eq!(e.code, dst_server::model::ErrorCode::Overflow);
                high = middle;
            }
        }
    }
    assert!(low > 2000);
    let resource = resource(low);
    (resource, cluster, shard)
}

pub fn local_record(event: &LogEvent) -> Value {
    let mut value = serde_json::to_value(event).unwrap();
    value["resource"] = json!({"service.name":"dst-server"});
    value
}
