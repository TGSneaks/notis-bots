"""Upload your local bot configs (and optionally saved state) to this repo's GitHub secrets.

Run on your PC from the cloud-bots folder after changing any config:
    python upload_secrets.py --tiktok-dir PATH             # TikTok config only
    python upload_secrets.py --tiktok-dir PATH --news      # TikTok + news configs
    add --state to also upload current saved progress (use when switching a bot over from the PC)

PATH is the folder with the TikTok bot's config.json (default ../notifier). News configs come from ../news-notifier.
Nothing is printed or written to the repo; values go straight to `gh secret set`.
"""

import argparse
import base64
import io
import json
import subprocess
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOCAL = HERE.parent
NEWS_CONFIGS = ["config.json", "creator-sports.json", "tech-ai.json", "anime.json", "online-news.json"]


def set_secret(name, value):
    subprocess.run(["gh", "secret", "set", name], input=value.encode(), cwd=HERE, check=True)
    print(f"Set {name} ({len(value)} chars)")


def configs(tiktok_dir, news):
    out = {"notifier/config.json": json.loads((tiktok_dir / "config.json").read_text(encoding="utf-8"))}
    for name in NEWS_CONFIGS if news else []:
        path = LOCAL / "news-notifier" / name
        if path.exists():
            cfg = json.loads(path.read_text(encoding="utf-8"))
            if not cfg.get("telegram_chat_id"):
                print(f"Skipped {name}: no telegram_chat_id yet")  # the cloud run would fail on it
                continue
            out[f"news-notifier/{name}"] = cfg
    return out


def state(tiktok_dir, news, fresh_tiktok):
    files = {"notifier/state.json": tiktok_dir / "state.json"}
    for path in (LOCAL / "news-notifier").glob("*state.json") if news else []:
        files[f"news-notifier/{path.name}"] = path
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for rel, path in files.items():
            if not path.exists():
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            if fresh_tiktok and rel == "notifier/state.json":
                # Keep topic IDs, forget seen posts: the first cloud check records what's there without sending.
                data = {"_topics": data.get("_topics", {})}
            z.writestr(rel, json.dumps(data, separators=(",", ":")))
            print(f"Packed {rel}")
    return base64.b64encode(buf.getvalue()).decode()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tiktok-dir", type=Path, default=LOCAL / "notifier", help="folder with the TikTok config.json")
    ap.add_argument("--news", action="store_true", help="include the news watchers (stop them on the PC first)")
    ap.add_argument("--state", action="store_true", help="also upload current state as STATE_SEED")
    ap.add_argument("--fresh-tiktok", action="store_true", help="with --state: drop TikTok seen posts, keep topics")
    args = ap.parse_args()
    set_secret("BOT_CONFIGS", json.dumps(configs(args.tiktok_dir, args.news), ensure_ascii=False, separators=(",", ":")))
    if args.state:
        seed = state(args.tiktok_dir, args.news, args.fresh_tiktok)
        if len(seed) > 48_000:
            raise SystemExit("State is too big for one GitHub secret (48 KB)")
        set_secret("STATE_SEED", seed)
