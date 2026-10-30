"""Job runner: validates PostJobs, runs each in its own tab through a platform driver, records the results.

One job = one tab = one ``Page``. ``run_batch`` runs jobs in parallel (``max_parallel`` tabs at once) but at most
``per_platform`` jobs of the same platform at a time (default 1: one video to three platforms goes out in
parallel, a week of YouTube posts goes out one after another — human pace, and no two upload dialogs fighting over
one account). Every result, failed or not, is appended to ``<home>/history.jsonl``.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import drivers
from .bridge import Bridge, BridgeError, ExtensionTransport, log
from .models import PLATFORMS, DriverError, PostJob, PostResult
from .page import Page, PageError

JOB_TIMEOUT = 30 * 60.0  # whole flow incl. upload of a big file
CHECK_TIMEOUT = 90.0
SCREENSHOT_TIMEOUT = 15.0

# platform text limits (characters)
LIMITS = {
    "youtube": {"title": 100, "caption": 5000, "tags": 500},
    "tiktok": {"caption": 4000},
    "facebook": {"caption": 2200},
}
# how far ahead each platform accepts a scheduled time (UI limits observed / documented)
SCHEDULE_WINDOW: dict[str, tuple[timedelta, timedelta | None]] = {
    "youtube": (timedelta(minutes=15), None),
    "tiktok": (timedelta(minutes=15), timedelta(days=10)),
    "facebook": (timedelta(minutes=20), timedelta(days=29)),
}
OPTION_VALUES = {
    ("youtube", "visibility"): ("public", "unlisted", "private"),
    ("tiktok", "visibility"): ("everyone", "friends", "only_me"),
}


class ValidationError(ValueError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


# ------------------------------------------------------------ parsing + validation
def parse_time(value: str | datetime | None) -> datetime | None:
    """ISO 8601 with an offset ("2026-11-01T17:00+02:00", "...Z") -> aware datetime. Naive times are refused."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        s = str(value).strip()
        if s.endswith(("Z", "z")):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            raise ValueError(f"schedule_at {value!r} is not ISO 8601 (example: 2026-11-01T17:00+02:00)") from None
    if dt.tzinfo is None:
        raise ValueError(f"schedule_at {value!r} has no UTC offset; write it like 2026-11-01T17:00+02:00")
    return dt


def job_from_dict(d: dict[str, Any]) -> PostJob:
    """Build a PostJob from tool/CLI/JSON input. Accepts caption_file instead of caption."""
    known = {"platform", "video", "caption", "caption_file", "title", "tags", "schedule_at", "options"}
    extra = set(d) - known
    if extra:
        raise ValueError(f"unknown job field(s): {', '.join(sorted(extra))}")
    platform = str(d.get("platform", "")).lower().strip()
    if platform not in PLATFORMS:
        raise ValueError(f"platform must be one of {', '.join(PLATFORMS)} (got {d.get('platform')!r})")
    if not d.get("video"):
        raise ValueError("video (path of the video file) is required")
    caption = d.get("caption") or ""
    if d.get("caption_file"):
        caption = Path(d["caption_file"]).expanduser().read_text(encoding="utf-8").strip()
    tags = d.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    return PostJob(
        platform=platform,  # type: ignore[arg-type]
        video=Path(str(d["video"])).expanduser(),
        caption=str(caption),
        title=str(d.get("title") or ""),
        tags=[str(t) for t in tags],
        schedule_at=parse_time(d.get("schedule_at")),
        options=dict(d.get("options") or {}),
    )


