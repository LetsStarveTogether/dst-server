//! Synthetic children only: these checks never start a game or touch a room.
use std::{io, os::unix::process::ExitStatusExt, time::Duration};

use dst_server::process::{EventKind, GameProcess, ProcessSpec, StreamKind, native_message};
use tokio::time::timeout;

fn python(source: &str, arguments: &[String]) -> GameProcess {
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
    for (index, child) in children.iter().enumerate() {
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
                let event = child.next_event().await.expect("child output");
                match (event.stream, event.kind) {
                    (StreamKind::Lifecycle, EventKind::Line(bytes)) => {
                        assert_eq!(bytes, format!("ready:{index}\n").as_bytes());
                        ready = true;
                    }
                    (StreamKind::Reply, EventKind::Line(bytes)) => {
                        assert_eq!(bytes, format!("{index}:request-{index}\n").as_bytes());
                        reply = true;
                    }
                    (StreamKind::Stdout, EventKind::Oversized) => oversized = true,
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
    for (index, child) in children.iter().enumerate() {
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
        let event = child.next_event().await.unwrap();
        assert_eq!(event.stream, StreamKind::Reply);
        match event.kind {
            EventKind::Line(bytes) => assert_eq!(bytes, format!("bye:{index}\n").as_bytes()),
            other => panic!("unexpected output: {other:?}"),
        }
        assert!(child.next_event().await.is_none());
        assert_reaped(child.pid());
    }
}

#[tokio::test]
async fn log_pressure_does_not_block_control_or_exit() {
    let child = python(
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
    let mut streams = Vec::new();
    for _ in 0..2 {
        let event = child.next_event().await.unwrap();
        let EventKind::Line(line) = event.kind else {
            panic!("expected control line");
        };
        assert!(line == b"complete\n" || native_message(&line) == b"DST_DRIVER|{}\n");
        streams.push(event.stream);
    }
    assert!(streams.contains(&StreamKind::Reply));
    assert!(streams.contains(&StreamKind::Stdout));
    assert_reaped(child.pid());
}

#[tokio::test]
async fn protocol_pressure_fails_closed() {
    let child = python(
        r#"
import os, time
os.write(4, b'reply\n' * 129)
time.sleep(60)
"#,
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

#[tokio::test]
async fn mandatory_stdout_pressure_and_oversize_fail_closed() {
    for output in [
        "os.write(1, b'[125:00:01]: DST_DRIVER|{}\\n' * 129)",
        "os.write(1, b'DST_DRIVER|' + b'x' * 65536 + b'\\n')",
    ] {
        let child = python(&format!("import os, time\n{output}\ntime.sleep(60)"), &[]);
        let report = timeout(Duration::from_secs(3), child.wait())
            .await
            .unwrap()
            .unwrap();
        assert!(report.forced);
        assert!(report.protocol_error.is_some());
        assert_eq!(report.status.signal(), Some(libc::SIGKILL));
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
    let child = python(
        r#"
import os, signal, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
os.write(5, b'ready\n')
time.sleep(60)
"#,
        &[],
    );
    timeout(Duration::from_secs(3), child.next_event())
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
    let child = python(
        r#"
import os, signal, sys, time
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
os.write(5, b'ready\n')
time.sleep(60)
"#,
        &[],
    );
    timeout(Duration::from_secs(3), child.next_event())
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
    let child = python(
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
