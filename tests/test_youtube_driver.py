from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

import pytest

from social_publish_mcp.drivers import youtube as yt
from social_publish_mcp.models import DriverError, PostJob

from .fakes import FakePage

CID = "UC0123456789abcdefABCD_-"
CAIRO = timezone(timedelta(hours=2))
SHORT = "https://youtube.com/shorts/AbCdEfGhIj0"


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def nop(_s):
        return None

    monkeypatch.setattr(yt, "_sleep", nop)


def _fill_echo(js: str) -> str:
    """JS_FILL ends with (index, "json text"): echo the text back as the textbox's innerText."""
    return json.loads(re.search(r"\)\(\d+, (\".*\")\)$", js, re.S).group(1))


def make_page(*, existing_tags=("nilebeats",), playlists=("Arabic songs", "Shorts 2026"), tz_text="(GMT+0200) Cairo",
              date_text="", time_value="", done_label="Publish", result=None, pre=(), commit_on=","):
    """commit_on: the key that turns typed tag text into a chip ("," = comma key event, "Enter", or None = never)."""
    page = FakePage()

    def committed_tags() -> list[str]:
        out, pending = [], None
        for c in page.calls:
            if c[0] == "text":
                pending = c[1]
            elif pending is not None and ((c[0] == "type_keys" and c[1] == commit_on)
                                         or (c[0] == "press" and c[1] == commit_on)):
                out.append(pending)
                pending = None
        return out

    def tags(_js):
        chips = list(existing_tags) + committed_tags()
        return {"chips": chips, "text": " ".join(chips)}

    def clicked_box(name: str) -> bool:
        return any(c[0] == "click" and isinstance(c[1], str) and "yt:pl-box" in c[1]
                   and c[1].endswith(f"({json.dumps(name, ensure_ascii=False)})") for c in page.calls)

    def arg_name(js: str) -> str:
        return json.loads(re.search(r"\}\)\((\".*\")\)$", js, re.S).group(1))

    def pl_text(_js):
        names = [n for n in playlists if clicked_box(n)]
        return ", ".join(names) or "Select"

    page.rules = list(pre) + [
        ("yt:dialog", "ok"),
        ("yt:fill", _fill_echo),
        ("yt:visible", False),
        ("yt:tag-input", ""),
        ("yt:tags", tags),
        ("yt:popup", None),
        ("yt:neutral", None),
        ("yt:pl-items", {"names": list(playlists), "checked": [], "empty": False}),
        ("yt:pl-checked", lambda js: clicked_box(arg_name(js))),
        ("yt:checked", True),
        ("yt:pl-box", lambda js: arg_name(js) in playlists),
        ("yt:pl-text", pl_text),
        ("yt:links", [[SHORT]]),  # one list per call (a bare list is a call sequence)
        ("yt:tz", tz_text),
        ("yt:date-text", date_text),
        ("yt:time-value", time_value),
        ("yt:time-fill", time_value),
        ("yt:upload", "done"),
        ("yt:progress", "Uploading 40% ..."),
        ("yt:done-state", {"label": done_label, "visible": True, "enabled": True}),
        ("yt:result", result or {"heading": "Video published", "text": "Video published " + SHORT, "links": [SHORT]}),
    ]
    return page


def job(**kw):
    opts = {"channel_id": CID, "playlists": ["Shorts 2026"]}
    opts.update(kw.pop("options", {}))
    kw.setdefault("title", "Habibi ya Nour")
    kw.setdefault("caption", "حبيبي يا نور العين ❤️\nNew song #NileBeats")
    kw.setdefault("tags", ["NileBeats", "arabic pop", "#egypt"])
    return PostJob(platform="youtube", video="clip.mp4", options=opts, **kw)


def clicks(page):
    return [c[1] for c in page.calls if c[0] == "click"]


# --------------------------------------------------------------------------- pure helpers
def test_title_fallback_and_truncation():
    assert yt.make_title("  My  <Song>  ", "x") == "My Song"
    assert yt.make_title("", "\n\n  First line here \nsecond") == "First line here"
    long = " ".join(["word"] * 40)  # 199 chars
    t = yt.make_title("", long)
    assert len(t) <= 100 and t.endswith("word") and t == long[: len(t)]
    nospace = "x" * 150
    assert yt.make_title(nospace, "") == "x" * 100
    assert yt.make_description("a <b> c" + "z" * 6000).startswith("a b c") and len(yt.make_description("z" * 6000)) == 5000


