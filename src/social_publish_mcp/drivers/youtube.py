"""YouTube Studio driver: upload a video, fill the details, then publish now or schedule.

Selectors come from docs/platform_notes.md (verified live, Oct 2026). The ones marked ``GUESS`` below were not
verified and are the first thing to check when the flow breaks.

Assumptions:
- Studio UI in English (button labels, month names, "Video published", AM/PM times).
- Schedule time zone: the offset shown in the schedule section ("GMT+2", "(GMT+0200) Cairo") is used when it can be
  read; otherwise ``job.schedule_at`` is typed in its own time zone, so pass it in the account's zone. A fixed
  offset read today ignores a DST change before the scheduled date.
- A scheduled video always goes public at that time (Studio's own behaviour), so schedule_at + a non-public
  visibility is rejected.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any

from ..models import DriverError, PostJob, PostResult
from ..page import Page

START_URL = "https://studio.youtube.com/"
STUDIO = "https://studio.youtube.com"

TITLE_MAX = 100
DESC_MAX = 5000
TAGS_MAX = 500
VISIBILITIES = {"public": "PUBLIC", "unlisted": "UNLISTED", "private": "PRIVATE"}
UPLOAD_TIMEOUT = 3600.0  # seconds to wait for the file upload to finish before the final click

# --------------------------------------------------------------------------- selectors (verified unless GUESS)
DLG = "ytcp-uploads-dialog"
FILE_INPUT = "ytcp-uploads-file-picker input[type=file]"
TEXTBOX = f"{DLG} #textbox"  # [0] title, [1] description
KIDS_RADIO = 'tp-yt-paper-radio-button[name="VIDEO_MADE_FOR_KIDS_{}"]'  # MFK / NOT_MFK
SHOW_MORE = f"{DLG} #toggle-button"
ALTERED_RADIO = 'tp-yt-paper-radio-button[name="VIDEO_HAS_ALTERED_CONTENT_{}"]'  # YES / NO
TAGS_CONTAINER = f"{DLG} #tags-container"
TAGS_INPUT = f"{TAGS_CONTAINER} input#text-input"
TAG_CHIPS = "ytcp-chip, ytcp-free-text-chip"  # GUESS: chip elements inside #tags-container
PLAYLISTS = "ytcp-video-metadata-playlists"
PLAYLIST_TRIGGER = f"{PLAYLISTS} ytcp-dropdown-trigger"
PLAYLIST_ROWS = "ytcp-playlist-dialog li, tp-yt-paper-dialog li"  # GUESS: rows of the playlist checkbox list
NEXT = f"{DLG} #next-button"
VIS_RADIO = 'tp-yt-paper-radio-button[name="{}"]'  # PUBLIC / UNLISTED / PRIVATE
DONE = f"{DLG} #done-button"
SCHEDULE_EXPAND = f"{DLG} #second-container-expand-button"
SCHEDULE_BOX = f"{DLG} #second-container"  # GUESS: holds the date/time pickers and the time-zone label
DATE_TRIGGER = "#datepicker-trigger"
CALENDAR_ROOTS = "ytcp-date-picker, ytcp-scrollable-calendar, tp-yt-iron-dropdown"  # GUESS: open calendar list
NEUTRAL = f"{DLG} #dialog-title"  # GUESS: harmless spot to click to close popups

_ROWS = json.dumps(PLAYLIST_ROWS)


# --------------------------------------------------------------------------- pure helpers (unit-tested)
def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    if space >= limit * 0.6:  # prefer a word boundary when it does not cost too much
        cut = cut[:space]
    return cut.rstrip()


def make_title(title: str, caption: str) -> str:
    """job.title, else the first non-empty caption line; one line, no < >, max 100 chars."""
    t = (title or "").strip() or next((ln for ln in (caption or "").splitlines() if ln.strip()), "")
    return _truncate(_norm(re.sub(r"[<>]", "", t)), TITLE_MAX)


def make_description(caption: str) -> str:
    """YouTube rejects < and > in descriptions; max 5000 chars."""
    return _truncate(re.sub(r"[<>]", "", caption or "").strip(), DESC_MAX)


def clean_tag(tag: str) -> str:
    return _norm(re.sub(r"[,<>]", " ", tag or "")).lstrip("#").strip()


def tag_cost(tag: str) -> int:
    """Studio counts a tag with spaces as if quoted."""
    return len(tag) + (2 if " " in tag else 0)


def plan_tags(existing: list[str], wanted: list[str], limit: int = TAGS_MAX) -> list[str]:
    """Tags to add: cleaned, not already present (case-insensitive), within the 500-char budget (commas count)."""
    have = [t for t in (_norm(x) for x in existing) if t]
    seen = {t.lower() for t in have}
    used = sum(tag_cost(t) for t in have) + max(len(have) - 1, 0)
    out: list[str] = []
    for raw in wanted:
        t = clean_tag(raw)
        if not t or t.lower() in seen:
            continue
        add = tag_cost(t) + (1 if have or out else 0)
        if used + add > limit:
            continue  # a shorter tag later may still fit
        out.append(t)
        seen.add(t.lower())
        used += add
    return out


_GMT = re.compile(r"\b(?:GMT|UTC)\s*([+\-−–])\s*(\d{1,2})(?::?(\d{2}))?")
_GMT_ZERO = re.compile(r"\((?:GMT|UTC)\)|\b(?:GMT|UTC)\s+[A-Z][a-z]")


def parse_gmt_offset(text: str) -> tzinfo | None:
    """'(GMT+0200) Cairo', 'GMT+2', 'GMT-5:30', '(GMT) London' -> fixed-offset tz; None when absent."""
    m = _GMT.search(text or "")
    if m:
        minutes = int(m.group(2)) * 60 + int(m.group(3) or 0)
        if minutes <= 14 * 60:
            return timezone(timedelta(minutes=-minutes if m.group(1) != "+" else minutes))
    if _GMT_ZERO.search(text or ""):
        return timezone.utc
    return None


def time_text(dt: datetime) -> str:
    """'5:00 PM' — what Studio's time input shows and accepts."""
    h = dt.hour % 12 or 12
    return f"{h}:{dt.minute:02d} {'AM' if dt.hour < 12 else 'PM'}"


