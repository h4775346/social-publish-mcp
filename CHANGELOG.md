# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed
- YouTube: real (non-dry) jobs timed out waiting for the "Video scheduled/published" dialog. The schedule time list
  stayed open after the time was typed and swallowed the click on "Schedule". Open dropdowns are now closed and
  checked before the final click, the driver waits for the button to be enabled, re-clicks (max 3) when nothing
  reacts, finds the result dialog by its heading ("Video scheduled" / "Video published") wherever it is in the DOM,
  and writes `youtube_<step>_dom.json` on failure. Results carry the Shorts URL and the video id (`post_id`).
- YouTube playlists: names were read from the altered-content checkboxes instead of the playlist picker. The picker
  popup is now found on its own, the driver waits for its list to load, and `playlists` accepts the full name or a
  unique case-insensitive start/part of it (`"OPETO Shorts"` finds `"OPETO Shorts · …"`); ambiguous names fail clearly.
- YouTube tags: tags are committed one chip at a time (tag, then a comma key event; Enter as fallback) and verified
  by exact chip text; Arabic tags no longer merge into one string. Commas, including the Arabic comma `،`, split tags.
  A tag that does not commit is a warning in the result message, not a job failure.
- Facebook schedule date: the date is entered up to 3 times (tab brought to front, real click, select all, type,
  Enter, Tab on retries, key events on the last try) and read back each time; the date is re-checked after the time.

## [0.2.0] - 2026-10-31

### Added
- One-line installers: `install.ps1` (Windows) and `install.sh` (macOS/Linux).
- `social-publish-mcp setup`: copies the Chrome extension to `~/.social-publish-mcp/extension`, installs the Claude Code
  skill and registers the MCP server in Claude Code, Claude Desktop, Cursor, Windsurf, VS Code, Gemini CLI and Codex
  (configs merged and backed up); opens `chrome://extensions` and the extension folder.
- `docs/AGENT_INSTALL.md`: install instructions an AI agent can follow end to end.
- The extension and the skill now ship inside the Python package.

## [0.1.0] - 2026-10-31

First public release.

### Added
- MCP server (stdio) with the tools `publish`, `publish_batch`, `check_accounts`, `status` and `history`.
- Command line: `--check`, `post`, `batch`, `status`, `history`.
- Chrome MV3 companion extension: a generic Chrome DevTools Protocol relay (one tab per job, dedicated window and
  tab group), pinned on first use.
- Drivers for YouTube Studio (title, description, tags, playlists, audience, altered-content disclosure, publish or
  schedule), Facebook Page reels in Meta Business Suite (upload, caption, remix setting, share or schedule) and
  TikTok Studio (upload, caption, visibility, comments, AI label, post or schedule).
- Validation of every job before anything runs; dry runs; read-back of date and time before scheduling.
- Results history (`history.jsonl`), failure screenshots and DOM summaries.
- Hub/peer mode so several agents and the CLI share one bridge.
- Agent skill (`skill/SKILL.md`) and documentation of verified site behaviour (`docs/platform_notes.md`).
