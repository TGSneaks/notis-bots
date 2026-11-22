"""Watch public X RSS feeds and send new posts to per-account Telegram topics."""

import argparse
import html
import json
import logging
import os
import re
import socket
import time
import unicodedata
import xml.etree.ElementTree as ET
import zlib
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path

import requests


HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
STATE_PATH = HERE / "state.json"
LOG_PATH = HERE / "news-notifier.log"
OLD_CONFIG_PATH = HERE.parent / "notifier" / "config.json"
FEED_HOSTS = ("fxtwitter.com", "fixupx.com")
API_HOST = "api.fxtwitter.com"
POST_URL = re.compile(r"^https://x\.com/([A-Za-z0-9_]+)/status/(\d+)$")
HANDLE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
log = logging.getLogger("news-notifier")


class TextFromHtml(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in {"br", "p", "div", "li"}:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)

    def text(self):
        value = "".join(self.parts).replace("\xa0", " ")
        value = re.sub(r"[ \t]+", " ", value)
        value = re.sub(r" *\n *", "\n", value)
        return re.sub(r"\n{3,}", "\n\n", value).strip()


def load_config():
    if not CONFIG_PATH.exists():
        raise ValueError(f"Config file not found: {CONFIG_PATH.name}")
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    accounts = cfg.get("accounts")
    if accounts is None:
        accounts = []
        seen = set()
        for category_accounts in cfg.get("categories", {}).values():
            for account in category_accounts:
                key = account.lower() if isinstance(account, str) else account
                if key not in seen:
                    accounts.append(account)
                    seen.add(key)
    if any(not isinstance(a, str) or not HANDLE.fullmatch(a) for a in accounts):
        raise ValueError("Each account must be an X username without @")
    if not accounts or len(set(a.lower() for a in accounts)) != len(accounts):
        raise ValueError("config.json needs a nonempty list of unique accounts")
    cfg["accounts"] = accounts
    emoji_for = {a: cfg.get("emoji", "📰") for a in accounts}
    for category, category_accounts in reversed(list(cfg.get("categories", {}).items())):
        for account in category_accounts:
            emoji_for[account] = cfg.get("category_emoji", {}).get(category, emoji_for[account])
    cfg["emoji_for"] = emoji_for
    label_for = {a: cfg.get("label", "") for a in accounts}
    for category, category_accounts in reversed(list(cfg.get("categories", {}).items())):
        for account in category_accounts:
            label_for[account] = cfg.get("category_label", {}).get(category, label_for[account])
    for account, label in cfg.get("account_labels", {}).items():
        if account in label_for:
            label_for[account] = label
    cfg["label_for"] = label_for
    cfg["fixed_label"] = set(cfg.get("account_labels", {}))
    cfg["filters_for"] = compile_filters(cfg.get("filters", []), accounts)
    cfg["poll_interval_seconds"] = max(60, int(cfg.get("poll_interval_seconds", 180)))
    cfg["feed_count"] = min(100, max(1, int(cfg.get("feed_count", 50))))
    return cfg


def load_token_and_old_chat():
    token = os.environ.get("NEWS_TELEGRAM_BOT_TOKEN", "").strip()
    old_chat = None
    if OLD_CONFIG_PATH.exists():
        old_cfg = json.loads(OLD_CONFIG_PATH.read_text(encoding="utf-8"))
        old_chat = str(old_cfg.get("telegram_chat_id", ""))
        if not token:
            token = str(old_cfg.get("telegram_bot_token", "")).strip()
    if not token:
        raise ValueError("Set NEWS_TELEGRAM_BOT_TOKEN or configure the existing TikTok bot")
    return token, old_chat


def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"topics": {}, "accounts": {}}


def save_state(state):
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STATE_PATH)


def parse_feed(data):
    root = ET.fromstring(data)
    if root.tag != "rss":
        raise ValueError("feed response was not RSS")
    channel = root.find("channel")
    if channel is None:
        raise ValueError("feed response has no channel")
    posts = []
    for item in channel.findall("item"):
        url = (item.findtext("link") or "").strip()
        match = POST_URL.fullmatch(url)
        if not match:
            continue
        raw = item.findtext("description") or ""
        parser = TextFromHtml()
        parser.feed(raw)
        text = parser.text() or html.unescape(item.findtext("title") or "").strip()
        date = item.findtext("pubDate")
        if not date:
            continue
        stamp = int(parsedate_to_datetime(date).timestamp())
        posts.append({"id": match.group(2), "url": url, "text": text,
                      "timestamp": stamp, "author": match.group(1)})
    return posts


def parse_api(body):
    if not isinstance(body, dict):
        raise ValueError("API response was not an object")
    if body.get("code") != 200:
        raise ValueError(f"API code {body.get('code')}")
    posts = []
    for item in body.get("results", []):
        if item.get("type") != "status":
            continue
        url = item.get("url", "")
        match = POST_URL.fullmatch(url)
        if not match or str(item.get("id")) != match.group(2):
            continue
        try:
            stamp = int(item["created_timestamp"])
        except (KeyError, TypeError, ValueError):
            continue
        posts.append({"id": match.group(2), "url": url,
                      "text": item.get("text") or "", "timestamp": stamp,
                      "author": match.group(1)})
    return posts


def fetch_feed(session, account, count, preferred=None):
    hosts = [*FEED_HOSTS, API_HOST]
    if preferred in hosts:
        hosts.remove(preferred)
        hosts.insert(0, preferred)
    errors = []
    for host in hosts:
        try:
            if host == API_HOST:
                url = f"https://{host}/2/profile/{account}/statuses"
            else:
                url = f"https://{host}/{account}/feed.xml"
            response = session.get(url, params={"count": count, "with_replies": 0}, timeout=20)
            response.raise_for_status()
            posts = parse_api(response.json()) if host == API_HOST else parse_feed(response.content)
            # A transient empty feed is not a dependable baseline for an active account.
            if not posts:
                raise ValueError("feed contained no usable posts")
            return posts, host
        except (requests.RequestException, ET.ParseError, ValueError) as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            detail = f"HTTP {status}" if status else str(exc) if isinstance(exc, ValueError) else type(exc).__name__
            errors.append(f"{host}: {detail}")
    raise RuntimeError("; ".join(errors))


class SendUncertain(RuntimeError):
    """A send request reached Telegram but no answer came back; it may have been delivered."""


def never_sent(exc):
    """True when a request failed before any byte reached Telegram (DNS, TLS, connect)."""
    import urllib3

    if isinstance(exc, (requests.ConnectTimeout, requests.exceptions.SSLError)):
        return True
    reason = getattr(exc.args[0], "reason", None) if exc.args else None
    return isinstance(exc, requests.ConnectionError) and isinstance(reason, urllib3.exceptions.NewConnectionError)


