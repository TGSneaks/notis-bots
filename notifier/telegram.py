"""Telegram Bot API helper shared by every platform."""

import json
import logging
import time

import requests

TG_API = "https://api.telegram.org/bot{token}/{method}"
CAPTION_LIMIT = 1024
UPLOAD_LIMIT = 50 * 1024 * 1024  # Bot API upload limit
TOPIC_GONE_ERRORS = ("message thread not found", "topic_deleted", "topic_id_invalid")
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

log = logging.getLogger("notifier.telegram")


def html_escape(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def download(url, headers=None):
    h = {"User-Agent": UA}
    h.update(headers or {})
    r = requests.get(url, headers=h, timeout=180)
    r.raise_for_status()
    return r.content


class TopicGone(RuntimeError):
    """The forum topic being sent to no longer exists (deleted in Telegram)."""


class MediaItem:
    """One photo or video ready to upload."""

    def __init__(self, data, is_video, name, width=None, height=None, duration=None):
        self.data = data
        self.is_video = is_video
        self.name = name
        self.width = width
        self.height = height
        self.duration = duration


class Telegram:
    def __init__(self, token, chat_id, send_as_files=False, topic_id=None):
        self.token = token
        self.chat_id = chat_id
        self.send_as_files = send_as_files
        self.topic_id = topic_id

    def for_chat(self, chat_id, topic_id=None):
        """Same bot, different destination channel (or forum topic)."""
        return Telegram(self.token, chat_id, self.send_as_files, topic_id)

    def _call(self, method, data=None, files=None):
        url = TG_API.format(token=self.token, method=method)
        if data is not None and self.topic_id:
            data = dict(data, message_thread_id=self.topic_id)
        r = None
        network_failures = 0
        for _ in range(5):
            try:
                r = requests.post(url, data=data, files=files, timeout=300)
            except requests.RequestException as e:
                # Uploads occasionally drop (SSLError, reset); retry a couple of times before giving up
                network_failures += 1
                if network_failures >= 3:
                    raise RuntimeError(f"Telegram {method} request failed: {type(e).__name__}") from None
                log.warning("Telegram %s network error (%s), retrying", method, type(e).__name__)
                time.sleep(5 * network_failures)
                continue
            if r.status_code == 429:
                wait = r.json().get("parameters", {}).get("retry_after", 5)
                log.warning("Telegram rate limit, sleeping %ss", wait)
                time.sleep(wait + 1)
                continue
            if not r.ok:
                if self.topic_id and any(e in r.text.lower() for e in TOPIC_GONE_ERRORS):
                    raise TopicGone(f"topic {self.topic_id} no longer exists")
                log.error("Telegram %s failed: %s", method, r.text)
                raise RuntimeError(f"Telegram {method} failed: HTTP {r.status_code}")
            body = r.json()
            if not body.get("ok"):
                raise RuntimeError(f"Telegram {method} failed: {body.get('description', 'unknown error')}")
            return r
        raise RuntimeError(f"Telegram {method} stayed rate limited after 5 attempts")

    def text(self, msg):
        self._call("sendMessage", {
            "chat_id": self.chat_id,
            "text": msg,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        })

    def media(self, items, caption):
        """Sends one item, or albums of up to 10. Caption goes on the first one."""
        caption = caption[:CAPTION_LIMIT]
        for start in range(0, len(items), 10):
            chunk = items[start:start + 10]
            cap = caption if start == 0 else ""
            if len(chunk) == 1:
                self._single(chunk[0], cap)
            else:
                self._album(chunk, cap)

    def _single(self, item, caption):
        base = {"chat_id": self.chat_id, "caption": caption, "parse_mode": "HTML"}
        if self.send_as_files:
            self._call("sendDocument", base, {"document": (item.name, item.data)})
        elif item.is_video:
            base.update(self._video_meta(item))
            base["supports_streaming"] = "true"  # form field, not JSON
            self._call("sendVideo", base, {"video": (item.name, item.data)})
        else:
            self._call("sendPhoto", base, {"photo": (item.name, item.data)})

    def _album(self, chunk, caption):
        media, files = [], {}
        for i, item in enumerate(chunk):
            key = f"file{i}"
            files[key] = (item.name, item.data)
            if self.send_as_files:
                kind = "document"
            else:
                kind = "video" if item.is_video else "photo"
            entry = {"type": kind, "media": f"attach://{key}"}
            if kind == "video":
                entry.update(self._video_meta(item))
            if i == 0 and caption:
                entry["caption"] = caption
                entry["parse_mode"] = "HTML"
            media.append(entry)
        self._call("sendMediaGroup", {"chat_id": self.chat_id, "media": json.dumps(media)}, files)

    def create_topic(self, name):
        """Creates a forum topic in this chat (bot needs 'Manage Topics'). Returns its message_thread_id."""
        r = requests.post(TG_API.format(token=self.token, method="createForumTopic"),
                          data={"chat_id": self.chat_id, "name": name[:128]}, timeout=30)
        body = r.json()
        if not body.get("ok"):
            raise RuntimeError(f"couldn't create topic '{name}': {body.get('description')}")
        return body["result"]["message_thread_id"]

    def delete_topic(self):
        """Deletes this forum topic and everything in it (bot needs 'Manage Topics'). Already gone is fine."""
        try:
            self._call("deleteForumTopic", {"chat_id": self.chat_id})
        except TopicGone:
            pass

    def topic_exists(self, topic_id):
        """False only if the topic was deleted. Probes the topic's opening message with an edit that
        Telegram always refuses ("can't be edited" vs "not found"), so nothing in the chat changes."""
        try:
            r = requests.post(TG_API.format(token=self.token, method="editMessageReplyMarkup"),
                              data={"chat_id": self.chat_id, "message_id": topic_id}, timeout=30)
        except requests.RequestException:
            return True  # can't tell - keep the topic rather than make a duplicate
        return "message to edit not found" not in r.text.lower()

    def list_chats(self):
        """Chats/channels the bot has recently seen, from getUpdates. Returns list of (id, type, title)."""
        r = requests.get(TG_API.format(token=self.token, method="getUpdates"), timeout=30)
        body = r.json()
        if not body.get("ok"):
            raise RuntimeError(body.get("description", r.text))
        chats, topics = {}, {}
        for upd in body.get("result", []):
            for key in ("message", "channel_post", "my_chat_member", "edited_message", "edited_channel_post"):
                msg = upd.get(key) or {}
                chat = msg.get("chat")
                if not chat:
                    continue
                chats[chat["id"]] = (chat["id"], chat.get("type"), chat.get("title") or chat.get("username"))
                thread = msg.get("message_thread_id")
                if thread and msg.get("is_topic_message"):
                    created = msg.get("forum_topic_created") or (msg.get("reply_to_message") or {}).get("forum_topic_created")
                    name = (created or {}).get("name") or topics.get((chat["id"], thread)) or "?"
                    topics[(chat["id"], thread)] = name
        return list(chats.values()), topics

    @staticmethod
    def _video_meta(item):
        meta = {"supports_streaming": True}
        for key in ("width", "height", "duration"):
            value = getattr(item, key)
            if value:
                meta[key] = int(value)
        return meta
