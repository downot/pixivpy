#!/usr/bin/env python3
"""Export all bookmarks (public & private, illusts & novels) of a logged-in pixiv account.

Saves complete metadata (including bookmark IDs, visibility/restrict flags,
user bookmark tags, work metadata, and raw API payloads) so bookmarks can be
accurately imported into another pixiv account with their original public/private
status and order preserved.

Standalone: works with standard library Python 3 (no third-party packages required).

Usage:
    python3 export_bookmarks.py --cookie-file cookie_a.txt
    python3 export_bookmarks.py --cookie-file cookie_a.txt --user-id 15032614 --delay 0.8
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
UNCATEGORIZED_TAGS = {"未分類", "未分类", "Uncategorized"}

log = logging.getLogger("export_bookmarks")


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


class PixivBookmarksClient:
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

        # Pin to TLS 1.2 because LibreSSL on macOS can be flaky with pixiv TLS 1.3
        ctx = ssl.create_default_context()
        ctx.maximum_version = ssl.TLSVersion.TLSv1_2
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ctx),
            _NoRedirectHandler(),
        )

    def _headers(self, referer: str | None = None, mobile: bool = False) -> dict[str, str]:
        ua = (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
            if mobile
            else UA
        )
        return {
            "User-Agent": ua,
            "Cookie": self.cookies.get(),
            "x-user-id": self.user_id,
            "Referer": referer or f"{BASE}/users/{self.user_id}/bookmarks/artworks",
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
        mobile: bool = False,
    ) -> dict[str, Any]:
        url = BASE + path
        if params:
            url += "?" + urllib.parse.urlencode(params)

        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            req = urllib.request.Request(
                url, headers=self._headers(referer=referer, mobile=mobile), method="GET"
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
                    raise PixivError(f"HTTP {status} redirect to {loc} on {url}", status, raw_text[:300])
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


def fetch_bookmark_tags(client: PixivBookmarksClient, kind: str) -> dict[str, Any]:
    """kind is 'illusts' or 'novels'."""
    path = f"/ajax/user/{client.user_id}/{kind}/bookmark/tags"
    try:
        res = client.get_json(path, params={"lang": client.lang})
        return res.get("body") or {"public": [], "private": []}
    except PixivError as e:
        log.warning("failed to fetch %s bookmark tags: %s", kind, e)
        return {"public": [], "private": []}


def extract_custom_tags(tags_body: dict[str, Any], visibility: str) -> list[str]:
    """Return user's custom bookmark tag names (excluding '未分類')."""
    entries = tags_body.get(visibility) or []
    custom: list[str] = []
    for item in entries:
        t = item.get("tag")
        if t and t not in UNCATEGORIZED_TAGS:
            custom.append(t)
    return custom


def normalize_illust_bookmark(
    work: dict[str, Any],
    visibility: str,
    order_index: int,
    user_tags: list[str],
) -> dict[str, Any]:
    rest = "show" if visibility == "public" else "hide"
    restrict_int = 0 if visibility == "public" else 1
    wid = str(work.get("id"))
    bdata = work.get("bookmarkData") or {}
    is_bookmarkable = bool(work.get("isBookmarkable", True))
    is_masked = bool(work.get("isMasked", False))
    title = work.get("title") or ""
    is_deleted_or_inaccessible = (not is_bookmarkable) or is_masked or (title == "-----" and not work.get("userId"))

    return {
        "work_id": wid,
        "id": wid,
        "work_type": "illust",
        "visibility": visibility,
        "rest": rest,
        "restrict": restrict_int,
        "order_index": order_index,
        "bookmark_id": str(bdata.get("id")) if bdata.get("id") is not None else None,
        "bookmark_private": bool(bdata.get("private", visibility == "private")),
        "user_bookmark_tags": user_tags,
        "user_bookmark_comment": work.get("bookmarkComment", ""),
        "is_bookmarkable": is_bookmarkable,
        "is_deleted_or_inaccessible": is_deleted_or_inaccessible,
        "title": title,
        "illustType": work.get("illustType"),
        "xRestrict": work.get("xRestrict"),
        "work_restrict": work.get("restrict"),
        "sl": work.get("sl"),
        "url": work.get("url"),
        "description": work.get("description"),
        "tags": work.get("tags") or [],
        "userId": str(work.get("userId")) if work.get("userId") is not None else None,
        "userName": work.get("userName"),
        "pixivId": work.get("pixivId"),
        "width": work.get("width"),
        "height": work.get("height"),
        "pageCount": work.get("pageCount"),
        "alt": work.get("alt"),
        "titleCaptionTranslation": work.get("titleCaptionTranslation"),
        "createDate": work.get("createDate"),
        "updateDate": work.get("updateDate"),
        "isUnlisted": work.get("isUnlisted"),
        "isMasked": is_masked,
        "aiType": work.get("aiType"),
        "visibilityScope": work.get("visibilityScope"),
        "profileImageUrl": work.get("profileImageUrl"),
        "bookmarkData": bdata,
        "raw": work,
    }


