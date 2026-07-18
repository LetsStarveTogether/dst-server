from __future__ import annotations

import argparse
import logging
from collections.abc import Sequence
from pathlib import Path

from .generator import generate_components, generate_modutil

logger = logging.getLogger(__name__)


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate LSP-compatible Lua annotations."
    )
    parser.add_argument("input", type=Path, help="Components directory or modutil file")
    parser.add_argument("-o", "--output", type=Path, help="Output Lua file")
    parser.add_argument("-m", "--max-workers", type=int, help="Worker process count")
    parser.add_argument(
        "--mode",
        choices=("auto", "components", "modutil"),
        default="auto",
    )
    return parser


def resolve_mode(
    parser: argparse.ArgumentParser,
    input_path: Path,
    requested: str,
) -> str:
    if requested != "auto":
        return requested
    if input_path.is_dir():
        return "components"
    if input_path.is_file() and "modutil" in input_path.name.lower():
        return "modutil"
    return parser.error("Cannot infer mode; pass --mode explicitly.")


def write_components(input_path: Path, output: Path | None, workers: int | None) -> int:
    content = generate_components(input_path, workers)
    if not content:
        logger.error("No Lua component definitions generated from %s", input_path)
        return 1

    output_path = output or Path(f"{input_path.name}_def.lua")
    output_path.write_text(content, encoding="utf-8")
    logger.info("Output written to %s", output_path)
    return 0


def write_modutil(input_path: Path, output: Path | None) -> int:
    if not input_path.is_file():
        raise FileNotFoundError(input_path)

    content = generate_modutil(input_path)
    if not content:
        logger.error("No definitions found in %s", input_path)
        return 1

    output_path = output or Path(f"{input_path.stem}_def.lua")
    output_path.write_text(content, encoding="utf-8")
    logger.info("Output written to %s", output_path)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = create_parser()
    args = parser.parse_args(argv)
    mode = resolve_mode(parser, args.input, args.mode)

    try:
        if mode == "components":
            return write_components(args.input, args.output, args.max_workers)
        return write_modutil(args.input, args.output)
    except Exception as error:
        parser.error(str(error))
