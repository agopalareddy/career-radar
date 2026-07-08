#!/usr/bin/env python3
"""Career Radar — daily Reddit career intel, synthesized by an LLM.

Pulls top posts and comments from career-related subreddits (well within
Reddit's free API tier), grounds them against a locally-stored CV / personal
website (never committed to the repo), and has an OpenRouter-hosted model
(DeepSeek V4 Flash by default) rewrite a living markdown document of
actionable insights.

Run daily via cron or a systemd timer. See README.md.
"""

import argparse
import base64
import datetime
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

import tomllib

ROOT = Path(__file__).resolve().parent
SEEN_PATH = ROOT / "data" / "seen.json"
ERR_LOG = ROOT / "data" / "errors.log"

SEEN_CAP = 3000  # remember this many post ids before dropping oldest
PROFILE_CHAR_CAP = 24_000  # cap on CV + website text sent to the LLM
DIGEST_CHAR_CAP = 100_000  # cap on the reddit digest sent to the LLM
POST_TEXT_CAP = 1500  # chars of selftext kept per post
COMMENT_TEXT_CAP = 800  # chars kept per comment

SYSTEM_PROMPT = """\
You are an expert career coach and technical recruiter. You maintain a living
markdown document of career insights for ONE specific person. Your job is to
read their profile, then read today's digest of career-related posts from
Reddit/HN/Dev.to, and produce a SHARP, ACTIONABLE, PERSONALIZED document.

Rules that govern every response:
- Everything you write must reference specifics from their profile — past
  roles, technologies they know, their degree, their seniority. Generic
  advice ("network more", "tailor your resume") is banned. Replace it with
  "Your Crittero internship taught you recommendation systems; emphasize that
  when applying to personalization teams at Spotify or Netflix."
- You are seeing raw, unfiltered posts from job seekers. Extract the
  PATTERNS behind the anecdotes: what are hiring managers actually looking
  for, what ATS systems are filtering on, what interview formats are
  changing. The individual posts are just data points — your job is to find
  the signal.
- Write at length. 300-600 lines. Each section should have 4-8 substantive
  bullet points with explanations.
- Use their name. Address them directly.
- The document should feel like a senior mentor wrote it after spending an
  hour studying their career and reading today's job market news.

FORMAT RULES — follow this section structure EXACTLY on every run:
- The document begins with a title: "# Career Insights for [name]"
- Below the title, the subtitle: "_Maintained automatically by career-radar._"
- Then these sections, in this order, with these exact headings:
  ## Employment Market Reality
  ## Grad School Reality
  ## Resume Fit & Advice
  ## CV Fit & Advice
  ## Job Search Strategy & Tactics
  ## Interview Preparation
  ## Target Companies & Roles
  ## Networking & Outreach
  ## Negotiation & Market Signals
  ## Mental Health & Resilience
  ## Log
- ALL 11 sections MUST appear. Dropping a section is more harmful than
  shortening its content. If you are running out of output space, reduce
  each section to 2-3 tightly focused bullet points — never omit a section
  entirely.
- NEVER add deadlines or schedules. Do not say "by Monday", "this week",
  "by Friday", "within 30 days", or assign dates. Describe actions without
  timelines. Write "Update your resume's impact metrics" not "Update your
  resume's impact metrics by Friday."
- NEVER add an "Action Items" section, checklist, numbered task list, or
  calendar. The sections above ARE the action items.
- The Log section MUST have one new dated line per run, formatted as:
  "- YYYY-MM-DD: N total posts (RR Reddit, HH Hacker News, DD Dev.to). [summary]"
  The source counts (RR, HH, DD) are provided in the digest
  under 'Source Summary' at the end of the digest.

Their professional profile (including their industry Resume and academic CV):

<profile>
{profile}
</profile>

CRITICAL DISTINCTION — their documents are labeled:
- **Resume (Industry)** = for industry job applications (tech companies,
  startups, corporate roles). One page, focused on work experience and
  impact.
- **CV (Academic)** = for academic/research positions ONLY (PhD programs,
  postdocs, faculty roles, research grants). Multi-page, includes
  publications, teaching, service, honors.

Never suggest repurposing their academic CV for industry — they already
have a separate Resume for that. The "## Resume Fit & Advice" section
covers industry positioning. The "## CV Fit & Advice" section covers
academic/research positioning (PhD applications, conferences, publications,
research statements).
"""

