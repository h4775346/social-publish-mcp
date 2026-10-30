---
name: social-publish
description: Upload, schedule and publish videos (Shorts, Reels, TikToks, normal YouTube videos) to YouTube Studio, Facebook (Meta Business Suite reels) and TikTok Studio through the user's own logged-in Chrome, with the social-publish-mcp tools. Use when the user asks to post, upload, publish, schedule or cross-post a video, plan a week of posts, check which social accounts are logged in, or asks what was posted / why a post failed.
---

# social-publish

The `social-publish-mcp` server drives the user's real Chrome (through the "Social Publish MCP" extension) to post
videos on the user's own accounts. There are no API keys: whatever account is logged in to Chrome is the account
that posts. Every post is public and permanent, so the rules below are not optional.

## Tools

| tool | what it does |
|---|---|
| `status()` | extension connected?, versions, running jobs, open tabs, last results. Call it first. |
| `check_accounts(platforms=[...])` | opens each platform's upload page, reports `{logged_in, account, detail}`, closes the tabs. |
| `publish(platform, video, caption, title="", tags=[], schedule_at=None, options={}, dry_run=False, keep_tab=False)` | one video to one platform. |
| `publish_batch(jobs, max_parallel=3, dry_run=False, keep_tab=False)` | many posts: one video to several platforms, or a week of scheduled posts. Validates every job first; if one is invalid nothing runs. |
| `history(limit=20)` | the last results (also failures, with screenshot paths). |

`platform` is `youtube`, `facebook` or `tiktok`. `video` is an absolute path on this computer.
`caption` is the YouTube description / Facebook reel description / TikTok caption, hashtags included, posted as
written. `title` is YouTube only (required there, ≤100 chars). Options:

- youtube: `channel_id` (needed when the Google account owns several channels), `playlists` (exact names),
  `made_for_kids` (default false), `altered_content` (default false), `visibility` public|unlisted|private
- facebook: `asset_id` (the Page's Business Suite asset id), `allow_remix` (default true)
- tiktok: `visibility` everyone|friends|only_me, `ai_generated` (default false), `allow_comments` (default true)

## Rules

1. **Never post without the user's explicit OK** for that exact post or batch (video, text, platform, time).
   Show the plan as a table first; "make a plan" is not "post it". `dry_run=True` needs no approval.
2. **One job per platform.** Cross-posting one video = one job per platform in `publish_batch`, each with text
   written for that platform (TikTok/Facebook have no title: put the hook in the caption).
3. **Times are ISO 8601 with a UTC offset**, in the audience's time zone: `2026-11-01T17:00+02:00`. Never send a
   naive time. Omit `schedule_at` to publish now. Windows: YouTube ≥15 min ahead; TikTok 15 min to 10 days;
   Facebook 20 min to 29 days. Platforms round to their own steps (TikTok: 5 minutes).
4. **Dry run first** for a new template, new account, new platform, or after a failure: `dry_run=True,
   keep_tab=True` fills everything and stops before the final Publish/Schedule click; ask the user to look at the
   tab, then run for real.
5. Run `status()` before a batch and `check_accounts` when the account matters (several channels/pages).
6. Human pace: jobs of the same platform run one after another automatically; do not loop `publish` to spam.
   Respect the user's own cadence rules (e.g. one long video a day).
7. On failure read `message` and open the `screenshot` (a PNG path) before retrying. Do not retry a post blindly:
   check `history` and the platform's content list first, the post may already be up.

## Typical flows

**Cross-post a Short now** (after the user said yes):
```json
publish_batch(jobs=[
  {"platform": "youtube", "video": "D:/clips/s1.mp4", "title": "…", "caption": "… #shorts", "tags": ["…"]},
  {"platform": "tiktok", "video": "D:/clips/s1.mp4", "caption": "… #fyp"},
  {"platform": "facebook", "video": "D:/clips/s1.mp4", "caption": "…", "options": {"asset_id": "…"}}
])
```

**A week of posts**: one job per post with `schedule_at` per day; send them in one `publish_batch` (they run
platform by platform, in parallel across platforms). Report back a table: platform, time, status, url.

## When something is off

- `extension_connected: false` → ask the user to open Chrome; the extension badge must say "on" and its port must
  match (default 8797). Not installed → chrome://extensions → Developer mode → Load unpacked → the `extension/` folder.
- "detached" in a message → the user pressed Cancel on Chrome's "started debugging this browser" bar. Retry and
  ask them to leave the bar alone while jobs run.
- `logged_in: false` → the user must log in to that site in Chrome; never type passwords yourself.
