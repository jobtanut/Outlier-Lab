# Outlier Lab

**A local YouTube outlier scout and ideation board.** It finds videos that beat their own channel's normal performance, lets you triage them in seconds, and ranks them by how well they fit *your* audience, using your own YouTube Studio data.

Runs entirely on your machine. No accounts, no hosting, no third-party services: just the YouTube APIs and a small Python server.

> Built for a Thai channel, but every list (channels, keywords, topic clusters) is editable for any niche.

<img width="1512" height="828" alt="Screenshot 2569-10-27 at 00 23 02" src="https://github.com/user-attachments/assets/36dccc29-cb20-470b-bd91-8588be361358" />

---

## Features

- **Outlier scoring (relative performance):** each video is compared against the channel's **average views over the 180 days before it was published**, so channel growth doesn't distort old videos.
- **Size-aware pass bars:** small channels must outperform by more (3× for 300K+ subs, 5× for 100K+, 10× below). Fully configurable.
- **Five sources:**
  - **Watchlist** of competitor channels
  - **Topic search** across all of YouTube (any language)
  - **Format search:** title patterns from *any* niche (e.g. "how I manage…", "I tried … for 30 days") to borrow and adapt
  - **Add by link:** paste a video URL, or press ⌘V anywhere on the page
  - **Playlist import:** score every video in a public or unlisted playlist
- **Fast triage:** Promote / Park / Kill with keyboard shortcuts (P / L / X / U / O / ← →).
- **Filters:** source, channel, subscriber range (with quick picks), and seven sort orders including views per hour.
- **Suggested channels:** creators who keep producing outliers are suggested for your watchlist.
- **My channel (optional, read-only Google sign-in):**
  - your own videos through the same formula
  - average view duration, retention %, subscribers gained, and CTR (via the Reporting API)
  - an audience profile: viewer search terms, traffic sources, **channels your viewers also watch**, age, gender and countries
- **Audience fit + "For you" ranking:** every outlier gets a 0–100 fit score from your own data (search demand, your proven topics and formats, audience channel overlap), with the reasons shown on hover.
- **Responsive, one-line header**; light and dark themes.

---

## How the score works

```
outlier score = video views ÷ average views of the channel's long-form videos
                published in the 180 days before this video (up to 30 videos)
```

- **Long-form only:** videos over 3 minutes. Shorts are excluded from both the baseline and the candidates.
- **Fallback:** if fewer than 10 videos fall in that window, the 30 videos just before it are used instead.
- **Candidates:** videos 7–90 days old (configurable).
- **Strict comparison:** 9.98× does not pass a 10× bar.

---

## Requirements

- **Python 3.9+**, standard library only, so there's nothing to `pip install`.
- A **YouTube Data API v3 key** (free).
- *Optional, for My channel:* a Google **OAuth client**, plus the YouTube Analytics API and YouTube Reporting API enabled on the same Google Cloud project.

> **macOS note:** the python.org installer ships without root certificates, so HTTPS calls fail with `CERTIFICATE_VERIFY_FAILED`. Either run `Install Certificates.command` from that Python's folder, or use the system Python (`/usr/bin/python3`).

---

## Setup

### 1. YouTube API key (required)

