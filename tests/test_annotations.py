from pathlib import Path

import pytest
from luaparser.ast import SyntaxException

from dst_server.annotations import generate_components, generate_modutil
from dst_server.annotations.visitors import parse_component, parse_modutil
from dst_server.cli import main


@pytest.mark.parametrize(
    ("definition", "name"),
    [
        ("function Widget:Outer(value, ...) {body} end", "_l:Outer"),
        ("Widget.Outer = function(value, ...) {body} end", "_l.Outer"),
        ("env.Outer = function(value, ...) {body} end", "Outer"),
    ],
    ids=["component-method", "component-function", "modutil"],
)
@pytest.mark.parametrize(
    "nested",
    [
        "local helper = function() return true end",
        "local function helper() return true end",
        "function helper() return true end",
        "function Other:Helper() return true end",
    ],
    ids=["anonymous", "local-function", "function", "method"],
)
@pytest.mark.parametrize(
    ("before", "after", "annotation"),
    [
        ("", "", None),
        ("if value then return 7 end", "", "number"),
        ("", "return 'outer'", "string"),
    ],
    ids=["no-return", "return-before-nested-function", "return-after-nested-function"],
)
def test_nested_function_returns_do_not_change_outer_annotations(
    definition: str,
    name: str,
    nested: str,
    before: str,
    after: str,
    annotation: str | None,
) -> None:
    source = definition.format(body=f"{before}\n{nested}\n{after}")
    definitions = (
        parse_modutil(source, "modutil")
        if name == "Outer"
        else parse_component(source, "widget", "Widget", "components")[1]
    )

    assert len(definitions) == 1
    result = definitions[0]
    assert "---@param value any\n---@param ... any" in result
    assert result.endswith(f"function {name}(value, ...) end")
    if annotation is None:
        assert "---@return" not in result
    else:
        assert f"---@return {annotation}\n" in result


def test_generate_component_annotations(tmp_path: Path) -> None:
    components = tmp_path / "components"
    components.mkdir()
    (components / "widget.lua").write_text(
        """
local Widget = Class(function(self)
    self.count = 1
end)

Widget.Ping = function(target)
    return true
end

function Widget:Pong(target)
    return true
end

return Widget
""",
        encoding="utf-8",
    )

    result = generate_components(components, max_workers=1)

    assert result.startswith("---@meta\n\n")
    assert "---@class Widget" in result
    assert "---@field count number" in result
    assert "local _l={}\ncomponents.widget=_l" in result
    assert "---@source components/widget.lua:6\n" in result
    assert "---@source components/widget.lua:10\n" in result
    assert "---@return boolean\nfunction _l.Ping(target) end" in result
    assert "---@return boolean\nfunction _l:Pong(target) end" in result
    assert "_l.count=" not in result
    output = tmp_path / "components_def.lua"
    assert (
        main([
            "annotations",
            str(components),
            "--max-workers",
            "1",
            "--output",
            str(output),
        ])
        == 0
    )
    assert output.read_text(encoding="utf-8") == result


def test_empty_components_do_not_generate_a_meta_only_file(tmp_path: Path) -> None:
    assert generate_components(tmp_path, max_workers=1) == ""


def test_generate_modutil_annotations(tmp_path: Path) -> None:
    source = tmp_path / "modutil.lua"
    output = tmp_path / "modutil_def.lua"
    source.write_text(
        """
env.AddThing = function(name)
    return true
end
""",
        encoding="utf-8",
    )

    assert main(["annotations", str(source), "--output", str(output)]) == 0
    result = output.read_text(encoding="utf-8")
    assert result == generate_modutil(source)
    assert result.startswith("---@meta\n\n")
    assert "---@source modutil.lua:2\n" in result
    assert "---@return boolean\nfunction AddThing(name) end" in result


def test_component_cli_preserves_output_when_generation_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    components = tmp_path / "components"
    components.mkdir()
    (components / "broken.lua").write_text("local =", encoding="utf-8")
    output = tmp_path / "components_def.lua"
    output.write_text("existing\n", encoding="utf-8")

    assert (
        main([
            "annotations",
            str(components),
            "--mode",
            "components",
            "--max-workers",
            "1",
            "--output",
            str(output),
        ])
        == 1
    )
    assert capsys.readouterr().err
    assert output.read_text(encoding="utf-8") == "existing\n"


@pytest.mark.parametrize("max_workers", [1, 2], ids=["serial", "process-pool"])
def test_generate_components_fails_on_any_invalid_lua(
    tmp_path: Path,
    max_workers: int,
) -> None:
    components = tmp_path / "components"
    components.mkdir()
    (components / "a_valid.lua").write_text("return Valid\n", encoding="utf-8")
    (components / "z_invalid.lua").write_text("local =", encoding="utf-8")

    with pytest.raises(SyntaxException):
        generate_components(components, max_workers=max_workers)
