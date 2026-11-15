#!/usr/bin/env python3
"""Import exported pixiv followed users (public & private) into a target account via Pixiv AppAPI.

Uses Pixiv's official AppAPI (`POST https://app-api.pixiv.net/v1/user/follow/add`,
corresponding to `AppPixivAPI.user_follow_add` in `pixivpy3/aapi.py`) authenticated
with OAuth `refresh_token` or `access_token`.

Features:
- Standard library Python 3 only (implements `pixivpy3`'s exact OAuth `auth()` and
  `user_follow_add()` protocol without requiring external packages).
- Supports `--refresh-token`, `--access-token`, or `--token-file` (e.g. `token_b.txt`).
- Automatically exchanges `refresh_token` for `access_token` and refreshes on expiry.
- Preserves public (`restrict="public"`) and private (`restrict="private"`) visibility.
- Imports from oldest to newest by default (`--order oldest-first`) so the final
  following list order on the target account matches the source account.
- Resumable state saved in `<source>/import_following_state_<target_uid>.json`.
- Human-like random intervals between requests (`--min-delay` to `--max-delay`)
  plus periodic pauses every N items (`--pause-every`).

Usage:
    # Test importing 1 followed user first:
    python3 import_following.py --refresh-token <TOKEN> --source export/15032614 --limit 1
    # Or from a token file:
    python3 import_following.py --token-file token_b.txt --source export/15032614 --limit 1

    # Import all remaining followed users:
    python3 import_following.py --token-file token_b.txt --source export/15032614
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import logging
import os
import random
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

OAUTH_URL = "https://oauth.secure.pixiv.net/auth/token"
APP_API_HOST = "https://app-api.pixiv.net"

# Credentials & headers from pixivpy3/api.py and pixivpy3/aapi.py
CLIENT_ID = "MOBrBDS8blbauoSck0ZfDbtuzpyT"
CLIENT_SECRET = "lsACyCD94FhDUtGTXi3QzcFE2uU1hqtDaKeqrdwj"
HASH_SECRET = "28c1fdd170a5204386cb1313c7077b34f83e4aaf4aa829ce78c231e05b0bae2c"
APP_OS = "ios"
APP_OS_VERSION = "14.6"
APP_UA = "PixivIOSApp/7.13.3 (iOS 14.6; iPhone13,2)"

log = logging.getLogger("import_following")


class PixivError(Exception):
    def __init__(self, msg: str, status: int | None = None, body: Any = None) -> None:
        super().__init__(msg)
        self.status = status
        self.body = body


class ImportFollowingState:
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

    def is_done(self, follow_user_id: str) -> bool:
        entry = self.data["items"].get(str(follow_user_id))
        return bool(entry and entry.get("status") in ("done", "skipped"))

    def mark(self, follow_user_id: str, entry: dict[str, Any]) -> None:
        now = datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat()
        entry["updated_at"] = now
        self.data["updated_at"] = now
        self.data["items"][str(follow_user_id)] = entry
        self.save()

    def save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)


class AppPixivFollowImporter:
    """Implements pixivpy3.AppPixivAPI.auth() and user_follow_add() via standard library."""

    def __init__(
        self,
        refresh_token: str | None = None,
        access_token: str | None = None,
        user_id: str | None = None,
        min_delay: float = 3.5,
        max_delay: float = 7.5,
        max_retries: int = 4,
    ) -> None:
        self.refresh_token = refresh_token
        self.access_token = access_token
        self.user_id = str(user_id) if user_id else None
        self.user_name: str | None = None
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.max_retries = max_retries

        ctx = ssl.create_default_context()
        ctx.maximum_version = ssl.TLSVersion.TLSv1_2
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ctx)
        )

    def _app_headers(self, with_auth: bool = True) -> dict[str, str]:
        h = {
            "app-os": APP_OS,
            "app-os-version": APP_OS_VERSION,
            "user-agent": APP_UA,
            "Accept-Language": "zh-cn",
        }
        if with_auth:
            if not self.access_token:
                raise PixivError("access_token is not set; call auth() first")
            h["Authorization"] = f"Bearer {self.access_token}"
        return h

    def human_sleep(self) -> None:
        if self.max_delay <= 0:
            return
        low = max(0.0, min(self.min_delay, self.max_delay))
        high = max(self.min_delay, self.max_delay)
        wait = random.triangular(low, high, low + (high - low) * 0.45)
        log.info("sleeping %.2fs (human-like interval)...", wait)
        time.sleep(wait)

    def auth(self) -> dict[str, Any]:
        """Exchange refresh_token for access_token (pixivpy3.BasePixivAPI.auth)."""
        if not self.refresh_token:
            raise PixivError("refresh_token is required for auth()")

        local_time = datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S+00:00"
        )
        client_hash = hashlib.md5(
            (local_time + HASH_SECRET).encode("utf-8")
        ).hexdigest()

        headers = self._app_headers(with_auth=False)
        headers["x-client-time"] = local_time
        headers["x-client-hash"] = client_hash
        headers["Content-Type"] = "application/x-www-form-urlencoded"

        data = urllib.parse.urlencode(
            {
                "get_secure_url": 1,
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
            }
        ).encode("utf-8")

        req = urllib.request.Request(OAUTH_URL, data=data, headers=headers, method="POST")
        try:
            with self.opener.open(req, timeout=30) as resp:
                raw_text = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace") if e.fp else ""
            raise PixivError(
                f"OAuth auth() failed! HTTP {e.code}: {err_body[:400]}",
                e.code,
                err_body,
            )

        token_data = json.loads(raw_text)
        response_obj = token_data.get("response") or token_data
        user_obj = response_obj.get("user") or {}
        self.access_token = response_obj.get("access_token")
        if response_obj.get("refresh_token"):
            self.refresh_token = response_obj["refresh_token"]
        if user_obj.get("id"):
            self.user_id = str(user_obj["id"])
        self.user_name = user_obj.get("name")

        log.info(
            "AppAPI OAuth success! Authenticated as user_id=%s (%s)",
            self.user_id,
            self.user_name,
        )
        return token_data

    def user_follow_add(
        self, follow_user_id: int | str, restrict: str = "public"
    ) -> dict[str, Any]:
        """POST https://app-api.pixiv.net/v1/user/follow/add (pixivpy3.AppPixivAPI.user_follow_add)."""
        if not self.access_token:
            self.auth()

        url = f"{APP_API_HOST}/v1/user/follow/add"
        body_bytes = urllib.parse.urlencode(
            {
                "user_id": str(follow_user_id),
                "restrict": restrict,
            }
        ).encode("utf-8")

        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            headers = self._app_headers(with_auth=True)
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            req = urllib.request.Request(url, data=body_bytes, headers=headers, method="POST")
            try:
                with self.opener.open(req, timeout=30) as resp:
                    raw_text = resp.read().decode("utf-8", errors="replace")
                    return json.loads(raw_text) if raw_text.strip() else {}
            except urllib.error.HTTPError as e:
                status = e.code
                raw_text = e.read().decode("utf-8", errors="replace") if e.fp else ""
                # Token expired -> refresh and retry once
                if status in (400, 401, 403) and "invalid_grant" not in raw_text and self.refresh_token and attempt == 0:
                    if "Error occurred at the OAuth process" in raw_text or status == 401:
                        log.info("Access token expired, refreshing via refresh_token...")
                        self.auth()
                        continue
                if status in (429, 500, 502, 503, 504):
                    wait = 8 * (2**attempt)
                    log.warning(
                        "HTTP %d on /v1/user/follow/add (attempt %d/%d), sleeping %.1fs: %s",
                        status,
                        attempt + 1,
                        self.max_retries + 1,
                        wait,
                        raw_text[:200],
                    )
                    time.sleep(wait)
                    last_err = PixivError(f"HTTP {status}: {raw_text[:300]}", status, raw_text[:300])
                    continue
                raise PixivError(
                    f"HTTP {status} on /v1/user/follow/add: {raw_text[:400]}",
                    status,
                    raw_text[:400],
                )
            except Exception as e:
                last_err = e
                log.warning(
                    "network error on /v1/user/follow/add (attempt %d/%d): %s",
                    attempt + 1,
                    self.max_retries + 1,
                    e,
                )
                time.sleep(4 * (2**attempt))

        raise PixivError(f"retries exhausted on /v1/user/follow/add: {last_err}")


