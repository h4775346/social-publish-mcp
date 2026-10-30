"""`social-publish-mcp setup`: one command that installs everything except the one click Chrome reserves for the user.

- copies the bundled Chrome extension to <home>/extension (a stable folder to "Load unpacked" once)
- installs the agent skill for Claude Code (~/.claude/skills/social-publish/SKILL.md)
- registers the MCP server in every AI client found on this computer (or the ones passed with --client)

Every config file is merged (other servers are kept) and backed up to <file>.bak before it is changed.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from collections.abc import Callable
from importlib import resources
from pathlib import Path

SERVER_NAME = "social-publish"
HOME = Path.home()


def state_home() -> Path:
    return Path(os.environ.get("SOCIAL_PUBLISH_HOME") or HOME / ".social-publish-mcp")


def server_command() -> str:
    """Absolute path of the installed console script, so clients do not depend on PATH."""
    found = shutil.which("social-publish-mcp")
    if found:
        return str(Path(found).resolve())
    exe = Path(sys.argv[0])
    return str(exe.resolve()) if exe.exists() else "social-publish-mcp"


# ------------------------------------------------------------------ bundled files
def _bundled(name: str) -> Path:
    """Folder shipped inside the wheel (extension/ or skill/); falls back to the source checkout."""
    try:
        p = Path(str(resources.files("social_publish_mcp") / name))
        if p.exists():
            return p
    except (ModuleNotFoundError, TypeError):
        pass
    p = Path(__file__).resolve().parents[2] / name
    if p.exists():
        return p
    raise FileNotFoundError(f"bundled folder {name!r} not found (reinstall social-publish-mcp)")


def install_extension(home: Path) -> Path:
    dest = home / "extension"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(_bundled("extension"), dest)
    return dest


def install_skill() -> Path:
    dest = HOME / ".claude" / "skills" / "social-publish"
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy2(_bundled("skill") / "SKILL.md", dest / "SKILL.md")
    return dest / "SKILL.md"


# ------------------------------------------------------------------ client configs
def _appdata() -> Path:
    return Path(os.environ.get("APPDATA") or HOME / "AppData" / "Roaming")


def _claude_desktop_path() -> Path:
    system = platform.system()
    if system == "Windows":
        return _appdata() / "Claude" / "claude_desktop_config.json"
    if system == "Darwin":
        return HOME / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    return HOME / ".config" / "Claude" / "claude_desktop_config.json"


def _vscode_user_dir() -> Path:
    system = platform.system()
    if system == "Windows":
        return _appdata() / "Code" / "User"
    if system == "Darwin":
        return HOME / "Library" / "Application Support" / "Code" / "User"
    return HOME / ".config" / "Code" / "User"


def _merge_json(path: Path, section: str, entry: dict) -> str:
    data: dict = {}
    if path.exists():
        text = path.read_text(encoding="utf-8").strip()
        if text:
            try:
                data = json.loads(text)
            except ValueError:
                return f"skipped: {path} is not plain JSON (comments?); add the server by hand (see README)"
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    servers = data.setdefault(section, {})
    if not isinstance(servers, dict):
        return f"skipped: '{section}' in {path} is not an object"
    servers[SERVER_NAME] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return f"configured {path}"


def _codex(cmd: str) -> str:
    path = HOME / ".codex" / "config.toml"
    block = (f'\n[mcp_servers.{SERVER_NAME}]\ncommand = {json.dumps(cmd)}\n'
             'tool_timeout_sec = 1800  # uploads can take minutes\n')
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if f"[mcp_servers.{SERVER_NAME}]" in text:
        return f"already configured in {path}"
    if text:
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + block, encoding="utf-8")
    return f"configured {path}"


def _claude_code(cmd: str) -> str:
    claude = shutil.which("claude")
    if not claude:
        return "skipped: the `claude` command was not found"
    subprocess.run([claude, "mcp", "remove", SERVER_NAME, "--scope", "user"], capture_output=True, text=True)
    r = subprocess.run([claude, "mcp", "add", SERVER_NAME, "--scope", "user", "--", cmd], capture_output=True, text=True)
    if r.returncode != 0:
        return f"failed: claude mcp add ({(r.stderr or r.stdout).strip()[:200]})"
    return "configured (claude mcp add --scope user)"


def clients(cmd: str) -> dict[str, tuple[Callable[[], bool], Callable[[], str]]]:
    """name -> (is it installed?, configure)."""
    std = {"command": cmd}
    return {
        "claude-code": (lambda: shutil.which("claude") is not None, lambda: _claude_code(cmd)),
        "claude-desktop": (lambda: _claude_desktop_path().parent.exists(),
                           lambda: _merge_json(_claude_desktop_path(), "mcpServers", std)),
        "cursor": (lambda: (HOME / ".cursor").exists(),
                   lambda: _merge_json(HOME / ".cursor" / "mcp.json", "mcpServers", std)),
        "windsurf": (lambda: (HOME / ".codeium" / "windsurf").exists(),
                     lambda: _merge_json(HOME / ".codeium" / "windsurf" / "mcp_config.json", "mcpServers", std)),
        "vscode": (lambda: _vscode_user_dir().exists(),
                   lambda: _merge_json(_vscode_user_dir() / "mcp.json", "servers", {"type": "stdio", "command": cmd})),
        "gemini": (lambda: (HOME / ".gemini").exists() or shutil.which("gemini") is not None,
                   lambda: _merge_json(HOME / ".gemini" / "settings.json", "mcpServers", {**std, "timeout": 1800000})),
        "codex": (lambda: (HOME / ".codex").exists() or shutil.which("codex") is not None, lambda: _codex(cmd)),
    }


CLIENT_NAMES = tuple(clients("x"))


# ------------------------------------------------------------------ command
def run_setup(*, client_names: list[str] | None, skill: bool, extension: bool, open_pages: bool = True) -> int:
    home = state_home()
    home.mkdir(parents=True, exist_ok=True)
    cmd = server_command()
    print(f"social-publish-mcp setup\n  server command: {cmd}\n")

    ext_dir = None
    if extension:
        ext_dir = install_extension(home)
        print(f"[extension] copied to {ext_dir}")
    if skill:
        print(f"[skill] installed {install_skill()}")

    table = clients(cmd)
    names = client_names or [n for n, (found, _) in table.items() if found()]
    if not names:
        print("[clients] no AI client detected; see README > Connect your AI agent")
    for n in names:
        if n not in table:
            print(f"[{n}] unknown client (choose from {', '.join(CLIENT_NAMES)})")
            continue
        try:
            print(f"[{n}] {table[n][1]()}")
        except OSError as e:
            print(f"[{n}] failed: {e}")

    folder = ext_dir or home / "extension"
    print("\nLast step (Chrome allows extensions only by hand, once):")
    print("  1. in chrome://extensions turn on Developer mode")
    print(f"  2. click Load unpacked and choose: {folder}")
    print("  3. restart your AI client, then run:  social-publish-mcp --check")
    if open_pages:
        _open_extension_page(folder)
    return 0


def _chrome() -> str | None:
    for name in ("chrome", "google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    candidates = [
        Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
        Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    ]
    if os.environ.get("LOCALAPPDATA"):
        candidates.append(Path(os.environ["LOCALAPPDATA"]) / "Google/Chrome/Application/chrome.exe")
    return next((str(p) for p in candidates if p.exists()), None)


def _open_extension_page(folder: Path) -> None:
    """Best effort: show the extensions page and the folder to pick, so the last step is one click."""
    try:
        chrome = _chrome()
        if chrome:
            subprocess.Popen([chrome, "chrome://extensions"])
        system = platform.system()
        if system == "Windows":
            os.startfile(folder)  # type: ignore[attr-defined]
        elif system == "Darwin":
            subprocess.Popen(["open", str(folder)])
        elif shutil.which("xdg-open"):
            subprocess.Popen(["xdg-open", str(folder)])
    except OSError:
        pass
