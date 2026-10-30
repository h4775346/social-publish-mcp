from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest

from social_publish_mcp.drivers import tiktok as tt
from social_publish_mcp.models import DriverError, PostJob

from .fakes import FakePage

CAPTION = "حبيبي يا نور العين ❤️🎶\nNew song out now! #NileBeats #أغاني_مصرية done\n#music"
NOW = datetime(2026, 10, 30, 18, 0, tzinfo=timezone.utc)
CAIRO = timezone(timedelta(hours=2))


@pytest.fixture(autouse=True)
def _patch(monkeypatch):
    async def nop(_s):
        return None

    monkeypatch.setattr(tt, "_sleep", nop)
    monkeypatch.setattr(tt, "_now", lambda: NOW)


# ------------------------------------------------------------------ fakes
class Wheel:
    """A non-circular wheel: shows the selected value and up to 3 neighbours on each side."""

    def __init__(self, values: list[int], selected: int) -> None:
        self.values, self.selected, self.clicks = values, selected, []

    def visible(self) -> list[int]:
        i = self.values.index(self.selected)
        return self.values[max(0, i - 3): i + 4]

    def state(self, _js: str) -> dict:
        return {"selected": self.selected, "items": self.visible()}

    def click(self, js: str):
        """Evaluating the item expression returns its [x, y]; the trusted click at that point then selects it."""
        v = int(re.search(r"tt:wheelitem:\w:(\d+)", js).group(1))
        if v not in self.visible():
            return None
        self.clicks.append(v)
        self.selected = v
        return [100.0, 200.0]


class Studio:
    """Scripted TikTok Studio upload page around a FakePage."""

    def __init__(self, *, date="2026-10-30", time="20:35", readback=None, pre=None, row=None) -> None:
        self.date, self.time = date, time
        self.month_clicks = 0
        self.hours = Wheel(list(range(24)), int(time[:2]))
        self.minutes = Wheel(list(range(0, 60, 5)), int(time[3:]))
        self.readback = readback
        rules = list(pre or []) + [
            ("tt:login", {"url": tt.START_URL, "upload": True, "login": False, "name": "Nile Beats · نايل بيتس"}),
            ("tt:uploaded", "done"),
            ("tt:editor", True),
            ("tt:visdrop", "Everyone"),
            ("tt:ctl:AI-generated content:state", "off"),
            ("tt:ctl:Comment:state", "on"),
            ("tt:tz", [[120, "Africa/Cairo"]]),  # one answer that is a list (a bare list = a sequence of answers)
            ("tt:schedchecked", False),
            ("tt:pickertext:date", lambda _js: self.date),
            ("tt:pickertext:time", lambda _js: self.time_text()),
            ("tt:nextmonth", self.next_month),
            ("tt:day:", self.day),
            ("tt:wheelitem:h", self.hours.click),
            ("tt:wheelitem:m", self.minutes.click),
            ("tt:wheel:h", self.hours.state),
            ("tt:wheel:m", self.minutes.state),
            ("tt:href", "https://www.tiktok.com/tiktokstudio/content"),
            ("tt:row", row if row is not None else "حبيبي يا نور العين ❤️🎶 New song Content under review"),
        ]
        self.page = FakePage(rules)
        orig_text = self.page.text

        async def text(sel: str = "document.body") -> str:
            if "tt:editor" in sel and self.page.resolve(sel):
                self.page.calls.append(("text_of", sel))
                return self.editor()
            return await orig_text(sel)

        self.page.text = text

    def time_text(self) -> str:
        return f"{self.hours.selected:02d}:{self.minutes.selected:02d}"

    def editor(self) -> str:
        if self.readback is not None:
            if isinstance(self.readback, list):
                return self.readback.pop(0) if len(self.readback) > 1 else self.readback[0]
            return self.readback
        calls = self.page.calls
        idx = max((i for i, c in enumerate(calls) if c == ("press", "a", ("ctrl",))), default=-1)
        out = []
        for c in calls[idx + 1:]:
            if c[0] == "text":
                out.append(c[1])
            elif c[0] == "press" and c[1] == "Enter":
                out.append("\n")
        return "".join(out)

    def next_month(self, _js: str) -> bool:
        self.month_clicks += 1
        return True

    def day(self, js: str) -> bool:
        d = int(re.search(r"tt:day:(\d+)", js).group(1))
        y, m = int(self.date[:4]), int(self.date[5:7]) + self.month_clicks
        y, m = y + (m - 1) // 12, (m - 1) % 12 + 1
        self.date = f"{y:04d}-{m:02d}-{d:02d}"
        return True


