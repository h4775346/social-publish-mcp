# Platform notes (observed live, Oct 2026)

What worked when the flows were driven by hand through Chrome. Coordinates are irrelevant here: drivers use
selectors + trusted CDP input (see `page.py`). Times below are the account's local time zone (the UI shows
times in the account's zone; YouTube and TikTok showed Cairo GMT+2 for the test accounts).

## YouTube Studio (Shorts and normal videos)

- Upload URL that always opens a fresh dialog: `https://studio.youtube.com/channel/<CHANNEL_ID>/videos/upload?d=ud`.
  If the account owns several channels and the wrong one is active, Studio shows "Oops, you don't have permission
  to view this page" → report `logged_in: false` / wrong channel; never guess a channel.
  Navigating to the same URL twice may not reload: go to `/channel/<id>/content` first, then the upload URL.
- File input: `ytcp-uploads-file-picker input[type=file]` (exists hidden in the dialog). `DOM.setFileInputFiles` works
  for big files (tested 50 MB).
- After the file is set the details form appears: title and description are `ytcp-uploads-dialog #textbox`
  ([0] = title, [1] = description; contenteditable). Filling with `document.execCommand('selectAll')` +
  `document.execCommand('insertText', false, text)` after focusing works (keeps newlines, emoji, Arabic).
  Title max 100 chars, description max 5000.
- Made for kids: `tp-yt-paper-radio-button[name="VIDEO_MADE_FOR_KIDS_NOT_MFK"]` / `..._MFK`.
- "Show more": `ytcp-uploads-dialog #toggle-button`. Then altered/synthetic content:
  `tp-yt-paper-radio-button[name="VIDEO_HAS_ALTERED_CONTENT_YES"]` / `_NO`.
- Tags: input `ytcp-uploads-dialog #tags-container input#text-input`; focus it and type `tag1,tag2,` (each comma
  commits a tag; trusted typing needed — Input.insertText of the whole string worked). Channel default tags may
  already be present (500 char limit total).
- Playlists: `ytcp-video-metadata-playlists` → click its `ytcp-dropdown-trigger`; a list of checkboxes opens
  (order = most recently used first). JS `.click()` on rows toggles twice (li + label) → use a trusted click on the
  checkbox element of the row whose text starts with the playlist name, then click the "Done" button. Verify with
  `ytcp-video-metadata-playlists` innerText (shows the selected playlist name, or "Select").
  A hashtag-suggestion popup from the title field can cover the list: click an empty area first.
- Steps: `ytcp-uploads-dialog #next-button` ×3 → Visibility step.
- Publish now: `tp-yt-paper-radio-button[name="PUBLIC"]` (also UNLISTED / PRIVATE) then `#done-button` (label "Publish").
- Schedule: `ytcp-uploads-dialog #second-container-expand-button`, then `#datepicker-trigger` opens a calendar list;
  the date must be picked with a trusted click on the day cell (typing into the date input mixes date+time).
  Time: the `ytcp-datetime-picker input` whose value matches /AM|PM/ — focus (trusted click), select, then
  `execCommand('insertText', false, '5:00 PM')` + dispatch `change` + blur; close the list by clicking elsewhere.
  Then `#done-button` (label "Schedule").
- Checks: footer text "Checks complete. No issues found." Upload progress text: "Uploading 14% …",
  "Upload complete … Processing will begin shortly". You can publish/schedule before processing finishes.
- Result dialog: "Video published" or "Video scheduled" with "Your video will be set to public on <date> at <time>";
  the share link `https://youtube.com/shorts/<ID>` (or watch URL) is inside it (anchor href / text).
- Gotcha: the MCP extension's own file_upload (10 MB) and Chrome's native file dialog are both unusable; setFileInputFiles
  via CDP is the right path.

## Facebook (Meta Business Suite, page reels)

- Composer: `https://business.facebook.com/latest/reels_composer/?ref=biz_web_home_create_reel&asset_id=<ASSET_ID>`
  (or Home → "Create reel"). "Post to" shows the page name.
- Media: "Add Video" button; a hidden `input[type=file]` with accept video exists in the composer → setFileInputFiles.
  Drag-and-drop onto the card did NOT work. Upload shows "<file> 1080 x 1920 … 22%" then "100%".
- Description: contenteditable textbox with aria-label "Write into the dialogue box to include text with your post."
  under "Description". JS insertText garbles it → use trusted typing. Newlines: Shift+Enter. After a hashtag,
  the hashtag autocomplete can swallow the next characters → press Escape after each hashtag and before a newline.
  Read back with `[contenteditable=true].innerText` to verify.
