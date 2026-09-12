"""Prepare native game scripts without contacting running rooms."""

from pathlib import Path

from cyclopts import App

from dst_server.scripts import build_bundle, verify_bundle

from .common import emit

scripts_app = App(name="scripts", help="Build and verify native SDK script bundles.")


@scripts_app.command
def build(source: Path, *, output: Path) -> None:
    """Embed SDK startup in scripts.zip; output may replace source before startup."""
    emit(build_bundle(source, output))


@scripts_app.command
def verify(path: Path, *, source: Path | None = None) -> None:
    """Verify the managed bundle and optionally its trusted native source archive."""
    emit(verify_bundle(path, source=source))
