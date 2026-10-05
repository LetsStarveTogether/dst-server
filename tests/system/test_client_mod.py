import asyncio
import shutil
from pathlib import Path

import pytest

from dst_server import commands as c
from dst_server.lua_codec import lua_string
from tests.system.helpers import (
    OPERATION_TIMEOUT,
    REPOSITORY_ROOT,
    running_server,
    write_cluster,
)

pytestmark = pytest.mark.system


async def test_fieldkit_native_engine_contracts(tmp_path: Path) -> None:
    cluster = write_cluster(tmp_path, configured=True)
    mod = cluster / "mods/fieldkit"
    shutil.copytree(REPOSITORY_ROOT / "mods/fieldkit", mod)
    # Disabled client mods cannot load through the server's manifest.
    # Preload their exact sources without changing flags or enabling server hooks.
    preload = []
    for path in sorted((mod / "scripts").rglob("*.lua")):
        module = path.relative_to(mod / "scripts").with_suffix("").as_posix()
        preload.append(
            f"package.preload[{lua_string(module)}]=function(...)\n"
            f"{path.read_text()}\nend\n"
        )
    entries = ",\n".join(
        f"{entry}=function()\n{(mod / f'{entry}.lua').read_text()}\nend"
        for entry in ("modinfo", "modmain")
    )
    bootstrap = cluster / "fieldkit-preload.lua"
    bootstrap.write_text("".join(preload) + "return {" + entries + "}\n")
    script = cluster / "fieldkit-native.lua"
    shutil.copyfile(REPOSITORY_ROOT / "tests/client_mod/native.lua", script)
    async with running_server(tmp_path, cluster) as server:
        try:
            result = await server.game.invoke(
                c.Evaluate(
                    source=f"FIELDKIT_NATIVE_TEST=dofile({lua_string(str(script))})("
                    f"dofile({lua_string(str(bootstrap))}));"
                    "return FIELDKIT_NATIVE_TEST:run()"
                )
            )
            assert result.error is None, result.error
            async with asyncio.timeout(OPERATION_TIMEOUT * 2):
                while True:
                    report = await server.game.invoke(
                        c.ExecuteJson(
                            source="local test=FIELDKIT_NATIVE_TEST;"
                            "local ok, result=pcall(test.poll, test);"
                            "if not ok then local label=test.waiting and "
                            "test.waiting.label or test.step_index;"
                            'return {error=tostring(test.scene).."/"..'
                            'tostring(label)..": "..tostring(result)} end;'
                            "return result"
                        )
                    )
                    if report is not None:
                        break
                    await asyncio.sleep(0.1)
        finally:
            cleanup = await server.game.invoke(
                c.Evaluate(
                    source="if FIELDKIT_NATIVE_TEST then FIELDKIT_NATIVE_TEST:close();"
                    "FIELDKIT_NATIVE_TEST=nil end"
                )
            )
            assert cleanup.error is None, cleanup.error
        assert report == {
            "dedicated_guard": True,
            "native_text": True,
            "native_recipes": True,
            "native_announcement_costs": True,
            "native_range_identity": True,
            "native_range_catalog": True,
            "native_range_light": True,
            "native_loot_blueprints": True,
            "native_information": True,
            "native_calendar": True,
            "native_temperature": True,
            "native_storage": True,
            "native_cursor_return": True,
            "native_drying": True,
            "native_fill": True,
            "native_cook": True,
            "native_quick_cook": True,
            "native_batch_cook": True,
            "native_pickup": True,
            "native_tools": True,
            "native_equipment_stack": True,
            "native_craft": True,
            "native_manufactured_craft": True,
            "native_craft_equipment": True,
            "native_build": True,
            "native_deploy": True,
            "native_till": True,
            "native_water": True,
            "native_fertilize": True,
            "native_terraform": True,
            "native_drop": True,
            "native_fishing": True,
            "native_fishing_cancel": True,
            "native_follow": True,
            "native_cane_follow": True,
            "native_push": True,
            "native_route": True,
            "native_route_append": True,
            "native_cane_restore": True,
            "native_cane_empty_hand": True,
            "native_cane_heavy": True,
            "local_labels": True,
            "native_placer": True,
            "cleanup": True,
        }
