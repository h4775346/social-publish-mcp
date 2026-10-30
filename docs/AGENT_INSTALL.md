# Installing Social Publish MCP — instructions for AI agents

You are an AI coding agent asked to install **Social Publish MCP** for the user. Follow these steps exactly, run the
commands yourself, and only ask the user to do the one step that Chrome reserves for humans (step 4).

## 1. Install uv and the server

Check whether `uv` exists (`uv --version`). If not, install it:

- Windows (PowerShell): `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`
- macOS / Linux: `curl -LsSf https://astral.sh/uv/install.sh | sh`

Then install the server (adds the `social-publish-mcp` command to `~/.local/bin`):

```bash
uv tool install --force git+https://github.com/h4775346/social-publish-mcp
```

If `social-publish-mcp` is not found afterwards, use the full path `~/.local/bin/social-publish-mcp`
(Windows: `%USERPROFILE%\.local\bin\social-publish-mcp.exe`).

## 2. Run setup

```bash
social-publish-mcp setup
```

It copies the Chrome extension to `~/.social-publish-mcp/extension`, installs the Claude Code skill to
`~/.claude/skills/social-publish/SKILL.md`, and registers the MCP server (name `social-publish`) in every client it
finds: Claude Code, Claude Desktop, Cursor, Windsurf, VS Code, Gemini CLI, Codex. Existing configs are merged and
backed up as `*.bak`.

- To configure only your own client: `social-publish-mcp setup --client <name>` (repeatable).
- If **you** are a client that setup does not know (Cline, Roo Code, Zed, OpenCode, Continue, ...), register a stdio
  MCP server named `social-publish` whose command is the absolute path printed by setup as "server command", with no
  arguments, and allow tool calls of at least 30 minutes.

## 3. Report to the user

Tell the user what setup printed (which clients were configured, the extension folder).

## 4. Ask the user to load the extension (the only manual step)

Setup opened `chrome://extensions` and the extension folder. Ask the user to:

1. turn on **Developer mode** (top right of `chrome://extensions`);
2. click **Load unpacked** and choose the folder `~/.social-publish-mcp/extension`
   (Windows: `C:\Users\<user>\.social-publish-mcp\extension`);
3. make sure they are logged in to YouTube Studio, Meta Business Suite and TikTok Studio in that Chrome.

## 5. Verify

After the user confirms, run:

```bash
social-publish-mcp --check
```

Expected: JSON with `"logged_in": true` per platform. Report the result. Then tell the user to restart their AI client
so it loads the new `social-publish` tools, and that every post needs their explicit OK.

## Rules while using it

Read `~/.claude/skills/social-publish/SKILL.md` (also in the repository at `skill/SKILL.md`). Never post or schedule
without the user's explicit approval of the exact video, text, platform and time; use `dry_run=true` first for new
templates.
