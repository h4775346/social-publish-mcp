from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from social_publish_mcp.drivers import facebook as fb
from social_publish_mcp.models import DriverError, PostJob

from .fakes import FakePage

CAPTION = "حبيبي يا نور العين ❤️🎶\nNew song out now! #NileBeats #أغاني_مصرية done\n\n#music"
PAGE_NAME = "Nile Beats · نايل بيتس"


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def nop(_s):
        return None

    monkeypatch.setattr(fb, "_sleep", nop)


def make_page(readback=CAPTION, **extra):
    rules = [
        ("fb:postto", f"Post to {PAGE_NAME}"),
        ("fb:upload", "done"),
        ("fb:readback", readback),
        ("fb:radiochecked", True),
        ("fb:finish", "processing"),
    ]
    rules = [(k, v) for k, v in extra.get("pre", [])] + rules
    return FakePage(rules)


def job(**kw):
    kw.setdefault("caption", CAPTION)
    return PostJob(platform="facebook", video="clip.mp4", options={"asset_id": "123", "page_name": PAGE_NAME}, **kw)


def typed(page):
    return [c for c in page.calls if c[0] in ("text", "press")]


def plan_as_calls(caption):
    return [("text", op[1]) if op[0] == "text" else ("press", op[1], tuple(op[2:])) for op in fb.caption_plan(caption)]


def test_caption_plan_hashtags_lines_and_unicode():
    ops = fb.caption_plan("أهلا 😀 #NileBeats #حب نهاية\nline2")
    assert ops == [
        ("text", "أهلا 😀 "),
        ("text", "#NileBeats"), ("text", " "), ("key", "Escape"),
        ("text", "#حب"), ("text", " "), ("key", "Escape"),
        ("text", "نهاية"),
        ("key", "Enter", "shift"),
        ("text", "line2"),
    ]
    # typed result (with our extra space) normalises back to the caption
    joined = "".join(o[1] if o[0] == "text" else ("\n" if o[1] == "Enter" else "") for o in ops)
    assert fb._norm(joined) == fb._norm("أهلا 😀 #NileBeats #حب نهاية\nline2")


async def test_publish_now_types_caption_and_shares():
    page = make_page()
    res = await fb.publish(page, job())
    assert res.ok and res.status == "published" and res.platform == "facebook"
    assert ("goto", fb.COMPOSER_URL.format(asset_id="123")) in page.calls
    assert any(c[0] == "set_files" and c[2] == ["clip.mp4"] for c in page.calls)
    assert typed(page) == plan_as_calls(CAPTION)
    # shift+Enter between lines (3 separators for 4 lines), Escape after each of the 3 hashtags
    assert typed(page).count(("press", "Enter", ("shift",))) == 3
    assert typed(page).count(("press", "Escape", ())) == 3
    assert ("text", "حبيبي يا نور العين ❤️🎶") in typed(page)
    clicks = [c[1] for c in page.calls if c[0] == "click"]
    assert sum("fb:next" in s for s in clicks) == 2
    assert any("fb:share" in s for s in clicks) and any("fb:done" in s for s in clicks)
    assert not any("fb:schedule" in s for s in clicks)


async def test_readback_mismatch_retries_once():
    page = make_page(readback=["garbled", CAPTION])
    res = await fb.publish(page, job(), dry_run=True)
    assert res.status == "dry_run"
    t = typed(page)
    assert t.count(("press", "a", ("ctrl",))) == 1 and ("press", "Delete", ()) in t
    assert t == plan_as_calls(CAPTION) + [("press", "a", ("ctrl",)), ("press", "Delete", ())] + plan_as_calls(CAPTION)


async def test_readback_mismatch_twice_fails():
    page = make_page(readback="garbled")
    with pytest.raises(DriverError) as ei:
        await fb.publish(page, job())
    assert ei.value.step == "description" and ei.value.screenshot


