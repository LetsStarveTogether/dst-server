use std::{fs, os::unix::fs::PermissionsExt, path::PathBuf, time::Duration};

use anyhow::Result;
use dst_server::logs::{
    Direction, JournalCursorError, JournalLogs, JournalQuery, LogProcessError, NetdataLogQuery,
    NetdataLogs,
};
use serde_json::json;
use tempfile::TempDir;
use tokio::time::{sleep, timeout};

const DEADLINE: Duration = Duration::from_secs(5);
// Concurrent fixture writes can leave an inherited writable FD in another fork,
// making exec of the new script fail with ETXTBSY before that fork reaches exec.
static SCRIPT_TESTS: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());

struct Script {
    directory: TempDir,
    executable: PathBuf,
}
impl Script {
    fn new(body: &str) -> Result<Self> {
        let directory = tempfile::tempdir()?;
        let executable = directory.path().join("reader");
        fs::write(
            &executable,
            format!(
                r#"#!/usr/bin/python3
import json, os, pathlib, signal, sys, time
path = pathlib.Path(sys.argv[0])
path.with_suffix('.pid').write_text(str(os.getpid()))
path.with_suffix('.args').write_text(json.dumps(sys.argv[1:]))
{body}
"#
            ),
        )?;
        fs::set_permissions(&executable, fs::Permissions::from_mode(0o755))?;
        Ok(Self {
            directory,
            executable,
        })
    }
    fn journal(&self) -> JournalLogs {
        JournalLogs {
            executable: self.executable.clone(),
            ..JournalLogs::default()
        }
    }
    fn netdata(&self, concurrency: usize, record: usize, output: usize) -> Result<NetdataLogs> {
        NetdataLogs::new(
            self.executable.clone(),
            self.directory.path().join("stock.yaml"),
            self.directory.path().join("custom.yaml"),
            concurrency,
            record,
            output,
        )
    }
    fn args(&self) -> Result<Vec<String>> {
        Ok(serde_json::from_slice(&fs::read(
            self.executable.with_extension("args"),
        )?)?)
    }
    fn pid(&self) -> Result<i32> {
        Ok(fs::read_to_string(self.executable.with_extension("pid"))?.parse()?)
    }
}

fn reaped(pid: i32) -> bool {
    // SAFETY: signal zero only checks existence; waitpid cannot affect another task's child.
    unsafe {
        libc::kill(pid, 0) == -1
            && std::io::Error::last_os_error().raw_os_error() == Some(libc::ESRCH)
    }
}
async fn wait_reaped(pid: i32) -> Result<()> {
    timeout(DEADLINE, async {
        while !reaped(pid) {
            sleep(Duration::from_millis(10)).await;
        }
    })
    .await?;
    Ok(())
}

