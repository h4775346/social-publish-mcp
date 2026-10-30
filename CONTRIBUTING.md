# Contributing

Thanks for helping. Most fixes will be in a platform driver after a site changed its UI.

## Setup

```bash
uv sync --extra dev
uv run pytest -q
uv run ruff check .
```
Load `extension/` unpacked in Chrome (chrome://extensions → Developer mode). After editing `sw.js`, press the
reload button on the extension card; the server does not need a restart. After editing Python, restart your MCP
client (or the CLI).

## Layout

- `src/social_publish_mcp/page.py` — the only browser API drivers use (CDP over a transport).
- `src/social_publish_mcp/drivers/<platform>.py` — one flow per site; contract in `drivers/__init__.py`.
- `runner.py` (validation, tabs, history), `bridge.py` (WebSocket + extension auth), `server.py` (MCP tools, CLI).
- `extension/` — MV3 relay; keep it free of site logic.

## Driver guidelines

- Selectors as stable as possible (ids, `name`, aria labels); fall back to `__sp.byText(...)`. Note every
  observation in `docs/platform_notes.md`.
- Use trusted input (`page.click`, `page.insert_text`, `page.press`), and verify what you typed by reading it back.
- Raise `DriverError(step, detail, screenshot)` with a step name a user can act on.
- `dry_run=True` must never click the final Publish/Schedule/Post button.
- Test against your own accounts with `--dry-run --keep-tab` first. Never commit account names, ids, screenshots
  or videos.

## Pull requests

- One topic per PR; `ruff check` and `pytest` green; update `CHANGELOG.md`.
- New behaviour in the runner/bridge needs a test (see `tests/conftest.py` for the fake extension).