async def test_dry_run_stops_before_final_click():
    page = make_page()
    res = await fb.publish(page, job(), dry_run=True)
    assert res.ok and res.status == "dry_run"
    clicks = [c[1] for c in page.calls if c[0] == "click"]
    assert not any("fb:share" in s or "fb:schedule" in s or "fb:done" in s for s in clicks)


async def test_wrong_page_name_fails():
    page = make_page(pre=[("fb:postto", "Post to Someone Else")])
    with pytest.raises(DriverError) as ei:
        await fb.publish(page, job())
    assert ei.value.step == "post_to"


async def test_missing_add_video_raises_driver_error_with_dom_dump(tmp_path):
    page = make_page(pre=[("fb:addvideo", False), ("fb:domdump", {"url": "x", "inputs": [], "controls": [],
                                                                  "headings": ["Create reel"]})])
    page.artifacts_dir = tmp_path
    with pytest.raises(DriverError) as ei:
        await fb.publish(page, job())
    assert ei.value.step == "open_composer"
    assert ei.value.screenshot.startswith("facebook_open_composer")
    dump = tmp_path / "facebook_open_composer_dom.json"
    assert dump.exists() and "Create reel" in dump.read_text(encoding="utf-8")
    assert str(dump) in str(ei.value)


async def test_add_video_hooks_detached_input_and_restores():
    page = make_page()
    await fb.publish(page, job(), dry_run=True)
    seq = [c for c in page.calls if (c[0] == "eval" and ("fb:hookfile" in c[1] or "fb:unhookfile" in c[1]))
           or (c[0] == "click" and "fb:addvideo" in c[1]) or c[0] == "set_files"
           or (c[0] == "wait_for" and "fb:fileready" in c[1])]
    kinds = [c[0] if c[0] != "eval" else ("unhook" if "unhook" in c[1] else "hook") for c in seq]
    assert kinds == ["hook", "click", "wait_for", "set_files", "unhook"]
    assert seq[3][1] == fb.FILE_INPUT and seq[3][2] == ["clip.mp4"]


async def test_add_video_creates_no_input_fails_and_unhooks():
    page = make_page(pre=[("fb:fileready", False)])
    with pytest.raises(DriverError) as ei:
        await fb.publish(page, job())
    assert ei.value.step == "upload" and "did not create a file input" in str(ei.value)
    assert any(c[0] == "eval" and "fb:unhookfile" in c[1] for c in page.calls)


async def test_schedule_sets_and_verifies_values():
    when_utc = (datetime.now(timezone.utc) + timedelta(days=3)).replace(hour=16, minute=5, second=0, microsecond=0)
    local = when_utc.astimezone(timezone(timedelta(hours=2)))  # browser offset -120 -> GMT+2
    want_date = local.strftime("%m/%d/%Y")
    want_time = fb._fmt_time(local)  # 6:05 PM
    page = make_page(pre=[
        ("fb:tzoffset", -120),
        ("fb:dateval", want_date),
        ("fb:timeval", want_time),
        ("fb:dateinput", [False, True]),  # hidden until "Scheduling options" is opened
    ])
    res = await fb.publish(page, job(schedule_at=when_utc))
    assert res.ok and res.status == "scheduled"
    assert res.scheduled_for == local.isoformat()
    assert want_time == "6:05 PM"
    clicks = [c[1] for c in page.calls if c[0] == "click"]
    assert any("fb:schedsegment" in s for s in clicks)  # the "Schedule" segment (verified live)
    assert any(s.endswith("/*fb:schedule*/") for s in clicks)
    assert not any(s.endswith("/*fb:share*/") for s in clicks)
    t = typed(page)
    i = t.index(("text", want_date))
    assert t[i - 1] == ("press", "a", ("ctrl",)) and t[i + 1] == ("press", "Enter", ())
    assert ("text", want_time) in t


async def test_schedule_verify_mismatch_fails():
    when = datetime.now(timezone.utc) + timedelta(days=2)
    page = make_page(pre=[("fb:tzoffset", 0), ("fb:dateval", "01/01/2000"), ("fb:timeval", "1:00 AM")])
    with pytest.raises(DriverError) as ei:
        await fb.publish(page, job(schedule_at=when), dry_run=True)
    assert ei.value.step == "schedule" and "date shows" in str(ei.value)