USER_PROMPT = """\
Below is the **current insights document** (from the previous daily run), followed
by today's digest of new posts from career communities (Reddit, Hacker News,
Dev.to). Do NOT treat the current document as a blank template — it already
contains personalized advice from prior days. Your job is to evolve it.

The digest ends with per-source post counts in a '## Source Summary' section.
Use those numbers in the Log entry (format specified in system prompt).
Do NOT keep the Source Summary section in your output — it is metadata only.

Rewrite the ENTIRE insights document. Rules:
- Fold relevant new findings into existing sections (job-search trends,
  CV/resume advice, interview strategies, negotiation/market signals).
  Merge and update — do not just append. Remove items that went stale.
  Preserve any section that still applies; only rewrite what needs updating.
- Only include digest items relevant to this person's field and level;
  silently drop the rest.
- Make advice concrete: what to change in the CV, what to practice, what to
  watch in the market — not platitudes.
- Keep a "## Log" section at the bottom; add one dated line ({today})
  summarizing what changed today.
- Keep the whole document under ~600 lines.
- CRITICAL: All 11 sections must be present in the final output. A section
  with 2 short bullets is acceptable — a missing section is not.
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


# ---------- helpers ----------


def log_error(source: str, detail: str) -> None:
    ts = datetime.datetime.now().isoformat()
    try:
        with open(ERR_LOG, "a") as f:
            f.write(f"{ts} | {source} | {detail}\n")
    except OSError:
        pass


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


# ---------- reddit RSS (posts only, no auth needed) ----------

REDDIT_RSS_NS = "http://www.w3.org/2005/Atom"
REDDIT_AGENT = "career-radar/1.0 (RSS reader)"
REDDIT_RSS_CAP = 120  # max seconds to wait on rate-limit


def _html_to_text(html: str | None) -> str:
    """Strip HTML tags and common entities from Reddit RSS content."""
    if not html:
        return ""
    text = re.sub(r"<!--.*?-->", "", html, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"&(amp|lt|gt|quot|#\d+);", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    # Strip Reddit RSS footer: "submitted by /u/... [link] [comments]"
    text = re.sub(r"\s*submitted by\s+/u/\S+.*$", "", text)
    return text


def _reddit_rss_get(url: str, delay: float) -> tuple[int, str]:
    """GET with rate-limit handling. Returns (http_status, body)."""
    import requests

    # ponytail: simple retry loop with backoff; 1 req/60s is the observed limit
    waited = 0.0
    while True:
        resp = requests.get(url, headers={"User-Agent": REDDIT_AGENT}, timeout=30)
        if resp.status_code == 429:
            retry = resp.headers.get("x-ratelimit-reset", "60")
            try:
                wait = min(float(retry) + 2, REDDIT_RSS_CAP - waited)
            except ValueError:
                wait = 60
            if waited + wait >= REDDIT_RSS_CAP:
                return 429, ""
            time.sleep(wait)
            waited += wait
            continue
        if resp.status_code != 200:
            return resp.status_code, ""
        # success — respect delay for next call
        time.sleep(delay)
        return 200, resp.text


def fetch_reddit_rss(subreddit: str, cfg: dict) -> list[dict]:
    import xml.etree.ElementTree as ET

    url = f"https://www.reddit.com/r/{subreddit}/top.rss?t=day"
    status, body = _reddit_rss_get(url, cfg.get("reddit_rss_delay", 10))
    if status != 200 or not body:
        return []

    root = ET.fromstring(body)
    posts = []
    for entry in root.findall(f"{{{REDDIT_RSS_NS}}}entry"):
        eid = entry.find(f"{{{REDDIT_RSS_NS}}}id")
        title_el = entry.find(f"{{{REDDIT_RSS_NS}}}title")
        content_el = entry.find(f"{{{REDDIT_RSS_NS}}}content")
        post_id = eid.text.removeprefix("t3_") if eid is not None and eid.text else ""
        title = title_el.text if title_el is not None else ""
        raw_body = content_el.text if content_el is not None else ""
        # ponytail: extract comment count from Reddit RSS footer "[N comments]"
        nc_match = re.search(r"\[(\d+)\s+comments?\]", raw_body) if raw_body else None
        try:
            num_comments = int(nc_match.group(1)) if nc_match else 0
        except (ValueError, TypeError):
            num_comments = 0
        selftext = _html_to_text(raw_body)[:POST_TEXT_CAP]
        if post_id and title:
            posts.append(
                {
                    "id": f"reddit:{post_id}",
                    "source": f"r/{subreddit}",
                    "title": title,
                    "selftext": selftext,
                    "score": 0,
                    "num_comments": num_comments,
                    "comments": [],
                }
            )
        if len(posts) >= cfg.get("reddit_posts_per_sub", 10):
            break
    return posts


# ---------- HN Algolia ----------


def fetch_hn_posts(query: str, cfg: dict) -> list[dict]:
    import requests

    hits = cfg.get("hn_hits_per_query", 5)
    url = f"https://hn.algolia.com/api/v1/search_by_date?query={urllib.parse.quote(query)}&tags=story&hitsPerPage={hits}"
    resp = requests.get(url, headers={"User-Agent": REDDIT_AGENT}, timeout=15)
    if resp.status_code != 200:
        return []
    posts = []
    for h in resp.json().get("hits", []):
        posts.append(
            {
                "id": f"hn:{h['objectID']}",
                "source": "hn",
                "title": h.get("title", ""),
                "selftext": "",
                "score": h.get("points", 0),
                "num_comments": h.get("num_comments", 0),
                "comments": [],
                "_objectID": h["objectID"],  # transient, stripped in build_digest
            }
        )
    return posts


def fetch_hn_comments(post: dict, cfg: dict) -> list[str]:
    import requests

    oid = post.get("_objectID")
    if not oid:
        return []
    maxc = cfg.get("hn_comments_per_post", 3)
    resp = requests.get(f"https://hn.algolia.com/api/v1/items/{oid}", timeout=15)
    if resp.status_code != 200:
        return []

    def _walk(node, depth=0):
        if depth > 1:
            return
        text = (node.get("text") or "").strip()
        if text and depth == 0:
            comments.append(text[:COMMENT_TEXT_CAP])
        for child in node.get("children", []):
            if len(comments) < maxc:
                _walk(child, depth + 1)

    comments: list[str] = []
    _walk(resp.json())
    return comments


# ---------- Dev.to ----------


def fetch_devto_posts(tag: str, cfg: dict) -> list[dict]:
    import requests

    per_page = cfg.get("devto_articles_per_tag", 5)
    url = f"https://dev.to/api/articles?tag={urllib.parse.quote(tag)}&per_page={per_page}&top=1"
    resp = requests.get(url, headers={"User-Agent": REDDIT_AGENT}, timeout=15)
    if resp.status_code != 200:
        return []
    posts = []
    for a in resp.json():
        desc = (a.get("description") or "").strip()
        posts.append(
            {
                "id": f"devto:{a['id']}",
                "source": f"dev.to/{tag}",
                "title": a.get("title", ""),
                "selftext": desc[:POST_TEXT_CAP],
                "score": a.get("positive_reactions_count", 0),
                "num_comments": a.get("comments_count", 0),
                "comments": [],
                "_article_id": a["id"],  # transient
            }
        )
        if len(posts) >= per_page:
            break
    return posts


def fetch_devto_comments(post: dict, cfg: dict) -> list[str]:
    import requests

    aid = post.get("_article_id")
    if not aid:
        return []
    maxc = cfg.get("devto_comments_per_post", 3)
    resp = requests.get(f"https://dev.to/api/comments?a_id={aid}", timeout=15)
    if resp.status_code != 200:
        return []
    comments = []
    for c in resp.json():
        body = (c.get("body_html") or "").strip()
        if body:
            comments.append(_html_to_text(body)[:COMMENT_TEXT_CAP])
        if len(comments) >= maxc:
            break
    return comments


# ---------- pure helpers (tested) ----------


def filter_new_posts(posts: list[dict], seen: list[str]) -> list[dict]:
    seen_set = set(seen)
    return [p for p in posts if p["id"] not in seen_set]


def filter_active_posts(posts: list[dict], min_messages: int) -> list[dict]:
    """Keep posts with num_comments >= min_messages. Below-threshold posts stay unseen."""
    return [p for p in posts if p["num_comments"] >= min_messages]


def build_digest(posts: list[dict]) -> str:
    lines = []
    for p in posts:
        src = p.get("source", "unknown")
        lines.append(
            f"### [{src}] {p['title']} ({p['score']} pts, {p['num_comments']} comments)"
        )
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
                capture_output=True,
                text=True,
                check=True,
            ).stdout
        except (FileNotFoundError, subprocess.CalledProcessError):
            print(
                f"warning: could not extract {path} (install poppler-utils, "
                "or point CV_PATH at a .md/.txt export)",
                file=sys.stderr,
            )
            return ""
    return path.read_text(errors="ignore")


def gather_profile() -> tuple[str, list[tuple[str, Path]]]:
    parts: list[str] = []
    pdf_paths: list[tuple[str, Path]] = []
    for cv_raw in os.environ.get("CV_PATH", "").split(","):
        cv_path = Path(cv_raw.strip())
        if cv_raw.strip() and cv_path.exists() and cv_path.is_file():
            name_lower = cv_path.name.lower()
            label = "Resume (Industry)" if "resume" in name_lower else "CV (Academic)"
            if cv_path.suffix.lower() == ".pdf":
                pdf_paths.append((label, cv_path))
            else:
                parts.append(f"## {label}\n" + extract_text(cv_path))
    site = os.environ.get("WEBSITE_DIR", "")
    if site and Path(site).is_dir():
        for f in sorted(Path(site).rglob("*")):
            if f.is_file() and f.suffix.lower() in {".md", ".txt", ".html", ".tex"}:
                parts.append(f"## website: {f.name}\n" + f.read_text(errors="ignore"))
    profile = "\n\n".join(parts).strip()
    if not profile and not pdf_paths:
        print(
            "warning: no profile found (CV_PATH / WEBSITE_DIR unset or empty); "
            "insights will be generic",
            file=sys.stderr,
        )
        profile = "(no profile provided)"
    return profile[:PROFILE_CHAR_CAP], pdf_paths


# ---------- synthesis ----------


# Synthesis retry guardrails — prevent runaway spend on truncated outputs.
_MAX_RETRIES = 1  # retry at most once after a length-truncated generation
_MAX_OUTPUT_TOKENS_HARD = 131072  # never exceed this even on retry (DeepSeek cap)

# Sections that MUST appear in every output (validated post-generation).
_REQUIRED_SECTIONS = [
    "## Employment Market Reality",
    "## Grad School Reality",
    "## Resume Fit & Advice",
    "## CV Fit & Advice",
    "## Job Search Strategy & Tactics",
    "## Interview Preparation",
    "## Target Companies & Roles",
    "## Networking & Outreach",
    "## Negotiation & Market Signals",
    "## Mental Health & Resilience",
    "## Log",
]


def _validate_output(text: str, today: str) -> list[str]:
    """Return list of problems (empty = valid). Checks section presence and Log date."""
    problems: list[str] = []
    missing = [s for s in _REQUIRED_SECTIONS if s not in text]
    if missing:
        problems.append(f"missing sections: {', '.join(missing)}")
    log_prefix = f"- {today}:"
    if "## Log" in text and log_prefix not in text:
        problems.append(f"Log section present but missing today's date ({today})")
    if not text.startswith("# Career Insights for"):
        problems.append("missing or malformed title line")
    return problems


def synthesize(
    cfg: dict,
    profile: str,
    pdf_paths: list[tuple[str, Path]],
    digest: str,
    current: str,
) -> str:
    import requests as _requests

    today = datetime.date.today().isoformat()
    user_content: list[dict] = [
        {
            "type": "text",
            "text": USER_PROMPT.format(current=current, digest=digest, today=today),
        }
    ]
    for label, pdf_path in pdf_paths:
        try:
            with open(pdf_path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
        except OSError as e:
            print(f"warning: could not read {pdf_path}: {e}", file=sys.stderr)
            continue
        user_content.append({"type": "text", "text": f"## {label}\n"})
        user_content.append(
            {
                "type": "file",
                "file": {"file_data": f"data:application/pdf;base64,{b64}"},
            }
        )
        print(f"embedded {label}: {pdf_path.name} ({len(b64) // 1024} KB base64)")

    payload = {
        "model": cfg["model"],
        "max_tokens": cfg["max_output_tokens"],
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT.format(profile=profile)},
            {"role": "user", "content": user_content},
        ],
    }
    resp = _requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}"},
        json=payload,
        timeout=300,
    )
    resp.raise_for_status()
    body = resp.json()
    choice = body["choices"][0]
    text = choice["message"]["content"].strip()
    finish = choice.get("finish_reason", "unknown")

    if finish == "length":
        print(
            f"warning: model stopped early (finish_reason=length) — "
            f"output likely truncated at {cfg['max_output_tokens']} tokens",
            file=sys.stderr,
        )

    if len(text) < 100:
        sys.exit(
            f"model returned suspiciously short output; not overwriting insights:\n{text}"
        )

    problems = _validate_output(text, today)
    if problems:
        print(
            f"warning: output validation found {len(problems)} issue(s):",
            file=sys.stderr,
        )
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
    # Retry capped: only if length-truncated, only if sections are missing,
    # only up to _MAX_RETRIES, and token budget never exceeds hard cap.
    retries = 0
    while (
        finish == "length"
        and any("missing sections" in p for p in problems)
        and retries < _MAX_RETRIES
    ):
        next_tokens = min(payload["max_tokens"] * 2, _MAX_OUTPUT_TOKENS_HARD)
        if next_tokens <= payload["max_tokens"]:
            print(
                "warning: already at hard token cap, cannot retry",
                file=sys.stderr,
            )
            break
        retries += 1
        print(
            f"retry {retries}/{_MAX_RETRIES}: max_tokens={next_tokens} "
            f"(was {payload['max_tokens']})",
            file=sys.stderr,
        )
        payload["max_tokens"] = next_tokens
        resp = _requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}"},
            json=payload,
            timeout=300,
        )
        resp.raise_for_status()
        body = resp.json()
        choice = body["choices"][0]
        text = choice["message"]["content"].strip()
        finish = choice.get("finish_reason", "unknown")
        problems = _validate_output(text, today)
        if finish == "length":
            print(
                "warning: model still truncated on retry — "
                "consider increasing max_output_tokens further",
                file=sys.stderr,
            )
        if problems:
            print(
                f"warning: retry still has {len(problems)} issue(s):",
                file=sys.stderr,
            )
            for p in problems:
                print(f"  - {p}", file=sys.stderr)

    return text + "\n"


# ---------- main ----------


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="fetch and print the digest, skip the LLM call",
    )
    args = parser.parse_args()

    load_env(ROOT / ".env")
    cfg = load_config()
    if not args.dry_run:
        require_env("OPENROUTER_API_KEY")

    try:
        seen = json.loads(SEEN_PATH.read_text()) if SEEN_PATH.exists() else []
    except (json.JSONDecodeError, OSError):
        seen = []
    new_posts: list[dict] = []
    reddit_n = hn_n = devto_n = 0

    # ── Reddit RSS ──
    for sub in cfg.get("reddit_subreddits", []):
        try:
            posts = filter_new_posts(fetch_reddit_rss(sub, cfg), seen)
            new_posts.extend(posts)
            reddit_n += len(posts)
            print(f"r/{sub}: {len(posts)} new posts")
        except Exception as e:
            msg = f"r/{sub}: {e}"
            print(f"warning: {msg}", file=sys.stderr)
            log_error(f"reddit-rss:{sub}", msg)

    # ── HN Algolia ──
    hn_top = cfg.get("hn_top_for_comments", 3)
    for query in cfg.get("hn_queries", []):
        try:
            posts = filter_new_posts(fetch_hn_posts(query, cfg), seen)
            for post in posts[:hn_top]:
                try:
                    post["comments"] = fetch_hn_comments(post, cfg)
                except Exception as e:
                    print(
                        f"warning: HN comments for {post['id']} failed: {e}",
                        file=sys.stderr,
                    )
                    log_error(f"hn-comments:{post['id']}", str(e))
            new_posts.extend(posts)
            hn_n += len(posts)
            print(f"HN '{query[:40]}': {len(posts)} new posts")
        except Exception as e:
            msg = f"HN query '{query[:40]}': {e}"
            print(f"warning: {msg}", file=sys.stderr)
            log_error("hn-algolia", msg)

    # ── Dev.to ──
    dv_top = cfg.get("devto_top_for_comments", 2)
    for tag in cfg.get("devto_tags", []):
        try:
            posts = filter_new_posts(fetch_devto_posts(tag, cfg), seen)
            for post in posts[:dv_top]:
                try:
                    post["comments"] = fetch_devto_comments(post, cfg)
                except Exception as e:
                    print(
                        f"warning: dev.to comments for {post['id']} failed: {e}",
                        file=sys.stderr,
                    )
                    log_error(f"devto-comments:{post['id']}", str(e))
            new_posts.extend(posts)
            devto_n += len(posts)
            print(f"dev.to/{tag}: {len(posts)} new posts")
        except Exception as e:
            msg = f"dev.to/{tag}: {e}"
            print(f"warning: {msg}", file=sys.stderr)
            log_error("devto-forem", msg)

    total_n = reddit_n + hn_n + devto_n
    if not new_posts:
        print("no new posts today; nothing to do")
        return

    min_msgs = cfg.get("min_messages", 25)
    active_posts = filter_active_posts(new_posts, min_msgs)
    skipped = len(new_posts) - len(active_posts)
    if skipped:
        print(
            f"skipped {skipped} post(s) below {min_msgs}-message threshold "
            f"(left unseen for future runs)"
        )

    if not active_posts:
        print(f"no active posts above {min_msgs}-message threshold; nothing to do")
        return

    min_posts = cfg.get("min_posts_to_synthesize", 8)
    if len(active_posts) < min_posts:
        print(
            f"only {len(active_posts)} active post(s) — need {min_posts} "
            f"to justify LLM call; skipping synthesis"
        )
        return

    digest = build_digest(active_posts)
    digest += (
        f"\n\n<!-- Source Summary for Log -->\n"
        f"## Source Summary\n"
        f"- Reddit: {reddit_n} new posts\n"
        f"- Hacker News: {hn_n} new posts\n"
        f"- Dev.to: {devto_n} new posts\n"
        f"- Total: {total_n} new posts\n"
    )
    print(f"collected {total_n} new posts total")

    if args.dry_run:
        print(digest)
        return

    insights_path = ROOT / cfg["insights_path"]
    current = insights_path.read_text() if insights_path.exists() else INSIGHTS_TEMPLATE
    profile, pdf_paths = gather_profile()
    updated = synthesize(cfg, profile, pdf_paths, digest, current)

    insights_path.parent.mkdir(parents=True, exist_ok=True)
    insights_path.write_text(updated)
    SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    SEEN_PATH.write_text(json.dumps(update_seen(seen, active_posts)))
    print(f"updated {insights_path}")


if __name__ == "__main__":
    main()
