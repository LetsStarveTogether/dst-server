from pathlib import Path

import pytest
from luaparser.ast import SyntaxException

from dst_server.annotations import generate_components, generate_modutil
from dst_server.annotations.cli import main


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

    assert "---@class Widget" in result
    assert "---@field count number" in result
    assert "function _l.Ping(target) return false end" in result
    assert "function _l:Pong(target) return false end" in result
    assert "_l.count=0" in result


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

    assert main([str(source), "--output", str(output)]) == 0
    assert output.read_text(encoding="utf-8") == generate_modutil(source)
    assert "function AddThing(name) return false end" in output.read_text(
        encoding="utf-8"
    )


def test_component_cli_preserves_output_when_generation_fails(tmp_path: Path) -> None:
    components = tmp_path / "components"
    components.mkdir()
    (components / "broken.lua").write_text("local =", encoding="utf-8")
    output = tmp_path / "components_def.lua"
    output.write_text("existing\n", encoding="utf-8")

    with pytest.raises(SystemExit) as error:
        main([
            str(components),
            "--mode",
            "components",
            "--max-workers",
            "1",
            "--output",
            str(output),
        ])

    assert error.value.code == 2
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
