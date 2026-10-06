default:
    @just --list

sync:
    uv sync --all-groups
    uv run prek install

fmt:
    cargo fmt --all
    uv run --locked ruff format .
    uv run --locked rumdl fmt .

lint: fmt
    uv run --locked ruff check --fix .
    uv run --locked rumdl check --fix .

tc: lint
    cargo clippy --locked --workspace --all-targets -- -D warnings
    uv run --locked ty check python

_test-core: tc
    cargo nextest run --locked -p dst-server --all-targets
    cargo build --locked -p dst-server --bin dst-server --example rpc_fixture
    uv run --locked --only-group dev pytest tests/lua

test: _test-core
    uv sync --locked --all-groups --reinstall-package dst-server
    uv run --locked --no-sync sh tests/rust/run_sdk_checks.sh python "$PWD/target/debug/examples/rpc_fixture" "$PWD/target/debug/dst-server"

build: _test-core
    uv build --clear --out-dir dist --no-create-gitignore --no-sources -C 'maturin.build-args=--compatibility pypi'
    uv run --isolated --no-project --no-config --with dist/dst_server-*.whl -- sh tests/rust/run_sdk_checks.sh python "$PWD/target/debug/examples/rpc_fixture" "$PWD/target/debug/dst-server"

# Verify the packaged wheel and repository hooks.
verify: build
    uv run --locked prek run --all-files --show-diff-on-failure --skip just-check

check:
    uv lock --check
    cargo fmt --all -- --check
    cargo clippy --locked --workspace --all-targets -- -D warnings
    uv run --locked ruff format --check .
    uv run --locked ruff check --no-fix .
    uv run --locked ty check python
    uv run --locked rumdl check .

clean:
    fd -I -t d -F __pycache__ -x rm -rf
    rm -rf dist/
    uv run ruff clean
    uv run rumdl clean