def validate(job: PostJob, *, now: datetime | None = None) -> list[str]:
    """Problems that would make the platform refuse the post (empty list = fine)."""
    p = job.platform
    where = f"{p} {job.video.name}"
    problems: list[str] = []
    if p not in PLATFORMS:
        return [f"unknown platform {p!r}"]
    if not job.video.is_file():
        problems.append(f"{where}: video file not found: {job.video}")
    elif job.video.stat().st_size == 0:
        problems.append(f"{where}: video file is empty: {job.video}")
    lim = LIMITS[p]
    if len(job.caption) > lim["caption"]:
        problems.append(f"{where}: caption is {len(job.caption)} characters; {p} allows {lim['caption']}")
    if p == "youtube":
        if not job.title.strip():
            problems.append(f"{where}: YouTube needs a title")
        if len(job.title) > lim["title"]:
            problems.append(f"{where}: title is {len(job.title)} characters; YouTube allows {lim['title']}")
        if re.search(r"[<>]", job.title + job.caption):
            problems.append(f"{where}: YouTube refuses '<' and '>' in the title and description")
        if len(",".join(job.tags)) > lim["tags"]:
            problems.append(f"{where}: tags total {len(','.join(job.tags))} characters; YouTube allows {lim['tags']}")
    elif not job.caption.strip():
        problems.append(f"{where}: caption is empty")
    for (plat, key), allowed in OPTION_VALUES.items():
        if plat == p and key in job.options and job.options[key] not in allowed:
            problems.append(f"{where}: options.{key} must be one of {', '.join(allowed)}")
    if job.schedule_at is not None:
        now = now or datetime.now(timezone.utc)
        lo, hi = SCHEDULE_WINDOW[p]
        if job.schedule_at < now + lo:
            problems.append(f"{where}: schedule_at {job.schedule_at.isoformat()} must be at least "
                            f"{int(lo.total_seconds() // 60)} minutes in the future")
        if hi is not None and job.schedule_at > now + hi:
            problems.append(f"{where}: schedule_at {job.schedule_at.isoformat()} is more than {hi.days} days ahead; "
                            f"{p} does not allow that")
    return problems


def validate_all(jobs: list[PostJob]) -> None:
    problems = [x for j in jobs for x in validate(j)]
    seen: set[tuple[str, str, str]] = set()
    for j in jobs:
        key = (j.platform, str(j.video.resolve()), j.schedule_at.isoformat() if j.schedule_at else "now")
        if key in seen:
            problems.append(f"{j.platform} {j.video.name}: the same video is in the batch twice for the same time")
        seen.add(key)
    if problems:
        raise ValidationError(problems)


def new_job_id(platform: str) -> str:
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{platform}-{uuid.uuid4().hex[:6]}"


