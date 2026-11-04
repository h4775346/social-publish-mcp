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
from pathlib import Path
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
# The playlist picker popup. Live 2026-11-01: it shows "Create playlist" + "Done" at the bottom and a checkbox list
# (or "No playlists available" while it loads). Element names GUESS; found by tag first, else by climbing from the
# "Create playlist" button to the nearest ancestor holding a "Done" button. Never the upload dialog's own <li>s
# (those are the altered-content examples — the 2026-11-01 "playlist not found" bug).
PLAYLIST_POPUPS = "ytcp-playlist-dialog, ytcp-playlist-dialog-v2"
PLAYLIST_BOXES = "[role=checkbox], tp-yt-paper-checkbox, ytcp-checkbox-lit, input[type=checkbox]"
NEXT = f"{DLG} #next-button"
VIS_RADIO = 'tp-yt-paper-radio-button[name="{}"]'  # PUBLIC / UNLISTED / PRIVATE
DONE = f"{DLG} #done-button"
SCHEDULE_EXPAND = f"{DLG} #second-container-expand-button"
SCHEDULE_BOX = f"{DLG} #second-container"  # GUESS: holds the date/time pickers and the time-zone label
DATE_TRIGGER = "#datepicker-trigger"
CALENDAR_ROOTS = "ytcp-date-picker, ytcp-scrollable-calendar, tp-yt-iron-dropdown"  # GUESS: open calendar list
NEUTRAL = f"{DLG} #dialog-title"  # GUESS: harmless spot to click to close popups
# plain (non-interactive) texts of the Visibility step: clicking one closes an open dropdown without side effects
NEUTRAL_TEXTS = ("Before you publish, check the following:", "Choose when to publish and who can see your video")

RESULT_POLLS = 90  # x RESULT_POLL_S seconds to wait for the "Video published/scheduled" dialog
RESULT_POLL_S = 1.0
RECLICK_EVERY = 12  # polls without any reaction before the final button is clicked again
FINAL_CLICKS = 3  # at most this many clicks on the final button (only while it is still visible and enabled)


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


_TAG_SEP = re.compile(r"[,،﹐，]")  # Latin, Arabic (U+060C), small and full-width commas


def clean_tag(tag: str) -> str:
    return _norm(re.sub(r"[<>]", " ", _TAG_SEP.sub(" ", tag or ""))).lstrip("#").strip()


def split_tags(tags: list[str] | str) -> list[str]:
    """One entry per tag: items (or a single string) are split on commas, incl. the Arabic comma '،'; cleaned;
    empties dropped. Studio commits a chip at each comma, so a comma inside a 'tag' really means two tags."""
    items = [tags] if isinstance(tags, str) else list(tags or [])
    out: list[str] = []
    for item in items:
        out += [t for t in (clean_tag(p) for p in _TAG_SEP.split(str(item or ""))) if t]
    return out


def tag_cost(tag: str) -> int:
    """Studio counts a tag with spaces as if quoted."""
    return len(tag) + (2 if " " in tag else 0)


def plan_tags(existing: list[str], wanted: list[str] | str, limit: int = TAGS_MAX) -> list[str]:
    """Tags to add: split/cleaned, not already present (case-insensitive), within the 500-char budget (commas count)."""
    have = [t for t in (_norm(x) for x in existing) if t]
    seen = {t.lower() for t in have}
    used = sum(tag_cost(t) for t in have) + max(len(have) - 1, 0)
    out: list[str] = []
    for t in split_tags(wanted):
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
    """First YouTube video link -> (url, video_id); a /shorts/ link wins over other forms of a link."""
    found = [m for m in (_VID.search(link or "") for link in links) if m]
    m = next((m for m in found if "/shorts/" in m.group(0)), found[0] if found else None)
    return (m.group(0), m.group(1)) if m else ("", "")