class Telegram:
    def __init__(self, token, chat_id=None):
        self.token = token
        self.chat_id = chat_id
        self.emoji = {}
        self.labels = {}
        self.fixed = set()
        self.upload_ids = {}

    def call(self, method, data=None, files=None):
        # Sends are not idempotent: once the request may have been delivered, never resend it.
        sending = method.startswith("send")
        timeout = (10, 120) if files else (10, 60)
        for attempt in range(4):
            try:
                if files:
                    for handle in files.values():
                        (handle[1] if isinstance(handle, tuple) else handle).seek(0)
                response = requests.post(
                    f"https://api.telegram.org/bot{self.token}/{method}",
                    data=data, files=files, timeout=timeout,
                )
            except requests.RequestException as exc:
                if sending and not never_sent(exc):
                    raise SendUncertain(f"Telegram {method}: no reply ({type(exc).__name__})") from None
            else:
                if sending and response.status_code >= 500:
                    # A gateway error after upload: Telegram may already have delivered it.
                    raise SendUncertain(f"Telegram {method}: HTTP {response.status_code}")
                try:
                    body = response.json()
                except ValueError:
                    raise RuntimeError(f"Telegram {method} returned HTTP {response.status_code}") from None
                if response.status_code == 429 and attempt < 3:
                    time.sleep(min(60, int(body.get("parameters", {}).get("retry_after", 5)) + 1))
                    continue
                if not body.get("ok"):
                    raise RuntimeError(f"Telegram {method}: {body.get('description', 'HTTP ' + str(response.status_code))}")
                return body["result"]
            if attempt == 3:
                raise RuntimeError(f"Telegram {method} network error")
            time.sleep(2 * (attempt + 1))
        raise RuntimeError(f"Telegram {method} stayed rate limited")

    def chats(self):
        result = self.call("getUpdates", {"timeout": 0})
        found = {}
        for update in result:
            for key in ("message", "my_chat_member", "channel_post"):
                chat = (update.get(key) or {}).get("chat")
                if chat:
                    found[chat["id"]] = chat.get("title") or chat.get("username") or "(unnamed)"
        return found

    def create_topic(self, name):
        result = self.call("createForumTopic", {"chat_id": self.chat_id, "name": name})
        return result["message_thread_id"]

    def post(self, topic_id, account, post):
        emoji = self.emoji.get(account, "📰")
        topic = headline(post["text"], self.labels.get(account, ""), account in self.fixed)
        base = {
            "chat_id": self.chat_id,
            "message_thread_id": topic_id,
            "parse_mode": "HTML",
            "reply_markup": json.dumps({"inline_keyboard": [[{"text": "Open on X ↗", "url": post["url"]}]]}),
        }
        caption = format_post(account, post, emoji, topic)
        self.download_deadline = time.monotonic() + 60
        self.downloaded = {}
        # Best visual first. Tweet media goes by URL; other images are downloaded and uploaded,
        # since Telegram's own fetcher is refused by some sites.
        for kind, url in post.get("media") or []:
            try:
                if kind == "album":
                    # Albums can't carry a button, so the X link goes in the caption instead.
                    linked = f'{caption}\n<a href="{html.escape(post["url"], quote=True)}">Open on X ↗</a>'
                    items = [{"type": "photo", "media": u, **({"caption": linked, "parse_mode": "HTML"} if i == 0 else {})}
                             for i, u in enumerate(url)]
                    self.call("sendMediaGroup", {"chat_id": self.chat_id, "message_thread_id": topic_id,
                                                 "media": json.dumps(items)})
                elif kind == "upload-album":
                    self.send_uploaded_album(topic_id, post, caption, url)
                elif kind == "video":
                    self.call("sendVideo", {**base, "video": url, "caption": caption, "supports_streaming": "true"})
                elif kind == "video-upload":
                    self.call("sendVideo", {**base, "caption": caption, "supports_streaming": "true"},
                              files={"video": ("video.mp4", download_bytes(url, MAX_UPLOAD_BYTES, self.download_deadline),
                                               "video/mp4")})
                elif kind == "photo":
                    self.call("sendPhoto", {**base, "photo": url, "caption": caption})
                else:
                    self.send_uploaded(base, caption, url)
                return
            except SendUncertain as exc:
                log.warning("@%s %s may have been sent; not retrying to avoid a duplicate: %s", account, kind, exc)
                return
            except Exception as exc:  # any bad visual just means: try the next one
                log.warning("@%s %s rejected, trying next visual: %s", account, kind, exc)
        try:
            self.call("sendMessage", {**base, "text": caption,
                                      "link_preview_options": json.dumps({"is_disabled": True})})
        except SendUncertain as exc:
            log.warning("@%s text may have been sent: %s", account, exc)

    def send_uploaded_album(self, topic_id, post, caption, urls):
        """Download each picture and send them together; needs at least two that load."""
        files = {}
        for url in urls:
            try:
                files[f"p{len(files)}"] = (f"p{len(files)}.jpg", self.fetch_image(url), "image/jpeg")
            except Exception as exc:
                log.warning("Skipping album picture %s: %s", url, exc)
        if len(files) < 2:
            raise RuntimeError("fewer than two album pictures loaded")
        linked = f'{caption}\n<a href="{html.escape(post["url"], quote=True)}">Open on X ↗</a>'
        items = [{"type": "photo", "media": f"attach://{key}",
                  **({"caption": linked, "parse_mode": "HTML"} if i == 0 else {})}
                 for i, key in enumerate(files)]
        self.call("sendMediaGroup", {"chat_id": self.chat_id, "message_thread_id": topic_id,
                                     "media": json.dumps(items)}, files=files)

    def fetch_image(self, url):
        """Download a picture once per post, within the post's download budget."""
        if url not in self.downloaded:
            self.downloaded[url] = download_image(url, getattr(self, "download_deadline", None)).getvalue()
        return BytesIO(self.downloaded[url])

    def send_uploaded(self, base, caption, url):
        cached = self.upload_ids.get(url)
        if cached:
            self.call("sendPhoto", {**base, "photo": cached, "caption": caption})
            return
        image = self.fetch_image(url)
        result = self.call("sendPhoto", {**base, "caption": caption},
                           files={"photo": ("image.jpg", image, "image/jpeg")})
        try:
            self.upload_ids[url] = result["photo"][-1]["file_id"]
        except (KeyError, IndexError, TypeError):
            pass


# Checked in order; the first match names the heading.
TOPICS = [(name, re.compile(rf"(?<![\w#@])(?:{pattern})(?:'?s)?(?!\w)", re.I)) for name, pattern in [
    ("GTA 6", r"GTA ?(?:6|VI)|Grand Theft Auto (?:6|VI)"),
    ("Fortnite", r"Fortnite"),
    ("Call of Duty", r"Call of Duty|CoD|Warzone|Black Ops|Modern Warfare"),
    ("Overwatch", r"Overwatch"),
    ("Rocket League", r"Rocket League"),
    ("Roblox", r"Roblox"),
    ("Minecraft", r"Minecraft"),
    ("Valorant", r"Valorant"),
    ("Apex Legends", r"Apex Legends"),
    ("League of Legends", r"League of Legends"),
    ("Marvel Rivals", r"Marvel Rivals"),
    ("Battlefield", r"Battlefield"),
    ("Counter-Strike", r"CS2|Counter-Strike"),
    ("Pokémon", r"Pok[eé]mon"),
    ("Elden Ring", r"Elden Ring"),
    ("EA FC", r"EA (?:Sports )?FC"),
    ("NBA 2K", r"NBA ?2K\d*"),
    ("Nintendo", r"Nintendo|Switch 2"),
    ("PlayStation", r"PlayStation|PS5|PSN"),
    ("Xbox", r"Xbox|Game Pass"),
    ("Steam", r"Steam"),
    ("Dragon Ball", r"Dragon ?Ball(?: (?:Super|Z|GT|Daima|Sparking!? Zero))?|DBS|DBZ"),
    ("One Piece", r"One Piece"),
    ("Naruto", r"Naruto|Boruto"),
    ("Jujutsu Kaisen", r"Jujutsu Kaisen|JJK"),
    ("Demon Slayer", r"Demon Slayer|Kimetsu no Yaiba"),
    ("Attack on Titan", r"Attack on Titan|Shingeki no Kyojin"),
    ("My Hero Academia", r"My Hero Academia|Boku no Hero"),
    ("Chainsaw Man", r"Chainsaw Man"),
    ("Solo Leveling", r"Solo Leveling"),
    ("Bleach", r"(?-i:Bleach|BLEACH)"),
    ("Frieren", r"Frieren"),
    ("Spy x Family", r"Spy ?[x×] ?Family"),
    ("Sakamoto Days", r"Sakamoto Days"),
    ("NVIDIA", r"NVIDIA|RTX|GeForce"),
    ("OpenAI", r"OpenAI|ChatGPT|GPT-?\d[\w.]*|Codex|Sora"),
    ("Google", r"Google|Gemini|DeepMind"),
    ("Anthropic", r"Anthropic|Claude"),
    ("xAI", r"xAI|Grok"),
    ("Meta", r"Meta AI|Llama"),
    ("Apple", r"Apple|iPhone|iOS"),
    ("Microsoft", r"Microsoft|Copilot|Windows"),
    ("Mistral", r"Mistral"),
]]


