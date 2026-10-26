#!/usr/bin/env python3
"""Outlier gate for the content scout.

A video only enters the Scout Feed if it beats its own channel's baseline
from the same period:

    outlier_score = views / baseline_average

    baseline_average = average views of the channel's long-form videos published
                      in the WINDOW_DAYS before this video (up to the 30 nearest).
                      If fewer than MIN_BASELINE fall in that window, the 30
                      long-form videos published just before it are used instead.

Comparing against the channel "at that time" keeps a channel that has since
grown (or shrunk) from making older videos look better or worse than they were.

Gate (all must pass):
    - long-form (duration > SHORTS_MAX_SECONDS)
    - age between MIN_AGE_DAYS and LOOKBACK_DAYS
    - outlier_score >= 3x for 300k+ subscribers, 5x for 100k-300k, 10x below 100k

Usage: python3 -m outlier_lab.outlier_filter [--lookback 90] CHANNEL_ID ...
Reads YOUTUBE_API_KEY from ~/.config/content-scout/.env
"""
import argparse
import json
import re
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API = "https://www.googleapis.com/youtube/v3/"
ENV_FILE = Path.home() / ".config/content-scout/.env"
SHORTS_MAX_SECONDS = 180
MIN_AGE_DAYS = 7
BASELINE_SIZE = 30  # long-form videos averaged for a baseline
MIN_BASELINE = 10   # fewer than this and the baseline is flagged as low confidence
MAX_PAGES = 6       # up to 300 uploads scanned per channel
WINDOW_DAYS = 180   # "similar timeframe": videos published up to this long before
# Pass bar depends on channel size: small channels swing more, so they must beat
# their baseline by a wider margin to count.
BAR_TIERS = [  # (min subscribers, pass bar), checked top-down
    (300_000, 3.0),
    (100_000, 5.0),
    (0, 10.0),
]


def min_score_for(subs):
    return next(bar for floor, bar in BAR_TIERS if subs >= floor)


def load_key():
    for line in ENV_FILE.read_text().splitlines():
        if line.startswith("YOUTUBE_API_KEY="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f"YOUTUBE_API_KEY not found in {ENV_FILE}")


# Rough running total of API quota spent (daily limit is 10,000 units)
QUOTA = {"units": 0}
UNIT_COST = {"search": 100}


def get(endpoint, key, **params):
    QUOTA["units"] += UNIT_COST.get(endpoint, 1)
    url = API + endpoint + "?" + urllib.parse.urlencode({**params, "key": key})
    # Retry brief network drops (not API errors like 403/404), so long jobs survive a hiccup
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))


def duration_seconds(iso):
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso)
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def channel_videos(channel_id, key, need_before):
    """need_before: ISO time of the oldest video to score; paging continues until
    there's a full baseline published before it (~2 quota units per page)."""
    ch = get("channels", key, part="snippet,contentDetails,statistics", id=channel_id)["items"][0]
    uploads = ch["contentDetails"]["relatedPlaylists"]["uploads"]
    vids, page_token = [], None
    for _ in range(MAX_PAGES):
        params = {"part": "contentDetails", "playlistId": uploads, "maxResults": 50}
        if page_token:
            params["pageToken"] = page_token
        page = get("playlistItems", key, **params)
        ids = ",".join(i["contentDetails"]["videoId"] for i in page["items"])
        vids += get("videos", key, part="snippet,statistics,contentDetails", id=ids)["items"]
        before = sum(duration_seconds(v["contentDetails"]["duration"]) > SHORTS_MAX_SECONDS
                     and v["snippet"]["publishedAt"].replace("Z", "+00:00") < need_before for v in vids)
        page_token = page.get("nextPageToken")
        if before >= BASELINE_SIZE or not page_token:
            break
    return ch["snippet"]["title"], int(ch["statistics"].get("subscriberCount", 0)), [parse_video(v) for v in vids]


