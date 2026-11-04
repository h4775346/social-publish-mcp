"""Social Publish MCP: MCP server (stdio) + CLI. Publishes videos to YouTube, Facebook and TikTok through the
user's own logged-in Chrome, via the companion extension (see bridge.py) and the platform drivers.

The bridge starts with the server in the same process. If another social-publish-mcp process already owns the port
(Claude Desktop and Claude Code both running, or the CLI next to them), this process becomes a *peer*: every
operation (publish, check, status) is forwarded to the owner, so there is one extension connection, one set of
tabs and one history no matter how many sessions are open.

Several browsers can be connected at once (see bridge.py): ``use_browser`` or the extension popup choose which one a
platform uses, and ``options.browser`` overrides that for one job.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Awaitable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, TypeVar

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations

from . import __version__
from .bridge import NO_EXTENSION, Bridge, BridgeError, log
from .models import PLATFORMS
from .runner import JOB_TIMEOUT, Runner, ValidationError, job_from_dict, validate_all

T = TypeVar("T")
EXTENSION_WAIT = 15.0  # tools: how long to wait for the extension to (re)connect before failing
CLI_EXTENSION_WAIT = 45.0  # the extension retries with backoff up to 30 s


class Service:
    """The operations behind the tools and the CLI; runs them locally (hub) or in the hub process (peer)."""

    def __init__(self, bridge: Bridge | None = None) -> None:
        self.bridge = bridge or Bridge()
        self.runner = Runner(self.bridge)
        self.bridge.peer_handler = self._local
        self.bridge.scan_handler = self.runner.scan_browsers

    async def start(self) -> str:
        return await self.bridge.start()

    async def stop(self) -> None:
        await self.bridge.stop()

    async def call(self, op: str, args: dict[str, Any], *, extension_wait: float = EXTENSION_WAIT) -> Any:
        if self.bridge.role == "peer":
            n = len(args.get("jobs") or []) or 1
            return await self.bridge.call_hub(op, args, timeout=JOB_TIMEOUT * n + 600)
        return await self._local(op, args, extension_wait=extension_wait)

    async def _local(self, op: str, args: dict[str, Any], *, extension_wait: float = EXTENSION_WAIT) -> Any:
        if op == "status":
            return await self._status()
        if op == "history":
            return self.runner.history(int(args.get("limit", 20)))
        if op == "list_browsers" and not args.get("check"):
            return self.bridge.roster()
        if op == "use_browser" and str(args.get("browser", "")).strip().casefold() != "auto":
            return await self.bridge.choose(str(args.get("browser", "")), args.get("platforms") or None)
        if op not in ("publish_batch", "check_accounts", "list_browsers", "use_browser"):
            raise ValueError(f"unknown operation {op!r}")
        if op == "publish_batch":  # validate before waiting for Chrome: bad input should fail fast
            jobs = [job_from_dict(d) for d in args.get("jobs") or []]
            if not jobs:
                raise ValueError("no jobs")
            validate_all(jobs)
        if not await self.bridge.wait_extension(extension_wait):
            raise BridgeError(NO_EXTENSION)
        if op == "check_accounts":
            return await self.runner.check_accounts(args.get("platforms") or PLATFORMS, browser=args.get("browser"))
        if op in ("list_browsers", "use_browser"):  # with a check, or use_browser("auto"): read who is logged in where
            return await self.runner.scan_browsers(args.get("platforms") or PLATFORMS)
        for j in jobs:  # refuse before opening any tab: no browser chosen, or the chosen one is offline
            self.bridge.select(j.options.get("browser"), j.platform)
        results = await self.runner.run_batch(
            jobs, max_parallel=int(args.get("max_parallel", 3)), dry_run=bool(args.get("dry_run")),
            keep_tab=bool(args.get("keep_tab")))
        return [r.to_dict() for r in results]

    async def _status(self) -> dict[str, Any]:
        b = self.bridge
        st: dict[str, Any] = {
            "server_version": __version__,
            "bridge": "hub (this process owns the port)",
            "port": b.port,
            "state_dir": str(b.home),
            "extension_connected": b.ext is not None,
            "extension": b.ext_info,
            **b.roster(),
            "running_jobs": list(self.runner.running.values()),
            "last_results": self.runner.history(5),
        }
        if b.ext is None:
            st["hint"] = NO_EXTENSION
        else:
            try:
                st["tabs"] = await b.list_tabs()
            except BridgeError as e:
                st["tabs_error"] = str(e)
        return st


def normalize_job(d: dict[str, Any], base: Path | None = None) -> dict[str, Any]:
    """Make paths absolute and inline caption_file, so the job means the same thing in the hub process."""
    d = dict(d)
    base = base or Path.cwd()
    if d.get("video"):
        v = Path(str(d["video"])).expanduser()
        d["video"] = str(v if v.is_absolute() else (base / v).resolve())
    if d.get("caption_file"):
        c = Path(str(d.pop("caption_file"))).expanduser()
        d["caption"] = (c if c.is_absolute() else base / c).read_text(encoding="utf-8").strip()
    if d.get("schedule_at") is not None and not isinstance(d["schedule_at"], str):
        d["schedule_at"] = d["schedule_at"].isoformat()
    return d


# ------------------------------------------------------------ MCP
service: Service | None = None


@asynccontextmanager
async def lifespan(_app: FastMCP):
    global service
    service = Service()
    await service.start()
    try:
        yield
    finally:
        await service.stop()
        service = None


mcp = FastMCP("social-publish-mcp", lifespan=lifespan)


def _hints(read_only: bool, destructive: bool, idempotent: bool, open_world: bool) -> ToolAnnotations:
    return ToolAnnotations(readOnlyHint=read_only, destructiveHint=destructive, idempotentHint=idempotent,
                           openWorldHint=open_world)


async def _with_heartbeat(ctx: Context | None, work: Awaitable[T]) -> T:
    """Report progress every 10 s while a long operation runs (clients that reset their timeout on progress
    will not cut off a 10-minute upload)."""
    task = asyncio.ensure_future(work)
    beat = 0
    while True:
        done, _ = await asyncio.wait({task}, timeout=10)
        if done:
            return task.result()
        beat += 1
        if ctx is not None:
            try:
                await ctx.report_progress(beat, None, "publishing…")
            except Exception:
                pass


def _svc() -> Service:
    if service is None:
        raise RuntimeError("the server is not running")
    return service


async def _publish(jobs: list[dict[str, Any]], max_parallel: int, dry_run: bool, keep_tab: bool,
                   ctx: Context | None) -> dict[str, Any]:
    try:
        jobs = [normalize_job(j) for j in jobs]
        results = await _with_heartbeat(ctx, _svc().call(
            "publish_batch", {"jobs": jobs, "max_parallel": max_parallel, "dry_run": dry_run, "keep_tab": keep_tab}))
    except ValidationError as e:
        return {"ok": False, "error": "invalid job(s); nothing was posted", "problems": e.problems}
    except (ValueError, OSError) as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}; nothing was posted"}
    except BridgeError as e:  # no extension, or the hub (peer mode) failed: some posts may have run
        return {"ok": False, "error": f"{e}. Check history() before retrying."}
    return {"ok": all(r["ok"] for r in results), "results": results}


@mcp.tool(annotations=_hints(read_only=False, destructive=False, idempotent=False, open_world=True))
async def publish(platform: str, video: str, caption: str, title: str = "", tags: list[str] | None = None,
                  schedule_at: str | None = None, options: dict[str, Any] | None = None, dry_run: bool = False,
                  keep_tab: bool = False, ctx: Context | None = None) -> dict[str, Any]:
    """Upload one video to one platform through the user's logged-in Chrome, and publish or schedule it.

    Only call this after the user approved this exact post (video, text, platform, time).
    platform: "youtube" | "facebook" | "tiktok".
    video: absolute path of the video file on this computer.
    caption: YouTube description / Facebook reel description / TikTok caption, hashtags included, written as-is.
    title: YouTube title (required for YouTube, max 100 chars; ignored elsewhere).
    tags: YouTube tags. schedule_at: ISO 8601 with UTC offset (e.g. "2026-11-01T17:00+02:00"); omit to publish now.
    options: youtube {channel_id, playlists, made_for_kids, altered_content, visibility}; facebook {asset_id,
    allow_remix}; tiktok {visibility, ai_generated, allow_comments}. Any platform: browser (name or id from
    list_browsers), to post from that browser for this job only.
    dry_run: fill everything but stop before the final Publish/Schedule click (use it first for a new template).
    keep_tab: leave the tab open afterwards (to look at a dry run).
    Returns {ok, results:[{platform, ok, status, url, post_id, scheduled_for, message, screenshot}]}.
    """
    job = {"platform": platform, "video": video, "caption": caption, "title": title, "tags": tags or [],
           "schedule_at": schedule_at, "options": options or {}}
    return await _publish([job], 1, dry_run, keep_tab, ctx)


@mcp.tool(annotations=_hints(read_only=False, destructive=False, idempotent=False, open_world=True))
async def publish_batch(jobs: list[dict[str, Any]], max_parallel: int = 3, dry_run: bool = False,
                        keep_tab: bool = False, ctx: Context | None = None) -> dict[str, Any]:
    """Run several posts: e.g. one video to three platforms at once, or a week of scheduled posts.

    Only call this after the user approved every post in the batch.
    jobs: list of {platform, video, caption, title?, tags?, schedule_at?, options?} (same fields as publish;
    caption_file may replace caption). All jobs are validated first: if one is invalid nothing runs.
    Jobs run in parallel tabs (max_parallel), but one job per platform at a time.
    Returns {ok, results:[...]} in the input order.
    """
    return await _publish(jobs, max_parallel, dry_run, keep_tab, ctx)


@mcp.tool(annotations=_hints(read_only=True, destructive=False, idempotent=True, open_world=True))
async def check_accounts(platforms: list[str] | None = None, browser: str | None = None) -> dict[str, Any]:
    """Open each platform's upload page in Chrome and report which account is logged in (tabs are closed after).

    platforms: any of "youtube", "facebook", "tiktok" (default: all). browser: name or id (default: the chosen one).
    Returns {platform: {logged_in, account, detail}}.
    """
    return await _svc().call("check_accounts", {"platforms": platforms or list(PLATFORMS), "browser": browser})


@mcp.tool(annotations=_hints(read_only=True, destructive=False, idempotent=True, open_world=True))
async def list_browsers(check_accounts: bool = False) -> dict[str, Any]:
    """Which browsers (Chrome profiles, Brave, Edge...) are connected, and which platform each one posts to.

    check_accounts: also open each platform in each browser to read who is logged in, and route every platform that
    is logged in to in exactly one browser (tabs are closed after). Use it when status says needs_choice.
    Returns {browsers:[{id, label, connected, email, is_default, routes, accounts, jobs}], default, routes, needs_choice}.
    """
    return await _svc().call("list_browsers", {"check": check_accounts})


@mcp.tool(annotations=_hints(read_only=False, destructive=False, idempotent=True, open_world=False))
async def use_browser(browser: str, platforms: list[str] | None = None) -> dict[str, Any]:
    """Choose which browser posts. Nothing is published by this call.

    browser: a name or id from list_browsers; "auto" scans accounts and routes each platform to the browser logged
    in to it; "none" clears the choice. platforms: route only these platforms to the browser (default: make it the
    default browser for every platform without a route of its own).
    Returns the same roster as list_browsers.
    """
    return await _svc().call("use_browser", {"browser": browser, "platforms": platforms})


@mcp.tool(annotations=_hints(read_only=True, destructive=False, idempotent=True, open_world=False))
async def status() -> dict[str, Any]:
    """Is the Chrome extension connected? Server/extension versions, running jobs, open tabs, last results."""
    st = await _svc().call("status", {})
    if _svc().bridge.role == "peer":
        st["bridge"] = "peer (operations go through another social-publish-mcp process that owns the port)"
    return st


@mcp.tool(annotations=_hints(read_only=True, destructive=False, idempotent=True, open_world=False))
async def history(limit: int = 20) -> list[dict[str, Any]]:
    """The last `limit` posting results (newest last), from <state dir>/history.jsonl."""
    return _svc().runner.history(limit)


# ------------------------------------------------------------ CLI
def _option(kv: str) -> tuple[str, Any]:
    key, sep, val = kv.partition("=")
    if not sep:
        raise argparse.ArgumentTypeError(f"--option needs key=value (got {kv!r})")
    try:
        return key.strip(), json.loads(val)
    except ValueError:
        return key.strip(), val


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="social-publish-mcp",
        description="With no arguments: run the MCP server on stdio. Otherwise run one command and exit.")
    ap.add_argument("--version", "-V", action="store_true", help="print the version")
    ap.add_argument("--check", action="store_true", help="check which accounts are logged in")
    ap.add_argument("--platforms", default=",".join(PLATFORMS), help="for --check: comma separated platforms")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("post", help="publish or schedule one video")
    p.add_argument("--platform", required=True, choices=PLATFORMS)
    p.add_argument("--video", required=True)
    g = p.add_mutually_exclusive_group()
    g.add_argument("--caption", default="")
    g.add_argument("--caption-file")
    p.add_argument("--title", default="")
    p.add_argument("--tags", default="", help="comma separated (YouTube)")
    p.add_argument("--schedule", help="ISO 8601 with offset, e.g. 2026-11-01T17:00+02:00 (omit = now)")
    p.add_argument("--option", action="append", type=_option, default=[], metavar="KEY=VALUE",
                   help="platform option, value parsed as JSON when possible (repeatable)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--keep-tab", action="store_true")

    b = sub.add_parser("batch", help="run the jobs in a JSON file (a list, or {\"jobs\": [...]})")
    b.add_argument("file")
    b.add_argument("--max-parallel", type=int, default=3)
    b.add_argument("--dry-run", action="store_true")
    b.add_argument("--keep-tab", action="store_true")

    st = sub.add_parser("setup", help="install the extension folder and the skill, and register the server in your AI clients")
    st.add_argument("--client", action="append", metavar="NAME",
                    help="client to configure (repeatable; default: every client found). "
                         "One of: claude-code, claude-desktop, cursor, windsurf, vscode, gemini, codex")
    st.add_argument("--no-skill", action="store_true", help="do not install the Claude Code skill")
    st.add_argument("--no-extension", action="store_true", help="do not copy the extension folder")
    st.add_argument("--no-open", action="store_true", help="do not open chrome://extensions and the extension folder")

    br = sub.add_parser("browsers", help="connected browsers and what each one posts to")
    br.add_argument("--check", action="store_true", help="also read who is logged in where, and route the platforms")
    ub = sub.add_parser("use-browser", help="choose the browser: a name or id, 'auto' or 'none'")
    ub.add_argument("browser")
    ub.add_argument("--platforms", default="", help="comma separated: route only these platforms to it")

    sub.add_parser("status", help="extension connection and running jobs")
    h = sub.add_parser("history", help="last results")
    h.add_argument("-n", type=int, default=20)
    return ap


def load_jobs_file(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Jobs from a JSON file; relative video/caption_file paths are relative to the file's folder."""
    data = json.loads(path.read_text(encoding="utf-8"))
    settings: dict[str, Any] = {}
    if isinstance(data, dict):
        settings = {k: v for k, v in data.items() if k != "jobs"}
        data = data.get("jobs")
    if not isinstance(data, list):
        raise ValueError(f"{path}: expected a list of jobs or {{\"jobs\": [...]}}")
    return [normalize_job(d, path.parent.resolve()) for d in data], settings


