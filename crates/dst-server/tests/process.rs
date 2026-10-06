//! Synthetic children only: these checks never start a game or touch a room.
use std::{io, os::unix::process::ExitStatusExt, time::Duration};

use dst_server::process::{
    EventKind, GameProcess, MAX_RPC_REPLY_BYTES, ProcessOutput, ProcessSpec, StreamKind,
    native_message,
};
use tokio::time::timeout;

fn python(source: &str, arguments: &[String]) -> (GameProcess, ProcessOutput) {
    let mut args = vec!["-u".into(), "-c".into(), source.into()];
    args.extend(arguments.iter().map(Into::into));
    GameProcess::spawn(ProcessSpec {
        program: "python3".into(),
        args,
        cwd: std::env::temp_dir(),
    })
    .expect("spawn Python fixture")
}

fn assert_reaped(pid: u32) {
    let mut status = 0;
    // SAFETY: waitpid is used only to assert that our former child was reaped.
    assert_eq!(
        unsafe { libc::waitpid(pid as i32, &mut status, libc::WNOHANG) },
        -1
    );
    assert_eq!(
        io::Error::last_os_error().raw_os_error(),
        Some(libc::ECHILD)
    );
}

#[tokio::test]
async fn five_children_have_isolated_pipes_and_drain_oversized_logs() {
    let script = r#"
import os, sys
identity = sys.argv[1].encode()
os.write(1, b'x' * (1024 * 1024 + 1) + b'\nafter\n')
os.write(5, b'ready:' + identity + b'\n')
while True:
    request = os.read(3, 4096)
    if request == b'quit':
        os.write(4, b'bye:' + identity + b'\n')
        break
    if not request:
        raise SystemExit(2)
    os.write(4, identity + b':' + request + b'\n')
"#;
    let mut children = Vec::new();
    for index in 0..5 {
        children.push(python(script, &[index.to_string()]));
    }
    for (index, (child, output)) in children.iter_mut().enumerate() {
        assert_eq!(
            child.send(&vec![0; 4097]).await.unwrap_err().kind(),
            io::ErrorKind::InvalidInput
        );
        child
            .send(format!("request-{index}").as_bytes())
            .await
            .unwrap();
        let (mut ready, mut reply, mut oversized, mut after) = (false, false, false, false);
        timeout(Duration::from_secs(10), async {
            while !(ready && reply && oversized && after) {
                let event = output.next_event().await.expect("child output");
                match (event.stream, event.kind) {
                    (StreamKind::Lifecycle, EventKind::Line(bytes)) => {
                        assert_eq!(bytes, format!("ready:{index}\n").as_bytes());
                        ready = true;
                    }
                    (StreamKind::Reply, EventKind::Line(bytes)) => {
                        assert_eq!(bytes, format!("{index}:request-{index}\n").as_bytes());
                        reply = true;
                    }
                    (StreamKind::Stdout, EventKind::Oversized { mandatory: false }) => {
                        oversized = true
                    }
                    (StreamKind::Stdout, EventKind::Line(bytes)) => {
                        assert_eq!(bytes, b"after\n");
                        after = true;
                    }
                    other => panic!("unexpected output: {other:?}"),
                }
            }
        })
        .await
        .expect("all outputs drained");
    }
    // Other children remain alive: leaked descriptors would keep these pipes open.
    for (index, (child, output)) in children.iter_mut().enumerate() {
        child.send(b"quit").await.unwrap();
        let report = timeout(Duration::from_secs(3), child.wait())
            .await
            .unwrap()
            .unwrap();
        assert!(report.status.success());
        assert!(!report.forced);
        assert!(report.output_drained);
        assert!(report.protocol_error.is_none());
        assert_eq!(report.stats.oversized, [0, 0, 1, 0]);
        let event = output.next_event().await.unwrap();
        assert_eq!(event.stream, StreamKind::Reply);
        match event.kind {
            EventKind::Line(bytes) => assert_eq!(bytes, format!("bye:{index}\n").as_bytes()),
            other => panic!("unexpected output: {other:?}"),
        }
        assert!(output.next_event().await.is_none());
        assert_reaped(child.pid());
    }
}

