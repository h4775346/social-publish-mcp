"""Test doubles for platform drivers: a FakePage with the same API as page.Page, and a FakeTransport.

FakePage resolves every selector / JS expression against ``rules``: an ordered list of (substring, value);
the first rule whose substring occurs in the expression wins. value may be
  * a plain value (returned every time),
  * a list (one item per call; the last item repeats),
  * a callable taking the expression.
Unmatched expressions return ``default`` (True, i.e. "element exists / condition holds").
wait_for / wait_el never sleep: a falsy value raises PageError at once, like a timeout.
Every call is appended to ``calls`` as a tuple, e.g. ("click", sel), ("text", "abc"), ("press", "Enter", ("shift",)).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from social_publish_mcp.page import PageError


class FakePage:
    def __init__(self, rules: list[tuple[str, Any]] | None = None, *, default: Any = True) -> None:
        self.rules: list[tuple[str, Any]] = list(rules or [])
        self.default = default
        self.calls: list[tuple] = []
        self.url = ""

    # -- scripting
    def resolve(self, js: str) -> Any:
        for key, val in self.rules:
            if key in js:
                if callable(val):
                    return val(js)
                if isinstance(val, list):
                    return val.pop(0) if len(val) > 1 else (val[0] if val else None)
                return val
        return self.default

    def ops(self, *kinds: str) -> list[tuple]:
        return [c for c in self.calls if not kinds or c[0] in kinds]

    # -- Page API
    async def goto(self, url: str, *, timeout: float = 45) -> None:
        self.url = url
        self.calls.append(("goto", url))

    async def eval(self, js: str, *, await_promise: bool = True) -> Any:
        self.calls.append(("eval", js))
        return self.resolve(js)

    async def wait_for(self, js: str, *, timeout: float = 30, interval: float = 0.4, what: str = "") -> Any:
        self.calls.append(("wait_for", js))
        v = self.resolve(js)
        if not v:
            raise PageError(f"timeout after {timeout:.0f}s waiting for {what or js}")
        return v

    async def exists(self, sel: str) -> bool:
        self.calls.append(("exists", sel))
        return bool(self.resolve(sel))

    async def wait_el(self, sel: str, *, timeout: float = 30, visible: bool = True) -> None:
        self.calls.append(("wait_el", sel))
        if not self.resolve(sel):
            raise PageError(f"timeout after {timeout:.0f}s waiting for element {sel}")

    async def center(self, sel: str) -> tuple[float, float]:
        if not self.resolve(sel):
            raise PageError(f"not found: {sel}")
        return 10.0, 10.0

    async def click(self, sel: str | None = None, *, xy: tuple[float, float] | None = None, wait: float = 10) -> None:
        if sel is not None and not self.resolve(sel):
            raise PageError(f"timeout after {wait:.0f}s waiting for element {sel}")
        self.calls.append(("click", sel if sel is not None else xy))

    async def js_click(self, sel: str) -> None:
        if not self.resolve(sel):
            raise PageError(f"not found: {sel}")
        self.calls.append(("js_click", sel))

    async def focus(self, sel: str) -> None:
        await self.click(sel)

    async def insert_text(self, text: str) -> None:
        self.calls.append(("text", text))

    async def press(self, key: str, *mods: str) -> None:
        self.calls.append(("press", key, tuple(mods)))

    async def type_keys(self, text: str) -> None:
        self.calls.append(("type_keys", text))

    async def bring_to_front(self) -> None:
        self.calls.append(("bring_to_front",))

    async def select_all_and_delete(self) -> None:
        await self.press("a", "ctrl")
        await self.press("Delete")

    async def set_files(self, sel: str, paths: list[str | Path]) -> None:
        if not self.resolve(sel):
            raise PageError(f"file input not found: {sel}")
        self.calls.append(("set_files", sel, [str(p) for p in paths]))

    async def screenshot(self, name: str) -> str:
        self.calls.append(("screenshot", name))
        return f"{name}.png"

    async def text(self, sel: str = "document.body") -> str:
        self.calls.append(("text_of", sel))
        return self.resolve(sel) or ""


class FakeTransport:
    """Records CDP sends; ``results`` maps a CDP method to a scripted result dict (or a callable(params))."""

    def __init__(self, results: dict[str, Any] | None = None) -> None:
        self.results = results or {}
        self.sent: list[tuple[str, dict]] = []

    async def send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.sent.append((method, params or {}))
        r = self.results.get(method, {})
        return r(params or {}) if callable(r) else r