def date_matches(shown: str, dt: datetime) -> bool:
    """True when the picker text names dt's day, month and year ('Oct 31, 2026', '31 Oct 2026', ...)."""
    s = shown.lower()
    return (str(dt.year) in s and dt.strftime("%b").lower() in s
            and re.search(rf"(?<!\d){dt.day}(?!\d)", s) is not None)


def parse_confirmed(text: str, tz: tzinfo) -> str:
    """'... set to public on Oct 31, 2026 at 5:00 PM' -> ISO 8601 in tz ('' when not found)."""
    m = re.search(r"\bon\s+(.+?)\s+at\s+(\d{1,2}:\d{2}\s*[AP]M)", text or "", re.I)
    if not m:
        return ""
    when = f"{_norm(m.group(1))} {m.group(2).upper().replace(' ', '')}"
    for fmt in ("%b %d, %Y %I:%M%p", "%B %d, %Y %I:%M%p", "%d %b %Y %I:%M%p", "%d %B %Y %I:%M%p"):
        try:
            return datetime.strptime(when, fmt).replace(tzinfo=tz).isoformat()
        except ValueError:
            continue
    return ""


_VID = re.compile(r"https?://(?:www\.)?(?:youtube\.com/(?:shorts/|watch\?v=)|youtu\.be/)([\w-]{11})")


def extract_video(links: list[str]) -> tuple[str, str]:
    """First YouTube video link -> (url, video_id)."""
    for link in links:
        m = _VID.search(link or "")
        if m:
            return m.group(0), m.group(1)
    return "", ""


def read_options(job: PostJob) -> dict[str, Any]:
    o = job.options or {}
    cid = str(o.get("channel_id") or "").strip()
    if not re.fullmatch(r"UC[\w-]{22}", cid):
        raise DriverError("options", f"options.channel_id must be a channel id like UC…, got {cid!r}")
    vis = str(o.get("visibility") or "public").lower()
    if vis not in VISIBILITIES:
        raise DriverError("options", f"visibility must be one of {sorted(VISIBILITIES)}, got {vis!r}")
    if job.schedule_at is not None and vis != "public":
        raise DriverError("options", "a scheduled video always becomes public; drop visibility or schedule_at")
    pls = o.get("playlists") or []
    if isinstance(pls, str):
        pls = [pls]
    return {"channel_id": cid, "visibility": vis, "playlists": [p.strip() for p in pls if str(p).strip()],
            "made_for_kids": bool(o.get("made_for_kids", False)),
            "altered_content": bool(o.get("altered_content", False))}