#[tokio::test]
async fn log_pressure_does_not_block_control_or_exit() {
    let (child, mut output) = python(
        r#"
import os
for _ in range(6000):
    os.write(1, b'x' * 4095 + b'\n')
os.write(1, b'[123:45:56]: DST_DRIVER|{}\n')
os.write(4, b'complete\n')
"#,
        &[],
    );
    let report = timeout(Duration::from_secs(10), child.wait())
        .await
        .unwrap()
        .unwrap();
    assert!(report.status.success());
    assert!(report.output_drained);
    assert!(report.protocol_error.is_none());
    assert!(report.stats.dropped[2] > 0);
    let first = output.next_event().await.unwrap();
    assert_eq!(first.stream, StreamKind::Reply);
    assert!(matches!(first.kind, EventKind::Line(line) if line == b"complete\n"));
    while let Some(event) = output.next_event().await {
        assert_eq!(event.stream, StreamKind::Stdout);
        assert!(matches!(event.kind, EventKind::Line(line) if line.len() == 4096));
    }
    assert_reaped(child.pid());
}

#[tokio::test]
async fn native_diagnostics_survive_log_drops_but_reject_oversized_and_partial_pairs() {
    let (child, mut output) = python(
        r#"
import os
dns = b'CURL ERROR: (login.kleientertainment.com) [6]Could not resolve host: login.kleientertainment.com\n'
account = b'[0] Account Failed (6): ""\n'
os.write(1, b'x\n' * 6000)
os.write(1, dns + b'x' * (1024 * 1024 + 1) + b'\n' + account)
os.write(1, dns + b'[0:00:01]: DST_DRIVER|private-marker\n' + account)
for byte in dns + account:
    os.write(1, bytes([byte]))
os.write(1, b'LUA ERROR stack traceback:\n' + dns + account[:-1])
os.write(4, b'complete\n')
"#,
        &[],
    );
    let report = timeout(Duration::from_secs(5), child.wait())
        .await
        .unwrap()
        .unwrap();
    assert!(report.status.success() && report.output_drained && !report.forced);
    assert!(report.protocol_error.is_none());
    assert!(report.stats.dropped[2] > 0);
    assert_eq!(report.stats.oversized[2], 1);
    let diagnostics = child.native_diagnostics();
    assert_eq!(diagnostics.login_transport.unwrap().count, 1);
    assert_eq!(diagnostics.lua_error.unwrap().count, 1);
    let control = output.next_event().await.unwrap();
    assert_eq!(control.stream, StreamKind::Reply);
    assert!(matches!(control.kind, EventKind::Line(line) if line == b"complete\n"));
    while let Some(event) = output.next_event().await {
        if let EventKind::Line(line) = event.kind {
            assert_eq!(line, b"x\n");
        }
    }
    assert_reaped(child.pid());
}

