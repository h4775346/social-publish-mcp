from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from social_publish_mcp import drivers
from social_publish_mcp.bridge import Bridge
from social_publish_mcp.models import DriverError, PostJob, PostResult
from social_publish_mcp.runner import Runner, ValidationError, job_from_dict, parse_time, validate
from social_publish_mcp.server import Service

from .conftest import FakeExtension


def later(**kw) -> datetime:
    return datetime.now(timezone.utc) + timedelta(**kw)


class FakeDrivers:
    """A driver module per platform; records concurrency."""

    def __init__(self, delay: float = 0.3, fail: set[str] | None = None, crash: set[str] | None = None) -> None:
        self.delay, self.fail, self.crash = delay, fail or set(), crash or set()
        self.active = 0
        self.max_active = 0
        self.per_platform_max: dict[str, int] = {}
        self._per: dict[str, int] = {}

    def get(self, platform: str):
        async def publish(page, job: PostJob, *, dry_run: bool = False) -> PostResult:
            self.active += 1
            self._per[platform] = self._per.get(platform, 0) + 1
            self.max_active = max(self.max_active, self.active)
            self.per_platform_max[platform] = max(self.per_platform_max.get(platform, 0), self._per[platform])
            try:
                await page.click("#upload")
                await asyncio.sleep(self.delay)
                if platform in self.fail:
                    raise DriverError("set file", "no file input on screen")
                if platform in self.crash:
                    raise KeyError("oops")
                status = "dry_run" if dry_run else ("scheduled" if job.schedule_at else "published")
                return PostResult(platform, ok=True, status=status, url=f"https://{platform}.test/p/1")  # type: ignore[arg-type]
            finally:
                self.active -= 1
                self._per[platform] -= 1

        async def check_login(page) -> dict:
            return {"logged_in": True, "account": f"{platform}-acct"}

        return SimpleNamespace(START_URL=f"https://{platform}.test/upload", publish=publish, check_login=check_login)


@pytest.fixture
def fake(monkeypatch) -> FakeDrivers:
    f = FakeDrivers()
    monkeypatch.setattr(drivers, "get", f.get)
    return f


def job(platform: str, video: Path, **kw) -> PostJob:
    base = {"caption": "hello #tag", "title": "A title" if platform == "youtube" else ""}
    return PostJob(platform=platform, video=video, **{**base, **kw})  # type: ignore[arg-type]


