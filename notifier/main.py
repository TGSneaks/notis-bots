"""
TikTok (+ optional Instagram) -> Telegram notifier.

Run:                       python main.py
Test one check:            python main.py --once
List your channels' IDs:   python main.py --chats
"""

import json
import logging
import os
import random
import sys
import time
from pathlib import Path

from telegram import Telegram, TopicGone

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
STATE_PATH = BASE_DIR / "state.json"

log = logging.getLogger("notifier")


def load_config():
    if not CONFIG_PATH.exists():
        sys.exit("config.json not found. Copy config.example.json to config.json and fill it in.")
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    if not cfg.get("telegram_bot_token"):
        sys.exit("config.json is missing 'telegram_bot_token'.")
    return cfg


def parse_targets(pcfg, default_chat):
    """
    targets entries are either "username" (sent to telegram_chat_id) or
    {"username": "...", "chat_id": "@channel or -100...", "topic_id": 123}.
    Returns {username: (chat_id, topic_id)} and rewrites pcfg["targets"] to plain usernames.
    """
    routes = {}
    for t in pcfg.get("targets", []):
        if isinstance(t, str):
            t = {"username": t}
        name = t["username"].strip().lstrip("@")
        chat = t.get("chat_id") or default_chat
        if not chat:
            sys.exit(f"No chat_id for @{name} and no default telegram_chat_id in config.json.")
        routes[name] = (chat, t.get("topic_id"))
    pcfg["targets"] = list(routes)
    return routes


def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {}


def save_state(state):
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)


def build_platforms(cfg):
    """Returns {name: (platform, routes)}."""
    platforms = {}
    default_chat = cfg.get("telegram_chat_id")
    tt = cfg.get("tiktok", {})
    if tt.get("enabled", True) and tt.get("targets"):
        from tiktok import TikTok
        routes = parse_targets(tt, default_chat)
        platforms["tiktok"] = (TikTok(tt), routes)
    ig = cfg.get("instagram", {})
    if ig.get("enabled") and ig.get("targets"):
        from instagram import Instagram
        routes = parse_targets(ig, default_chat)
        platforms["instagram"] = (Instagram(ig, BASE_DIR), routes)
    if not platforms:
        sys.exit("Nothing to watch: add usernames to tiktok.targets (and/or enable instagram).")
    return platforms


def ensure_topics(cfg, platforms, tg, state):
    """With "auto_topics": true, give every username without a topic_id its own topic (created once, remembered)."""
    if not cfg.get("auto_topics"):
        return
    known = state.setdefault("_topics", {})
    for name, (p, routes) in platforms.items():
        for user, (chat, topic) in list(routes.items()):
            if topic:
                continue
            key = f"{chat}|{name}|{user}"
            if key in known and not tg.for_chat(chat).topic_exists(known[key]):
                log.warning("[%s] @%s's topic %s was deleted in Telegram - making a new one", name, user, known[key])
                del known[key]
            if key in known:
                routes[user] = (chat, known[key])
            else:
                new_topic(platforms, name, user, chat, tg, state)


def new_topic(platforms, name, user, chat, tg, state):
    """Creates a topic for one username, remembers it and routes the username to it."""
    title = f"{user} ({name})" if len(platforms) > 1 else user
    topic = tg.for_chat(chat).create_topic(title)
    state.setdefault("_topics", {})[f"{chat}|{name}|{user}"] = topic
    save_state(state)
    log.info("[%s] created topic '%s' in %s", name, title, chat)
    platforms[name][1][user] = (chat, topic)


