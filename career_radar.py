#!/usr/bin/env python3
"""Career Radar — daily Reddit career intel, synthesized by an LLM.

Pulls top posts and comments from career-related subreddits (well within
Reddit's free API tier), grounds them against a locally-stored CV / personal
website (never committed to the repo), and has an OpenRouter-hosted model
(DeepSeek V4 Flash by default) rewrite a living markdown document of
actionable insights.

Run daily via cron or a systemd timer. See README.md.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys
import time
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
API_BASE = "https://oauth.reddit.com"
SEEN_PATH = ROOT / "data" / "seen.json"

SEEN_CAP = 3000            # remember this many post ids before dropping oldest
PROFILE_CHAR_CAP = 24_000  # cap on CV + website text sent to the LLM
DIGEST_CHAR_CAP = 100_000  # cap on the reddit digest sent to the LLM
POST_TEXT_CAP = 1500       # chars of selftext kept per post
COMMENT_TEXT_CAP = 800     # chars kept per comment

SYSTEM_PROMPT = """\
You are a sharp, practical career advisor. You maintain a living markdown
document of career insights for ONE specific person. Everything you write
must be actionable and tailored to their background, seniority, and field —
generic advice is noise. Their professional profile:

<profile>
{profile}
</profile>
"""

USER_PROMPT = """\
Below is the current insights document, then today's digest of new posts from
career-related subreddits.

Rewrite the ENTIRE insights document. Rules:
- Fold relevant new findings into existing sections (job-search trends,
  CV/resume advice, interview strategies, negotiation/market signals).
  Merge and update — do not just append. Remove items that went stale.
- Only include digest items relevant to this person's field and level;
  silently drop the rest.
- Make advice concrete: what to change in the CV, what to practice, what to
  watch in the market — not platitudes.
- Keep a "## Log" section at the bottom; add one dated line ({today})
  summarizing what changed today.
- Keep the whole document under ~400 lines.
- Output ONLY the markdown document. No preamble, no code fences around it.

<current_document>
{current}
</current_document>

<todays_digest>
{digest}
</todays_digest>
"""

INSIGHTS_TEMPLATE = """\
# Career Insights

_Maintained automatically by career-radar._

## Log
"""


# ---------- config / env ----------

def load_env(path: Path) -> None:
    """Minimal .env loader; real environment always wins."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def load_config() -> dict:
    for name in ("config.toml", "config.example.toml"):
        path = ROOT / name
        if path.exists():
            return tomllib.loads(path.read_text())
    sys.exit("no config.toml or config.example.toml found")


def require_env(*keys: str) -> None:
    missing = [k for k in keys if not os.environ.get(k)]
    if missing:
        sys.exit(f"missing required env vars: {', '.join(missing)} (see .env.example)")


# ---------- reddit ----------

def reddit_session(user_agent: str):
    import requests  # ponytail: lazy import keeps pure helpers testable without deps

    resp = requests.post(
        TOKEN_URL,
        auth=(os.environ["REDDIT_CLIENT_ID"], os.environ["REDDIT_CLIENT_SECRET"]),
        data={"grant_type": "client_credentials"},
        headers={"User-Agent": user_agent},
        timeout=30,
    )
    resp.raise_for_status()
    token = resp.json()["access_token"]
    session = requests.Session()
    session.headers.update({"Authorization": f"bearer {token}", "User-Agent": user_agent})
    return session