#[tokio::test]
async fn journal_cursor_pages_keep_native_order_and_lookahead() -> Result<()> {
    let _isolation = SCRIPT_TESTS.lock().await;
    let script = Script::new(
        r#"
args = dict(arg.split('=', 1) for arg in sys.argv[1:] if '=' in arg)
records = [{'__CURSOR': str(i), '__REALTIME_TIMESTAMP': '1000001', 'MESSAGE': str(i)} for i in range(7)]
if '--grep' in args: records = records[::2]
if '--reverse' in sys.argv: records.reverse()
if '--cursor' in args: records = records[next(i for i, r in enumerate(records) if r['__CURSOR'] == args['--cursor']):]
for record in records[:int(args['--lines'])]: print(json.dumps(record))
"#,
    )?;
    let units = [
        "dst-007-pod.service".to_owned(),
        "dst-007-*.service".to_owned(),
        "dst-007-*.service".to_owned(),
    ];
    for direction in [Direction::Forward, Direction::Backward] {
        for grep in [None, Some("--literal-option".to_owned())] {
            let mut request = JournalQuery {
                limit: 2,
                direction,
                grep,
                namespace: Some("games".into()),
                ..JournalQuery::default()
            };
            let mut delivered = Vec::new();
            loop {
                let result = script
                    .journal()
                    .query(Some(&units), &request, DEADLINE)
                    .await?;
                assert_eq!(
                    result.next_cursor.as_deref(),
                    result.records.last().map(|r| r.cursor())
                );
                delivered.extend(
                    result
                        .records
                        .iter()
                        .map(|r| r.cursor().parse::<u8>().unwrap()),
                );
                if !result.has_more {
                    break;
                }
                request.cursor = result.next_cursor;
            }
            let mut expected: Vec<u8> = (0..7)
                .step_by(if request.grep.is_some() { 2 } else { 1 })
                .collect();
            if direction == Direction::Backward {
                expected.reverse();
            }
            assert_eq!(delivered, expected);
            let args = script.args()?;
            assert_eq!(
                args.iter()
                    .filter(|a| *a == "--unit=dst-007-*.service")
                    .count(),
                1
            );
            assert!(args.contains(&"--namespace=games".into()));
            assert!(args.contains(&format!(
                "--lines={}4",
                if direction == Direction::Forward {
                    "+"
                } else {
                    ""
                }
            )));
            assert!(reaped(script.pid()?));
        }
    }
    let result = script
        .journal()
        .query(
            None,
            &JournalQuery {
                limit: 0,
                ..JournalQuery::default()
            },
            DEADLINE,
        )
        .await?;
    assert!(result.has_more && result.records.is_empty() && result.next_cursor.is_none());
    assert!(!script.args()?.iter().any(|arg| arg.starts_with("--unit=")));
    Ok(())
}

#[tokio::test]
async fn journal_preserves_raw_fields_and_bounded_diagnostics() -> Result<()> {
    let _isolation = SCRIPT_TESTS.lock().await;
    let script = Script::new(
        r#"
print(json.dumps({'__CURSOR': 'a', '__REALTIME_TIMESTAMP': '1000001', 'UNIT': 'dst-007-cave.service', '_SYSTEMD_UNIT': 'init.scope', 'MESSAGE': [104,105,0,255], 'EXTRA': ['first',[115,101,99,111,110,100]]}))
sys.stderr.write('x' * 100000 + ' partial journal access')
"#,
    )?;
    let result = script
        .journal()
        .query(None, &JournalQuery::default(), DEADLINE)
        .await?;
    let record = &result.records[0];
    assert_eq!(record.timestamp_us(), 1_000_001);
    assert_eq!(record.unit(), "dst-007-cave.service");
    assert_eq!(record.message(), "hi\0�");
    assert_eq!(
        record.fields["EXTRA"],
        json!(["first", [115, 101, 99, 111, 110, 100]])
    );
    assert!(result.diagnostics_truncated);
    assert_eq!(result.diagnostics.len(), 65536);
    assert!(result.diagnostics.ends_with("partial journal access"));
    assert!(reaped(script.pid()?));
    Ok(())
}