- A right-side card says "Your video is safe to publish! No copyright issues were found." when the check passes.
- Buttons at the bottom: "Next" (→ Edit step: audio/trim; "Next" again) → Share step. Share step has
  "Scheduling options" (date/time) above, "Remixing and use of original audio" radios (default: allow remixing),
  closed captions, "Share" button (label changes to "Schedule" when a time is set).
- After Share: modal "Reel processing — Once your reel finishes processing, it will be published and you will be notified." + "Done".
- Scheduled items: Content → Scheduled (Planner). Reschedule via ⋯ → Manage post → Reschedule post.
- Rules learned: the full YouTube link should not be in the caption (reach); first comment is not available for scheduled posts.

## TikTok Studio (web)

- Upload page: `https://www.tiktok.com/tiktokstudio/upload`. Hidden `input[type=file]` (accept video) → setFileInputFiles
  (drag-and-drop onto "Select video to upload" also worked). After upload: "<file>.mp4 1080P Uploaded (20.21MB)".
- Description: the editor under "Description" (accessible role combobox, a Draft.js contenteditable) prefilled with the
  file name. Click it, Ctrl+A, Delete, then trusted typing. Typing "#tag " opens a hashtag suggestion list →
  press Escape after each hashtag. Max 4000 chars (counter "164/4000").
- Settings: "When to post" radios Now / Schedule. Schedule shows two pickers:
  - time button "22:35" → a two-column scroll list (hours | minutes in 5-min steps), centered on the current value;
    clicking an item selects it and re-centers the list (only ±3 items visible), so reach a value by clicking the
    farthest visible item toward the target repeatedly. The field is not typeable.
  - date button "2026-10-30" → month calendar; click the day cell. Allowed window: ~15 min to 10 days ahead.
- "Who can see this post": Everyone (default) / Friends / Only you. "High-quality uploads" toggle (on).
  "Show more" holds: allow comments/duet/stitch, "AI-generated content" toggle (keep OFF unless asked), disclose content.
- Checks: "Music copyright check — No issues found." and "Content check lite".
- Submit button at the bottom: "Post" (now) or "Schedule". Then the page goes to `tiktokstudio/content`; the new row
  shows the caption, "Content under review" briefly, and for scheduled posts a badge like "Oct 31, 8:00 PM (GMT+2) Cairo".
  The post URL is not shown in the web list; return the content page URL + the row text.
- Account check: avatar top-right; the preview shows the account name ("My Page · صفحتي").

## Verified while integrating the drivers (2026-10-30/31)

General
- Job tabs run in the background. Background tabs pause animations/rAF: animated pickers stay collapsed (TikTok's
  time wheel items keep height 0) → drivers call `Page.bring_to_front()` before using such widgets.
- A trusted CDP click sometimes does not reach a button in a background tab (Facebook "Add Video"); `element.click()`
  is the fallback when the click only has to trigger the page's own handler.

Facebook reels composer
- No file input in the DOM. "Add Video" creates a DETACHED `input[type=file]` (accept "video/*, …, .ts, .mkv") and
  calls `input.click()`. Hook `HTMLInputElement.prototype.click`/`showPicker` to capture it, then
  `DOM.setFileInputFiles` on it (fires change; upload starts). Restore the prototype afterwards.
- Share step: card "Scheduling options" with a segmented control **Share now | Schedule | Save as draft**
  (role=button each). After "Schedule": text "Select a future date and time to publish your reel at.", a date input
  (placeholder `mm/dd/yyyy`, shows "Oct 31, 2026"; ctrl+a + type mm/dd/yyyy + Enter works) and three spinbutton
  inputs aria-label `hours` / `minutes` / `meridiem` that take key events (not insertText) and keep an empty `.value`;
  the chosen time is drawn as text beside them ("05:00 PM Africa/Cairo"). The footer button changes to "Schedule".
  Also on the Share step: "Active times" button, switches Add to playlist / Share to Facebook Story / Share to groups /
  Add a poll, "Remixing and use of original audio" radios.

TikTok Studio
- Schedule radios: `input[type=radio][value=post_now|schedule]`.
- Date and time are read-only `input.TUXTextInputCore-input` (values "YYYY-MM-DD", "HH:MM"); open with a trusted click.
- Time wheel: `span.tiktok-timepicker-option-text.tiktok-timepicker-left|right` in `.tiktok-timepicker-option-item`,
  `.tiktok-timepicker-option-list`, clipped by `.tiktok-timepicker-time-scroll-container`; active item class
  `tiktok-timepicker-is-active`. Only trusted clicks select; the wheel re-centers after each click. Visibility test that
  works: `document.elementFromPoint` at the item centre.
- Calendar: `span.day` (`valid` = selectable, `selected`), month arrows `span.arrow` (second = next).
- The page shows the picker zone ("Africa/Cairo UTC+0200").
