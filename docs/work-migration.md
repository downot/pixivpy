# Account & Work Migration (Works, Bookmarks, Following)

Standalone tools to migrate a Pixiv account's **published works**, **bookmarks**
(public & private, illusts & novels), and **followed users** (public & private)
to another account.

| File | Purpose | Dependencies |
| --- | --- | --- |
| `export_works.py` | Export all published works (illusts + novels) and download original images | `python3` + `requests` |
| `upload_works.py` | Re-upload exported illusts to a target account | `python3` + `requests` (+ `node`/Chrome for CAPTCHA) |
| `export_bookmarks.py` | Export all public & private bookmarks (illusts + novels + user bookmark tags) | `python3` stdlib only |
| `import_bookmarks.py` | Import exported bookmarks into a target account (preserving public/private & order) | `python3` stdlib only |
| `export_following.py` | Export all public & private followed users (including custom follow tags) | `python3` stdlib only |
| `import_following.py` | Import exported followed users into a target account via AppAPI (`user_follow_add`) | `python3` stdlib only |
| `captcha_solver.mjs` | Automatic reCAPTCHA solver for work creation (CDP + LLM, headless) | `node` + Chrome |
| `captcha_human.mjs` | Human-in-the-loop reCAPTCHA solver (visible window, records labeled samples) | `node` + Chrome |

The tools primarily use the **web AJAX API** (`https://www.pixiv.net/ajax/...`)
with browser `Cookie` authentication (`cookie_a.txt` for source, `cookie_b.txt`
for target), while `import_following.py` uses Pixiv's **OAuth App-API**
(`https://app-api.pixiv.net/v1/user/follow/add` via `token_b.txt`) because the
web follow endpoint requires a per-request Google reCAPTCHA Enterprise token.
All APIs are summarized in [API Reference](#new-web-apis).

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

## Bookmarks Migration (export_bookmarks.py / import_bookmarks.py)

Export all public (`rest=show` / `restrict=0`) and private (`rest=hide` / `restrict=1`)
bookmarks (both **illusts/manga** and **novels**, plus custom bookmark tags) from
the source account and import them into the target account. Both scripts rely
only on the Python 3 standard library (`urllib.request`).

### Export Bookmarks: `export_bookmarks.py`

```bash
# Export all public & private bookmarks (illusts + novels)
python3 export_bookmarks.py --cookie-file cookie_a.txt

# Only update/merge novel bookmarks into existing bookmarks.json
python3 export_bookmarks.py --cookie-file cookie_a.txt --only-novels
```

| Option | Default | Description |
| --- | --- | --- |
| `--cookie-file` | `cookie_a.txt` | source account cookie file |
| `--user-id` | from PHPSESSID | source account user id |
| `--out` | `export` | output root directory (`export/<user_id>/`) |
| `--delay` | `0.8` | base delay between API calls (s, ±30% jitter) |
| `--max-retries` | `4` | max retries per request |
| `--skip-novels` | off | skip novel bookmarks |
| `--only-novels` | off | only fetch novel bookmarks and merge into existing `bookmarks.json` |

Output files under `export/<user_id>/`:
- `bookmarks.json` — full combined payload (`summary`, `bookmark_tags`, `illusts.public`, `illusts.private`, `novels.public`, `novels.private`)
- `bookmarks_illusts_public.json` / `bookmarks_illusts_private.json`
- `bookmarks_novels_public.json` / `bookmarks_novels_private.json`

Works that were deleted or made private by their original authors are flagged with
`"is_deleted_or_inaccessible": true` and automatically skipped during import.

### Import Bookmarks: `import_bookmarks.py`

```bash
# Test importing 1 bookmark first (oldest first, so final order matches source)
python3 import_bookmarks.py --cookie-file cookie_b.txt --source export/15032614 --limit 1

# Import all remaining bookmarks with human-like pacing
python3 import_bookmarks.py --cookie-file cookie_b.txt --source export/15032614

# Dry-run or filter by type / visibility
python3 import_bookmarks.py --cookie-file cookie_b.txt --source export/15032614 --type illust --visibility private --dry-run
```

| Option | Default | Description |
| --- | --- | --- |
| `--cookie-file` | `cookie_b.txt` | target account cookie file |
| `--source` | `export/15032614` | export directory or `bookmarks.json` path |
| `--type` | `all` | `all`, `illust`, or `novel` |
| `--visibility` | `all` | `all`, `public`, or `private` |
| `--order` | `oldest-first` | `oldest-first` (preserves original bookmark list order) or `newest-first` |
| `--limit` | `0` | max bookmarks to import in this run (`0` = all pending) |
| `--only` | none | only import a specific `work_id` |
| `--min-delay` / `--max-delay` | `3.5` / `7.5` | random human-like interval range between requests (s) |
| `--pause-every` | `20` | take a longer break every N imported bookmarks (`0` = disable) |
| `--pause-min` / `--pause-max` | `15.0` / `30.0` | duration range of periodic breaks (s) |
| `--dry-run` | off | preview items to import without calling API |

Progress is tracked in `export/<source_uid>/import_bookmarks_state_<target_uid>.json`
and resumes automatically across runs.

---

## Following Migration (export_following.py / import_following.py)

Export all public (`rest=show`) and private (`rest=hide`) followed users from the
source account and follow them on the target account. Both scripts rely only on
the Python 3 standard library.

### Export Following: `export_following.py`

```bash
python3 export_following.py --cookie-file cookie_a.txt
```

| Option | Default | Description |
| --- | --- | --- |
| `--cookie-file` | `cookie_a.txt` | source account cookie file |
| `--user-id` | from PHPSESSID | source account user id |
| `--out` | `export` | output root directory (`export/<user_id>/`) |
| `--limit` | `24` | page size per request (`offset` advances by `limit` up to `reported_total`) |
| `--delay` | `0.8` | base delay between API calls (s, ±30% jitter) |

Output files under `export/<user_id>/`:
- `following.json` — full combined payload (`summary`, `follow_user_tags`, `following.public`, `following.private`)
- `following_public.json` / `following_private.json`

> Note: Pixiv's `/ajax/user/{uid}/following` `total` count includes deactivated/suspended
> accounts, which Pixiv filters out of the per-page `users[]` array. `export_following.py`
> advances `offset += limit` until `offset >= reported_total` so all active followed
> users are exported.

### Import Following: `import_following.py`

Because Pixiv's web follow endpoint (`/bookmark_add.php`) requires a per-request
Google reCAPTCHA Enterprise score token (`recaptcha_enterprise_score_token`),
`import_following.py` uses Pixiv's official OAuth **App-API**
(`POST https://app-api.pixiv.net/v1/user/follow/add`, matching
`AppPixivAPI.user_follow_add` in `pixivpy3/aapi.py`).

Save the target account's OAuth token in `token_b.txt` (plain `refresh_token` string
or JSON `{"access_token": "...", "refresh_token": "..."}`, ignored by git):

