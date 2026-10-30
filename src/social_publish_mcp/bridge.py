"""Local WebSocket bridge between this server and the "Social Publish MCP" Chrome extension.

The extension is a generic Chrome DevTools Protocol relay: the server asks it to open a tab (debugger attached),
then sends CDP commands for that tab by id. Every request carries an ``id``; the extension answers with
``{type: "response", id, ok, result | error}``. Unsolicited ``{type: "event", event, tabId, reason}`` messages
report a detached debugger (user pressed Cancel on Chrome's infobar) or a closed tab.

Only one process can own the port. The first social-publish-mcp process is the *hub*; a later one (a second MCP
client session, the CLI while Claude Desktop is running...) becomes a *peer* that forwards whole operations to the
hub (see ``Bridge.call_hub``), so all sessions share one extension and one set of tabs.

Auth (loopback only):
  * sockets with an http(s) Origin (web pages) are always refused;
  * the extension is recognised by its chrome-extension://<id> Origin; the first id seen is pinned in
    ``<home>/extensions.json`` (trust on first use) and any other id is refused until added there or to
    ``SOCIAL_PUBLISH_EXTENSION_IDS`` (comma separated);
  * peers present the random token from ``<home>/token.txt`` (created by the hub, read by peers).
"""
from __future__ import annotations

import asyncio
import hmac
import itertools
import json
import os
import secrets
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from websockets.asyncio.client import connect
from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from . import __version__

HOST = "127.0.0.1"  # never configurable: the bridge is loopback only
DEFAULT_PORT = 8797
MAX_MSG = 64 * 2**20  # screenshots arrive as base64
CDP_TIMEOUT = 60.0
OPEN_TAB_TIMEOUT = 30.0
NO_EXTENSION = (
    "the Social Publish MCP extension is not connected: open Google Chrome with the extension loaded "
    "(chrome://extensions > Developer mode > Load unpacked > the extension/ folder), check that its badge says 'on' "
    "and that its port matches the server, then retry"
)

PeerHandler = Callable[[str, dict[str, Any]], Awaitable[Any]]


def log(*a: Any) -> None:
    print("[social-publish-mcp]", *a, file=sys.stderr, flush=True)  # stdout belongs to the MCP protocol


def home_dir() -> Path:
    return Path(os.environ.get("SOCIAL_PUBLISH_HOME") or Path.home() / ".social-publish-mcp").expanduser()


def default_port() -> int:
    return int(os.environ.get("SOCIAL_PUBLISH_PORT") or DEFAULT_PORT)


class BridgeError(RuntimeError):
    pass


def _read_json(p: Path) -> Any:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_json(p: Path, data: Any) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, p)


