"""TikTok Studio (web) upload driver: tiktok.com/tiktokstudio/upload.

Flow (docs/platform_notes.md, "TikTok Studio (web)"): attach the video to the hidden ``input[type=file]`` ->
wait for "Uploaded (xx MB)" -> replace the prefilled Description (Draft.js editor, role=combobox) with trusted
typing (Escape after every hashtag closes the suggestion list) and read it back -> "Who can see this post" ->
"Show more" options (AI-generated content toggle, comments) -> optional "Schedule" (date calendar + hours/minutes
wheel) -> "Post" / "Schedule" -> tiktokstudio/content -> read the new row's status.

Time zone: the schedule pickers show the *browser's* local time (Cairo GMT+2 for the test account, which is also
the zone the content list prints). The driver asks the page for its UTC offset at the scheduled instant
(``new Date(ms).getTimezoneOffset()``) and converts ``job.schedule_at`` to that offset; if the page does not
answer it falls back to the machine's local zone (``schedule_at.astimezone()``). UNVERIFIED: that the pickers
follow the browser zone rather than an account setting (both were Cairo when observed).

Minutes: the wheel only has 5-minute steps, so the time is rounded *up* to the next multiple of 5 and the
rounded value is reported in PostResult.scheduled_for / message.

Selectors marked ``# UNVERIFIED`` were not observed live; check them before trusting the driver. Every JS
expression carries a ``/*tt:<name>*/`` marker so tests (and debug logs) can tell them apart.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from ..models import DriverError, PostJob, PostResult
from ..page import Page, PageError

START_URL = "https://www.tiktok.com/tiktokstudio/upload"
CONTENT_URL_PART = "tiktokstudio/content"

MAX_CAPTION = 4000
MIN_LEAD = timedelta(minutes=15)
MAX_LEAD = timedelta(days=10)
UPLOAD_TIMEOUT = 900.0
STEP_TIMEOUT = 30.0
SUBMIT_READY_TIMEOUT = 180.0  # Post/Schedule stays disabled while the copyright/content checks run
FINISH_TIMEOUT = 120.0
WHEEL_MAX_STEPS = 40
WHEEL_SETTLE = 0.25  # seconds after a wheel click (the list animates/re-centers)
HASHTAG_SETTLE = 0.3  # seconds between typing a hashtag + space and pressing Escape

VIS_LABELS = {"everyone": "Everyone", "friends": "Friends", "only_me": "Only you"}  # labels verified
NEWLINE_KEY: tuple[str, ...] = ("Enter", "shift")  # UNVERIFIED: plain Enter is probably fine too in TikTok's Draft.js


def _js(s: Any) -> str:
    return json.dumps(s, ensure_ascii=False)


_N = "const n = s => (s || '').replace(/\\s+/g, ' ').trim();"

# ------------------------------------------------------------------ selectors
# Verified: hidden input[type=file][accept="video/*"] on the upload page.
FILE_INPUT = ("(document.querySelector('input[type=file][accept=\"video/*\"]')"
              " || document.querySelector('input[type=file][accept*=\"video\"]')"
              " || document.querySelector('input[type=file]'))/*tt:file*/")
# Verified: Draft.js contenteditable with role=combobox under "Description". Fallbacks UNVERIFIED.
EDITOR = ("(document.querySelector('[contenteditable=true][role=combobox]')"
          " || document.querySelector('.public-DraftEditor-content[contenteditable=true]')"
          " || document.querySelector('[contenteditable=true]'))/*tt:editor*/")

LOGIN_JS = r"""(() => {
  const t = document.body ? document.body.innerText : '';
  const upload = !!document.querySelector('input[type=file]') || /Select video to upload/i.test(t);
  const login = /\/login/.test(location.href) || (!upload && !!__sp.byText('button,a,div[role=button]', 'Log in', true));
  let name = '';
  const img = document.querySelector('[data-e2e="profile-icon"] img, header img[alt], [class*="avatar" i] img[alt]');
  if (img && img.alt && !/avatar|profile/i.test(img.alt)) name = img.alt.trim();
  if (!name) {
    const el = document.querySelector('[class*="preview" i] [class*="user" i], [class*="preview" i] [class*="name" i]');
    if (el) name = (el.innerText || '').trim().split('\n')[0];
  }
  return {url: location.href, upload, login, name};
})()/*tt:login*/"""  # upload/login checks follow the notes; account-name selectors UNVERIFIED

UPLOAD_JS = r"""(() => {
  const t = document.body.innerText || '';
  if (/upload failed|couldn.t upload|can.t upload|upload error|unsupported/i.test(t)) return 'error';
  if (/\bUploaded\b/.test(t)) return 'done';
  return '';
})()/*tt:uploaded*/"""  # "Uploaded (20.21MB)" verified; error strings UNVERIFIED


def _label_ctl(label: str, part: str) -> str:
    """The switch/checkbox belonging to a text label; part='el' returns a clickable element, 'state' -> 'on'/'off'/null.

    UNVERIFIED structure: label text and control are assumed to share an ancestor within 4 levels.
    """
    return f"""((label, part) => {{
  {_N}
  const lab = [...document.querySelectorAll('span,div,label,p')].find(e => __sp.visible(e) && n(e.innerText) === label);
  if (!lab) return null;
  let p = lab, c = null;
  for (let i = 0; i < 4 && p && !c; i++) {{
    c = p.querySelector('[role=switch],[role=checkbox],input[type=checkbox]');
    p = p.parentElement;
  }}
  if (!c) return null;
  if (part === 'state') {{
    const a = c.getAttribute('aria-checked');
    const on = a !== null ? a === 'true' : (c.checked !== undefined ? !!c.checked : /checked|\\bon\\b/i.test(c.className));
    return on ? 'on' : 'off';
  }}
  return (c.matches('input') && !__sp.visible(c)) ? (c.closest('label') || c.parentElement) : c;
}})({_js(label)}, {_js(part)})/*tt:ctl:{label}:{part}*/"""


VIS_DROPDOWN = f"""(() => {{
  {_N}
  const lab = [...document.querySelectorAll('div,span,p,label,h3')]
    .find(e => __sp.visible(e) && n(e.innerText) === 'Who can see this post');
  if (!lab) return null;
  let p = lab;
  for (let i = 0; i < 5 && p.parentElement; i++) {{
    p = p.parentElement;
    const c = [...p.querySelectorAll('[role=combobox],[aria-haspopup],button,[class*="select" i]')]
      .find(e => __sp.visible(e) && !e.contains(lab) && !lab.contains(e));
    if (c) return c;
  }}
  return null;
}})()/*tt:visdrop*/"""  # label verified; dropdown element UNVERIFIED


def _vis_option(label: str) -> str:
    return f"__sp.byText('[role=option],li,[class*=\"option\" i]', {_js(label)}, true)/*tt:visopt*/"  # UNVERIFIED


SHOW_MORE = "__sp.byText('button,[role=button],div,span', 'Show more', true)/*tt:showmore*/"  # text verified

# Verified: real radio inputs value="post_now" / value="schedule" (checked / aria-checked reflect the state).
SCHEDULE_RADIO = """(i => i ? (__sp.visible(i) ? i : (i.closest('label') || i.parentElement)) : null)(
  document.querySelector('input[type=radio][value="schedule"]'))/*tt:schedradio*/"""

SCHEDULE_CHECKED = """(i => i ? (i.checked || i.getAttribute('aria-checked') === 'true') : null)(
  document.querySelector('input[type=radio][value="schedule"]'))/*tt:schedchecked*/"""


def _picker(kind: str) -> str:
    """The read-only date ("YYYY-MM-DD") or time ("HH:MM") input shown after selecting Schedule (verified:
    ``input.TUXTextInputCore-input``; the value is in .value, not text). A trusted click opens its picker."""
    rx = r"^\d{4}-\d{2}-\d{2}$" if kind == "date" else r"^\d{2}:\d{2}$"
    return (f"[...document.querySelectorAll('input.TUXTextInputCore-input')].find(e => /{rx}/.test((e.value || '').trim()))"
            f"/*tt:picker:{kind}*/")


def _picker_text(kind: str) -> str:
    return f"(e => e ? (e.value || '').trim() : '')({_picker(kind)})/*tt:pickertext:{kind}*/"


def _day_cell(day: int) -> str:
    """Calendar cell for ``day`` of the shown month (verified: ``span.day`` cells; selectable ones have class ``valid``,
    and only the shown month's days are valid)."""
    return f"""((d) => {{
  {_N}
  return [...document.querySelectorAll('span.day.valid')].find(e => __sp.visible(e) && +n(e.innerText) === d) || null;
}})({day})/*tt:day:{day}*/"""


# Verified: two span.arrow month arrows; the second one is "next month".
NEXT_MONTH = "(a => a.length > 1 ? a[a.length - 1] : null)(__sp.all('span.arrow'))/*tt:nextmonth*/"

# Hours/minutes wheel (verified): every option is in the DOM as span.tiktok-timepicker-option-text with
# .tiktok-timepicker-left (hours) / .tiktok-timepicker-right (minutes), parent .tiktok-timepicker-option-item, clipped by
# .tiktok-timepicker-time-scroll-container (overflow hidden). Active item: class tiktok-timepicker-is-active (on the span
# or its parent). Only items intersecting the clip container are clickable; element.click() does not select, a trusted
# click does, and scrollIntoView does not stick (the wheel re-centers on the active value).
_WHEEL_FN = r"""const __ttWheel = (kind) => {
  const side = kind === 'm' ? 'right' : 'left';
  const spans = [...document.querySelectorAll('span.tiktok-timepicker-option-text.tiktok-timepicker-' + side)];
  if (!spans.length) return null;
  // clickable = the browser's hit test at the item's centre lands on it (the clip container's own rect proved unreliable:
  // a collapsed duplicate picker with height 0 shares the same classes)
  const inView = spans.filter(e => {
    const r = e.getBoundingClientRect();
    if (!r.width || !r.height) return false;
    const x = r.left + r.width / 2, y = r.top + r.height / 2;
    if (x < 0 || y < 0 || x > innerWidth || y > innerHeight) return false;
    const hit = document.elementFromPoint(x, y);
    return !!hit && (hit === e || e.contains(hit) || (e.parentElement && e.parentElement.contains(hit)));
  });
  const sized = spans.filter(e => e.getBoundingClientRect().height > 0).length;
  const active = e => e.classList.contains('tiktok-timepicker-is-active') ||
    !!(e.parentElement && e.parentElement.classList.contains('tiktok-timepicker-is-active'));
  let selected = null;
  const a = spans.find(active);
  if (a) selected = +a.innerText.trim();
  else {
    const t = [...document.querySelectorAll('input.TUXTextInputCore-input')].find(e => /^\d{2}:\d{2}$/.test(e.value || ''));
    if (t) selected = +(kind === 'm' ? t.value.slice(3, 5) : t.value.slice(0, 2));
  }
  return {items: inView, vals: inView.map(e => +e.innerText.trim()), selected, total: spans.length, sized};
};"""


def _wheel_state(kind: str) -> str:
    return (f"(() => {{ {_WHEEL_FN} const w = __ttWheel({_js(kind)}); if (!w) return null;"
            f" return {{selected: w.selected, items: w.vals, total: w.total, sized: w.sized}}; }})()/*tt:wheel:{kind}*/")


def _wheel_item(kind: str, value: int) -> str:
    """Viewport [x, y] of a visible wheel item (no scrollIntoView: the wheel would re-center before the click)."""
    return (f"(() => {{ {_WHEEL_FN} const w = __ttWheel({_js(kind)}); if (!w) return null;"
            f" const e = w.items.find(e => +e.innerText.trim() === {value}); if (!e) return null;"
            f" const r = e.getBoundingClientRect(); return [r.x + r.width / 2, r.y + r.height / 2]; }})()"
            f"/*tt:wheelitem:{kind}:{value:02d}*/")


def _button(label: str) -> str:
    """Bottom submit button: exact text "Post" or "Schedule" (button tag so the "Schedule" radio is not hit)."""
    fallback = " || document.querySelector('[data-e2e=\"post_video_button\"]')" if label == "Post" else ""  # UNVERIFIED
    return f"(__sp.byText('button', {_js(label)}, true){fallback})/*tt:submit:{label}*/"


def _enabled(sel: str) -> str:
    return (f"(e => !!e && __sp.visible(e) && !e.disabled && e.getAttribute('aria-disabled') !== 'true'"
            f" && !/disabled/i.test(e.className || ''))({sel})")


POST_NOW_MODAL = "__sp.byText('button', 'Post now', true)/*tt:postnow*/"  # UNVERIFIED ("checks still running" modal)


def _row_js(prefix: str) -> str:
    """Text of the content-list row whose caption starts with ``prefix`` (or the first row with a status if empty)."""
    return f"""((pre) => {{
  {_N}
  const st = /under review|Scheduled|\\(GMT[+-]?\\d*\\)|Posted|Published|Processing|Only you|Friends|Everyone|Private|Public/i;
  const cands = pre
    ? [...document.querySelectorAll('*')].filter(e => __sp.visible(e) && n(e.innerText).startsWith(pre)
        && ![...e.children].some(c => n(c.innerText).startsWith(pre)))
    : [];
  for (const el of cands) {{
    let p = el;
    for (let i = 0; i < 8 && p; i++, p = p.parentElement) {{
      const t = n(p.innerText);
      if (t.length > pre.length + 3 && st.test(t.slice(pre.length))) return p.innerText;
    }}
  }}
  return '';
}})({_js(prefix)})/*tt:row*/"""  # UNVERIFIED row structure (row text content verified)


TZ_JS = "(ms => [-new Date(ms).getTimezoneOffset(), Intl.DateTimeFormat().resolvedOptions().timeZone || ''])({ms})/*tt:tz*/"


# ------------------------------------------------------------------ helpers
async def _sleep(s: float) -> None:  # patched in tests
    if s > 0:
        await asyncio.sleep(s)


def _now() -> datetime:  # patched in tests
    return datetime.now(timezone.utc)


class _Step:
    """async with _Step(page, "upload"): ... -> any failure becomes DriverError(step, detail, screenshot)."""

    def __init__(self, page: Page, name: str) -> None:
        self.page, self.name = page, name

    async def __aenter__(self) -> _Step:
        return self

    async def __aexit__(self, et: Any, e: BaseException | None, tb: Any) -> bool:
        if e is None or not isinstance(e, Exception):
            return False
        if isinstance(e, DriverError):
            if not e.screenshot:
                e.screenshot = await _shot(self.page, e.step)
            return False
        raise DriverError(self.name, str(e) or type(e).__name__, await _shot(self.page, self.name)) from e


async def _shot(page: Page, step: str) -> str:
    try:
        return await page.screenshot(f"tiktok_{step}_{int(time.time())}")
    except Exception:  # noqa: BLE001 - a failed screenshot must not hide the real error
        return ""


def _norm(s: str) -> str:
    s = s.replace("​", "").replace("﻿", "").replace(" ", " ")
    return re.sub(r"\s+", " ", s).strip()


_HASHTAG = re.compile(r"(#[^\s#]+)")


def caption_plan(caption: str) -> list[tuple[str, ...]]:
    """The exact input sequence for a caption: ("text", s) / ("key", name, *mods).

    Each hashtag is typed, then a space, then Escape (closes TikTok's hashtag suggestion list, which would otherwise
    take the next Enter/characters); that typed space replaces one leading space of the following segment. Lines are
    joined with NEWLINE_KEY. Emoji / Arabic segments go through as single insertText calls.
    """
    ops: list[tuple[str, ...]] = []
    lines = caption.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for i, line in enumerate(lines):
        if i:
            ops.append(("key", *NEWLINE_KEY))
        after_tag = False
        for seg in _HASHTAG.split(line):
            if not seg:
                continue
            if after_tag and seg[0] in " \t":
                seg = seg[1:]
            if _HASHTAG.fullmatch(seg):
                ops += [("text", seg), ("text", " "), ("key", "Escape")]
                after_tag = True
            else:
                if seg:
                    ops.append(("text", seg))
                after_tag = False
    return ops


def round_up_5(dt: datetime) -> datetime:
    """Round up to the next whole 5 minutes (seconds count: 20:00:30 -> 20:05)."""
    base = dt.replace(second=0, microsecond=0)
    if base != dt:
        base += timedelta(minutes=1)
    return base + timedelta(minutes=(-base.minute) % 5)


def schedule_target(when: datetime, *, now: datetime | None = None, utc_offset_min: int | None = None) -> datetime:
    """``when`` in the picker's zone (fixed offset if given, else this machine's), rounded up to 5 min, window-checked."""
    if when.tzinfo is None:
        raise DriverError("schedule", "schedule_at must be timezone-aware")
    tz = timezone(timedelta(minutes=utc_offset_min)) if utc_offset_min is not None else None
    target = round_up_5(when.astimezone(tz))
    now = now or _now()
    lead = target - now
    if lead < MIN_LEAD:
        raise DriverError("schedule", f"{target.isoformat()} is less than 15 minutes ahead (TikTok minimum)")
    if lead > MAX_LEAD:
        raise DriverError("schedule", f"{target.isoformat()} is more than 10 days ahead (TikTok maximum)")
    return target


async def _browser_offset(page: Page, when: datetime) -> tuple[int | None, str]:
    try:
        v = await page.eval(TZ_JS.format(ms=int(when.timestamp() * 1000)))
    except Exception:  # noqa: BLE001 - fall back to the machine zone
        return None, ""
    if isinstance(v, list) and v and isinstance(v[0], (int, float)) and not isinstance(v[0], bool):
        return int(v[0]), str(v[1]) if len(v) > 1 else ""
    return None, ""


async def _pick_wheel(page: Page, column: str, target: int) -> None:
    """Click visible items of the hours ('h') or minutes ('m') wheel until ``target`` is the selected value.

    Each click re-centers the list with ~3 items visible on each side, so every step clicks the visible item closest
    to the target in the right direction (the target itself once it is visible). Bounded by WHEEL_MAX_STEPS and
    fails if a click does not change the selection twice in a row.
    """
    stuck = 0
    last: int | None = None
    for _ in range(WHEEL_MAX_STEPS):
        st = await page.eval(_wheel_state(column))
        if not isinstance(st, dict) or st.get("selected") is None:
            raise DriverError("schedule", f"time wheel '{column}' not found")
        sel, items = int(st["selected"]), [int(v) for v in st.get("items") or []]
        if sel == target:
            return
        if last is not None and sel == last:
            stuck += 1
            if stuck >= 2:
                raise DriverError("schedule", f"time wheel '{column}' does not move (stuck at {sel:02d}, want {target:02d})")
        else:
            stuck = 0
        last = sel
        side = [v for v in items if (v > sel if target > sel else v < sel)]
        if not side:
            raise DriverError("schedule", f"time wheel '{column}': no item toward {target:02d} (visible {items},"
                                          f" selected {sel:02d}, {st.get('sized')}/{st.get('total')} items have a size)")
        pick = min(side, key=lambda v: abs(v - target))
        xy = await page.eval(_wheel_item(column, pick))
        if not (isinstance(xy, list) and len(xy) == 2):
            raise DriverError("schedule", f"time wheel '{column}': item {pick:02d} not clickable")
        await page.click(xy=(float(xy[0]), float(xy[1])))
        await _sleep(WHEEL_SETTLE)
    raise DriverError("schedule", f"time wheel '{column}' did not reach {target:02d} in {WHEEL_MAX_STEPS} clicks")


async def _type_caption(page: Page, caption: str) -> None:
    for op in caption_plan(caption):
        if op[0] == "text":
            await page.insert_text(op[1])
        else:
            if op[1] == "Escape":
                await _sleep(HASHTAG_SETTLE)
            await page.press(op[1], *op[2:])


async def _fill_caption(page: Page, caption: str) -> None:
    want = _norm(caption)
    got = ""
    for _attempt in range(2):
        await page.click(EDITOR, wait=STEP_TIMEOUT)
        await page.select_all_and_delete()
        if _norm(str(await page.text(EDITOR) or "")):  # the file-name prefill sometimes survives one Delete
            await page.press("a", "ctrl")
            await page.press("Backspace")
        await _type_caption(page, caption)
        await _sleep(0.3)
        got = _norm(str(await page.text(EDITOR) or ""))
        if got == want:
            return
    raise DriverError("caption", f"read-back mismatch: want {want[:80]!r}, got {got[:80]!r}")


async def _set_visibility(page: Page, visibility: str) -> str:
    label = VIS_LABELS[visibility]
    cur = _norm(str(await page.text(VIS_DROPDOWN) or ""))
    if cur == label:
        return ""
    if not await page.exists(VIS_DROPDOWN):
        if visibility == "everyone":  # TikTok's default
            return "'Who can see this post' dropdown not found; left as TikTok's default (Everyone)"
        raise DriverError("visibility", "'Who can see this post' dropdown not found")
    await page.click(VIS_DROPDOWN)
    await page.click(_vis_option(label), wait=STEP_TIMEOUT)
    await _sleep(0.3)
    cur = _norm(str(await page.text(VIS_DROPDOWN) or ""))
    if label not in cur:
        raise DriverError("visibility", f"dropdown shows {cur!r}, want {label!r}")
    return ""


async def _set_toggle(page: Page, label: str, want_on: bool, *, required_when_on: bool) -> str:
    """Make the switch/checkbox next to ``label`` match want_on; returns a note ('' when nothing to report)."""
    state = await page.eval(_label_ctl(label, "state"))
    if state not in ("on", "off"):
        if want_on == (not required_when_on):  # the default we want is what TikTok has by default
            return f"'{label}' control not found; left as TikTok's default"
        raise DriverError("options", f"'{label}' control not found")
    if (state == "on") == want_on:
        return ""
    await page.click(_label_ctl(label, "el"))
    await _sleep(0.3)
    state = await page.eval(_label_ctl(label, "state"))
    if (state == "on") != want_on:
        raise DriverError("options", f"'{label}' is {state}, want {'on' if want_on else 'off'}")
    return ""


async def _set_options(page: Page, ai_generated: bool, allow_comments: bool) -> list[str]:
    notes: list[str] = []
    if await page.exists(SHOW_MORE):
        await page.click(SHOW_MORE)
        await _sleep(0.4)
    # AI-generated content: default off; only touched when its state differs from what we want.
    n = await _set_toggle(page, "AI-generated content", ai_generated, required_when_on=True)
    if n:
        notes.append(n)
    # Comments: label "Comment" under "Allow users to" UNVERIFIED; TikTok's default is on.
    n = await _set_toggle(page, "Comment", allow_comments, required_when_on=False)
    if n:
        notes.append(n)
    return notes


async def _schedule(page: Page, target: datetime) -> None:
    # live: in a background tab the pickers' open animation never runs (all wheel items keep height 0)
    await page.bring_to_front()
    if not await page.eval(SCHEDULE_CHECKED):
        await page.click(SCHEDULE_RADIO, wait=STEP_TIMEOUT)
        await page.wait_el(_picker("date"), timeout=STEP_TIMEOUT)

    # date
    want_date = target.strftime("%Y-%m-%d")
    cur = await page.eval(_picker_text("date")) or ""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", cur):
        raise DriverError("schedule", f"date button not found (got {cur!r})")
    if cur != want_date:
        await page.click(_picker("date"))
        await _sleep(0.4)
        cy, cm = int(cur[:4]), int(cur[5:7])
        months = (target.year * 12 + target.month) - (cy * 12 + cm)
        if months < 0:
            raise DriverError("schedule", f"target month is before the calendar's ({cur})")
        for _ in range(months):
            await page.click(NEXT_MONTH, wait=STEP_TIMEOUT)
            await _sleep(0.3)
        await page.click(_day_cell(target.day), wait=STEP_TIMEOUT)
        await _sleep(0.4)
        cur = await page.eval(_picker_text("date")) or ""
        if cur != want_date:
            raise DriverError("schedule", f"date shows {cur!r}, want {want_date!r}")

    # time
    want_time = target.strftime("%H:%M")
    cur = await page.eval(_picker_text("time")) or ""
    if cur != want_time:
        await page.click(_picker("time"), wait=STEP_TIMEOUT)
        await _sleep(0.4)
        await _pick_wheel(page, "h", target.hour)
        await _pick_wheel(page, "m", target.minute)
        await page.press("Escape")  # close the wheel; UNVERIFIED that Escape closes it (selection is already applied)
        await _sleep(0.3)
        cur = await page.eval(_picker_text("time")) or ""
        if cur != want_time:
            raise DriverError("schedule", f"time shows {cur!r}, want {want_time!r}")
    # the date can shift if the picker clamps an invalid time; check both again
    cur_d = await page.eval(_picker_text("date")) or ""
    if cur_d != want_date:
        raise DriverError("schedule", f"date changed to {cur_d!r} after setting the time, want {want_date!r}")


def _badge_text(target: datetime) -> str:
    """"Oct 31, 8:00 PM" as the content list prints it (the zone suffix follows)."""
    h12 = target.hour % 12 or 12
    return f"{target:%b} {target.day}, {h12}:{target:%M} {'AM' if target.hour < 12 else 'PM'}"


# ------------------------------------------------------------------ contract
async def _login_state(page: Page, timeout: float = 25) -> dict:
    """TikTok Studio is an SPA: readyState is 'complete' while the upload area is still a spinner, so poll."""
    try:
        return await page.wait_for(f"(i => (i && (i.upload || i.login)) ? i : null)({LOGIN_JS})", timeout=timeout,
                                   what="TikTok upload area or login prompt")
    except PageError:
        return await page.eval(LOGIN_JS) or {}


async def check_login(page: Page) -> dict:
    info = await _login_state(page, timeout=5)
    if not isinstance(info, dict) or not (info.get("upload") or info.get("login")):
        await page.goto(START_URL)
        info = await _login_state(page)
    if not isinstance(info, dict):
        info = {}
    if info.get("login") or not info.get("upload"):
        return {"logged_in": False, "account": "", "detail": f"no upload area (login prompt?) at {info.get('url', '')}"}
    account = str(info.get("name") or "")
    detail = "TikTok Studio upload page open" + ("" if account else " (account name not found; UNVERIFIED selector)")
    return {"logged_in": True, "account": account, "detail": detail}


async def publish(page: Page, job: PostJob, *, dry_run: bool = False) -> PostResult:
    opts = job.options or {}
    visibility = str(opts.get("visibility") or "everyone").strip().lower()
    ai_generated = bool(opts.get("ai_generated", False))
    allow_comments = bool(opts.get("allow_comments", True))
    caption = job.caption or ""
    notes: list[str] = []

    if visibility not in VIS_LABELS:
        raise DriverError("validate", f"visibility must be one of {sorted(VIS_LABELS)}, got {visibility!r}")
    if len(caption) > MAX_CAPTION:
        raise DriverError("validate", f"caption is {len(caption)} chars; TikTok allows {MAX_CAPTION}")

    async with _Step(page, "open"):
        info = await _login_state(page, timeout=5)
        if not isinstance(info, dict) or not info.get("upload"):
            await page.goto(START_URL)
            info = await _login_state(page)
        if isinstance(info, dict) and info.get("login"):
            raise DriverError("login", "TikTok shows a login prompt; log in in Chrome first")
        await page.wait_el(FILE_INPUT, visible=False, timeout=STEP_TIMEOUT)

    target: datetime | None = None
    if job.schedule_at is not None:
        async with _Step(page, "schedule"):
            off, zone = await _browser_offset(page, job.schedule_at)
            target = schedule_target(job.schedule_at, utc_offset_min=off)
            if target != job.schedule_at:
                notes.append(f"time rounded up to {target:%H:%M} (5-minute steps)")
            notes.append(f"picker zone: {zone or 'machine local'} UTC{target:%z}")

    async with _Step(page, "upload"):
        await page.set_files(FILE_INPUT, [job.video])
        state = await page.wait_for(UPLOAD_JS, timeout=UPLOAD_TIMEOUT, what="'Uploaded'")
        if state == "error":
            raise DriverError("upload", "TikTok reported an upload error")

    async with _Step(page, "caption"):
        await _fill_caption(page, caption)

    async with _Step(page, "visibility"):
        n = await _set_visibility(page, visibility)
        if n:
            notes.append(n)

    async with _Step(page, "options"):
        notes += await _set_options(page, ai_generated, allow_comments)

    if target is not None:
        async with _Step(page, "schedule"):
            await _schedule(page, target)

    submit = _button("Schedule" if target is not None else "Post")
    async with _Step(page, "submit"):
        await page.wait_for(_enabled(submit), timeout=SUBMIT_READY_TIMEOUT, what="enabled Post/Schedule button")
        if target is not None and target - _now() < MIN_LEAD:
            raise DriverError("schedule", f"{target.isoformat()} is now less than 15 minutes ahead (the upload took too long)")

    scheduled_for = target.isoformat() if target is not None else ""
    if dry_run:
        return PostResult(platform="tiktok", ok=True, status="dry_run", url=START_URL, scheduled_for=scheduled_for,
                          message="; ".join(["filled, not submitted", *notes]), screenshot=await _shot(page, "dry_run"))

    async with _Step(page, "submit"):
        await page.click(submit)
        end = time.monotonic() + FINISH_TIMEOUT
        while True:
            href = await page.eval("location.href/*tt:href*/") or ""
            if CONTENT_URL_PART in str(href):
                break
            if await page.exists(POST_NOW_MODAL):
                await page.click(POST_NOW_MODAL)
            if time.monotonic() > end:
                raise DriverError("submit", f"did not reach {CONTENT_URL_PART} (still at {href})")
            await _sleep(1.0)

    # The post exists from here on: never raise (a retry would post twice); report what we could read.
    prefix = _norm(caption)[:20]
    row = ""
    try:
        if prefix:
            row = await page.wait_for(_row_js(prefix), timeout=60, what="new content row") or ""
    except Exception:  # noqa: BLE001
        notes.append("new row not found in the content list (UNVERIFIED row selector)")
    row_n = _norm(str(row))
    url = str(await page.eval("location.href/*tt:href*/") or "")
    if target is not None:
        m = re.search(r"[A-Z][a-z]{2} \d{1,2}, \d{1,2}:\d{2} [AP]M(?: \(GMT[+-]?\d*(?::\d{2})?\) ?[\w/ ]*)?", row_n)
        badge = m.group(0).strip() if m else ""
        if badge and not badge.startswith(_badge_text(target)):
            notes.append(f"WARNING: list shows {badge!r}, expected {_badge_text(target)!r}")
        status_txt = badge or "scheduled"
        return PostResult(platform="tiktok", ok=True, status="scheduled", url=url, scheduled_for=scheduled_for,
                          message="; ".join([status_txt, *notes]), screenshot=await _shot(page, "done"))
    m = re.search(r"Content under review|Under review|Processing|Posted|Published", row_n, re.I)
    status_txt = m.group(0) if m else (row_n[:120] or "submitted")
    return PostResult(platform="tiktok", ok=True, status="published", url=url,
                      message="; ".join([status_txt, *notes]), screenshot=await _shot(page, "done"))