def job(**kw):
    kw.setdefault("caption", CAPTION)
    return PostJob(platform="tiktok", video="clip.mp4", **kw)


def typed(page):
    return [c for c in page.calls if c[0] in ("text", "press")]


def clicks(page):
    return [c[1] for c in page.calls if c[0] == "click" and isinstance(c[1], str)]


def plan_as_calls(caption):
    return [("text", op[1]) if op[0] == "text" else ("press", op[1], tuple(op[2:])) for op in tt.caption_plan(caption)]


# ------------------------------------------------------------------ caption
def test_caption_plan_hashtag_space_escape_and_unicode():
    ops = tt.caption_plan("أهلا 😀 #NileBeats #حب نهاية\nline2 #end")
    assert ops == [
        ("text", "أهلا 😀 "),
        ("text", "#NileBeats"), ("text", " "), ("key", "Escape"),
        ("text", "#حب"), ("text", " "), ("key", "Escape"),
        ("text", "نهاية"),
        ("key", "Enter", "shift"),
        ("text", "line2 "),
        ("text", "#end"), ("text", " "), ("key", "Escape"),
    ]
    joined = "".join(o[1] if o[0] == "text" else ("\n" if o[1] == "Enter" else "") for o in ops)
    assert tt._norm(joined) == tt._norm("أهلا 😀 #NileBeats #حب نهاية\nline2 #end")


async def test_publish_now_types_caption_and_posts():
    s = Studio()
    res = await tt.publish(s.page, job())
    assert res.ok and res.status == "published" and res.platform == "tiktok"
    assert res.message.startswith("Content under review")
    assert tt.CONTENT_URL_PART in res.url
    assert any(c[0] == "set_files" and c[2] == ["clip.mp4"] for c in s.page.calls)
    t = typed(s.page)
    assert t[:2] == [("press", "a", ("ctrl",)), ("press", "Delete", ())]
    assert t[2:] == plan_as_calls(CAPTION)
    assert t.count(("press", "Escape", ())) == 3  # one per hashtag
    assert ("text", "حبيبي يا نور العين ❤️🎶") in t and ("text", "#أغاني_مصرية") in t
    for i, c in enumerate(t):  # every hashtag is followed by a typed space and Escape
        if c[0] == "text" and c[1].startswith("#"):
            assert t[i + 1] == ("text", " ") and t[i + 2] == ("press", "Escape", ())
    cl = clicks(s.page)
    assert any("tt:submit:Post" in c for c in cl)
    assert not any("tt:schedradio" in c or "tt:ctl:" in c or "tt:visdrop" in c for c in cl)


async def test_caption_too_long_rejected_early():
    s = Studio()
    with pytest.raises(DriverError) as ei:
        await tt.publish(s.page, job(caption="x" * 4001))
    assert ei.value.step == "validate"
    assert not any(c[0] == "set_files" for c in s.page.calls)


async def test_readback_mismatch_retries_once_then_ok():
    s = Studio(readback=["", "garbled", "", CAPTION])
    res = await tt.publish(s.page, job(), dry_run=True)
    assert res.status == "dry_run"
    assert typed(s.page).count(("press", "a", ("ctrl",))) == 2


async def test_readback_mismatch_twice_fails():
    s = Studio(readback=["", "garbled"])
    with pytest.raises(DriverError) as ei:
        await tt.publish(s.page, job())
    assert ei.value.step == "caption" and ei.value.screenshot.startswith("tiktok_caption")


# ------------------------------------------------------------------ wheel / scheduling
@pytest.mark.parametrize("start,target", [(20, 2), (3, 23), (11, 12), (0, 0), (23, 0)])
async def test_pick_wheel_hours(start, target):
    w = Wheel(list(range(24)), start)
    page = FakePage([("tt:wheelitem:h", w.click), ("tt:wheel:h", w.state)])
    await tt._pick_wheel(page, "h", target)
    assert w.selected == target
    assert len(w.clicks) <= abs(start - target) // 3 + 1


@pytest.mark.parametrize("start,target", [(35, 5), (0, 55), (50, 55)])
async def test_pick_wheel_minutes(start, target):
    w = Wheel(list(range(0, 60, 5)), start)
    page = FakePage([("tt:wheelitem:m", w.click), ("tt:wheel:m", w.state)])
    await tt._pick_wheel(page, "m", target)
    assert w.selected == target


