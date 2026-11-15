#!/usr/bin/env python3
"""Import exported pixiv bookmarks (public & private, illusts & novels) into a target account.

Reads `export/<source_uid>/bookmarks.json` (created by `export_bookmarks.py`)
and adds each bookmark to the target account (authenticated via `--cookie-file`,
default `cookie_b.txt`), preserving public (`restrict=0`) and private (`restrict=1`)
visibility, user bookmark tags, and bookmark comments.

Features:
- Standard library Python 3 only (no third-party packages required).
- Hot-reloads the cookie file on every request so cookies can be refreshed live.
- Resumable state saved in `<source>/import_bookmarks_state_<target_uid>.json`.
- Imports from oldest to newest by default (`--order oldest-first`) so the final
  bookmark order on the target account matches the source account.
- Human-like random intervals between requests (`--min-delay` to `--max-delay`)
  plus periodic browsing pauses every N items (`--pause-every`).

Usage:
    # Test importing 1 bookmark first:
    python3 import_bookmarks.py --cookie-file cookie_b.txt --source export/15032614 --limit 1

    # Import all remaining bookmarks with human-like pacing:
    python3 import_bookmarks.py --cookie-file cookie_b.txt --source export/15032614
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import logging
import os
import random
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

BASE = "https://www.pixiv.net"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

log = logging.getLogger("import_bookmarks")


class CookieProvider:
    """Reads the cookie from a file on every request and detects updates."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._hash: str | None = None
        self._cookie = ""

    @staticmethod
    def _parse(text: str) -> str:
        m = re.search(r'"cookie":\s*"([^"]+)"', text)
        if m:
            return m.group(1)
        return text.strip().strip('"')

    def get(self) -> str:
        with open(self.path, encoding="utf-8") as f:
            raw = f.read()
        h = hashlib.sha256(raw.encode()).hexdigest()
        if h != self._hash:
            self._cookie = self._parse(raw)
            self._hash = h
            log.info("cookie loaded/updated (sha256=%s...)", h[:12])
        return self._cookie

    @staticmethod
    def user_id_from(cookie: str) -> str | None:
        m = re.search(r"PHPSESSID=(\d+)_", cookie)
        return m.group(1) if m else None


class PixivError(Exception):
    def __init__(self, msg: str, status: int | None = None, body: Any = None) -> None:
        super().__init__(msg)
        self.status = status
        self.body = body


class ImportState:
    def __init__(self, path: str, source_user_id: str, target_user_id: str) -> None:
        self.path = path
        self.data: dict[str, Any] = {
            "source_user_id": source_user_id,
            "target_user_id": target_user_id,
            "updated_at": None,
            "items": {},
        }
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                loaded = json.load(f)
                if isinstance(loaded, dict) and "items" in loaded:
                    self.data = loaded

    @staticmethod
    def key_for(work_type: str, work_id: str) -> str:
        return f"{work_type}:{work_id}"

    def is_done(self, work_type: str, work_id: str) -> bool:
        entry = self.data["items"].get(self.key_for(work_type, work_id))
        return bool(entry and entry.get("status") in ("done", "skipped_inaccessible"))

    def mark(self, work_type: str, work_id: str, entry: dict[str, Any]) -> None:
        now = datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat()
        entry["updated_at"] = now
        self.data["updated_at"] = now
        self.data["items"][self.key_for(work_type, work_id)] = entry
        self.save()

    def save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)


