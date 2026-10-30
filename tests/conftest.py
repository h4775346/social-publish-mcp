from __future__ import annotations

import asyncio
import base64
import json
import socket
from pathlib import Path
from typing import Any

import pytest
from websockets.asyncio.client import ClientConnection, connect

from social_publish_mcp.bridge import Bridge

# 1x1 transparent PNG
PNG = base64.b64encode(bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")).decode()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeExtension:
    """Plays the Chrome extension: answers open_tab / cdp / close_tab like sw.js does."""

    def __init__(self, port: int, ext_id: str = "fakeextensionid") -> None:
        self.port = port
        self.ext_id = ext_id
        self.ws: ClientConnection | None = None
        self.calls: list[dict[str, Any]] = []
        self.open_tabs: set[int] = set()
        self.silent_methods: set[str] = set()  # cdp methods never answered
        self.failing_methods: dict[str, str] = {}  # cdp method -> error text
        self._next_tab = 100
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> dict[str, Any]:
        self.ws = await connect(f"ws://127.0.0.1:{self.port}", origin=f"chrome-extension://{self.ext_id}")  # type: ignore[arg-type]
        await self.ws.send(json.dumps({"type": "hello", "role": "extension", "version": "test"}))
        welcome = json.loads(await self.ws.recv())
        self._task = asyncio.create_task(self._loop())
        return welcome

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        if self.ws:
            await self.ws.close()

    async def event(self, event: str, tab_id: int, reason: str = "") -> None:
        assert self.ws
        await self.ws.send(json.dumps({"type": "event", "event": event, "tabId": tab_id, "reason": reason}))

    async def _loop(self) -> None:
        assert self.ws
        async for raw in self.ws:
            msg = json.loads(raw)
            if "id" not in msg:
                continue
            self.calls.append(msg)
            reply = self._answer(msg)
            if reply is not None:
                await self.ws.send(json.dumps({"type": "response", "id": msg["id"], **reply}))

    def _answer(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        t = msg["type"]
        if t == "open_tab":
            self._next_tab += 1
            self.open_tabs.add(self._next_tab)
            return {"ok": True, "result": {"tabId": self._next_tab}}
        if t == "close_tab":
            self.open_tabs.discard(msg["tabId"])
            return {"ok": True, "result": {}}
        if t == "activate_tab":
            return {"ok": True, "result": {}}
        if t == "list_tabs":
            return {"ok": True, "result": {"tabs": [{"tabId": i} for i in sorted(self.open_tabs)]}}
        if t == "cdp":
            m = msg["method"]
            if m in self.silent_methods:
                return None
            if m in self.failing_methods:
                return {"ok": False, "error": self.failing_methods[m]}
            if m == "Runtime.evaluate":
                expr = msg["params"].get("expression", "")
                if "getBoundingClientRect" in expr:  # Page.center()
                    return {"ok": True, "result": {"result": {"type": "object", "value": [10, 20]}}}
                return {"ok": True, "result": {"result": {"type": "boolean", "value": True}}}
            if m == "Page.captureScreenshot":
                return {"ok": True, "result": {"data": PNG}}
            return {"ok": True, "result": {}}
        return {"ok": False, "error": f"unknown request type {t}"}


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return tmp_path / "state"


@pytest.fixture
async def bridge(home: Path):
    b = Bridge(port=free_port(), home=home)
    assert await b.start() == "hub"
    yield b
    await b.stop()


@pytest.fixture
async def ext(bridge: Bridge):
    e = FakeExtension(bridge.port)
    await e.start()
    assert await bridge.wait_extension(5)
    yield e
    await e.stop()


@pytest.fixture
def video(tmp_path: Path) -> Path:
    p = tmp_path / "clip.mp4"
    p.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64)
    return p