#[tokio::test]
async fn journal_rejects_invalid_scope_records_and_cursor() -> Result<()> {
    let _isolation = SCRIPT_TESTS.lock().await;
    let script = Script::new("print('{}', flush=True)\ntime.sleep(60)")?;
    for units in [
        vec![],
        vec!["--help".into()],
        vec!["../bad".into()],
        vec!["a\\x00b".into()],
        vec!["bad\nunit".into()],
        vec!["洞穴".into()],
    ] {
        assert!(
            script
                .journal()
                .query(Some(&units), &JournalQuery::default(), DEADLINE)
                .await
                .is_err()
        );
    }
    assert!(!script.executable.with_extension("pid").exists());
    for value in [
        json!({"limit":true}),
        json!({"limit":-1}),
        json!({"direction":"sideways"}),
    ] {
        assert!(serde_json::from_value::<JournalQuery>(value).is_err());
    }
    for request in [
        JournalQuery {
            cursor: Some("x".into()),
            since: Some("yesterday".into()),
            ..JournalQuery::default()
        },
        JournalQuery {
            grep: Some(" \n".into()),
            ..JournalQuery::default()
        },
    ] {
        assert!(
            script
                .journal()
                .query(None, &request, DEADLINE)
                .await
                .is_err()
        );
    }
    assert!(
        script
            .journal()
            .follow(None, &JournalQuery::default())
            .await
            .is_err()
    );
    let error = script
        .journal()
        .query(None, &JournalQuery::default(), DEADLINE)
        .await
        .unwrap_err();
    assert!(error.to_string().contains("invalid journalctl JSON record"));
    assert!(reaped(script.pid()?));
    for body in [
        "pass",
        "print('{\"__CURSOR\":\"different\",\"__REALTIME_TIMESTAMP\":\"1\"}')",
    ] {
        let missing = Script::new(body)?;
        let error = missing
            .journal()
            .query(
                None,
                &JournalQuery {
                    cursor: Some("missing".into()),
                    ..JournalQuery::default()
                },
                DEADLINE,
            )
            .await
            .unwrap_err();
        assert_eq!(
            error.downcast_ref::<JournalCursorError>().unwrap().cursor,
            "missing"
        );
        assert!(reaped(missing.pid()?));
    }
    Ok(())
}

#[tokio::test]
async fn journal_grep_empty_results_preserve_actual_process_errors() -> Result<()> {
    let _isolation = SCRIPT_TESTS.lock().await;
    for grep in [None, Some("search".to_owned())] {
        for diagnostics in ["", "permission denied"] {
            let script = Script::new(&format!("sys.stderr.write({diagnostics:?})\nsys.exit(1)"))?;
            let result = script
                .journal()
                .query(
                    None,
                    &JournalQuery {
                        grep: grep.clone(),
                        ..JournalQuery::default()
                    },
                    DEADLINE,
                )
                .await;
            if grep.is_some() && diagnostics.is_empty() {
                assert!(result?.records.is_empty());
            } else {
                let error = result.unwrap_err();
                let process = error.downcast_ref::<LogProcessError>().unwrap();
                assert_eq!(process.returncode, 1);
                assert_eq!(process.diagnostics, diagnostics);
            }
            assert!(reaped(script.pid()?));
        }
    }
    Ok(())
}

#[tokio::test]
async fn follow_cancellation_keeps_anchor_and_close_reaps_even_under_backpressure() -> Result<()> {
    let _isolation = SCRIPT_TESTS.lock().await;
    let script = Script::new(
        r#"
time.sleep(0.15)
for cursor in ['anchor', 'next']:
    print(json.dumps({'__CURSOR': cursor, '__REALTIME_TIMESTAMP':'1'}), flush=True)
sys.stderr.write('reader warning\n'); sys.stderr.flush()
time.sleep(60)
"#,
    )?;
    let mut stream = script
        .journal()
        .follow(
            None,
            &JournalQuery {
                cursor: Some("anchor".into()),
                ..JournalQuery::follow()
            },
        )
        .await?;
    assert!(
        timeout(Duration::from_millis(20), stream.next_record())
            .await
            .is_err()
    );
    assert_eq!(
        timeout(DEADLINE, stream.next_record())
            .await??
            .unwrap()
            .cursor(),
        "next"
    );
    stream.close().await?;
    assert!(reaped(script.pid()?));
    assert_eq!(stream.diagnostics(), "reader warning");

    let noisy = Script::new(
        r#"
signal.signal(signal.SIGTERM, signal.SIG_IGN)
i = 0
while True:
    print(json.dumps({'__CURSOR':str(i),'__REALTIME_TIMESTAMP':'1','MESSAGE':'x'*262144}), flush=True)
    i += 1
    path.with_suffix('.count').write_text(str(i))
"#,
    )?;
    let mut stream = noisy
        .journal()
        .follow(None, &JournalQuery::follow())
        .await?;
    assert!(timeout(DEADLINE, stream.next_record()).await??.is_some());
    sleep(Duration::from_millis(100)).await;
    let count: usize = fs::read_to_string(noisy.executable.with_extension("count"))?.parse()?;
    assert!(count <= 4, "writer ran past bounded queue: {count}");
    // Cancelling close still leaves the supervisor responsible for escalation.
    assert!(
        timeout(Duration::from_millis(20), stream.close())
            .await
            .is_err()
    );
    timeout(DEADLINE, stream.close()).await??;
    assert!(reaped(noisy.pid()?));
    Ok(())
}

