"""Check CLI log filters, records and signal-driven child cleanup."""

# Each command and executable belongs to the disposable test directory.
# ruff: file-ignore[suspicious-subprocess-import, subprocess-without-shell-equals-true]

import json
import os
import select
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import suppress
from pathlib import Path


def executable(directory: Path, name: str, body: str) -> Path:
    path = directory / name
    path.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys, time\n"
        "path = pathlib.Path(sys.argv[0])\n"
        "path.with_suffix('.args').write_text(json.dumps(sys.argv[1:]))\n"
        "tmp = path.with_suffix(f'.{os.getpid()}.tmp')\n"
        "tmp.write_text(str(os.getpid()))\n"
        "tmp.replace(path.with_suffix('.pid'))\n" + body,
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def command(*arguments: str) -> list[str]:
    program = sys.argv[1:] or [sys.executable, "-I", "-m", "dst_server"]
    return [*program, "--json", "logs", *arguments]


def reaped(reader: Path) -> None:
    pid = int(reader.with_suffix(".pid").read_text())
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return
    message = f"log CLI returned before reaping native reader {pid}"
    raise AssertionError(message)


JOURNAL = """
record = {'__CURSOR': 'first', '__REALTIME_TIMESTAMP': '1000001',
          'MESSAGE': '玩家\\nsecond line', '_SYSTEMD_UNIT': 'dst-007.service'}
if '--follow' in sys.argv:
    print('reader diagnostic', file=sys.stderr, flush=True)
print(json.dumps(record), flush=True)
if '--follow' in sys.argv:
    time.sleep(60)
else:
    record['__CURSOR'] = 'second'
    print(json.dumps(record))
"""


