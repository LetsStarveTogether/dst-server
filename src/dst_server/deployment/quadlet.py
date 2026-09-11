import shlex
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from dst_server.configuration.files import read_text

from .models import (
    PortMapping,
    QuadletUnit,
    UnitField,
    VolumeMount,
    _validate_unique,
    _validate_unit_name,
)


def _escape_expansions(value: str) -> str:
    return value.replace("\\", "\\\\").replace("$", "$$").replace("%", "%%")


def _collapse_expansions(value: str) -> str:
    return value.replace("$$", "$").replace("%%", "%")


def _collapse_literal(value: str) -> str:
    return _collapse_expansions(value.replace("\\\\", "\\"))


def _literal_expansions(value: str, label: str) -> str:
    index = 0
    while index < len(value):
        character = value[index]
        if character in "\\$%":
            if index + 1 >= len(value) or value[index + 1] != character:
                msg = (
                    f"dynamic systemd expansion or escape is unsupported in "
                    f"{label}: {value!r}"
                )
                raise ValueError(msg)
            index += 1
        index += 1
    return _collapse_literal(value)


type _ParsedSections = dict[str, dict[str, list[str]]]
type _Schema = Mapping[str, Mapping[str, bool]]


def _parse_sections(
    text: str,
    source_name: str,
    schema: _Schema,
    required: str,
) -> _ParsedSections:
    parsed: _ParsedSections = {}
    section = ""
    for number, source in enumerate(text.split("\n"), 1):
        line = source.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            if section not in schema:
                msg = f"unknown Quadlet section at {source_name}:{number}: {section}"
                raise ValueError(msg)
            parsed.setdefault(section, {})
            continue
        if not section or "=" not in line:
            msg = f"invalid Quadlet line at {source_name}:{number}: {source!r}"
            raise ValueError(msg)
        key, value = (item.strip() for item in line.split("=", 1))
        repeat = schema[section].get(key)
        if repeat is None:
            msg = f"unknown Quadlet key at {source_name}:{number}: {section}.{key}"
            raise ValueError(msg)
        section_values = parsed[section]
        if not repeat and key in section_values:
            msg = (
                f"duplicate Quadlet singleton at {source_name}:{number}: "
                f"{section}.{key}"
            )
            raise ValueError(msg)
        section_values.setdefault(key, []).append(value)
    if required not in parsed:
        msg = f"missing Quadlet section: {required}"
        raise ValueError(msg)
    return parsed


def _unit_list(values: tuple[str, ...], label: str) -> tuple[str, ...]:
    result = tuple(item for value in values for item in value.split())
    if not result:
        msg = f"invalid Quadlet {label}"
        raise ValueError(msg)
    _validate_unique(result, label)
    return result


def _shell_split(value: str, label: str) -> list[str]:
    try:
        return shlex.split(value)
    except ValueError as error:
        msg = f"invalid Quadlet {label}: {value!r}"
        raise ValueError(msg) from error


def _command(value: str) -> tuple[str, ...]:
    _literal_expansions(value, "Quadlet Exec")
    command = tuple(_collapse_literal(item) for item in _shell_split(value, "Exec"))
    if not command:
        msg = "Quadlet Exec cannot be empty"
        raise ValueError(msg)
    return command


def _boolean(value: str) -> bool:
    match value.casefold():
        case "yes" | "true" | "on" | "1":
            return True
        case "no" | "false" | "off" | "0":
            return False
    msg = f"invalid systemd boolean: {value!r}"
    raise ValueError(msg)


def _integer(value: str, label: str) -> int:
    try:
        return int(value)
    except ValueError as error:
        msg = f"invalid Quadlet {label}: {value!r}"
        raise ValueError(msg) from error


def _environment(values: tuple[str, ...]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        _literal_expansions(value, "Quadlet Environment")
        assignments = _shell_split(value, "Environment")
        if not assignments:
            msg = "Quadlet Environment cannot be empty"
            raise ValueError(msg)
        for source in assignments:
            assignment = _collapse_literal(source)
            name, separator, item = assignment.partition("=")
            if not separator or name in result:
                msg = f"invalid or duplicate Quadlet environment: {name!r}"
                raise ValueError(msg)
            result[name] = item
    return result


def _single_token(value: str, label: str) -> str:
    tokens = _shell_split(value, label)
    if len(tokens) != 1:
        msg = f"invalid Quadlet {label}: {value!r}"
        raise ValueError(msg)
    return tokens[0]


def _literal_token(value: str, label: str) -> str:
    _literal_expansions(value, label)
    return _collapse_expansions(_single_token(value, label))


def _unit_reference(value: str, label: str) -> str:
    if value[:1] in {'"', "'"} and value[-1:] == value[:1]:
        value = value[1:-1]
    if not value or any(character.isspace() for character in value):
        msg = f"invalid Quadlet {label}: {value!r}"
        raise ValueError(msg)
    return value


def _network_references(values: tuple[str, ...]) -> tuple[str, ...]:
    result = []
    for value in values:
        reference = _unit_reference(value, "Network")
        result.append(
            reference
            if reference.endswith(".network")
            else _literal_token(value, "Network")
        )
    return tuple(result)


def _render_sections(
    sections: tuple[tuple[str, list[str]], ...],
    *,
    required: frozenset[str] = frozenset(),
) -> str:
    populated = [
        f"[{name}]" + ("\n" + "\n".join(lines) if lines else "")
        for name, lines in sections
        if lines or name in required
    ]
    return "\n\n".join(populated) + ("\n" if populated else "")


def _unit_name(path: Path, suffix: str) -> str:
    if not path.name.endswith(suffix):
        msg = f"expected a {suffix} Quadlet unit: {path}"
        raise ValueError(msg)
    return _validate_unit_name(path.name[: -len(suffix)])


def references_pod(path: Path, pod_source: str) -> bool:
    try:
        lines = path.read_text(encoding="utf-8").split("\n")
    except OSError, UnicodeError:
        return False
    section = ""
    for source in lines:
        line = source.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        if section == "Container" and "=" in line:
            key, value = (item.strip() for item in line.split("=", 1))
            if key == "Pod":
                try:
                    return _unit_reference(value, "Pod") == pod_source
                except ValueError:
                    return False
    return False


def drop_ins(path: Path) -> tuple[Path, ...]:
    """Return local drop-ins in systemd order, with specific names taking priority."""
    directories = (
        f"{path.suffix[1:]}.d",
        *(
            f"{path.stem[: index + 1]}{path.suffix}.d"
            for index, character in enumerate(path.stem)
            if character == "-"
        ),
        f"{path.name}.d",
    )
    files = {
        source.name: source
        for directory in directories
        for source in (path.parent / directory).glob("*.conf")
    }
    return tuple(files[name] for name in sorted(files))


def validate_update(path: Path, updated: QuadletUnit) -> None:
    if not path.exists() and not path.is_symlink():
        return
    previous = type(updated).load(path)
    changed = {
        (field.section, field.key)
        for name, field in _fields(type(updated))
        if getattr(previous, name) != getattr(updated, name)
    }
    if not changed:
        return
    for source in drop_ins(path):
        section = ""
        for raw in read_text(source).splitlines():
            line = raw.strip()
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1]
            elif not line.startswith(("#", ";")) and "=" in line:
                key = line.partition("=")[0].strip()
                if (section, key) in changed:
                    msg = f"Quadlet {section}.{key} is overridden by {source}"
                    raise ValueError(msg)