class Bridge:
    def __init__(self, *, port: int | None = None, home: Path | None = None) -> None:
        self.port = port if port is not None else default_port()
        self.home = home or home_dir()
        self.role: str | None = None  # "hub" | "peer"
        self.server: Server | None = None
        self.ext: ServerConnection | None = None
        self.ext_info: dict[str, Any] = {}
        self.ext_connected = asyncio.Event()
        self.peer_handler: PeerHandler | None = None  # set by the server: runs operations forwarded by peers
        self._ids = itertools.count(1)
        self._pending: dict[int, tuple[asyncio.Future[Any], int | None]] = {}  # id -> (future, tabId)
        self.dead_tabs: dict[int, str] = {}  # tabId -> why it can no longer be driven

    # ------------------------------------------------------------ files in the state dir
    @property
    def token_file(self) -> Path:
        return self.home / "token.txt"

    @property
    def extensions_file(self) -> Path:
        return self.home / "extensions.json"

    def token(self, *, create: bool) -> str:
        if self.token_file.exists():
            t = self.token_file.read_text(encoding="utf-8").strip()
            if t:
                return t
        if not create:
            return ""
        t = secrets.token_urlsafe(32)
        self.home.mkdir(parents=True, exist_ok=True)
        self.token_file.write_text(t, encoding="utf-8")
        try:
            self.token_file.chmod(0o600)
        except OSError:
            pass
        return t

    def allowed_extensions(self) -> set[str]:
        ids = {i.strip() for i in os.environ.get("SOCIAL_PUBLISH_EXTENSION_IDS", "").split(",") if i.strip()}
        raw = _read_json(self.extensions_file)
        if isinstance(raw, dict):
            ids |= {str(i) for i in raw.get("allowed", [])}
        return ids

    def extension_allowed(self, ext_id: str) -> bool:
        ids = self.allowed_extensions()
        if not ids:
            _write_json(self.extensions_file, {
                "allowed": [ext_id],
                "note": "Chrome extension ids allowed to drive tabs; the first one that connected was pinned automatically",
            })
            log(f"pinned Chrome extension {ext_id} (trust on first use, see {self.extensions_file})")
            return True
        return ext_id in ids

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> str:
        """Own the port (hub) or, when another instance already does, become its peer."""
        try:
            self.server = await serve(self._handler, HOST, self.port, max_size=MAX_MSG, ping_interval=20, ping_timeout=60)
        except OSError:
            self.role = "peer"
            log(f"port {self.port} is busy: running as a peer of the social-publish-mcp process that owns it")
            return self.role
        self.role = "hub"
        self.token(create=True)
        log(f"bridge listening on ws://{HOST}:{self.port}")
        return self.role

    async def stop(self) -> None:
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        self._fail_all(BridgeError("the server is shutting down"))

    async def wait_extension(self, timeout: float) -> bool:
        if self.ext is not None:
            return True
        try:
            await asyncio.wait_for(self.ext_connected.wait(), timeout)
        except asyncio.TimeoutError:
            return False
        return self.ext is not None

    # ------------------------------------------------------------ websocket side
    async def _handler(self, ws: ServerConnection) -> None:
        origin = (ws.request.headers.get("Origin") or "") if ws.request else ""
        if origin.startswith(("http://", "https://")):  # web pages may never talk to the bridge
            await ws.close(4003, "origin not allowed")
            return
        try:
            hello = json.loads(await asyncio.wait_for(ws.recv(), 10))
            role = hello.get("role")
        except Exception:
            await ws.close(4000, "expected hello")
            return
        if origin.startswith("chrome-extension://"):
            ext_id = origin.removeprefix("chrome-extension://").strip("/")
            if role != "extension" or not self.extension_allowed(ext_id):
                err = f"extension {ext_id} is not allowed; add its id to {self.extensions_file}"
                await ws.send(json.dumps({"type": "error", "error": err}))
                await ws.close(4004, "extension not allowed")
                return
            await self._serve_extension(ws, hello)
        elif role == "extension":  # only a real chrome-extension origin may take the extension role
            await ws.close(4004, "the extension role needs a chrome-extension origin")
        elif role == "peer":
            if not hmac.compare_digest(str(hello.get("token", "")).encode(), self.token(create=True).encode()):
                await ws.send(json.dumps({"type": "error", "error": "bad token"}))
                await ws.close(4001, "bad token")
                return
            await self._serve_peer(ws)
        else:
            await ws.close(4000, "unknown role")

    async def _serve_extension(self, ws: ServerConnection, hello: dict[str, Any]) -> None:
        old = self.ext
        self.ext = ws
        self.ext_info = {"version": hello.get("version"), "connected_at": time.strftime("%Y-%m-%d %H:%M:%S")}
        if old is not None:
            self._fail_all(BridgeError("the extension reconnected; tabs from the old connection are gone"))
            try:
                await old.close(4002, "replaced by a newer connection")
            except Exception:
                pass
        await ws.send(json.dumps({"type": "welcome", "server": __version__}))
        self.ext_connected.set()
        log("extension connected", hello.get("version"))
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                await self._on_extension_message(ws, msg)
        except ConnectionClosed:
            pass
        finally:
            if self.ext is ws:
                self.ext = None
                self.ext_connected.clear()
                self._fail_all(BridgeError("the extension disconnected (Chrome closed or the extension was reloaded)"))
                log("extension disconnected")

    async def _on_extension_message(self, ws: ServerConnection, msg: dict[str, Any]) -> None:
        kind = msg.get("type")
        if kind == "ping":
            await ws.send('{"type":"pong"}')
        elif kind == "response":
            entry = self._pending.get(msg.get("id"))  # type: ignore[arg-type]
            if entry and not entry[0].done():
                if msg.get("ok"):
                    entry[0].set_result(msg.get("result") or {})
                else:
                    entry[0].set_exception(BridgeError(msg.get("error") or "the extension reported an unknown error"))
        elif kind == "event" and msg.get("event") in ("detached", "tab_closed"):
            self._tab_lost(int(msg.get("tabId", -1)), msg.get("event"), str(msg.get("reason") or ""))

    def _tab_lost(self, tab_id: int, event: str, reason: str) -> None:
        why = ("the tab was closed" if event == "tab_closed"
               else f"Chrome detached the debugger from the tab ({reason or 'unknown reason'}; "
                    "pressing Cancel on the 'started debugging this browser' bar does this)")
        self.dead_tabs[tab_id] = why
        for fut, tid in self._pending.values():
            if tid == tab_id and not fut.done():
                fut.set_exception(BridgeError(why))

    def _fail_all(self, err: BridgeError) -> None:
        for fut, _ in self._pending.values():
            if not fut.done():
                fut.set_exception(err)

    async def _serve_peer(self, ws: ServerConnection) -> None:
        try:
            req = json.loads(await ws.recv())
            try:
                if self.peer_handler is None:
                    raise BridgeError("this hub does not accept forwarded operations")
                res = await self.peer_handler(str(req.get("op")), req.get("args") or {})
                await ws.send(json.dumps({"ok": True, "result": res}))
            except Exception as e:  # report every failure to the peer instead of dropping the socket
                await ws.send(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}))
        except ConnectionClosed:
            pass

    # ------------------------------------------------------------ requests to the extension
    async def request(self, msg: dict[str, Any], *, timeout: float = CDP_TIMEOUT) -> dict[str, Any]:
        if self.role == "peer":
            raise BridgeError("this process is a peer; tabs are driven by the hub process")
        tab_id = msg.get("tabId")
        if tab_id is not None and tab_id in self.dead_tabs:
            raise BridgeError(self.dead_tabs[tab_id])
        ws = self.ext
        if ws is None:
            raise BridgeError(NO_EXTENSION)
        rid = next(self._ids)
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[rid] = (fut, tab_id)
        try:
            await ws.send(json.dumps({**msg, "id": rid}))
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            what = msg.get("method") or msg.get("type")
            raise BridgeError(f"timeout: the extension did not answer {what} within {timeout:.0f}s") from None
        except ConnectionClosed:
            raise BridgeError("the extension disconnected while the request was being sent") from None
        finally:
            self._pending.pop(rid, None)

    async def open_tab(self, url: str = "about:blank", *, active: bool = False) -> int:
        """Open a tab in the extension's "Social Publish" window/group with the debugger attached; return its id."""
        r = await self.request({"type": "open_tab", "url": url, "active": active}, timeout=OPEN_TAB_TIMEOUT)
        return int(r["tabId"])

    async def close_tab(self, tab_id: int) -> None:
        self.dead_tabs.pop(tab_id, None)
        await self.request({"type": "close_tab", "tabId": tab_id}, timeout=15)

    async def activate_tab(self, tab_id: int) -> None:
        """Make the tab the visible one of its window (background tabs are throttled and may not screenshot)."""
        await self.request({"type": "activate_tab", "tabId": tab_id}, timeout=15)

    async def list_tabs(self) -> list[dict[str, Any]]:
        r = await self.request({"type": "list_tabs"}, timeout=15)
        return list(r.get("tabs") or [])

    # ------------------------------------------------------------ peer side
    async def call_hub(self, op: str, args: dict[str, Any], *, timeout: float) -> Any:
        """Run an operation in the hub process (used when this process is a peer)."""
        try:
            async with connect(f"ws://{HOST}:{self.port}", max_size=MAX_MSG, open_timeout=5, ping_interval=20,
                               ping_timeout=120) as ws:
                await ws.send(json.dumps({"type": "hello", "role": "peer", "token": self.token(create=False)}))
                await ws.send(json.dumps({"op": op, "args": args}))
                ans = json.loads(await asyncio.wait_for(ws.recv(), timeout))
        except (OSError, ConnectionClosed, asyncio.TimeoutError) as e:
            if await self.start() == "hub":  # the hub went away: take over the port
                raise BridgeError("this process just became the bridge hub; the extension reconnects within ~30 s, "
                                  "retry then") from None
            raise BridgeError(f"cannot reach the social-publish-mcp hub on port {self.port}: {e!r}") from None
        if not ans.get("ok"):
            raise BridgeError(ans.get("error") or "hub error")
        return ans.get("result")


class ExtensionTransport:
    """page.Transport over the bridge: CDP commands for one tab."""

    def __init__(self, bridge: Bridge, tab_id: int, *, timeout: float = CDP_TIMEOUT) -> None:
        self.bridge = bridge
        self.tab_id = tab_id
        self.timeout = timeout

    async def send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self.bridge.request(
            {"type": "cdp", "tabId": self.tab_id, "method": method, "params": params or {}}, timeout=self.timeout)
