default:
    @just --list

sync:
    uv sync --all-extras --all-groups
    uv run prek install

fmt:
    uv run ruff format
    uv run rumdl fmt

lint: fmt
    uv run ruff check --fix
    uv run rumdl check --fix

tc: lint
    uv run --all-extras ty check

test:
    uv run --locked --all-extras pytest

# Run the real game against an explicitly selected local image without network access.
test-system image:
    DST_SERVER_IMAGE="{{ image }}" DST_SERVER_PODMAN_TEST=1 uv run --locked --all-extras pytest -m system tests/test_podman.py

# Query a real SteamCMD installation separately because it requires host network access.
test-steamcmd-system:
    DST_SERVER_STEAMCMD_TEST=1 uv run --locked --all-extras pytest -m system tests/test_steamcmd.py

check:
    uv lock --check
    uv run ruff format --check
    uv run ruff check
    uv run --all-extras ty check

build: test
    uv build --no-create-gitignore --no-sources

clean:
    fd -I -t d -F __pycache__ -x rm -rf
    rm -rf dist/ .pytest_cache/
    uv run ruff clean
    uv run rumdl clean
