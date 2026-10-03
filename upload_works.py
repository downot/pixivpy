#!/usr/bin/env python3
"""Re-upload exported works to another pixiv account.

Reads an export directory (created by export_works.py) and uploads each
illust via the web AJAX create API. Standalone: python3 + requests only.

The cookie file is re-read on every request, so it can be refreshed while
uploading. Progress is tracked in <source>/upload_state.json and can be
resumed.

Usage:
    python3 upload_works.py --cookie-file cookie_b.txt --source export/15032614
    python3 upload_works.py --cookie-file cookie_b.txt --source export/15032614 --limit 2
    python3 upload_works.py --cookie-file cookie_b.txt --source export/15032614 --only 150148884
"""
from __future__ import annotations

import argparse
import html as htmlmod
import hashlib
import json
import logging
import os
import random
import re
import ssl
import subprocess
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
CREATE_PAGE = "https://www.pixiv.net/illustration/create"
PROGRESS_INTERVAL = 2.5
PROGRESS_TIMEOUT = 600
# Node solver that issues a reCAPTCHA token from a pixiv-trusted origin.
# Emits "TOKEN=<token>" on stdout when it succeeds.
CAPTCHA_SOLVER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "captcha_solver.mjs")
# Human-in-the-loop solver: opens a visible window, waits for the user to solve,
# and saves a labeled example (grid + user's selection + LLM's answer) for learning.
CAPTCHA_HUMAN_SOLVER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "captcha_human.mjs")

AI_TYPE = {0: "undefined", 1: "notAiGenerated", 2: "aiGenerated"}
X_RESTRICT = {0: "general", 1: "r18", 2: "r18g"}


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


class CaptchaRequiredError(PixivError):
    pass


def html_to_text(value: str | None) -> str:
    if not value:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", value, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = htmlmod.unescape(text)
    return text.strip()