def parse_video(v):
    """Flatten a videos.list item (needs snippet, statistics, contentDetails)."""
    published = datetime.fromisoformat(v["snippet"]["publishedAt"].replace("Z", "+00:00"))
    return {
        "id": v["id"],
        "channel_id": v["snippet"]["channelId"],
        "title": v["snippet"]["title"],
        "url": f"https://www.youtube.com/watch?v={v['id']}",
        "published": published.date().isoformat(),
        "published_at": published.isoformat(),
        "age_days": (datetime.now(timezone.utc) - published).days,
        "views": int(v["statistics"].get("viewCount", 0)),
        "seconds": duration_seconds(v["contentDetails"]["duration"]),
    }


def baseline_for(v, longform):
    """(average views, n) from the channel's long-form videos published before v,
    within WINDOW_DAYS if enough are there, else the nearest ones before it."""
    pub = datetime.fromisoformat(v["published_at"])
    earlier = sorted((w for w in longform if w["published_at"] < v["published_at"]
                      and w["age_days"] >= MIN_AGE_DAYS),
                     key=lambda w: w["published_at"], reverse=True)
    in_window = [w for w in earlier
                 if (pub - datetime.fromisoformat(w["published_at"])).days <= WINDOW_DAYS][:BASELINE_SIZE]
    pool = in_window if len(in_window) >= MIN_BASELINE else earlier[:BASELINE_SIZE]
    return (statistics.mean(w["views"] for w in pool), len(pool)) if pool else (0, 0)


def score_videos(videos, longform):
    for v in videos:
        v["baseline"], v["baseline_n"] = baseline_for(v, longform)
        # Unrounded on purpose: the pass bar is strict (9.996x does not pass 10x)
        v["score"] = v["views"] / v["baseline"] if v["baseline"] else 0


def score_channel(channel_id, key, lookback, extra=()):
    """Scores the channel's long-form uploads from the last `lookback` days, plus any
    `extra` videos (parsed dicts, e.g. pasted links) against same-period baselines."""
    oldest = min([datetime.now(timezone.utc).timestamp() - lookback * 86400]
                 + [datetime.fromisoformat(v["published_at"]).timestamp() for v in extra])
    need_before = datetime.fromtimestamp(oldest, timezone.utc).isoformat()
    title, subs, vids = channel_videos(channel_id, key, need_before)
    min_score = min_score_for(subs)
    longform = [v for v in vids if v["seconds"] > SHORTS_MAX_SECONDS]
    # Channel's current baseline (average), shown on the watchlist
    mature = sorted((v for v in longform if v["age_days"] >= MIN_AGE_DAYS),
                    key=lambda v: v["age_days"])[:BASELINE_SIZE]
    current_avg = statistics.mean(v["views"] for v in mature) if mature else 0
    candidates = [v for v in longform if MIN_AGE_DAYS <= v["age_days"] <= lookback]
    extra = [dict(v) for v in extra]
    score_videos(candidates + extra, longform)
    passed = sorted((v for v in candidates if v["score"] >= min_score),
                    key=lambda v: v["score"], reverse=True)
    return {
        "channel": title, "channel_id": channel_id, "subscribers": subs, "min_score": min_score,
        # "median_views" keeps the db column's name; it holds the average
        "median_views": current_avg, "baseline_videos": len(mature),
        "low_confidence": len(mature) < MIN_BASELINE,
        "checked": len(candidates), "passed": passed, "extra": extra,
        "top_scores": sorted(candidates, key=lambda v: v["score"], reverse=True)[:3],
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("channels", nargs="+")
    p.add_argument("--lookback", type=int, default=90)
    p.add_argument("--json", action="store_true")
    a = p.parse_args()
    key = load_key()
    results = [score_channel(c, key, a.lookback) for c in a.channels]
    if a.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return
    for r in results:
        print(f"\n{r['channel']} ({r['subscribers']:,} subs, bar {r['min_score']:.0f}x)"
              f"  |  avg {r['median_views']:,.0f} views "
              f"(from {r['baseline_videos']} long-form)  |  checked {r['checked']} videos "
              f"from the last {a.lookback} days  |  passed {len(r['passed'])}"
              + ("  |  LOW-CONFIDENCE BASELINE" if r["low_confidence"] else ""))
        for v in r["top_scores"]:
            mark = "PASS" if v["score"] >= r["min_score"] else "    "
            print(f"  {mark} {v['score']:>6.2f}x{v['views']:>9,}  {v['age_days']:>3}d  {v['title'][:70]}")


if __name__ == "__main__":
    main()
