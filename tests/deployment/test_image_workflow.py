import json
import shutil
import subprocess  # ruff:ignore[suspicious-subprocess-import]
from pathlib import Path
from typing import Any

import pytest

WORKFLOW = Path(__file__).parents[2] / ".github/workflows/image.yml"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Workflow checks require Node.js")


def workflow_script(step: str) -> str:
    block = WORKFLOW.read_text().split(f"      - name: {step}\n", 1)[1]
    lines = block.split("          script: |\n", 1)[1].splitlines()
    script = []
    for line in lines:
        if line and not line.startswith("            "):
            break
        script.append(line[12:])
    return "\n".join(script)


def run_script(step: str, **scenario: Any) -> dict[str, Any]:
    # Execute the shipped JavaScript, replacing only its external API boundaries.
    harness = r"""
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const result = { outputs: {}, calls: [], messages: [], error: null };
const context = {
  ref: 'refs/heads/main', sha: 'current', eventName: 'workflow_dispatch',
  repo: { owner: 'owner', repo: 'repo' }, ...input.context,
};
const github = { rest: { git: { getRef: async (args) => {
  result.calls.push(['getRef', args]);
  if (input.fail === 'getRef') throw new Error('getRef forbidden');
  return { data: { object: { sha: input.head || 'current' } } };
} } } };
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
    'github', 'context', 'core', 'exec', 'fetch', 'process', 'require', input.script,
  );
  await execute(
    github, context, core, exec, fetch, process, fakeRequire,
  );
} catch (error) {
  result.error = error.message;
}
console.log(JSON.stringify(result));
"""
    completed = subprocess.run(  # ruff:ignore[subprocess-without-shell-equals-true]
        [str(NODE), "--input-type=commonjs", "-e", f"(async () => {{{harness}}})()"],
        input=json.dumps({"script": workflow_script(step), **scenario}),
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    return json.loads(completed.stdout)


@pytest.mark.parametrize("step", ["Check build source", "Check publish source"])
@pytest.mark.parametrize(
    ("scenario", "allowed"),
    [
        ({}, True),
        ({"head": "newer"}, False),
        ({"context": {"ref": "refs/heads/feature"}}, False),
        ({"fail": "getRef"}, False),
    ],
    ids=["current", "superseded", "branch", "denied"],
)
def test_build_and_publish_recheck_main(
    step: str, scenario: dict[str, Any], allowed: bool
) -> None:
    result = run_script(step, **scenario)
    assert (result["error"] is None) is allowed


@pytest.mark.parametrize(
    ("event", "force", "build"),
    [
        ("workflow_dispatch", "false", False),
        ("workflow_dispatch", "true", True),
        ("push", "false", True),
    ],
)
def test_channel_tags_and_same_version_rebuild(
    event: str, force: str, build: bool
) -> None:
    result = run_script(
        "Resolve image versions",
        context={"eventName": event},
        env={"FORCE_BUILD": force},
        builds={"release": ["99", "100"], "updatebeta": ["100"]},
        published={"latest": "100|release", "beta": "100|beta"},
    )
    assert result["error"] is None
    matrix = json.loads(result["outputs"]["matrix"])["include"]
    assert [(item["image_tag"], item["version_tag"]) for item in matrix] == [
        ("latest", "100"),
        ("beta", "beta-100"),
    ]
    assert all(item["build"] is build for item in matrix)


def test_publication_gates_are_wired_before_mutations() -> None:
    workflow = WORKFLOW.read_text()
    assert (
        "\nconcurrency:\n"
        "  group: ${{ github.workflow }}-${{ github.ref }}\n"
        "  cancel-in-progress: true\n"
    ) in workflow
    assert workflow.count("concurrency:") == 1
    assert workflow.count("if: ${{ github.ref == 'refs/heads/main' }}") == 3
    assert "actions: write" not in workflow
    assert workflow.index("- name: Check build source") < workflow.index(
        "- name: Log in to registry"
    )
    assert workflow.index("- name: Check publish source") < workflow.index(
        "- name: Push to registry"
    )
    release = WORKFLOW.with_name("release.yml").read_text()
    assert "makeLatest: true" not in release
    assert "makeLatest: legacy" in release