# --------------------------------------------------------------------------- JS snippets (markers yt:* for tests)
def _checked(sel_expr: str) -> str:
    return (f"/*yt:checked*/ (e => !!e && (e.getAttribute('aria-checked') === 'true' || e.hasAttribute('checked')))"
            f"({sel_expr})")


def _q(css: str) -> str:
    return f"document.querySelector({json.dumps(css)})"


JS_LOGIN = r"""/*yt:login*/ (() => {
  if (/accounts\.google\.com/.test(location.host)) return {state: 'signed_out', href: location.href};
  const txt = document.body ? document.body.innerText : '';
  if (/don.t have permission/i.test(txt)) return {state: 'denied', href: location.href};
  let cid = (location.href.match(/\/channel\/(UC[\w-]{22})/) || [])[1] || '';
  try { cid = cid || (window.ytcfg && ytcfg.get && ytcfg.get('CHANNEL_ID')) || ''; } catch (e) {}
  if (!cid) return null;
  const n = document.querySelector('ytcp-navigation-drawer #entity-name, #entity-name');
  return {state: 'ok', href: location.href, channel_id: cid, name: n ? n.innerText.trim() : ''};
})()"""

JS_DIALOG_STATE = f"""/*yt:dialog*/ (() => {{
  if (/accounts\\.google\\.com/.test(location.host)) return 'signed_out';
  if (/don.t have permission/i.test(document.body ? document.body.innerText : '')) return 'denied';
  return document.querySelector({json.dumps(FILE_INPUT)}) ? 'ok' : null;
}})()"""

JS_FORM_READY = f"""/*yt:form*/ (() => {{
  const b = document.querySelectorAll({json.dumps(TEXTBOX)})[0];
  return !!b && __sp.visible(b) && b.innerText.trim().length > 0;
}})()"""

JS_FILL = f"""/*yt:fill*/ ((i, text) => {{
  const box = document.querySelectorAll({json.dumps(TEXTBOX)})[i];
  if (!box) return null;
  box.focus();
  document.execCommand('selectAll', false, null);
  document.execCommand('insertText', false, text);
  box.dispatchEvent(new Event('input', {{bubbles: true}}));
  return box.innerText;
}})(%d, %s)"""

JS_TAGS = f"""/*yt:tags*/ (() => {{
  const c = document.querySelector({json.dumps(TAGS_CONTAINER)});
  if (!c) return null;
  const chips = [...c.querySelectorAll({json.dumps(TAG_CHIPS)})].map(e => (e.innerText || '').trim()).filter(Boolean);
  return {{chips, text: c.innerText || ''}};
}})()"""

JS_PLAYLIST_NAMES = f"""/*yt:pl-names*/ (() => {{
  const rows = __sp.all({_ROWS});
  return rows.length ? rows.map(r => (r.innerText || '').split('\\n')[0].trim()) : null;
}})()"""

JS_PLAYLIST_TEXT = f"/*yt:pl-text*/ (e => e ? e.innerText : '')({_q(PLAYLISTS)})"

JS_PLAYLIST_DONE = """(() => { /*yt:pl-done*/
  return __sp.byText('ytcp-playlist-dialog ytcp-button, ytcp-playlist-dialog button', 'Done', true)
      || __sp.byText('ytcp-button, button', 'Done', true);
})()"""

JS_SCHEDULE_TEXT = f"/*yt:tz*/ (e => e ? e.innerText : '')({_q(SCHEDULE_BOX)} || {_q(DLG)})"

JS_DATE_TEXT = f"""/*yt:date-text*/ (e => !e ? '' :
  e.innerText + ' ' + [...e.querySelectorAll('input')].map(i => i.value).join(' '))({_q(DATE_TRIGGER)})"""

_TIME_INPUT = "[...document.querySelectorAll('ytcp-datetime-picker input')].find(i => /AM|PM/i.test(i.value))"
SEL_TIME_INPUT = f"(() => {{ /*yt:time-input*/ return {_TIME_INPUT} || null; }})()"