def tag_committed(tag: str, chips: list[str]) -> bool:
    """True when a chip reads exactly tag (case-insensitive, spaces normalised). A merged chip like
    'a,b,c' does not count for 'a'."""
    want = _norm(tag).casefold()
    return any(_norm(c).casefold() == want for c in chips)


class AmbiguousPlaylist(ValueError):
    pass


def match_playlist(name: str, available: list[str]) -> int | None:
    """Index of the playlist for name in available (picker row names): exact, else prefix, else contains;
    case-insensitive, spaces normalised. 'OPETO Shorts' -> 'OPETO Shorts · شورتس أغاني أوبيتو | Arabic Song Shorts'.
    None when nothing matches; AmbiguousPlaylist when several rows match at the best level (give a longer name)."""
    want = _norm(name).casefold()
    if not want:
        return None
    names = [_norm(a).casefold() for a in available]
    exact = [i for i, a in enumerate(names) if a == want]
    if exact:
        return exact[0]
    for test in (lambda a: a.startswith(want), lambda a: want in a):
        hits = [i for i, a in enumerate(names) if test(a)]
        if len(hits) == 1:
            return hits[0]
        if hits:
            raise AmbiguousPlaylist(f"playlist {name!r} matches several: {[available[i] for i in hits]}")
    return None


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

JS_TAG_INPUT_VALUE = f"/*yt:tag-input*/ (e => e ? e.value : '')({_q(TAGS_INPUT)})"

# Shared by the playlist snippets: finds the open picker popup and its checkbox rows (outermost checkbox per row,
# its label = first non-empty text line of the nearest ancestor that holds only that checkbox).
_PL_LIB = f"""
  const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
  const isDone = b => /^done$/i.test(norm(b.innerText || b.getAttribute('aria-label')));
  const doneIn = r => [...r.querySelectorAll('ytcp-button, button, [role=button]')]
      .find(b => __sp.visible(b) && isDone(b)) || null;
  const plRoot = () => {{
    for (const e of document.querySelectorAll({json.dumps(PLAYLIST_POPUPS)})) if (doneIn(e)) return e;
    const cp = __sp.byText('ytcp-button, button, [role=button]', 'Create playlist');
    for (let p = cp && cp.parentElement; p; p = p.parentElement) {{
      if (p.matches({json.dumps(DLG)})) break;
      if (doneIn(p)) return p;
    }}
    return null;
  }};
  const BOX = {json.dumps(PLAYLIST_BOXES)};
  const isChecked = b => b.getAttribute('aria-checked') === 'true' || b.hasAttribute('checked')
      || (b.matches('input') && b.checked) || !!b.querySelector('[aria-checked=true]');
  const plRows = () => {{
    const root = plRoot();
    if (!root) return null;
    const boxes = [...root.querySelectorAll(BOX)].filter(__sp.visible);
    const outer = boxes.filter(b => !boxes.some(o => o !== b && o.contains(b)));
    const LAB = '.label-text, #checkbox-label, .checkbox-label, #label, [class*=label-text], [class*=item-text]';
    const label = b => {{
      for (let p = b, i = 0; p && p !== root && i < 6; p = p.parentElement, i++) {{
        if (p !== b && p.querySelectorAll(BOX).length > [...b.querySelectorAll(BOX)].length + 1) break;
        const lab = [...p.querySelectorAll(LAB)].find(l => !l.querySelector(BOX) && norm(l.innerText));
        if (lab) return norm(lab.innerText);
        const t = norm(((p.innerText || '').split('\\n').find(x => x.trim())) || '');
        if (t) return t;
      }}
      return norm(b.getAttribute('aria-label'));
    }};
    return {{root, rows: outer.map(b => ({{el: b, name: label(b), checked: isChecked(b)}}))}};
  }};
"""

JS_PLAYLIST_ITEMS = f"""/*yt:pl-items*/ (() => {{ {_PL_LIB}
  const r = plRows();
  if (!r) return null;
  return {{names: r.rows.map(x => x.name), checked: r.rows.filter(x => x.checked).map(x => x.name),
           empty: /no playlists available/i.test(r.root.innerText || '')}};
}})()"""