async def test_parallel_batch_one_tab_per_job(bridge: Bridge, ext: FakeExtension, fake: FakeDrivers, video: Path):
    runner = Runner(bridge)
    t0 = time.monotonic()
    results = await runner.run_batch([job(p, video) for p in ("youtube", "facebook", "tiktok")], max_parallel=3)
    elapsed = time.monotonic() - t0
    assert [r.ok for r in results] == [True, True, True]
    assert [r.platform for r in results] == ["youtube", "facebook", "tiktok"]
    assert fake.max_active == 3 and elapsed < 2.5  # sequential would be ~3.5 s (goto + click + delay per job)
    assert len([c for c in ext.calls if c["type"] == "open_tab"]) == 3
    assert ext.open_tabs == set()  # every tab closed
    navs = [c["params"]["url"] for c in ext.calls if c.get("method") == "Page.navigate"]
    assert sorted(navs) == sorted(f"https://{p}.test/upload" for p in ("youtube", "facebook", "tiktok"))
    rows = [json.loads(x) for x in runner.history_file.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 3 and all(r["ok"] for r in rows)
    assert runner.history(2) == rows[-2:]


async def test_same_platform_jobs_run_one_at_a_time(bridge: Bridge, ext: FakeExtension, fake: FakeDrivers, video: Path):
    jobs = [job("tiktok", video, schedule_at=later(hours=h)) for h in (1, 2, 3)]
    results = await Runner(bridge).run_batch(jobs, max_parallel=3)
    assert all(r.status == "scheduled" for r in results)
    assert fake.per_platform_max["tiktok"] == 1


async def test_max_parallel_is_respected(bridge: Bridge, ext: FakeExtension, fake: FakeDrivers, video: Path):
    await Runner(bridge).run_batch([job(p, video) for p in ("youtube", "facebook", "tiktok")], max_parallel=1)
    assert fake.max_active == 1


async def test_driver_error_becomes_failed_result(bridge: Bridge, ext: FakeExtension, fake: FakeDrivers, video: Path):
    fake.fail.add("facebook")
    fake.crash.add("tiktok")
    runner = Runner(bridge)
    yt, fb, tt = await runner.run_batch([job(p, video) for p in ("youtube", "facebook", "tiktok")])
    assert yt.ok
    assert not fb.ok and fb.status == "failed" and "set file" in fb.message
    assert Path(fb.screenshot).is_file() and fb.screenshot.endswith("failure.png")
    assert "artifacts" in fb.screenshot
    assert not tt.ok and "KeyError" in tt.message
    assert ext.open_tabs == set()
    rows = [json.loads(x) for x in runner.history_file.read_text(encoding="utf-8").splitlines()]
    assert [r["ok"] for r in rows if r["platform"] == "facebook"] == [False]


async def test_timeout_becomes_failed_result(bridge: Bridge, ext: FakeExtension, fake: FakeDrivers, video: Path):
    fake.delay = 5
    r = await Runner(bridge).run_job(job("youtube", video), timeout=0.3)
    assert not r.ok and "timeout" in r.message


async def test_detached_tab_fails_job_without_hanging(bridge: Bridge, ext: FakeExtension, fake: FakeDrivers, video: Path):
    ext.silent_methods.add("Input.dispatchMouseEvent")
    task = asyncio.create_task(Runner(bridge).run_job(job("youtube", video)))
    while not any(c.get("method") == "Input.dispatchMouseEvent" for c in ext.calls):
        await asyncio.sleep(0.05)
    tab = next(c["tabId"] for c in ext.calls if c["type"] == "cdp")
    await ext.event("detached", tab, "canceled_by_user")
    r = await asyncio.wait_for(task, 5)
    assert not r.ok and "detached" in r.message


async def test_dry_run_and_keep_tab(bridge: Bridge, ext: FakeExtension, fake: FakeDrivers, video: Path):
    r = await Runner(bridge).run_job(job("tiktok", video), dry_run=True, keep_tab=True)
    assert r.status == "dry_run"
    assert len(ext.open_tabs) == 1


async def test_check_accounts(bridge: Bridge, ext: FakeExtension, fake: FakeDrivers):
    out = await Runner(bridge).check_accounts(["youtube", "tiktok", "myspace"])
    assert out["youtube"] == {"logged_in": True, "account": "youtube-acct", "detail": ""}
    assert out["tiktok"]["logged_in"] and not out["myspace"]["logged_in"]
    assert ext.open_tabs == set()


# ------------------------------------------------------------ validation
def test_validation_problems(tmp_path: Path, video: Path):
    assert validate(job("youtube", video)) == []
    assert any("not found" in p for p in validate(job("tiktok", tmp_path / "missing.mp4")))
    assert any("title" in p for p in validate(job("youtube", video, title="x" * 101)))
    assert any("needs a title" in p for p in validate(job("youtube", video, title="")))
    assert any("'<'" in p for p in validate(job("youtube", video, caption="a <b>")))
    assert any("5000" in p for p in validate(job("youtube", video, caption="x" * 5001)))
    assert any("2200" in p for p in validate(job("facebook", video, caption="x" * 2201)))
    assert any("4000" in p for p in validate(job("tiktok", video, caption="x" * 4001)))
    assert validate(job("tiktok", video, caption="x" * 4000)) == []
    assert any("future" in p for p in validate(job("facebook", video, schedule_at=later(minutes=-5))))
    assert any("10 days" in p for p in validate(job("tiktok", video, schedule_at=later(days=11))))
    assert validate(job("youtube", video, schedule_at=later(days=60))) == []
    assert any("visibility" in p for p in validate(job("tiktok", video, options={"visibility": "public"})))


def test_parse_time():
    assert parse_time("2026-11-01T17:00+02:00").utcoffset() == timedelta(hours=2)
    assert parse_time("2026-11-01T15:00:00Z").utcoffset() == timedelta(0)
    assert parse_time(None) is None
    with pytest.raises(ValueError, match="offset"):
        parse_time("2026-11-01T17:00")
    with pytest.raises(ValueError, match="ISO"):
        parse_time("next friday")


def test_job_from_dict(tmp_path: Path, video: Path):
    cap = tmp_path / "c.txt"
    cap.write_text("from file #x\n", encoding="utf-8")
    j = job_from_dict({"platform": "TikTok", "video": str(video), "caption_file": str(cap), "tags": "a, b"})
    assert j.platform == "tiktok" and j.caption == "from file #x" and j.tags == ["a", "b"]
    with pytest.raises(ValueError, match="platform"):
        job_from_dict({"platform": "myspace", "video": str(video)})
    with pytest.raises(ValueError, match="unknown job field"):
        job_from_dict({"platform": "tiktok", "video": str(video), "captoin": "typo"})


async def test_invalid_batch_runs_nothing(bridge: Bridge, ext: FakeExtension, fake: FakeDrivers, video: Path, tmp_path: Path):
    jobs = [job("youtube", video), job("tiktok", tmp_path / "nope.mp4"), job("youtube", video)]
    with pytest.raises(ValidationError) as e:
        await Runner(bridge).run_batch(jobs)
    assert any("not found" in p for p in e.value.problems)
    assert any("twice" in p for p in e.value.problems)
    assert ext.calls == []


# ------------------------------------------------------------ service (tools/CLI layer), incl. peer forwarding
async def test_service_publish_and_peer_forwarding(bridge: Bridge, ext: FakeExtension, fake: FakeDrivers, video: Path):
    hub = Service(bridge)
    peer = Service(Bridge(port=bridge.port, home=bridge.home))
    assert await peer.start() == "peer"
    jobs = [{"platform": "youtube", "video": str(video), "title": "T", "caption": "c"},
            {"platform": "facebook", "video": str(video), "caption": "c", "schedule_at": later(hours=2).isoformat()}]
    results = await peer.call("publish_batch", {"jobs": jobs, "max_parallel": 2})
    assert [r["status"] for r in results] == ["published", "scheduled"]
    st = await peer.call("status", {})
    assert st["extension_connected"] and st["last_results"][-1]["platform"] == "facebook"
    assert hub.runner.running == {}