#[tokio::test]
async fn journal_budgets_timeout_and_task_abort_reap_children() -> Result<()> {
    let _isolation = SCRIPT_TESTS.lock().await;
    for (record, output, expected) in [
        (100, 1_000_000, "record exceeds"),
        (1_000_000, 100, "query exceeds"),
    ] {
        let script = Script::new(
            "print(json.dumps({'__CURSOR':'a','__REALTIME_TIMESTAMP':'1','MESSAGE':'x'*1000}),flush=True)\ntime.sleep(60)",
        )?;
        let mut reader = script.journal();
        reader.max_record_bytes = record;
        reader.max_output_bytes = output;
        let error = reader
            .query(None, &JournalQuery::default(), DEADLINE)
            .await
            .unwrap_err();
        assert!(error.to_string().contains(expected));
        assert!(reaped(script.pid()?));
    }
    let script =
        Script::new("sys.stderr.write('waiting '*20000);sys.stderr.flush()\ntime.sleep(60)")?;
    let error = script
        .journal()
        .query(None, &JournalQuery::default(), Duration::from_millis(100))
        .await
        .unwrap_err();
    assert!(error.to_string().contains("timed out"));
    assert!(reaped(script.pid()?));
    let reader = script.journal();
    fs::remove_file(script.executable.with_extension("pid"))?;
    let query =
        tokio::spawn(async move { reader.query(None, &JournalQuery::default(), DEADLINE).await });
    timeout(DEADLINE, async {
        while !script.executable.with_extension("pid").exists() {
            sleep(Duration::from_millis(5)).await;
        }
    })
    .await?;
    let pid = script.pid()?;
    query.abort();
    let _ = query.await;
    wait_reaped(pid).await?;
    Ok(())
}

#[tokio::test]
async fn query_cancellation_confirms_reaping_before_returning() -> Result<()> {
    let _isolation = SCRIPT_TESTS.lock().await;
    for journal in [true, false] {
        let script = Script::new(
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\npath.with_suffix('.ready').touch()\ntime.sleep(60)",
        )?;
        let cancelled = async {
            timeout(DEADLINE, async {
                while !script.executable.with_extension("ready").exists() {
                    sleep(Duration::from_millis(5)).await;
                }
            })
            .await
            .expect("query subprocess started");
        };
        let error = if journal {
            script
                .journal()
                .query_cancellable(None, &JournalQuery::default(), DEADLINE, cancelled)
                .await
                .unwrap_err()
        } else {
            script
                .netdata(1, 4096, 65536)?
                .query_cancellable(&netdata_request("hang"), DEADLINE, cancelled)
                .await
                .unwrap_err()
        };
        assert_eq!(
            error.downcast_ref::<std::io::Error>().unwrap().kind(),
            std::io::ErrorKind::Interrupted
        );
        assert!(reaped(script.pid()?));
    }
    Ok(())
}

const PLUGIN: &str = r#"
assert sys.argv[1] == 'logs'
args = dict(arg[2:].split('=', 1) for arg in sys.argv[2:])
since, until = int(args['since']), int(args['until'])
query = args.get('query','')
if query == 'hang':
    time.sleep(60)