#[tokio::test]
async fn reply_telemetry_shares_log_bounds_and_keeps_control_available() {
    for size in [64, 65536] {
        let (child, mut output) = python(
            r#"
import os, sys
size = int(sys.argv[1])
if size == 65536:
    for _ in range(128):
        os.write(1, b'x' * 32767 + b'\n')
for _ in range(1200):
    os.write(4, b'DST_OTEL|' + b'x' * (size - 10) + b'\n')
os.write(4, b'DST_DRIVER|{}\nDST_RPC|{}\nDST_RemoteCommandDone\n')
"#,
            &[size.to_string()],
        );
        let report = timeout(Duration::from_secs(10), child.wait())
            .await
            .unwrap()
            .unwrap();
        assert!(report.status.success() && report.output_drained && !report.forced);
        assert!(report.protocol_error.is_none());
        assert!(report.stats.dropped[0] > 0);
        for expected in [
            b"DST_DRIVER|{}\n".as_slice(),
            b"DST_RPC|{}\n",
            b"DST_RemoteCommandDone\n",
        ] {
            let event = output.next_event().await.unwrap();
            let EventKind::Line(line) = event.kind else {
                panic!("expected control line")
            };
            assert_eq!(event.stream, StreamKind::Reply);
            assert_eq!(line, expected);
        }
        let (mut lines, mut bytes) = (0, 0);
        while let Some(event) = output.next_event().await {
            let EventKind::Line(line) = event.kind else {
                panic!("expected optional line")
            };
            lines += 1;
            bytes += line.len();
        }
        assert!(lines > 0 && lines <= 1024 && bytes <= 8 * 1024 * 1024);
        if size == 64 {
            assert_eq!(lines, 1024);
            assert_eq!(report.stats.dropped[0], 176);
        } else {
            assert!(
                lines < 1024,
                "the shared byte budget must fill before the line count"
            );
        }
        assert_reaped(child.pid());
    }
}

#[tokio::test]
async fn reply_telemetry_boundaries_discard_only_the_bad_line() {
    let (child, mut output) = python(
        r#"
import os, time
for prefix in (b'DST_OTEL|', b'[0:00:01]: DST_OTEL|', b'xDST_OTEL|', b'DST_OTELx|', b'[0:00:01]: [0:00:02]: DST_OTEL|'):
    for byte in prefix:
        os.write(4, bytes([byte]))
        time.sleep(.001)
    os.write(4, b'{}\n')
for size in (4095, 4096, 4097, 8192, 32768, 65535, 65536, 65537):
    os.write(4, b'DST_OTEL|' + b'x' * (size - 10) + b'\n')
os.write(4, b'DST_RPC|{}\nDST_OTEL|{}\nDST_OTEL|{')
"#,
        &[],
    );
    let report = timeout(Duration::from_secs(5), child.wait())
        .await
        .unwrap()
        .unwrap();
    assert!(report.status.success() && report.output_drained && !report.forced);
    assert!(report.protocol_error.is_none());
    assert_eq!(report.stats.oversized, [1, 0, 0, 0]);
    let mut control = Vec::new();
    for _ in 0..5 {
        let event = output.next_event().await.unwrap();
        let EventKind::Line(line) = event.kind else {
            panic!("expected mandatory line")
        };
        assert!(!line.starts_with(b"DST_OTEL|"));
        control.push(line);
    }
    assert_eq!(control.last().unwrap(), b"DST_RPC|{}\n");
    let mut lengths = Vec::new();
    let mut oversized = 0;
    while let Some(event) = output.next_event().await {
        match event.kind {
            EventKind::Line(line) => lengths.push(line.len()),
            EventKind::Oversized { mandatory: false } => oversized += 1,
            other => panic!("unexpected telemetry: {other:?}"),
        }
    }
    assert_eq!(
        lengths,
        [12, 4095, 4096, 4097, 8192, 32768, 65535, 65536, 12, 10]
    );
    assert_eq!(oversized, 1);
    assert_reaped(child.pid());
}

#[tokio::test]
async fn only_raw_fd4_rpc_gets_the_larger_line_limit() {
    let (child, mut output) = python(
        r#"
import os, sys
limit = int(sys.argv[1])
for size in (65537, limit):
    os.write(4, b'DST')
    os.write(4, b'_RPC|' + b'x' * (size - 9) + b'\n')
"#,
        &[MAX_RPC_REPLY_BYTES.to_string()],
    );
    for expected in [65537, MAX_RPC_REPLY_BYTES] {
        let event = output.next_event().await.unwrap();
        let EventKind::Line(line) = event.kind else {
            panic!("expected a complete reply")
        };
        assert_eq!(event.stream, StreamKind::Reply);
        assert_eq!(line.len(), expected);
        assert!(line.starts_with(b"DST_RPC|") && line.ends_with(b"\n"));
    }
    assert!(child.wait().await.unwrap().status.success());
}

