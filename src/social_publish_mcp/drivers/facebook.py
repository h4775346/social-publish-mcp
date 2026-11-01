"""Facebook page reels through Meta Business Suite (business.facebook.com/latest).

Flow (docs/platform_notes.md, "Facebook"): reels composer -> check "Post to" page -> attach the video to the
composer's hidden ``input[type=file]`` -> wait for "100%" -> type the description with trusted input ->
Next (Edit step) -> Next (Share step) -> remix radio -> optional scheduling -> Share / Schedule ->
"Reel processing" modal -> Done.

Selectors marked ``# UNVERIFIED`` were not observed live; check them before trusting the driver. Every JS
expression carries a ``/*fb:<name>*/`` marker so tests (and debug logs) can tell them apart.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..models import DriverError, PostJob, PostResult
from ..page import Page, PageError

START_URL = "https://business.facebook.com/latest/home"
COMPOSER_URL = "https://business.facebook.com/latest/reels_composer/?ref=biz_web_home_create_reel&asset_id={asset_id}"

UPLOAD_TIMEOUT = 900.0  # seconds; big reels on slow uplinks
STEP_TIMEOUT = 45.0
FINISH_TIMEOUT = 180.0

_BTN = "div[role=button],button,a[role=button],[role=button]"


def _js(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)


def _btn(label: str, marker: str) -> str:
    """Exact-label button (text or aria-label)."""
    return f"__sp.byText({_js(_BTN)}, {_js(label)}, true)/*fb:{marker}*/"


def _footer_btn(label: str, marker: str) -> str:
    """Last visible exact-label button: the footer action. Verified live: "Share" also exists in the step bar and
    "Schedule" in the Scheduling options segmented control, both earlier in DOM order."""
    return (f"((l, t) => {{ const all = __sp.all(l).filter(e => [e.innerText, e.getAttribute('aria-label')]"
            f".some(s => (s || '').replace(/\\s+/g, ' ').trim() === t)); return all.length ? all[all.length - 1] : null; }})"
            f"({_js(_BTN)}, {_js(label)})/*fb:{marker}*/")


def _enabled(sel: str) -> str:
    return f"(e => !!e && __sp.visible(e) && e.getAttribute('aria-disabled') !== 'true' && !e.disabled)({sel})"


# ------------------------------------------------------------------ selectors
# Verified: contenteditable with this aria-label under "Description".
EDITOR = ("(document.querySelector('[contenteditable=true][aria-label^=\"Write into the dialogue box\"]')"
          " || document.querySelector('[contenteditable=true][role=textbox]'))/*fb:editor*/")  # fallback UNVERIFIED
# Verified live: the composer has NO file input until "Add Video" is clicked; the click creates a DETACHED
# input[type=file] and calls input.click() (or showPicker()) on it. We intercept that call, keep the element in
# window.__spFile and give it the file with DOM.setFileInputFiles (works on detached inputs, fires change).
ADD_VIDEO = _btn("Add Video", "addvideo")  # verified: [role=button]/button with exact text "Add Video"
FILE_INPUT = "window.__spFile/*fb:file*/"
HOOK_FILE_JS = r"""(() => {
  window.__spFile = null;
  const P = HTMLInputElement.prototype;
  if (!window.__spOrig) window.__spOrig = {click: P.click, showPicker: P.showPicker};
  const o = window.__spOrig;
  P.click = function () { if (this.type === 'file') { window.__spFile = this; return; } return o.click.call(this); };
  if (o.showPicker) P.showPicker = function () {
    if (this.type === 'file') { window.__spFile = this; return; } return o.showPicker.call(this); };
  return true;
})()/*fb:hookfile*/"""
UNHOOK_FILE_JS = r"""(() => {
  const o = window.__spOrig; if (!o) return false;
  HTMLInputElement.prototype.click = o.click;
  if (o.showPicker) HTMLInputElement.prototype.showPicker = o.showPicker;
  delete window.__spOrig; return true;
})()/*fb:unhookfile*/"""
FILE_READY_JS = "(!!window.__spFile)/*fb:fileready*/"

# Compact summary of the visible page, saved next to the screenshot when a step fails.
DOM_DUMP_JS = r"""(() => {
  const n = s => (s || '').replace(/\s+/g, ' ').trim().slice(0, 160);
  const vis = __sp.all;
  return {
    url: location.href,
    inputs: vis('input,textarea,[contenteditable=true]').map(e => ({tag: e.tagName.toLowerCase(), type: e.type || '',
      value: n(e.value || (e.isContentEditable ? e.innerText : '')), placeholder: n(e.placeholder),
      aria: n(e.getAttribute('aria-label')), role: e.getAttribute('role') || ''})),
    controls: vis('button,[role=button],[role=radio],[role=switch],[role=checkbox],[role=spinbutton],[role=combobox],'
      + '[role=tab],[role=menuitem],[role=option]').map(e => ({role: e.getAttribute('role') || e.tagName.toLowerCase(),
      text: n(e.innerText), aria: n(e.getAttribute('aria-label')), checked: e.getAttribute('aria-checked'),
      disabled: e.getAttribute('aria-disabled') === 'true' || !!e.disabled,
      valuetext: e.getAttribute('aria-valuetext') || ''})).filter(x => x.text || x.aria),
    headings: vis('h1,h2,h3,h4,[role=heading]').map(e => n(e.innerText)).filter(Boolean),
  };
})()/*fb:domdump*/"""
CREATE_REEL = f"__sp.byText({_js(_BTN + ',a')}, 'Create reel')/*fb:createreel*/"  # UNVERIFIED element type
NEXT = _btn("Next", "next")  # label verified; role/tag UNVERIFIED
SHARE = _footer_btn("Share", "share")  # label verified; footer button (last match)
SCHEDULE = _footer_btn("Schedule", "schedule")  # footer button (last match); exact so "Scheduling options" is not hit
DONE = _btn("Done", "done")  # label verified
SCHED_OPTIONS = ("(__sp.byText('[role=button],button,[aria-expanded]', 'Scheduling options')"
                 " || __sp.byText('div,span', 'Scheduling options', true))/*fb:schedopts*/")  # UNVERIFIED clickable element

# "Post to <page name>": find the "Post to" label and climb until the text includes more than the label.
POST_TO_JS = r"""(() => {
  const els = [...document.querySelectorAll('div,span,h2,h3,label')]
    .filter(e => __sp.visible(e) && /^\s*Post to\s*$/i.test(e.innerText || ''));
  for (const el of els) {
    let p = el;
    for (let i = 0; i < 5 && p.parentElement; i++) {
      p = p.parentElement;
      const t = (p.innerText || '').replace(/\s+/g, ' ').trim();
      if (t.length > 8) return t;
    }
  }
  return '';
})()/*fb:postto*/"""  # UNVERIFIED structure (label text "Post to" verified)

UPLOAD_JS = r"""(() => {
  const t = document.body.innerText || '';
  if (/upload failed|couldn.t upload|can.t upload|upload error/i.test(t)) return 'error';
  if (/(^|\D)100\s*%/.test(t)) return 'done';
  return '';
})()/*fb:upload*/"""  # "100%" verified; error phrases UNVERIFIED

READBACK_JS = f"(e => e ? e.innerText : null)({EDITOR})/*fb:readback*/"

SHARE_STEP_JS = (f"(!!{SHARE} || !!{SCHEDULE} || /Scheduling options/.test(document.body.innerText))"
                 "/*fb:sharestep*/")

FINISH_JS = r"""(() => {
  const t = document.body.innerText || '';
  if (/Reel processing/i.test(t)) return 'processing';
  const ok = /(reel|post) (has been|was|is) (scheduled|published|shared)|successfully (scheduled|published|shared)/i;
  if (ok.test(t)) return 'toast';
  if (/something went wrong|couldn.t (share|publish|schedule)/i.test(t)) return 'error';
  return '';
})()/*fb:finish*/"""  # "Reel processing" verified; toast/error phrases UNVERIFIED

# Remix radios. Label for True from the task brief; False labels are guesses.  # UNVERIFIED
REMIX_ALLOW = ["Remixing and original audio use"]
REMIX_DENY = ["Original audio use only", "Don't allow", "Do not allow", "No remixing"]


def _radio_row(labels: list[str]) -> str:
    """Smallest ancestor of a radio (input or role=radio) whose text contains one of labels, holding only that radio."""
    return (r"""((txts) => {
  for (const r of document.querySelectorAll('input[type=radio],[role=radio]')) {
    let p = r;
    for (let i = 0; i < 6 && p; i++) {
      if (p !== r && p.querySelectorAll('input[type=radio],[role=radio]').length > 1) break;
      const t = (p.innerText || '') + ' ' + ((p.getAttribute && p.getAttribute('aria-label')) || '');
      if (txts.some(x => t.includes(x))) return p;
      p = p.parentElement;
    }
  }
  return null;
})(""" + _js_list(labels) + ")/*fb:radio*/")


def _radio_checked(labels: list[str]) -> str:
    return (f"(row => {{ if (!row) return null; const r = row.matches('input[type=radio],[role=radio]') ? row :"
            f" row.querySelector('input[type=radio],[role=radio]'); return !!r && (r.checked ||"
            f" r.getAttribute('aria-checked') === 'true'); }})({_radio_row(labels)})/*fb:radiochecked*/")


def _js_list(xs: list[str]) -> str:
    return json.dumps(xs, ensure_ascii=False)


# Scheduling inputs (all UNVERIFIED: the Share-step scheduler was never inspected live).
DATE_INPUT = r"""(() => {
  const ins = __sp.all('input');
  return ins.find(e => /mm\/dd|date/i.test((e.placeholder || '') + ' ' + (e.getAttribute('aria-label') || '')))
      || ins.find(e => /^\d{1,2}\/\d{1,2}\/\d{4}$/.test((e.value || '').trim())) || null;
})()/*fb:dateinput*/"""
TIME_INPUT = r"""(() => {
  const ins = __sp.all('input');
  return ins.find(e => /hh|time/i.test((e.placeholder || '') + ' ' + (e.getAttribute('aria-label') || '')))
      || ins.find(e => /^\d{1,2}:\d{2}(\s*[AP]M)?$/i.test((e.value || '').trim())) || null;
})()/*fb:timeinput*/"""
SPIN = "(__sp.all('[role=spinbutton]').find(e => /{key}/i.test(e.getAttribute('aria-label') || '')) || null)/*fb:spin{key}*/"
SCHED_SEGMENT = "__sp.byText('[role=button],button', 'Schedule', true)/*fb:schedsegment*/"  # verified live 2026-10-30
SCHED_TOGGLE = (r"""(() => { for (const t of ['Set date and time', 'Schedule for later', 'Schedule']) {
  const row = __sp.byText('[role=switch],[role=checkbox],[role=radio],label', t);
  if (row) return row; } return null; })()/*fb:schedtoggle*/""")
DATE_VALUE_JS = f"(e => e ? (e.value || e.innerText || '') : null)({DATE_INPUT})/*fb:dateval*/"
# live: the hours/minutes/meridiem spinbutton inputs keep an empty .value; the time is drawn as text beside them
# ("05:00 PM Africa/Cairo"), so read the text of the nearest ancestor that shows h:mm AM/PM
TIME_VALUE_JS = (f"(e => e ? (e.value || e.innerText || '') : (h => {{ for (let p = h; p; p = p.parentElement) {{"
                 f" const m = (p.innerText || '').replace(/\\s+/g, '').match(/(\\d{{1,2}}):(\\d{{2}})([AP]M)/i);"
                 f" if (m) return m[1] + ':' + m[2] + ' ' + m[3]; }} return ''; }})"
                 f"({SPIN.format(key='hour')}))({TIME_INPUT})/*fb:timeval*/")

# check_login
LOGIN_JS = r"""(() => {
  const u = location.href, t = document.body ? (document.body.innerText || '') : '';
  const loginPage = /\/login|loginpage|checkpoint/.test(u) || !!document.querySelector('input[name=email],input[name=pass]');
  const bs = /business\.facebook\.com\/latest/.test(u);
  let name = '';
  const cands = [
    ...document.querySelectorAll('[aria-label*="business asset" i],[aria-label*="Switch" i],[role=combobox]'),
  ].filter(__sp.visible);
  for (const c of cands) { const s = (c.innerText || c.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim();
    if (s && s.length < 80) { name = s; break; } }
  const asset = new URL(u).searchParams.get('asset_id') || '';
  return {url: u, loginPage, bs, name, asset, home: /Home/.test(t) && /Content|Planner/.test(t)};
})()/*fb:login*/"""  # header/selector structure UNVERIFIED


# ------------------------------------------------------------------ helpers
async def _sleep(s: float) -> None:  # patched in tests
    await asyncio.sleep(s)


class _Step:
    """async with _Step(page, "upload"): ... -> any failure becomes DriverError(step, detail, screenshot)."""

    def __init__(self, page: Page, name: str) -> None:
        self.page, self.name = page, name

    async def __aenter__(self) -> _Step:
        return self

    async def __aexit__(self, et: Any, e: BaseException | None, tb: Any) -> bool:
        if e is None or not isinstance(e, Exception):
            return False
        if isinstance(e, DriverError) and "dom dump:" in str(e):
            return False  # already handled by an inner step
        step, detail = self.name, str(e) or type(e).__name__
        if isinstance(e, DriverError):
            step = e.step
            if detail.startswith(f"{step}: "):
                detail = detail[len(step) + 2:]
            elif detail == step:
                detail = ""
        shot = getattr(e, "screenshot", "") or await _shot(self.page, step)
        dump = await _dump(self.page, step)
        if dump:
            detail = f"{detail} [dom dump: {dump}]" if detail else f"dom dump: {dump}"
        raise DriverError(step, detail, shot) from e


async def _shot(page: Page, step: str) -> str:
    try:
        return await page.screenshot(f"facebook_{step}_{int(time.time())}")
    except Exception:  # noqa: BLE001 - a failed screenshot must not hide the real error
        return ""


async def _dump(page: Page, step: str) -> str:
    """Write <artifacts>/facebook_<step>_dom.json (visible inputs/controls/headings); return its path or ''."""
    out = getattr(page, "artifacts_dir", None)
    if out is None:
        return ""
    try:
        data = await page.eval(DOM_DUMP_JS)
        out = Path(out)
        out.mkdir(parents=True, exist_ok=True)
        path = out / f"facebook_{step}_dom.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        return str(path)
    except Exception:  # noqa: BLE001 - debugging aid only
        return ""


def _norm(s: str) -> str:
    s = s.replace("​", "").replace("﻿", "")
    return re.sub(r"\s+", " ", s).strip()


_HASHTAG = re.compile(r"(#[^\s#]+)")


def caption_plan(caption: str) -> list[tuple[str, ...]]:
    """The exact input sequence used for a caption: ("text", s) / ("key", name, *mods).

    Each hashtag is followed by a typed space and Escape (closes the autocomplete that otherwise swallows
    the next characters); the space we typed replaces one leading space of the following segment. Lines are
    separated with Shift+Enter.
    """
    ops: list[tuple[str, ...]] = []
    lines = caption.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for i, line in enumerate(lines):
        if i:
            ops.append(("key", "Enter", "shift"))
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


async def _type_caption(page: Page, caption: str) -> None:
    for op in caption_plan(caption):
        if op[0] == "text":
            await page.insert_text(op[1])
        else:
            await page.press(op[1], *op[2:])


async def _wait(page: Page, js: str, timeout: float, what: str) -> Any:
    return await page.wait_for(js, timeout=timeout, what=what)


# ------------------------------------------------------------------ contract
async def check_login(page: Page) -> dict:
    info = await page.eval(LOGIN_JS) or {}
    if not info.get("bs") and not info.get("loginPage"):
        await page.goto(START_URL)
        info = await page.eval(LOGIN_JS) or {}
    if info.get("loginPage"):
        return {"logged_in": False, "account": "", "detail": f"login page: {info.get('url', '')}"}
    if not info.get("bs"):
        return {"logged_in": False, "account": "", "detail": f"not on Business Suite: {info.get('url', '')}"}
    account = info.get("name") or (f"asset_id={info['asset']}" if info.get("asset") else "")
    detail = "Business Suite open" + ("" if account else " (page name not found in header; UNVERIFIED selector)")
    return {"logged_in": True, "account": account, "detail": detail}


async def publish(page: Page, job: PostJob, *, dry_run: bool = False) -> PostResult:
    opts = job.options or {}
    asset_id = str(opts.get("asset_id") or "").strip()
    want_page = str(opts.get("page_name") or "").strip()

    async with _Step(page, "open_composer"):
        if asset_id:
            await page.goto(COMPOSER_URL.format(asset_id=asset_id))
        else:
            await page.click(CREATE_REEL, wait=STEP_TIMEOUT)
        await page.wait_el(ADD_VIDEO, timeout=STEP_TIMEOUT)

    async with _Step(page, "post_to"):
        post_to = _norm(await page.eval(POST_TO_JS) or "")
        if want_page and want_page.lower() not in post_to.lower():
            raise DriverError("post_to", f"expected page {want_page!r}, composer shows {post_to or 'nothing'!r}")
        account = re.sub(r"^\s*Post to\s*", "", post_to, flags=re.I)

    async with _Step(page, "upload"):
        await page.eval(HOOK_FILE_JS)
        try:
            await page.click(ADD_VIDEO)
            try:
                await page.wait_for(FILE_READY_JS, timeout=3, what="file input created by 'Add Video'")
            except PageError:
                # live: in a background job tab the trusted click did not reach the button; element.click() does,
                # and the hooked input.click() needs no user activation
                await page.js_click(ADD_VIDEO)
                try:
                    await page.wait_for(FILE_READY_JS, timeout=12, what="file input created by 'Add Video'")
                except PageError as e:
                    raise DriverError("upload", "'Add Video' did not create a file input") from e
            await page.set_files(FILE_INPUT, [job.video])
        finally:
            try:
                await page.eval(UNHOOK_FILE_JS)
            except Exception:  # noqa: BLE001 - restoring the prototype is best effort
                pass
        state = await _wait(page, UPLOAD_JS, UPLOAD_TIMEOUT, "upload 100%")
        if state == "error":
            raise DriverError("upload", "the composer reported an upload error")

    async with _Step(page, "description"):
        if job.caption.strip():
            await _fill_description(page, job.caption)

    async with _Step(page, "next_edit"):
        await _wait(page, _enabled(NEXT), STEP_TIMEOUT, "Next enabled (to Edit step)")
        await page.click(NEXT)
        await _sleep(1.5)
    async with _Step(page, "next_share"):
        await _wait(page, _enabled(NEXT), STEP_TIMEOUT, "Next enabled (to Share step)")
        await page.click(NEXT)
        await _wait(page, SHARE_STEP_JS, STEP_TIMEOUT, "Share step")

    async with _Step(page, "remix"):
        await _set_remix(page, bool(opts.get("allow_remix", True)))

    scheduled_for = ""
    if job.schedule_at is not None:
        async with _Step(page, "schedule"):
            scheduled_for = await _schedule(page, job.schedule_at)
        final, final_label = SCHEDULE, "Schedule"
    else:
        final, final_label = SHARE, "Share"

    async with _Step(page, "final_button"):
        await _wait(page, _enabled(final), STEP_TIMEOUT, f"{final_label} button enabled")

    if dry_run:
        return PostResult("facebook", True, "dry_run", scheduled_for=scheduled_for,
                          message=f"filled; stopped before {final_label}" + (f" (page {account})" if account else ""))

    async with _Step(page, "submit"):
        # live: in a background tab the trusted click on "Schedule" did nothing (page unchanged for 180 s)
        await page.bring_to_front()
        await page.click(final)  # never retried: a second click could create a duplicate post
        state = await _wait(page, FINISH_JS, FINISH_TIMEOUT, "Reel processing / success")
        if state == "error":
            raise DriverError("submit", "Business Suite reported an error after " + final_label)
        if await page.exists(DONE):
            await page.click(DONE)

    status = "scheduled" if job.schedule_at is not None else "published"
    where = "Content > Scheduled (Planner)" if status == "scheduled" else "Content > Posts & reels"
    return PostResult("facebook", True, status, scheduled_for=scheduled_for,
                      message=f"{final_label} clicked ({state}); find it in Business Suite {where}"
                      + (f" for {account}" if account else ""),
                      screenshot=await _shot(page, "done"))


# ------------------------------------------------------------------ steps
async def _fill_description(page: Page, caption: str) -> None:
    want = _norm(caption)
    await page.wait_el(EDITOR, timeout=STEP_TIMEOUT)
    got = ""
    for attempt in range(2):
        await page.click(EDITOR)
        if attempt:
            await page.select_all_and_delete()
        await _type_caption(page, caption)
        await _sleep(0.5)
        got = _norm(await page.eval(READBACK_JS) or "")
        if got == want:
            return
    raise DriverError("description", f"read-back differs after retry: got {got[:200]!r}, want {want[:200]!r}")


async def _set_remix(page: Page, allow: bool) -> None:
    labels = REMIX_ALLOW if allow else REMIX_DENY
    row = _radio_row(labels)
    if not await page.exists(row):
        if allow:  # allowing remix is the composer default: nothing to do
            return
        raise DriverError("remix", f"no 'disallow remix' radio found (looked for {labels}; UNVERIFIED labels)")
    if await page.eval(_radio_checked(labels)):
        return
    await page.click(row)
    if not await page.eval(_radio_checked(labels)):
        await page.js_click(row)
        if not await page.eval(_radio_checked(labels)):
            raise DriverError("remix", f"could not select remix option {labels[0]!r}")


def _local(when: datetime, offset_min: int) -> datetime:
    # JS getTimezoneOffset() is minutes *behind* UTC (Cairo GMT+2 -> -120)
    return when.astimezone(timezone(timedelta(minutes=-offset_min)))


def _fmt_time(d: datetime) -> str:
    return f"{d.hour % 12 or 12}:{d.minute:02d} {'AM' if d.hour < 12 else 'PM'}"


def _parse_date(s: str) -> tuple[int, int, int] | None:
    s = _norm(s)
    for fmt in ("%m/%d/%Y", "%b %d, %Y", "%B %d, %Y", "%a, %b %d, %Y", "%A, %B %d, %Y", "%Y-%m-%d"):
        try:
            d = datetime.strptime(s, fmt)
            return d.year, d.month, d.day
        except ValueError:
            continue
    return None


def _parse_time(s: str) -> tuple[int, int] | None:
    m = re.search(r"(\d{1,2})\s*:\s*(\d{2})\s*([AaPp])?", s or "")
    if not m:
        return None
    h, mi, ap = int(m.group(1)), int(m.group(2)), (m.group(3) or "").upper()
    if ap == "P" and h != 12:
        h += 12
    elif ap == "A" and h == 12:
        h = 0
    return h, mi


DATE_TRIES = 3


def date_entry_plan(date_s: str, attempt: int) -> list[tuple[str, ...]]:
    """Input sequence for one try at the date field (after a real click on it): select all, the date, commit.

    Try 0 is what worked live (ctrl+a, insertText mm/dd/yyyy, Enter); later tries also press Tab to blur/commit,
    and the last one types the date as key events (for a masked input that ignores insertText)."""
    typed: tuple[str, ...] = ("keys", date_s) if attempt >= 2 else ("text", date_s)
    ops: list[tuple[str, ...]] = [("key", "a", "ctrl"), typed, ("key", "Enter")]
    if attempt >= 1:
        ops.append(("key", "Tab"))
    return ops


async def _enter_date(page: Page, local: datetime, date_s: str) -> str:
    """Set the date field and read it back; up to DATE_TRIES tries.

    2026-11-01: one job left the field at its default ("Nov 1, 2026", wanted 11/02/2026) while a TikTok job in the
    same window was bringing its own tab to the front; the retry minutes later worked. So: bring this tab to the
    front, real click, select all, type, commit, re-read; retry with a short pause."""
    want = (local.year, local.month, local.day)
    shown = ""
    for attempt in range(DATE_TRIES):
        await page.bring_to_front()
        await page.click(DATE_INPUT)
        for op in date_entry_plan(date_s, attempt):
            if op[0] == "text":
                await page.insert_text(op[1])
            elif op[0] == "keys":
                await page.type_keys(op[1])
            else:
                await page.press(op[1], *op[2:])
        await _sleep(0.6)
        shown = await page.eval(DATE_VALUE_JS) or ""
        if _parse_date(shown) == want:
            return shown
        await _sleep(0.8 * (attempt + 1))
    raise DriverError("schedule", f"date shows {shown!r}, wanted {date_s} (after {DATE_TRIES} tries)")


async def _schedule(page: Page, when: datetime) -> str:
    # The UI shows times in the browser/account zone; ask the browser for its offset at that exact instant.
    offset = await page.eval(f"new Date({int(when.timestamp() * 1000)}).getTimezoneOffset()/*fb:tzoffset*/")
    if offset is None:
        raise DriverError("schedule", "could not read the browser time zone offset")
    local = _local(when, int(offset))
    if local <= datetime.now(local.tzinfo) + timedelta(minutes=10):
        raise DriverError("schedule", f"schedule time {local.isoformat()} is in the past or too close")
    date_s, time_s = local.strftime("%m/%d/%Y"), _fmt_time(local)

    if not await page.exists(SCHED_OPTIONS):
        raise DriverError("schedule", "'Scheduling options' not found on the Share step (UNVERIFIED selector)")
    if not await page.exists(DATE_INPUT):
        # live: "Scheduling options" is a card with a segmented control Share now | Schedule | Save as draft;
        # its "Schedule" segment is the first exact "Schedule" button in DOM order (the footer button comes later)
        await page.click(SCHED_SEGMENT)
        await _sleep(0.8)
    if not await page.exists(DATE_INPUT) and await page.exists(SCHED_TOGGLE):  # UNVERIFIED: a switch may gate the fields
        await page.click(SCHED_TOGGLE)
        await _sleep(0.8)
    try:
        await page.wait_el(DATE_INPUT, timeout=10)
    except PageError as e:
        raise DriverError("schedule", "no date input after opening 'Scheduling options' (UNVERIFIED selector)") from e

    await _enter_date(page, local, date_s)

    # Time: a single text input, or hour/minute/meridiem spinbuttons.  # UNVERIFIED either way
    await page.bring_to_front()
    if await page.exists(TIME_INPUT):
        await page.click(TIME_INPUT)
        await page.press("a", "ctrl")
        await page.insert_text(time_s)
        await page.press("Tab")
    else:
        hour, minute, mer = SPIN.format(key="hour"), SPIN.format(key="minute"), SPIN.format(key="meridiem|am|pm")
        if not (await page.exists(hour) and await page.exists(minute)):
            raise DriverError("schedule", "no time input or hour/minute spinbuttons found (UNVERIFIED selectors)")
        h12 = str(local.hour % 12 or 12)
        # live: inputs aria-label hours / minutes / meridiem (role=spinbutton); they take key events, not insertText
        for sel, val in ((hour, f"{h12:0>2}"), (minute, f"{local.minute:02d}")):
            await page.click(sel)
            await page.type_keys(val)
        if await page.exists(mer):
            want = "AM" if local.hour < 12 else "PM"
            await page.click(mer)
            await page.type_keys(want[0])
            # live: typing "P" did not flip an AM field; ArrowUp toggles it — re-read aria-valuetext each time
            for _ in range(2):
                shown = await page.eval(f"(e => e ? (e.getAttribute('aria-valuetext') || e.value || '') : '')({mer})") or ""
                if shown.strip().upper() == want:
                    break
                await page.press("ArrowUp")
        await page.press("Tab")
    await _sleep(0.5)

    shown_date = await page.eval(DATE_VALUE_JS) or ""
    if _parse_date(shown_date) != (local.year, local.month, local.day):
        await _enter_date(page, local, date_s)  # the time entry must not move the date; if it did, set it again
    shown_time = await page.eval(TIME_VALUE_JS) or ""
    if _parse_time(shown_time) != (local.hour, local.minute):
        raise DriverError("schedule", f"time shows {shown_time!r}, wanted {time_s}")
    try:
        await page.wait_el(SCHEDULE, timeout=10)
    except PageError as e:
        raise DriverError("schedule", "final button did not change to 'Schedule' after setting date/time") from e
    return local.isoformat()