if query == 'empty':
    stream = ', stream=' + args.get('namespace','') + '/' + args['name'] if 'name' in args else ''
    print(f'no WAL/SFST files matched (tenant=default, window={since}..{until}{stream})', file=sys.stderr)
    sys.exit(0)
count = int(args['limit']) + 1 if query == 'excess' else 2
for i in range(count):
    print(json.dumps({'timestamp_ns':since*1000000000+count-i,'fields':[['tag','a'],['tag','b'],['event_name','dst.player.action']]}))
if query == 'missing': sys.exit(0)
if query == 'fail':
    print('query failed',file=sys.stderr);sys.exit(7)
if query == 'diagnostics': print('diagnostic '*20000,file=sys.stderr)
matched = 5 if query == 'limited' else 0 if query == 'count-failure' else count
returned = count+1 if query == 'wrong-count' else count
end = until+1 if query == 'wrong-window' else until
summary = f'matched={matched} returned={returned} window={since}..{end}'
print(summary,file=sys.stderr)
if query == 'duplicate-summary': print(summary,file=sys.stderr)
if query == 'lost-summary': print('later diagnostic '*20000,file=sys.stderr)
"#;
fn netdata_request(query: &str) -> NetdataLogQuery {
    NetdataLogQuery {
        until: Some(1_000_300),
        query: Some(query.into()),
        ..NetdataLogQuery::new(1_000_000)
    }
}

#[tokio::test]
async fn netdata_arguments_window_duplicates_and_reported_counts() -> Result<()> {
    let _isolation = SCRIPT_TESTS.lock().await;
    let script = Script::new(PLUGIN)?;
    let reader = script.netdata(1, 4 * 1024 * 1024, 64 * 1024 * 1024)?;
    let request = NetdataLogQuery {
        service_name: Some("--service".into()),
        service_namespace: Some(String::new()),
        filters: vec![
            ("cluster".into(), "dst-000".into()),
            ("event".into(), "joined".into()),
            ("event".into(), "left".into()),
        ],
        fields: vec!["event_name".into(), "tag".into()],
        limit: 20,
        ..netdata_request("玩家 | --limit 999")
    };
    let result = reader.query(&request, DEADLINE).await?;
    assert_eq!(result.records[0].values("tag"), vec!["a", "b"]);
    assert!(result.records[0].timestamp_ns > result.records[1].timestamp_ns);
    assert_eq!((result.since, result.until), (1_000_000, 1_000_300));
    assert_eq!(result.matched, Some(2));
    assert_eq!(result.truncated(), Some(false));
    let args = script.args()?;
    for argument in [
        "logs",
        "--since=1000000",
        "--until=1000300",
        "--name=--service",
        "--namespace=",
        "--filter=cluster=dst-000,event=joined,event=left",
        "--query=玩家 | --limit 999",
        "--fields=event_name,tag",
        "--limit=20",
        "--output=ndjson",
    ] {
        assert!(args.contains(&argument.into()), "missing {argument}");
    }
    for (query, matched, truncated) in [
        ("limited", Some(5), Some(true)),
        ("count-failure", Some(0), Some(false)),
        ("diagnostics", Some(2), Some(false)),
        ("lost-summary", None, None),
        ("empty", Some(0), Some(false)),
    ] {
        let result = reader.query(&netdata_request(query), DEADLINE).await?;
        assert_eq!(result.matched, matched);
        assert_eq!(result.truncated(), truncated);
        assert_eq!(
            result.diagnostics_truncated,
            ["diagnostics", "lost-summary"].contains(&query)
        );
        assert!(result.diagnostics.len() <= 65536);
        assert!(reaped(script.pid()?));
    }
    let before = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)?
        .as_secs()
        + 1;
    let result = reader
        .query(&NetdataLogQuery::new(1_000_000), DEADLINE)
        .await?;
    let after = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)?
        .as_secs()
        + 1;
    assert!((before..=after).contains(&(result.until as u64)));
    Ok(())
}

