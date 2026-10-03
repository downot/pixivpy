# Work Migration (Export / Re-upload)

Export all works (illusts + novels) of a pixiv account and re-upload them to
another account. The scripts are standalone and do not depend on the
`pixivpy3` package: they need `python3` + `requests` only; the CAPTCHA
solvers additionally require `node` and Google Chrome.

| File | Purpose |
| --- | --- |
| `export_works.py` | Export: list all works of the account, fetch details, download original images |
| `upload_works.py` | Import: read an export directory and re-upload illusts to a target account |
| `captcha_solver.mjs` | Automatic reCAPTCHA solver (CDP + LLM, headless) |
| `captcha_human.mjs` | Human-in-the-loop reCAPTCHA solver (visible window, records labeled samples) |

The tools use the **web AJAX API** (`https://www.pixiv.net/ajax/...`) with
browser `Cookie` authentication, independent of the OAuth App-API in
`pixivpy3`. The newly used web APIs are summarized in
[new web APIs](#new-web-apis).

## Setup

1. Log in to www.pixiv.net in your browser, open DevTools → Network, and copy
   the `cookie` request header of any `ajax` request.
2. Save it to a file:
   - `cookie_a.txt` — source account (export)
   - `cookie_b.txt` — target account (import)

   Two file formats are supported: a plain cookie string, or node-fetch style
   JSON containing a `"cookie": "..."` field. Cookie files contain your login
   credentials; do not commit them to git (already in `.gitignore`).
3. The scripts **re-read the cookie file before every request** (change
   detected via content hash), so you can overwrite the file while a run is in
   progress to refresh the cookie — no restart needed.
4. The user id is taken from the numeric prefix of `PHPSESSID` in the cookie
   (`15032614_xxx` → `15032614`), or pass `--user-id` explicitly.

## Export: export_works.py

```bash
# Full export (metadata + original images)
python3 export_works.py --cookie-file cookie_a.txt

# Explicit account, slower pacing
python3 export_works.py --cookie-file cookie_a.txt --user-id 15032614 --delay 1.0

# Metadata only / skip novels
python3 export_works.py --cookie-file cookie_a.txt --no-images --no-novels
```

| Option | Default | Description |
| --- | --- | --- |
| `--cookie-file` | required | cookie file (source account) |
| `--user-id` | from PHPSESSID | source account id |
| `--out` | `export` | output root directory |
| `--delay` | `0.8` | base delay between requests (s, with ±30% jitter) |
| `--max-retries` | `4` | max retries per request |
| `--no-images` | off | metadata only, skip image downloads |
| `--no-novels` | off | skip novels |

Flow:

1. Paginate `/ajax/dashboard/works/illust` and `/ajax/dashboard/works/novel`
   (200 per page) and save list snapshots `works_illusts.json` /
   `works_novels.json`;
2. For each work, fetch the detail `/ajax/illust/{id}`; for illusts also fetch
   `/ajax/illust/{id}/uploadedImages` and download the original images
   (named by page order: `0.png`, `1.png`, …);
3. Write `meta.json` per work and update progress in `state.json`.
   **Resumable**: on re-run, works marked `status=done` are skipped.

### Output layout

```
export/
├── logs/export.log
└── <user_id>/
    ├── state.json             # export progress {illusts: {id: {status, ...}}, novels: {...}}
    ├── works_illusts.json     # illust list snapshot (works / thumbnails / tagTranslation)
    ├── works_novels.json      # novel list snapshot
    ├── illusts/
    │   └── <work_id>/
    │       ├── meta.json
    │       └── 0.png, 1.png, … # original images (by page order)
    └── novels/
        └── <work_id>/
            └── meta.json
```

`meta.json` (illust, field example):

```json
{
  "list": {
    "id": "150000000",
    "title": "Example title",
    "tags": ["tag-a", "tag-b"],
    "createDate": "2024-01-01T00:00:00+09:00",
    "aiType": 1,
    "isUnlisted": false,
    "visibilityScope": 1,
    "titleCaptionTranslation": { "workTitle": "", "workCaption": "" }
  },
  "detail": {
    "title": "Example title",
    "description": "Caption (HTML)",
    "isOriginal": true,
    "isUnlisted": false,
    "isLoginOnly": false,
    "restrict": 0,
    "xRestrict": 0,
    "createDate": "2024-01-01T00:00:00+09:00",
    "uploadDate": "2024-01-01T00:00:00+09:00",
    "pageCount": 2,
    "width": 2048,
    "height": 2048,
    "illustType": 0,
    "seriesNavData": null
  },
  "images": [
    {
      "index": 0,
      "url": "https://i.pximg.net/orig/xxxx.png",
      "byteLength": 123456,
      "mimeType": "image/png",
      "file": "0.png",
      "downloaded_bytes": 123456,
      "verified": true
    }
  ]
}
```

Reliability behavior:

- 429 / 5xx are retried with exponential backoff (honors `Retry-After`);
- HTTP 403 with a Cloudflare challenge: wait and retry; if it persists, refresh
  the cookie file;
- downloaded files are size-verified against `byteLength`; mismatches are
  re-downloaded;
- TLS is pinned to 1.2 because TLS 1.3 handshakes with pixiv are flaky on
  macOS LibreSSL;
- exit codes: `0` all ok; `1` cannot determine the user id; `2` some works
  failed (ids and reasons in `state.json` and the log).

## Import: upload_works.py

Re-upload the illusts from an export directory to the target account.
**Novels are currently export-only and are not re-uploaded** (the web novel
creation flow is different).

```bash
python3 upload_works.py --cookie-file cookie_b.txt --source export/15032614

# Trial run of 2 works / only specific works
python3 upload_works.py --cookie-file cookie_b.txt --source export/15032614 --limit 2
python3 upload_works.py --cookie-file cookie_b.txt --source export/15032614 --only 150148884,150148885

# Human CAPTCHA: a visible window opens and waits for you
python3 upload_works.py --cookie-file cookie_b.txt --source export/15032614 --human

# No auto-solving: on CAPTCHA back off 60s and retry, skip the work after 3 tries
python3 upload_works.py --cookie-file cookie_b.txt --source export/15032614 --no-solve --captcha-retries 3
```

| Option | Default | Description |
| --- | --- | --- |
| `--cookie-file` | required | cookie file (target account) |
| `--source` | required | export directory (e.g. `export/<user_id>`) |
| `--user-id` | from cookie | target account id |
| `--delay` | `3.0` | base delay between works (s, ±30% jitter) |
| `--limit` | none | upload at most N new works in this run |
| `--only` | none | only upload the given work ids (comma-separated) |
| `--human` | off | human-in-the-loop CAPTCHA mode |
| `--no-solve` | off | don't auto-solve CAPTCHAs (back off, then skip) |
| `--captcha-retries` | `3` | per-work CAPTCHA backoff retries in `--no-solve` mode |

Per-work upload flow:

1. Read `works_illusts.json`, sorted by original `createDate` **ascending**
   (oldest first, to preserve timeline order); works already present in
   `upload_state.json` are skipped;
2. Refresh the CSRF token from the `/illustration/create` page; call
   `/ajax/work/create/illustration/init` to check for an in-progress creation
   request and whether a CAPTCHA is currently required;
3. Build the form fields (mapping below) + the original image files, and
   multipart POST `/ajax/work/create/illustration`;
4. Poll `/ajax/work/create/illustration/progress?convertKey=…` every 2.5s
   until `COMPLETE`, take the new `illustId`, and record the old → new id
   mapping in `upload_state.json`;
5. The reCAPTCHA token is **single-use** and is discarded after a successful
   upload; a 400/403 on create (stale CSRF) refreshes the token and retries
   once.

Field mapping (export `meta.json` → create request):

| create field | Source |
| --- | --- |
| `title` / `titleTranslations[en]` | `detail.title` / `list.titleCaptionTranslation.workTitle` |
| `caption` / `captionTranslations[en]` | `detail.description` (HTML → plain text, with a trailing line `🕰️ 元投稿日 (Original posting date): YYYY-MM-DD` appended) / `…workCaption` |
| `tags[]` | `list.tags` |
| `xRestrict` | `detail.xRestrict` (0/1/2 → `general`/`r18`/`r18g`) |
| `aiType` | `list.aiType` (0/1/2 → `undefined`/`notAiGenerated`/`aiGenerated`) |
| `restrict` | `list.isUnlisted` → `unlisted`; `list.visibilityScope == 4` or `detail.isLoginOnly` → `loginOnly`; otherwise `public` |
| `original` | `detail.isOriginal` |
| `files[]` / `imageOrder[i][type]` / `imageOrder[i][fileKey]` | the original images from `meta.json` (file size is verified against `byteLength` before upload) |
| rest | fixed values: `sexual=false`, `allowTagEdit=true`, `allowComment=true`, `responseAutoAccept=false`, `ratings[*]=false`, `allowCitationWork=0` |

Notes:

- the content-rating fields `ratings[*]` are not migrated (all set to
  `false`);
- re-uploaded works get **new ids**; the old → new mapping is in
  `upload_state.json` (`uploaded` / `failed`);
- exit codes are the same as export: `0` / `1` / `2`.

## CAPTCHA solvers

The create API randomly requires a reCAPTCHA token (form field
`gRecaptchaResponse`). The Python scripts invoke `node <solver>.mjs` as a
subprocess; on success the solver prints `TOKEN=<token>` to stdout, with all
diagnostics on stderr.

### captcha_solver.mjs (automatic)

How it works:

1. Start a local HTTP server (127.0.0.1:8799) serving a minimal reCAPTCHA page
   (using pixiv's own site key);
2. Launch Chrome with `--host-resolver-rules=MAP www.pixiv.net 127.0.0.1` so
   the reCAPTCHA is issued from a pixiv-trusted origin;
3. Drive the widget via CDP: composite the 3×3 / 4×4 grid into a single
   numbered image in the browser, ask an LLM which cells contain part of the
   target, then click the selected cells and the verify button with
   human-like mouse motion;
4. Up to 6 rounds; on success prints `TOKEN=` and exits.

Constants to adjust per environment: `LLM_URL` (an OpenAI-compatible
`/v1/chat/completions` endpoint; defaults to a local deployment), the
`model` in `LLM.ask`, and the Chrome executable path (defaults to the macOS
location).

### captcha_human.mjs (human-in-the-loop)

Same origin-mapping trick, but opens a **visible** window that a human solves.
While you solve it, a labeled sample (numbered grid image + instruction + the
cells you clicked + the LLM's answer to the same grid, for comparison) is
saved to `/tmp/cdp/human_solves/` to calibrate the automatic solver.

## New web APIs

Base URL: `https://www.pixiv.net`.

Common request headers: `cookie`, `x-user-id`, `referer` (the page the request
originates from), `accept: application/json`; create requests additionally
need `x-csrf-token` (a 32-hex token embedded in the `/illustration/create`
page).

Common response envelope: `{"error": bool, "message": str, "body": …}`; the
tables below only describe `body`.

### Export

| Method | Path | Description |
| --- | --- | --- |
| GET | `/ajax/dashboard/works/illust?offset=0&limit=200` | List my illusts (paginated: `offset` accumulates, `limit` ≤ 200). `body.data.works[]`, `body.thumbnails.illust[]` (includes `tags`, `aiType`, `isUnlisted`, `visibilityScope`, `titleCaptionTranslation`, `createDate`, …), `body.tagTranslation` |
| GET | `/ajax/dashboard/works/novel?offset=0&limit=200` | List my novels (same shape; the thumbnails key is `novel`) |
| GET | `/ajax/illust/{illust_id}` | Illust detail: `title`, `description`, `xRestrict`, `isOriginal`, `isLoginOnly`, `createDate`, `pageCount`, `width`/`height`, `illustType`, … |
| GET | `/ajax/illust/{illust_id}/uploadedImages` | The originally uploaded images (by page order): `url`, `mimeType`, `byteLength` |
| GET | `/ajax/novel/{novel_id}` | Novel detail: `title`, `content` (full text), `genre`, `language`, `wordCount`, `characterCount`, `tags`, … |

### Import

| Method | Path | Description |
| --- | --- | --- |
| GET | `/illustration/create` | The create page (HTML), embedding the CSRF token |
| GET | `/ajax/work/create/illustration/init` | Pre-create check: non-empty `body.request` means a creation is already in progress; `body.showCaptcha` means a CAPTCHA is required right now |
| POST | `/ajax/work/create/illustration` | Create an illust (multipart/form-data); returns `body.convertKey` on success |
| GET | `/ajax/work/create/illustration/progress?convertKey=…` | Poll conversion progress: `body.status`; on `COMPLETE`, `body.illustId` is the new work id; `FAILED` / `ERROR` / `TIMEOUT` / `CANCELLED` are failures |

Create request field list (values per the field mapping table above):

```
files[]                              # original image files (by page order)
imageOrder[i][type] = newFile
imageOrder[i][fileKey] = i
title / titleTranslations[en]
caption / captionTranslations[en]
tags[]                               # repeated fields for multiple values; a single value is sent plain
allowTagEdit / sexual / allowComment / responseAutoAccept
xRestrict / aiType / restrict
original
ratings[violent|drug|thoughts|antisocial|religion]
gRecaptchaResponse                   # may be empty when no CAPTCHA is required
allowCitationWork
```

## Troubleshooting

- **Cloudflare challenge (403)**: the cookie expired — re-copy a fresh cookie
  over the file; a running script picks it up on its own.
- **Frequent 429 / 5xx**: increase `--delay`.
- **`captcha required for this account right now`**: the target account is
  being throttled — wait and retry, or use `--human` / `--no-solve`.
- **`a create request is already in progress`**: a creation is pending on the
  web — finish or cancel it in the browser first.
- **Image size mismatch on a work**: re-export that work (remove its entry
  from `state.json`, then re-run the export; `status=done` works are skipped
  by default).