JS_PLAYLIST_TEXT = f"/*yt:pl-text*/ (e => e ? e.innerText : '')({_q(PLAYLISTS)})"

JS_PLAYLIST_DONE = f"""(() => {{ /*yt:pl-done*/ {_PL_LIB}
  const root = plRoot();
  return (root && doneIn(root)) || __sp.byText('ytcp-playlist-dialog ytcp-button, ytcp-playlist-dialog button', 'Done', true);
}})()"""

# An open dropdown overlay (the schedule time list, a calendar, a select menu). Closed iron-dropdowns are display:none.
JS_POPUP_OPEN = r"""/*yt:popup*/ (() => {
  const open = [...document.querySelectorAll('tp-yt-iron-dropdown')].filter(e => {
    const r = e.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && getComputedStyle(e).display !== 'none' && e.getAttribute('aria-hidden') !== 'true'
      && e.opened !== false;
  });
  return open.length ? open.map(e => (e.innerText || '').trim().split('\n').slice(0, 2).join(' / ')) : null;
})()"""

JS_NEUTRAL_POINT = """/*yt:neutral*/ (texts => {
  const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
  const bad = 'button, a, input, textarea, select, label, [role=button], [role=option], [role=listbox], [role=checkbox],'
    + ' [role=radio], [role=tab], [role=switch], [contenteditable=true], tp-yt-iron-dropdown, tp-yt-paper-radio-button,'
    + ' ytcp-button, ytcp-dropdown-trigger, ytcp-icon-button, tp-yt-paper-icon-button, #second-container-expand-button';
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    const el = n.parentElement;
    if (!el || !texts.includes(norm(n.nodeValue)) || !__sp.visible(el) || el.closest(bad)) continue;
    const r = el.getBoundingClientRect();
    const x = r.left + Math.min(20, r.width / 4), y = r.top + r.height / 2;
    const hit = document.elementFromPoint(x, y);
    if (hit && (hit === el || el.contains(hit))) return [x, y];
  }
  return null;
})(%s)"""

JS_DONE_STATE = f"""/*yt:done-state*/ (e => !e ? null : {{
  label: (e.innerText || '').replace(/\\s+/g, ' ').trim(),
  visible: __sp.visible(e),
  enabled: !e.hasAttribute('disabled') && e.getAttribute('aria-disabled') !== 'true'
      && !e.querySelector('button[disabled], [aria-disabled=true]')
}})({_q(DONE)})"""

JS_DOM_DUMP = f"""/*yt:dom-dump*/ (() => {{
  const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
  const vis = __sp.visible;
  const short = (s, n) => norm(s).slice(0, n);
  const dialogs = [...document.querySelectorAll('ytcp-uploads-dialog, ytcp-video-share-dialog, ytcp-dialog,'
      + ' tp-yt-paper-dialog, [role=dialog], ytcp-playlist-dialog, tp-yt-iron-dropdown')]
    .filter(vis).map(d => ({{tag: d.tagName.toLowerCase(), id: d.id, text: short(d.innerText, 800)}}));
  const buttons = [...document.querySelectorAll('ytcp-button, button, [role=button]')].filter(vis)
    .map(b => ({{tag: b.tagName.toLowerCase(), id: b.id, text: short(b.innerText || b.getAttribute('aria-label'), 60),
               disabled: b.hasAttribute('disabled') || b.getAttribute('aria-disabled') === 'true'}}))
    .filter(b => b.text).slice(0, 80);
  const links = [...document.querySelectorAll('a[href*="youtu"]')].filter(vis).map(a => a.href).slice(0, 20);
  const done = document.querySelector({json.dumps(DONE)});
  return {{url: location.href, dialogs, buttons, links,
          done_button: done ? {{text: norm(done.innerText), visible: vis(done), disabled: done.hasAttribute('disabled'),
                               aria_disabled: done.getAttribute('aria-disabled')}} : null,
          upload_dialog_tail: norm((document.querySelector({json.dumps(DLG)}) || {{}}).innerText || '').slice(-400)}};
}})()"""

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