def headline(text, label, fixed=False):
    """The account's fixed topic, else the topic the post names first, else the group label."""
    if not fixed:
        hits = [(m.start(), i, name) for i, (name, pattern) in enumerate(TOPICS) if (m := pattern.search(text))]
        if hits:
            return min(hits)[2]
    return label


TRAILING_LINKS = re.compile(r"(\s*https?://\S+)+\s*$")


# Creator-code plugs and hashtag tails add nothing to a notification.
FILLER = re.compile(r"\buse (?:my |creator |the )?code\b|\bsupport (?:me|us|the channel)\b|#ad\b", re.I)
TRAILING_TAGS = re.compile(r"(?:\s*#\w+)+\s*$")


def utf16_len(text):
    return len(text.encode("utf-16-le")) // 2


def format_post(account, post, emoji, topic=""):
    """Compact card: emoji + TOPIC · name (what a phone preview shows), then the text in a quote."""
    text = TRAILING_LINKS.sub("", post["text"]).strip()
    text = "\n".join(line for line in text.splitlines() if not FILLER.search(line))
    text = TRAILING_TAGS.sub("", text).strip()
    # Telegram counts captions in UTF-16 units (emoji and styled letters count double); cap at 1024 total.
    if utf16_len(text) > 600:
        while utf16_len(text) > 598:
            text = text[:max(1, int(len(text) * 0.95))]
        cut = text.rfind(" ")
        text = (text[:cut] if cut > len(text) - 40 else text) + "…"
    head = (f"{emoji} <b>{html.escape(topic.upper())}</b> · {html.escape(account)}" if topic
            else f"{emoji} <b>{html.escape(account)}</b>")
    if not text:
        return head
    quote = "<blockquote expandable>" if len(text) > 250 else "<blockquote>"
    return f"{head}\n{quote}{html.escape(text)}</blockquote>"


PROMO = re.compile(
    r"giveaway|giving away|give away|enter to win|retweet to win|"
    r"join us live|we(?:'| a)re live|going live|live now|watch live|"
    r"^\s*up next\b|(?:first|next|final) guest",
    re.I | re.M)
LINK = re.compile(r"https?://\S+")
# Sale wording only; bare "deal"/"discount"/"% off" also appear in contract news and stats.
SALE = re.compile(
    r"\bon sale\b|\b\d{1,2}% off (?:on|at|in|for|until|through|during|everything|select)\b|"
    r"\b(?:sale|discounts?) (?:is |are )?(?:now )?(?:live|starts?|started|ends?|ending)\b|"
    r"\b(?:Steam|PlayStation|PS Store|eShop|Xbox|Epic|Nintendo|publisher|Autumn|Winter|Summer|Spring"
    r"|holiday|Halloween|Lunar New Year) (?:Store )?sale\b|"
    r"\b(?:Black Friday|Cyber Monday|Prime Day)\b|\bprice (?:drop|cut)\b|\blowest price\b",
    re.I)


def _plain(text):
    """Lowercase, accents removed and curly quotes straightened, so "Procházka" matches "prochazka"."""
    text = unicodedata.normalize("NFKD", text.replace("’", "'").replace("‘", "'"))
    return "".join(c for c in text if not unicodedata.combining(c))


def compile_filters(filters, accounts):
    """Config "filters": [{"accounts": [...], "reason": "...", "require": [regex], "drop": [regex]}].
    A post from a listed account is sent only if it matches a "require" pattern and no "drop" pattern."""
    known = {a.lower(): a for a in accounts}
    out = {}
    for f in filters:
        def join(patterns):
            return re.compile("|".join(f"(?:{_plain(p)})" for p in patterns), re.I | re.M) if patterns else None
        rule = (f.get("reason", "filtered"), join(f.get("require", [])), join(f.get("drop", [])))
        for account in f.get("accounts", []):
            if account.lower() not in known:
                raise ValueError(f"filters: @{account} is not one of the watched accounts")
            out.setdefault(known[account.lower()], []).append(rule)
    return out


def filter_reason(filters, text):
    plain = _plain(text)
    words = LINK.sub(" ", plain)  # "require" ignores links: article URLs name other fights
    for reason, require, drop in filters:
        if (require and not require.search(words)) or (drop and drop.search(plain)):
            return reason
    return ""


def skip_reason(text):
    """Return why a post isn't worth a notification, or "" to send it."""
    if PROMO.search(text):
        return "giveaway/promo"
    if SALE.search(text):
        return "sale"
    plain = LINK.sub("", text).strip()
    # Bait: one short question and nothing else (no link, no second sentence with the actual news).
    if (plain.endswith("?") and len(plain) <= 140 and not LINK.search(text)
            and len(re.findall(r"[.!?]+(?:\s|$)", plain)) == 1):
        return "bait question"
    return ""


MAX_VIDEO_BYTES = 18_000_000   # Telegram fetches video URLs itself up to 20 MB
MAX_UPLOAD_BYTES = 45_000_000  # bot uploads are capped at 50 MB
MAX_IMAGE_BYTES = 12_000_000
VISUALS_SECONDS = 40           # total time one post may spend looking for pictures
HTTP_HEADERS = {"User-Agent": "news-notifier/1.0 (personal non-commercial Telegram news bot)"}
OG_IMAGE = re.compile(
    r"<meta[^>]+(?:property|name)=[\"'](?:og:image|twitter:image)[\"'][^>]*content=[\"']([^\"']+)"
    r"|<meta[^>]+content=[\"']([^\"']+)[\"'][^>]*(?:property|name)=[\"'](?:og:image|twitter:image)[\"']",
    re.I)
YOUTUBE = re.compile(r"(?:youtu\.be/|youtube\.com/(?:watch\?(?:.*&)?v=|live/|shorts/|embed/))([\w-]{11})")
MENTION = re.compile(r"(?<!\w)@(\w{1,15})")

