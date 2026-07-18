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

test: tc
    uv run --all-extras pytest

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