class PixivBookmarkImporter:
    def __init__(
        self,
        cookies: CookieProvider,
        user_id: str,
        min_delay: float = 3.5,
        max_delay: float = 7.5,
        max_retries: int = 4,
    ) -> None:
        self.cookies = cookies
        self.user_id = user_id
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.max_retries = max_retries
        self.csrf: str | None = None

        ctx = ssl.create_default_context()
        ctx.maximum_version = ssl.TLSVersion.TLSv1_2
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ctx)
        )

    def _headers(
        self,
        referer: str | None = None,
        accept: str = "application/json",
        content_type: str | None = None,
        with_csrf: bool = False,
    ) -> dict[str, str]:
        h = {
            "User-Agent": UA,
            "Cookie": self.cookies.get(),
            "x-user-id": self.user_id,
            "Referer": referer or f"{BASE}/",
            "Origin": BASE,
            "Accept": accept,
            "Accept-Language": "zh-CN,zh;q=0.9,ja;q=0.8,en;q=0.7",
        }
        if content_type:
            h["Content-Type"] = content_type
        if with_csrf and self.csrf:
            h["x-csrf-token"] = self.csrf
        return h

    def human_sleep(self) -> None:
        if self.max_delay <= 0:
            return
        low = max(0.0, min(self.min_delay, self.max_delay))
        high = max(self.min_delay, self.max_delay)
        # Triangular distribution centered slightly below midpoint feels natural
        wait = random.triangular(low, high, low + (high - low) * 0.45)
        log.info("sleeping %.2fs (human-like interval)...", wait)
        time.sleep(wait)

    def refresh_csrf(self, sample_illust_id: str | None = None) -> str:
        """Fetch CSRF token from a Pixiv page using target account's cookie."""
        candidates_urls = [
            f"{BASE}/artworks/{sample_illust_id}" if sample_illust_id else None,
            f"{BASE}/users/{self.user_id}/bookmarks/artworks",
            f"{BASE}/illustration/create",
            f"{BASE}/",
        ]
        last_err: Exception | None = None
        for page_url in [u for u in candidates_urls if u]:
            req = urllib.request.Request(
                page_url,
                headers=self._headers(
                    referer=f"{BASE}/",
                    accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                ),
                method="GET",
            )
            try:
                with self.opener.open(req, timeout=30) as resp:
                    html = resp.read().decode("utf-8", errors="replace")
            except Exception as e:
                last_err = e
                log.warning("failed to fetch %s for csrf: %s", page_url, e)
                continue

            # Match token in Next.js __NEXT_DATA__ or global init config
            for pat in (
                r'token\\?":\\?"([0-9a-f]{32})',
                r'"postKey"\s*:\s*"([0-9a-f]{32})"',
                r'csrfToken\\?":\\?"([0-9a-f]{32})',
            ):
                m = re.search(pat, html)
                if m:
                    self.csrf = m.group(1)
                    log.info("obtained csrf token: %s (from %s)", self.csrf, page_url)
                    return self.csrf

        raise PixivError(
            f"unable to extract x-csrf-token (check if cookie_b.txt is logged in): {last_err}"
        )

    def post_json(
        self, path: str, payload: dict[str, Any], referer: str
    ) -> dict[str, Any]:
        if not self.csrf:
            self.refresh_csrf()

        url = BASE + path
        body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        last_err: Exception | None = None

        for attempt in range(self.max_retries + 1):
            req = urllib.request.Request(
                url,
                data=body_bytes,
                headers=self._headers(
                    referer=referer,
                    accept="application/json",
                    content_type="application/json; charset=utf-8",
                    with_csrf=True,
                ),
                method="POST",
            )
            try:
                with self.opener.open(req, timeout=30) as resp:
                    status = resp.status
                    raw_text = resp.read().decode("utf-8", errors="replace")
            except urllib.error.HTTPError as e:
                status = e.code
                raw_text = e.read().decode("utf-8", errors="replace") if e.fp else ""
                if status in (429, 500, 502, 503, 504):
                    retry_after = e.headers.get("Retry-After") if e.headers else None
                    wait = float(retry_after) if retry_after else 8 * (2**attempt)
                    log.warning(
                        "HTTP %d on %s (attempt %d/%d), sleeping %.1fs",
                        status,
                        path,
                        attempt + 1,
                        self.max_retries + 1,
                        wait,
                    )
                    time.sleep(wait)
                    last_err = PixivError(f"HTTP {status}", status, raw_text[:300])
                    continue
                if status == 403 and "challenge" in raw_text[:2000].lower():
                    log.warning(
                        "Cloudflare challenge on %s - refresh cookie_b.txt if it persists",
                        path,
                    )
                    time.sleep(12)
                    last_err = PixivError("Cloudflare challenge", 403, raw_text[:200])
                    continue
                if status in (400, 403) and attempt == 0:
                    log.info("HTTP %d on %s, refreshing CSRF token and retrying...", status, path)
                    try:
                        self.refresh_csrf()
                    except Exception:
                        pass
                    time.sleep(2)
                    last_err = PixivError(f"HTTP {status}: {raw_text[:300]}", status, raw_text[:300])
                    continue
                raise PixivError(
                    f"HTTP {status} on {path}: {raw_text[:300]}", status, raw_text[:300]
                )
            except Exception as e:
                last_err = e
                log.warning(
                    "network error on %s (attempt %d/%d): %s",
                    path,
                    attempt + 1,
                    self.max_retries + 1,
                    e,
                )
                time.sleep(4 * (2**attempt))
                continue

            try:
                data = json.loads(raw_text)
            except json.JSONDecodeError as e:
                raise PixivError(
                    f"Non-JSON response on {path}: {e}", status, raw_text[:300]
                )

            if data.get("error"):
                msg = data.get("message", "?")
                if ("csrf" in msg.lower() or "token" in msg.lower() or "不正なリクエスト" in msg) and attempt == 0:
                    log.info("Pixiv error %r on %s, refreshing CSRF token...", msg, path)
                    self.refresh_csrf()
                    time.sleep(2)
                    last_err = PixivError(f"pixiv error: {msg}", 200, data)
                    continue
                raise PixivError(f"pixiv error on {path}: {msg}", 200, data)

            return data

        raise PixivError(f"retries exhausted on {path}: {last_err}")

    def add_bookmark(self, item: dict[str, Any]) -> dict[str, Any]:
        work_type = item.get("work_type", "illust")
        work_id = str(item["work_id"])
        restrict = int(item.get("restrict", 0 if item.get("visibility") == "public" else 1))
        comment = item.get("user_bookmark_comment") or ""
        tags = item.get("user_bookmark_tags") or []

        if work_type == "illust":
            path = "/ajax/illusts/bookmarks/add"
            referer = f"{BASE}/artworks/{work_id}"
            payload = {
                "illust_id": work_id,
                "restrict": restrict,
                "comment": comment,
                "tags": tags,
            }
        elif work_type == "novel":
            path = "/ajax/novels/bookmarks/add"
            referer = f"{BASE}/novel/show.php?id={work_id}"
            payload = {
                "novel_id": work_id,
                "restrict": restrict,
                "comment": comment,
                "tags": tags,
            }
        else:
            raise ValueError(f"Unsupported work_type: {work_type}")

        res = self.post_json(path, payload, referer=referer)
        return res.get("body") or {}