#[tokio::test]
async fn protocol_pressure_fails_closed() {
    for (line, count) in [
        ("b'reply\\n'".to_owned(), 129),
        (
            format!("b'DST_RPC|' + b'x' * {} + b'\\n'", MAX_RPC_REPLY_BYTES - 9),
            9,
        ),
    ] {
        let (child, _output) = python(
            &format!(
                "import os, time\nline = {line}\nfor _ in range({count}): os.write(4, line)\ntime.sleep(60)"
            ),
            &[],
        );
        let report = timeout(Duration::from_secs(3), child.wait())
            .await
            .unwrap()
            .unwrap();
        assert!(report.forced);
        assert!(report.protocol_error.unwrap().contains("queue is full"));
        assert_eq!(report.status.signal(), Some(libc::SIGKILL));
        assert!(report.stats.dropped[0] > 0);
        assert_reaped(child.pid());
    }
}

#[tokio::test]
async fn only_fd4_delivers_sdk_frames_and_fd5_stats_consume_no_control_slots() {
    let (child, mut output) = python(
        r#"
import os
private = b'private-marker'
for fd in (1, 2):
    for name in (b'DRIVER', b'RPC', b'CONTROL', b'OTEL'):
        for stamp in (b'', b'[125:00:01]: '):
            os.write(fd, stamp + b'DST_' + name + b'|' + private + b'\n')
    os.write(fd, b'[0:00:01]: player mentioned DST_RPC| in chat\n')
    os.write(fd, b'DST_DRIVER|' + b'x' * 65536 + b'\n')
    os.write(fd, b'[0:00:01]: DST_CONTROL|' + private)
os.write(4, b'[0:00:01]: DST_RPC|' + private + b'\n')
os.write(5, b'DST_Stats|sample\n' * 256 + b'DST_Master_Ready\n')
os.write(4, b'DST_OTEL|{}')
"#,
        &[],
    );
    let report = timeout(Duration::from_secs(3), child.wait())
        .await
        .unwrap()
        .unwrap();
    assert!(report.status.success() && report.protocol_error.is_none());
    assert_eq!(report.stats, Default::default());
    let mut records = Vec::new();
    while let Some(event) = output.next_event().await {
        let EventKind::Line(line) = event.kind else {
            panic!("expected line")
        };
        records.push((event.stream, String::from_utf8(line).unwrap()));
    }
    assert_eq!(records.len(), 5, "{records:?}");
    for stream in [StreamKind::Stdout, StreamKind::Stderr] {
        assert!(records.contains(&(
            stream,
            "[0:00:01]: player mentioned DST_RPC| in chat\n".into()
        )));
    }
    assert!(records.contains(&(StreamKind::Reply, "SDK RPC frame (34 bytes)\n".into())));
    assert!(records.contains(&(StreamKind::Lifecycle, "DST_Master_Ready\n".into())));
    assert!(records.contains(&(StreamKind::Reply, "DST_OTEL|{}".into())));
    assert_reaped(child.pid());
}