def normalize_novel_bookmark(
    work: dict[str, Any],
    visibility: str,
    order_index: int,
    user_tags: list[str],
) -> dict[str, Any]:
    rest = "show" if visibility == "public" else "hide"
    restrict_int = 0 if visibility == "public" else 1
    wid = str(work.get("id"))
    bdata = work.get("bookmarkData") or {}
    bookmark_id = bdata.get("id") or work.get("bookmark_id")
    bookmark_private = bdata.get(
        "private",
        str(work.get("bookmark_restrict", restrict_int)) == "1"
        if work.get("bookmark_restrict") is not None
        else (visibility == "private"),
    )
    user_id = work.get("userId") if work.get("userId") is not None else work.get("user_id")
    user_name = work.get("userName") or work.get("user_name")
    is_bookmarkable = bool(work.get("isBookmarkable", not work.get("is_private", False)))
    is_masked = bool(work.get("isMasked", False))
    title = work.get("title") or ""
    is_deleted_or_inaccessible = (
        (not is_bookmarkable)
        or is_masked
        or (title in ("", "-----") and not user_id)
    )

    return {
        "work_id": wid,
        "id": wid,
        "work_type": "novel",
        "visibility": visibility,
        "rest": rest,
        "restrict": restrict_int,
        "order_index": order_index,
        "bookmark_id": str(bookmark_id) if bookmark_id is not None else None,
        "bookmark_private": bool(bookmark_private),
        "user_bookmark_tags": user_tags,
        "user_bookmark_comment": work.get("bookmarkComment", ""),
        "is_bookmarkable": is_bookmarkable,
        "is_deleted_or_inaccessible": is_deleted_or_inaccessible,
        "title": title,
        "xRestrict": work.get("xRestrict") if work.get("xRestrict") is not None else work.get("x_restrict"),
        "work_restrict": work.get("restrict"),
        "url": work.get("url"),
        "description": work.get("description") if work.get("description") is not None else work.get("comment"),
        "tags": work.get("tags") or [],
        "userId": str(user_id) if user_id is not None else None,
        "userName": user_name,
        "wordCount": work.get("wordCount") if work.get("wordCount") is not None else work.get("word_count"),
        "textCount": work.get("textCount") if work.get("textCount") is not None else (work.get("text_length") or work.get("character_count")),
        "readingTime": work.get("readingTime") if work.get("readingTime") is not None else work.get("reading_time"),
        "useWordCount": work.get("useWordCount") if work.get("useWordCount") is not None else work.get("use_word_count"),
        "isOriginal": work.get("isOriginal") if work.get("isOriginal") is not None else work.get("is_original"),
        "isBungei": work.get("isBungei"),
        "createDate": work.get("createDate") or work.get("upload_timestamp"),
        "updateDate": work.get("updateDate") or work.get("reupload_timestamp"),
        "isUnlisted": work.get("isUnlisted"),
        "isMasked": is_masked,
        "aiType": work.get("aiType") if work.get("aiType") is not None else work.get("ai_type"),
        "seriesId": work.get("seriesId") or work.get("series_id"),
        "seriesTitle": work.get("seriesTitle") or work.get("series_title"),
        "profileImageUrl": work.get("profileImageUrl") or work.get("profile_image_url"),
        "bookmarkData": bdata or ({"id": str(bookmark_id), "private": bool(bookmark_private)} if bookmark_id else {}),
        "raw": work,
    }


