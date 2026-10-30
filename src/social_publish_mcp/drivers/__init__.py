"""Platform drivers.

Contract (every module in this package):

    START_URL: str                      # page the job runner opens in a fresh tab before calling publish()
    async def check_login(page: Page) -> dict   # {"logged_in": bool, "account": str, "detail": str}
    async def publish(page: Page, job: PostJob, *, dry_run: bool = False) -> PostResult

publish() starts on START_URL, runs the whole flow (upload, fill, schedule or publish now) and returns a
PostResult. With dry_run=True it fills everything but stops before the final Publish/Schedule/Post click and
returns status="dry_run". On any failure it raises models.DriverError(step, detail, screenshot) — the runner
turns that into a failed PostResult. Drivers touch the filesystem only through Page.set_files /
Page.screenshot and debug dumps written into Page.artifacts_dir.
"""
from __future__ import annotations

from importlib import import_module
from types import ModuleType

from ..models import Platform


def get(platform: Platform) -> ModuleType:
    return import_module(f"{__name__}.{platform}")
