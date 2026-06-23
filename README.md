# Opportunity Hunter (24/7)

Runs on GitHub Actions every ~20 minutes — no laptop needed. It auto-hunts:

- **`bounty_watch.py`** — winnable coding bounties (GitHub / Algora `💎 Bounty`), filtering out honeypots, taken, and crypto.
- **`reddit_watch.py`** — one-off freelance gigs on r/forhire that an AI can deliver.

When it finds a real one, it pings Telegram. State (what it has already seen) is saved back to the repo so it never double-pings.

## Setup
Add two repository secrets in **Settings → Secrets and variables → Actions**:

| Secret | Value |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | your Telegram bot token |
| `TELEGRAM_CHAT_ID` | your Telegram chat id |

The GitHub bounty search uses the built-in Actions token automatically — nothing to add.
