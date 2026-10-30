from __future__ import annotations

import asyncio
import json

import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from social_publish_mcp.bridge import Bridge, BridgeError, ExtensionTransport
from social_publish_mcp.page import Page

from .conftest import FakeExtension


async def _closed_code(ws) -> int | None:
    try:
        while True:
            await asyncio.wait_for(ws.recv(), 5)
    except ConnectionClosed:
        return ws.close_code


async def test_handshake_pins_first_extension(bridge: Bridge, ext: FakeExtension):
    assert bridge.ext is not None
    pinned = json.loads(bridge.extensions_file.read_text(encoding="utf-8"))
    assert pinned["allowed"] == ["fakeextensionid"]
    assert bridge.ext_info["version"] == "test"


async def test_other_extension_id_refused(bridge: Bridge, ext: FakeExtension):
    async with connect(f"ws://127.0.0.1:{bridge.port}", origin="chrome-extension://intruder") as ws:  # type: ignore[arg-type]
        await ws.send(json.dumps({"type": "hello", "role": "extension", "version": "x"}))
        assert await _closed_code(ws) == 4004
    assert bridge.ext is not None  # the pinned extension stays connected


async def test_web_page_origin_refused(bridge: Bridge):
    async with connect(f"ws://127.0.0.1:{bridge.port}", origin="https://evil.example") as ws:  # type: ignore[arg-type]
        assert await _closed_code(ws) == 4003
    assert bridge.ext is None


async def test_extension_role_needs_extension_origin(bridge: Bridge):
    async with connect(f"ws://127.0.0.1:{bridge.port}") as ws:
        await ws.send(json.dumps({"type": "hello", "role": "extension"}))
        assert await _closed_code(ws) == 4004
    assert bridge.ext is None


async def test_peer_needs_token(bridge: Bridge):
    async with connect(f"ws://127.0.0.1:{bridge.port}") as ws:
        await ws.send(json.dumps({"type": "hello", "role": "peer", "token": "wrong"}))
        assert await _closed_code(ws) == 4001


async def test_open_tab_and_cdp_roundtrip(bridge: Bridge, ext: FakeExtension, tmp_path):
    tab = await bridge.open_tab("about:blank")
    page = Page(ExtensionTransport(bridge, tab), artifacts_dir=tmp_path)
    assert await page.eval("1+1") is True  # the fake answers every evaluate with true
    await page.click("#go")
    shot = await page.screenshot("proof")
    assert shot.endswith("proof.png")
    methods = [c.get("method") for c in ext.calls if c["type"] == "cdp"]
    assert "Input.dispatchMouseEvent" in methods and "Page.captureScreenshot" in methods
    assert all(c["tabId"] == tab for c in ext.calls if c["type"] == "cdp")
    await bridge.close_tab(tab)
    assert tab not in ext.open_tabs


async def test_cdp_error_is_raised(bridge: Bridge, ext: FakeExtension):
    ext.failing_methods["DOM.setFileInputFiles"] = "No node with given id found"
    tab = await bridge.open_tab()
    with pytest.raises(BridgeError, match="No node"):
        await ExtensionTransport(bridge, tab).send("DOM.setFileInputFiles", {"files": []})


async def test_detach_event_fails_pending_and_later_calls(bridge: Bridge, ext: FakeExtension):
    ext.silent_methods.add("Page.navigate")
    tab = await bridge.open_tab()
    t = ExtensionTransport(bridge, tab)
    pending = asyncio.create_task(t.send("Page.navigate", {"url": "https://example.test"}))
    await asyncio.sleep(0.1)
    await ext.event("detached", tab, "canceled_by_user")
    with pytest.raises(BridgeError, match="detached"):
        await pending
    with pytest.raises(BridgeError, match="canceled_by_user"):
        await t.send("Runtime.evaluate", {"expression": "1"})


async def test_request_timeout(bridge: Bridge, ext: FakeExtension):
    ext.silent_methods.add("Page.navigate")
    tab = await bridge.open_tab()
    with pytest.raises(BridgeError, match="timeout"):
        await ExtensionTransport(bridge, tab, timeout=0.3).send("Page.navigate", {"url": "x"})


async def test_no_extension_error(bridge: Bridge):
    with pytest.raises(BridgeError, match="not connected"):
        await bridge.open_tab()


async def test_extension_disconnect_fails_pending(bridge: Bridge, ext: FakeExtension):
    ext.silent_methods.add("Page.navigate")
    tab = await bridge.open_tab()
    pending = asyncio.create_task(ExtensionTransport(bridge, tab).send("Page.navigate", {"url": "x"}))
    await asyncio.sleep(0.1)
    await ext.stop()
    with pytest.raises(BridgeError, match="disconnected"):
        await pending


async def test_second_instance_becomes_peer(bridge: Bridge, home):
    peer = Bridge(port=bridge.port, home=home)
    assert await peer.start() == "peer"
    bridge.peer_handler = _echo
    assert await peer.call_hub("echo", {"x": 1}, timeout=5) == {"op": "echo", "args": {"x": 1}}


async def _echo(op, args):
    return {"op": op, "args": args}