# ------------------------------------------------------------ running
class Runner:
    def __init__(self, bridge: Bridge, *, home: Path | None = None) -> None:
        self.bridge = bridge
        self.home = home or bridge.home
        self.running: dict[str, dict[str, Any]] = {}  # job id -> what it is doing (for status())

    @property
    def history_file(self) -> Path:
        return self.home / "history.jsonl"

    def artifacts_dir(self, job_id: str) -> Path:
        return self.home / "artifacts" / job_id

    async def run_job(self, job: PostJob, *, dry_run: bool = False, keep_tab: bool = False,
                      timeout: float = JOB_TIMEOUT, job_id: str | None = None) -> PostResult:
        """Run one job in a fresh tab. Never raises for platform problems: returns a failed PostResult instead."""
        job_id = job_id or new_job_id(job.platform)
        self.running[job_id] = {"platform": job.platform, "video": str(job.video), "title": job.title,
                                "started": time.strftime("%H:%M:%S"), "dry_run": dry_run}
        started = time.monotonic()
        tab_id: int | None = None
        page: Page | None = None
        try:
            problems = validate(job)
            if problems:
                raise ValidationError(problems)
            driver = drivers.get(job.platform)
            tab_id = await self.bridge.open_tab("about:blank")
            page = Page(ExtensionTransport(self.bridge, tab_id), artifacts_dir=self.artifacts_dir(job_id))
            await page.goto(driver.START_URL)
            result = await asyncio.wait_for(driver.publish(page, job, dry_run=dry_run), timeout)
        except asyncio.TimeoutError:
            result = await self._failed(job, page, tab_id, f"timeout: the {job.platform} flow took over {timeout:.0f}s")
        except DriverError as e:
            result = await self._failed(job, page, tab_id, str(e), e.screenshot)
        except ValidationError as e:
            result = PostResult(job.platform, ok=False, status="failed", message="invalid job: " + str(e))
        except (BridgeError, PageError) as e:
            result = await self._failed(job, page, tab_id, f"{type(e).__name__}: {e}")
        except Exception as e:  # a driver bug must not take the whole batch down
            result = await self._failed(job, page, tab_id, f"unexpected {type(e).__name__}: {e}")
        finally:
            self.running.pop(job_id, None)
        if tab_id is not None and not keep_tab:
            try:
                await self.bridge.close_tab(tab_id)
            except BridgeError:
                pass
        self._record(job_id, job, result, dry_run=dry_run, seconds=time.monotonic() - started)
        return result

    async def run_batch(self, jobs: Iterable[PostJob], *, max_parallel: int = 3, per_platform: int = 1,
                        dry_run: bool = False, keep_tab: bool = False, timeout: float = JOB_TIMEOUT) -> list[PostResult]:
        """Validate every job first (nothing runs if one is invalid), then run them; results keep the input order."""
        jobs = list(jobs)
        validate_all(jobs)
        overall = asyncio.Semaphore(max(1, max_parallel))
        per = {p: asyncio.Semaphore(max(1, per_platform)) for p in PLATFORMS}

        async def one(j: PostJob) -> PostResult:
            async with per[j.platform], overall:
                return await self.run_job(j, dry_run=dry_run, keep_tab=keep_tab, timeout=timeout)

        return list(await asyncio.gather(*(one(j) for j in jobs)))

    async def check_accounts(self, platforms: Iterable[str] = PLATFORMS) -> dict[str, dict[str, Any]]:
        """Open each platform's START_URL in a tab and ask its driver who is logged in (tabs are closed after)."""
        async def one(p: str) -> tuple[str, dict[str, Any]]:
            if p not in PLATFORMS:
                return p, {"logged_in": False, "account": "", "detail": f"unknown platform; use {', '.join(PLATFORMS)}"}
            tab_id = None
            try:
                driver = drivers.get(p)  # type: ignore[arg-type]
                tab_id = await self.bridge.open_tab("about:blank")
                page = Page(ExtensionTransport(self.bridge, tab_id), artifacts_dir=self.artifacts_dir(f"check-{p}"))
                await page.goto(driver.START_URL)
                info = await asyncio.wait_for(driver.check_login(page), CHECK_TIMEOUT)
                return p, {"logged_in": False, "account": "", "detail": "", **(info or {})}
            except asyncio.TimeoutError:
                return p, {"logged_in": False, "account": "", "detail": f"timeout after {CHECK_TIMEOUT:.0f}s"}
            except Exception as e:
                return p, {"logged_in": False, "account": "", "detail": f"{type(e).__name__}: {e}"}
            finally:
                if tab_id is not None:
                    try:
                        await self.bridge.close_tab(tab_id)
                    except BridgeError:
                        pass

        return dict(await asyncio.gather(*(one(p) for p in dict.fromkeys(platforms))))

    def history(self, limit: int = 20) -> list[dict[str, Any]]:
        try:
            lines = self.history_file.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out = []
        for line in lines[-max(0, limit):] if limit else []:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    # ------------------------------------------------------------
    async def _failed(self, job: PostJob, page: Page | None, tab_id: int | None, message: str,
                      screenshot: str = "") -> PostResult:
        if not screenshot and page is not None and tab_id is not None and tab_id not in self.bridge.dead_tabs:
            screenshot = await self._screenshot(page, tab_id)
        log(f"{job.platform} {job.video.name} failed: {message}")
        return PostResult(job.platform, ok=False, status="failed", message=message, screenshot=screenshot)

    async def _screenshot(self, page: Page, tab_id: int) -> str:
        try:  # background tabs may not paint: bring it to front in its own window first
            await self.bridge.activate_tab(tab_id)
            await asyncio.sleep(0.5)
            return await asyncio.wait_for(page.screenshot("failure"), SCREENSHOT_TIMEOUT)
        except Exception:
            return ""

    def _record(self, job_id: str, job: PostJob, result: PostResult, *, dry_run: bool, seconds: float) -> None:
        row = {
            "ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "job_id": job_id,
            "video": str(job.video),
            "title": job.title,
            "caption_start": job.caption[:80],
            "schedule_at": job.schedule_at.isoformat() if job.schedule_at else None,
            "dry_run": dry_run,
            "seconds": round(seconds, 1),
            **result.to_dict(),
        }
        self.home.mkdir(parents=True, exist_ok=True)
        with self.history_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
