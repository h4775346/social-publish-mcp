# Security

## Model

The extension has the `debugger` permission: whoever can send it commands can act as you on the sites it opens.
So the bridge is locked down:

- The WebSocket server binds **127.0.0.1 only** (not configurable).
- Connections with an `http://` or `https://` Origin (any web page) are refused.
- The extension role is accepted only from a `chrome-extension://<id>` Origin, and only for the id pinned on first
  connection (`~/.social-publish-mcp/extensions.json`) or listed in `SOCIAL_PUBLISH_EXTENSION_IDS`.
- Other local processes (a second MCP session) must present the random token in `~/.social-publish-mcp/token.txt`.
- The extension only opens tabs it is asked to open and only attaches the debugger to its own tabs; it never
  touches your other tabs. Its host permissions are limited to YouTube, Facebook and TikTok.
- No passwords, cookies or tokens are read or stored; the sites use your existing Chrome session.

Local malware running as your user can read the token file or forge an Origin header and talk to the bridge; this project does not try to
defend against that (it could read your Chrome profile anyway).

## Reporting

Please report vulnerabilities privately through GitHub's "Report a vulnerability" (Security tab) rather than a
public issue. Include steps to reproduce and the affected version. We aim to answer within a week.
