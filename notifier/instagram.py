"""
Instagram watcher (optional). Needs an Instagram login because stories are only visible to logged-in users.
Uses instaloader; media is the highest resolution Instagram serves (1080p for normal uploads).
"""

import logging
import sys
from itertools import islice
from pathlib import Path

from telegram import UPLOAD_LIMIT, MediaItem, download, html_escape

log = logging.getLogger("notifier.instagram")


class Instagram:
    def __init__(self, cfg, base_dir):
        import instaloader

        self.il = instaloader
        self.targets = [t.strip().lstrip("@") for t in cfg.get("targets", [])]
        self.poll_interval = cfg.get("poll_interval_seconds", 600)
        self.L = self._login(cfg, Path(base_dir))

    def _login(self, cfg, base_dir):
        il = self.il
        L = il.Instaloader(
            quiet=True,
            download_pictures=False,
            download_videos=False,
            save_metadata=False,
            max_connection_attempts=3,
        )
        user = cfg.get("ig_username")
        if not user:
            sys.exit("instagram.ig_username is missing in config.json")
        session_file = base_dir / f"session-{user}"
        try:
            L.load_session_from_file(user, str(session_file))
            log.info("Loaded Instagram session for %s", user)
        except FileNotFoundError:
            password = cfg.get("ig_password")
            if not password:
                sys.exit(
                    f"No saved Instagram session for {user}. Either set instagram.ig_password in config.json, "
                    f"or run:  instaloader --login {user} --sessionfile \"{session_file}\""
                )
            try:
                L.login(user, password)
            except il.TwoFactorAuthRequiredException:
                L.two_factor_login(input("Instagram 2FA code: ").strip())
            L.save_session_to_file(str(session_file))
            log.info("Logged in to Instagram and saved session to %s", session_file.name)
        return L

    # ------------------------------------------------------------------ media

    @staticmethod
    def _collect(entries, prefix):
        """entries: list of (url, is_video). Returns (items, urls_that_could_not_be_attached)."""
        items, failed = [], []
        for i, (url, is_video) in enumerate(entries):
            try:
                data = download(url)
            except Exception as e:
                log.error("Download failed for %s: %s", url, e)
                failed.append(url)
                continue
            if len(data) > UPLOAD_LIMIT:
                failed.append(url)
                continue
            ext = "mp4" if is_video else "jpg"
            items.append(MediaItem(data, is_video, f"{prefix}_{i + 1}.{ext}"))
        return items, failed

    @staticmethod
    def _post_entries(post):
        if post.typename == "GraphSidecar":
            return [(n.video_url if n.is_video else n.display_url, n.is_video) for n in post.get_sidecar_nodes()]
        if post.is_video:
            return [(post.video_url, True)]
        return [(post.url, False)]

    # ------------------------------------------------------------------ check

    def check(self, tg, username, state, notify_existing):
        st = state.setdefault(username, {})
        first_run = "is_private" not in st
        link = f"https://www.instagram.com/{username}/"

        profile = self.il.Profile.from_username(self.L.context, username)

        # ---- private <-> public
        was_private = st.get("is_private")
        if was_private is not None and was_private != profile.is_private:
            if profile.is_private:
                tg.text(f"🔒 <b>@{username}</b> just went <b>private</b> on Instagram.\n{link}")
            else:
                tg.text(f"🔓 <b>@{username}</b> is now <b>public</b> on Instagram!\n{link}")
        st["is_private"] = profile.is_private

        if profile.is_private and not profile.followed_by_viewer:
            log.info("@%s is private and not followed - skipping posts/stories", username)
            return

        # No baseline yet (first run, or the account was private until now): record silently
        notify_posts = "seen_posts" in st or (first_run and notify_existing)
        notify_stories = "seen_stories" in st or (first_run and notify_existing)
        seen_posts = set(st.get("seen_posts", []))
        seen_stories = set(st.get("seen_stories", []))

        # ---- stories
        new_stories = [
            item
            for story in self.L.get_stories(userids=[profile.userid])
            for item in story.get_items()
            if str(item.mediaid) not in seen_stories
        ]
        for item in sorted(new_stories, key=lambda i: i.date_utc):
            if notify_stories:
                entries = [(item.video_url, True)] if item.is_video else [(item.url, False)]
                items, failed = self._collect(entries, f"ig_story_{item.mediaid}")
                cap = (
                    f"📸 <b>@{username}</b> posted a new Instagram story\n"
                    f"🕒 {item.date_utc:%Y-%m-%d %H:%M} UTC\n"
                    f"https://www.instagram.com/stories/{username}/{item.mediaid}/"
                )
                if items:
                    tg.media(items, cap)
                else:
                    tg.text(cap)
                for url in failed:
                    tg.text(f"⚠️ Story file couldn't be attached, direct link:\n{url}")
            seen_stories.add(str(item.mediaid))
        st["seen_stories"] = sorted(seen_stories)[-300:]

        # ---- posts (pinned posts appear first, so look at a few beyond the newest)
        new_posts = [p for p in islice(profile.get_posts(), 12) if p.shortcode not in seen_posts]
        for post in sorted(new_posts, key=lambda p: p.date_utc):
            if notify_posts:
                items, failed = self._collect(self._post_entries(post), f"ig_{post.shortcode}")
                kind = {"GraphSidecar": "carousel", "GraphVideo": "reel/video"}.get(post.typename, "photo")
                body = html_escape(post.caption or "")
                if len(body) > 600:
                    body = body[:600] + "…"
                cap = (
                    f"🆕 <b>@{username}</b> posted on Instagram ({kind})\n"
                    f"🕒 {post.date_utc:%Y-%m-%d %H:%M} UTC\n"
                    f"https://www.instagram.com/p/{post.shortcode}/"
                    + (f"\n\n{body}" if body else "")
                )
                if items:
                    tg.media(items, cap)
                else:
                    tg.text(cap)
                for url in failed:
                    tg.text(f"⚠️ Post file couldn't be attached, direct link:\n{url}")
            seen_posts.add(post.shortcode)
        st["seen_posts"] = sorted(seen_posts)[-300:]

        if first_run:
            log.info("@%s (Instagram) baseline saved - will notify on new content from now", username)