JS_TIME_FILL = f"""/*yt:time-fill*/ (t => {{
  const i = {_TIME_INPUT};
  if (!i) return null;
  i.focus(); i.select();
  document.execCommand('insertText', false, t);
  i.dispatchEvent(new Event('change', {{bubbles: true}}));
  i.blur();
  return i.value;
}})(%s)"""

JS_TIME_VALUE = f"/*yt:time-value*/ (i => i ? i.value : '')({_TIME_INPUT})"

JS_UPLOAD = f"""/*yt:upload*/ (() => {{
  const d = document.querySelector({json.dumps(DLG)});
  if (!d) return 'gone';
  const t = d.innerText || '';
  const bad = t.match(/[^\\n]*(upload failed|limit reached|couldn.t upload|upload canceled|upload cancelled)[^\\n]*/i);
  if (bad) return 'error: ' + bad[0].trim();
  if (/Uploading\\s*\\d+\\s*%/i.test(t)) return '';
  return /upload complete|checks complete|processing/i.test(t) ? 'done' : '';
}})()"""

JS_PROGRESS = f"""/*yt:progress*/ (d => {{
  const m = d ? (d.innerText || '').match(/(Uploading[^\\n]*|Upload complete[^\\n]*|Checks[^\\n]*)/i) : null;
  return m ? m[0] : '';
}})({_q(DLG)})"""

JS_DIALOG_LINKS = f"""/*yt:links*/ (d => d ? [...d.querySelectorAll('a[href]')].map(a => a.href)
  .concat((d.innerText || '').match(/https?:\\/\\/\\S+/g) || []) : [])({_q(DLG)})"""

JS_RESULT = """/*yt:result*/ (() => {
  for (const d of __sp.all('tp-yt-paper-dialog, ytcp-dialog, ytcp-video-share-dialog')) {
    const t = d.innerText || '';
    const m = t.match(/Video (published|scheduled|saved)/i);
    if (!m || d.closest('ytcp-uploads-dialog')) continue;
    const links = [...d.querySelectorAll('a[href]')].map(a => a.href).concat(t.match(/https?:\\/\\/\\S+/g) || []);
    return {heading: m[0], text: t, links};
  }
  return null;
})()"""


def _day_cell(dt: datetime) -> str:
    """Selector expression: the enabled day cell of dt's month in the open calendar (GUESS: structure)."""
    return f"""((y, mon, d) => {{ /*yt:day*/
  const want = new RegExp('^' + mon + '[a-z]*\\\\.?\\\\s+' + y + '$', 'i');
  const leaves = root => [...root.querySelectorAll('*')].filter(e => !e.children.length);
  for (const root of __sp.all({json.dumps(CALENDAR_ROOTS)})) {{
    for (const h of leaves(root)) {{
      if (!want.test((h.textContent || '').trim())) continue;
      for (let box = h.parentElement; box && box !== root.parentElement; box = box.parentElement) {{
        const cells = leaves(box).filter(e => e !== h && (e.textContent || '').trim() === String(d));
        if (!cells.length) continue;
        const ok = cells.filter(e => e.getAttribute('aria-disabled') !== 'true' && !/disabled/i.test(String(e.className)));
        return ok[0] || null;
      }}
    }}
  }}
  return null;
}})({dt.year}, {json.dumps(dt.strftime('%b'))}, {dt.day})"""


def _playlist_box(name: str) -> str:
    """Selector expression: the checkbox of the playlist row whose first line is (or starts with) name."""
    return f"""((name) => {{ /*yt:pl-box*/
  const rows = __sp.all({_ROWS});
  const first = r => (r.innerText || '').split('\\n')[0].replace(/\\s+/g, ' ').trim();
  const row = rows.find(r => first(r) === name) || rows.find(r => first(r).startsWith(name));
  return row ? (row.querySelector('tp-yt-paper-checkbox, ytcp-checkbox-lit, [role=checkbox]') || row) : null;
}})({json.dumps(name)})"""


# --------------------------------------------------------------------------- plumbing
_sleep = asyncio.sleep


async def _shot(page: Page, step: str) -> str:
    try:
        return await page.screenshot(f"youtube_{step}_{int(time.time())}")
    except Exception:  # noqa: BLE001 - a failed screenshot must not hide the real error
        return ""


