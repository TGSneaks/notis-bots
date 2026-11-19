"""
TikTok watcher.

All sources are free and need no login:
  * tiktok.com/@user          -> private/public flag, story flag, video count
  * tiktok.com/embed/@user    -> the 10 latest post IDs with covers (official creator embed)
  * tikwm.com/api/            -> 1080p no-watermark video / slideshow photos for a post ID
  * tikwm.com/api/user/story  -> story media, when it returns any
yt-dlp is used as a fallback downloader for a single post.
"""

import json
import logging
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from telegram import UA, UPLOAD_LIMIT, MediaItem, download, html_escape
from story_crop import fill_story_video, video_dimensions

log = logging.getLogger("notifier.tiktok")

TIKWM = "https://www.tikwm.com"
EMBED_SAFETY_EVERY = 6  # read the post list at least every Nth check even if the count looks unchanged
MEDIA_RETRIES = 3  # checks to wait for a post's media before sending a text-only alert
STATE_RE = r'<script id="{}"[^>]*>(.*?)</script>'
SEND_LIMIT = UPLOAD_LIMIT - 1024 * 1024  # leave room for the upload's form data, or Telegram answers 413
TOO_BIG_NOTE = "⚠️ Video is too big to upload to Telegram. Tap the link to watch it."


class TikTokError(Exception):
    pass


