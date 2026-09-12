import shutil
import subprocess  # ruff:ignore[suspicious-subprocess-import]
from pathlib import Path
from typing import Any

import orjson
import pytest

WORKFLOW = Path(__file__).parents[2] / ".github/workflows/image.yml"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Workflow checks require Node.js")


def workflow_script() -> str:
    block = WORKFLOW.read_text().split("      - name: Resolve image versions\n", 1)[1]
    lines = block.split("          script: |\n", 1)[1].splitlines()
    script = []
    for line in lines:
        if line and not line.startswith("            "):
            break
        script.append(line[12:])
    return "\n".join(script)


def run_script(**scenario: Any) -> dict[str, Any]:
    # Execute the shipped JavaScript, replacing only its external API boundaries.
    harness = r"""
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const result = { outputs: {}, calls: [], messages: [], error: null };
const context = { eventName: 'workflow_dispatch', ...input.context };
const core = {
  setOutput: (name, value) => { result.outputs[name] = value; },
  warning: (message) => result.messages.push(message),
};
const exec = { getExecOutput: async (command, args) => {
  result.calls.push([command, args]);
  const tag = args.at(-1).split(':').at(-1);
  const metadata = input.published?.[tag];
  return { exitCode: metadata ? 0 : 1, stdout: metadata || '' };
} };
const fetch = async () => ({ ok: true, json: async () => input.builds });
const process = { env: {
  BUILD_TARGET: 'both', FORCE_BUILD: 'false',
  IMAGE_REGISTRY: 'example.test', IMAGE_NAME: 'image',
  ...input.env,
} };
const fakeRequire = () => ({
  appendFile: async (_, summary) => { result.summary = summary; },
});
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
try {
  const execute = new AsyncFunction(
    'context', 'core', 'exec', 'fetch', 'process', 'require', input.script,
  );
  await execute(
    context, core, exec, fetch, process, fakeRequire,
  );
} catch (error) {
  result.error = error.message;
}
console.log(JSON.stringify(result));
"""
    completed = subprocess.run(  # ruff:ignore[subprocess-without-shell-equals-true]
        [str(NODE), "--input-type=commonjs", "-e", f"(async () => {{{harness}}})()"],
        input=orjson.dumps({"script": workflow_script(), **scenario}).decode(),
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    return orjson.loads(completed.stdout)


@pytest.mark.parametrize(
    ("event", "force", "published", "build", "inspections"),
    [
        ("workflow_dispatch", "false", "100", False, 2),
        ("workflow_dispatch", "true", "100", True, 0),
        ("push", "false", "100", True, 0),
        ("workflow_dispatch", "false", "99", True, 2),
        ("workflow_dispatch", "false", None, True, 2),
    ],
)
def test_channel_tags_and_build_selection(
    event: str, force: str, published: str | None, build: bool, inspections: int
) -> None:
    result = run_script(
        context={"eventName": event},
        env={"FORCE_BUILD": force},
        builds={"release": ["99", "100"], "updatebeta": ["100"]},
        published=(
            {"latest": f"{published}|release", "beta": f"{published}|beta"}
            if published is not None
            else {}
        ),
    )
    assert result["error"] is None
    matrix = orjson.loads(result["outputs"]["matrix"])["include"]
    assert [(item["image_tag"], item["version_tag"]) for item in matrix] == [
        ("latest", "100"),
        ("beta", "beta-100"),
    ]
    assert all(item["build"] is build for item in matrix)
    assert len(result["calls"]) == inspections