# --- Who is the post about? -------------------------------------------------------------
# Sentence breaks, so "...the Heat. LeBron James..." can't glue names across sentences.
SENTENCE = re.compile(r"(?<=[a-z0-9)\"”'’]{2}[.!?;:])\s+|\n+|\s[-–—|•]\s|\s+x\s+|\s+vs\.?\s+|\s+&\s+", re.I)
_CAP = r"(?:[A-Z]\.){1,3}(?:[A-Z][a-z][\w'-]*)?|(?:[A-Z]|[a-z]{1,2}[A-Z])[\w'-]*"  # "J.J.", "D.Va", "xQc"
_JOIN = r"(?:tha|da|de|del|du|di|dos|der|van|von|la|le|al|bin|ibn)(?=\s+[A-Z])"         # "Dricus du Plessis"
NAME_ANY = re.compile(rf"(?<![\w@#$])((?:{_CAP})(?:\s+(?:{_CAP}|{_JOIN})){{0,5}})")
NBA_TEAMS = {"hawks", "celtics", "nets", "hornets", "bulls", "cavaliers", "cavs", "pistons", "pacers", "heat",
             "bucks", "knicks", "magic", "76ers", "sixers", "raptors", "wizards", "mavericks", "mavs", "nuggets",
             "warriors", "rockets", "clippers", "lakers", "grizzlies", "timberwolves", "wolves", "pelicans",
             "thunder", "suns", "blazers", "trail blazers", "kings", "spurs", "jazz"}
NOT_NAMES = {
    "breaking", "the", "this", "that", "these", "those", "new", "official", "officially", "update", "report",
    "reports", "reportedly", "per", "via", "watch", "full", "here", "here's", "just", "first", "big", "huge",
    "today", "tonight", "tomorrow", "yesterday", "live", "now", "a", "an", "and", "in", "on", "at", "for",
    "from", "with", "after", "before", "it", "it's", "we", "we're", "we've", "you", "your", "our", "my", "i",
    "i'm", "he", "she", "they", "his", "her", "their", "there", "what", "why", "how", "who", "when", "if",
    "is", "are", "was", "will", "can", "did", "do", "does", "to", "one", "all", "more", "most", "every",
    "some", "no", "yes", "so", "but", "or", "rt", "confirmed", "rumor", "rumour", "leak", "leaked",
    "exclusive", "sources", "source", "ok", "congratulations", "congrats", "fans", "according", "finally",
    "meanwhile", "reminder", "welcome", "happy", "thank", "thanks", "trying", "sounds", "probably", "worth",
    "credit", "whether", "also", "still", "even", "only", "again", "never", "always", "next", "last",
    "former", "legendary", "star", "superstar", "veteran", "rookie", "coming", "def", "leaked", "revealed",
}
# Job titles in front of a name: "Nvidia CEO Jensen Huang" -> "Jensen Huang".
ROLES = {"ceo", "cto", "cfo", "coo", "founder", "co-founder", "cofounder", "president", "chairman", "coach",
         "gm", "owner", "commissioner", "champion", "guard", "forward", "center", "rapper", "streamer",
         "youtuber", "insider", "reporter", "analyst", "director", "producer", "developer", "dev", "head",
         "lead", "boss", "manager", "agent", "actor", "actress", "singer", "creator", "host", "legend"}
# Common nouns, places and labels a wiki might also have a page for; never a "subject" picture.
GENERIC = {
    "season", "item shop", "battle pass", "battle royale", "ranked", "stadium", "reloaded", "update", "patch",
    "patch notes", "map", "maps", "mode", "modes", "event", "events", "challenge", "challenges",
    "weekly challenges", "news", "game", "games", "trailer", "server", "servers", "store", "shop", "skin",
    "skins", "bundle", "bundles", "collab", "collaboration", "crossover", "rewards", "reward", "twitch",
    "youtube", "kick", "x", "twitter", "media", "video", "clip", "stream", "players", "player", "people",
    "everyone", "team", "teams", "week", "day", "night", "weekend", "monday", "tuesday", "wednesday",
    "thursday", "friday", "saturday", "sunday", "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december", "us", "usa", "uk", "eu", "na", "pc",
    "dlc", "beta", "alpha", "early access", "launch", "release", "decal", "decals", "outfit", "outfits",
    "pickaxe", "glider", "emote", "emotes", "wrap", "wraps", "vehicle", "vehicles", "treasure", "chest",
    "chests", "vault", "vaults", "key", "keys", "loadout", "loadouts", "gizmo", "gizmos", "weapon",
    "weapons", "xp", "double xp", "york", "zealand", "jersey", "orleans", "mexico", "delhi", "england",
    "los santos", "vice city", "liberty city", "miami", "los angeles", "las vegas", "new york", "abu dhabi",
    "london", "paris", "tokyo", "japan", "china", "india", "canada", "australia", "brazil", "europe",
    "america", "game awards", "the game awards", "goty", "game of the year", "nba finals", "playoffs",
    "all-star", "draft", "free agency", "trade", "trades", "media day", "press conference", "weigh-ins",
    "main event", "co-main event", "fight night", "card", "title", "belt", "ama", "q&a", "ps5 pro",
    "switch 2", "xbox series x", "steam deck", "discord", "reddit", "instagram", "tiktok", "spotify",
    "epic games", "rockstar games", "sony", "nintendo switch", "chapter", "year", "pro", "wars", "icon series",
    "rocket pass", "ranked reload", "zero build", "prime video", "netflix", "crunchyroll", "hulu", "disney+",
    "korea", "lisbon", "season 2", "part 2", "pv", "key visual", "main visual", "new visual", "teaser",
}
# Accept a Wikipedia page as a subject only if its short description says person or character.
PERSON_DESC = re.compile(
    r"\b(?:characters?|mascots?|singers?|rappers?|musicians?|vocaloid|virtual|voice ?bank|voice synthesi[sz]er|"
    r"idols?|actors?|actress|athletes?|players?|fighters?|martial artists?|boxers?|wrestlers?|kickboxers?|"
    r"streamers?|youtubers?|influencers?|personality|comedians?|presenters?|hosts?|coach|executive|"
    r"businessman|businesswoman|entrepreneur|politician|footballers?|creators?|designers?|director|producer|"
    r"journalists?|industrialist|technologist|investor|engineer|founder|scientist|researcher|programmer|"
    r"artists?|models?|dj)\b|\(born \d{4}\)|born \d{4}",
    re.I)
QUALIFIER_OK = re.compile(PERSON_DESC.pattern + r"|basketball|football|baseball|hockey|soccer|gamer|mixed martial arts|\bMMA\b|born", re.I)
FICTIONAL_DESC = re.compile(r"character|mascot|vocaloid|virtual|voice ?bank|voice synthesi[sz]er", re.I)
# A game wiki page counts as a subject only if it is filed under characters/skins/etc.
CHAR_CATEGORY = re.compile(
    r"\b(?:characters?|heroes|hero|outfits?|operators?|legends|agents?|champions?|protagonists?|antagonists?|"
    r"skins?|survivors?|playable|npcs?|bosses|boss|vehicles?|cars?|bod(?:y|ies)|collabs?|hunters?|pirates?|"
    r"sorcerers?|slayers?|ninjas?|shinobi|humans?|saiyans?)\b", re.I)
LOGO_FILE = re.compile(r"logo|wordmark|\.svg", re.I)
SPORT_DESC = {"UFC": re.compile(r"martial artist|fighter|boxer|wrestler|kickboxer", re.I),
              "NBA": re.compile(r"basketball", re.I)}
SEARCH_HINT = {"UFC": "mixed martial artist", "NBA": "basketball player", "Streamers": "streamer",
               "YouTube": "YouTuber", "Creators": "internet personality", "Tech & AI": "technology"}