def load_token_from_file(path: str) -> tuple[str | None, str | None]:
    """Read refresh_token (or JSON with refresh_token/access_token) from file."""
    with open(path, encoding="utf-8") as f:
        raw = f.read().strip()
    if raw.startswith("{"):
        d = json.loads(raw)
        if "response" in d and isinstance(d["response"], dict):
            d = d["response"]
        return d.get("refresh_token"), d.get("access_token")
    return raw.strip('"').strip("'"), None


def build_follow_queue(
    following_data: dict[str, Any],
    visibility_filter: str,
    order: str,
    only_id: str | None = None,
) -> list[dict[str, Any]]:
    queue: list[dict[str, Any]] = []
    vis_keys = (
        ["public", "private"]
        if visibility_filter == "all"
        else [visibility_filter]
    )
    following_group = following_data.get("following") or {}
    for vkey in vis_keys:
        items = list(following_group.get(vkey) or [])
        if order == "oldest-first":
            items.reverse()
        queue.extend(items)

    if only_id:
        queue = [x for x in queue if str(x.get("user_id")) == str(only_id)]

    return queue


def main() -> int:
    p = argparse.ArgumentParser(
        description="Import exported pixiv followed users via Pixiv AppAPI (user_follow_add)"
    )
    p.add_argument(
        "--refresh-token",
        default=None,
        help="Target account OAuth refresh_token",
    )
    p.add_argument(
        "--access-token",
        default=None,
        help="Target account OAuth access_token (optional if refresh-token is given)",
    )
    p.add_argument(
        "--token-file",
        default="token_b.txt",
        help="File containing target account refresh_token or OAuth JSON (default: token_b.txt)",
    )
    p.add_argument(
        "--user-id",
        default=None,
        help="Target account user ID (auto-detected when using refresh_token)",
    )
    p.add_argument(
        "--source",
        default="export/15032614",
        help="export directory or following.json path (default: export/15032614)",
    )
    p.add_argument(
        "--visibility",
        choices=["all", "public", "private"],
        default="all",
        help="which following visibility to import (default: all)",
    )
    p.add_argument(
        "--order",
        choices=["oldest-first", "newest-first"],
        default="oldest-first",
        help="import order; oldest-first preserves original following list order (default: oldest-first)",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=0,
        help="max number of users to follow in this run (0 = no limit)",
    )
    p.add_argument(
        "--only",
        default=None,
        help="only follow a specific user_id",
    )
    p.add_argument(
        "--min-delay",
        type=float,
        default=3.5,
        help="minimum delay in seconds between follow requests (default: 3.5)",
    )
    p.add_argument(
        "--max-delay",
        type=float,
        default=7.5,
        help="maximum delay in seconds between follow requests (default: 7.5)",
    )
    p.add_argument(
        "--pause-every",
        type=int,
        default=20,
        help="take a longer human-like break every N followed users (default: 20, 0=disable)",
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
    p.add_argument("--dry-run", action="store_true", help="print what would be followed without calling API")
    args = p.parse_args()

    source_path = os.path.abspath(args.source)
    if os.path.isdir(source_path):
        source_dir = source_path
        following_file = os.path.join(source_dir, "following.json")
    else:
        following_file = source_path
        source_dir = os.path.dirname(following_file)

    if not os.path.exists(following_file):
        print(f"ERROR: following file not found: {following_file}", file=sys.stderr)
        return 1

    log_dir = os.path.join(source_dir, "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "import_following.log")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_path, encoding="utf-8"),
        ],
    )

    refresh_token = args.refresh_token
    access_token = args.access_token
    if not refresh_token and not access_token:
        if os.path.exists(args.token_file):
            refresh_token, access_token = load_token_from_file(args.token_file)
        elif not args.dry_run:
            log.error(
                "Please provide --refresh-token, --access-token, or create %s",
                args.token_file,
            )
            return 1

    importer = AppPixivFollowImporter(
        refresh_token=refresh_token,
        access_token=access_token,
        user_id=args.user_id,
        min_delay=args.min_delay,
        max_delay=args.max_delay,
        max_retries=args.max_retries,
    )

    if not args.dry_run and not access_token and refresh_token:
        importer.auth()

    target_uid = importer.user_id or args.user_id or "131078705"

    with open(following_file, encoding="utf-8") as f:
        following_data = json.load(f)

    source_uid = str(following_data.get("user_id", "unknown"))
    state_path = os.path.join(source_dir, f"import_following_state_{target_uid}.json")
    state = ImportFollowingState(
        state_path, source_user_id=source_uid, target_user_id=str(target_uid)
    )

    queue = build_follow_queue(
        following_data,
        visibility_filter=args.visibility,
        order=args.order,
        only_id=args.only,
    )

    pending: list[dict[str, Any]] = []
    skipped_done = 0
    for item in queue:
        fuid = str(item["user_id"])
        if fuid == str(target_uid):
            state.mark(
                fuid,
                {
                    "status": "skipped",
                    "visibility": item.get("visibility"),
                    "restrict": item.get("restrict"),
                    "user_name": item.get("user_name"),
                    "reason": "cannot follow self",
                },
            )
            continue
        if state.is_done(fuid) and not args.only:
            skipped_done += 1
            continue
        pending.append(item)

    log.info(
        "Source user=%s -> Target user=%s | Total in scope=%d | Already done=%d | Pending=%d (order=%s)",
        source_uid,
        target_uid,
        len(queue),
        skipped_done,
        len(pending),
        args.order,
    )

    if not pending:
        log.info("Nothing left to follow!")
        return 0

    to_process = pending[: args.limit] if args.limit > 0 else pending
    log.info("Will follow %d user(s) in this run.", len(to_process))

    if args.dry_run:
        for i, item in enumerate(to_process, 1):
            vis = "public" if item.get("visibility") == "public" else "private"
            log.info(
                "[DRY-RUN %d/%d] user %s (restrict=%s) name=%r",
                i,
                len(to_process),
                item.get("user_id"),
                vis,
                item.get("user_name"),
            )
        return 0

    followed_count = 0
    errors: list[str] = []

    for i, item in enumerate(to_process, 1):
        fuid = str(item["user_id"])
        uname = item.get("user_name") or ""
        vis = "public" if item.get("visibility") == "public" else "private"

        try:
            res = importer.user_follow_add(fuid, restrict=vis)
            state.mark(
                fuid,
                {
                    "status": "done",
                    "visibility": vis,
                    "restrict": item.get("restrict"),
                    "user_name": uname,
                    "source_order_index": item.get("order_index"),
                    "response": res,
                },
            )
            followed_count += 1
            log.info(
                "[%d/%d] OK user %s (restrict=%s) | %s",
                i,
                len(to_process),
                fuid,
                vis,
                uname,
            )
        except Exception as e:
            errors.append(fuid)
            state.mark(
                fuid,
                {
                    "status": "error",
                    "visibility": vis,
                    "restrict": item.get("restrict"),
                    "user_name": uname,
                    "error": str(e),
                },
            )
            log.error(
                "[%d/%d] FAILED user %s (restrict=%s): %s (%s)",
                i,
                len(to_process),
                fuid,
                vis,
                e,
                uname,
            )

        if i < len(to_process):
            if args.pause_every > 0 and followed_count > 0 and followed_count % args.pause_every == 0:
                pause_sec = random.uniform(args.pause_min, args.pause_max)
                log.info(
                    "Reached %d followed users — taking a human-like break for %.1fs...",
                    followed_count,
                    pause_sec,
                )
                time.sleep(pause_sec)
            else:
                importer.human_sleep()

    log.info(
        "RUN COMPLETE: followed=%d/%d, errors=%s | State file: %s",
        followed_count,
        len(to_process),
        errors or "none",
        state_path,
    )
    return 0 if not errors else 2


if __name__ == "__main__":
    sys.exit(main())