class Uploader:
    def __init__(
        self,
        session: requests.Session,
        cookies: CookieProvider,
        user_id: str,
        delay: float,
        human: bool = False,
    ) -> None:
        self.session = session
        self.cookies = cookies
        self.user_id = user_id
        self.delay = delay
        self.human = human
        self.human_round = 0
        self.csrf: str | None = None
        self.captcha_token: str | None = None

    def _headers(self, extra: dict | None = None) -> dict[str, str]:
        h = {
            "cookie": self.cookies.get(),
            "x-user-id": self.user_id,
            "accept": "application/json",
        }
        if self.csrf:
            h["x-csrf-token"] = self.csrf
        if extra:
            h.update(extra)
        return h

    def _jitter_sleep(self, base: float) -> None:
        time.sleep(base * random.uniform(0.7, 1.3))

    def refresh_csrf(self) -> str:
        r = self.session.get(CREATE_PAGE, headers=self._headers(), timeout=30)
        if r.status_code != 200:
            raise PixivError(f"create page HTTP {r.status_code}", r.status_code)
        m = re.search(r'token\\?":\\?"([0-9a-f]{32})', r.text)
        if not m:
            cands = sorted(set(re.findall(r"\b[0-9a-f]{32}\b", r.text)))
            if len(cands) != 1:
                raise PixivError(f"cannot extract csrf token (candidates={cands})")
            m = re.match(r"([0-9a-f]{32})", cands[0])
        self.csrf = m.group(1)
        log.info("csrf token: %s", self.csrf)
        return self.csrf

    def get_json(self, path: str, params: dict | None = None) -> dict:
        r = self.session.get(BASE + path, params=params, headers=self._headers(), timeout=30)
        if r.status_code != 200:
            raise PixivError(f"HTTP {r.status_code} on {path}", r.status_code, r.text[:300])
        d = r.json()
        if d.get("error"):
            raise PixivError(f"pixiv error on {path}: {d.get('message')}", 200, d)
        return d

    def check_init(self) -> None:
        d = self.get_json("/ajax/work/create/illustration/init")
        body = d.get("body", {})
        if body.get("request"):
            raise PixivError(f"a create request is already in progress: {body['request']}")
        if body.get("showCaptcha"):
            raise CaptchaRequiredError("captcha required for this account right now")

    def solve_captcha(self) -> str:
        """Run the Node solver and return a fresh reCAPTCHA token (pixiv origin)."""
        if self.human:
            self.human_round += 1
            cmd = ["node", CAPTCHA_HUMAN_SOLVER, "www.pixiv.net", str(self.human_round)]
            log.info("CAPTCHA round %d: a VISIBLE window is opening — please solve it", self.human_round)
            kwargs = dict(stdout=subprocess.PIPE, text=True, timeout=320)  # stderr streams live
        else:
            cmd = ["node", CAPTCHA_SOLVER, "www.pixiv.net"]
            log.info("solving reCAPTCHA via local solver (may take ~30-60s)...")
            kwargs = dict(capture_output=True, text=True, timeout=300)
        try:
            proc = subprocess.run(cmd, **kwargs)
        except FileNotFoundError:
            raise PixivError("node not found; cannot solve captcha")
        except subprocess.TimeoutExpired as e:
            err = (e.stderr or b"").decode(errors="ignore")[-400:] if e.stderr is not None else ""
            log.warning("captcha solver timed out%s", ("; stderr: " + err) if err else " (no stderr captured)")
            raise PixivError("captcha solver timed out (you may not have solved it in time)")
        for line in proc.stdout.splitlines():
            if line.startswith("TOKEN="):
                tok = line[len("TOKEN="):].strip()
                self.captcha_token = tok
                log.info("captcha solved: token %s...", tok[:24])
                return tok
        log.warning("captcha solver stderr: %s", (proc.stderr or "").strip()[-400:])
        raise PixivError("captcha solver did not return a token")

    def build_fields(self, meta: dict) -> dict[str, Any]:
        detail = meta.get("detail", {})
        thumb = meta.get("list", {})
        images = meta["images"]

        n = len(images)
        fields: dict[str, Any] = {}
        for i in range(n):
            fields[f"imageOrder[{i}][type]"] = "newFile"
            fields[f"imageOrder[{i}][fileKey]"] = str(i)

        tc = thumb.get("titleCaptionTranslation") or {}
        fields["title"] = (detail.get("title") or thumb.get("title") or "").strip()
        fields["titleTranslations[en]"] = tc.get("workTitle") or ""
        caption = html_to_text(detail.get("description"))
        created = detail.get("createDate") or thumb.get("createDate") or ""
        if created:
            line = f"🕰️ 元投稿日 (Original posting date): {created[:10]}"
            caption = f"{caption}\n{line}" if caption else line
        fields["caption"] = caption
        fields["captionTranslations[en]"] = tc.get("workCaption") or ""
        fields["crop[x]"] = "0"
        fields["crop[y]"] = "0"

        tags = [t for t in (thumb.get("tags") or []) if t]
        fields["tags[]"] = tags if len(tags) > 1 else (tags[0] if tags else "")
        fields["allowTagEdit"] = "true"
        fields["xRestrict"] = X_RESTRICT.get(detail.get("xRestrict"), "general")
        fields["sexual"] = "false"
        fields["aiType"] = AI_TYPE.get(thumb.get("aiType"), "undefined")

        if thumb.get("isUnlisted"):
            fields["restrict"] = "unlisted"
        elif thumb.get("visibilityScope") == 4 or detail.get("isLoginOnly"):
            fields["restrict"] = "loginOnly"
        else:
            fields["restrict"] = "public"

        fields["allowComment"] = "true"
        fields["original"] = "true" if detail.get("isOriginal") else "false"
        fields["responseAutoAccept"] = "false"
        for attr in ("violent", "drug", "thoughts", "antisocial", "religion"):
            fields[f"ratings[{attr}]"] = "false"
        fields["gRecaptchaResponse"] = self.captcha_token or ""
        fields["allowCitationWork"] = "0"
        return fields

    def create_illust(self, meta: dict, work_dir: str) -> str:
        fields = self.build_fields(meta)
        images = meta["images"]

        files = []
        for i, img in enumerate(images):
            fpath = os.path.join(work_dir, img["file"])
            size = os.path.getsize(fpath)
            if size != img.get("byteLength"):
                raise PixivError(f"image size mismatch {img['file']}: {size} != {img['byteLength']}")
            files.append(("files[]", (img["file"], open(fpath, "rb"), img.get("mimeType") or "image/png")))
        try:
            r = self.session.post(
                BASE + "/ajax/work/create/illustration",
                data=fields,
                files=files,
                headers=self._headers({"referer": CREATE_PAGE}),
                timeout=300,
            )
        finally:
            for _, (_, fh, _) in files:
                fh.close()
        if r.status_code in (400, 403) and self.csrf:
            log.warning("create rejected (HTTP %d), refreshing csrf and retrying", r.status_code)
            self.csrf = None
            self.refresh_csrf()
            return self.create_illust(meta, work_dir)
        if r.status_code != 200:
            raise PixivError(f"create HTTP {r.status_code}: {r.text[:300]}", r.status_code)
        d = r.json()
        if d.get("error"):
            errs = d.get("errors") or d.get("message")
            raise PixivError(f"create failed: {errs}", 200, d)
        return d

    def wait_progress(self, convert_key: str) -> str:
        deadline = time.time() + PROGRESS_TIMEOUT
        while time.time() < deadline:
            time.sleep(PROGRESS_INTERVAL)
            d = self.get_json(
                "/ajax/work/create/illustration/progress",
                {"convertKey": convert_key},
            )
            body = d.get("body", {})
            status = body.get("status")
            if status == "COMPLETE":
                return str(body["illustId"])
            if status in ("FAILED", "ERROR", "TIMEOUT", "CANCELLED"):
                raise PixivError(f"convert {status}: {body}")
            log.info("  progress: %s", status)
        raise PixivError(f"progress timeout for {convert_key}")