# Each game's own wiki knows its characters, skins and cars better than Wikipedia.
FANDOM_WIKIS = {
    "Overwatch": "overwatch", "Fortnite": "fortnite", "Rocket League": "rocketleague", "GTA 6": "gta",
    "Call of Duty": "callofduty", "Valorant": "valorant", "Apex Legends": "apexlegends",
    "Marvel Rivals": "marvelrivals", "League of Legends": "leagueoflegends", "Roblox": "roblox",
    "Minecraft": "minecraft", "Pokémon": "pokemon", "Elden Ring": "eldenring",
    "Counter-Strike": "counterstrike", "Battlefield": "battlefield",
    "Dragon Ball": "dragonball", "One Piece": "onepiece", "Naruto": "naruto", "Jujutsu Kaisen": "jujutsu-kaisen",
    "Demon Slayer": "kimetsu-no-yaiba", "Attack on Titan": "attackontitan", "My Hero Academia": "myheroacademia",
    "Chainsaw Man": "chainsaw-man", "Solo Leveling": "solo-leveling", "Bleach": "bleach", "Frieren": "frieren",
    "Spy x Family": "spy-x-family",
}
# Series whose name is also a character's ("Naruto Uzumaki"): never trim it off the front of a name.
CHARACTER_TOPICS = {"Naruto", "Frieren", "Bleach", "Dragon Ball", "One Piece"}
# Fallback when a post names no one: the game, league or company itself.
TOPIC_IMAGES = {
    "Call of Duty": "steam:1938090", "Overwatch": "steam:2357570", "Rocket League": "steam:252950",
    "Apex Legends": "steam:1172470", "Marvel Rivals": "steam:2767030", "Counter-Strike": "steam:730",
    "Elden Ring": "steam:1245620", "Battlefield": "steam:2807960", "EA FC": "steam:3405690",
    "NBA 2K": "steam:3472040",
    "GTA 6": "wiki:Grand Theft Auto VI", "Minecraft": "file:Minecraft Key-art.png",
    "Fortnite": "file:Fortnite logo padded.svg", "Roblox": "file:Roblox Logo Black.svg",
    "Valorant": "file:Valorant logo - pink color version.svg",
    "League of Legends": "file:League of Legends 2019 vector.svg",
    "Pokémon": "file:International Pokémon logo.svg", "Nintendo": "file:Nintendo logo.svg",
    "PlayStation": "file:PlayStation logo and wordmark.svg", "Xbox": "file:Xbox logo (2019).svg",
    "Steam": "wiki:Steam (service)", "OpenAI": "file:OpenAI logo 2025 (wordmark).svg",
    "Anthropic": "file:Anthropic logo.svg", "xAI": "file:XAI Logo.svg",
    "Meta": "file:Meta Platforms Inc. logo.svg", "Apple": "file:Apple logo black.svg",
    "Microsoft": "file:Microsoft logo (2012).svg", "Mistral": "file:Mistral AI logo (2025–).svg",
    "UFC": "file:UFC logo.svg", "NBA": "file:National Basketball Association logo 2017-present.svg",
    "Outage": "file:Downdetector Logo.svg",
}


# --- Polite, failure-aware wiki access ----------------------------------------------------
class WikiBusy(Exception):
    """The wiki is rate limiting or down; try again later and remember nothing."""


_host_block_until = {}
_host_last_call = {}
_cache = {}  # key -> (value, expires_at); only definite answers are stored
HIT_TTL, MISS_TTL = 7 * 86400, 86400


def cache_get(key):
    entry = _cache.get(key)
    if entry and entry[1] > time.time():
        return True, entry[0]
    return False, None


def cache_put(key, value):
    if len(_cache) > 5000:
        _cache.clear()
    _cache[key] = (value, time.time() + (HIT_TTL if value else MISS_TTL))


def wiki_get(session, url, params):
    """GET a MediaWiki/Wikidata API as JSON, at most one call per second per host;
    raise WikiBusy (and back off) on 429, 5xx, network errors or non-JSON replies."""
    base = url.split("/")[2]
    host = base
    if params.get("generator") == "search" or params.get("list") == "search":
        host += " search"  # search has its own, stricter rate limit; don't let it block plain lookups
    if time.time() < _host_block_until.get(host, 0):
        raise WikiBusy(host)
    wait = 1.0 - (time.monotonic() - _host_last_call.get(host, -10))
    if wait > 0:
        time.sleep(wait)
    try:
        response = session.get(url, params={"format": "json", **params}, headers=HTTP_HEADERS, timeout=(5, 10))
    except requests.RequestException as exc:
        _host_block_until[host] = time.time() + 30
        raise WikiBusy(f"{host}: {type(exc).__name__}") from None
    finally:
        _host_last_call[host] = time.monotonic()
    if response.status_code == 429 or response.status_code >= 500:
        try:
            pause = int(response.headers.get("retry-after", 60))
        except ValueError:
            pause = 60
        until = time.time() + min(max(pause, 30), 600)
        for key in (base, base + " search"):
            _host_block_until[key] = max(_host_block_until.get(key, 0), until)
        raise WikiBusy(f"{host}: HTTP {response.status_code}")
    try:
        body = response.json()
    except ValueError:
        _host_block_until[host] = time.time() + 30
        raise WikiBusy(f"{host}: non-JSON reply") from None
    if not isinstance(body, dict) or "error" in body or response.headers.get("MediaWiki-API-Error"):
        # e.g. "search is too busy" arrives as HTTP 200; that is not an answer.
        _host_block_until[host] = time.time() + 60
        raise WikiBusy(f"{host}: API error {(body.get('error') or {}).get('code') if isinstance(body, dict) else ''}")
    return body


def resolve_titles(data):
    """Map each resolved page title back to the names that were asked for."""
    steps = {}
    for step in (data.get("normalized") or []) + (data.get("redirects") or []):
        steps[step["from"]] = step["to"]
    return steps


def lookup_titles(session, api, names, props):
    """Exact-title lookup of names on one wiki: {name: page} for pages that exist."""
    data = (wiki_get(session, api, {"action": "query", "redirects": 1, "titles": "|".join(names), **props})
            .get("query") or {})
    steps = resolve_titles(data)
    pages = {p["title"]: p for p in (data.get("pages") or {}).values() if "missing" not in p}
    found = {}
    for name in names:
        title, hops = name, 0
        while title in steps and hops < 5:
            title, hops = steps[title], hops + 1
        if title in pages:
            found[name] = pages[title]
    return found


def wikidata_images(session, items):
    """Main photo (property P18) for Wikidata items: {item id: image url}."""
    if not items:
        return {}
    data = wiki_get(session, "https://www.wikidata.org/w/api.php",
                    {"action": "wbgetentities", "ids": "|".join(items), "props": "claims"})
    images = {}
    for item, entity in (data.get("entities") or {}).items():
        for claim in (entity.get("claims") or {}).get("P18") or []:
            filename = ((claim.get("mainsnak") or {}).get("datavalue") or {}).get("value")
            if filename:
                images[item] = ("https://commons.wikimedia.org/wiki/Special:FilePath/"
                                f"{requests.utils.quote(filename)}?width=1000")
                break
    return images