#[tokio::test]
async fn netdata_invalid_protocol_and_query_validation() -> Result<()> {
    let _isolation = SCRIPT_TESTS.lock().await;
    let script = Script::new(PLUGIN)?;
    let reader = script.netdata(1, 4 * 1024 * 1024, 64 * 1024 * 1024)?;
    for (query, expected) in [
        ("missing", "valid query summary"),
        ("wrong-count", "returned count"),
        ("wrong-window", "summary window"),
        ("duplicate-summary", "multiple query summaries"),
        ("excess", "more records"),
    ] {
        let error = reader
            .query(&netdata_request(query), DEADLINE)
            .await
            .unwrap_err();
        assert!(error.to_string().contains(expected), "{error:#}");
        assert!(reaped(script.pid()?));
    }
    let error = reader
        .query(&netdata_request("fail"), DEADLINE)
        .await
        .unwrap_err();
    assert_eq!(
        error.downcast_ref::<LogProcessError>().unwrap().returncode,
        7
    );
    for patch in [
        json!({"limit":0}),
        json!({"until":1_000_000}),
        json!({"service_namespace":""}),
        json!({"filters":[["x"," x"]]}),
        json!({"filters":[["x,y","v"]]}),
        json!({"fields":[" x"]}),
        json!({"query":"\0"}),
    ] {
        let mut request = serde_json::to_value(netdata_request("valid"))?;
        request
            .as_object_mut()
            .unwrap()
            .extend(patch.as_object().unwrap().clone());
        let request: NetdataLogQuery = serde_json::from_value(request)?;
        assert!(reader.command(&request).is_err());
    }
    for patch in [
        json!({"since":-1}),
        json!({"until":4_294_967_296_u64}),
        json!({"limit":true}),
    ] {
        let mut request = serde_json::to_value(netdata_request("valid"))?;
        request
            .as_object_mut()
            .unwrap()
            .extend(patch.as_object().unwrap().clone());
        assert!(serde_json::from_value::<NetdataLogQuery>(request).is_err());
    }
    assert!(script.netdata(0, 100, 100).is_err());
    assert!(script.netdata(1, 0, 100).is_err());
    Ok(())
}

#[tokio::test]
async fn netdata_timeout_includes_slot_wait_and_reaps_cancelled_process() -> Result<()> {
    let _isolation = SCRIPT_TESTS.lock().await;
    let script = Script::new(PLUGIN)?;
    let reader = std::sync::Arc::new(script.netdata(1, 4096, 65536)?);
    let error = reader
        .query(&netdata_request("valid"), Duration::MAX)
        .await
        .unwrap_err();
    assert!(error.to_string().contains("timeout"));
    assert!(!script.executable.with_extension("pid").exists());
    let running = reader.clone();
    let task = tokio::spawn(async move { running.query(&netdata_request("hang"), DEADLINE).await });
    timeout(DEADLINE, async {
        while !script.executable.with_extension("pid").exists() {
            sleep(Duration::from_millis(5)).await;
        }
    })
    .await?;
    let pid = script.pid()?;
    let error = reader
        .query_cancellable(&netdata_request("valid"), DEADLINE, async {})
        .await
        .unwrap_err();
    assert_eq!(
        error.downcast_ref::<std::io::Error>().unwrap().kind(),
        std::io::ErrorKind::Interrupted
    );
    assert_eq!(script.pid()?, pid);
    let error = reader
        .query(&netdata_request("valid"), Duration::from_millis(20))
        .await
        .unwrap_err();
    assert!(error.to_string().contains("timed out"));
    assert_eq!(script.pid()?, pid);
    task.abort();
    let _ = task.await;
    wait_reaped(pid).await?;
    assert_eq!(
        reader
            .query(&netdata_request("valid"), DEADLINE)
            .await?
            .matched,
        Some(2)
    );
    Ok(())
}