async def test_pick_wheel_is_bounded_when_stuck():
    state = {"selected": 10, "items": [7, 8, 9, 10, 11, 12, 13]}
    page = FakePage([("tt:wheelitem:h", lambda _js: [1.0, 2.0]), ("tt:wheel:h", state)])  # clicks never move it
    with pytest.raises(DriverError) as ei:
        await tt._pick_wheel(page, "h", 20)
    assert "does not move" in str(ei.value)
    assert len([c for c in page.calls if c[0] == "click"]) <= 3


async def test_pick_wheel_missing_raises():
    page = FakePage([("tt:wheel:h", None)])
    with pytest.raises(DriverError):
        await tt._pick_wheel(page, "h", 5)


def test_round_up_5():
    d = datetime(2026, 10, 31, 20, 0, tzinfo=CAIRO)
    assert tt.round_up_5(d) == d
    assert tt.round_up_5(d.replace(minute=1)) == d.replace(minute=5)
    assert tt.round_up_5(d.replace(second=30)) == d.replace(minute=5)
    assert tt.round_up_5(d.replace(minute=57)) == datetime(2026, 10, 31, 21, 0, tzinfo=CAIRO)
    assert tt.round_up_5(datetime(2026, 10, 31, 23, 58, tzinfo=CAIRO)) == datetime(2026, 11, 1, 0, 0, tzinfo=CAIRO)


def test_schedule_window_and_zone():
    t = tt.schedule_target(NOW + timedelta(hours=26, minutes=2), now=NOW, utc_offset_min=120)
    assert t.isoformat() == "2026-10-31T22:05:00+02:00"
    with pytest.raises(DriverError, match="less than 15"):
        tt.schedule_target(NOW + timedelta(minutes=10), now=NOW, utc_offset_min=120)
    with pytest.raises(DriverError, match="more than 10 days"):
        tt.schedule_target(NOW + timedelta(days=10, minutes=5), now=NOW, utc_offset_min=0)
    assert tt.schedule_target(NOW + timedelta(minutes=15), now=NOW, utc_offset_min=0) == NOW + timedelta(minutes=15)


async def test_schedule_out_of_window_fails_before_upload():
    s = Studio()
    with pytest.raises(DriverError) as ei:
        await tt.publish(s.page, job(schedule_at=NOW + timedelta(days=11)))
    assert ei.value.step == "schedule"
    assert not any(c[0] == "set_files" for c in s.page.calls)


async def test_schedule_full_flow_rounds_and_verifies():
    s = Studio(date="2026-10-30", time="20:35")
    when = datetime(2026, 10, 31, 18, 2, tzinfo=timezone.utc)  # 20:02 Cairo -> 20:05
    s.page.rules.insert(0, ("tt:row", "حبيبي يا نور العين ❤️🎶 Oct 31, 8:05 PM (GMT+2) Cairo"))
    res = await tt.publish(s.page, job(schedule_at=when))
    assert res.ok and res.status == "scheduled"
    assert res.scheduled_for == "2026-10-31T20:05:00+02:00"
    assert "rounded up to 20:05" in res.message and "Oct 31, 8:05 PM (GMT+2) Cairo" in res.message
    assert "WARNING" not in res.message
    assert s.date == "2026-10-31" and s.time_text() == "20:05"
    assert s.hours.clicks == [] and s.minutes.clicks == [20, 5]
    cl = clicks(s.page)
    assert any("tt:schedradio" in c for c in cl) and any("tt:day:31" in c for c in cl)
    assert any("tt:submit:Schedule" in c for c in cl) and not any("tt:submit:Post" in c for c in cl)
    # the date/time are set before the final click
    calls = s.page.calls
    last_wheel = max(i for i, c in enumerate(calls) if c[0] == "eval" and "tt:wheelitem" in c[1])
    first_submit = next(i for i, c in enumerate(calls) if c[0] == "click" and "tt:submit" in str(c[1]))
    assert last_wheel < first_submit
    # wheel items are clicked by coordinates (trusted click, no scrollIntoView)
    assert [c[1] for c in calls if c[0] == "click" and isinstance(c[1], tuple)] == [(100.0, 200.0)] * 2


