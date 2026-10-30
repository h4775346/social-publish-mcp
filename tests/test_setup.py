import json

import pytest

from social_publish_mcp import setup as st


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(st, "HOME", tmp_path)
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData" / "Roaming"))
    monkeypatch.setenv("SOCIAL_PUBLISH_HOME", str(tmp_path / ".social-publish-mcp"))
    monkeypatch.setattr(st, "server_command", lambda: "/opt/bin/social-publish-mcp")
    return tmp_path


def test_setup_installs_extension_skill_and_merges_configs(home):
    cursor = home / ".cursor" / "mcp.json"
    cursor.parent.mkdir(parents=True)
    cursor.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}), encoding="utf-8")

    assert st.run_setup(client_names=["cursor", "gemini", "codex"], skill=True, extension=True, open_pages=False) == 0

    assert (home / ".social-publish-mcp" / "extension" / "manifest.json").exists()
    assert (home / ".claude" / "skills" / "social-publish" / "SKILL.md").exists()
    data = json.loads(cursor.read_text(encoding="utf-8"))
    assert data["mcpServers"]["other"] == {"command": "x"}  # other servers kept
    assert data["mcpServers"]["social-publish"] == {"command": "/opt/bin/social-publish-mcp"}
    assert (home / ".cursor" / "mcp.json.bak").exists()
    gemini = json.loads((home / ".gemini" / "settings.json").read_text(encoding="utf-8"))
    assert gemini["mcpServers"]["social-publish"]["timeout"] == 1800000
    codex = (home / ".codex" / "config.toml").read_text(encoding="utf-8")
    assert "[mcp_servers.social-publish]" in codex and "/opt/bin/social-publish-mcp" in codex


def test_setup_is_idempotent_and_skips_non_json(home):
    st.run_setup(client_names=["codex"], skill=False, extension=False, open_pages=False)
    st.run_setup(client_names=["codex"], skill=False, extension=False, open_pages=False)
    assert (home / ".codex" / "config.toml").read_text(encoding="utf-8").count("[mcp_servers.social-publish]") == 1

    bad = home / ".cursor" / "mcp.json"
    bad.parent.mkdir(parents=True)
    bad.write_text("{ // comment\n}", encoding="utf-8")
    assert "skipped" in st._merge_json(bad, "mcpServers", {"command": "x"})
    assert bad.read_text(encoding="utf-8") == "{ // comment\n}"
