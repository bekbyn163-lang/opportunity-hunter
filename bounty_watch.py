"""Bounty WATCH — find genuinely winnable coding bounties, ignore the traps.

Why this exists
---------------
The public bounty feed (June 2026) is flooded with AI-bait honeypots: repos
literally named `agent-playground`, `bug-bounty`, `oss-hunter-livefire`, tagged
"AI agent friendly", with joke tasks ("Calculate the exact value of PI, $1k").
A naive scanner walks straight into them and burns your GitHub reputation. The
honeypots also SPAM new issues, which buries real bounties below the newest feed.

So this watcher uses two passes:
  PASS A — curated: scan a hand-picked list of orgs that genuinely pay and merge
           (CURATED_REPOS). These are pre-trusted, so honeypot spam can't hide
           their bounties from us.
  PASS B — global: scan the newest bounty issues everywhere, and keep one only if
           it clears a legitimacy gate honeypots can't fake — real products have
           stars and history; traps don't.

A bounty is reported only if it is UNASSIGNED, not picked-over (<= MAX_COMMENTS),
not crypto/web3, and (pass B) lives in an established repo (>= MIN_STARS stars,
not a fork, not archived) whose language is TypeScript / JavaScript / Python.

Survivors are written to watch_hits.json (and watch.log), printed, and — if you
fill in watch_config.json — pushed to your phone over Telegram. Then you paste the
hit to Claude, Claude writes + tests the fix, you open the PR with /claim. Speed
wins the merge race, so the phone ping matters.

Zero dependencies (standard library only) and zero cost. Run by hand:

    python bounty_watch.py

…or unattended every 90 min via Windows Task Scheduler (see RUN_BOUNTY_WATCH.bat).

Recommended: set a GitHub token so repo look-ups don't hit the unauthenticated
rate limit — `set GITHUB_TOKEN=ghp_xxx` (a classic token with NO scopes is enough;
it's only used for read-only API calls).
"""
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HITS_FILE = ROOT / "watch_hits.json"
SEEN_FILE = ROOT / "watch_seen.json"
LOG_FILE = ROOT / "watch.log"
CONFIG_FILE = ROOT / "watch_config.json"
STATE_FILE = ROOT / "watch_state.json"

# --- what counts as winnable -------------------------------------------------
MIN_STARS = 200          # established project, not a seeded honeypot (pass B only)
MAX_COMMENTS = 12        # more than this = contested / picked over
MIN_REWARD = 30          # ignore $5 noise
MAX_REWARD = 1500        # keep the big ones too; you decide what to take
LANG_ALLOWLIST = {"typescript", "javascript", "python"}
SEARCH_LABEL = "💎 Bounty"   # the label Algora's bot adds to every bounty issue
PER_PAGE = 100
MAX_REPO_LOOKUPS = 30    # cap repo API calls per run to respect rate limits

# Orgs/repos that genuinely post bounties AND merge + pay them. Pre-trusted, so we
# don't run the stars gate on these. Edit freely — add any repo you've seen pay out.
CURATED_REPOS = [
    "calcom/cal.com", "activepieces/activepieces", "twentyhq/twenty",
    "documenso/documenso", "triggerdotdev/trigger.dev", "formbricks/formbricks",
    "novuhq/novu", "dittofeed/dittofeed", "teableio/teable", "nocodb/nocodb",
    "medusajs/medusa", "refinedev/refine", "appsmithorg/appsmith",
    "onyx-dot-app/onyx", "mendableai/firecrawl", "khoj-ai/khoj",
    "zauberzeug/nicegui", "reflex-dev/reflex",
]

# Repo names that are almost always AI-bait or test targets, not real products.
HONEYPOT_PATTERNS = (
    "playground", "test-target", "testtarget", "bug-bounty", "bugbounty",
    "oss-hunter", "osshunter", "livefire", "sandbox", "demo", "sample",
    "example", "foobar", "-hunter", "agent-", "bounties", "bounty-",
)
# Crypto / web3 — excluded on halal grounds and because they're hard to test.
CRYPTO_TERMS = (
    "solidity", "web3", "defi", "erc-20", "erc20", "erc-721", "erc721",
    "nft", "blockchain", "ethereum", "solana", "on-chain", "onchain",
    "smart contract", "smart-contract", " crypto", "tokenomics", "staking",
)

