"""Helper for running the bots on GitHub Actions.

  python ci.py write-configs   BOT_CONFIGS secret (JSON: path -> file content) -> config files
  python ci.py seed            STATE_SEED secret (base64 zip) -> state/
  python ci.py unpack          state/ -> bot folders
  python ci.py merge-topics    after a restore: copy state files only STATE_SEED has, add its missing topic IDs
  python ci.py pack            bot folders -> state/
  python ci.py run             one check of every bot (TikTok + each news config)
  python ci.py probe           check feeds and TikTok without sending anything
"""

import base64
import io
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
NEWS = ROOT / "news-notifier"
TIKTOK = ROOT / "notifier"


def state_files():
    """Every state file the bots write, as paths relative to ROOT."""
    files = [TIKTOK / "state.json", NEWS / "state.json"]
    files += sorted(NEWS.glob("*.state.json"))
    return [f.relative_to(ROOT) for f in files]


def news_configs():
    return sorted(p.name for p in NEWS.glob("*.json") if not p.name.endswith(".state.json") and p.name != "state.json")


def write_configs():
    configs = json.loads(os.environ["BOT_CONFIGS"])
    for rel, content in configs.items():
        path = (ROOT / rel).resolve()
        if ROOT not in path.parents or path.suffix != ".json":
            raise SystemExit(f"Refusing to write {rel}")
        path.write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {len(configs)} config files: {', '.join(configs)}")


def seed():
    raw = base64.b64decode(os.environ.get("STATE_SEED", ""))
    if not raw:
        print("No STATE_SEED secret; bots start fresh and record existing posts without sending them.")
        return
    STATE_DIR.mkdir(exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        for name in z.namelist():
            target = (STATE_DIR / name).resolve()
            if STATE_DIR not in target.parents:
                raise SystemExit(f"Bad path in seed: {name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(z.read(name))
            print(f"Seeded {name}")


def merge_topics():
    """Runs when saved state was restored, so `seed` was skipped. Brings in what STATE_SEED has and saved state lacks:
    a state file missing from saved state (e.g. a bot just moved from the PC) is copied in whole; for files that
    already exist, only Telegram topic IDs (`_topics`) they don't have yet are added."""
    raw = base64.b64decode(os.environ.get("STATE_SEED", ""))
    if not raw:
        return
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        for name in z.namelist():
            path = (ROOT / name).resolve()
            if ROOT not in path.parents or path.suffix != ".json":
                raise SystemExit(f"Bad path in seed: {name}")
            data = z.read(name)
            if not path.exists():
                json.loads(data)  # refuse to copy a corrupt file
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                print(f"{name}: not in saved state, copied from STATE_SEED")
                continue
            seed_topics = json.loads(data).get("_topics")
            if not seed_topics:
                continue
            state = json.loads(path.read_text(encoding="utf-8"))
            topics = state.setdefault("_topics", {})
            added = {k: v for k, v in seed_topics.items() if k not in topics}
            if added:
                topics.update(added)
                path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"{name}: added topics {', '.join(added)}")


def unpack():
    if not STATE_DIR.exists():
        print("No saved state yet.")
        return
    for src in STATE_DIR.rglob("*.json"):
        dst = ROOT / src.relative_to(STATE_DIR)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        print(f"Restored {dst.relative_to(ROOT)}")


def pack():
    for rel in state_files():
        if (ROOT / rel).exists():
            dst = STATE_DIR / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / rel, dst)
    print(f"Saved {len(list(STATE_DIR.rglob('*.json')))} state files")


def run():
    jobs = []
    if (TIKTOK / "config.json").exists():
        jobs.append(("tiktok", subprocess.Popen([sys.executable, "main.py", "--once"], cwd=TIKTOK)))
    for cfg in news_configs():
        jobs.append((f"news {cfg}", subprocess.Popen([sys.executable, "main.py", "--config", cfg, "--once"], cwd=NEWS)))
    failed = [name for name, p in jobs if p.wait() != 0]
    if failed:
        raise SystemExit(f"Failed: {', '.join(failed)}")
    print(f"All {len(jobs)} bots finished.")


def probe():
    failed = []
    for cfg in news_configs():
        print(f"\n== news {cfg}")
        if subprocess.run([sys.executable, "main.py", "--config", cfg, "--probe"], cwd=NEWS).returncode:
            failed.append(cfg)
    print("\n== tiktok")
    sys.path.insert(0, str(TIKTOK))
    from tiktok import TikTok  # noqa: E402

    cfg = json.loads((TIKTOK / "config.json").read_text(encoding="utf-8"))["tiktok"]
    targets = [t if isinstance(t, str) else t["username"] for t in cfg.get("targets", [])]
    tt = TikTok({"targets": targets})
    for user in tt.targets:
        try:
            prof = tt.profile(user)
            posts = tt.latest_posts(user)
            print(f"@{user}: OK (private={prof['private']}, story={prof['story']}, {len(posts)} posts)")
        except Exception as e:
            failed.append(user)
            print(f"@{user}: FAILED ({type(e).__name__}: {e})")
    print("\nffmpeg:", shutil.which("ffmpeg") or "missing")
    if failed:
        raise SystemExit(f"Failed: {', '.join(failed)}")


if __name__ == "__main__":
    commands = {"write-configs": write_configs, "seed": seed, "merge-topics": merge_topics, "unpack": unpack, "pack": pack, "run": run, "probe": probe}
    if len(sys.argv) != 2 or sys.argv[1] not in commands:
        raise SystemExit(__doc__)
    commands[sys.argv[1]]()
