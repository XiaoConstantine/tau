# TUI dialog safe defaults and cancellation

## What changed

The internal `UiBridge.confirm(...)` host seam now accepts `default: bool = True`. Existing extension-facing calls preserve the prior Yes-first behavior, while trusted host integrations can request `default=False` for security-sensitive approval dialogs.

Cancelling the coroutine waiting on any Textual extension dialog now removes that specific dialog, including when another modal covers it. Timeout continues to return the no-op default. Confirmation messages are placed in a bounded literal-text scroll viewport; Page Up/Down allow complete review without changing the selected decision.

## Why

A host-owned mutation approval must not place the affirmative action under the default Enter key, and parent cancellation must not leave an orphaned modal covering the session. These are frontend policies; they remain in `tau_coding` and do not add Textual dependencies to `tau_agent`.

## Verification

```bash
uv run pytest tests/test_tui_app.py -k extension_confirm_dialog
uv run ruff check .
uv run mypy
```