def check_journal(directory: Path) -> None:
    reader = executable(directory, "journal", JOURNAL)
    query = {
        "limit": 1,
        "direction": "forward",
        "since": "2026-10-01",
        "until": "now",
        "namespace": "games",
        "grep": "--literal",
    }
    result = subprocess.run(
        command(
            "journal",
            "--executable",
            str(reader),
            "--user",
            "--unit",
            "dst-007.service",
            "--query",
            "-",
        ),
        input=json.dumps(query),
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    page = json.loads(result.stdout)
    assert page["has_more"]
    assert page["next_cursor"] == "first"
    assert page["records"][0]["fields"]["MESSAGE"] == "玩家\nsecond line"
    arguments = json.loads(reader.with_suffix(".args").read_text())
    assert "--user" not in arguments
    assert "--user-unit=dst-007.service" in arguments
    assert not any(arg.startswith("--lines=") for arg in arguments)
    for key in ("since", "until", "namespace", "grep"):
        assert f"--{key}={query[key]}" in arguments
    reaped(reader)

    for stop in (signal.SIGINT, signal.SIGTERM):
        process = subprocess.Popen(
            command(
                "journal",
                "--executable",
                str(reader),
                "--follow",
                "--query",
                '{"grep":"players"}',
            ),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            assert process.stdout is not None
            assert select.select([process.stdout], [], [], 10)[0]
            record = json.loads(process.stdout.readline())
            assert record["fields"]["__CURSOR"] == "first"
            arguments = json.loads(reader.with_suffix(".args").read_text())
            assert "--system" in arguments
            assert "--follow" in arguments
            assert "--lines=0" in arguments
            assert "--reverse" not in arguments
            process.send_signal(stop)
            _, errors = process.communicate(timeout=10)
            assert process.returncode == 0, errors
            assert json.loads(errors)["diagnostics"] == "reader diagnostic"
            reaped(reader)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


def check_history(directory: Path) -> None:
    reader = executable(
        directory,
        "history",
        """
args = dict(arg.split('=', 1) for arg in sys.argv[1:] if '=' in arg)
assert '--unit=dst-007.service' in sys.argv and '--user' not in sys.argv
for n, player in enumerate(('other', 'KU_a.b'), start=1):
    if n < int(args.get('--cursor', 1)):
        continue
    log = {'event_name':'dst.client.authenticated', 'body':{'userid':player},
           'observed_timestamp_ns':10**9, 'attributes':{
               'log.record.uid':str(n).zfill(26), 'dst.cluster.name':'seven',
               'dst.shard.name':'Forest', 'dst.game.attempt.id':'0'*26,
               'dst.runtime.generation':1, 'dst.event.sequence':n, 'dst.tick':n,
               'dst.monotonic_ms':n}}
    print(json.dumps({'__CURSOR':str(n), '__REALTIME_TIMESTAMP':str(10**6+n),
                      '_SYSTEMD_UNIT':'dst-007.service', '_UID':str(os.getuid()),
                      'MESSAGE':'DST_RECORD|'+json.dumps(log)}))
""",
    )
    args = command(
        "history",
        "--executable",
        str(reader),
        "--room",
        "7",
        "--since",
        "1970-01-01T00:00:01Z",
        "--until",
        "1970-01-01T00:00:02Z",
        "--event",
        "dst.client.authenticated",
        "--shard",
        "Forest",
        "--userid",
        "KU_a.b",
        "--page-size",
        "1",
    )

    def page(*extra: str) -> dict:
        result = subprocess.run(
            [*args, *extra], capture_output=True, text=True, check=True, timeout=10
        )
        return json.loads(result.stdout)

    first = page()
    assert first["has_more"]
    assert first["next_cursor"] == "1"
    assert first["observations"] == []
    assert first["candidates_read"] == 1
    last = page("--checkpoint", json.dumps(first["checkpoint"]))
    assert not last["has_more"]
    assert last["next_cursor"] == "2"
    (event,) = last["observations"]
    assert event["unit"] == "dst-007.service"
    assert event["uid"] == str(2).zfill(26)
    assert event["event"]["data"]["userid"] == "KU_a.b"


def check_netdata(directory: Path) -> None:
    reader = executable(
        directory,
        "otel-plugin",
        """
assert sys.argv[1] == 'logs'
print(json.dumps({'timestamp_ns': 18446744073709551615,
                  'fields': [['tag', 'a'], ['tag', 'b']]}))
print('matched=5 returned=1 window=1..2', file=sys.stderr)
""",
    )
    query = directory / "query.json"
    query.write_text(
        json.dumps({
            "since": 1,
            "until": 2,
            "service_name": "dst",
            "service_namespace": "007",
            "userid": "KU_example",
            "filters": [["tag", "player"], ["body.userid", "KU_example"]],
            "fields": ["tag", "body.userid"],
            "limit": 1,
        }),
        encoding="utf-8",
    )
    result = subprocess.run(
        command(
            "telemetry",
            f"@{query}",
            "--executable",
            str(reader),
            "--stock-config",
            str(directory / "stock.yaml"),
            "--config",
            str(directory / "otel.yaml"),
            "--max-concurrency",
            "2",
        ),
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    page = json.loads(result.stdout)
    assert page["matched"] == 5
    assert page["truncated"] is True
    assert page["records"][0]["timestamp_ns"] == 18_446_744_073_709_551_615
    assert page["records"][0]["fields"] == [["tag", "a"], ["tag", "b"]]
    arguments = json.loads(reader.with_suffix(".args").read_text())
    assert "--filter=tag=player,body.userid=KU_example" in arguments
    assert "--fields=tag,body.userid" in arguments
    assert "--name=dst" in arguments
    assert "--namespace=007" in arguments
    assert any(
        value.startswith("--query=^body\\.") and value.endswith("=KU_example$")
        for value in arguments
    )
    reaped(reader)


def check_structured_search(directory: Path) -> None:
    reader = executable(
        directory,
        "projected",
        """
args = dict(a.split('=', 1) for a in sys.argv[2:])
coverage = 'event_name=dst.telemetry.coverage' in args['--filter']
if args['--name'] in ('bad-output', 'stalled'):
    print('x'*70000, 'WARN skipped index', file=sys.stderr, flush=True)
    if args['--name'] == 'stalled':
        time.sleep(60)
    print('invalid' if coverage else 'x'*1000)
    sys.exit(0)
if chr(10) in args['--name']:
    name, ns, tenant = args['--name'], args['--namespace'], args['--tenant']
    empty = f'no WAL/SFST files matched (tenant={tenant}, window=1..2, stream='
    if coverage:
        print('WARN skipped index', file=sys.stderr)
    if name.startswith('forged'):
        empty = 'warning contains ' + empty
    print(empty + f'{ns}/{name})', file=sys.stderr)
    sys.exit(0)
if args['--name'] == 'literal':
    import re
    player = 'KU' + chr(9) + chr(0) + '.1'
    gameplay = '--query' in args
    if gameplay:
        assert re.fullmatch(args['--query'], 'body.player.userid=' + player)
        assert not re.fullmatch(args['--query'], 'body.victim.userid=' + player)
        row = [('event_name','dst.player.ate'), ('attributes.log.record.uid','0'*26),
               ('attributes.dst.shard.name','Forest'),
               ('resource.attributes.dst.cluster.name','one'),
               ('resource.attributes.service.instance.id','agent'),
               ('body.player.userid', player)]
        print(json.dumps({'timestamp_ns':10**9, 'fields':row}))
    print(f'matched={int(gameplay)} returned={int(gameplay)} window=1..2',
          file=sys.stderr)
    sys.exit(0)
if args['--name'] == 'quiet':
    if 'event_name=dst.telemetry.coverage' in args['--filter']:
        print('WARN skipped corrupt index', file=sys.stderr)
        print('no WAL/SFST files matched (tenant=default, window=1..2, stream=/quiet)',
              file=sys.stderr)
    else:
        print('matched=0 returned=0 window=1..2', file=sys.stderr)
    sys.exit(0)
if 'event_name=dst.telemetry.coverage' in args['--filter']:
    print('coverage reader failed', file=sys.stderr)
    sys.exit(8)
roles = [
    ('dst.player.ate', [('body.victim.userid','KU_1')]),
    ('dst.player.ate', [('body.player.userid','KU_1'), ('body.player.userid','KU_2')]),
    ('dst.player.ate', []),
    ('dst.server.presence', [('body.players[].userid','KU_2'),
                             ('body.players[].userid','KU_1')]),
]
fields = args['--fields'].split(',')
for n,(event,body) in enumerate(roles, start=1):
    row = [('event_name',event), ('attributes.log.record.uid',str(n).zfill(26)),
           ('attributes.dst.shard.name','Forest'),
           ('resource.attributes.dst.cluster.name','one'),
           ('resource.attributes.service.instance.id','agent'), *body]
    print(json.dumps({'timestamp_ns':10**9,
                      'fields':[p for p in row if p[0] in fields]}))
print('matched=5 returned=4 window=1..2', file=sys.stderr)
""",
    )
    request = {
        "source": {
            "room": 1,
            "service_name": "dst",
            "service_namespace": "",
            "cluster": "one",
            "instance_id": "agent",
        },
        "since": "1970-01-01T00:00:01Z",
        "until": "1970-01-01T00:00:02Z",
        "limit": 4,
        "fields": ["body.players[].userid"],
        "where": {"userid": "KU_1"},
    }

    def search(status: int = 0, *extra: str) -> dict:
        args = command(
            "telemetry",
            "--structured",
            json.dumps(request),
            "--executable",
            str(reader),
            *extra,
        )
        result = subprocess.run(
            args, capture_output=True, text=True, check=False, timeout=10
        )
        assert result.returncode == status, result.stderr
        return json.loads(result.stdout)

    report = search(1)
    page = report["gameplay"]
    assert page["matched_candidates"] == 5
    assert page["returned_candidates"] == 4
    assert page["truncated"]
    assert page["unverified"] == 3
    (hit,) = page["hits"]
    assert hit["event_name"] == "dst.server.presence"
    assert hit["fields"] == [
        ["body.players[].userid", "KU_2"],
        ["body.players[].userid", "KU_1"],
    ]
    assert "status 8" in report["coverage"]["error"]
    assert report["coverage"]["diagnostics"] == "coverage reader failed"

    request["source"]["service_name"] = "quiet"
    report = search()
    assert report["gameplay"]["file_status"] == "files_found"
    assert report["coverage"]["file_status"] == "diagnostics"

    request["source"]["service_name"] = "literal"
    request["where"] = {"events": ["dst.player.ate"], "userid": "KU\t\0.1"}
    assert len(search()["gameplay"]["hits"]) == 1

    for mode in ("bad-output", "stalled"):
        request["source"]["service_name"] = mode
        report = search(
            1, "--max-concurrency", "2", "--max-record-bytes", "500", "--timeout", "2"
        )
        for kind in ("gameplay", "coverage"):
            page = report[kind]
            assert page["diagnostics"].endswith("WARN skipped index")
            assert page["diagnostics_truncated"]
            if mode == "stalled":
                assert "timed out" in page["error"]
            else:
                assert ("NDJSON" if kind == "coverage" else "byte limit") in page[
                    "error"
                ]

    request["source"].update(
        service_name="dst\nstream", service_namespace="ns\n2", tenant="t\n2"
    )
    report = search()
    assert report["gameplay"]["file_status"] == "no_files"
    assert report["coverage"]["file_status"] == "diagnostics"
    request["source"]["service_name"] = "forged\nstream"
    assert "valid query summary" in search(1)["gameplay"]["error"]

    request["since"] = "2016-12-31T23:59:60.2Z"
    request["until"] = "2017-01-01T00:00:01Z"
    invalid = subprocess.run(
        command(
            "telemetry",
            "--structured",
            json.dumps(request),
            "--executable",
            str(reader),
        ),
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert invalid.returncode == 1
    assert "leap seconds" in invalid.stderr


def check_cancelled_queries(directory: Path) -> None:
    reader = executable(directory, "hanging", "time.sleep(60)\n")
    marker = reader.with_suffix(".pid")
    for options in (("journal",), ("telemetry", '{"since":1}')):
        for stop in (signal.SIGINT, signal.SIGTERM):
            marker.unlink(missing_ok=True)
            process = subprocess.Popen(
                command(*options, "--executable", str(reader)),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                deadline = time.monotonic() + 10
                while not marker.exists():
                    assert time.monotonic() < deadline
                    assert process.poll() is None
                    time.sleep(0.01)
                process.send_signal(stop)
                _, errors = process.communicate(timeout=10)
                assert process.returncode == 1, errors
                assert "cancel" in errors.lower(), errors
                reaped(reader)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()


def check_structured_cancellation(directory: Path) -> None:
    query = {
        "source": {
            "room": 1,
            "service_name": "dst",
            "service_namespace": "",
            "cluster": "one",
            "instance_id": "agent",
        },
        "since": "1970-01-01T00:00:01Z",
        "until": "1970-01-01T00:00:02Z",
        "limit": 2,
    }
    for slots in (1, 2):
        work = directory / f"structured-{slots}"
        work.mkdir()
        reader = work / "plugin"
        reader.write_text(
            f"#!{sys.executable}\n"
            "import os, pathlib, signal, sys, time\n"
            "child = os.fork()\n"
            "if child == 0:\n"
            "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "    time.sleep(60)\n"
            "else:\n"
            "    marker = pathlib.Path(sys.argv[0]).with_name(str(os.getpid()))\n"
            "    marker.write_text(str(child))\n"
            "    marker.rename(marker.with_suffix('.ready'))\n"
            "    time.sleep(60)\n"
        )
        reader.chmod(0o755)
        process = subprocess.Popen(
            command(
                "telemetry",
                "--structured",
                json.dumps(query),
                "--executable",
                str(reader),
                "--max-concurrency",
                str(slots),
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.monotonic() + 10
            while len(list(work.glob("*.ready"))) < slots:
                assert time.monotonic() < deadline
                assert process.poll() is None
                time.sleep(0.01)
            process.terminate()
            _, errors = process.communicate(timeout=10)
            assert process.returncode == 1, errors
            assert "cancelled" in errors, errors
            markers = list(work.glob("*.ready"))
            assert len(markers) == slots
            for marker in markers:
                assert not Path("/proc", marker.stem).exists()
                status = Path("/proc", marker.read_text(), "stat")
                try:
                    state = status.read_bytes().rsplit(b")", 1)[1].split()[0]
                except FileNotFoundError:
                    continue
                assert state in {b"Z", b"X"}, (
                    f"reader's descendant is still running ({state})"
                )
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            for marker in work.glob("*.ready"):
                with suppress(ProcessLookupError):
                    os.killpg(int(marker.stem), signal.SIGKILL)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as temporary:
        check_journal(Path(temporary))
        check_history(Path(temporary))
        check_netdata(Path(temporary))
        check_structured_search(Path(temporary))
        check_cancelled_queries(Path(temporary))
        check_structured_cancellation(Path(temporary))
    sys.stdout.write("Native log CLI checks passed\n")
