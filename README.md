# Notification bots on GitHub Actions

Runs the TikTok notifier and the X news watchers for free on GitHub Actions. Every 10 minutes, a run checks every account once, sends anything new to Telegram, and saves what it has seen.

- Configs (including the bot token) live in the encrypted `BOT_CONFIGS` secret, never in the repo.
- Saved state is kept in the Actions cache between runs. `STATE_SEED` is used only when no saved state exists, or when a run is started with **reseed**.

## Everyday use

- **Change accounts or settings:** edit the config files in `../notifier` and `../news-notifier` on your PC, then run `python upload_secrets.py`. The next run uses them.
- **Pause:** Actions tab → **Bots** → `...` → **Disable workflow**. Re-enable it the same way.
- **Check without sending:** Actions tab → **Bots** → **Run workflow** → mode `probe`.
- **Logs:** open any run in the Actions tab.

Don't run the bots on your PC at the same time, or every alert arrives twice.

## Limits

- GitHub runs schedules on a best-effort basis: runs can be 5–20 minutes late, and occasionally one is skipped.
- If no run happens for 7 days, the saved state expires and the next run falls back to `STATE_SEED`.
