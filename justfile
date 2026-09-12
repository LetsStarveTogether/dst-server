default:
    @just --list

sync:
    uv sync --all-extras --all-groups
    uv run prek install

fmt:
    uv run --locked ruff format .
    uv run --locked rumdl fmt .

lint: fmt
    uv run --locked ruff check --fix .
    uv run --locked rumdl check --fix .

tc: lint
    uv run --locked --all-extras ty check

test: tc
    uv run --locked --all-extras pytest

build: test
    uv build --clear --out-dir dist --no-create-gitignore --no-sources

# Run the full dependency chain, repository hooks, and isolated wheel check.
verify: build
    uv run --locked prek run --all-files --show-diff-on-failure --skip just-check
    uv run --isolated --no-project --no-config --with dist/dst_server-*.whl -- python -I tests/distribution.py

# Run the real game against an explicitly selected local image.
test-system image:
    DST_SERVER_IMAGE="{{ image }}" uv run --locked --all-extras pytest -m system tests/system

# Also verify the OTLP round trip against a configured local Netdata.
test-netdata-system image:
    DST_SERVER_NETDATA_TEST=1 just test-system "{{ image }}"

check:
    uv lock --check
    uv run --locked ruff format --check .
    uv run --locked ruff check --no-fix .
    uv run --locked --all-extras ty check
    uv run --locked rumdl check .

clean:
    fd -I -t d -F __pycache__ -x rm -rf
    rm -rf dist/ .pytest_cache/
    uv run ruff clean
    uv run rumdl clean