#[tokio::test]
async fn damaged_reply_and_lifecycle_frames_remain_mandatory() {
    for (fd, payload) in [
        (4, "b'DST_OTEL'"),
        (4, "b'[0:00:01]: DST_OTEL|{'"),
        (4, "b'xDST_OTEL|{'"),
        (4, "b'DST_DRIVER|{'"),
        (4, "b'DST_RPC|{'"),
        (4, "b'DST_CONTROL|{'"),
        (4, "b'unknown'"),
        (5, "b'DST_OTEL|{'"),
        (5, "b'DST_Stats|sample'"),
        (4, "b'DST_RPC|' + b'x' * (1024 * 1024 - 8) + b'\\n'"),
        (4, "b'[0:00:01]: DST_RPC|' + b'x' * 65536 + b'\\n'"),
        (5, "b'DST_RPC|' + b'x' * 65536 + b'\\n'"),
        (4, "b'DST_DRIVER|' + b'x' * 65536 + b'\\n'"),
        (4, "b'DST_CONTROL|' + b'x' * 65536 + b'\\n'"),
        (5, "b'DST_Stats|' + b'x' * 65536 + b'\\n'"),
        (5, "b'DST_Master_Ready\\n' * 129"),
    ] {
        let (child, _output) = python(
            &format!("import os, time\nos.write({fd}, {payload})\nos.close({fd})\ntime.sleep(60)"),
            &[],
        );
        let report = timeout(Duration::from_secs(3), child.wait())
            .await
            .unwrap()
            .unwrap();
        assert!(
            report.forced && report.protocol_error.is_some(),
            "{fd}: {payload}"
        );
        assert_reaped(child.pid());
    }
}

#[test]
fn native_timestamps_do_not_accept_embedded_control_prefixes() {
    for line in [
        b"[0:00:01]: DST_DRIVER|{}".as_slice(),
        b"[125:01:01]:DST_DRIVER|{}",
    ] {
        assert_eq!(native_message(line), b"DST_DRIVER|{}");
    }
    for line in [
        b"player: DST_DRIVER|{}".as_slice(),
        b"[player]: DST_DRIVER|{}",
        b"[0:1:01]: DST_DRIVER|{}",
    ] {
        assert_eq!(native_message(line), line);
    }
}

#[tokio::test]
async fn cancelled_shutdown_still_escalates_and_reaps() {
    let (child, mut output) = python(
        r#"
import os, signal, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
os.write(5, b'ready\n')
time.sleep(60)
"#,
        &[],
    );
    timeout(Duration::from_secs(3), output.next_event())
        .await
        .unwrap()
        .unwrap();
    assert_eq!(
        child.shutdown(Duration::MAX).await.unwrap_err().kind(),
        io::ErrorKind::InvalidInput
    );
    assert!(
        timeout(
            Duration::from_millis(20),
            child.shutdown(Duration::from_millis(100))
        )
        .await
        .is_err()
    );
    let report = timeout(Duration::from_secs(3), child.wait())
        .await
        .unwrap()
        .unwrap();
    assert!(report.forced);
    assert_eq!(report.status.signal(), Some(libc::SIGKILL));
    assert_eq!(report.returncode(), Some(-libc::SIGKILL));
    assert!(report.output_drained);
    assert_reaped(child.pid());
}

#[tokio::test]
async fn sigterm_fallback_is_forced_even_when_the_child_exits_zero() {
    let (child, mut output) = python(
        r#"
import os, signal, sys, time
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
os.write(5, b'ready\n')
time.sleep(60)
"#,
        &[],
    );
    timeout(Duration::from_secs(3), output.next_event())
        .await
        .unwrap()
        .unwrap();
    let report = timeout(
        Duration::from_secs(3),
        child.shutdown(Duration::from_secs(1)),
    )
    .await
    .unwrap()
    .unwrap();
    assert!(report.status.success());
    assert_eq!(report.returncode(), Some(0));
    assert!(report.forced && report.output_drained);
    assert_reaped(child.pid());
}

#[tokio::test]
async fn full_atomic_requests_are_not_combined() {
    let (child, _output) = python(
        r#"
import os
for _ in range(10):
    request = os.read(3, 8192)
    if len(request) != 4096:
        raise SystemExit(4)
os.write(4, b'complete\n')
"#,
        &[],
    );
    timeout(Duration::from_secs(5), async {
        for _ in 0..10 {
            child.send(&[b'x'; 4096]).await.unwrap();
        }
    })
    .await
    .unwrap();
    let report = timeout(Duration::from_secs(3), child.wait())
        .await
        .unwrap()
        .unwrap();
    assert!(report.status.success(), "{:?}", report.status);
    assert_reaped(child.pid());
}