def fetch_illust_bookmarks_by_visibility(
    client: PixivBookmarksClient,
    visibility: str,
    custom_tags: list[str],
    limit: int = 48,
) -> tuple[list[dict[str, Any]], int]:
    """Fetch all illust bookmarks for a visibility ('public' or 'private')."""
    rest = "show" if visibility == "public" else "hide"
    referer = (
        f"{BASE}/users/{client.user_id}/bookmarks/artworks"
        if visibility == "public"
        else f"{BASE}/users/{client.user_id}/bookmarks/artworks?rest=hide"
    )

    # Map work_id -> list of user custom bookmark tags if the account uses custom tags
    work_custom_tags: dict[str, list[str]] = {}
    for tag in custom_tags:
        t_offset = 0
        while True:
            d = client.get_json(
                f"/ajax/user/{client.user_id}/illusts/bookmarks",
                params={
                    "tag": tag,
                    "offset": t_offset,
                    "limit": limit,
                    "rest": rest,
                    "lang": client.lang,
                },
                referer=referer,
            )
            body = d.get("body") or {}
            batch = body.get("works") or []
            for w in batch:
                wid = str(w.get("id"))
                work_custom_tags.setdefault(wid, []).append(tag)
            t_total = body.get("total", 0)
            t_offset += len(batch)
            if not batch or t_offset >= t_total or len(batch) < limit:
                break

    raw_works: list[dict[str, Any]] = []
    offset = 0
    reported_total = 0
    while True:
        d = client.get_json(
            f"/ajax/user/{client.user_id}/illusts/bookmarks",
            params={
                "tag": "",
                "offset": offset,
                "limit": limit,
                "rest": rest,
                "lang": client.lang,
            },
            referer=referer,
        )
        body = d.get("body") or {}
        batch = body.get("works") or []
        reported_total = body.get("total", reported_total)
        raw_works.extend(batch)
        log.info(
            "illust bookmarks (%s / rest=%s): offset=%d got=%d (accumulated=%d / total=%d)",
            visibility,
            rest,
            offset,
            len(batch),
            len(raw_works),
            reported_total,
        )
        if not batch or len(raw_works) >= reported_total or len(batch) < limit:
            break
        offset += len(batch)

    normalized = [
        normalize_illust_bookmark(
            w,
            visibility=visibility,
            order_index=idx,
            user_tags=work_custom_tags.get(str(w.get("id")), []),
        )
        for idx, w in enumerate(raw_works)
    ]
    return normalized, reported_total