@asynccontextmanager
async def _step(page: Page, name: str) -> AsyncIterator[None]:
    """Run a block as a named step: any failure becomes DriverError(name, detail, screenshot)."""
    try:
        yield
    except DriverError as e:
        if not e.screenshot:
            e.screenshot = await _shot(page, e.step)
        raise
    except Exception as e:  # noqa: BLE001
        raise DriverError(name, f"{type(e).__name__}: {e}", await _shot(page, name)) from e


async def _poll(page: Page, js: str, ok, *, tries: int = 10, delay: float = 0.5) -> Any:
    """Evaluate js until ok(value); return the last value either way."""
    v = None
    for _ in range(tries):
        v = await page.eval(js)
        if ok(v):
            break
        await _sleep(delay)
    return v


async def _click_neutral(page: Page) -> None:
    """Close hashtag suggestions / dropdown lists by clicking a harmless spot (best effort)."""
    try:
        if await page.exists(NEUTRAL):
            await page.click(NEUTRAL, wait=2)
    except Exception:  # noqa: BLE001
        pass


async def _check_radio(page: Page, sel: str) -> None:
    await page.click(sel)
    await page.wait_for(_checked(_q(sel)), timeout=5, what=f"{sel} checked")


# --------------------------------------------------------------------------- login
async def check_login(page: Page) -> dict:
    """Open Studio (read-only) and report the signed-in channel."""
    try:
        await page.goto(START_URL)
        info = await page.wait_for(JS_LOGIN, timeout=30, what="Studio channel redirect")
    except Exception as e:  # noqa: BLE001
        return {"logged_in": False, "account": "", "detail": f"could not read Studio: {e}"}
    state = info.get("state") if isinstance(info, dict) else None
    if state == "signed_out":
        return {"logged_in": False, "account": "", "detail": "redirected to Google sign-in"}
    if state == "denied":
        return {"logged_in": False, "account": "", "detail": "Studio says no permission (wrong channel/account)"}
    if state != "ok":
        return {"logged_in": False, "account": "", "detail": f"unexpected Studio state: {info!r}"}
    cid, name = info.get("channel_id", ""), info.get("name", "")
    return {"logged_in": True, "account": f"{name} ({cid})" if name else cid,
            "detail": f"channel_id={cid}; {info.get('href', '')}"}


# --------------------------------------------------------------------------- publish steps
async def open_upload_dialog(page: Page, channel_id: str) -> None:
    async with _step(page, "open_upload_dialog"):
        await page.goto(f"{STUDIO}/channel/{channel_id}/content")
        await page.goto(f"{STUDIO}/channel/{channel_id}/videos/upload?d=ud")
        state = await page.wait_for(JS_DIALOG_STATE, timeout=45, what="upload dialog")
        if state == "denied":
            raise DriverError("open_upload_dialog", f"no permission for channel {channel_id}: the active account/channel "
                              "is a different one; switch channel in Studio")
        if state == "signed_out":
            raise DriverError("open_upload_dialog", "not signed in to YouTube Studio")


async def attach_video(page: Page, job: PostJob) -> None:
    async with _step(page, "attach_video"):
        await page.set_files(FILE_INPUT, [job.video])


async def wait_details_form(page: Page) -> None:
    async with _step(page, "wait_details_form"):
        # the title box is prefilled with the file name; wait for it so Studio does not overwrite ours later
        await page.wait_for(JS_FORM_READY, timeout=180, what="details form")


async def _fill_box(page: Page, index: int, text: str, step: str) -> None:
    await page.click(f"document.querySelectorAll({json.dumps(TEXTBOX)})[{index}]")
    got = await page.eval(JS_FILL % (index, json.dumps(text)))
    if _norm(got if isinstance(got, str) else "") != _norm(text):
        raise DriverError(step, f"textbox {index} reads {got!r} after typing")


async def fill_title(page: Page, title: str) -> None:
    async with _step(page, "fill_title"):
        await _fill_box(page, 0, title, "fill_title")


async def fill_description(page: Page, description: str) -> None:
    async with _step(page, "fill_description"):
        await _fill_box(page, 1, description, "fill_description")


async def set_made_for_kids(page: Page, kids: bool) -> None:
    async with _step(page, "made_for_kids"):
        await _check_radio(page, KIDS_RADIO.format("MFK" if kids else "NOT_MFK"))


