# Social Publish MCP - one-line installer for Windows.
#   irm https://raw.githubusercontent.com/h4775346/social-publish-mcp/main/install.ps1 | iex
# Installs uv (if missing) and social-publish-mcp, then runs `social-publish-mcp setup`, which copies the Chrome
# extension, installs the agent skill and registers the MCP server in every AI client found on this computer.
$ErrorActionPreference = "Stop"
$repo = "git+https://github.com/h4775346/social-publish-mcp"

Write-Host "==> Social Publish MCP installer" -ForegroundColor Cyan

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "==> installing uv (Python package manager)"
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    $env:Path = "$HOME\.local\bin;$env:Path"
}

Write-Host "==> installing social-publish-mcp"
uv tool install --force $repo
$env:Path = "$HOME\.local\bin;$env:Path"
uv tool update-shell | Out-Null

Write-Host "==> running setup"
social-publish-mcp setup

Write-Host ""
Write-Host "Done. Load the extension in Chrome (the page and the folder were opened for you)," -ForegroundColor Green
Write-Host "restart your AI client, and ask it: 'check my social accounts'." -ForegroundColor Green