async def _cli(a: argparse.Namespace) -> int:
    svc = Service()
    await svc.start()
    try:
        if a.check:
            out: Any = await svc.call("check_accounts", {"platforms": [x.strip() for x in a.platforms.split(",") if x.strip()]},
                                      extension_wait=CLI_EXTENSION_WAIT)
            print(json.dumps(out, indent=2, ensure_ascii=False))
            return 0 if all(v.get("logged_in") for v in out.values()) else 1
        if a.cmd == "status":
            print(json.dumps(await svc.call("status", {}), indent=2, ensure_ascii=False))
            return 0
        if a.cmd == "browsers":
            print(json.dumps(await svc.call("list_browsers", {"check": a.check}, extension_wait=CLI_EXTENSION_WAIT),
                             indent=2, ensure_ascii=False))
            return 0
        if a.cmd == "use-browser":
            plats = [x.strip() for x in a.platforms.split(",") if x.strip()]
            print(json.dumps(await svc.call("use_browser", {"browser": a.browser, "platforms": plats},
                                            extension_wait=CLI_EXTENSION_WAIT), indent=2, ensure_ascii=False))
            return 0
        if a.cmd == "history":
            print(json.dumps(svc.runner.history(a.n), indent=2, ensure_ascii=False))
            return 0
        if a.cmd == "post":
            job = {"platform": a.platform, "video": a.video, "caption": a.caption, "title": a.title,
                   "tags": [t.strip() for t in a.tags.split(",") if t.strip()], "schedule_at": a.schedule,
                   "options": dict(a.option)}
            if a.caption_file:
                job["caption_file"] = a.caption_file
            jobs, max_parallel = [normalize_job(job)], 1
        else:
            jobs, settings = load_jobs_file(Path(a.file))
            max_parallel = int(settings.get("max_parallel", a.max_parallel))
        args = {"jobs": jobs, "max_parallel": max_parallel, "dry_run": a.dry_run, "keep_tab": a.keep_tab}
        results = await svc.call("publish_batch", args, extension_wait=CLI_EXTENSION_WAIT)
        print(json.dumps(results, indent=2, ensure_ascii=False))
        return 0 if all(r["ok"] for r in results) else 1
    except ValidationError as e:
        print("invalid job(s); nothing was posted:\n  " + "\n  ".join(e.problems), file=sys.stderr)
        return 2
    except (BridgeError, ValueError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    finally:
        await svc.stop()


def main(argv: list[str] | None = None) -> None:
    a = _parser().parse_args(argv)
    if a.version:
        print(f"social-publish-mcp {__version__}")
        return
    if a.cmd == "setup":
        from .setup import run_setup
        sys.exit(run_setup(client_names=a.client, skill=not a.no_skill, extension=not a.no_extension,
                           open_pages=not a.no_open))
    if a.check or a.cmd:
        # account names and captions are often non-Latin; Windows consoles default to cp1252
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
        sys.exit(asyncio.run(_cli(a)))
    log(f"{__version__} starting (MCP on stdio)")
    mcp.run()


if __name__ == "__main__":
    main()
