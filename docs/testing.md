# Testing

Tests protect behavior that callers and saved rooms rely on while allowing frequent internal refactoring.
There is no coverage percentage target or required number of tests.
Keep a test when it catches a meaningful failure at a reasonable maintenance cost.
Remove duplicate assertions and tests whose only expectation is a copy of the implementation.

## What to Test

Prefer the public operation that owns the behavior.
Rust integration tests exercise public APIs, as described in the [Rust Book][rust-tests].
Use module tests for parsing rules, recovery decisions and other focused logic that needs direct boundary inputs.
An internal function gaining a branch does not automatically need another test.

| Area | Behavior worth preserving |
| --- | --- |
| Save and recovery | Native save acceptance, consistent rollback targets, idempotence and failure without losing existing data. |
| Processes and RPC | Accepted work survives caller cancellation, children are reaped and final replies arrive before closure. |
| Configuration and Lua data | Values retain their types, invalid input is rejected and resource limits return errors. |
| Embedded Lua | Native callbacks keep their results and errors, and repeated installation cannot duplicate effects. |
| Python bindings | Installed packages expose the intended API, exceptions and asynchronous ownership. |

Assert the relevant result, error or side effect.
Avoid checking complete debug output, private field layouts or incidental helper call order.
Check event ordering when it is part of the operation's behavior.
Keep concrete regressions for failures such as parser stack exhaustion and room lock error translation.

## Tools and Test Locations

| Tool | Role in this project |
| --- | --- |
| [nextest][nextest] | Runs Rust tests in individual processes with named results and bounded execution time. |
| [proptest][proptest] | Generates and minimizes inputs for the [Lua serialization property][lua-data]. |
| [pytest][pytest] | Selects and reports Lua scenarios, passing each case to a fresh Lua 5.1 or LuaJIT process. |

The Rust sources remain ordinary `#[test]` and `#[tokio::test]` functions.
nextest controls their execution without adding an application dependency.
The repository uses four concurrent test processes and a two minute timeout per test.
CI collects all failures; automatic retries are disabled by nextest's default configuration.
Investigate an intermittent failure before adding a retry.

The Lua suite lives in [`tests/lua`](../tests/lua).
`test_contracts.py` selects native callback and lifecycle scenarios; `test_rpc.py` checks RPC inputs and results.
`conftest.py` supplies interpreters, paths and the subprocess boundary.
Driver scenarios are listed by the Lua fixture itself.
Adding a scenario does not require a matching Python registry or source parsing rule.
The Lua fixtures load the checked out game modules, and their global state is discarded with each process.

Python wheel checks remain in [`tests/rust`](../tests/rust) because installation and binding behavior need an installed SDK.
Native game checks use disposable rooms and containers as described in the [migration guide](migration.md#native-verification).

## Running Tests

Install [nextest][nextest-install], Lua 5.1 and LuaJIT, and initialize the `dst-scripts/scripts` submodule.
The Lua command resolves the locked development dependencies through uv.

```sh
cargo nextest run --locked -p dst-server --all-targets
cargo build --locked -p dst-server --bin dst-server
uv run --locked --only-group dev pytest tests/lua
```

During an edit, select the behavior being changed.

```sh
cargo nextest run --locked -p dst-server -E 'binary(lua_data)'
cargo nextest run --locked -p dst-server -E 'test(recovery)'
uv run --locked --only-group dev pytest tests/lua -k 'print_boundary and luajit'
uv run --locked --only-group dev pytest tests/lua/test_rpc.py --lf
```

The same Lua suite can check scripts extracted from a built game image.

```sh
uv run --locked --only-group dev pytest tests/lua --scripts /tmp/native/source/scripts --binary /tmp/native/dst-server
```

Missing interpreters, native scripts or the SDK executable fail the suite instead of silently skipping tests.
Use `cargo test --locked -p dst-server --doc` when adding executable Rust documentation examples.

## Writing Cases That Survive Refactoring

Name each case after a behavior and keep its setup local to the scenario.
Extract a fixture when it removes repeated setup while keeping the important inputs visible.
Avoid fixture inheritance, generic test builders and mock expectations for every internal call.
Use the existing temporary directories, local sockets and native game modules where they provide the needed boundary.

Use parameterization for independent examples that share one assertion.
Give edge cases useful names such as `at_limit`, `over_limit` and `stale_generation`.
Keep sequences together when the behavior depends on their history, such as applying the same rollback twice.
Several assertions can belong to one test when they describe that one behavior.

Use properties for stable invariants such as preserving supported data through serialization.
The Lua data property generates 64 small inputs per run, with bounded recursion and collection sizes.
Its input domain excludes null and empty arrays because the Lua literal subset cannot preserve those JSON values.
Concrete numeric type and escape regressions remain alongside the property.
Proptest [shrinks failures][proptest] and [persists replay seeds][persistence]; commit a generated seed file with the fix.
Keep an explicit regression example when its exact input matters across later changes to the generator.

Synchronize asynchronous tests with acknowledgements, channels or observable state.
Poll the Rust future or eagerly start the Python task before cancellation.
Wait for an acknowledgement if the test requires a later stage of the operation.
Publish fixture files containing state by writing a temporary file and renaming it after the contents are complete.
For logic driven entirely by Tokio timers, its [paused clock][tokio-tests] can avoid real waiting.
Tests involving native processes, file descriptors or external clocks still need real time and bounded deadlines.
A timeout should produce enough context to identify the operation that did not finish.

## When Another Dependency Helps

Evaluate a test dependency by the code and maintenance it removes.
The current suite has no coverage gate, broad snapshot baseline or general mocking framework.

| Candidate | Use it when |
| --- | --- |
| [rstest][rstest] | Repeated Rust case setup is obscuring the scenario and named parameters make it smaller. |
| [insta][insta] | A stable user facing text format needs reviewable diffs; redact volatile values and snapshot only relevant output. |
| [Busted][busted] | A Lua suite benefits from shared setup and teardown within an interpreter. |
| [datatest stable][datatest] | Most cases naturally consist of input and expected output files. |

Pytest checks JSON framing and validates native game customization choices through the Rust configuration models.
Running the installed Lua interpreters preserves the game runtime boundary and avoids shared global cleanup between cases.
An additional Lua framework or embedded interpreter would add another setup path to maintain.

[rust-tests]: https://doc.rust-lang.org/book/ch11-03-test-organization.html
[nextest]: https://nexte.st/docs/design/how-it-works/
[nextest-install]: https://nexte.st/docs/installation/pre-built-binaries/
[proptest]: https://proptest-rs.github.io/proptest/intro.html
[persistence]: https://altsysrq.github.io/proptest-book/proptest/failure-persistence.html
[pytest]: https://docs.pytest.org/en/stable/how-to/parametrize.html
[tokio-tests]: https://tokio.rs/tokio/topics/testing
[rstest]: https://docs.rs/rstest/latest/rstest/
[insta]: https://insta.rs/docs/redactions/
[busted]: https://lunarmodules.github.io/busted/
[datatest]: https://nexte.st/docs/design/custom-test-harnesses/
[lua-data]: ../crates/dst-server/tests/lua_data.rs