def test_verified_selectors():
    assert 'input[type=radio][value="schedule"]' in tt.SCHEDULE_RADIO
    assert "input.TUXTextInputCore-input" in tt._picker("time") and ".value" in tt._picker_text("date")
    assert "span.day.valid" in tt._day_cell(5) and "span.arrow" in tt.NEXT_MONTH
    w = tt._wheel_item("m", 5)
    assert "tiktok-timepicker-" in w and "'right' : 'left'" in w and "scrollIntoView" not in w
    assert 'accept=\\"video/*\\"' in tt.FILE_INPUT or 'accept=\"video/*\"' in tt.FILE_INPUT


async def test_schedule_next_month():
    s = Studio(date="2026-10-30", time="10:00")
    when = datetime(2026, 11, 2, 7, 0, tzinfo=timezone.utc)  # 09:00 Cairo
    res = await tt.publish(s.page, job(schedule_at=when), dry_run=True)
    assert res.status == "dry_run" and res.scheduled_for == "2026-11-02T09:00:00+02:00"
    assert s.month_clicks == 1 and s.date == "2026-11-02" and s.time_text() == "09:00"


async def test_schedule_date_mismatch_fails():
    s = Studio()
    s.day = lambda js: True  # noqa: E731 - day click does nothing
    s.page.rules = [r if r[0] != "tt:day:" else ("tt:day:", s.day) for r in s.page.rules]
    with pytest.raises(DriverError) as ei:
        await tt.publish(s.page, job(schedule_at=NOW + timedelta(days=2)), dry_run=True)
    assert ei.value.step == "schedule" and "date shows" in str(ei.value)


# ------------------------------------------------------------------ options / dry run / errors
async def test_dry_run_stops_before_final_click():
    s = Studio()
    res = await tt.publish(s.page, job(), dry_run=True)
    assert res.ok and res.status == "dry_run"
    assert not any("tt:submit" in c for c in clicks(s.page))
    assert any(c[0] == "wait_for" and "tt:submit:Post" in c[1] for c in s.page.calls)  # waited for it to be enabled


async def test_options_visibility_ai_and_comments():
    vis = ["Everyone", "Only you"]
    ai = ["on", "off"]
    com = ["on", "off"]
    s = Studio(pre=[("tt:visdrop", vis), ("tt:ctl:AI-generated content:state", ai), ("tt:ctl:Comment:state", com)])
    j = job(options={"visibility": "only_me", "ai_generated": False, "allow_comments": False})
    await tt.publish(s.page, j, dry_run=True)
    cl = clicks(s.page)
    assert any("tt:visdrop" in c for c in cl) and any("tt:visopt" in c and "Only you" in c for c in cl)
    assert any("tt:ctl:AI-generated content:el" in c for c in cl)
    assert any("tt:ctl:Comment:el" in c for c in cl)


async def test_ai_toggle_left_alone_when_already_off():
    s = Studio()
    await tt.publish(s.page, job(), dry_run=True)
    assert not any("tt:ctl:" in c for c in clicks(s.page))


async def test_bad_visibility_rejected():
    with pytest.raises(DriverError) as ei:
        await tt.publish(Studio().page, job(options={"visibility": "public"}))
    assert ei.value.step == "validate"


async def test_missing_file_input_raises_driver_error():
    s = Studio(pre=[("tt:file", False)])
    with pytest.raises(DriverError) as ei:
        await tt.publish(s.page, job())
    assert ei.value.step == "open" and ei.value.screenshot.startswith("tiktok_open")


async def test_missing_editor_raises_driver_error():
    s = Studio(pre=[("tt:editor", False)])
    with pytest.raises(DriverError) as ei:
        await tt.publish(s.page, job())
    assert ei.value.step == "caption" and ei.value.screenshot


async def test_login_prompt():
    s = Studio(pre=[("tt:login", {"url": "https://www.tiktok.com/login", "upload": False, "login": True})])
    with pytest.raises(DriverError) as ei:
        await tt.publish(s.page, job())
    assert ei.value.step == "login"


async def test_check_login():
    r = await tt.check_login(Studio().page)
    assert r["logged_in"] is True and r["account"] == "Nile Beats · نايل بيتس"
    page = FakePage([("tt:login", {"url": "https://www.tiktok.com/login?redirect", "upload": False, "login": True})])
    r = await tt.check_login(page)
    assert r["logged_in"] is False and not page.ops("goto")
    page = FakePage([("tt:login", {"url": "about:blank", "upload": False, "login": False})])  # not on Studio yet
    r = await tt.check_login(page)
    assert r["logged_in"] is False and ("goto", tt.START_URL) in page.calls