def _fields(model: type[QuadletUnit]) -> tuple[tuple[str, UnitField], ...]:
    return tuple(
        (name, metadata)
        for name, field in model.model_fields.items()
        for metadata in field.metadata
        if isinstance(metadata, UnitField)
    )


def _decode_value(  # ruff: ignore[complex-structure, too-many-branches]
    field: UnitField, values: list[str]
) -> object:
    value = values[0]
    label = f"{field.section}.{field.key}"
    match field.kind:
        case "text":
            return _literal_expansions(value, label)
        case "token":
            return _literal_token(value, label)
        case "reference":
            return _unit_reference(value, label)
        case "image":
            reference = _unit_reference(value, label)
            return (
                reference
                if reference.endswith((".build", ".image"))
                else _literal_token(value, label)
            )
        case "units":
            return _unit_list(tuple(values), field.key)
        case "networks":
            return _network_references(tuple(values))
        case "ports":
            return tuple(
                PortMapping.parse(_single_token(item, field.key)) for item in values
            )
        case "volumes":
            return tuple(
                VolumeMount.parse(_literal_expansions(item, label)) for item in values
            )
        case "environment":
            return _environment(tuple(values))
        case "command":
            return _command(value)
        case "integer":
            return _integer(value, field.key)
        case "boolean" | "yesno":
            return _boolean(value)
        case _:
            return value


def parse[T: QuadletUnit](text: str, model: type[T], *, name: str) -> T:
    fields = _fields(model)
    schema: dict[str, dict[str, bool]] = {}
    for _, field in fields:
        schema.setdefault(field.section, {})[field.key] = field.repeat
    parsed = _parse_sections(text, name, schema, model.section)
    values: dict[str, object] = {"name": name}
    for field_name, field in fields:
        if items := parsed.get(field.section, {}).get(field.key):
            values[field_name] = _decode_value(field, items)
        elif model.model_fields[field_name].is_required():
            msg = f"missing Quadlet key: {field.section}.{field.key}"
            raise ValueError(msg)
    return model.model_validate(values)


def load[T: QuadletUnit](path: Path, model: type[T]) -> T:
    if path.is_symlink():
        msg = f"Quadlet unit cannot be a symlink: {path}"
        raise ValueError(msg)
    if not path.is_file():
        raise FileNotFoundError(path)
    try:
        source = read_text(path)
    except (OSError, UnicodeError) as error:
        msg = f"invalid Quadlet unit: {path}: {error}"
        raise ValueError(msg) from error
    return parse(source, model, name=_unit_name(path, f".{model.section.lower()}"))


def _encode_value(  # ruff: ignore[complex-structure]
    field: UnitField, value: Any
) -> tuple[str, ...]:
    match field.kind:
        case "text" | "token":
            return (_escape_expansions(value),)
        case "image":
            return (
                value
                if value.endswith((".build", ".image"))
                else _escape_expansions(value),
            )
        case "units":
            return value
        case "networks":
            return tuple(
                item if item.endswith(".network") else _escape_expansions(item)
                for item in value
            )
        case "ports":
            return tuple(item.render() for item in value)
        case "volumes":
            return tuple(_escape_expansions(item.render()) for item in value)
        case "environment":
            return tuple(
                shlex.quote(_escape_expansions(f"{name}={item}"))
                for name, item in sorted(value.items())
            )
        case "command":
            return (shlex.join(tuple(map(_escape_expansions, value))),)
        case "boolean":
            return ("true" if value else "false",)
        case "yesno":
            return ("yes" if value else "no",)
        case _:
            return (str(value),)


def render(unit: QuadletUnit) -> str:
    sections = {name: [] for name in ("Unit", unit.section, "Service", "Install")}
    for name, field in _fields(type(unit)):
        value = getattr(unit, name)
        if name not in unit.model_fields_set or value is None:
            continue
        if not value and not isinstance(value, (bool, int)):
            continue
        sections[field.section].extend(
            f"{field.key}={item}" for item in _encode_value(field, value)
        )
    return _render_sections(tuple(sections.items()), required=frozenset({unit.section}))
