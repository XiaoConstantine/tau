# Host-only composed tools

This public seam is introduced in Tau 0.3.3.

## What changed

Tau now separates the final extension-wrapped tool catalog from the subset exposed
to the ordinary model/provider loop.

- `CodingSessionConfig.expose_tools_to_model=False` keeps composed tools out of the
  system prompt, harness tool list, and provider requests.
- `CodingSession.composed_tools` returns the final built-in/extension merge after
  extension wrappers are applied.
- Reload rebuilds this catalog even when the ordinary harness remains tool-free.
- Wrapped tools capture their extension generation and reject stale execution after
  `/reload`.
- `AgentTool.prepare_call()` and `execute_prepared()` split canonical argument
  preparation from execution. This lets a host run extension argument-rewrite hooks,
  validate the final arguments, and only then execute without running hooks twice.

## Why it exists

Some hosts use Tau's session, extension, persistence, and UI layers but own a
different provider-neutral control plane. An RLM, for example, exposes tools as
bounded functions inside a persistent programming environment rather than as native
provider tool calls. Passing composed tools through the ordinary harness would
mislead the model, duplicate orchestration, and inject schemas into the wrong prompt.

The host-only seam preserves Tau's extension semantics without making `tau_agent`
depend on a frontend or RLM implementation:

```text
ExtensionRuntime.compose_tools
  -> CodingSession.composed_tools
  -> host allow-list / broker
  -> AgentTool.prepare_call
  -> host validation
  -> AgentTool.execute_prepared
```

## Safety properties

- The ordinary provider sees no tools when exposure is disabled.
- Extension loading remains explicitly trust-gated.
- A host must independently classify and allow-list composed tools.
- Call hooks run before host validation, so validation observes rewritten arguments.
- Result hooks run after execution.
- Blocked call hooks return a host-owned blocked result without invoking the tool.
- Reload invalidates old wrapped tools and publishes a fresh immutable tuple.

## Verification

Focused tests cover hidden model exposure, composed catalog access, hook-preserving
prepare/execute staging, reload replacement, and stale-tool rejection. Run:

```bash
uv run pytest tests/test_extensions.py
uv run ruff check .
uv run mypy
```
