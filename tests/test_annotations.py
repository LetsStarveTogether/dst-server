from __future__ import annotations

from pathlib import Path

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

function Widget:Ping(target)
    return true
end

return Widget
""",
        encoding="utf-8",
    )

    result = generate_components(components, max_workers=1)

    assert "---@class Widget" in result
    assert "---@field count number" in result
    assert "function _l:Ping(target) return false end" in result
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