def build_work_queue(
    bookmarks_data: dict[str, Any],
    work_type_filter: str,
    visibility_filter: str,
    order: str,
    only_id: str | None = None,
) -> list[dict[str, Any]]:
    """Build the ordered queue of bookmark items to import."""
    queue: list[dict[str, Any]] = []

    type_keys = (
        ["illusts", "novels"]
        if work_type_filter == "all"
        else ["illusts" if work_type_filter == "illust" else "novels"]
    )
    vis_keys = (
        ["public", "private"]
        if visibility_filter == "all"
        else [visibility_filter]
    )

    for tkey in type_keys:
        group = bookmarks_data.get(tkey) or {}
        for vkey in vis_keys:
            items = list(group.get(vkey) or [])
            if order == "oldest-first":
                items.reverse()
            queue.extend(items)

    if only_id:
        queue = [x for x in queue if str(x.get("work_id")) == str(only_id)]

    return queue


def main() -> int:
    p = argparse.ArgumentParser(
        description="Import exported pixiv bookmarks (public & private) into a target account"
    )
    p.add_argument(
        "--cookie-file",
        default="cookie_b.txt",
        help="target account cookie file (default: cookie_b.txt)",
    )
    p.add_argument(
        "--user-id",
        default=None,
        help="target account user ID (default: extracted from PHPSESSID in cookie file)",
    )
    p.add_argument(
        "--source",
        default="export/15032614",
        help="export directory or bookmarks.json path (default: export/15032614)",
    )
    p.add_argument(
        "--type",
        choices=["all", "illust", "novel"],
        default="all",
        help="which work types to import (default: all)",
    )
    p.add_argument(
        "--visibility",
        choices=["all", "public", "private"],
        default="all",
        help="which bookmark visibility to import (default: all)",
    )
    p.add_argument(
        "--order",
        choices=["oldest-first", "newest-first"],
        default="oldest-first",
        help="import order; oldest-first preserves original bookmark list order (default: oldest-first)",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=0,
        help="max number of bookmarks to import in this run (0 = no limit)",
    )
    p.add_argument(
        "--only",
        default=None,
        help="only import a specific work_id",
    )
    p.add_argument(
        "--min-delay",
        type=float,
        default=3.5,
        help="minimum delay in seconds between bookmark requests (default: 3.5)",
    )
    p.add_argument(
        "--max-delay",
        type=float,
        default=7.5,
        help="maximum delay in seconds between bookmark requests (default: 7.5)",
    )
    p.add_argument(
        "--pause-every",
        type=int,
        default=20,
        help="take a longer human-like break every N imported bookmarks (default: 20, 0=disable)",
    )
    p.add_argument(
        "--pause-min",
        type=float,
        default=15.0,
        help="minimum duration of periodic break in seconds (default: 15.0)",
    )
    p.add_argument(
        "--pause-max",
        type=float,
        default=30.0,
        help="maximum duration of periodic break in seconds (default: 30.0)",
    )
    p.add_argument("--max-retries", type=int, default=4)
    p.add_argument("--dry-run", action="store_true", help="print what would be imported without calling API")
    args = p.parse_args()

    source_path = os.path.abspath(args.source)
    if os.path.isdir(source_path):
        source_dir = source_path
        bookmarks_file = os.path.join(source_dir, "bookmarks.json")
    else:
        bookmarks_file = source_path
        source_dir = os.path.dirname(bookmarks_file)

    if not os.path.exists(bookmarks_file):
        print(f"ERROR: bookmarks file not found: {bookmarks_file}", file=sys.stderr)
        return 1

    log_dir = os.path.join(source_dir, "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "import_bookmarks.log")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_path, encoding="utf-8"),
        ],
    )

    cookies = CookieProvider(args.cookie_file)
    cookie = cookies.get()
    target_uid = args.user_id or CookieProvider.user_id_from(cookie)
    if not target_uid:
        log.error("cannot determine target user_id from %s, please pass --user-id", args.cookie_file)
        return 1

    with open(bookmarks_file, encoding="utf-8") as f:
        bookmarks_data = json.load(f)

    source_uid = str(bookmarks_data.get("user_id", "unknown"))
    state_path = os.path.join(source_dir, f"import_bookmarks_state_{target_uid}.json")
    state = ImportState(state_path, source_user_id=source_uid, target_user_id=str(target_uid))

    queue = build_work_queue(
        bookmarks_data,
        work_type_filter=args.type,
        visibility_filter=args.visibility,
        order=args.order,
        only_id=args.only,
    )

    # First mark any deleted/inaccessible works so they are recorded and skipped cleanly
    pending: list[dict[str, Any]] = []
    skipped_done = 0
    skipped_inaccessible = 0
    for item in queue:
        wtype = item.get("work_type", "illust")
        wid = str(item["work_id"])
        if state.is_done(wtype, wid) and not args.only:
            skipped_done += 1
            continue
        if item.get("is_deleted_or_inaccessible"):
            skipped_inaccessible += 1
            state.mark(
                wtype,
                wid,
                {
                    "status": "skipped_inaccessible",
                    "visibility": item.get("visibility"),
                    "restrict": item.get("restrict"),
                    "title": item.get("title"),
                    "reason": "work deleted or private/masked on source",
                },
            )
            continue
        pending.append(item)

    log.info(
        "Source user=%s -> Target user=%s | Total in scope=%d | Already done=%d | Inaccessible skipped=%d | Pending=%d (order=%s)",
        source_uid,
        target_uid,
        len(queue),
        skipped_done,
        skipped_inaccessible,
        len(pending),
        args.order,
    )

    if not pending:
        log.info("Nothing left to import!")
        return 0

    to_process = pending[: args.limit] if args.limit > 0 else pending
    log.info("Will import %d bookmark(s) in this run.", len(to_process))

    if args.dry_run:
        for i, item in enumerate(to_process, 1):
            log.info(
                "[DRY-RUN %d/%d] %s %s (%s / restrict=%s) title=%r user=%r",
                i,
                len(to_process),
                item.get("work_type"),
                item.get("work_id"),
                item.get("visibility"),
                item.get("restrict"),
                item.get("title"),
                item.get("userName"),
            )
        return 0

    importer = PixivBookmarkImporter(
        cookies=cookies,
        user_id=str(target_uid),
        min_delay=args.min_delay,
        max_delay=args.max_delay,
        max_retries=args.max_retries,
    )

    # Obtain CSRF token upfront using the first illust ID if available
    first_illust_id = next(
        (str(x["work_id"]) for x in to_process if x.get("work_type") == "illust"),
        "140372199",
    )
    importer.refresh_csrf(sample_illust_id=first_illust_id)

    imported_count = 0
    errors: list[str] = []

    for i, item in enumerate(to_process, 1):
        wtype = item.get("work_type", "illust")
        wid = str(item["work_id"])
        vis = item.get("visibility")
        restrict = item.get("restrict")
        title = (item.get("title") or "")[:45]

        try:
            res_body = importer.add_bookmark(item)
            new_bmid = None
            if isinstance(res_body, dict):
                new_bmid = res_body.get("last_bookmark_id") or res_body.get("id")
            elif isinstance(res_body, (str, int)):
                new_bmid = str(res_body)

            state.mark(
                wtype,
                wid,
                {
                    "status": "done",
                    "visibility": vis,
                    "restrict": restrict,
                    "title": item.get("title"),
                    "source_order_index": item.get("order_index"),
                    "new_bookmark_id": new_bmid,
                },
            )
            imported_count += 1
            log.info(
                "[%d/%d] OK %s %s (%s / restrict=%s) -> new_bookmark_id=%s | %s",
                i,
                len(to_process),
                wtype,
                wid,
                vis,
                restrict,
                new_bmid,
                title,
            )
        except Exception as e:
            errors.append(f"{wtype}:{wid}")
            state.mark(
                wtype,
                wid,
                {
                    "status": "error",
                    "visibility": vis,
                    "restrict": restrict,
                    "title": item.get("title"),
                    "error": str(e),
                },
            )
            log.error(
                "[%d/%d] FAILED %s %s (%s): %s (%s)",
                i,
                len(to_process),
                wtype,
                wid,
                vis,
                e,
                title,
            )

        # Sleep only if there are more items to process in this run
        if i < len(to_process):
            if args.pause_every > 0 and imported_count > 0 and imported_count % args.pause_every == 0:
                pause_sec = random.uniform(args.pause_min, args.pause_max)
                log.info(
                    "Reached %d imported items — taking a human-like break for %.1fs...",
                    imported_count,
                    pause_sec,
                )
                time.sleep(pause_sec)
            else:
                importer.human_sleep()

    log.info(
        "RUN COMPLETE: imported=%d/%d, errors=%s | State file: %s",
        imported_count,
        len(to_process),
        errors or "none",
        state_path,
    )
    return 0 if not errors else 2


if __name__ == "__main__":
    sys.exit(main())
