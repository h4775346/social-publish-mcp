"""Page: the small browser API every platform driver uses.

It speaks Chrome DevTools Protocol (CDP) through a transport with one method, ``send(method, params)``.
The real transport is the companion extension (chrome.debugger attached to one tab, relayed over a local
WebSocket); tests use a fake. Because input goes through CDP, clicks and typing are *trusted* events
(rich editors like TikTok's and Facebook's accept them) and files are set by path with
DOM.setFileInputFiles (no size limit, no native file dialog).

Selectors: every ``sel`` argument is a JavaScript *expression* that evaluates to an Element (or null), e.g.
``document.querySelector('ytcp-uploads-dialog #next-button')`` or a helper like ``byText('button', 'Post')``.
Plain CSS strings are accepted too and wrapped in document.querySelector(...).
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
from pathlib import Path
from typing import Any, Protocol


class Transport(Protocol):
    async def send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]: ...


# Injected once per document: small helpers drivers can use inside selector expressions.
HELPERS_JS = r"""
(() => {
  if (window.__sp) return;
  const visible = el => !!el && el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden';
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  window.__sp = {
    visible,
    // first visible element matching css whose text (or aria-label) contains/equals txt
    byText(css, txt, exact = false) {
      for (const el of document.querySelectorAll(css)) {
        const t = norm(el.innerText || el.textContent), a = norm(el.getAttribute('aria-label'));
        if (!visible(el)) continue;
        if (exact ? (t === txt || a === txt) : (t.includes(txt) || a.includes(txt))) return el;
      }
      return null;
    },
    all(css) { return [...document.querySelectorAll(css)].filter(visible); },
  };
})();
"""

_KEYS: dict[str, tuple[str, int]] = {  # key -> (code, windowsVirtualKeyCode)
    "Enter": ("Enter", 13), "Escape": ("Escape", 27), "Backspace": ("Backspace", 8), "Delete": ("Delete", 46),
    "Tab": ("Tab", 9), "ArrowDown": ("ArrowDown", 40), "ArrowUp": ("ArrowUp", 38), "ArrowLeft": ("ArrowLeft", 37),
    "ArrowRight": ("ArrowRight", 39), "Home": ("Home", 36), "End": ("End", 35), "a": ("KeyA", 65), " ": ("Space", 32),
}
_MODS = {"alt": 1, "ctrl": 2, "meta": 4, "shift": 8}


class PageError(RuntimeError):
    pass


def _sel(sel: str) -> str:
    s = sel.strip()
    if s.startswith(("document.", "__sp.", "window.", "(", "[")) or "=>" in s:
        return s
    return f"document.querySelector({json.dumps(s)})"


class Page:
    def __init__(self, transport: Transport, *, artifacts_dir: Path | None = None) -> None:
        self.t = transport
        self.artifacts_dir = artifacts_dir or Path.cwd()

    # ------------------------------------------------------------ navigation / JS
    async def goto(self, url: str, *, timeout: float = 45) -> None:
        await self.t.send("Page.navigate", {"url": url})
        await asyncio.sleep(0.5)
        await self.wait_for("document.readyState === 'complete'", timeout=timeout, what=f"load {url}")

    async def eval(self, js: str, *, await_promise: bool = True) -> Any:
        """Evaluate an expression in the page (helpers available as __sp) and return its JSON value."""
        await self._helpers()
        r = await self.t.send("Runtime.evaluate", {
            "expression": js, "awaitPromise": await_promise, "returnByValue": True, "userGesture": True})
        if r.get("exceptionDetails"):
            d = r["exceptionDetails"]
            raise PageError((d.get("exception") or {}).get("description") or d.get("text") or "JS error")
        return (r.get("result") or {}).get("value")

    async def wait_for(self, js: str, *, timeout: float = 30, interval: float = 0.4, what: str = "") -> Any:
        """Poll a JS expression until it is truthy; return its value."""
        end = time.monotonic() + timeout
        last_err = ""
        while True:
            try:
                v = await self.eval(js)
                if v:
                    return v
            except PageError as e:
                last_err = str(e)
            if time.monotonic() > end:
                raise PageError(f"timeout after {timeout:.0f}s waiting for {what or js}" + (f" ({last_err})" if last_err else ""))
            await asyncio.sleep(interval)

    async def exists(self, sel: str) -> bool:
        return bool(await self.eval(f"!!({_sel(sel)})"))

    async def wait_el(self, sel: str, *, timeout: float = 30, visible: bool = True) -> None:
        cond = f"(e => e && {'__sp.visible(e)' if visible else 'true'})({_sel(sel)})"
        await self.wait_for(cond, timeout=timeout, what=f"element {sel}")

    # ------------------------------------------------------------ input (trusted, via CDP)
    async def center(self, sel: str) -> tuple[float, float]:
        box = await self.eval(
            f"(e => {{ if (!e) return null; e.scrollIntoView({{block:'center', inline:'center'}});"
            f" const r = e.getBoundingClientRect(); return [r.x + r.width/2, r.y + r.height/2]; }})({_sel(sel)})")
        if not box:
            raise PageError(f"not found: {sel}")
        await asyncio.sleep(0.15)
        return float(box[0]), float(box[1])

    async def click(self, sel: str | None = None, *, xy: tuple[float, float] | None = None, wait: float = 10) -> None:
        if sel is not None:
            await self.wait_el(sel, timeout=wait)
            xy = await self.center(sel)
        if xy is None:
            raise ValueError("click needs sel or xy")
        x, y = xy
        for t in ("mouseMoved", "mousePressed", "mouseReleased"):
            p = {"type": t, "x": x, "y": y}
            if t != "mouseMoved":
                p.update(button="left", clickCount=1)
            await self.t.send("Input.dispatchMouseEvent", p)
        await asyncio.sleep(0.2)

    async def js_click(self, sel: str) -> None:
        """element.click() — for elements that ignore pointer events or sit under overlays."""
        ok = await self.eval(f"(e => {{ if (!e) return false; e.click(); return true; }})({_sel(sel)})")
        if not ok:
            raise PageError(f"not found: {sel}")

    async def focus(self, sel: str) -> None:
        await self.click(sel)

    async def insert_text(self, text: str) -> None:
        """Type text into the focused element as one trusted input (emoji/RTL safe)."""
        await self.t.send("Input.insertText", {"text": text})
        await asyncio.sleep(0.1)

    async def press(self, key: str, *mods: str) -> None:
        code, vk = _KEYS.get(key, (key, 0))
        m = sum(_MODS[x] for x in mods)
        base = {"key": key, "code": code, "windowsVirtualKeyCode": vk, "modifiers": m}
        await self.t.send("Input.dispatchKeyEvent", {"type": "keyDown", **base})
        await self.t.send("Input.dispatchKeyEvent", {"type": "keyUp", **base})
        await asyncio.sleep(0.05)

    async def bring_to_front(self) -> None:
        """Activate this tab. Background tabs pause animations, so animated pickers stay collapsed there."""
        await self.t.send("Page.bringToFront", {})
        await asyncio.sleep(0.3)

    async def type_keys(self, text: str) -> None:
        """Type ASCII text one key event per character — for masked/spinbutton inputs that ignore insertText."""
        for ch in text:
            code = f"Digit{ch}" if ch.isdigit() else f"Key{ch.upper()}" if ch.isalpha() else _KEYS.get(ch, (ch, 0))[0]
            vk = ord(ch.upper()) if ch.isalnum() else _KEYS.get(ch, (ch, 0))[1]
            base = {"key": ch, "code": code, "windowsVirtualKeyCode": vk}
            await self.t.send("Input.dispatchKeyEvent", {"type": "keyDown", "text": ch, "unmodifiedText": ch, **base})
            await self.t.send("Input.dispatchKeyEvent", {"type": "keyUp", **base})
            await asyncio.sleep(0.05)

    async def select_all_and_delete(self) -> None:
        await self.press("a", "ctrl")
        await self.press("Delete")

    # ------------------------------------------------------------ files / screenshots
    async def set_files(self, sel: str, paths: list[str | Path]) -> None:
        """Attach local files to an <input type=file> (it may be hidden)."""
        await self._helpers()
        r = await self.t.send("Runtime.evaluate", {"expression": _sel(sel)})
        obj = (r.get("result") or {}).get("objectId")
        if not obj:
            raise PageError(f"file input not found: {sel}")
        files = [str(Path(p).resolve()) for p in paths]
        await self.t.send("DOM.setFileInputFiles", {"files": files, "objectId": obj})

    async def screenshot(self, name: str) -> str:
        r = await self.t.send("Page.captureScreenshot", {"format": "png"})
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        path = self.artifacts_dir / f"{name}.png"
        path.write_bytes(base64.b64decode(r["data"]))
        return str(path)

    async def text(self, sel: str = "document.body") -> str:
        return await self.eval(f"(e => e ? e.innerText : '')({_sel(sel)})") or ""

    # ------------------------------------------------------------
    async def _helpers(self) -> None:
        await self.t.send("Runtime.evaluate", {"expression": HELPERS_JS})
