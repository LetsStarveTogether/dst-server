import stat
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import orjson
import pytest

from dst_server import scripts
from dst_server.cli import main


@pytest.fixture
def native(tmp_path: Path) -> Path:
    path = tmp_path / "native.zip"
    with ZipFile(path, "w") as archive:
        archive.comment = b"native archive comment"
        for name, content in {
            "scripts/main.lua": b'require("globalvariableoverrides")\r\n',
            scripts.ENTRYPOINT: scripts.ORIGINAL_ENTRYPOINT,
            "scripts/components/native.lua": b"-- native\r\nreturn '\xff'\x00\n",
        }.items():
            info = ZipInfo(name, (2026, 9, 16, 1, 2, 4))
            info.compress_type = ZIP_DEFLATED
            info.external_attr = (stat.S_IFREG | 0o640) << 16
            info.comment = b"native member comment"
            archive.writestr(info, content)
    path.chmod(0o640)
    return path


def rewrite(path: Path, changes: dict[str, bytes]) -> None:
    with ZipFile(path) as archive:
        entries = [(info, archive.read(info)) for info in archive.infolist()]
        comment = archive.comment
    with ZipFile(path, "w") as archive:
        archive.comment = comment
        for info, content in entries:
            archive.writestr(info, changes.pop(info.filename, content))
        for name, content in changes.items():
            archive.writestr(name, content)


def test_bundle_preserves_native_bytes_metadata_and_source(native: Path) -> None:
    original = native.read_bytes()
    output = native.with_name("managed.zip")
    result = scripts.build_bundle(native, output)

    assert native.read_bytes() == original
    assert stat.S_IMODE(output.stat().st_mode) == 0o640
    assert scripts.verify_bundle(output, source=native) == result
    with ZipFile(native) as source, ZipFile(output) as bundled:
        assert source.comment == bundled.comment
        assert result.native_files == len(source.infolist())
        assert result.sdk_files > 0
        for info in source.infolist():
            expected = (
                scripts.BOOTSTRAP
                if info.filename == scripts.ENTRYPOINT
                else source.read(info)
            )
            assert bundled.read(info.filename) == expected
            actual = bundled.getinfo(info.filename)
            assert (actual.date_time, actual.comment, actual.external_attr) == (
                info.date_time,
                info.comment,
                info.external_attr,
            )
        assert bundled.read("scripts/dst_server/bootstrap.lua")
        assert bundled.read("scripts/components/dst_server_runtime.lua")

    before = output.read_bytes()
    assert scripts.build_bundle(output, output) == result
    assert output.read_bytes() == before


def test_managed_bundle_upgrades_sdk_and_removes_old_modules(
    native: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = scripts._payload()
    old = payload | {"scripts/dst_server/obsolete.lua": b"return {}\n"}
    monkeypatch.setattr(scripts, "_payload", lambda: old)
    monkeypatch.setattr(scripts, "version", lambda _: "old")
    output = native.with_name("managed.zip")
    before = scripts.build_bundle(native, output)

    updated = payload | {"scripts/dst_server/new.lua": b"return {version=2}\n"}
    monkeypatch.setattr(scripts, "_payload", lambda: updated)
    monkeypatch.setattr(scripts, "version", lambda _: "new")
    with pytest.raises(ValueError, match="installed SDK"):
        scripts.verify_bundle(output)
    after = scripts.build_bundle(output, output)
    assert after.sdk_version == "new"
    assert after.source_digest == before.source_digest
    assert scripts.verify_bundle(output, source=native) == after
    with ZipFile(output) as archive:
        assert "scripts/dst_server/obsolete.lua" not in archive.namelist()
        assert (
            archive.read("scripts/dst_server/new.lua")
            == updated["scripts/dst_server/new.lua"]
        )


@pytest.mark.parametrize(
    ("name", "content", "error"),
    [
        (scripts.ENTRYPOINT, b"-- upstream changed\n", "entrypoint"),
        ("scripts/main.lua", b"return {}\n", "no longer loads"),
        (
            "scripts/main.lua",
            b'-- require("globalvariableoverrides")\n',
            "no longer loads",
        ),
        (
            "scripts/main.lua",
            b'if false then require("globalvariableoverrides") end\n',
            "no longer loads",
        ),
        ("scripts/dst_server/custom.lua", b"return {}\n", "SDK namespace"),
        ("scripts/../outside.lua", b"return {}\n", "unsupported"),
    ],
)
def test_reject_changed_startup_or_unsafe_source_without_replacing_output(
    native: Path, name: str, content: bytes, error: str
) -> None:
    rewrite(native, {name: content})
    output = native.with_name("managed.zip")
    output.write_bytes(b"existing output")
    with pytest.raises(ValueError, match=error):
        scripts.build_bundle(native, output)
    assert output.read_bytes() == b"existing output"
    assert not list(native.parent.glob(".*.tmp"))


@pytest.mark.parametrize(
    "name",
    [
        "scripts/components/native.lua",
        "scripts/dst_server/bootstrap.lua",
        scripts.ENTRYPOINT,
    ],
)
def test_verify_and_rebuild_reject_tampering(native: Path, name: str) -> None:
    output = native.with_name("managed.zip")
    scripts.build_bundle(native, output)
    rewrite(output, {name: b"tampered\n"})
    before = output.read_bytes()
    with pytest.raises(ValueError, match="manifest"):
        scripts.verify_bundle(output)
    with pytest.raises(ValueError, match="manifest"):
        scripts.build_bundle(output, output)
    assert output.read_bytes() == before


def test_verify_checks_trusted_native_source(native: Path) -> None:
    output = native.with_name("managed.zip")
    scripts.build_bundle(native, output)
    rewrite(native, {"scripts/components/native.lua": b"different game version\n"})
    with pytest.raises(ValueError, match="supplied native source"):
        scripts.verify_bundle(output, source=native)


@pytest.mark.parametrize("in_place", [False, True])
def test_failed_final_verification_preserves_output_and_cleans_temporary_file(
    native: Path, monkeypatch: pytest.MonkeyPatch, *, in_place: bool
) -> None:
    output = native if in_place else native.with_name("managed.zip")
    if not in_place:
        output.write_bytes(b"existing output")
    original, before = native.read_bytes(), output.read_bytes()

    def reject(_path: Path) -> scripts.ScriptBundle:
        message = "final verification failed"
        raise ValueError(message)

    monkeypatch.setattr(scripts, "verify_bundle", reject)
    with pytest.raises(ValueError, match="final verification failed"):
        scripts.build_bundle(native, output)
    assert native.read_bytes() == original
    assert output.read_bytes() == before
    assert not list(native.parent.glob(".*.tmp"))


def test_scripts_cli_build_verify_and_failure(
    native: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DST_SERVER_DEBUG", raising=False)
    output = native.with_name("managed.zip")
    assert (
        main(["--json", "scripts", "build", str(native), "--output", str(output)]) == 0
    )
    built = capsys.readouterr()
    result = orjson.loads(built.out)
    assert result["path"] == str(output)
    assert result["entrypoint"] == scripts.ENTRYPOINT
    assert built.err == ""
    assert (
        main(["--json", "scripts", "verify", str(output), "--source", str(native)]) == 0
    )
    assert orjson.loads(capsys.readouterr().out) == result

    rewrite(output, {"scripts/components/native.lua": b"tampered\n"})
    assert main(["--json", "scripts", "verify", str(output)]) == 1
    failed = capsys.readouterr()
    assert failed.out == ""
    assert "manifest" in failed.err