GITHUB_API = "https://api.github.com"
USER_AGENT = "bounty-watch/1.0 (personal use)"
REWARD_RE = re.compile(r"^\$?\s*([\d,]+(?:\.\d+)?)\s*(k?)\s*$", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# Tiny stdlib HTTP helper
# --------------------------------------------------------------------------- #
_TOKEN_CACHE = None


def github_token():
    """Read the GitHub token from env or watch_config.json (cached). Optional; lifts rate limits."""
    global _TOKEN_CACHE
    if _TOKEN_CACHE is None:
        env = os.getenv("GITHUB_TOKEN", "").strip()
        cfg = (load_json(CONFIG_FILE, {}).get("github_token") or "").strip()
        _TOKEN_CACHE = env or cfg
    return _TOKEN_CACHE


def gh_get(path_or_url, params=None):
    """GET a GitHub API endpoint and return parsed JSON (or None on failure)."""
    url = path_or_url if path_or_url.startswith("http") else f"{GITHUB_API}{path_or_url}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", USER_AGENT)
    token = github_token()
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 403:
            log("GitHub rate limit hit (403). Set GITHUB_TOKEN to raise it, or run less often.")
        else:
            log(f"GitHub HTTP {e.code} for {url}")
    except Exception as e:  # noqa: BLE001 - network errors shouldn't crash the watch
        log(f"GitHub request failed for {url}: {e}")
    return None


def search_issues(extra_qualifiers="", sort="created"):
    """Search open bounty issues. extra_qualifiers e.g. 'repo:a repo:b'; sort 'created'|'updated'."""
    q = f'label:"{SEARCH_LABEL}" state:open {extra_qualifiers}'.strip()
    result = gh_get("/search/issues", {"q": q, "sort": sort,
                                       "order": "desc", "per_page": PER_PAGE})
    return result.get("items", []) if result else []


# --------------------------------------------------------------------------- #
# Filters
# --------------------------------------------------------------------------- #
def parse_reward(labels):
    """Find a dollar amount in the issue's labels (e.g. '$100', '$1.5k'). None if absent."""
    for name in labels:
        m = REWARD_RE.match(name.strip())
        if m:
            amount = float(m.group(1).replace(",", ""))
            if m.group(2).lower() == "k":
                amount *= 1000
            return amount
    return None


def looks_like_honeypot(full_name):
    low = full_name.lower()
    return any(p in low for p in HONEYPOT_PATTERNS)


def has_crypto_terms(*texts):
    blob = " ".join(t.lower() for t in texts if t)
    return any(term in blob for term in CRYPTO_TERMS)


def repo_is_legit(repo):
    """repo = GitHub repo JSON. True only for an established, runnable, halal project."""
    if not repo:
        return False, "repo lookup failed"
    if repo.get("fork"):
        return False, "is a fork"
    if repo.get("archived"):
        return False, "archived"
    stars = repo.get("stargazers_count", 0)
    if stars < MIN_STARS:
        return False, f"only {stars} stars"
    lang = (repo.get("language") or "").lower()
    if lang not in LANG_ALLOWLIST:
        return False, f"language {lang or 'unknown'} not in allowlist"
    topics = " ".join(repo.get("topics", []) or [])
    if has_crypto_terms(repo.get("full_name", ""), repo.get("description") or "", topics):
        return False, "crypto/web3 project"
    return True, f"{stars}★ {lang}"


# --------------------------------------------------------------------------- #
# State + notifications
# --------------------------------------------------------------------------- #
def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def log(msg):
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  {msg}"
    print(line)
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def telegram_send(text):
    """Send a Telegram message if configured. Returns True on success, False otherwise."""
    cfg = load_json(CONFIG_FILE, {})
    token = (cfg.get("telegram_bot_token") or "").strip()
    chat_id = (cfg.get("telegram_chat_id") or "").strip()
    if not (token and chat_id):
        return False  # phone pings are optional; silently skip if not configured
    try:
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
        urllib.request.urlopen(req, timeout=20)
        return True
    except Exception as e:  # noqa: BLE001
        log(f"Telegram send failed: {e}")
        return False


def telegram_notify(hits):
    head = "🎯 New winnable bounty:" if len(hits) == 1 else f"🎯 {len(hits)} new winnable bounties:"
    lines = [head]
    for h in hits:
        amount = f"${h['reward']:.0f}" if h["reward"] else "$?"
        lines.append(f"• {amount} — {h['repo']} ({h['meta']})\n  {h['title']}\n  {h['url']}")
    if telegram_send("\n".join(lines)):
        log("Telegram ping sent.")


def maybe_heartbeat(scanned_summary):
    """Once per day, send a proof-of-life ping so you know the watcher is alive."""
    cfg = load_json(CONFIG_FILE, {})
    if not cfg.get("heartbeat", True):
        return
    today = datetime.now().strftime("%Y-%m-%d")
    state = load_json(STATE_FILE, {})
    if state.get("last_heartbeat") == today:
        return
    total = len(load_json(HITS_FILE, []))
    msg = (f"Still hunting. {scanned_summary}, 0 new winnable right now "
           f"({total} saved so far). I'll ping you the second a real one lands.")
    if telegram_send(msg):
        state["last_heartbeat"] = today
        STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
        log("daily heartbeat sent.")


