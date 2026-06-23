"""Reddit gig WATCH — find freelance gigs on r/forhire that Claude can deliver.

Same idea as bounty_watch.py, but for posted freelance work instead of coding
bounties. It scans r/forhire + r/slavelabour (people posting "[Hiring] / [TASK] —
I'll pay for X"), and keeps only gigs that are:
  • someone HIRING (not offering),
  • a task Claude can actually deliver (writing, code, research, data, etc.), and
  • not haram / unethical / impossible (no exams, gambling, adult, audio/video, crypto).

Reddit blocks its .json API for bots, but the .rss (Atom) feed is open, so we read
that. Survivors -> reddit_hits.json + a Telegram ping (shared watch_config.json).
Then YOU reply using the Gig Kit templates, relay the buyer's "yes", Claude does the
actual deliverable, you collect (PayPal). Zero dependencies, zero cost.

    python reddit_watch.py
"""
import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HITS_FILE = ROOT / "reddit_hits.json"
SEEN_FILE = ROOT / "reddit_seen.json"
LOG_FILE = ROOT / "reddit_watch.log"
CONFIG_FILE = ROOT / "watch_config.json"   # shared with the bounty hunter (Telegram creds)

SUBREDDITS = ["forhire"]  # slavelabour rate-limits the RSS hard; add back later if wanted
LISTING_LIMIT = 100
ATOM = "{http://www.w3.org/2005/Atom}"
# A browser-like User-Agent — Reddit 403s obvious bots, but serves the RSS to this.
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# Tasks Claude can actually deliver — a post must mention at least one of these.
DOABLE = (
    "writ", "article", "blog", "content", "copywrit", "ghostwrit", "script",
    "python", "javascript", "code", "coding", "automat", "bot", "scrap",
    "data entry", "data-entry", "excel", "spreadsheet", "google sheet",
    "research", "summar", "transcri", "proofread", "edit", "rewrite", "seo",
    "resume", "cv", "cover letter", "translat", "powerpoint", "presentation",
    "api", "notion", "newsletter", "product description", "landing page",
)
# Haram / unethical / impossible — matched against the title AND body.
HARAM = (
    "essay", "homework", "exam", "thesis", "dissertation", "assignment",
    "coursework", "casino", "gambl", "betting", "poker", "adult", "nsfw",
    "onlyfans", "escort", "porn", "smut", "crypto", "forex", "trading signal",
)
# Role / recurring / wrong-medium signals — matched against the TITLE ONLY.
# (Bodies mention these words incidentally; the title concisely signals the gig
#  type, e.g. "Marketing Manager" / "Daily TikTok poster" vs "Need a Python script".)
ROLE_SKIP = (
    "manager", "coordinator", "specialist", "recruiter", "assistant",
    "full-time", "full time", "part-time", "part time", "/hr", "per hour",
    "hourly", "salary", "long-term", "long term", "ongoing", "daily", "weekly",
    "moderator", "salesperson", "sales rep", "commission", "outreach", "ugc",
    "creator", "influencer", "tiktok", "instagram", "designer", "photographer",
    "videographer", "video edit", "voiceover", "logo", "illustrat", "animation",
    "photoshop", "on site", "onsite", "on-site", "in person", "in-person",
    "iphone", "android", "upload", "engineer", "developers", "phone",
)


def load_json(p, d):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return d


def log(msg):
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  {msg}"
    print(line)
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def telegram_send(text):
    cfg = load_json(CONFIG_FILE, {})
    token = (cfg.get("telegram_bot_token") or "").strip()
    chat = (cfg.get("telegram_chat_id") or "").strip()
    if not (token and chat):
        return False
    try:
        data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
        urllib.request.urlopen(req, timeout=20)
        return True
    except Exception as e:  # noqa: BLE001
        log(f"Telegram send failed: {e}")
        return False


def strip_html(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def fetch_listing(sub):
    """Read a subreddit's Atom RSS feed and return a list of post dicts."""
    time.sleep(5)  # be polite / avoid Reddit 429 rate-limiting between subreddits
    url = f"https://www.reddit.com/r/{sub}/new.rss?limit={LISTING_LIMIT}"
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        log(f"reddit HTTP {e.code} for r/{sub} (try again later)")
        return []
    except Exception as e:  # noqa: BLE001
        log(f"reddit fetch failed for r/{sub}: {e}")
        return []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        log(f"reddit RSS parse error for r/{sub}: {e}")
        return []
    posts = []
    for entry in root.findall(f"{ATOM}entry"):
        def txt(tag):
            el = entry.find(f"{ATOM}{tag}")
            return (el.text or "") if el is not None else ""
        link_el = entry.find(f"{ATOM}link")
        auth_el = entry.find(f"{ATOM}author/{ATOM}name")
        posts.append({
            "id": txt("id"),
            "title": txt("title"),
            "url": link_el.get("href") if link_el is not None else "",
            "author": (auth_el.text or "") if auth_el is not None else "",
            "created": txt("published"),
            "body": strip_html(txt("content")),
        })
    return posts


def is_hiring(title):
    t = title.lower()
    if "[for hire]" in t or "[offer]" in t:
        return False  # someone offering a service, not hiring
    return "[hiring]" in t or "[task]" in t


def main():
    log("=== reddit gig watch run ===")
    seen = set(load_json(SEEN_FILE, []))
    new_hits = []
    for sub in SUBREDDITS:
        posts = fetch_listing(sub)
        log(f"r/{sub}: pulled {len(posts)} recent posts")
        for p in posts:
            pid = p["id"]
            if not pid or pid in seen:
                continue
            if not is_hiring(p["title"]):
                seen.add(pid)
                continue
            title_l, body_l = p["title"].lower(), p["body"].lower()
            if any(h in title_l or h in body_l for h in HARAM):
                seen.add(pid)
                continue
            if any(rs in title_l for rs in ROLE_SKIP):
                seen.add(pid)
                continue
            if not any(d in title_l or d in body_l for d in DOABLE):
                seen.add(pid)
                continue
            hit = {
                "sub": sub,
                "title": p["title"][:200],
                "url": p["url"],
                "author": p["author"],
                "created": p["created"][:16],
                "snippet": p["body"][:400],
                "found_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            new_hits.append(hit)
            seen.add(pid)
            log(f"  GIG  r/{sub}  {hit['title']}")

    SEEN_FILE.write_text(json.dumps(sorted(seen)), encoding="utf-8")
    if new_hits:
        merged = new_hits + load_json(HITS_FILE, [])
        HITS_FILE.write_text(json.dumps(merged[:100], indent=2, ensure_ascii=False), encoding="utf-8")
        head = "💼 New gig" + ("s" if len(new_hits) > 1 else "") + " on Reddit:"
        lines = [head]
        for h in new_hits[:8]:
            lines.append(f"• r/{h['sub']}: {h['title']}\n  {h['url']}")
        telegram_send("\n".join(lines))
        log(f"added {len(new_hits)} new gig(s) -> reddit_hits.json")
    else:
        log("no new doable gigs this run")


if __name__ == "__main__":
    main()