def test_tag_dedup_and_budget():
    assert yt.plan_tags(["NileBeats"], ["nilebeats", "#Arabic", "arabic", "a,b", " "]) == ["Arabic", "a", "b"]
    # budget: existing 'x'*490 (490) + ',' + 'abc' (3) = 494; 'with space' costs 10+2+1 > 6 left -> skipped; 'de' fits
    assert yt.plan_tags(["x" * 490], ["abc", "with space", "de"]) == ["abc", "de"]
    assert yt.plan_tags([], ["y" * 501]) == []
    assert yt.plan_tags([], ["y" * 500]) == ["y" * 500]


def test_gmt_offset_and_time_text():
    assert yt.parse_gmt_offset("Time zone (GMT+0200) Cairo") == timezone(timedelta(hours=2))
    assert yt.parse_gmt_offset("GMT-5:30") == timezone(-timedelta(hours=5, minutes=30))
    assert yt.parse_gmt_offset("GMT+2 Cairo") == timezone(timedelta(hours=2))
    assert yt.parse_gmt_offset("(GMT) London") == timezone.utc
    assert yt.parse_gmt_offset("no zone here") is None
    assert yt.time_text(datetime(2030, 1, 1, 0, 5)) == "12:05 AM"
    assert yt.time_text(datetime(2030, 1, 1, 12, 0)) == "12:00 PM"
    assert yt.time_text(datetime(2030, 1, 1, 17, 0)) == "5:00 PM"
    assert yt.date_matches("Apr 1, 2030", datetime(2030, 4, 1))
    assert not yt.date_matches("Apr 11, 2030", datetime(2030, 4, 1))
    assert yt.parse_confirmed("will be set to public on Apr 1, 2030 at 1:30 AM", CAIRO) == "2030-04-01T01:30:00+02:00"
    assert yt.extract_video(["https://studio.youtube.com/x", "https://youtu.be/AbCdEfGhIj0"]) == (
        "https://youtu.be/AbCdEfGhIj0", "AbCdEfGhIj0")


def test_options_validation():
    with pytest.raises(DriverError) as e:
        yt.read_options(job(options={"channel_id": ""}))
    assert e.value.step == "options"
    with pytest.raises(DriverError):
        yt.read_options(job(options={"visibility": "secret"}))
    with pytest.raises(DriverError):
        yt.read_options(job(options={"visibility": "private"}, schedule_at=datetime(2030, 1, 1, tzinfo=CAIRO)))


# --------------------------------------------------------------------------- publish now
async def test_publish_now_step_order():
    page = make_page()
    r = await yt.publish(page, job())
    assert r.ok and r.status == "published" and r.url == SHORT and r.post_id == "AbCdEfGhIj0"

    gotos = [c[1] for c in page.calls if c[0] == "goto"]
    assert gotos == [f"https://studio.youtube.com/channel/{CID}/content",
                     f"https://studio.youtube.com/channel/{CID}/videos/upload?d=ud"]
    assert ("set_files", yt.FILE_INPUT, ["clip.mp4"]) in page.calls

    fills = [_fill_echo(c[1]) for c in page.calls if c[0] == "eval" and "yt:fill" in c[1]]
    assert fills == ["Habibi ya Nour", "حبيبي يا نور العين ❤️\nNew song #NileBeats"]

    typed = [c[1] for c in page.calls if c[0] == "text"]
    assert typed == ["arabic pop", "egypt"]  # one tag at a time; 'NileBeats' already present as 'nilebeats'
    keys = [c for c in page.calls if c[0] in ("text", "type_keys")]
    assert keys == [("text", "arabic pop"), ("type_keys", ","), ("text", "egypt"), ("type_keys", ",")]
    assert "tags not committed" not in r.message

    cl = clicks(page)
    order = [
        cl.index(yt.KIDS_RADIO.format("NOT_MFK")),
        cl.index(yt.SHOW_MORE),
        cl.index(yt.ALTERED_RADIO.format("NO")),
        cl.index(yt.TAGS_INPUT),
        cl.index(yt.PLAYLIST_TRIGGER),
        next(i for i, s in enumerate(cl) if "yt:pl-box" in s and "Shorts 2026" in s),
        next(i for i, s in enumerate(cl) if "yt:pl-done" in s),
        cl.index(yt.NEXT),
        cl.index(yt.VIS_RADIO.format("PUBLIC")),
        cl.index(yt.DONE),
    ]
    assert order == sorted(order)
    assert cl.count(yt.NEXT) == 3
    assert cl[-1] == yt.DONE
    # upload finished was checked before the final click
    i_upload = next(i for i, c in enumerate(page.calls) if "yt:upload" in str(c[1]))
    i_done = page.calls.index(("click", yt.DONE))
    assert i_upload < i_done


