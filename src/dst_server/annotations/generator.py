from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
from itertools import repeat
from pathlib import Path

from .visitors import DEFAULT_VAR, parse_component, parse_modutil


def extract_class_name(content: str) -> str | None:
    for source_line in reversed(content.splitlines()):
        line = source_line.strip()
        if line.startswith("return "):
            return line.removeprefix("return ").split(maxsplit=1)[0].rstrip(",")
    return None


def component_definition(path: Path, folder_name: str) -> tuple[str, str]:
    content = path.read_text(encoding="utf-8")
    class_name = extract_class_name(content) or path.stem
    fields, definitions = parse_component(
        content,
        path.stem,
        class_name,
        folder_name,
    )

    lines = [f"---@class {class_name}", *fields]
    lines.extend((
        f"local {DEFAULT_VAR}={{}}",
        f"{folder_name}.{path.stem}={DEFAULT_VAR}",
        "",
        *definitions,
        "",
    ))
    return path.stem, "\n".join(lines)


def generate_components(input_dir: Path, max_workers: int | None = None) -> str:
    if not input_dir.is_dir():
        raise NotADirectoryError(input_dir)

    files = sorted(input_dir.rglob("*.lua"))
    if max_workers == 1:
        results = [component_definition(path, input_dir.name) for path in files]
    else:
        with ProcessPoolExecutor(max_workers=max_workers) as executor:
            results = list(
                executor.map(component_definition, files, repeat(input_dir.name))
            )

    definitions = sorted(results)
    return "\n".join(content for _, content in definitions)


def generate_modutil(input_file: Path) -> str:
    content = input_file.read_text(encoding="utf-8")
    definitions = parse_modutil(content, input_file.stem)
    return "\n".join(definitions) + ("\n" if definitions else "")