```bash
# Test importing 1 followed user first
python3 import_following.py --token-file token_b.txt --source export/15032614 --limit 1

# Import all remaining followed users with human-like pacing
python3 import_following.py --token-file token_b.txt --source export/15032614

# Or pass token directly on CLI
python3 import_following.py --refresh-token <REFRESH_TOKEN> --source export/15032614
```

| Option | Default | Description |
| --- | --- | --- |
| `--token-file` | `token_b.txt` | file with target account `refresh_token` or OAuth JSON |
| `--refresh-token` / `--access-token` | none | pass OAuth tokens directly via CLI |
| `--source` | `export/15032614` | export directory or `following.json` path |
| `--visibility` | `all` | `all`, `public`, or `private` |
| `--order` | `oldest-first` | `oldest-first` (preserves original following list order) or `newest-first` |
| `--limit` | `0` | max users to follow in this run (`0` = all pending) |
| `--only` | none | only follow a specific `user_id` |
| `--min-delay` / `--max-delay` | `3.5` / `7.5` | random human-like interval range between requests (s) |
| `--pause-every` | `20` | take a longer break every N followed users (`0` = disable) |
| `--pause-min` / `--pause-max` | `15.0` / `30.0` | duration range of periodic breaks (s) |
| `--dry-run` | off | preview users to follow without calling API |

Progress is tracked in `export/<source_uid>/import_following_state_<target_uid>.json`
and resumes automatically across runs.

---

## New web APIs

Base URL: `https://www.pixiv.net` (and `https://app-api.pixiv.net` for App-API follow import).

Common request headers: `cookie`, `x-user-id`, `referer` (the page the request
originates from), `accept: application/json`; state-changing web requests
additionally need `x-csrf-token` (a 32-hex token embedded in Pixiv HTML pages).

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
| GET | `/ajax/user/{user_id}/illusts/bookmark/tags?lang=zh` | User's illust bookmark tags (`body.public[]`, `body.private[]`) |
| GET | `/ajax/user/{user_id}/illusts/bookmarks?tag=&offset=0&limit=48&rest=show\|hide` | User's public (`rest=show`) or private (`rest=hide`) illust bookmarks (`body.works[]`, `body.total`) |
| GET | `/ajax/user/{user_id}/novels/bookmark/tags?lang=zh` | User's novel bookmark tags (`body.public[]`, `body.private[]`) |
| GET | `/touch/ajax/user/bookmarks?id={user_id}&type=novel&rest=show\|hide&p=1` | User's public or private novel bookmarks (`body.works[]` / `body.bookmarks[]`, `body.total`) |
| GET | `/ajax/user/{user_id}/following?offset=0&limit=24&rest=show\|hide&tag=&acceptingRequests=0` | User's public (`rest=show`) or private (`rest=hide`) followed users (`body.users[]`, `body.total`, `body.followUserTags[]`) |

### Import

| Method | Path | Description |
| --- | --- | --- |
| GET | `/illustration/create` | The create page (HTML), embedding the CSRF token |
| GET | `/ajax/work/create/illustration/init` | Pre-create check: non-empty `body.request` means a creation is already in progress; `body.showCaptcha` means a CAPTCHA is required right now |
| POST | `/ajax/work/create/illustration` | Create an illust (multipart/form-data); returns `body.convertKey` on success |
| GET | `/ajax/work/create/illustration/progress?convertKey=…` | Poll conversion progress: `body.status`; on `COMPLETE`, `body.illustId` is the new work id; `FAILED` / `ERROR` / `TIMEOUT` / `CANCELLED` are failures |
| POST | `/ajax/illusts/bookmarks/add` | Add illust bookmark (`application/json`: `{"illust_id": str, "restrict": 0\|1, "comment": str, "tags": list[str]}`); returns `body.last_bookmark_id` |
| POST | `/ajax/novels/bookmarks/add` | Add novel bookmark (`application/json`: `{"novel_id": str, "restrict": 0\|1, "comment": str, "tags": list[str]}`); returns `body` (new bookmark id) |
| POST | `https://app-api.pixiv.net/v1/user/follow/add` | Follow a user via OAuth App-API (`Bearer <access_token>`, form body `user_id=<id>&restrict=public\|private`) |

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