async def set_altered_content(page: Page, altered: bool) -> None:
    async with _step(page, "altered_content"):
        radio = ALTERED_RADIO.format("YES" if altered else "NO")
        if not await page.eval(f"/*yt:visible*/ __sp.visible({_q(radio)})"):
            await page.click(SHOW_MORE)
        await _check_radio(page, radio)


async def _read_tags(page: Page) -> dict:
    v = await page.eval(JS_TAGS)
    if not isinstance(v, dict):
        raise DriverError("tags", "tags field not found (Show more not expanded?)")
    return {"chips": [str(c) for c in v.get("chips") or []], "text": str(v.get("text") or "")}


async def add_tags(page: Page, tags: list[str]) -> list[str]:
    async with _step(page, "tags"):
        before = await _read_tags(page)
        new = plan_tags(before["chips"], tags)
        if not new:
            return []
        await page.click(TAGS_INPUT)
        await page.insert_text(",".join(new) + ",")
        after = await _read_tags(page)
        have = {c.lower() for c in after["chips"]}
        missing = [t for t in new if t.lower() not in have and t.lower() not in after["text"].lower()]
        if missing:
            raise DriverError("tags", f"tags not committed: {missing}")
        return new


async def select_playlists(page: Page, names: list[str]) -> None:
    async with _step(page, "playlists"):
        await _click_neutral(page)  # a hashtag popup from the title can cover the list
        await page.click(PLAYLIST_TRIGGER)
        available = await page.wait_for(JS_PLAYLIST_NAMES, timeout=15, what="playlist list")
        for name in names:
            box = _playlist_box(name)
            if not await page.exists(box):
                raise DriverError("playlists", f"playlist {name!r} not found; available: {available}")
            if not await page.eval(_checked(box)):
                await page.click(box)  # trusted click: JS .click() toggles twice
                await page.wait_for(_checked(box), timeout=5, what=f"playlist {name!r} checked")
        await page.click(JS_PLAYLIST_DONE)
        shown = await _poll(page, JS_PLAYLIST_TEXT, lambda v: isinstance(v, str) and all(n in v for n in names))
        if not (isinstance(shown, str) and all(n in shown for n in names)):
            raise DriverError("playlists", f"selection not saved; field shows {shown!r}")


async def go_to_visibility(page: Page) -> None:
    async with _step(page, "next_steps"):
        for _ in range(3):
            await page.click(NEXT)
            await _sleep(0.8)
        await page.wait_el(VIS_RADIO.format("PUBLIC"), timeout=15)


async def set_visibility(page: Page, visibility: str) -> None:
    async with _step(page, "visibility"):
        await _check_radio(page, VIS_RADIO.format(VISIBILITIES[visibility]))


async def detect_display_tz(page: Page, fallback: tzinfo) -> tuple[tzinfo, str]:
    """Time zone Studio shows in the schedule section; fallback (schedule_at's zone) when not readable."""
    text = await page.eval(JS_SCHEDULE_TEXT)
    tz = parse_gmt_offset(text if isinstance(text, str) else "")
    return (tz, "studio") if tz else (fallback, "schedule_at")


async def pick_date(page: Page, local: datetime) -> None:
    await page.click(DATE_TRIGGER)
    await page.click(_day_cell(local))  # trusted click; typing into the date input mixes date and time
    shown = await _poll(page, JS_DATE_TEXT, lambda v: isinstance(v, str) and date_matches(v, local))
    if not (isinstance(shown, str) and date_matches(shown, local)):
        raise DriverError("schedule_date", f"date picker shows {shown!r}, wanted {local:%b %d, %Y}")


async def type_time(page: Page, local: datetime) -> None:
    want = time_text(local)
    await page.click(SEL_TIME_INPUT)
    await page.eval(JS_TIME_FILL % json.dumps(want))
    await _click_neutral(page)  # close the time list
    shown = await _poll(page, JS_TIME_VALUE, lambda v: isinstance(v, str) and _norm(v).upper() == want)
    if not (isinstance(shown, str) and _norm(shown).upper() == want):
        raise DriverError("schedule_time", f"time input shows {shown!r}, wanted {want!r}")


