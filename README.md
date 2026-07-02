# career-radar

Automated daily career-advancement assistant. Every morning it:

1. Pulls the day's top posts and comments from career-related subreddits
   (job-search trends, CV/resume advice, interview strategies) — staying
   comfortably inside Reddit's **free API tier**.
2. Feeds that raw digest, plus your **locally-stored CV and personal
   website**, to a budget LLM via OpenRouter (DeepSeek V4 Flash by default).
3. Rewrites `data/INSIGHTS.md` — a living markdown document of actionable,
   personalized insights that gets sharper every day.

All personalization (API keys, CV path, website path) lives in gitignored
local files (`.env`, `config.toml`). Nothing personal is ever committed, so
the repo is safe to publish and fork.

```
subreddits ──▶ career_radar.py ──▶ OpenRouter (DeepSeek V4 Flash)
                    ▲                      │
        .env / config.toml                 ▼
        CV + website (local)      data/INSIGHTS.md (updated daily)
```

## Requirements

- Python 3.11+ (uses stdlib `tomllib`)
- A free Reddit API app (script type)
- An [OpenRouter API key](https://openrouter.ai/keys)
- Optional: `poppler-utils` (`pdftotext`) if your CV is a PDF —
  `sudo apt install poppler-utils` / `brew install poppler`

## Setup

```bash
git clone https://github.com/agopalareddy/career-radar
cd career-radar
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 1. Create a Reddit app (free)

1. Go to <https://www.reddit.com/prefs/apps> → "create another app…"
2. Type: **script**. Name: `career-radar`. Redirect URI: `http://localhost:8080` (unused).
3. Note the client ID (under the app name) and the secret.

### 2. Configure

```bash
cp .env.example .env            # secrets + local paths — edit it
cp config.example.toml config.toml   # subreddits + model — edit it
```

- `.env` holds credentials and the **absolute paths** to your CV and (optionally)
  a folder with your personal-website source. These files stay wherever they
  already live on your machine; the tool only reads them.
- `config.toml` holds the subreddit list, the model, and request limits.
  Tailor the subreddits to your field.

### 3. Test

```bash
python3 test_career_radar.py        # offline self-checks
python3 career_radar.py --dry-run   # fetch Reddit, print digest, no LLM call
python3 career_radar.py             # full run — writes data/INSIGHTS.md
```

## Scheduling

### cron

```cron
15 8 * * * cd /path/to/career-radar && .venv/bin/python career_radar.py >> data/run.log 2>&1
```

### systemd user timer

`~/.config/systemd/user/career-radar.service`:

```ini
[Unit]
Description=career-radar daily run

[Service]
Type=oneshot
WorkingDirectory=/path/to/career-radar
ExecStart=/path/to/career-radar/.venv/bin/python career_radar.py
```

`~/.config/systemd/user/career-radar.timer`:

```ini
[Unit]
Description=career-radar daily timer

[Timer]
OnCalendar=*-*-* 08:15
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
systemctl --user enable --now career-radar.timer
```

> GitHub Actions is deliberately **not** the suggested runner: the whole point
> is grounding against files on your machine that never leave it.

## Costs and rate limits

- **Reddit**: free tier allows 100 requests/minute per OAuth client. Default
  config makes ~33 requests per run, spaced 1s apart, once a day.
- **LLM**: default model `deepseek/deepseek-v4-flash` via OpenRouter
  ($0.09 in / $0.18 out per million tokens). A typical run sends ~30–60K
  input tokens and gets a few thousand back — **well under a cent per day**.
  Any OpenRouter model id works in `config.toml` (`deepseek/deepseek-v4-pro`,
  `anthropic/claude-haiku-4.5`, ...) for deeper analysis at higher cost.

## How the insights doc evolves

Each run passes the current `data/INSIGHTS.md` back to the model along with
the day's digest and your profile, and asks it to merge — updating sections,
dropping stale advice, and appending one dated line to a `## Log` section.
Post IDs are tracked in `data/seen.json` so nothing is processed twice. If a
run fails, the previous document is left untouched.

## Files

| File | Committed? | Purpose |
|---|---|---|
| `career_radar.py` | ✅ | the whole pipeline |
| `config.example.toml` / `.env.example` | ✅ | templates |
| `test_career_radar.py` | ✅ | offline self-checks |
| `config.toml` / `.env` | ❌ gitignored | your personalization |
| `data/` | ❌ gitignored | insights doc, seen-post state, logs |

## License

MIT — see [LICENSE](LICENSE).
