from configparser import ConfigParser
from dataclasses import dataclass
from functools import cache
from typing import Any

from pydantic import BaseModel, SecretStr


@dataclass(frozen=True, slots=True)
class IniSection:
    name: str


@cache
def _schema(model: type[BaseModel]) -> dict[str, dict[str, str]]:
    sections: dict[str, dict[str, str]] = {}
    for field, definition in model.model_fields.items():
        section = next(
            item.name for item in definition.metadata if isinstance(item, IniSection)
        )
        sections.setdefault(section, {})[field] = field
    for section, fields in getattr(model, "_IGNORED_OPTIONS", {}).items():
        sections.setdefault(section, {}).update({field: field for field in fields})
    return sections


def parse_ini[Settings: BaseModel](  # ruff: ignore[complex-structure]
    source: str,
    model: type[Settings],
    *,
    include: frozenset[str] | None = None,
) -> Settings:
    parser = ConfigParser(interpolation=None)
    parser.read_string(source)
    schema = {section.casefold(): fields for section, fields in _schema(model).items()}
    if parser.defaults() and (
        include is None or include.intersection(parser.defaults())
    ):
        msg = "DST INI configuration cannot contain DEFAULT values"
        raise ValueError(msg)
    values = {}
    seen = set()
    for section in parser.sections():
        folded = section.casefold()
        fields = schema.get(folded)
        if include is not None and (fields is None or not include.intersection(fields)):
            continue
        if folded in seen:
            msg = f"duplicate DST INI section: {section}"
            raise ValueError(msg)
        seen.add(folded)
        if fields is None:
            msg = f"unknown DST INI section: {section}"
            raise ValueError(msg)
        for option, value in parser.items(section):
            if include is not None and option not in include:
                continue
            if option not in fields:
                msg = f"unknown DST INI option: {section}.{option}"
                raise ValueError(msg)
            definition = model.model_fields.get(option)
            if definition is None:
                continue
            if definition.annotation is bool and value.lower() not in {"true", "false"}:
                msg = f"DST {section}.{option} must be true or false, got {value!r}"
                raise ValueError(msg)
            values[option] = value
    return model.model_validate_strings(values)


def _ini_value(value: Any) -> str:
    if isinstance(value, SecretStr):
        value = value.get_secret_value()
    if isinstance(value, bool):
        return str(value).lower()
    text = str(value)
    if any(character in text for character in "\0\r\n"):
        msg = "INI values cannot contain NUL, CR, or LF"
        raise ValueError(msg)
    return text


def render_ini(model: BaseModel, *, include: set[str] | None = None) -> str:
    validated = type(model).model_validate(model)
    values = validated.model_dump(
        include=validated.model_fields_set | (include or set()),
        exclude_none=True,
    )
    sections = []
    for section, fields in _schema(type(model)).items():
        options = [
            f"{field} = {_ini_value(values[field])}"
            for field in fields
            if field in values
        ]
        if options:
            sections.append(f"[{section}]\n" + "\n".join(options))
    return "\n\n".join(sections) + ("\n" if sections else "")
