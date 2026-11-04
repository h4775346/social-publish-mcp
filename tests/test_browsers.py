"""Several browsers connected at once: no fights, explicit choice, per-platform routes, account scan."""
from __future__ import annotations

import asyncio
import json

import pytest

from social_publish_mcp.bridge import Bridge, BridgeError
from social_publish_mcp.server import Service

from .conftest import FakeExtension


@pytest.fixture
async def two(bridge: Bridge):
    a = FakeExtension(bridge.port, browser_id="aaaa", label="Work")
    b = FakeExtension(bridge.port, browser_id="bbbb", label="Home")
    b._next_tab = 5000
    await a.start()
    await b.start()
    yield a, b
    await a.stop()
    await b.stop()


async def test_two_browsers_do_not_replace_each_other(bridge: Bridge, two):
    await asyncio.sleep(0.2)
    assert set(bridge.browsers) == {"aaaa", "bbbb"}
    assert all(not c.ws.close_code for c in bridge.browsers.values())


async def test_no_choice_refuses_instead_of_guessing(bridge: Bridge, two):
    assert bridge.roster()["needs_choice"] is True
    with pytest.raises(BridgeError, match="none is chosen"):
        await bridge.open_tab(platform="youtube")
    assert not two[0].open_tabs and not two[1].open_tabs


async def test_default_and_commands_stay_with_their_browser(bridge: Bridge, two):
    a, b = two
    await bridge.choose("home")
    tab = await bridge.open_tab(platform="youtube")
    assert tab in b.open_tabs and tab not in a.open_tabs
    await bridge.request({"type": "cdp", "tabId": tab, "method": "Runtime.evaluate", "params": {"expression": "1"}})
    assert [c for c in b.calls if c["type"] == "cdp"] and not [c for c in a.calls if c["type"] == "cdp"]
    await bridge.close_tab(tab)
    assert not b.open_tabs


async def test_platform_route_beats_default_and_option_beats_route(bridge: Bridge, two):
    a, b = two
    await bridge.choose("Work")
    await bridge.choose("Home", ["tiktok"])
    assert await bridge.open_tab(platform="tiktok") in b.open_tabs
    assert await bridge.open_tab(platform="youtube") in a.open_tabs
    assert await bridge.open_tab(platform="tiktok", browser="work") in a.open_tabs


async def test_chosen_browser_offline_is_an_error_not_a_fallback(bridge: Bridge, two):
    a, b = two
    await bridge.choose("Home")
    await b.stop()
    await asyncio.sleep(0.2)
    with pytest.raises(BridgeError, match="not connected"):
        await bridge.open_tab(platform="youtube")
    assert not a.open_tabs


async def test_one_browser_dropping_keeps_the_others_jobs(bridge: Bridge, two):
    a, b = two
    await bridge.choose("Work")
    keep = await bridge.open_tab()
    await bridge.choose("Home")
    gone = await bridge.open_tab()
    b.silent_methods.add("Page.navigate")
    pending = asyncio.create_task(bridge.request({"type": "cdp", "tabId": gone, "method": "Page.navigate", "params": {}}))
    await asyncio.sleep(0.1)
    await b.stop()
    with pytest.raises(BridgeError, match="disconnected"):
        await pending
    await bridge.request({"type": "cdp", "tabId": keep, "method": "Runtime.evaluate", "params": {"expression": "1"}})


async def test_same_browser_reconnect_swaps_only_its_socket(bridge: Bridge, two):
    a, _ = two
    again = FakeExtension(bridge.port, browser_id="aaaa", label="Work")
    await again.start()
    await asyncio.sleep(0.1)
    assert len(bridge.browsers) == 2 and bridge.browsers["aaaa"].ws is not None
    await again.stop()


async def test_copied_profile_gets_a_new_id_instead_of_fighting(bridge: Bridge):
    socks = [FakeExtension(bridge.port, browser_id="same") for _ in range(4)]
    welcomes = [await s.start() for s in socks]
    ids = [w["browser_id"] for w in welcomes]
    assert ids[0] == ids[1] == ids[2] == "same"
    assert ids[3] != "same"
    for s in socks:
        await s.stop()


async def test_label_and_choice_survive_restart(bridge: Bridge, two):
    await bridge.rename("aaaa", "OPETO profile")
    await bridge.choose("aaaa")
    prefs = json.loads(bridge.prefs_file.read_text(encoding="utf-8"))
    assert prefs["default"] == "aaaa" and prefs["labels"]["aaaa"] == "OPETO profile"


async def test_old_extension_without_id_still_works(bridge: Bridge, ext: FakeExtension):
    assert await bridge.open_tab() in ext.open_tabs
    assert next(iter(bridge.browsers.values())).legacy is True


async def test_scan_routes_platforms_logged_in_to_in_one_browser(bridge: Bridge, two, monkeypatch):
    svc = Service(bridge)
    accounts = {"aaaa": {"youtube": True, "tiktok": True, "facebook": False},
                "bbbb": {"youtube": True, "tiktok": False, "facebook": True}}

    async def fake_check(platforms, *, browser=None):
        return {p: {"logged_in": accounts[browser][p], "account": "x", "detail": ""} for p in platforms}

    monkeypatch.setattr(svc.runner, "check_accounts", fake_check)
    out = await svc.call("use_browser", {"browser": "auto"})
    assert out["routed"] == {"tiktok": "aaaa", "facebook": "bbbb"}
    assert out["ambiguous"] == {"youtube": ["aaaa", "bbbb"]}
    assert bridge.prefs()["routes"] == {"tiktok": "aaaa", "facebook": "bbbb"}


async def test_batch_refused_before_any_tab_when_no_browser_is_chosen(bridge: Bridge, two, tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    svc = Service(bridge)
    with pytest.raises(BridgeError, match="none is chosen"):
        await svc.call("publish_batch", {"jobs": [{"platform": "tiktok", "video": str(video), "caption": "c"}]})
    assert not two[0].calls and not two[1].calls


async def test_same_tab_number_in_two_browsers_reaches_the_right_one(bridge: Bridge):
    a, b = FakeExtension(bridge.port, browser_id="aaaa"), FakeExtension(bridge.port, browser_id="bbbb")
    await a.start()
    await b.start()
    await bridge.choose("aaaa")
    in_a = await bridge.open_tab()
    await bridge.choose("bbbb")
    in_b = await bridge.open_tab()
    assert in_a != in_b  # both browsers answered with the same number
    await bridge.request({"type": "cdp", "tabId": in_b, "method": "Runtime.evaluate", "params": {"expression": "1"}})
    sent = [c for c in b.calls if c["type"] == "cdp"]
    assert sent and sent[0]["tabId"] == next(iter(b.open_tabs))
    assert not [c for c in a.calls if c["type"] == "cdp"]
    await bridge.close_tab(in_b)
    assert not b.open_tabs and a.open_tabs
    await a.stop()
    await b.stop()