async def test_dry_run_stops_before_final_click():
    page = make_page()
    r = await yt.publish(page, job(), dry_run=True)
    assert r.ok and r.status == "dry_run" and r.screenshot
    assert yt.DONE not in clicks(page)
    assert not any("yt:upload" in str(c[1]) or "yt:result" in str(c[1]) for c in page.calls)
    assert "Uploading 40%" in r.message


async def test_missing_element_raises_driver_error():
    page = make_page(pre=[(yt.KIDS_RADIO.format("NOT_MFK"), False)])
    with pytest.raises(DriverError) as e:
        await yt.publish(page, job())
    assert e.value.step == "made_for_kids" and e.value.screenshot.startswith("youtube_made_for_kids")
    assert yt.DONE not in clicks(page)


async def test_wrong_channel_permission_page():
    page = make_page(pre=[("yt:dialog", "denied")])
    with pytest.raises(DriverError) as e:
        await yt.publish(page, job())
    assert e.value.step == "open_upload_dialog" and "permission" in str(e.value)
    assert not any(c[0] == "set_files" for c in page.calls)


async def test_missing_playlist_lists_available():
    page = make_page()
    with pytest.raises(DriverError) as e:
        await yt.publish(page, job(options={"playlists": ["Nope"]}))
    assert e.value.step == "playlists" and "Arabic songs" in str(e.value)


# --------------------------------------------------------------------------- schedule
async def test_video_processing_dialog_counts_as_scheduled():
    # YouTube accepts the post before SD is ready: heading "Video processing", no share link in the dialog
    when = datetime(2030, 3, 31, 23, 30, tzinfo=timezone.utc)
    text = "Video processing\nYour video will be set to public on April 1, 2030 at 1:30 AM after the standard definition"
    page = make_page(date_text="Apr 1, 2030", time_value="1:30 AM", done_label="Schedule",
                     result={"heading": "Video processing", "text": text, "links": [SHORT]})
    r = await yt.publish(page, job(schedule_at=when))
    assert r.ok and r.status == "scheduled" and r.scheduled_for == "2030-04-01T01:30:00+02:00"
    assert "processing" in yt.JS_RESULT and "will be (set to" in yt.JS_RESULT


async def test_schedule_converts_to_studio_zone():
    when = datetime(2030, 3, 31, 23, 30, tzinfo=timezone.utc)  # = Apr 1, 01:30 in GMT+2
    text = "Video scheduled\nYour video will be set to public on Apr 1, 2030 at 1:30 AM\n" + SHORT
    page = make_page(date_text="Apr 1, 2030", time_value="1:30 AM", done_label="Schedule",
                     result={"heading": "Video scheduled", "text": text, "links": [SHORT]})
    r = await yt.publish(page, job(schedule_at=when))
    assert r.status == "scheduled" and r.scheduled_for == "2030-04-01T01:30:00+02:00"

    cl = clicks(page)
    day = next(s for s in cl if "yt:day" in s)
    assert day.endswith('(2030, "Apr", 1)')
    assert cl.index(yt.SCHEDULE_EXPAND) < cl.index(yt.DATE_TRIGGER) < cl.index(day)
    assert yt.VIS_RADIO.format("PUBLIC") not in cl
    fill = next(c[1] for c in page.calls if c[0] == "eval" and "yt:time-fill" in c[1])
    assert fill.endswith('("1:30 AM")')
    assert cl[-1] == yt.DONE


async def test_schedule_falls_back_to_schedule_at_zone():
    est = timezone(timedelta(hours=-5))
    when = datetime(2030, 6, 10, 17, 0, tzinfo=est)
    page = make_page(tz_text="no zone label", date_text="Jun 10, 2030", time_value="5:00 PM", done_label="Schedule")
    r = await yt.publish(page, job(schedule_at=when), dry_run=True)
    assert r.status == "dry_run" and r.scheduled_for == "2030-06-10T17:00:00-05:00"
    assert "schedule_at" in r.message
    assert next(s for s in clicks(page) if "yt:day" in s).endswith('(2030, "Jun", 10)')


async def test_schedule_wrong_time_shown_fails():
    when = datetime(2030, 6, 10, 15, 0, tzinfo=timezone.utc)
    page = make_page(date_text="Jun 10, 2030", time_value="5:15 PM", done_label="Schedule")
    with pytest.raises(DriverError) as e:
        await yt.publish(page, job(schedule_at=when))
    assert e.value.step == "schedule_time" and "5:00 PM" in str(e.value)
    assert yt.DONE not in clicks(page)