class TikTok:
    def __init__(self, cfg):
        self.targets = [t.strip().lstrip("@") for t in cfg.get("targets", [])]
        self.poll_interval = cfg.get("poll_interval_seconds", 300)
        self.notify_existing_stories = False
        self.s = requests.Session()
        self.s.headers["User-Agent"] = UA
        self._last_tikwm = 0.0
        self.too_large = False

    # ------------------------------------------------------------------ fetch

    def _page_state(self, url, script_id):
        r = self.s.get(url, timeout=30)
        r.raise_for_status()
        m = re.search(STATE_RE.format(script_id), r.text, re.S)
        if not m:
            raise TikTokError(f"no page data at {url}")
        return json.loads(m.group(1))

    def profile(self, username):
        """Returns dict with private, story, video_count, user_id. Raises TikTokError if missing."""
        state = self._page_state(f"https://www.tiktok.com/@{username}", "__UNIVERSAL_DATA_FOR_REHYDRATION__")
        detail = state.get("__DEFAULT_SCOPE__", {}).get("webapp.user-detail", {})
        user = detail.get("userInfo", {}).get("user")
        if not user:
            raise TikTokError(f"@{username} not found (status {detail.get('statusCode')})")
        return {
            "private": bool(user.get("privateAccount")),
            "story": bool(user.get("UserStoryStatus")),
            "video_count": detail.get("userInfo", {}).get("stats", {}).get("videoCount"),
            "user_id": user.get("id"),
            "avatar": user.get("avatarLarger"),
        }

    def latest_posts(self, username):
        """Newest-first list of the latest ~10 posts from the official creator embed, or from yt-dlp's
        profile listing when the embed is rate limited (it 429s often)."""
        try:
            state = self._page_state(f"https://www.tiktok.com/embed/@{username}", "__FRONTITY_CONNECT_STATE__")
            data = state.get("source", {}).get("data", {}).get(f"/embed/@{username}", {})
            posts = data.get("videoList") or []
        except Exception as e:
            log.info("embed post list unavailable for @%s (%s); using yt-dlp", username, e)
            posts = self._ytdlp_posts(username)
        return sorted(posts, key=lambda p: int(p["id"]), reverse=True)

    @staticmethod
    def _ytdlp_posts(username):
        import yt_dlp

        opts = {"quiet": True, "no_warnings": True, "extract_flat": True, "playlistend": 10}
        with yt_dlp.YoutubeDL(opts) as y:
            info = y.extract_info(f"https://www.tiktok.com/@{username}", download=False)
        return [{"id": str(e["id"]), "desc": e.get("description") or e.get("title") or ""}
                for e in info.get("entries") or [] if str(e.get("id", "")).isdigit()]

    def _tikwm(self, path, payload):
        for attempt in range(2):
            # tikwm's free tier allows about 1 request per second
            wait = 1.2 - (time.time() - self._last_tikwm)
            if wait > 0:
                time.sleep(wait)
            self._last_tikwm = time.time()
            try:
                r = self.s.post(f"{TIKWM}{path}", data=payload, timeout=60)
                break
            except requests.ConnectionError:
                if attempt:
                    raise
                time.sleep(5)  # tikwm sometimes drops the connection; one retry
        r.raise_for_status()
        body = r.json()
        if body.get("code") != 0:
            raise TikTokError(f"tikwm {path}: {body.get('msg')}")
        return body.get("data") or {}

    @staticmethod
    def _abs(url):
        return url if url.startswith("http") else TIKWM + url

    # ------------------------------------------------------------------ media

    def post_media(self, username, post_id, embed_post=None):
        """Highest-quality media for a post: tikwm 1080p, then yt-dlp, then the embed's 720p/cover.
        Sets self.too_large when the video exists but no copy fits Telegram's upload limit."""
        url = f"https://www.tiktok.com/@{username}/video/{post_id}"
        self.too_large = False
        try:
            d = self._tikwm("/api/", {"url": url, "hd": 1})
            return self._tikwm_media(d, f"tiktok_{post_id}")
        except Exception as e:
            self.too_large |= "too large" in str(e)
            log.warning("tikwm failed for %s: %s", post_id, e)

        try:
            return self._ytdlp_media(url, post_id)
        except Exception as e:
            self.too_large |= "too large" in str(e)
            log.warning("yt-dlp failed for %s: %s", post_id, e)

        if embed_post:
            for key, is_video in (("playAddr", True), ("originCoverUrl", False), ("coverUrl", False)):
                if embed_post.get(key):
                    try:
                        data = download(embed_post[key], {"Referer": "https://www.tiktok.com/"})
                    except Exception as e:
                        log.warning("embed %s download failed: %s", key, e)
                        continue
                    if is_video and len(data) > SEND_LIMIT:
                        self.too_large = True
                        log.warning("embed video for %s is too large for Telegram; sending the cover", post_id)
                        continue
                    ext = "mp4" if is_video else "jpg"
                    return [MediaItem(data, is_video, f"tiktok_{post_id}.{ext}")]
        return []

    def _tikwm_media(self, d, prefix):
        images = d.get("images") or []
        if images:
            return [MediaItem(download(u), False, f"{prefix}_{i + 1}.jpg") for i, u in enumerate(images)]
        src = d.get("hdplay") or d.get("play")
        if not src:
            cover = d.get("origin_cover") or d.get("cover")
            return [MediaItem(download(self._abs(cover)), False, f"{prefix}.jpg")] if cover else []
        data = download(self._abs(src))
        if len(data) > SEND_LIMIT and d.get("play") and src != d.get("play"):
            data = download(self._abs(d["play"]))  # smaller SD file still fits
        if len(data) > SEND_LIMIT:
            raise TikTokError("video too large for Telegram")
        return [MediaItem(data, True, f"{prefix}.mp4", duration=d.get("duration"))]

    def _ytdlp_media(self, url, post_id):
        import yt_dlp

        opts = {"quiet": True, "no_warnings": True, "noprogress": True}
        with yt_dlp.YoutubeDL(opts) as y:
            info = y.extract_info(url, download=False)
        fmts = [
            f for f in info.get("formats", [])
            if f.get("vcodec") not in (None, "none") and f.get("format_note") != "watermarked" and f.get("url")
        ]
        if not fmts:
            raise TikTokError("no formats")
        best = max(fmts, key=lambda f: ((f.get("height") or 0), (f.get("filesize") or 0)))
        # Let yt-dlp download it: TikTok's CDN rejects the bare URL without the cookies yt-dlp collected
        with tempfile.TemporaryDirectory(prefix="tiktok_post_") as temp_dir:
            outtmpl = str(Path(temp_dir) / "post.%(ext)s")
            with yt_dlp.YoutubeDL(dict(opts, format=best["format_id"], outtmpl=outtmpl)) as y:
                y.extract_info(url, download=True)
            files = list(Path(temp_dir).glob("post.*"))
            if not files:
                raise TikTokError("yt-dlp produced no file")
            data = files[0].read_bytes()
        if len(data) > SEND_LIMIT:
            raise TikTokError("video too large for Telegram")
        return [MediaItem(data, True, f"tiktok_{post_id}.mp4",
                          best.get("width"), best.get("height"), info.get("duration"))]

    def _higher_quality_story(self, username, story_id, item):
        """Prefer a larger TikTok copy when the story endpoint only offers a small one."""
        dimensions = video_dimensions(item.data)
        if not dimensions:
            return item
        url = f"https://www.tiktok.com/@{username}/video/{story_id}"
        try:
            import yt_dlp

            with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True}) as y:
                info = y.extract_info(url, download=False)
            formats = [
                fmt for fmt in info.get("formats", [])
                if fmt.get("ext") == "mp4" and fmt.get("vcodec") not in (None, "none")
                and fmt.get("format_note") != "watermarked"
                and (fmt.get("height") or 0) > dimensions[1]
                and (fmt.get("filesize") or fmt.get("filesize_approx") or 0) <= SEND_LIMIT
            ]
            if not formats:
                return item
            best = max(formats, key=lambda fmt: (fmt.get("height") or 0, fmt.get("tbr") or 0))
            with tempfile.TemporaryDirectory(prefix="tiktok_story_hd_") as temp_dir:
                pattern = str(Path(temp_dir) / "story.%(ext)s")
                with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "noprogress": True,
                                       "format": best["format_id"], "outtmpl": pattern}) as y:
                    y.extract_info(url, download=True)
                files = list(Path(temp_dir).glob("story.*"))
                if not files:
                    return item
                data = files[0].read_bytes()
            better_dimensions = video_dimensions(data)
            if not better_dimensions or better_dimensions[1] <= dimensions[1] or len(data) > SEND_LIMIT:
                return item
            log.info("@%s story %s: using %dx%d source instead of %dx%d",
                     username, story_id, *better_dimensions, *dimensions)
            return MediaItem(data, True, item.name, *better_dimensions, item.duration)
        except Exception as error:
            log.info("higher-resolution story source unavailable for @%s (%s)", username,
                     type(error).__name__)
            return item

    def stories(self, username):
        """Active stories via tikwm. Returns list of (story_id, create_time, media_items_loader)."""
        d = self._tikwm("/api/user/story", {"unique_id": username, "count": 20, "cursor": 0})
        out = []
        for v in d.get("videos") or []:
            sid = str(v.get("video_id") or v.get("id") or v.get("aweme_id") or "")
            if sid:
                out.append((sid, v.get("create_time"), v))
        return out

    # ------------------------------------------------------------------ check

    def check(self, tg, username, state, notify_existing):
        st = state.setdefault(username, {})
        first_run = "private" not in st
        notify = (not first_run) or notify_existing
        link = f"https://www.tiktok.com/@{username}"

        story_notify = notify or (first_run and self.notify_existing_stories)
        try:
            prof = self.profile(username)
        except Exception as profile_error:
            # TikTok's profile page may be rate limited while the separate
            # story source is still available. Keep checking stories.
            try:
                items = self.stories(username)
            except Exception:
                raise profile_error
            log.warning("@%s profile unavailable (%s); checking stories directly", username,
                        type(profile_error).__name__)
            self._check_stories(tg, username, st, {"story": bool(items)}, story_notify, items)
            return

        # ---- private <-> public
        if not first_run and st["private"] != prof["private"]:
            if prof["private"]:
                tg.text(f"🔒 <b>{username}</b> just went <b>private</b> on TikTok.\n{link}")
            else:
                tg.text(f"🔓 <b>{username}</b> is now <b>public</b> on TikTok!\n{link}")
        st["private"] = prof["private"]

        if prof["private"]:
            log.info("@%s is private - skipping posts/stories", username)
            st["story_flag"] = prof["story"]
            return

        # ---- stories
        self._check_stories(tg, username, st, prof, story_notify)

        # ---- posts
        # The embed page is rate limited (429) if hit every check, so only read it when the profile's
        # video count changed, when there's a pending media retry, or every few checks as a safety net.
        # Accounts whose count doesn't move per post (big ones show a rounded count) are read every check.
        skipped = st.get("embed_skips", 0)
        if ("seen_posts" in st and prof["video_count"] is not None
                and prof["video_count"] == st.get("video_count") and not st.get("count_unreliable")
                and not st.get("media_retries") and skipped < EMBED_SAFETY_EVERY):
            st["embed_skips"] = skipped + 1
            return

        # No baseline yet (first run, or the account was private until now): record silently
        notify_posts = "seen_posts" in st or (first_run and notify_existing)
        seen = set(st.get("seen_posts", []))
        retries = st.setdefault("media_retries", {})
        # Post IDs grow over time, so anything older than the newest seen post (a pinned video, say)
        # isn't new - unless it's still waiting for its media
        newest = max((int(s) for s in seen), default=0)
        new = [p for p in self.latest_posts(username)
               if p["id"] not in seen and (int(p["id"]) > newest or p["id"] in retries)]
        if (seen and not st.get("count_unreliable") and prof["video_count"] == st.get("video_count")
                and any(p["id"] not in retries for p in new)):
            st["count_unreliable"] = True
            log.info("@%s posted without its video count changing - reading its post list every check", username)
        for post in reversed(new):  # oldest first
            pid = post["id"]
            if notify_posts and not self._send_post(tg, username, post, retries.get(pid, 0)):
                retries[pid] = retries.get(pid, 0) + 1
                continue  # media sources failed; try again next check
            retries.pop(pid, None)
            seen.add(pid)
            st["seen_posts"] = sorted(seen, key=int)[-300:]
        st.setdefault("seen_posts", [])
        # Only remember the count once the post list was read, so a failed read is retried
        st["video_count"] = prof["video_count"]
        st["embed_skips"] = 0

        if first_run:
            log.info("@%s baseline saved (%d posts) - will notify on new ones from now", username, len(seen))

    def _check_stories(self, tg, username, st, prof, notify, items=None):
        had_flag = st.get("story_flag", False)
        if not prof["story"]:
            st["story_flag"] = False
            return

        seen = set(st.get("seen_stories", []))
        sent_any = False
        if items is None:
            try:
                items = self.stories(username)
            except Exception as e:
                log.warning("tikwm stories failed for @%s: %s", username, e)
                items = []
        log.info("@%s has an active story flag; story source returned %d item(s)", username, len(items))

        for sid, created, raw in sorted(items, key=lambda x: x[1] or 0):
            if sid in seen:
                continue
            if notify:
                try:
                    media = self._tikwm_media(raw, f"story_{sid}")
                    if len(media) == 1 and media[0].is_video:
                        video = self._higher_quality_story(username, sid, media[0])
                        filled, dimensions = fill_story_video(video.data, video.duration, SEND_LIMIT)
                        if dimensions:
                            video = MediaItem(filled, True, video.name, *dimensions, video.duration)
                        media = [video]
                except Exception as e:
                    log.warning("story media download failed: %s", e)
                    media = []
                when = _fmt_ts(created)
                cap = f"📸 <b>{username}</b> posted a new TikTok story\n🕒 {when}\n{_story_link(username)}"
                if media:
                    tg.media(media, cap)
                else:
                    tg.text(cap)
            sent_any = True
            seen.add(sid)
            # Save after each one so a later failure doesn't make already-sent stories go out again
            st["seen_stories"] = sorted(seen)[-300:]

        # No story media available: still say a story went up (once per story session)
        if not sent_any and not had_flag and notify:
            tg.text(f"📸 <b>{username}</b> has a new TikTok story.\n{_story_link(username)}")
        st["story_flag"] = True

    def _send_post(self, tg, username, post, attempts=0):
        """Sends the post. Returns False (nothing sent) when media failed and it's worth retrying later."""
        pid = post["id"]
        media = self.post_media(username, pid, post)
        too_large = self.too_large and not (media and media[0].is_video)
        if not media and not too_large and attempts < MEDIA_RETRIES:
            log.warning("@%s post %s: no media yet (attempt %d), retrying next check", username, pid, attempts + 1)
            return False
        desc = html_escape(post.get("desc") or "")
        if len(desc) > 600:
            desc = desc[:600] + "…"
        kind = "photo slideshow" if media and not media[0].is_video and not too_large else "video"

        def caption(note):
            return (
                f"🆕 <b>{username}</b> posted a new TikTok ({kind})\n"
                f"🕒 {_fmt_ts(int(pid) >> 32)}\n"
                f"https://www.tiktok.com/@{username}/video/{pid}"
                + (f"\n{note}" if note else "")
                + (f"\n\n{desc}" if desc else "")
            )

        note = TOO_BIG_NOTE if too_large else ""
        if not media:
            tg.text(caption(note))
            return True
        try:
            tg.media(media, caption(note))
        except RuntimeError as e:
            if "413" not in str(e):
                raise
            # Telegram refused the file as too large: send the alert without it instead of retrying every check
            log.warning("@%s post %s: Telegram says the upload is too large; sending the link only", username, pid)
            tg.text(caption(TOO_BIG_NOTE))
        return True


def _fmt_ts(ts):
    if not ts:
        return "just now"
    return datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _story_link(username):
    return f"https://www.tiktok.com/@{username}"