async def test_remix_disallow_clicks_radio():
    page = make_page(pre=[("fb:radiochecked", [False, True])])
    j = job()
    j.options["allow_remix"] = False
    await fb.publish(page, j, dry_run=True)
    radio_evals = [c[1] for c in page.calls if c[0] in ("exists", "eval") and "fb:radio" in c[1]]
    assert any("Original audio use only" in s for s in radio_evals)
    assert any(c[0] == "click" and "fb:radio" in c[1] for c in page.calls)


async def test_check_login():
    page = FakePage([("fb:login", {"url": fb.START_URL, "loginPage": False, "bs": True, "name": PAGE_NAME,
                                   "asset": "", "home": True})])
    r = await fb.check_login(page)
    assert r == {"logged_in": True, "account": PAGE_NAME, "detail": "Business Suite open"}
    page = FakePage([("fb:login", {"url": "https://www.facebook.com/login", "loginPage": True, "bs": False})])
    r = await fb.check_login(page)
    assert r["logged_in"] is False


# --------------------------------------------------------------------------- 2026-11-01: date entry retries
def test_parse_date_formats():
    assert fb._parse_date("11/02/2026") == (2026, 11, 2)
    assert fb._parse_date("Nov 2, 2026") == (2026, 11, 2)
    assert fb._parse_date(" Nov  1,  2026 ") == (2026, 11, 1)
    assert fb._parse_date("Monday, November 2, 2026") == (2026, 11, 2)
    assert fb._parse_date("2026-11-02") == (2026, 11, 2)
    assert fb._parse_date("") is None and fb._parse_date("tomorrow") is None


def test_date_entry_plan_escalates():
    assert fb.date_entry_plan("11/02/2026", 0) == [("key", "a", "ctrl"), ("text", "11/02/2026"), ("key", "Enter")]
    assert fb.date_entry_plan("11/02/2026", 1)[-1] == ("key", "Tab")
    assert ("keys", "11/02/2026") in fb.date_entry_plan("11/02/2026", 2)


def _sched_page(datevals):
    when_utc = (datetime.now(timezone.utc) + timedelta(days=3)).replace(hour=16, minute=5, second=0, microsecond=0)
    local = when_utc.astimezone(timezone(timedelta(hours=2)))
    page = make_page(pre=[
        ("fb:tzoffset", -120),
        ("fb:dateval", datevals(local)),
        ("fb:timeval", fb._fmt_time(local)),
    ])
    return page, when_utc, local


async def test_schedule_date_retried_until_it_sticks():
    # live failure: the field kept today's default; the second try works
    page, when, local = _sched_page(lambda d: ["Nov 1, 2026", d.strftime("%b %d, %Y").replace(" 0", " "), "x"][:2])
    res = await fb.publish(page, job(schedule_at=when), dry_run=True)
    assert res.ok and res.scheduled_for == local.isoformat()
    date_clicks = [c for c in page.calls if c[0] == "click" and "fb:dateinput" in str(c[1])]
    assert len(date_clicks) == 2
    assert page.calls.count(("bring_to_front",)) >= 2
    assert ("press", "Tab", ()) in page.calls  # second try also blurs with Tab


async def test_schedule_date_fails_after_three_tries():
    page, when, _ = _sched_page(lambda d: "Nov 1, 2026")
    with pytest.raises(DriverError) as ei:
        await fb.publish(page, job(schedule_at=when), dry_run=True)
    assert ei.value.step == "schedule" and "after 3 tries" in str(ei.value) and "Nov 1, 2026" in str(ei.value)
    assert len([c for c in page.calls if c[0] == "click" and "fb:dateinput" in str(c[1])]) == 3
    assert any(c[0] == "type_keys" and "/" in c[1] for c in page.calls)  # last try types key events