def run_target(platforms, name, username, tg, state, notify_existing):
    """Checks one target. Returns True if it failed."""
    platform, routes = platforms[name]
    pstate = state.setdefault(name, {})
    try:
        try:
            platform.check(tg.for_chat(*routes[username]), username, pstate, notify_existing)
        except TopicGone:
            chat, topic = routes[username]
            if state.get("_topics", {}).get(f"{chat}|{name}|{username}") != topic:
                raise  # topic_id set by hand in config.json - leave it alone
            # Unsent posts stay unseen, so checking again sends them to the new topic
            log.warning("[%s] @%s's topic %s was deleted in Telegram - making a new one", name, username, topic)
            new_topic(platforms, name, username, chat, tg, state)
            platform.check(tg.for_chat(*routes[username]), username, pstate, notify_existing)
        return False
    except Exception as e:
        log.error("[%s] @%s check failed: %s", name, username, e, exc_info=log.isEnabledFor(logging.DEBUG))
        return True
    finally:
        save_state(state)


def print_chats(tg):
    try:
        chats, topics = tg.list_chats()
    except RuntimeError as e:
        sys.exit(f"Telegram said: {e}\n(If it mentions a webhook, this bot is in use by another service, e.g. a "
                 "Cloudflare Worker; use a separate bot for the notifier or remove that webhook.)")
    if not chats:
        print("No chats seen yet. Add the bot as admin to each channel, post any message in each one, "
              "then run this again.")
        return
    print("Chats the bot can see (use the id as chat_id):")
    for cid, kind, title in chats:
        print(f"  {cid:>16}  {kind:<10} {title}")
        for (tcid, thread), name in sorted(topics.items()):
            if tcid == cid:
                print(f"  {'':>16}    topic_id {thread:<6} {name}")


def single_instance():
    """Holds a local port for the life of the process so a second copy exits instead of double-posting."""
    import socket

    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lock.bind(("127.0.0.1", 47831))
    except OSError:
        log.error("The notifier is already running - not starting a second copy.")
        sys.exit(0)
    return lock


def main():
    handlers = [logging.FileHandler(BASE_DIR / "notifier.log", encoding="utf-8")]
    if sys.stderr:  # pythonw (auto-start) has no console
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", handlers=handlers)
    cfg = load_config()
    tg = Telegram(cfg["telegram_bot_token"], cfg.get("telegram_chat_id"), cfg.get("send_as_files", False))

    if "--chats" in sys.argv:
        print_chats(tg)
        return

    _lock = single_instance()  # noqa: F841 - must stay referenced while running
    (BASE_DIR / "notifier.pid").write_text(str(os.getpid()))  # lets manager.pyw restart the bot
    notify_existing = cfg.get("notify_existing_on_first_run", False)
    platforms = build_platforms(cfg)
    if "tiktok" in platforms:
        platforms["tiktok"][0].notify_existing_stories = cfg.get("notify_existing_stories_on_first_run", False)
    state = load_state()
    ensure_topics(cfg, platforms, tg, state)

    for name, (p, routes) in platforms.items():
        for user, (chat, topic) in routes.items():
            log.info("[%s] watching @%s -> %s%s", name, user, chat, f" (topic {topic})" if topic else "")

    once = "--once" in sys.argv
    targets = [(name, user) for name, (p, _) in platforms.items() for user in p.targets]
    # Spread the first checks across one interval so TikTok doesn't see a burst of requests (429s)
    start = time.time()
    next_due = {}
    for name, (p, _) in platforms.items():
        users = [t for t in targets if t[0] == name]
        step = 0 if once else min(p.poll_interval / max(len(users), 1), 120)
        for i, target in enumerate(users):
            next_due[target] = start + i * step
    backoff = {target: 0 for target in targets}
    while True:
        now = time.time()
        checked = False
        for name, username in targets:
            target = (name, username)
            if now < next_due[target]:
                continue
            p = platforms[name][0]
            failed = run_target(platforms, name, username, tg, state, notify_existing)
            backoff[target] = min(backoff[target] * 2 or 300, 3600) if failed else 0
            wait = p.poll_interval + random.uniform(0, p.poll_interval * 0.2) + backoff[target]
            next_due[target] = time.time() + wait
            log.info("[%s] @%s next check in %d s", name, username, wait)
            checked = True
            time.sleep(random.uniform(3, 8))
        if once:
            break
        if not checked:
            time.sleep(10)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