1. In [Google Cloud Console](https://console.cloud.google.com), create a project.
2. Enable **YouTube Data API v3**.
3. **Credentials → Create credentials → API key.** Restrict the key to YouTube Data API v3.
4. Save it locally, outside the repo:

```bash
mkdir -p ~/.config/content-scout
echo "YOUTUBE_API_KEY=your-key-here" > ~/.config/content-scout/.env
chmod 600 ~/.config/content-scout/.env
```

### 2. Google sign-in for My channel (optional)

1. Enable **YouTube Analytics API** and **YouTube Reporting API** in the same project.
2. **Google Auth Platform:**
   - Branding: any app name
   - Audience: **External**, and add your Google account as a **Test user**
   - You can stay in **Testing**. Logins then expire every ~7 days, and you simply reconnect.
3. **Clients → Create client.** Either type works:
   - **Desktop app**
   - **Web application**, with the authorised redirect URI `http://localhost:8765/oauth/callback`
4. Download the JSON and save it:

```bash
mv ~/Downloads/client_secret_*.json ~/.config/content-scout/client_secret.json
chmod 600 ~/.config/content-scout/client_secret.json
```

5. Run the app, then open **http://localhost:8765/oauth/start** (or **Sources → My channel → Connect YouTube**).

The scopes requested are read-only: `youtube.readonly` and `yt-analytics.readonly`. The login token is stored at `~/.config/content-scout/token.json`.

### 3. Run

```bash
python3 server.py
```

Open **http://localhost:8765**. On first run the database (`scout.db`) is created and seeded with example channels and keywords. Replace them in the **Sources** drawer.

---

## Configuration

| Setting | Where | Default |
|---|---|---|
| Port | `SCOUT_PORT` env var | `8765` |
| Database file | `SCOUT_DB` env var | `./scout.db` |
| Candidate age window | `SCOUT_LOOKBACK_DAYS` env var | `90` days |
| Pass bars by subscriber tier | `BAR_TIERS` in `outlier_filter.py` | 3× / 5× / 10× |
| Baseline window and size | `WINDOW_DAYS`, `BASELINE_SIZE` in `outlier_filter.py` | 180 days, 30 videos |
| Shorts cut-off | `SHORTS_MAX_SECONDS` in `outlier_filter.py` | 180 s |
| Seed channels / keywords / formats | `SEED_CHANNELS`, `SEED_KEYWORDS`, `SEED_FORMATS` in `server.py` | example Thai niche |
| Audience-fit topic clusters | `FIT_CLUSTERS` in `static/index.html` | money, discipline, focus, learning |

If you change the port, update the OAuth redirect URI to match.

---

## API quota

The YouTube Data API allows **10,000 units per day** per project.

| Action | Approx. cost |
|---|---|
| Watchlist sweep | ~5 units per channel |
| Topic or format search | 100 per keyword + ~5 per candidate channel |
| Full scout (channels + 10 topics + 8 formats) | ~2,000–2,500 |
| Add a link | ~5 |
| Playlist import | ~1 per 50 videos + ~5 per channel |
| Rescore every card | ~5 per channel |

The Analytics and Reporting APIs have their own separate quotas. Network blips are retried automatically.

---

## Project structure

```
server.py          Local HTTP server, SQLite storage, background jobs, API routes
outlier_filter.py  The formula: uploads → same-period average → score → pass bars (also a CLI)
discover.py        Topic and format search: cheap pre-filter, then the real gate
my_channel.py      Your own channel: videos, Analytics, Reporting (CTR), audience profile
youtube_auth.py    Read-only Google OAuth (sign-in, token refresh)
static/index.html  The entire UI (vanilla JS + CSS, no build step)
```

Command-line scoring, no server needed:

```bash
python3 outlier_filter.py --lookback 90 UCxxxxxxxxxxxxxxxxxxxxxx UCyyyyyyyyyyyyyyyyyyyyyy
```

---

## Privacy & security

- Everything runs on `127.0.0.1`. Your channel data never leaves your machine except to talk to Google's APIs.
- Credentials live in `~/.config/content-scout/`, **outside the repository**.
- `scout.db` holds your triage history and private channel analytics. **Don't commit it.** Suggested `.gitignore`:

```gitignore
scout.db
__pycache__/
*.pyc
.env
client_secret*.json
token.json
.DS_Store
```

---

## Limitations

- **No recent-video comparison:** the YouTube API only gives *current* views, so a video under 7 days old can't be compared fairly and is skipped.
- **CTR delay:** CTR comes from the Reporting API as daily files. The first file appears ~24–48 hours after the report job is created.
- **Topic clusters are keyword lists:** audience fit misses phrasings that aren't in them.
- **No trending:** YouTube's trending lists are almost all videos under 7 days old, so they're not used as a source.

---