def fetch_novel_bookmarks_by_visibility(
    client: PixivBookmarksClient,
    visibility: str,
    custom_tags: list[str],
    expected_count: int | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Fetch all novel bookmarks for a visibility ('public' or 'private')."""
    if expected_count == 0:
        log.info("novel bookmarks (%s): tag count is 0, skipping", visibility)
        return [], 0

    rest = "show" if visibility == "public" else "hide"
    referer = (
        f"{BASE}/users/{client.user_id}/bookmarks/novels"
        if visibility == "public"
        else f"{BASE}/users/{client.user_id}/bookmarks/novels?rest=hide"
    )

    # Use touch AJAX endpoint /touch/ajax/user/bookmarks?id=<uid>&type=novel&rest=<show|hide>&p=<page>
    # (Desktop /ajax/user/<uid>/novels/bookmarks returns SPA HTML without internal headers)
    raw_works: list[dict[str, Any]] = []
    reported_total = 0
    page = 1
    while True:
        try:
            d = client.get_json(
                "/touch/ajax/user/bookmarks",
                params={
                    "id": client.user_id,
                    "type": "novel",
                    "rest": rest,
                    "p": page,
                    "lang": client.lang,
                },
                referer=referer,
                mobile=True,
            )
        except PixivError as e:
            log.warning("touch novel bookmarks failed on page %d: %s", page, e)
            break
        body = d.get("body") or {}
        batch = (
            body.get("works")
            or body.get("bookmarks")
            or body.get("novels")
            or []
        )
        novel_map = {}
        if isinstance(body.get("novels"), dict):
            novel_map = body["novels"]
        elif isinstance(body.get("thumbnails"), dict) and isinstance(body["thumbnails"].get("novel"), list):
            novel_map = {str(n.get("id")): n for n in body["thumbnails"]["novel"]}
        enriched_batch = []
        for item in batch:
            if isinstance(item, dict):
                wid = str(item.get("work_id") or item.get("novel_id") or item.get("id") or "")
                merged = dict(novel_map.get(wid, {}))
                merged.update(item)
                if "id" not in merged and wid:
                    merged["id"] = wid
                enriched_batch.append(merged)
        batch = enriched_batch
        reported_total = int(body.get("total") or len(batch))
        raw_works.extend(batch)
        log.info(
            "novel bookmarks (%s / rest=%s): page=%d got=%d (accumulated=%d / total=%d)",
            visibility,
            rest,
            page,
            len(batch),
            len(raw_works),
            reported_total,
        )
        if not batch or len(raw_works) >= reported_total:
            break
        page += 1

    normalized = [
        normalize_novel_bookmark(
            w,
            visibility=visibility,
            order_index=idx,
            user_tags=[],
        )
        for idx, w in enumerate(raw_works)
    ]
    return normalized, reported_total


def write_json_atomic(path: str, data: Any) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def summarize_items(items: list[dict[str, Any]], reported_total: int) -> dict[str, int]:
    bookmarkable = sum(1 for x in items if not x.get("is_deleted_or_inaccessible"))
    inaccessible = len(items) - bookmarkable
    return {
        "reported_total": reported_total,
        "exported_count": len(items),
        "bookmarkable_count": bookmarkable,
        "deleted_or_inaccessible_count": inaccessible,
    }


def main() -> int:
    p = argparse.ArgumentParser(
        description="Export all public & private bookmarks (illusts + novels) of a logged-in pixiv account"
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
        "--delay",
        type=float,
        default=0.8,
        help="base delay between API calls in seconds (default: 0.8)",
    )
    p.add_argument("--max-retries", type=int, default=4)
    p.add_argument("--lang", default="zh", help="language parameter for API (default: zh)")
    p.add_argument("--skip-novels", action="store_true", help="skip novel bookmarks")
    p.add_argument("--only-novels", action="store_true", help="only fetch novel bookmarks and merge into existingbookmarks.json")
    args = p.parse_args()

    out_root = os.path.abspath(args.out)
    log_dir = os.path.join(out_root, "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "export_bookmarks.log")

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
    log.info("Exporting bookmarks for user %s -> %s", user_id, account_dir)

    client = PixivBookmarksClient(
        cookies=cookies,
        user_id=str(user_id),
        delay=args.delay,
        max_retries=args.max_retries,
        lang=args.lang,
    )

    bookmarks_json_path = os.path.join(account_dir, "bookmarks.json")
    existing: dict[str, Any] = {}
    if args.only_novels and os.path.exists(bookmarks_json_path):
        with open(bookmarks_json_path, encoding="utf-8") as f:
            existing = json.load(f)

    if args.only_novels and existing.get("illusts"):
        illust_tags_body = existing.get("bookmark_tags", {}).get("illusts", {"public": [], "private": []})
        illusts_public = existing["illusts"].get("public", [])
        illusts_private = existing["illusts"].get("private", [])
        illusts_public_total = existing.get("summary", {}).get("illusts", {}).get("public", {}).get("reported_total", len(illusts_public))
        illusts_private_total = existing.get("summary", {}).get("illusts", {}).get("private", {}).get("reported_total", len(illusts_private))
        log.info("Reusing existing illust bookmarks: public=%d, private=%d", len(illusts_public), len(illusts_private))
    else:
        # 1. Fetch bookmark tags (illusts + novels)
        illust_tags_body = fetch_bookmark_tags(client, "illusts")
        illust_pub_custom_tags = extract_custom_tags(illust_tags_body, "public")
        illust_priv_custom_tags = extract_custom_tags(illust_tags_body, "private")
        log.info(
            "Illust bookmark tags: public=%s (custom=%s), private=%s (custom=%s)",
            illust_tags_body.get("public"),
            illust_pub_custom_tags,
            illust_tags_body.get("private"),
            illust_priv_custom_tags,
        )

        # 2. Fetch public & private illust bookmarks
        illusts_public, illusts_public_total = fetch_illust_bookmarks_by_visibility(
            client, "public", illust_pub_custom_tags
        )
        illusts_private, illusts_private_total = fetch_illust_bookmarks_by_visibility(
            client, "private", illust_priv_custom_tags
        )

    # 3. Fetch novel bookmark tags & bookmarks (if not skipped)
    novel_tags_body: dict[str, Any] = {"public": [], "private": []}
    novels_public: list[dict[str, Any]] = []
    novels_public_total = 0
    novels_private: list[dict[str, Any]] = []
    novels_private_total = 0

    if not args.skip_novels:
        novel_tags_body = fetch_bookmark_tags(client, "novels")
        novel_pub_custom_tags = extract_custom_tags(novel_tags_body, "public")
        novel_priv_custom_tags = extract_custom_tags(novel_tags_body, "private")
        pub_tag_cnt = sum(int(x.get("cnt", 0)) for x in (novel_tags_body.get("public") or []))
        priv_tag_cnt = sum(int(x.get("cnt", 0)) for x in (novel_tags_body.get("private") or []))
        log.info(
            "Novel bookmark tags: public=%s (sum=%d), private=%s (sum=%d)",
            novel_tags_body.get("public"),
            pub_tag_cnt,
            novel_tags_body.get("private"),
            priv_tag_cnt,
        )
        novels_public, novels_public_total = fetch_novel_bookmarks_by_visibility(
            client, "public", novel_pub_custom_tags, expected_count=pub_tag_cnt
        )
        novels_private, novels_private_total = fetch_novel_bookmarks_by_visibility(
            client, "private", novel_priv_custom_tags, expected_count=priv_tag_cnt
        )

    summary = {
        "illusts": {
            "public": summarize_items(illusts_public, illusts_public_total),
            "private": summarize_items(illusts_private, illusts_private_total),
            "total_exported": len(illusts_public) + len(illusts_private),
        },
        "novels": {
            "public": summarize_items(novels_public, novels_public_total),
            "private": summarize_items(novels_private, novels_private_total),
            "total_exported": len(novels_public) + len(novels_private),
        },
    }

    combined_payload = {
        "schema_version": 1,
        "user_id": str(user_id),
        "exported_at": datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat(),
        "summary": summary,
        "bookmark_tags": {
            "illusts": illust_tags_body,
            "novels": novel_tags_body,
        },
        "illusts": {
            "public": illusts_public,
            "private": illusts_private,
        },
        "novels": {
            "public": novels_public,
            "private": novels_private,
        },
    }

    # Save combined file + split files for convenience
    bookmarks_json_path = os.path.join(account_dir, "bookmarks.json")
    write_json_atomic(bookmarks_json_path, combined_payload)
    write_json_atomic(
        os.path.join(account_dir, "bookmarks_illusts_public.json"), illusts_public
    )
    write_json_atomic(
        os.path.join(account_dir, "bookmarks_illusts_private.json"), illusts_private
    )
    if not args.skip_novels:
        write_json_atomic(
            os.path.join(account_dir, "bookmarks_novels_public.json"), novels_public
        )
        write_json_atomic(
            os.path.join(account_dir, "bookmarks_novels_private.json"), novels_private
        )

    log.info(
        "DONE! Saved to %s | Illusts: public=%d (bookmarkable=%d), private=%d (bookmarkable=%d) | Novels: public=%d, private=%d",
        bookmarks_json_path,
        len(illusts_public),
        summary["illusts"]["public"]["bookmarkable_count"],
        len(illusts_private),
        summary["illusts"]["private"]["bookmarkable_count"],
        len(novels_public),
        len(novels_private),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