async def set_schedule(page: Page, when: datetime) -> tuple[datetime, str]:
    async with _step(page, "schedule"):
        if when <= datetime.now(timezone.utc):
            raise DriverError("schedule", f"schedule_at {when.isoformat()} is in the past")
        await page.click(SCHEDULE_EXPAND)
        tz, source = await detect_display_tz(page, when.tzinfo or timezone.utc)
        local = when.astimezone(tz)
        await pick_date(page, local)
        await type_time(page, local)
        # re-check the date: typing the time must not have moved it
        shown = await page.eval(JS_DATE_TEXT)
        if not (isinstance(shown, str) and date_matches(shown, local)):
            raise DriverError("schedule", f"date changed to {shown!r} after setting the time")
        return local, source


async def wait_upload_finished(page: Page) -> None:
    async with _step(page, "upload"):
        state = await page.wait_for(JS_UPLOAD, timeout=UPLOAD_TIMEOUT, what="upload to finish")
        if isinstance(state, str) and state.startswith("error"):
            raise DriverError("upload", state)


async def click_final(page: Page, scheduled: bool) -> None:
    async with _step(page, "final_click"):
        label = _norm(await page.text(DONE))
        if scheduled != ("schedule" in label.lower()):
            raise DriverError("final_click", f"button says {label!r}; expected {'Schedule' if scheduled else 'Publish/Save'}")
        await page.click(DONE)


async def read_result(page: Page, fallback_links: list[str]) -> dict:
    async with _step(page, "result"):
        res = await page.wait_for(JS_RESULT, timeout=90, what="'Video published/scheduled' dialog")
        if not isinstance(res, dict):
            raise DriverError("result", f"unexpected result dialog: {res!r}")
        url, vid = extract_video([str(x) for x in res.get("links") or []] + fallback_links)
        return {"heading": str(res.get("heading") or ""), "text": str(res.get("text") or ""), "url": url, "id": vid}


# --------------------------------------------------------------------------- entry point
async def publish(page: Page, job: PostJob, *, dry_run: bool = False) -> PostResult:
    opts = read_options(job)
    title = make_title(job.title, job.caption)
    if not title:
        raise DriverError("options", "no title: set job.title or a caption")
    description = make_description(job.caption)

    await open_upload_dialog(page, opts["channel_id"])
    await attach_video(page, job)
    await wait_details_form(page)
    await fill_title(page, title)
    if description:
        await fill_description(page, description)
    await set_made_for_kids(page, opts["made_for_kids"])
    await set_altered_content(page, opts["altered_content"])
    added = await add_tags(page, job.tags) if job.tags else []
    if opts["playlists"]:
        await select_playlists(page, opts["playlists"])
    link_now = await page.eval(JS_DIALOG_LINKS)  # the "Video link" in the dialog, kept as a fallback
    links = [str(x) for x in link_now] if isinstance(link_now, list) else []
    await go_to_visibility(page)

    local, tz_source = None, ""
    if job.schedule_at is not None:
        local, tz_source = await set_schedule(page, job.schedule_at)
    else:
        await set_visibility(page, opts["visibility"])
    when_note = f"; scheduled {local.isoformat()} (time zone from {tz_source})" if local else ""

    if dry_run:
        progress = await page.eval(JS_PROGRESS)
        url, vid = extract_video(links)
        return PostResult(platform="youtube", ok=True, status="dry_run", url=url, post_id=vid,
                          scheduled_for=local.isoformat() if local else "",
                          message=f"filled, stopped before the final click; title={title!r}; tags added={added}"
                                  f"{when_note}; upload: {progress or '?'}",
                          screenshot=await _shot(page, "dry_run"))

    await wait_upload_finished(page)
    await click_final(page, scheduled=local is not None)
    res = await read_result(page, links)
    scheduled_for = ""
    if local is not None:
        scheduled_for = parse_confirmed(res["text"], local.tzinfo) or local.isoformat()
    confirm = next((ln.strip() for ln in res["text"].splitlines() if "set to public" in ln.lower()), "")
    return PostResult(platform="youtube", ok=True, status="scheduled" if local is not None else "published",
                      url=res["url"], post_id=res["id"], scheduled_for=scheduled_for,
                      message=" — ".join(x for x in (res["heading"], confirm) if x) + when_note,
                      screenshot=await _shot(page, "done"))
