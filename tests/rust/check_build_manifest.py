"""Check that release manifests reject wheels with missing library notices."""

import json
import runpy
import tempfile
import zipfile
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[2]
    check = runpy.run_path(str(root / "tools/build_manifest.py"))["check_wheel_notices"]
    directory = "dst_server-0.5.0.dist-info/licenses/licenses/third-party/"
    manifest = json.dumps({"libarchive": {"notices": ["libarchive.txt"]}})
    members = {
        "dst_server.libs/libarchive-a1b2c3d4.so.13": b"shared library fixture",
        directory + "manifest.json": manifest.encode(),
        directory + "libarchive.txt": b"library copyright and license text",
    }
    with tempfile.TemporaryDirectory() as temporary:
        wheel = Path(temporary) / "fixture.whl"
        for missing in (None, "empty", "missing", "unknown", "manifest"):
            contents = dict(members)
            if missing == "empty":
                contents[directory + "libarchive.txt"] = b" \n"
            elif missing == "missing":
                del contents[directory + "libarchive.txt"]
            elif missing == "unknown":
                contents["dst_server.libs/libunknown-12345678.so.1"] = b"library"
            elif missing == "manifest":
                del contents[directory + "manifest.json"]
            with zipfile.ZipFile(wheel, "w") as archive:
                for name, data in contents.items():
                    archive.writestr(name, data)
            if missing is None:
                check(wheel)
                continue
            try:
                check(wheel)
            except ValueError:
                pass
            else:
                message = f"accepted {missing} library notice"
                raise AssertionError(message)


if __name__ == "__main__":
    main()