def get_json(session, path: str, delay: float, **params):
    # ponytail: fixed delay keeps us far below the free tier's 100 requests/min;
    # switch to X-Ratelimit-Remaining header parsing if you ever need more throughput
    time.sleep(delay)
    resp = session.get(f"{API_BASE}{path}", params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def fetch_posts(session, subreddit: str, cfg: dict) -> list[dict]:
    data = get_json(
        session, f"/r/{subreddit}/top", cfg["request_delay_seconds"],
        t=cfg["timeframe"], limit=cfg["posts_per_subreddit"],
    )
    posts = []
    for child in data["data"]["children"]:
        d = child["data"]
        posts.append({
            "id": d["id"],
            "subreddit": subreddit,
            "title": d.get("title", ""),
            "selftext": (d.get("selftext") or "")[:POST_TEXT_CAP],
            "score": d.get("score", 0),
            "num_comments": d.get("num_comments", 0),
        })
    return posts


def fetch_top_comments(session, post: dict, cfg: dict) -> list[str]:
    data = get_json(
        session, f"/r/{post['subreddit']}/comments/{post['id']}",
        cfg["request_delay_seconds"], limit=cfg["comments_per_post"] + 2,
        depth=1, sort="top",
    )
    comments = []
    for child in data[1]["data"]["children"]:
        body = child.get("data", {}).get("body")
        if body:
            comments.append(body[:COMMENT_TEXT_CAP])
        if len(comments) >= cfg["comments_per_post"]:
            break
    return comments


# ---------- pure helpers (tested) ----------

def filter_new_posts(posts: list[dict], seen: list[str]) -> list[dict]:
    seen_set = set(seen)
    return [p for p in posts if p["id"] not in seen_set]


def build_digest(posts: list[dict]) -> str:
    lines = []
    for p in posts:
        lines.append(f"### [r/{p['subreddit']}] {p['title']} "
                     f"({p['score']} pts, {p['num_comments']} comments)")
        if p.get("selftext"):
            lines.append(p["selftext"])
        for c in p.get("comments", []):
            lines.append(f"> top comment: {c}")
        lines.append("")
    return "\n".join(lines)[:DIGEST_CHAR_CAP]


def update_seen(seen: list[str], new_posts: list[dict]) -> list[str]:
    return (seen + [p["id"] for p in new_posts])[-SEEN_CAP:]


# ---------- profile grounding ----------

def extract_text(path: Path) -> str:
    if path.suffix.lower() == ".pdf":
        try:
            return subprocess.run(
                ["pdftotext", str(path), "-"],
                capture_output=True, text=True, check=True,
            ).stdout
        except (FileNotFoundError, subprocess.CalledProcessError):
            print(f"warning: could not extract {path} (install poppler-utils, "
                  "or point CV_PATH at a .md/.txt export)", file=sys.stderr)
            return ""
    return path.read_text(errors="ignore")


def gather_profile() -> str:
    parts = []
    cv = os.environ.get("CV_PATH", "")
    if cv and Path(cv).exists():
        parts.append("## CV\n" + extract_text(Path(cv)))
    site = os.environ.get("WEBSITE_DIR", "")
    if site and Path(site).is_dir():
        for f in sorted(Path(site).rglob("*")):
            if f.is_file() and f.suffix.lower() in {".md", ".txt", ".html"}:
                parts.append(f"## website: {f.name}\n" + f.read_text(errors="ignore"))
    profile = "\n\n".join(parts).strip()
    if not profile:
        print("warning: no profile found (CV_PATH / WEBSITE_DIR unset or empty); "
              "insights will be generic", file=sys.stderr)
        profile = "(no profile provided)"
    return profile[:PROFILE_CHAR_CAP]


# ---------- synthesis ----------

def synthesize(cfg: dict, profile: str, digest: str, current: str) -> str:
    import requests  # lazy import, see reddit_session

    today = datetime.date.today().isoformat()
    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}"},
        json={
            "model": cfg["model"],
            "max_tokens": cfg["max_output_tokens"],
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT.format(profile=profile)},
                {"role": "user",
                 "content": USER_PROMPT.format(current=current, digest=digest, today=today)},
            ],
        },
        timeout=300,
    )
    resp.raise_for_status()
    text = resp.json()["choices"][0]["message"]["content"].strip()
    if len(text) < 100:
        sys.exit(f"model returned suspiciously short output; not overwriting insights:\n{text}")
    return text + "\n"


# ---------- main ----------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="fetch and print the digest, skip the LLM call")
    args = parser.parse_args()

    load_env(ROOT / ".env")
    cfg = load_config()
    require_env("REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET")
    if not args.dry_run:
        require_env("OPENROUTER_API_KEY")

    seen = json.loads(SEEN_PATH.read_text()) if SEEN_PATH.exists() else []
    user_agent = os.environ.get("REDDIT_USER_AGENT", "career-radar/1.0")
    session = reddit_session(user_agent)

    new_posts: list[dict] = []
    for sub in cfg["subreddits"]:
        try:
            posts = filter_new_posts(fetch_posts(session, sub, cfg), seen)
        except Exception as e:  # one bad subreddit shouldn't kill the run
            print(f"warning: r/{sub} failed: {e}", file=sys.stderr)
            continue
        for post in posts[:cfg["top_posts_with_comments"]]:
            try:
                post["comments"] = fetch_top_comments(session, post, cfg)
            except Exception as e:
                print(f"warning: comments for {post['id']} failed: {e}", file=sys.stderr)
        new_posts.extend(posts)

    if not new_posts:
        print("no new posts today; nothing to do")
        return

    digest = build_digest(new_posts)
    print(f"collected {len(new_posts)} new posts from {len(cfg['subreddits'])} subreddits")

    if args.dry_run:
        print(digest)
        return

    insights_path = ROOT / cfg["insights_path"]
    current = insights_path.read_text() if insights_path.exists() else INSIGHTS_TEMPLATE
    updated = synthesize(cfg, gather_profile(), digest, current)

    insights_path.parent.mkdir(parents=True, exist_ok=True)
    insights_path.write_text(updated)
    SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    SEEN_PATH.write_text(json.dumps(update_seen(seen, new_posts)))
    print(f"updated {insights_path}")


if __name__ == "__main__":
    main()