# --- Finding names in a post ---------------------------------------------------------------
def subject_names(text, topic):
    """Proper names the post is about, in order: "Kiriko", "Hatsune Miku", "Conor McGregor"."""
    clean = MENTION.sub(" ", LINK.sub(" ", text)).replace("’", "'").replace("‘", "'")
    clean = re.sub(r'(?<=\w)\s+["“][^"”\n]{1,20}["”]\s+(?=[A-Z])', " ", clean)   # Jon "Bones" Jones
    clean = re.sub(r"#(\w+)", lambda m: " | " if re.search(r"\d", m.group(1)) else m.group(1), clean)
    for topic_name, pattern in TOPICS:  # a game/company name is never a subject, and must not split into one
        if topic_name not in CHARACTER_TOPICS:
            clean = pattern.sub(" | ", clean)
    names, seen = [], {topic.lower(), ""}
    for chunk in SENTENCE.split(clean):
        if not re.search(r"[a-z]", chunk):
            continue  # ALL-CAPS headline: capitals say nothing about names there
        for match in NAME_ANY.finditer(chunk):
            for n, piece in enumerate(re.split(r"'s\b|'(?=\s|$)", match.group(1))):
                words = piece.strip(" .'-").split()
                if n and len(words) < 2:
                    continue  # "Kai Cenat's Mafiathon" -> keep Kai Cenat, drop "Mafiathon"
                for i in range(len(words) - 1, -1, -1):  # cut at the last filler/role word
                    low = words[i].lower().strip(".")
                    if low in NOT_NAMES or (low in ROLES and i < len(words) - 1):
                        words = words[i + 1:]
                        break
                name = " ".join(words)
                key = name.lower()
                if not name or key in seen or key in GENERIC or key in NOT_NAMES or key in ROLES:
                    continue
                if words[-1].lower() in NBA_TEAMS:
                    continue  # "Lakers", "Miami Heat"
                if name.isupper() and len(name.replace(" ", "")) <= 5:
                    continue  # acronyms: NBA, PS5, GOTY
                if any(pattern.fullmatch(name) for _, pattern in TOPICS):
                    continue
                seen.add(key)
                names.append(name)
    return names[:8]


def fandom_subjects(session, wiki, names, topic=""):
    """Characters/skins/cars on the game's own wiki: {name: image}."""
    pages = lookup_titles(session, f"https://{wiki}.fandom.com/api.php", names, {
        "prop": "pageimages|pageprops|categories", "piprop": "thumbnail", "pithumbsize": 1000,
        "cllimit": "max"})
    found = {}
    for name, page in pages.items():
        if "disambiguation" in (page.get("pageprops") or {}) or not page.get("thumbnail"):
            continue
        if page["title"].lower() == topic.lower() or any(p.fullmatch(page["title"]) for _, p in TOPICS):
            continue  # the game's own page is not a subject
        categories = " ".join(c.get("title", "") for c in page.get("categories") or [])
        if topic:
            categories = re.sub(re.escape(topic), " ", categories, flags=re.I)  # "Weapons in Apex Legends"
        if CHAR_CATEGORY.search(categories):
            found[name] = page["thumbnail"]["source"]
    return found


def wikipedia_subjects(session, names, topic):
    """People and famous characters on Wikipedia: {name: image}. Logos are swapped for the
    subject's own photo from Wikidata, or dropped."""
    pages = lookup_titles(session, "https://en.wikipedia.org/w/api.php", names, {
        "prop": "pageimages|pageprops|description", "piprop": "thumbnail|name", "pithumbsize": 1000,
        "pilicense": "any"})
    found, need_photo, not_people = {}, {}, set()
    sport = SPORT_DESC.get(topic)
    for name, page in pages.items():
        props = page.get("pageprops") or {}
        desc = page.get("description") or ""
        if "disambiguation" in props:
            continue
        if not PERSON_DESC.search(desc) or (sport and not sport.search(desc)):
            not_people.add(name)  # a real page, just not a person/character for this topic
            continue
        thumb = (page.get("thumbnail") or {}).get("source")
        if thumb and not LOGO_FILE.search(page.get("pageimage") or ""):
            found[name] = thumb
        elif props.get("wikibase_item"):
            need_photo[name] = props["wikibase_item"]
    try:
        photos = wikidata_images(session, list(dict.fromkeys(need_photo.values())))
    except WikiBusy as exc:
        log.info("Wikidata busy; keeping Wikipedia pictures: %s", exc)
        photos = {}
    for name, item in need_photo.items():
        if item in photos:
            found[name] = photos[item]
    return found, not_people


def wikipedia_search(session, name, topic):
    """One search for a name the exact lookup missed (e.g. "Mike Perry" is a disambiguation page)."""
    hint = SEARCH_HINT.get(topic, topic if topic in TOPIC_IMAGES or topic in FANDOM_WIKIS else "")
    data = wiki_get(session, "https://en.wikipedia.org/w/api.php", {
        "action": "query", "generator": "search", "gsrsearch": f'"{name}" {hint}'.strip(), "gsrlimit": 5,
        "prop": "pageimages|description|pageprops", "piprop": "thumbnail", "pithumbsize": 1000,
        "pilicense": "any"})
    sport = SPORT_DESC.get(topic)
    for page in sorted(((data.get("query") or {}).get("pages") or {}).values(), key=lambda p: p.get("index", 0)):
        desc = page.get("description") or ""
        if not page.get("thumbnail") or "disambiguation" in (page.get("pageprops") or {}):
            continue
        m = re.match(r"(.*?)\s*\((.*)\)$", page["title"])
        title, qualifier = (m.group(1), m.group(2)) if m else (page["title"], "")
        if qualifier and not QUALIFIER_OK.search(qualifier):
            continue  # "(film)", "(album)", "(video game)"
        if not PERSON_DESC.search(desc) or (sport and not sport.search(desc)):
            continue
        if title.lower() == name.lower():
            return page["thumbnail"]["source"]
        if " " not in name:
            # One word: a fight/trade surname ("Jones vs Aspinall") or a famous character ("Miku").
            if sport and sport.search(desc) and title.split()[-1].lower() == name.lower():
                return page["thumbnail"]["source"]
            if FICTIONAL_DESC.search(desc) and re.search(rf"\b{re.escape(name)}\b", title, re.I):
                return page["thumbnail"]["source"]
    return None


def subject_images(session, text, topic, limit=4, deadline=None):
    """A picture of each person or character the post names, in the order they're named."""
    deadline = deadline or time.monotonic() + VISUALS_SECONDS
    names = subject_names(text, topic)
    wiki = FANDOM_WIKIS.get(topic)
    scope = wiki or f"wp:{topic}"  # answers depend on the topic (sport filter, search hint)
    results, todo = {}, []
    for name in names:
        hit, value = cache_get((name.lower(), scope))
        if hit:
            results[name] = value
        else:
            todo.append(name)
    answered = {}
    try:
        if todo and wiki and time.monotonic() < deadline:
            try:
                answered.update(fandom_subjects(session, wiki, todo, topic))
            except WikiBusy as exc:
                log.info("Game wiki busy: %s", exc)
        rest = [n for n in todo if n not in answered]
        not_people = set()
        if rest and time.monotonic() < deadline:
            try:
                found, not_people = wikipedia_subjects(session, rest, topic)
                answered.update(found)
                for name in not_people:
                    answered.setdefault(name, None)  # a real page, but not a person: a definite no
            except WikiBusy as exc:
                log.info("Wikipedia busy: %s", exc)
        # Searches are rationed: full names first; one-word names only where they can work
        # (fight/trade surnames, or characters in a game/anime topic).
        queue = sorted((n for n in todo if n not in answered), key=lambda n: " " not in n)
        queue = [n for n in queue if " " in n or topic in SPORT_DESC or topic in FANDOM_WIKIS]
        for name in queue[:2]:
            if time.monotonic() >= deadline:
                break
            try:
                answered[name] = wikipedia_search(session, name, topic)
            except WikiBusy as exc:
                log.info("Wikipedia search busy: %s", exc)
                break
    finally:
        for name, value in answered.items():  # only definite answers are remembered
            cache_put((name.lower(), scope), value)
    results.update(answered)
    images = []
    for name in names:
        if results.get(name) and results[name] not in images:
            images.append(results[name])
    return images[:limit]