async def test_schedule_button_label_checked():
    when = datetime(2030, 6, 10, 15, 0, tzinfo=timezone.utc)
    page = make_page(date_text="Jun 10, 2030", time_value="5:00 PM", done_label="Publish")
    with pytest.raises(DriverError) as e:
        await yt.publish(page, job(schedule_at=when))
    assert e.value.step == "final_click"


# --------------------------------------------------------------------------- login
async def test_check_login_states():
    ok = FakePage([("yt:login", {"state": "ok", "channel_id": CID, "name": "NileBeats", "href": "x"})])
    r = await yt.check_login(ok)
    assert r["logged_in"] and r["account"] == f"NileBeats ({CID})"
    assert ok.calls[0] == ("goto", yt.START_URL)

    out = await yt.check_login(FakePage([("yt:login", {"state": "signed_out"})]))
    assert not out["logged_in"] and "sign-in" in out["detail"]

    stuck = await yt.check_login(FakePage([("yt:login", None)]))
    assert not stuck["logged_in"]


# --------------------------------------------------------------------------- 2026-11-01 fixes
OPETO_SHORTS = "OPETO Shorts · شورتس أغاني أوبيتو | Arabic Song Shorts"
ALTERED = ["Make a real person appear to say or do something they didn’t say or do",
           "Alter footage of a real event or place"]


def test_split_tags_handles_commas_and_arabic_comma():
    assert yt.split_tags(["رونالدو,كريستيانو رونالدو", "الدون، أهداف رونالدو", " #شورتس ", ""]) == [
        "رونالدو", "كريستيانو رونالدو", "الدون", "أهداف رونالدو", "شورتس"]
    assert yt.split_tags("a, b ,,c") == ["a", "b", "c"]
    assert yt.plan_tags([], "x،y") == ["x", "y"]


def test_tag_committed_needs_an_exact_chip():
    assert yt.tag_committed("Arabic Pop", ["arabic  pop"])
    merged = "رونالدو,كريستيانو رونالدو,الدون"
    assert not yt.tag_committed("رونالدو", [merged])
    assert yt.tag_committed("الدون", ["OPETO", "الدون"])


def test_match_playlist_prefix_contains_and_ambiguity():
    names = ["Arabic songs", OPETO_SHORTS, "OPETO Music videos"]
    assert yt.match_playlist("OPETO Shorts", names) == 1
    assert yt.match_playlist("opeto shorts", names) == 1  # case-insensitive
    assert yt.match_playlist("Arabic Song Shorts", names) == 1  # contains
    assert yt.match_playlist("arabic songs", names) == 0  # exact beats the contains-hit in row 1
    assert yt.match_playlist("Nope", names) is None
    assert yt.match_playlist("", names) is None
    with pytest.raises(yt.AmbiguousPlaylist):
        yt.match_playlist("OPETO", names)


def test_extract_video_prefers_shorts_link():
    assert yt.extract_video(["https://youtu.be/AbCdEfGhIj0", SHORT]) == (SHORT, "AbCdEfGhIj0")
    assert yt.extract_video(["https://studio.youtube.com/video/x/edit"]) == ("", "")


async def test_playlist_matched_by_prefix_in_picker_only():
    page = make_page(playlists=("Arabic songs", OPETO_SHORTS))
    r = await yt.publish(page, job(options={"playlists": ["OPETO Shorts"]}))
    assert r.ok
    boxes = [s for s in clicks(page) if "yt:pl-box" in s]
    assert len(boxes) == 1 and boxes[0].endswith(f"({json.dumps(OPETO_SHORTS, ensure_ascii=False)})")
    assert OPETO_SHORTS in r.message
    # rows come from the picker popup (yt:pl-items), never from generic <li>s of the upload dialog
    items = next(c[1] for c in page.calls if "yt:pl-items" in str(c[1]))
    assert "ytcp-playlist-dialog" in items and "Create playlist" in items and "tp-yt-paper-dialog li" not in items


async def test_playlist_waits_for_loading_list():
    loading = {"names": [], "checked": [], "empty": True}
    page = make_page(pre=[("yt:pl-items", [loading, loading, {"names": ["Shorts 2026"], "checked": [], "empty": False}])])
    r = await yt.publish(page, job())
    assert r.ok and any("yt:pl-box" in s for s in clicks(page))


async def test_playlist_picker_never_loads_fails_clearly():
    page = make_page(pre=[("yt:pl-items", {"names": [], "checked": [], "empty": True})])
    with pytest.raises(DriverError) as e:
        await yt.publish(page, job())
    assert e.value.step == "playlists" and "No playlists available" in str(e.value)
    assert yt.DONE not in clicks(page)