# --------------------------------------------------------------------------- #
# Processing one search result
# --------------------------------------------------------------------------- #
def process_item(it, seen, trusted, budget):
    """Return (hit|None, used_lookup_bool). Marks url seen on any decision.

    trusted=True (curated repos): skip the stars/honeypot gate and the repo lookup.
    trusted=False (global feed): run the full legitimacy gate (costs one repo lookup).
    """
    url = it.get("html_url", "")
    if not url or url in seen or it.get("pull_request"):
        return None, False
    repo_url = it.get("repository_url", "")
    full_name = "/".join(repo_url.rstrip("/").split("/")[-2:]) if repo_url else ""
    if not full_name:
        return None, False

    # cheap rejects (no extra API calls)
    if not trusted and looks_like_honeypot(full_name):
        seen.add(url); return None, False
    if it.get("assignee") or it.get("assignees"):
        seen.add(url); return None, False
    if it.get("comments", 0) > MAX_COMMENTS:
        seen.add(url); return None, False
    label_names = [l.get("name", "") for l in it.get("labels", [])]
    if has_crypto_terms(it.get("title", ""), " ".join(label_names)):
        seen.add(url); return None, False
    reward = parse_reward(label_names)
    if reward is not None and not (MIN_REWARD <= reward <= MAX_REWARD):
        seen.add(url); return None, False

    used_lookup = False
    if trusted:
        why = "curated org"
    else:
        if budget <= 0:
            return None, False  # out of API budget; leave unseen for next run
        repo = gh_get(f"/repos/{full_name}")
        time.sleep(0.5)  # be polite to the API
        used_lookup = True
        legit, why = repo_is_legit(repo)
        if not legit:
            seen.add(url); return None, True

    hit = {
        "repo": full_name,
        "title": it.get("title", ""),
        "url": url,
        "reward": reward,
        "comments": it.get("comments", 0),
        "created_at": it.get("created_at", ""),
        "meta": why,
        "found_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    seen.add(url)
    amount = f"${reward:.0f}" if reward else "$?"
    log(f"  ✅ WINNABLE  {amount}  {full_name}  ({why})  — {hit['title']}")
    return hit, used_lookup


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    log("=== bounty watch run ===")
    seen = set(load_json(SEEN_FILE, []))
    new_hits, budget = [], MAX_REPO_LOOKUPS

    # PASS A — curated, trusted orgs (chunked so the query stays short).
    for i in range(0, len(CURATED_REPOS), 8):
        chunk = CURATED_REPOS[i:i + 8]
        quals = " ".join(f"repo:{r}" for r in chunk)
        for it in search_issues(quals):
            hit, _ = process_item(it, seen, trusted=True, budget=budget)
            if hit:
                new_hits.append(hit)

    # PASS B — global: scan both the NEWEST and the most recently ACTIVE, so
    # honeypot spam (which floods "newest") can't bury real bounties below it.
    seen_ids, items = set(), []
    for sort_mode in ("created", "updated"):
        for it in search_issues(sort=sort_mode):
            iid = it.get("id")
            if iid not in seen_ids:
                seen_ids.add(iid)
                items.append(it)
    log(f"scanned {len(items)} global bounty issues + {len(CURATED_REPOS)} curated repos")
    for it in items:
        hit, used = process_item(it, seen, trusted=False, budget=budget)
        if used:
            budget -= 1
        if hit:
            new_hits.append(hit)

    # de-dupe hits by url (a curated repo could also appear in the global feed)
    uniq, urls = [], set()
    for h in new_hits:
        if h["url"] not in urls:
            uniq.append(h); urls.add(h["url"])
    new_hits = uniq

    SEEN_FILE.write_text(json.dumps(sorted(seen), indent=0), encoding="utf-8")
    if new_hits:
        existing = load_json(HITS_FILE, [])
        merged = new_hits + existing
        HITS_FILE.write_text(json.dumps(merged[:100], indent=2, ensure_ascii=False),
                             encoding="utf-8")
        telegram_notify(new_hits)
        log(f"added {len(new_hits)} new winnable hit(s) -> watch_hits.json")
    else:
        log("no new winnable bounties this run (all current ones are traps/taken/too-big)")
        maybe_heartbeat(f"scanned {len(items)} newest + {len(CURATED_REPOS)} curated orgs")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