# --- Other picture sources -----------------------------------------------------------------
def media_candidates(tweet):
    """Visuals attached to a tweet, best first: its video/photos, a quoted post's, then link cards."""
    found = []
    own = tweet.get("media") or {}
    photos = [p["url"].replace("name=orig", "name=large") for p in own.get("photos") or []]
    if len(photos) > 1 and not own.get("videos"):
        found.append(("album", photos[:10]))  # e.g. a full item shop over several images
    for source in (tweet, tweet.get("quote") or {}):
        media = source.get("media") or {}
        for video in media.get("videos") or []:
            duration = float(video.get("duration") or 0)
            mp4s = [f for f in video.get("formats") or video.get("variants") or []
                    if f.get("container") == "mp4" and f.get("bitrate")
                    and f["bitrate"] * duration / 8 < MAX_VIDEO_BYTES]
            if mp4s:
                best = max(mp4s, key=lambda f: f["bitrate"])["url"]
                found += [("video", best), ("video-upload", best)]  # upload ourselves if Telegram can't fetch it
            if video.get("thumbnail_url"):
                found.append(("photo", video["thumbnail_url"]))
        for photo in media.get("photos") or []:
            found.append(("photo", photo["url"].replace("name=orig", "name=large")))
    for source in (tweet, tweet.get("quote") or {}):
        image = ((source.get("card") or {}).get("image") or {}).get("url")
        if image:
            found.append(("photo", image))
    return found


def link_image(session, text, deadline):
    """The linked article's own photo: a YouTube thumbnail, or the page's og:image."""
    for url in LINK.findall(text):
        if re.match(r"https?://(?:www\.)?(?:x|twitter)\.com/", url):
            continue
        video = YOUTUBE.search(url)
        if video:
            return f"https://i.ytimg.com/vi/{video.group(1)}/hqdefault.jpg"
        head = b""
        try:
            with session.get(url, timeout=(5, 8), stream=True, headers={"User-Agent": "Mozilla/5.0"}) as response:
                for part in response.iter_content(65536):
                    head += part
                    if len(head) >= 300_000 or time.monotonic() >= deadline:
                        break
        except Exception:  # any network or decoding trouble just means "no article photo"
            return None
        match = OG_IMAGE.search(head.decode("utf-8", "ignore"))
        image = match and html.unescape(match.group(1) or match.group(2))
        return image if image and image.startswith("http") else None
    return None


def profile_avatar(session, account):
    for url in (f"https://{API_HOST}/2/profile/{account}", f"https://{API_HOST}/{account}"):
        try:
            user = session.get(url, timeout=(5, 10)).json().get("user") or {}
        except Exception:
            continue
        avatar = user.get("avatar_url")
        if avatar:
            return avatar.replace("_normal.", "_400x400.")
    return None


def topic_image(session, topic):
    ref = TOPIC_IMAGES.get(topic)
    if not ref:
        return None
    kind, _, value = ref.partition(":")
    if kind == "steam":
        return f"https://cdn.cloudflare.steamstatic.com/steam/apps/{value}/capsule_616x353.jpg"
    if kind == "file":
        return f"https://commons.wikimedia.org/wiki/Special:FilePath/{requests.utils.quote(value)}?width=1000"
    hit, image = cache_get(("topic", ref))
    if hit:
        return image
    try:
        data = wiki_get(session, "https://en.wikipedia.org/w/api.php", {
            "action": "query", "titles": value, "prop": "pageimages", "piprop": "thumbnail",
            "pithumbsize": 1000, "pilicense": "any", "redirects": 1})
    except WikiBusy:
        return None
    thumbs = [p["thumbnail"]["source"] for p in ((data.get("query") or {}).get("pages") or {}).values()
              if p.get("thumbnail")]
    image = thumbs[0] if thumbs else None
    cache_put(("topic", ref), image)
    return image


def fetch_media(session, account, post_id, text="", topic=""):
    """All visuals worth trying, best first. The tweet's own media always wins; without any,
    the linked article's photo plus a picture of everyone the post names (an album when several),
    then mentioned accounts' pictures, the topic's image, and finally the poster's picture."""
    deadline = time.monotonic() + VISUALS_SECONDS

    def attempt(label, fn, default=None):
        if time.monotonic() >= deadline:
            return default
        try:
            return fn()
        except Exception:  # one broken source must not cost the others
            log.exception("@%s %s lookup failed", account, label)
            return default

    tweet = attempt("tweet", lambda: session.get(f"https://{API_HOST}/{account}/status/{post_id}",
                                                 timeout=(5, 15)).json().get("tweet") or {}, {})
    text = tweet.get("text") or text
    found = media_candidates(tweet)
    if found:
        return found
    visuals = []
    article = attempt("article", lambda: link_image(session, text, deadline))
    subjects = attempt("subject", lambda: subject_images(session, text, topic, deadline=deadline), [])
    for url in [article, *subjects]:
        if url and url not in visuals:
            visuals.append(url)
    if not visuals:
        mentions = list(dict.fromkeys(m.lower() for m in MENTION.findall(text) if m.lower() != account.lower()))
        for mention in mentions[:3]:
            avatar = attempt("mention", lambda m=mention: profile_avatar(session, m))
            if avatar and avatar not in visuals:
                visuals.append(avatar)
    if not visuals:
        image = attempt("topic", lambda: topic_image(session, topic)) or attempt(
            "poster", lambda: profile_avatar(session, account))
        visuals = [image] if image else []
    visuals = visuals[:4]
    found = [("upload-album", visuals)] if len(visuals) > 1 else []
    return found + [("upload", url) for url in visuals]


def download_bytes(url, cap, deadline=None):
    """Download up to cap bytes within the deadline; refuse anything larger or slower."""
    data = BytesIO()
    deadline = deadline or time.monotonic() + 60
    with requests.get(url, timeout=(5, 20), headers=HTTP_HEADERS, stream=True) as response:
        response.raise_for_status()
        if int(response.headers.get("content-length") or 0) > cap:
            raise OSError(f"{url} is larger than {cap} bytes")
        for part in response.iter_content(65536):
            data.write(part)
            if data.tell() > cap:
                raise OSError(f"{url} is larger than {cap} bytes")
            if time.monotonic() > deadline:
                raise OSError(f"{url} took too long to download")
    data.seek(0)
    return data


