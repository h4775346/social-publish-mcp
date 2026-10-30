"""Data passed between the MCP tools, the job runner and the platform drivers."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

Platform = Literal["youtube", "facebook", "tiktok"]
PLATFORMS: tuple[Platform, ...] = ("youtube", "facebook", "tiktok")


@dataclass
class PostJob:
    """One video to one platform.

    schedule_at: timezone-aware datetime; None = publish now.
    title: YouTube title (other platforms ignore it; put everything they need in caption).
    caption: YouTube description / Facebook reel description / TikTok caption (hashtags included, written as-is).
    options (per platform, all optional):
      youtube:  channel_id (str, required for multi-channel accounts), playlists (list[str], exact names),
                made_for_kids (bool, default False), altered_content (bool, default False),
                visibility ("public" | "unlisted" | "private", default "public")
      facebook: asset_id (Meta Business Suite page asset id), allow_remix (bool, default True)
      tiktok:   visibility ("everyone" | "friends" | "only_me", default "everyone"), ai_generated (bool, default False),
                allow_comments (bool, default True)
    """

    platform: Platform
    video: Path
    caption: str = ""
    title: str = ""
    tags: list[str] = field(default_factory=list)
    schedule_at: datetime | None = None
    options: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.video = Path(self.video)
        if self.schedule_at is not None and self.schedule_at.tzinfo is None:
            raise ValueError("schedule_at must be timezone-aware")


@dataclass
class PostResult:
    platform: Platform
    ok: bool
    status: Literal["published", "scheduled", "failed", "dry_run"]
    url: str = ""
    post_id: str = ""
    scheduled_for: str = ""  # ISO 8601 as the platform confirmed it
    message: str = ""
    screenshot: str = ""  # path of a screenshot taken on failure (or at the end, for proof)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class DriverError(RuntimeError):
    """A step of a platform flow failed; message says which step and what was on screen."""

    def __init__(self, step: str, detail: str = "", screenshot: str = "") -> None:
        super().__init__(f"{step}: {detail}" if detail else step)
        self.step = step
        self.screenshot = screenshot