async def test_playlist_not_found_lists_picker_rows():
    page = make_page(playlists=("Arabic songs", OPETO_SHORTS))
    with pytest.raises(DriverError) as e:
        await yt.publish(page, job(options={"playlists": ["Nope"]}))
    assert OPETO_SHORTS in str(e.value) and ALTERED[0] not in str(e.value)


async def test_tags_uncommitted_are_a_warning_not_a_failure():
    page = make_page(commit_on=None, pre=[("yt:tag-input", "رونالدو")])
    r = await yt.publish(page, job(tags=["رونالدو", "الدون"]))
    assert r.ok and r.status == "published"
    assert "tags not committed: ['رونالدو', 'الدون']" in r.message
    presses = [c for c in page.calls if c[0] == "press"]
    assert ("press", "Enter", ()) in presses  # fallback after the comma key
    assert presses.count(("press", "Backspace", ())) == 2  # leftover text cleared before the next tag


async def test_tags_committed_with_enter_fallback():
    page = make_page(commit_on="Enter", pre=[("yt:tag-input", ["رونالدو", "", "الدون", ""])])
    r = await yt.publish(page, job(tags=["رونالدو", "الدون"]))
    assert r.ok and "tags not committed" not in r.message


async def test_open_time_list_is_closed_before_final_click():
    when = datetime(2030, 6, 10, 15, 0, tzinfo=timezone.utc)
    text = "Video scheduled\nYour video will be set to public on Jun 10, 2030 at 5:00 PM\n" + SHORT
    page = make_page(date_text="Jun 10, 2030", time_value="5:00 PM", done_label="Schedule",
                     result={"heading": "Video scheduled", "text": text, "links": [SHORT]},
                     pre=[("yt:popup", [["12:00 AM / 12:15 AM"], None])])
    r = await yt.publish(page, job(schedule_at=when))
    assert r.status == "scheduled" and r.url == SHORT and r.post_id == "AbCdEfGhIj0"
    assert "video id AbCdEfGhIj0" in r.message
    cl = clicks(page)
    assert cl.index(yt.NEUTRAL) < cl.index(yt.DONE)
    assert cl.count(yt.DONE) == 1
    assert ("bring_to_front",) in page.calls


async def test_escape_when_neutral_click_does_not_close_the_list():
    when = datetime(2030, 6, 10, 15, 0, tzinfo=timezone.utc)
    page = make_page(date_text="Jun 10, 2030", time_value="5:00 PM", done_label="Schedule",
                     pre=[("yt:popup", [["list"], ["list"], None])])
    await yt.publish(page, job(schedule_at=when), dry_run=True)
    assert ("press", "Escape", ()) in page.calls


async def test_final_click_waits_until_enabled():
    off = {"label": "Publish", "visible": True, "enabled": False}
    on = {"label": "Publish", "visible": True, "enabled": True}
    page = make_page(pre=[("yt:done-state", [off, off, on])])
    r = await yt.publish(page, job())
    assert r.ok
    states = [i for i, c in enumerate(page.calls) if len(c) > 1 and "yt:done-state" in str(c[1])]
    assert len(states) >= 3 and states[2] < page.calls.index(("click", yt.DONE))


async def test_swallowed_final_click_is_retried():
    ok = {"heading": "Video published", "text": "Video published", "links": [SHORT]}
    page = make_page(pre=[("yt:result", [None] * yt.RECLICK_EVERY + [ok])])
    r = await yt.publish(page, job())
    assert r.ok and r.url == SHORT
    assert clicks(page).count(yt.DONE) == 2


async def test_result_timeout_reports_clicks_and_state():
    page = make_page(pre=[("yt:result", None)])
    with pytest.raises(DriverError) as e:
        await yt.publish(page, job())
    assert e.value.step == "result" and f"{yt.FINAL_CLICKS} click(s)" in str(e.value)
    assert clicks(page).count(yt.DONE) == yt.FINAL_CLICKS
    assert e.value.screenshot


async def test_result_link_falls_back_to_details_link():
    page = make_page(result={"heading": "Video published", "text": "Video published", "links": []})
    r = await yt.publish(page, job())
    assert r.url == SHORT and r.post_id == "AbCdEfGhIj0"


async def test_result_dom_dump_written_on_failure(tmp_path):
    page = make_page(pre=[("yt:result", None), ("yt:dom-dump", {"url": "x", "dialogs": []})])
    page.artifacts_dir = tmp_path
    with pytest.raises(DriverError) as e:
        await yt.publish(page, job())
    assert "dom dump:" in str(e.value) and (tmp_path / "youtube_result_dom.json").exists()