# The success dialog, found by its heading text ("Video scheduled" / "Video published" / "Video saved" / "Video
# processing" with the schedule or publish sentence, shown when YouTube accepted the post before SD is ready) wherever it is
# in the DOM (it may live inside ytcp-uploads-dialog), then its container's text and links.
JS_RESULT = """/*yt:result*/ (() => {
  // a scheduled video still processing gets the heading "Video processing" and "will be set to public on ..."
  const rx = /^Video (published|scheduled|saved|processing)$/i;
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    const head = (n.nodeValue || '').replace(/\\s+/g, ' ').trim();
    const el = n.parentElement;
    if (!rx.test(head) || !el || !__sp.visible(el)) continue;
    let box = el.closest('ytcp-video-share-dialog, ytcp-uploads-still-processing-dialog, ytcp-dialog,'
      + ' tp-yt-paper-dialog, [role=dialog]');
    if (!box) { box = el; for (let i = 0; i < 6 && box.parentElement; i++) box = box.parentElement; }
    const t = box.innerText || '';
    if (/processing/i.test(head) && !/will be (set to|published|scheduled)|before your video is/i.test(t)) continue;
    let links = [...box.querySelectorAll('a[href]')].map(a => a.href).concat(t.match(/https?:\\/\\/\\S+/g) || []);
    if (!links.some(l => /youtu\\.?be/.test(l)))
      links = links.concat([...document.querySelectorAll('a[href*="/shorts/"], a[href*="youtu.be/"], a[href*="watch?v="]')]
        .filter(__sp.visible).map(a => a.href));
    return {heading: head, text: t, links};
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


def _playlist_box(row_name: str) -> str:
    """Selector expression: the checkbox of the picker row named exactly row_name (a name read by JS_PLAYLIST_ITEMS)."""
    return f"""((name) => {{ /*yt:pl-box*/ {_PL_LIB}
  const r = plRows();
  const row = r && r.rows.find(x => x.name === name);
  return row ? row.el : null;
}})({json.dumps(row_name, ensure_ascii=False)})"""


def _playlist_checked(row_name: str) -> str:
    return f"""((name) => {{ /*yt:pl-checked*/ {_PL_LIB}
  const r = plRows();
  const row = r && r.rows.find(x => x.name === name);
  return !!row && row.checked;
}})({json.dumps(row_name, ensure_ascii=False)})"""


# --------------------------------------------------------------------------- plumbing
_sleep = asyncio.sleep


async def _shot(page: Page, step: str) -> str:
    try:
        return await page.screenshot(f"youtube_{step}_{int(time.time())}")
    except Exception:  # noqa: BLE001 - a failed screenshot must not hide the real error
        return ""


async def _dump(page: Page, step: str) -> str:
    """Write <artifacts>/youtube_<step>_dom.json (visible dialogs, buttons, links, final-button state); path or ''."""
    out = getattr(page, "artifacts_dir", None)
    if out is None:
        return ""
    try:
        data = await page.eval(JS_DOM_DUMP)
        out = Path(out)
        out.mkdir(parents=True, exist_ok=True)
        path = out / f"youtube_{step}_dom.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        return str(path)
    except Exception:  # noqa: BLE001 - debugging aid only
        return ""


@asynccontextmanager
async def _step(page: Page, name: str) -> AsyncIterator[None]:
    """Run a block as a named step: any failure becomes DriverError(step, detail [dom dump: ...], screenshot)."""
    try:
        yield
    except Exception as e:  # noqa: BLE001
        if isinstance(e, DriverError):
            step, msg = e.step, str(e)
            detail = msg[len(step) + 2:] if msg.startswith(f"{step}: ") else ("" if msg == step else msg)
            shot = e.screenshot
        else:
            step, detail, shot = name, f"{type(e).__name__}: {e}", ""
        if "dom dump:" in detail:
            raise
        shot = shot or await _shot(page, step)
        dump = await _dump(page, step)
        if dump:
            detail = f"{detail} [dom dump: {dump}]" if detail else f"dom dump: {dump}"
        raise DriverError(step, detail, shot) from e


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
    """Click a harmless spot (a plain text of the Visibility step, else the dialog title), best effort."""
    try:
        xy = await page.eval(JS_NEUTRAL_POINT % json.dumps(list(NEUTRAL_TEXTS)))
        if isinstance(xy, list) and len(xy) == 2:
            await page.click(xy=(float(xy[0]), float(xy[1])))
        elif await page.exists(NEUTRAL):
            await page.click(NEUTRAL, wait=2)
    except Exception:  # noqa: BLE001
        pass


async def close_popups(page: Page) -> bool:
    """Close an open dropdown overlay (the schedule time list, a calendar, hashtag suggestions).

    An open dropdown swallows the next click (the click only closes it). On 2026-11-01 the time list stayed open
    after the time was typed, so the trusted click on "Schedule" only closed the list and every real job timed out
    waiting for "Video scheduled". First a neutral click (what a person does), then Escape, which Polymer routes to
    the topmost overlay only (the dropdown, not the upload dialog). Returns True when nothing is open any more.
    """
    for attempt in range(3):
        if not await page.eval(JS_POPUP_OPEN):
            return True
        if attempt == 0:
            await _click_neutral(page)
        else:
            await page.press("Escape")
        await _sleep(0.4)
    return not await page.eval(JS_POPUP_OPEN)


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


def _chips_have(tag: str):
    return lambda v: isinstance(v, dict) and tag_committed(tag, [str(c) for c in v.get("chips") or []])


async def _clear_tag_input(page: Page, *, only_separators: bool = False) -> None:
    """Empty the tag input (never Backspace on an empty input: that deletes the last chip)."""
    val = await page.eval(JS_TAG_INPUT_VALUE)
    if not (isinstance(val, str) and val):
        return
    if only_separators and val.strip(" ,،"):
        return
    await page.press("a", "ctrl")
    await page.press("Backspace")


async def _commit_tag(page: Page, tag: str) -> bool:
    """Type one tag, then a comma as its own key event (Studio commits a chip on the comma key, not on inserted
    text that contains commas: the comma-joined insertText of 2026-11-01 stayed one merged Arabic string).
    Enter is the fallback. Returns True when a chip with exactly this text exists."""
    ok = _chips_have(tag)
    await page.insert_text(tag)
    await page.type_keys(",")
    if ok(await _poll(page, JS_TAGS, ok, tries=6, delay=0.3)):
        await _clear_tag_input(page, only_separators=True)
        return True
    if await page.eval(JS_TAG_INPUT_VALUE):
        await page.press("Enter")
        if ok(await _poll(page, JS_TAGS, ok, tries=6, delay=0.3)):
            await _clear_tag_input(page, only_separators=True)
            return True
    await _clear_tag_input(page)  # so the next tag does not merge into this one
    return False


async def add_tags(page: Page, tags: list[str] | str) -> tuple[list[str], str]:
    """Add tags one chip at a time. Never fails the job: returns (added, warning); tags are optional metadata."""
    try:
        before = await _read_tags(page)
    except Exception as e:  # noqa: BLE001
        return [], f"tags skipped: {e}"
    new = plan_tags(before["chips"], tags)
    if not new:
        return [], ""
    added: list[str] = []
    failed: list[str] = []
    try:
        await page.click(TAGS_INPUT)
        for t in new:
            (added if await _commit_tag(page, t) else failed).append(t)
    except Exception as e:  # noqa: BLE001
        failed += [t for t in new if t not in added and t not in failed]
        return added, f"tags error ({type(e).__name__}: {e}); not committed: {failed}"
    return added, (f"tags not committed: {failed}" if failed else "")


def _playlists_shown(text: Any, names: list[str]) -> bool:
    t = _norm(text if isinstance(text, str) else "").casefold()
    return bool(t) and all(_norm(n).casefold() in t for n in names)


async def select_playlists(page: Page, names: list[str]) -> list[str]:
    """Tick each playlist in the picker popup (exact/prefix/contains match), Done, verify the field.
    Returns the full row names that were selected."""
    async with _step(page, "playlists"):
        await close_popups(page)  # a hashtag popup from the title can cover the list
        await page.click(PLAYLIST_TRIGGER)
        # the popup first says "No playlists available" while it loads: wait for rows
        items = await _poll(page, JS_PLAYLIST_ITEMS, lambda v: isinstance(v, dict) and bool(v.get("names")),
                            tries=40, delay=0.5)
        if not isinstance(items, dict):
            raise DriverError("playlists", "the playlist picker did not open (popup with a 'Done' button not found)")
        available = [str(n) for n in items.get("names") or []]
        if not available:
            raise DriverError("playlists", "the playlist picker lists no playlists"
                              + (" ('No playlists available' for 20 s)" if items.get("empty") else ""))
        picked: list[str] = []
        for name in names:
            try:
                i = match_playlist(name, available)
            except AmbiguousPlaylist as e:
                raise DriverError("playlists", str(e)) from e
            if i is None:
                raise DriverError("playlists", f"playlist {name!r} not found; available: {available}")
            row = available[i]
            if not await page.eval(_playlist_checked(row)):
                await page.click(_playlist_box(row))  # trusted coordinate click: JS .click() on rows toggles twice
                if not await _poll(page, _playlist_checked(row), bool, tries=10, delay=0.3):
                    raise DriverError("playlists", f"playlist {row!r} did not get checked")
            picked.append(row)
        await page.click(JS_PLAYLIST_DONE)
        shown = await _poll(page, JS_PLAYLIST_TEXT, lambda v: _playlists_shown(v, names))
        if not _playlists_shown(shown, names):
            raise DriverError("playlists", f"selection not saved; field shows {shown!r}")
        return picked


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
    await close_popups(page)


async def type_time(page: Page, local: datetime) -> None:
    want = time_text(local)

    def ok(v: Any) -> bool:
        return isinstance(v, str) and _norm(v).upper() == want

    shown: Any = None
    for _ in range(2):
        await page.click(SEL_TIME_INPUT)
        await page.eval(JS_TIME_FILL % json.dumps(want))
        await close_popups(page)  # the time list stays open otherwise and eats the next click
        shown = await _poll(page, JS_TIME_VALUE, ok)
        if ok(shown):
            return
    raise DriverError("schedule_time", f"time input shows {shown!r}, wanted {want!r}")


async def set_schedule(page: Page, when: datetime) -> tuple[datetime, str]:
    async with _step(page, "schedule"):
        if when <= datetime.now(timezone.utc):
            raise DriverError("schedule", f"schedule_at {when.isoformat()} is in the past")
        await page.bring_to_front()  # the pickers are animated dropdowns; background tabs pause animations
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


def _ready(v: Any) -> bool:
    return isinstance(v, dict) and bool(v.get("visible")) and bool(v.get("enabled"))


async def click_final(page: Page, scheduled: bool) -> str:
    """Wait until the final button is visible and enabled, check its label, close popups, click. Returns the label."""
    async with _step(page, "final_click"):
        st = await _poll(page, JS_DONE_STATE, _ready, tries=240, delay=0.5)  # up to ~2 min
        if not _ready(st):
            raise DriverError("final_click", f"final button not clickable: {st!r}")
        label = _norm(str(st.get("label") or ""))
        if scheduled != ("schedule" in label.lower()):
            raise DriverError("final_click", f"button says {label!r}; expected {'Schedule' if scheduled else 'Publish/Save'}")
        await close_popups(page)
        await page.bring_to_front()
        await page.click(DONE)
        return label


async def read_result(page: Page, fallback_links: list[str], label: str = "") -> dict:
    """Wait for the "Video scheduled/published" dialog. If nothing reacts for RECLICK_EVERY polls and the final
    button is still visible, enabled and unchanged, the click was swallowed: close popups and click again (at most
    FINAL_CLICKS clicks in all). On timeout the step fails with a screenshot and a DOM dump."""
    async with _step(page, "result"):
        clicks, res = 1, None
        for i in range(1, RESULT_POLLS + 1):
            res = await page.eval(JS_RESULT)
            if isinstance(res, dict):
                break
            if i % RECLICK_EVERY == 0 and clicks < FINAL_CLICKS:
                st = await page.eval(JS_DONE_STATE)
                if _ready(st) and (not label or _norm(str(st.get("label") or "")) == label):
                    await close_popups(page)
                    await page.bring_to_front()
                    await page.click(DONE)
                    clicks += 1
            await _sleep(RESULT_POLL_S)
        else:
            st = await page.eval(JS_DONE_STATE)
            raise DriverError("result", f"no 'Video published/scheduled' dialog after ~{RESULT_POLLS * RESULT_POLL_S:.0f}s "
                              f"and {clicks} click(s) on {label or 'the final button'!r}; final button now: {st!r}")
        url, vid = extract_video([str(x) for x in res.get("links") or []] + fallback_links)
        return {"heading": str(res.get("heading") or ""), "text": str(res.get("text") or ""), "url": url, "id": vid,
                "clicks": clicks}


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
    added, tag_warning = await add_tags(page, job.tags) if job.tags else ([], "")
    picked = await select_playlists(page, opts["playlists"]) if opts["playlists"] else []
    link_now = await page.eval(JS_DIALOG_LINKS)  # the "Video link" in the dialog, kept as a fallback
    links = [str(x) for x in link_now] if isinstance(link_now, list) else []
    await go_to_visibility(page)

    local, tz_source = None, ""
    if job.schedule_at is not None:
        local, tz_source = await set_schedule(page, job.schedule_at)
    else:
        await set_visibility(page, opts["visibility"])
    when_note = f"; scheduled {local.isoformat()} (time zone from {tz_source})" if local else ""
    extras = "".join(f"; {x}" for x in (f"playlists={picked}" if picked else "", tag_warning) if x)

    if dry_run:
        progress = await page.eval(JS_PROGRESS)
        url, vid = extract_video(links)
        return PostResult(platform="youtube", ok=True, status="dry_run", url=url, post_id=vid,
                          scheduled_for=local.isoformat() if local else "",
                          message=f"filled, stopped before the final click; title={title!r}; tags added={added}"
                                  f"{when_note}{extras}; upload: {progress or '?'}",
                          screenshot=await _shot(page, "dry_run"))

    await wait_upload_finished(page)
    label = await click_final(page, scheduled=local is not None)
    res = await read_result(page, links, label)
    scheduled_for = ""
    if local is not None:
        scheduled_for = parse_confirmed(res["text"], local.tzinfo) or local.isoformat()
    confirm = next((ln.strip() for ln in res["text"].splitlines() if "set to public" in ln.lower()), "")
    id_note = f"; video id {res['id']}" if res["id"] else "; no video link found in the result dialog"
    return PostResult(platform="youtube", ok=True, status="scheduled" if local is not None else "published",
                      url=res["url"], post_id=res["id"], scheduled_for=scheduled_for,
                      message=" — ".join(x for x in (res["heading"], confirm) if x) + when_note + id_note + extras,
                      screenshot=await _shot(page, "done"))
