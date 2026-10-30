#!/bin/sh
# Social Publish MCP - one-line installer for macOS and Linux.
#   curl -LsSf https://raw.githubusercontent.com/h4775346/social-publish-mcp/main/install.sh | sh
# Installs uv (if missing) and social-publish-mcp, then runs `social-publish-mcp setup`, which copies the Chrome
# extension, installs the agent skill and registers the MCP server in every AI client found on this computer.
set -eu
REPO="git+https://github.com/h4775346/social-publish-mcp"

echo "==> Social Publish MCP installer"

if ! command -v uv >/dev/null 2>&1; then
  echo "==> installing uv (Python package manager)"
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="$HOME/.local/bin:$PATH"

echo "==> installing social-publish-mcp"
uv tool install --force "$REPO"
uv tool update-shell >/dev/null 2>&1 || true

echo "==> running setup"
social-publish-mcp setup

echo ""
echo "Done. Load the extension in Chrome (the page and the folder were opened for you),"
echo "restart your AI client, and ask it: 'check my social accounts'."
