#!/usr/bin/env python3
"""Export all followed users (public & private) of a logged-in pixiv account.

Saves complete metadata (including user ID, username, visibility/restrict flags,
user follow tags, profile info, sample works, and raw API payloads) so followed
users can be accurately imported into another pixiv account with their original
public/private status and order preserved.

Standalone: works with standard library Python 3 (no third-party packages required).

Usage:
    python3 export_following.py --cookie-file cookie_a.txt
    python3 export_following.py --cookie-file cookie_a.txt --user-id 15032614 --delay 0.8
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

log = logging.getLogger("export_following")


class CookieProvider:
    """Reads the cookie from a file on every fetch and detects changes."""

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


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            req.full_url, code, f"Redirect to {newurl}", headers, fp
        )


class PixivFollowingClient:
    def __init__(
        self,
        cookies: CookieProvider,
        user_id: str,
        delay: float = 0.8,
        max_retries: int = 4,
        lang: str = "zh",
    ) -> None:
        self.cookies = cookies
        self.user_id = user_id
        self.delay = delay
        self.max_retries = max_retries
        self.lang = lang

        ctx = ssl.create_default_context()
        ctx.maximum_version = ssl.TLSVersion.TLSv1_2
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ctx),
            _NoRedirectHandler(),
        )

    def _headers(self, referer: str | None = None) -> dict[str, str]:
        return {
            "User-Agent": UA,
            "Cookie": self.cookies.get(),
            "x-user-id": self.user_id,
            "Referer": referer or f"{BASE}/users/{self.user_id}/following",
            "Accept": "application/json",
            "Accept-Language": "zh-CN,zh;q=0.9,ja;q=0.8,en;q=0.7",
        }

    def _jitter_sleep(self, base: float | None = None) -> None:
        d = self.delay if base is None else base
        if d > 0:
            time.sleep(d * random.uniform(0.7, 1.3))

    def get_json(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        referer: str | None = None,
    ) -> dict[str, Any]:
        url = BASE + path
        if params:
            url += "?" + urllib.parse.urlencode(params)

        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            req = urllib.request.Request(
                url, headers=self._headers(referer=referer), method="GET"
            )
            try:
                with self.opener.open(req, timeout=30) as resp:
                    status = resp.status
                    raw_text = resp.read().decode("utf-8", errors="replace")
            except urllib.error.HTTPError as e:
                status = e.code
                raw_text = e.read().decode("utf-8", errors="replace") if e.fp else ""
                if status in (301, 302, 303, 307, 308):
                    loc = e.headers.get("Location") if e.headers else None
                    raise PixivError(
                        f"HTTP {status} redirect to {loc} on {url}", status, raw_text[:300]
                    )
                if status in (429, 500, 502, 503, 504):
                    retry_after = e.headers.get("Retry-After") if e.headers else None
                    wait = float(retry_after) if retry_after else 5 * (2**attempt)
                    log.warning(
                        "HTTP %d on %s (attempt %d/%d), sleeping %.1fs",
                        status,
                        path,
                        attempt + 1,
                        self.max_retries + 1,
                        wait,
                    )
                    time.sleep(wait)
                    last_err = PixivError(f"HTTP {status}", status, raw_text[:200])
                    continue
                if status == 403 and "challenge" in raw_text[:2000].lower():
                    log.warning(
                        "Cloudflare challenge on %s - refresh cookie file if it persists",
                        path,
                    )
                    time.sleep(10)
                    last_err = PixivError("Cloudflare challenge", 403, raw_text[:200])
                    continue
                raise PixivError(f"HTTP {status} on {url}", status, raw_text[:500])
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
                    f"Non-JSON response on {url}: {e}", status, raw_text[:300]
                )

            if data.get("error"):
                msg = data.get("message", "?")
                if "不正なリクエスト" in msg or "不明なエラー" in msg:
                    last_err = PixivError(f"pixiv error: {msg}", 200, data)
                    log.warning(
                        "pixiv error on %s (attempt %d): %s",
                        path,
                        attempt + 1,
                        msg,
                    )
                    time.sleep(5 * (2**attempt))
                    continue
                raise PixivError(f"pixiv error on {path}: {msg}", 200, data)

            self._jitter_sleep()
            return data

        raise PixivError(f"retries exhausted on {path}: {last_err}")


def normalize_followed_user(
    u: dict[str, Any],
    visibility: str,
    order_index: int,
    user_tags: list[str],
) -> dict[str, Any]:
    rest = "show" if visibility == "public" else "hide"
    restrict_int = 0 if visibility == "public" else 1
    uid = str(u.get("userId") or u.get("user_id") or u.get("id") or "")
    uname = u.get("userName") or u.get("user_name") or u.get("name") or ""

    return {
        "user_id": uid,
        "userId": uid,
        "user_name": uname,
        "userName": uname,
        "visibility": visibility,
        "rest": rest,
        "restrict": restrict_int,
        "order_index": order_index,
        "user_follow_tags": user_tags,
        "profile_image_url": u.get("profileImageUrl") or u.get("profile_image_url"),
        "user_comment": u.get("userComment") or u.get("user_comment") or "",
        "following": bool(u.get("following", True)),
        "followed": bool(u.get("followed", False)),
        "is_blocking": bool(u.get("isBlocking", False)),
        "is_mypixiv": bool(u.get("isMypixiv", False)),
        "accept_request": bool(u.get("acceptRequest", False)),
        "illust_count_in_preview": len(u.get("illusts") or []),
        "novel_count_in_preview": len(u.get("novels") or []),
        "raw": u,
    }


def fetch_following_by_visibility(
    client: PixivFollowingClient,
    visibility: str,
    limit: int = 24,
) -> tuple[list[dict[str, Any]], int, list[str]]:
    """Fetch all followed users for a visibility ('public' or 'private')."""
    rest = "show" if visibility == "public" else "hide"
    referer = (
        f"{BASE}/users/{client.user_id}/following"
        if visibility == "public"
        else f"{BASE}/users/{client.user_id}/following?rest=hide"
    )

    raw_users: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    offset = 0
    reported_total = 0
    follow_user_tags: list[str] = []

    while True:
        d = client.get_json(
            f"/ajax/user/{client.user_id}/following",
            params={
                "offset": offset,
                "limit": limit,
                "rest": rest,
                "tag": "",
                "acceptingRequests": 0,
                "lang": client.lang,
            },
            referer=referer,
        )
        body = d.get("body") or {}
        batch = body.get("users") or []
        reported_total = int(body.get("total", reported_total))
        if offset == 0 and isinstance(body.get("followUserTags"), list):
            follow_user_tags = [t for t in body["followUserTags"] if t]

        for u in batch:
            uid = str(u.get("userId") or u.get("user_id") or u.get("id") or "")
            if uid and uid not in seen_ids:
                seen_ids.add(uid)
                raw_users.append(u)

        log.info(
            "following (%s / rest=%s): offset=%d got=%d (accumulated=%d / reported_total=%d)",
            visibility,
            rest,
            offset,
            len(batch),
            len(raw_users),
            reported_total,
        )
        offset += limit
        if offset >= reported_total or (not batch and offset > reported_total):
            break

    # If the user has custom follow tags, map each followed user to their tags
    user_tag_map: dict[str, list[str]] = {}
    for tag in follow_user_tags:
        t_offset = 0
        while True:
            d = client.get_json(
                f"/ajax/user/{client.user_id}/following",
                params={
                    "offset": t_offset,
                    "limit": limit,
                    "rest": rest,
                    "tag": tag,
                    "acceptingRequests": 0,
                    "lang": client.lang,
                },
                referer=referer,
            )
            body = d.get("body") or {}
            batch = body.get("users") or []
            for u in batch:
                uid = str(u.get("userId") or u.get("user_id") or u.get("id") or "")
                if uid and tag not in user_tag_map.setdefault(uid, []):
                    user_tag_map[uid].append(tag)
            t_total = int(body.get("total", 0))
            t_offset += limit
            if t_offset >= t_total or (not batch and t_offset > t_total):
                break

    normalized = [
        normalize_followed_user(
            u,
            visibility=visibility,
            order_index=idx,
            user_tags=user_tag_map.get(
                str(u.get("userId") or u.get("user_id") or u.get("id") or ""), []
            ),
        )
        for idx, u in enumerate(raw_users)
    ]
    return normalized, reported_total, follow_user_tags


def write_json_atomic(path: str, data: Any) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def main() -> int:
    p = argparse.ArgumentParser(
        description="Export all public & private followed users of a logged-in pixiv account"
    )
    p.add_argument(
        "--cookie-file",
        default="cookie_a.txt",
        help="file with browser Cookie header (default: cookie_a.txt)",
    )
    p.add_argument(
        "--user-id",
        default=None,
        help="target user ID (default: extracted from PHPSESSID in cookie file)",
    )
    p.add_argument(
        "--out",
        default="export",
        help="root output directory (default: export -> saves to export/<user_id>/)",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=24,
        help="page size per API request (default: 24)",
    )
    p.add_argument(
        "--delay",
        type=float,
        default=0.8,
        help="base delay between API calls in seconds (default: 0.8)",
    )
    p.add_argument("--max-retries", type=int, default=4)
    p.add_argument("--lang", default="zh", help="language parameter for API (default: zh)")
    args = p.parse_args()

    out_root = os.path.abspath(args.out)
    log_dir = os.path.join(out_root, "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "export_following.log")

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
    user_id = args.user_id or CookieProvider.user_id_from(cookie)
    if not user_id:
        log.error("cannot determine user_id from cookie, please pass --user-id")
        return 1

    account_dir = os.path.join(out_root, str(user_id))
    os.makedirs(account_dir, exist_ok=True)
    log.info("Exporting following list for user %s -> %s", user_id, account_dir)

    client = PixivFollowingClient(
        cookies=cookies,
        user_id=str(user_id),
        delay=args.delay,
        max_retries=args.max_retries,
        lang=args.lang,
    )

    public_users, public_total, public_tags = fetch_following_by_visibility(
        client, "public", limit=args.limit
    )
    private_users, private_total, private_tags = fetch_following_by_visibility(
        client, "private", limit=args.limit
    )

    summary = {
        "public": {
            "reported_total": public_total,
            "exported_count": len(public_users),
            "deactivated_or_unavailable_count": max(0, public_total - len(public_users)),
        },
        "private": {
            "reported_total": private_total,
            "exported_count": len(private_users),
            "deactivated_or_unavailable_count": max(0, private_total - len(private_users)),
        },
        "total_exported": len(public_users) + len(private_users),
    }

    combined_payload = {
        "schema_version": 1,
        "user_id": str(user_id),
        "exported_at": datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat(),
        "summary": summary,
        "follow_user_tags": {
            "public": public_tags,
            "private": private_tags,
        },
        "following": {
            "public": public_users,
            "private": private_users,
        },
    }

    following_json_path = os.path.join(account_dir, "following.json")
    write_json_atomic(following_json_path, combined_payload)
    write_json_atomic(
        os.path.join(account_dir, "following_public.json"), public_users
    )
    write_json_atomic(
        os.path.join(account_dir, "following_private.json"), private_users
    )

    log.info(
        "DONE! Saved to %s | Public following: %d (reported=%d) | Private following: %d (reported=%d) | Total: %d",
        following_json_path,
        len(public_users),
        public_total,
        len(private_users),
        private_total,
        len(public_users) + len(private_users),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