def download_image(url, deadline=None):
    """Fetch an image and return JPEG bytes Telegram accepts; logos get a white background.
    Any decoding problem, or a blank/near-uniform picture, is reported as OSError."""
    import struct

    from PIL import Image, ImageStat

    raw = download_bytes(url, MAX_IMAGE_BYTES, deadline)
    try:
        image = Image.open(raw)
        image.load()
        if image.mode in ("RGBA", "LA", "P"):
            image = image.convert("RGBA")
            canvas = Image.new("RGB", (image.width + 160, image.height + 160), "white")
            canvas.paste(image, (80, 80), image)
            image = canvas
        image = image.convert("RGB")
        image.thumbnail((1600, 1600))
        if max(ImageStat.Stat(image.convert("L")).stddev) < 6:
            raise OSError("blank picture")
        out = BytesIO()
        image.save(out, "JPEG", quality=90)
    except OSError:
        raise
    except (Image.DecompressionBombError, ValueError, SyntaxError, struct.error, EOFError) as exc:
        raise OSError(f"unreadable picture: {exc}") from exc
    out.seek(0)
    return out


def check_account(session, tg, cfg, state, account):
    account_state = state.setdefault("accounts", {}).setdefault(account, {})
    try:
        posts, host = fetch_feed(session, account, cfg["feed_count"], account_state.get("host"))
    except RuntimeError as exc:
        account_state["consecutive_failures"] = account_state.get("consecutive_failures", 0) + 1
        account_state["last_error"] = str(exc)
        log.warning("@%s feed failed (%s): %s", account, account_state["consecutive_failures"], exc)
        save_state(state)
        return
    if account_state.get("consecutive_failures"):
        log.info("@%s feed recovered on %s", account, host)
    account_state["consecutive_failures"] = 0
    account_state.pop("last_error", None)
    account_state["host"] = host
    account_state["last_success_at"] = datetime.now(timezone.utc).isoformat()
    posts = [post for post in posts if post["author"].lower() == account.lower()]
    if not posts:
        log.warning("@%s feed had no posts by that account", account)
        save_state(state)
        return

    if not account_state.get("initialized"):
        account_state["seen_ids"] = [p["id"] for p in posts]
        account_state["watermark"] = max(p["timestamp"] for p in posts)
        account_state["initialized"] = True
        save_state(state)
        log.info("@%s baseline saved: %s posts via %s", account, len(posts), host)
        return

    seen = set(account_state.get("seen_ids", []))
    watermark = account_state.get("watermark", 0)
    topic_id = state["topics"][account]
    new_posts = sorted(
        (p for p in posts if p["id"] not in seen and p["timestamp"] >= watermark),
        key=lambda p: (p["timestamp"], int(p["id"])),
    )
    for post in new_posts:
        reason = skip_reason(post["text"]) or filter_reason(cfg.get("filters_for", {}).get(account, []), post["text"])
        if reason:
            log.info("@%s skipped (%s) %s", account, reason, post["url"])
        else:
            if session is not None:
                # Picture lookup must never block delivery: on any failure, send it without one.
                topic = headline(post["text"], "", False)  # the post's own subject, even on fixed-label accounts
                topic = topic or cfg["label_for"].get(account, "")
                try:
                    post["media"] = fetch_media(session, account, post["id"], post["text"], topic)
                except Exception:
                    log.exception("@%s picture lookup failed; sending without one", account)
                    post["media"] = []
            tg.post(topic_id, account, post)
            log.info("@%s posted %s", account, post["url"])
        seen.add(post["id"])
        account_state["seen_ids"] = (account_state.get("seen_ids", []) + [post["id"]])[-500:]
        account_state["watermark"] = max(account_state.get("watermark", 0), post["timestamp"])
        save_state(state)
    save_state(state)


def single_instance():
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    port = 47833 + zlib.crc32(CONFIG_PATH.name.encode()) % 1000
    try:
        lock.bind(("127.0.0.1", port))
    except OSError:
        raise RuntimeError("News watcher is already running") from None
    return lock


def main():
    global CONFIG_PATH, STATE_PATH, LOG_PATH
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.json", help="Config filename in this folder")
    parser.add_argument("--probe", action="store_true", help="Check free feeds without Telegram")
    parser.add_argument("--chats", action="store_true", help="List chats visible to the bot")
    parser.add_argument("--once", action="store_true", help="Check all accounts once")
    args = parser.parse_args()
    if Path(args.config).name != args.config or not args.config.lower().endswith(".json"):
        raise ValueError("--config must be a JSON filename in this folder")
    CONFIG_PATH = HERE / args.config
    if args.config == "config.json":
        STATE_PATH = HERE / "state.json"
        LOG_PATH = HERE / "news-notifier.log"
    else:
        STATE_PATH = HERE / f"{CONFIG_PATH.stem}.state.json"
        LOG_PATH = HERE / f"{CONFIG_PATH.stem}.log"
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8"), logging.StreamHandler()])
    cfg = load_config()
    if args.probe:
        session = requests.Session()
        failed = 0
        for account in cfg["accounts"]:
            try:
                posts, host = fetch_feed(session, account, cfg["feed_count"])
                newest = max(p["timestamp"] for p in posts)
                when = datetime.fromtimestamp(newest, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                print(f"@{account}: OK ({len(posts)} posts, newest {when}, {host})")
            except RuntimeError as exc:
                failed += 1
                print(f"@{account}: FAILED ({exc})")
        if failed:
            raise SystemExit(1)
        return

    token, old_chat = load_token_and_old_chat()
    tg = Telegram(token)
    if args.chats:
        chats = tg.chats()
        for chat_id, title in chats.items():
            print(f"{chat_id}: {title}")
        if not chats:
            print("No chats visible. Add the bot to the new group and send it /start there.")
        return

    chat_id = str(cfg.get("telegram_chat_id", "")).strip()
    if not chat_id:
        raise SystemExit("Create the news group, run --chats, then set telegram_chat_id in config.json")
    if old_chat and chat_id == old_chat:
        raise SystemExit("The configured chat is the existing TikTok group; set the new news group's ID")
    tg.chat_id = chat_id
    tg.emoji = cfg["emoji_for"]
    tg.labels = cfg["label_for"]
    tg.fixed = cfg["fixed_label"]
    lock = single_instance()
    state = load_state()
    if state.get("chat_id") != chat_id:
        state["chat_id"] = chat_id
        state["topics"] = {}
        save_state(state)
    # Accounts removed from the config: forget them, so re-adding one later gets a fresh topic.
    for account in [a for a in state.get("topics", {}) if a not in cfg["accounts"]]:
        state["topics"].pop(account)
        state.get("accounts", {}).pop(account, None)
        save_state(state)
        log.info("Forgot removed account @%s", account)
    for account in cfg["accounts"]:
        if account not in state.setdefault("topics", {}):
            state["topics"][account] = tg.create_topic(account)
            save_state(state)
            log.info("Created topic @%s", account)

    session = requests.Session()
    if args.once:
        for account in cfg["accounts"]:
            try:
                check_account(session, tg, cfg, state, account)
            except Exception:
                log.exception("@%s check failed", account)
        return

    interval = cfg["poll_interval_seconds"]
    step = min(10, interval / len(cfg["accounts"]))
    due = {account: time.monotonic() + i * step for i, account in enumerate(cfg["accounts"])}
    log.info("Watching %s accounts every %s seconds", len(due), interval)
    while True:
        account = min(due, key=due.get)
        time.sleep(max(0, due[account] - time.monotonic()))
        try:
            check_account(session, tg, cfg, state, account)
        except Exception:  # one bad post or outage must never stop the watcher
            log.exception("@%s check failed; will retry next round", account)
        due[account] = time.monotonic() + interval


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, requests.RequestException) as exc:
        raise SystemExit(str(exc)) from None
