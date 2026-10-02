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

test: tc
    cargo test --locked -p dst-server --all-targets
    cargo build --locked -p dst-server --bin dst-server --example rpc_fixture
    uv sync --locked --all-groups --reinstall-package dst-server
    uv run --locked python tests/rust/check_lua.py
    uv run --locked python tests/rust/check_binding.py target/debug/examples/rpc_fixture target/debug/dst-server
    for check in tests/rust/check_*.py; do case "$check" in */check_binding.py|*/check_lua.py) continue ;; esac; uv run --locked python "$check"; done
    for check in tests/rust/check_*_cli.py; do uv run --locked python "$check" target/debug/dst-server; done

build: test
    uv build --clear --out-dir dist --no-create-gitignore --no-sources -C 'maturin.build-args=--compatibility pypi'

# Run the full dependency chain, repository hooks, and isolated wheel check.
verify: build
    uv run --locked prek run --all-files --show-diff-on-failure --skip just-check
    uv run --isolated --no-project --no-config --with dist/dst_server-*.whl -- python -I tests/rust/check_binding.py target/debug/examples/rpc_fixture target/debug/dst-server

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
