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

tc:
    uv run --all-extras ty check

test:
    uv run --locked --all-extras pytest

# Run the real game against an explicitly selected local image.
test-system image:
    DST_SERVER_IMAGE="{{ image }}" uv run --locked --all-extras pytest -m system tests/system

# Also verify the OTLP round trip against a configured local Netdata.
test-netdata-system image:
    DST_SERVER_NETDATA_TEST=1 just test-system "{{ image }}"

check:
    uv lock --check
    uv run ruff format --check
    uv run ruff check
    uv run --all-extras ty check
    uv run rumdl check

build:
    uv build --no-create-gitignore --no-sources

# The same validation gate is used locally, for PRs, and before publishing.
verify:
    uv run --locked prek run --all-files
    just test build
    uv run --isolated --no-project --with dist/dst_server-*.whl python -I tests/distribution.py

clean:
    fd -I -t d -F __pycache__ -x rm -rf
    rm -rf dist/ .pytest_cache/
    uv run ruff clean
    uv run rumdl clean