class UploadState:
    def __init__(self, path: str) -> None:
        self.path = path
        self.data: dict[str, Any] = {"uploaded": {}, "failed": {}}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                self.data = json.load(f)

    def save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def uploaded(self, work_id: str) -> str | None:
        return self.data["uploaded"].get(work_id)

    def mark_uploaded(self, work_id: str, new_id: str) -> None:
        self.data["uploaded"][work_id] = new_id
        self.data["failed"].pop(work_id, None)
        self.save()

    def mark_failed(self, work_id: str, err: str) -> None:
        self.data["failed"][work_id] = err
        self.save()


def load_works(source: str) -> list[str]:
    """Work ids sorted by original createDate ascending (oldest first)."""
    with open(os.path.join(source, "works_illusts.json"), encoding="utf-8") as f:
        data = json.load(f)
    thumbs = sorted(
        data["thumbnails"], key=lambda t: t.get("createDate") or ""
    )
    return [t["id"] for t in thumbs]


def main() -> int:
    global log
    p = argparse.ArgumentParser(description="Re-upload exported works to another account")
    p.add_argument("--cookie-file", required=True)
    p.add_argument("--source", required=True, help="export directory, e.g. export/15032614")
    p.add_argument("--delay", type=float, default=3.0, help="base delay between works (s)")
    p.add_argument("--limit", type=int, default=None, help="upload at most N new works")
    p.add_argument("--only", default=None, help="comma-separated work ids to upload")
    p.add_argument("--user-id", default=None, help="target account id (default: from cookie)")
    p.add_argument("--human", action="store_true",
                   help="human-in-the-loop: open a visible window and wait for you to solve each CAPTCHA")
    p.add_argument("--no-solve", action="store_true",
                   help="do not solve CAPTCHA; on CAPTCHA back off 60s and retry (bounded), then skip")
    p.add_argument("--captcha-retries", type=int, default=3,
                   help="max CAPTCHA backoff retries per work in --no-solve mode (default 3)")
    args = p.parse_args()

    source = os.path.abspath(args.source)
    log_dir = os.path.join(source, "logs")
    os.makedirs(log_dir, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(os.path.join(log_dir, "upload.log"))],
    )
    log = logging.getLogger("upload")

    cookies = CookieProvider(args.cookie_file)
    user_id = args.user_id or CookieProvider.user_id_from(cookies.get())
    if not user_id:
        log.error("cannot determine target user id, pass --user-id")
        return 1

    session = make_session()
    up = Uploader(session, cookies, user_id, args.delay, human=args.human)
    state = UploadState(os.path.join(source, "upload_state.json"))

    work_ids = load_works(source)
    if args.only:
        wanted = {x.strip() for x in args.only.split(",")}
        work_ids = [w for w in work_ids if w in wanted]
    todo = [w for w in work_ids if state.uploaded(w) is None]
    if args.limit:
        todo = todo[: args.limit]
    log.info(
        "target user %s: %d works total, %d already uploaded, %d to upload",
        user_id, len(work_ids), len(work_ids) - len(todo), len(todo),
    )

    ok = 0
    for i, wid in enumerate(todo, 1):
        meta_path = os.path.join(source, "illusts", wid, "meta.json")
        work_dir = os.path.dirname(meta_path)
        try:
            with open(meta_path, encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            state.mark_failed(wid, f"meta load: {e}")
            log.error("[%d/%d] %s FAILED to load meta: %s", i, len(todo), wid, e)
            continue
        solved = False
        stop = False
        captcha_retries = 0
        while not stop:
            try:
                up.refresh_csrf()
                if not solved:
                    up.check_init()
                log.info("[%d/%d] uploading %s (%s, %d pages)", i, len(todo), wid,
                         (meta.get("detail", {}).get("title") or "")[:40], len(meta["images"]))
                d = up.create_illust(meta, work_dir)
                convert_key = d["body"]["convertKey"]
                log.info("  convertKey=%s", convert_key)
                new_id = up.wait_progress(convert_key)
                state.mark_uploaded(wid, new_id)
                ok += 1
                up.captcha_token = None  # token is single-use
                log.info("[%d/%d] %s -> %s", i, len(todo), wid, new_id)
                stop = True
            except CaptchaRequiredError:
                if args.no_solve:
                    captcha_retries += 1
                    if captcha_retries > args.captcha_retries:
                        state.mark_failed(wid, "captcha required (no-solve, retries exhausted)")
                        log.warning("[%d/%d] %s CAPTCHA hit %d times, skipping", i, len(todo), wid, captcha_retries)
                        stop = True
                    else:
                        log.warning("[%d/%d] %s CAPTCHA required; backing off 60s (retry %d/%d)",
                                    i, len(todo), wid, captcha_retries, args.captcha_retries)
                        time.sleep(60)
                elif solved:
                    log.warning("still throttled after a solve; stopping")
                    stop = True
                else:
                    try:
                        up.solve_captcha()
                        solved = True
                    except PixivError as ce:
                        state.mark_failed(wid, f"captcha solve: {ce}")
                        log.error("[%d/%d] %s captcha solve failed: %s", i, len(todo), wid, ce)
                        stop = True
            except PixivError as e:
                state.mark_failed(wid, str(e))
                log.error("[%d/%d] %s FAILED: %s", i, len(todo), wid, e)
                stop = True
            except Exception as e:
                state.mark_failed(wid, repr(e))
                log.exception("[%d/%d] %s unexpected error", i, len(todo), wid, e)
                stop = True
        up._jitter_sleep(args.delay)

    log.info(
        "DONE: %d/%d uploaded this run; total uploaded=%d failed=%s",
        ok, len(todo), len(state.data["uploaded"]),
        state.data["failed"] or "none",
    )
    return 0 if ok == len(todo) else 2


if __name__ == "__main__":
    sys.exit(main())
