"""Local WebSocket bridge between this server and the "Social Publish MCP" Chrome extension.

The extension is a generic Chrome DevTools Protocol relay: the server asks it to open a tab (debugger attached),
then sends CDP commands for that tab by id. Every request carries an ``id``; the extension answers with
``{type: "response", id, ok, result | error}``. Unsolicited ``{type: "event", event, tabId, reason}`` messages
report a detached debugger (user pressed Cancel on Chrome's infobar) or a closed tab.

Only one process can own the port. The first social-publish-mcp process is the *hub*; a later one (a second MCP
client session, the CLI while Claude Desktop is running...) becomes a *peer* that forwards whole operations to the
hub (see ``Bridge.call_hub``), so all sessions share one set of browsers and tabs.

Several browsers (Chrome profiles, Brave, Edge...) can be connected at the same time. Each extension copy has its own
persistent ``browserId``, so they never replace each other; only a reconnect of the *same* browser swaps its socket.
``Bridge.select`` picks the browser for a job: explicit choice > per-platform route > default > the only one connected.
With several connected and no choice it refuses, because a post from the wrong profile goes to the wrong account.
Choices, labels and the last account scan live in ``<home>/browsers.json``.

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
from dataclasses import dataclass
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
ScanHandler = Callable[[], Awaitable[Any]]
SWAP_WINDOW = 20.0  # seconds; 3 swaps of one browserId in this window = two browsers share an id (a copied profile)


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


@dataclass
class BrowserConn:
    """One connected extension copy."""
    id: str
    ws: ServerConnection
    label: str
    email: str
    brand: str
    version: str
    connected_at: str
    legacy: bool = False  # an extension older than 0.2 (no browserId): works, cannot be remembered between connects


class Bridge:
    def __init__(self, *, port: int | None = None, home: Path | None = None) -> None:
        self.port = port if port is not None else default_port()
        self.home = home or home_dir()
        self.role: str | None = None  # "hub" | "peer"
        self.server: Server | None = None
        self.browsers: dict[str, BrowserConn] = {}
        self.tab_owner: dict[int, str] = {}  # tabId -> browser id
        # separate browsers number their tabs independently: a clashing id gets a private alias (alias -> real id)
        self.tab_alias: dict[int, int] = {}
        self._aliases = itertools.count(2**40)
        self.scan_handler: ScanHandler | None = None  # set by the server: runs the popup's "Scan accounts"
        self._legacy_ids = itertools.count(1)
        self._swaps: dict[str, list[float]] = {}
        self.ext_connected = asyncio.Event()
        self.peer_handler: PeerHandler | None = None  # set by the server: runs operations forwarded by peers
        self._ids = itertools.count(1)
        self._pending: dict[int, tuple[asyncio.Future[Any], int | None, str]] = {}  # id -> (future, tabId, browser id)
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

    # ------------------------------------------------------------ browsers: choice and roster
    @property
    def prefs_file(self) -> Path:
        return self.home / "browsers.json"

    def prefs(self) -> dict[str, Any]:
        raw = _read_json(self.prefs_file)
        raw = raw if isinstance(raw, dict) else {}
        return {"default": raw.get("default") or "", "routes": dict(raw.get("routes") or {}),
                "labels": dict(raw.get("labels") or {}), "accounts": dict(raw.get("accounts") or {})}

    def _save_prefs(self, prefs: dict[str, Any]) -> None:
        _write_json(self.prefs_file, prefs)

    @property
    def ext(self) -> ServerConnection | None:
        """The socket of the browser used when nothing else is asked for (None = no browser connected)."""
        c = self._preferred()
        return c.ws if c else None

    @property
    def ext_info(self) -> dict[str, Any]:
        c = self._preferred()
        return {"version": c.version, "connected_at": c.connected_at} if c else {}

    def _preferred(self) -> BrowserConn | None:
        c = self.browsers.get(self.prefs()["default"])
        return c or next(iter(self.browsers.values()), None)

    def _names(self) -> str:
        return ", ".join(f"{c.label} [{c.id}]" for c in self.browsers.values()) or "none"

    def find_browser(self, name: str) -> BrowserConn:
        """A connected browser by id or label (exact, then a unique part of it; case does not matter)."""
        want = name.strip().casefold()
        for c in self.browsers.values():
            if want in (c.id.casefold(), c.label.casefold()):
                return c
        hits = [c for c in self.browsers.values() if want and (want in c.label.casefold() or want in c.email.casefold())]
        if len(hits) == 1:
            return hits[0]
        why = "matches several browsers" if hits else "is not connected"
        raise BridgeError(f"browser {name!r} {why}; connected: {self._names()}")

    def select(self, browser: str | None = None, platform: str | None = None) -> BrowserConn:
        """Which browser runs this job. Never guesses: a wrong profile would post to the wrong account."""
        if browser:
            return self.find_browser(browser)
        if not self.browsers:
            raise BridgeError(NO_EXTENSION)
        prefs = self.prefs()
        for what, wanted in ((f"the {platform} route", prefs["routes"].get(platform or "")), ("the default", prefs["default"])):
            if wanted:
                c = self.browsers.get(wanted)
                if c:
                    return c
                name = prefs["labels"].get(wanted, wanted)
                raise BridgeError(f"{what} points to browser {name!r}, which is not connected (open it, or choose another "
                                  f"with use_browser); connected: {self._names()}")
        if len(self.browsers) == 1:
            return next(iter(self.browsers.values()))
        raise BridgeError(f"{len(self.browsers)} browsers are connected and none is chosen, so nothing was posted: "
                          f"{self._names()}. Run use_browser('auto') to route each platform to the browser logged in to "
                          "it, use_browser('<name>'), or press 'Use' in the extension popup")

    async def choose(self, browser: str, platforms: list[str] | None = None) -> dict[str, Any]:
        """Make a browser the default, or the route of the given platforms. browser='none' clears the choice."""
        prefs = self.prefs()
        if browser.strip().casefold() == "none":
            if platforms:
                for p in platforms:
                    prefs["routes"].pop(p, None)
            else:
                prefs["default"], prefs["routes"] = "", {}
        else:
            c = self.find_browser(browser)
            if platforms:
                prefs["routes"].update({p: c.id for p in platforms})
            else:
                prefs["default"] = c.id
        self._save_prefs(prefs)
        await self._broadcast()
        return self.roster()

    async def set_routes(self, routes: dict[str, str]) -> None:
        prefs = self.prefs()
        prefs["routes"].update(routes)
        self._save_prefs(prefs)
        await self._broadcast()

    async def remember_accounts(self, browser_id: str, accounts: dict[str, Any]) -> None:
        prefs = self.prefs()
        prefs["accounts"][browser_id] = {**prefs["accounts"].get(browser_id, {}), **accounts}
        self._save_prefs(prefs)
        await self._broadcast()

    async def rename(self, browser_id: str, label: str) -> None:
        label = label.strip()[:60]
        c = self.browsers.get(browser_id)
        if c and label:
            c.label = label
            prefs = self.prefs()
            prefs["labels"][browser_id] = label
            self._save_prefs(prefs)
            await self._broadcast()

    def roster(self) -> dict[str, Any]:
        """Every known browser (connected, or remembered because a choice points to it) with its role."""
        prefs = self.prefs()
        jobs: dict[str, int] = {}
        for owner in self.tab_owner.values():
            jobs[owner] = jobs.get(owner, 0) + 1
        ids = list(self.browsers) + [i for i in {prefs["default"], *prefs["routes"].values()} if i and i not in self.browsers]
        rows = []
        for i in ids:
            c = self.browsers.get(i)
            rows.append({
                "id": i, "label": c.label if c else prefs["labels"].get(i, i), "connected": c is not None,
                "email": c.email if c else "", "brand": c.brand if c else "", "version": c.version if c else "",
                "connected_at": c.connected_at if c else "", "legacy": bool(c and c.legacy),
                "is_default": prefs["default"] == i, "routes": [p for p, b in prefs["routes"].items() if b == i],
                "accounts": prefs["accounts"].get(i, {}), "jobs": jobs.get(i, 0),
            })
        return {"browsers": rows, "default": prefs["default"], "routes": prefs["routes"],
                "needs_choice": len(self.browsers) > 1 and not prefs["default"] and not prefs["routes"]}

    async def _broadcast(self) -> None:
        roster = self.roster()
        for c in list(self.browsers.values()):
            try:
                await c.ws.send(json.dumps({"type": "roster", "you": c.id, **roster}))
            except (ConnectionClosed, RuntimeError):
                pass

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
        if self.browsers:
            return True
        try:
            await asyncio.wait_for(self.ext_connected.wait(), timeout)
        except asyncio.TimeoutError:
            return False
        return bool(self.browsers)

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
        bid = str(hello.get("browserId") or "")[:64]
        legacy = not bid
        if legacy:
            bid = f"legacy-{next(self._legacy_ids)}"
        old = self.browsers.get(bid)
        if old is not None:  # the same browser reconnected; a burst of these means two browsers share one id
            now = time.monotonic()
            swaps = [t for t in self._swaps.get(bid, []) if now - t < SWAP_WINDOW] + [now]
            self._swaps[bid] = swaps
            if len(swaps) >= 3:
                bid, old = secrets.token_hex(4), None
                log(f"two browsers shared one id: gave the newest one the id {bid}")
        label = (self.prefs()["labels"].get(bid) or str(hello.get("label") or hello.get("email") or "").strip()[:60]
                 or f"Browser {bid[:4]}")
        conn = BrowserConn(id=bid, ws=ws, label=label, email=str(hello.get("email") or ""),
                           brand=str(hello.get("brand") or ""), version=str(hello.get("version") or ""),
                           connected_at=time.strftime("%Y-%m-%d %H:%M:%S"), legacy=legacy)
        self.browsers[bid] = conn
        if old is not None:
            self._fail_browser(bid, BridgeError("the browser reconnected; its open jobs were interrupted"))
            try:
                await old.ws.close(4002, "replaced by a newer connection")
            except Exception:
                pass
        await ws.send(json.dumps({"type": "welcome", "server": __version__, "browser_id": bid}))
        self.ext_connected.set()
        log(f"browser connected: {label} [{bid}] extension {conn.version}")
        await self._broadcast()
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                await self._on_extension_message(conn, msg)
        except ConnectionClosed:
            pass
        finally:
            if self.browsers.get(bid) is conn:
                del self.browsers[bid]
                if not self.browsers:
                    self.ext_connected.clear()
                self._fail_browser(bid, BridgeError("the extension disconnected (Chrome closed or the extension was reloaded)"))
                log(f"browser disconnected: {label} [{bid}]")
                await self._broadcast()

    async def _on_extension_message(self, conn: BrowserConn, msg: dict[str, Any]) -> None:
        ws, kind = conn.ws, msg.get("type")
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
            self._tab_lost(self._local_tab(conn.id, int(msg.get("tabId", -1))), msg.get("event"), str(msg.get("reason") or ""))
        elif kind == "set_label":
            await self.rename(conn.id, str(msg.get("label") or ""))
        elif kind == "use_browser":  # the popup's buttons
            try:
                await self.choose(str(msg.get("browser") or conn.id), msg.get("platforms") or None)
            except BridgeError as e:
                await ws.send(json.dumps({"type": "error", "error": str(e)}))
        elif kind == "scan" and self.scan_handler is not None:
            asyncio.ensure_future(self._scan_for(ws))

    async def _scan_for(self, ws: ServerConnection) -> None:
        try:
            await self.scan_handler()  # type: ignore[misc]
        except Exception as e:
            try:
                await ws.send(json.dumps({"type": "error", "error": f"account scan failed: {e}"}))
            except ConnectionClosed:
                pass

    def _local_tab(self, browser_id: str, real_id: int) -> int:
        """Our id for a tab the browser calls real_id."""
        for alias, real in self.tab_alias.items():
            if real == real_id and self.tab_owner.get(alias) == browser_id:
                return alias
        return real_id

    def _tab_lost(self, tab_id: int, event: str, reason: str) -> None:
        why = ("the tab was closed" if event == "tab_closed"
               else f"Chrome detached the debugger from the tab ({reason or 'unknown reason'}; "
                    "pressing Cancel on the 'started debugging this browser' bar does this)")
        self.dead_tabs[tab_id] = why
        for fut, tid, _ in self._pending.values():
            if tid == tab_id and not fut.done():
                fut.set_exception(BridgeError(why))

    def _fail_all(self, err: BridgeError) -> None:
        for fut, _, _ in self._pending.values():
            if not fut.done():
                fut.set_exception(err)

    def _fail_browser(self, browser_id: str, err: BridgeError) -> None:
        """Only this browser's jobs die with its connection; the others keep running."""
        for fut, _, owner in self._pending.values():
            if owner == browser_id and not fut.done():
                fut.set_exception(err)
        for tab_id, owner in list(self.tab_owner.items()):
            if owner == browser_id:
                del self.tab_owner[tab_id]
                self.tab_alias.pop(tab_id, None)
                self.dead_tabs[tab_id] = str(err)

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
    async def request(self, msg: dict[str, Any], *, timeout: float = CDP_TIMEOUT,
                      browser_id: str | None = None) -> dict[str, Any]:
        if self.role == "peer":
            raise BridgeError("this process is a peer; tabs are driven by the hub process")
        tab_id = msg.get("tabId")
        if tab_id is not None and tab_id in self.dead_tabs:
            raise BridgeError(self.dead_tabs[tab_id])
        conn = self.browsers.get(browser_id or self.tab_owner.get(tab_id, ""))  # type: ignore[arg-type]
        if conn is None:
            if tab_id is not None or browser_id:
                raise BridgeError("the browser of this tab is no longer connected")
            conn = self.select()
        rid = next(self._ids)
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[rid] = (fut, tab_id, conn.id)
        try:
            real = {"tabId": self.tab_alias[tab_id]} if tab_id in self.tab_alias else {}
            await conn.ws.send(json.dumps({**msg, **real, "id": rid}))
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            what = msg.get("method") or msg.get("type")
            raise BridgeError(f"timeout: the extension did not answer {what} within {timeout:.0f}s") from None
        except ConnectionClosed:
            raise BridgeError("the extension disconnected while the request was being sent") from None
        finally:
            self._pending.pop(rid, None)

    async def open_tab(self, url: str = "about:blank", *, active: bool = False, browser: str | None = None,
                       platform: str | None = None) -> int:
        """Open a tab in the chosen browser's "Social Publish" window/group with the debugger attached; return its id."""
        conn = self.select(browser, platform)
        r = await self.request({"type": "open_tab", "url": url, "active": active}, timeout=OPEN_TAB_TIMEOUT, browser_id=conn.id)
        tab_id = int(r["tabId"])
        if self.tab_owner.get(tab_id, conn.id) != conn.id:
            alias = next(self._aliases)
            self.tab_alias[alias] = tab_id
            tab_id = alias
        self.dead_tabs.pop(tab_id, None)
        self.tab_owner[tab_id] = conn.id
        return tab_id

    async def close_tab(self, tab_id: int) -> None:
        self.dead_tabs.pop(tab_id, None)
        try:
            await self.request({"type": "close_tab", "tabId": tab_id}, timeout=15)
        finally:
            self.tab_owner.pop(tab_id, None)
            self.tab_alias.pop(tab_id, None)

    async def keep_tab(self, tab_id: int) -> None:
        """Keep this tab open even when the connection drops (a dry run the user wants to look at)."""
        await self.request({"type": "keep_tab", "tabId": tab_id}, timeout=15)

    async def activate_tab(self, tab_id: int) -> None:
        """Make the tab the visible one of its window (background tabs are throttled and may not screenshot)."""
        await self.request({"type": "activate_tab", "tabId": tab_id}, timeout=15)

    async def list_tabs(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for c in list(self.browsers.values()):
            r = await self.request({"type": "list_tabs"}, timeout=15, browser_id=c.id)
            for t in r.get("tabs") or []:
                local = self._local_tab(c.id, int(t["tabId"]))
                self.tab_owner.setdefault(local, c.id)
                out.append({**t, "tabId": local, "browser": c.label})
        return out

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
