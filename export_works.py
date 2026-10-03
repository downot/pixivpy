#!/usr/bin/env python3
"""Export all works (illusts + novels) of a logged-in pixiv account.

Downloads metadata + original images so works can later be re-uploaded to
another account. Standalone: python3 + requests only (no pydantic).

The cookie file is re-read on every request, so it can be refreshed while
the export is running without restarting.

Usage:
    python3 export_works.py --cookie-file cookie_a.txt
    python3 export_works.py --cookie-file cookie_a.txt --user-id 15032614 --delay 1.0
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import random
import re
import ssl
import sys
import time
from typing import Any

import requests
from requests.adapters import HTTPAdapter

BASE = "https://www.pixiv.net"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
MIME_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}
ILLUST_DETAIL_KEEP = [
    "title",
    "description",
    "isOriginal",
    "isUnlisted",
    "isLoginOnly",
    "restrict",
    "xRestrict",
    "createDate",
    "uploadDate",
    "reuploadDate",
    "pageCount",
    "width",
    "height",
    "illustType",
    "seriesNavData",
]
NOVEL_DETAIL_KEEP = [
    "title",
    "description",
    "content",
    "isOriginal",
    "isUnlisted",
    "isLoginOnly",
    "restrict",
    "xRestrict",
    "createDate",
    "uploadDate",
    "genre",
    "language",
    "wordCount",
    "characterCount",
    "useWordCount",
    "seriesNavData",
    "tags",
]


def make_session() -> requests.Session:
    # TLS 1.3 handshakes with pixiv are flaky on LibreSSL; pin to TLS 1.2.
    ctx = ssl.create_default_context()
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2

    class Tls12Adapter(HTTPAdapter):
        def init_poolmanager(self, connections, maxsize, block=False, **kw):
            kw["ssl_context"] = ctx
            return super().init_poolmanager(connections, maxsize, block, **kw)

    s = requests.Session()
    s.mount("https://", Tls12Adapter(pool_connections=4, pool_maxsize=8))
    s.headers["User-Agent"] = UA
    return s


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


class PixivWeb:
    def __init__(
        self,
        session: requests.Session,
        cookies: CookieProvider,
        user_id: str,
        delay: float,
        max_retries: int = 4,
    ) -> None:
        self.session = session
        self.cookies = cookies
        self.user_id = user_id
        self.delay = delay
        self.max_retries = max_retries

    def _headers(self, referer: str | None = None) -> dict[str, str]:
        return {
            "cookie": self.cookies.get(),
            "x-user-id": self.user_id,
            "referer": referer or "https://www.pixiv.net/dashboard/works",
            "accept": "application/json",
        }

    def _jitter_sleep(self, base: float) -> None:
        time.sleep(base * random.uniform(0.7, 1.3))

    def get_json(self, path: str, params: dict | None = None) -> dict:
        url = BASE + path
        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                r = self.session.get(
                    url,
                    params=params,
                    headers=self._headers(),
                    timeout=30,
                )
            except (requests.ConnectionError, requests.SSLError, requests.Timeout) as e:
                last_err = e
                log.warning(
                    "network error %s (attempt %d/%d): %s",
                    path,
                    attempt + 1,
                    self.max_retries + 1,
                    e,
                )
                time.sleep(5 * (2**attempt))
                continue
            if r.status_code in (429, 500, 502, 503, 504):
                retry_after = r.headers.get("Retry-After")
                wait = float(retry_after) if retry_after else 5 * (2**attempt)
                log.warning(
                    "HTTP %d on %s (attempt %d), sleeping %.0fs",
                    r.status_code,
                    path,
                    attempt + 1,
                    wait,
                )
                time.sleep(wait)
                last_err = PixivError(f"HTTP {r.status_code}", r.status_code, r.text[:200])
                continue
            if r.status_code == 403 and "challenge" in r.text[:2000].lower():
                log.warning(
                    "Cloudflare challenge on %s - refresh cookie file if it persists",
                    path,
                )
                time.sleep(10)
                last_err = PixivError("Cloudflare challenge", 403, r.text[:200])
                continue
            if r.status_code != 200:
                raise PixivError(
                    f"HTTP {r.status_code} on {path}", r.status_code, r.text[:500]
                )
            d = r.json()
            if d.get("error"):
                msg = d.get("message", "?")
                if "不正なリクエスト" in msg or "不明なエラー" in msg:
                    last_err = PixivError(f"pixiv error: {msg}", 200, d)
                    log.warning(
                        "pixiv error on %s (attempt %d): %s",
                        path,
                        attempt + 1,
                        msg,
                    )
                    time.sleep(5 * (2**attempt))
                    continue
                raise PixivError(f"pixiv error on {path}: {msg}", 200, d)
            self._jitter_sleep(self.delay)
            return d
        raise PixivError(f"retries exhausted on {path}: {last_err}")

    def download(
        self, url: str, dest: str, expected_bytes: int | None = None
    ) -> int:
        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                r = self.session.get(
                    url,
                    headers={
                        **self._headers("https://www.pixiv.net/"),
                    },
                    timeout=120,
                    stream=True,
                )
                if r.status_code in (429, 500, 502, 503, 504):
                    wait = 5 * (2**attempt)
                    log.warning(
                        "HTTP %d downloading %s (attempt %d), sleeping %.0fs",
                        r.status_code,
                        url[:80],
                        attempt + 1,
                        wait,
                    )
                    time.sleep(wait)
                    last_err = PixivError(f"HTTP {r.status_code}", r.status_code)
                    continue
                if r.status_code != 200:
                    raise PixivError(
                        f"HTTP {r.status_code} downloading {url[:100]}",
                        r.status_code,
                    )
                tmp = dest + ".part"
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1 << 16):
                        f.write(chunk)
                size = os.path.getsize(tmp)
                if expected_bytes is not None and size != expected_bytes:
                    os.remove(tmp)
                    last_err = PixivError(
                        f"size mismatch {size} != {expected_bytes} for {url[:100]}"
                    )
                    log.warning(
                        "size mismatch (attempt %d): %s", attempt + 1, last_err
                    )
                    time.sleep(5 * (2**attempt))
                    continue
                os.replace(tmp, dest)
                return size
            except (requests.ConnectionError, requests.SSLError, requests.Timeout) as e:
                last_err = e
                log.warning(
                    "download network error %s (attempt %d/%d): %s",
                    url[:80],
                    attempt + 1,
                    self.max_retries + 1,
                    e,
                )
                time.sleep(5 * (2**attempt))
        raise PixivError(f"download failed {url[:100]}: {last_err}")


class State:
    def __init__(self, path: str) -> None:
        self.path = path
        self.data: dict[str, Any] = {"illusts": {}, "novels": {}}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                self.data = json.load(f)

    def save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def mark_illust(self, work_id: str, entry: dict) -> None:
        self.data["illusts"][work_id] = entry
        self.save()

    def mark_novel(self, work_id: str, entry: dict) -> None:
        self.data["novels"][work_id] = entry
        self.save()


def fetch_illust_list(api: PixivWeb) -> dict:
    works: list[dict] = []
    thumbnails: list[dict] = []
    tag_translation: dict = {}
    offset = 0
    while True:
        d = api.get_json(
            "/ajax/dashboard/works/illust", params={"offset": offset, "limit": 200}
        )
        body = d["body"]
        batch = body["data"]["works"]
        works.extend(batch)
        thumbnails.extend(body.get("thumbnails", {}).get("illust", []))
        tag_translation = body.get("tagTranslation", tag_translation)
        log.info("illust list: offset=%d got %d (total %d)", offset, len(batch), len(works))
        if len(batch) < 200:
            break
        offset += len(batch)
    return {"works": works, "thumbnails": thumbnails, "tagTranslation": tag_translation}


def fetch_novel_list(api: PixivWeb) -> dict:
    works: list[dict] = []
    thumbnails: list[dict] = []
    tag_translation: dict = {}
    offset = 0
    while True:
        d = api.get_json(
            "/ajax/dashboard/works/novel", params={"offset": offset, "limit": 200}
        )
        body = d["body"]
        batch = body["data"]["works"]
        works.extend(batch)
        thumbnails.extend(body.get("thumbnails", {}).get("novel", []))
        tag_translation = body.get("tagTranslation", tag_translation)
        log.info("novel list: offset=%d got %d (total %d)", offset, len(batch), len(works))
        if len(batch) < 200:
            break
        offset += len(batch)
    return {"works": works, "thumbnails": thumbnails, "tagTranslation": tag_translation}


def pick(d: dict, keys: list[str]) -> dict:
    return {k: d.get(k) for k in keys}


def export_illust(
    api: PixivWeb, state: State, thumb: dict, illust_dir: str, download_images: bool
) -> None:
    wid = thumb["id"]
    entry = state.data["illusts"].get(wid, {})
    if entry.get("status") == "done":
        return
    work_dir = os.path.join(illust_dir, wid)
    os.makedirs(work_dir, exist_ok=True)

    detail = api.get_json(f"/ajax/illust/{wid}")["body"]
    images = api.get_json(f"/ajax/illust/{wid}/uploadedImages")["body"]

    saved_images: list[dict] = []
    if download_images:
        for i, img in enumerate(images):
            ext = MIME_EXT.get(img.get("mimeType"), ".bin")
            fname = f"{i}{ext}"
            fpath = os.path.join(work_dir, fname)
            expected = img.get("byteLength")
            if (
                os.path.exists(fpath)
                and expected is not None
                and os.path.getsize(fpath) == expected
            ):
                saved_images.append(
                    {
                        "index": i,
                        "url": img["url"],
                        "byteLength": expected,
                        "mimeType": img.get("mimeType"),
                        "file": fname,
                        "downloaded_bytes": os.path.getsize(fpath),
                        "verified": True,
                    }
                )
                continue
            size = api.download(img["url"], fpath, expected_bytes=expected)
            saved_images.append(
                {
                    "index": i,
                    "url": img["url"],
                    "byteLength": expected,
                    "mimeType": img.get("mimeType"),
                    "file": fname,
                    "downloaded_bytes": size,
                    "verified": size == expected,
                }
            )
            api._jitter_sleep(0.3)
        entry_images = saved_images
    else:
        entry_images = [
            {"index": i, "url": img["url"], "byteLength": img.get("byteLength"),
             "mimeType": img.get("mimeType"), "file": None, "verified": False}
            for i, img in enumerate(images)
        ]

    meta = {
        "list": thumb,
        "detail": pick(detail, ILLUST_DETAIL_KEEP),
        "images": entry_images,
    }
    with open(os.path.join(work_dir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)

    state.mark_illust(
        wid,
        {
            "status": "done",
            "title": thumb.get("title"),
            "images": entry_images,
        },
    )


def export_novel(api: PixivWeb, state: State, thumb: dict, novel_dir: str) -> None:
    wid = thumb["id"]
    if state.data["novels"].get(wid, {}).get("status") == "done":
        return
    work_dir = os.path.join(novel_dir, wid)
    os.makedirs(work_dir, exist_ok=True)
    detail = api.get_json(f"/ajax/novel/{wid}")["body"]
    meta = {"list": thumb, "detail": pick(detail, NOVEL_DETAIL_KEEP)}
    with open(os.path.join(work_dir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
    state.mark_novel(wid, {"status": "done", "title": thumb.get("title")})


def main() -> int:
    global log
    p = argparse.ArgumentParser(description="Export all works of a logged-in pixiv account")
    p.add_argument("--cookie-file", required=True, help="file with the browser Cookie header (plain or node-fetch format)")
    p.add_argument("--user-id", default=None, help="default: extracted from PHPSESSID")
    p.add_argument("--out", default="export", help="output directory")
    p.add_argument("--delay", type=float, default=0.8, help="base delay between API calls (s)")
    p.add_argument("--max-retries", type=int, default=4)
    p.add_argument("--no-images", action="store_true", help="metadata only, skip image downloads")
    p.add_argument("--no-novels", action="store_true", help="skip novels")
    args = p.parse_args()

    out_root = os.path.abspath(args.out)
    log_dir = os.path.join(out_root, "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "export.log")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(log_path)],
    )
    log = logging.getLogger("export")

    cookies = CookieProvider(args.cookie_file)
    cookie = cookies.get()
    user_id = args.user_id or CookieProvider.user_id_from(cookie)
    if not user_id:
        log.error("cannot determine user id, pass --user-id")
        return 1
    account_dir = os.path.join(out_root, user_id)
    os.makedirs(account_dir, exist_ok=True)
    log.info("exporting user %s -> %s", user_id, account_dir)

    api = PixivWeb(make_session(), cookies, user_id, args.delay, args.max_retries)
    state = State(os.path.join(account_dir, "state.json"))

    illust_list = fetch_illust_list(api)
    with open(
        os.path.join(account_dir, "works_illusts.json"), "w", encoding="utf-8"
    ) as f:
        json.dump(illust_list, f, ensure_ascii=False)
    log.info("illust list saved: %d works", len(illust_list["works"]))

    novel_list: dict | None = None
    if not args.no_novels:
        novel_list = fetch_novel_list(api)
        with open(
            os.path.join(account_dir, "works_novels.json"), "w", encoding="utf-8"
        ) as f:
            json.dump(novel_list, f, ensure_ascii=False)
        log.info("novel list saved: %d works", len(novel_list["works"]))

    illust_dir = os.path.join(account_dir, "illusts")
    total = len(illust_list["thumbnails"])
    done_count = 0
    errors: list[str] = []
    for i, thumb in enumerate(illust_list["thumbnails"], 1):
        wid = thumb["id"]
        try:
            export_illust(api, state, thumb, illust_dir, not args.no_images)
            done_count += 1
            log.info(
                "[%d/%d] illust %s ok (%s)",
                i,
                total,
                wid,
                (thumb.get("title") or "")[:40],
            )
        except PixivError as e:
            errors.append(wid)
            state.mark_illust(wid, {"status": "error", "error": str(e)})
            log.error("[%d/%d] illust %s FAILED: %s", i, total, wid, e)
        except Exception as e:
            errors.append(wid)
            state.mark_illust(wid, {"status": "error", "error": repr(e)})
            log.exception("[%d/%d] illust %s unexpected error", i, total, wid)

    novel_errors: list[str] = []
    if novel_list:
        novel_dir = os.path.join(account_dir, "novels")
        ntotal = len(novel_list["thumbnails"])
        for i, thumb in enumerate(novel_list["thumbnails"], 1):
            wid = thumb["id"]
            try:
                export_novel(api, state, thumb, novel_dir)
                log.info("[%d/%d] novel %s ok (%s)", i, ntotal, wid, (thumb.get("title") or "")[:40])
            except Exception as e:
                novel_errors.append(wid)
                state.mark_novel(wid, {"status": "error", "error": repr(e)})
                log.error("[%d/%d] novel %s FAILED: %s", i, ntotal, wid, e)

    total_images = sum(
        len(v.get("images", []))
        for v in state.data["illusts"].values()
        if v.get("status") == "done"
    )
    log.info(
        "DONE: illusts %d/%d ok, images %d, novels %d/%d ok; errors: illusts=%s novels=%s",
        done_count,
        total,
        total_images,
        len(novel_list["thumbnails"]) - len(novel_errors) if novel_list else 0,
        len(novel_list["thumbnails"]) if novel_list else 0,
        errors or "none",
        novel_errors or "none",
    )
    return 0 if not errors and not novel_errors else 2


if __name__ == "__main__":
    sys.exit(main())
