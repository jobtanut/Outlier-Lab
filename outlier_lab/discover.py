"""Topic search: find outliers from channels that aren't on the watchlist.

Two stages, so search finds pass the same gate as competitor videos:

  1. Pre-filter (cheap, from search data): long-form, 7..lookback days old,
     views >= BREAKOUT_VIEWS_PER_SUB x the channel's subscribers.
  2. Real gate: work out that channel's same-period average (outlier_filter.score_channel)
     and keep the video only if it passes its bar (3x / 5x / 10x by channel size).
"""
from datetime import datetime, timedelta, timezone

from . import outlier_filter as of

SEARCH_RESULTS = 25
BREAKOUT_VIEWS_PER_SUB = 2.0
MAX_CANDIDATE_CHANNELS = 40  # caps stage-2 quota (~2-12 units per channel)


def search(term, lang, key, lookback):
    after = (datetime.now(timezone.utc) - timedelta(days=lookback)).strftime("%Y-%m-%dT%H:%M:%SZ")
    params = {"part": "snippet", "type": "video", "q": term, "order": "viewCount",
              "publishedAfter": after, "maxResults": SEARCH_RESULTS}
    if lang == "TH":
        params.update(regionCode="TH", relevanceLanguage="th")
    else:
        params.update(relevanceLanguage="en")
    return [i["id"]["videoId"] for i in of.get("search", key, **params).get("items", [])]


def batched(ids, n=50):
    ids = list(ids)
    return [ids[i:i + n] for i in range(0, len(ids), n)]


def video_details(ids, key):
    out = []
    for chunk in batched(ids):
        items = of.get("videos", key, part="snippet,statistics,contentDetails", id=",".join(chunk))["items"]
        out += [of.parse_video(v) for v in items]
    return out


def channel_info(ids, key):
    """{channel_id: (title, subscribers)}; channels with hidden counts are left out."""
    out = {}
    for chunk in batched(ids):
        for c in of.get("channels", key, part="snippet,statistics", id=",".join(chunk))["items"]:
            if not c["statistics"].get("hiddenSubscriberCount"):
                out[c["id"]] = (c["snippet"]["title"], int(c["statistics"].get("subscriberCount", 0)))
    return out


def find_candidates(keywords, key, lookback, skip_channels, progress=lambda msg: None):
    """Stage 1. keywords: [(term, lang)]. Returns {video_id: video dict + keyword/channel info}."""
    found = {}
    for i, (term, lang) in enumerate(keywords, 1):
        progress(f"Searching “{term}” ({i}/{len(keywords)})")
        for vid in search(term, lang, key, lookback):
            found.setdefault(vid, term)
    videos = [v for v in video_details(found, key)
              if v["channel_id"] not in skip_channels
              and v["seconds"] > of.SHORTS_MAX_SECONDS
              and of.MIN_AGE_DAYS <= v["age_days"] <= lookback]
    channels = channel_info({v["channel_id"] for v in videos}, key)
    candidates = {}
    for v in videos:
        if v["channel_id"] not in channels:
            continue
        title, subs = channels[v["channel_id"]]
        if subs and v["views"] >= BREAKOUT_VIEWS_PER_SUB * subs:
            candidates[v["id"]] = {**v, "keyword": found[v["id"]], "channel_title": title, "subscribers": subs}
    return candidates


def gate_candidates(candidates, key, lookback, progress=lambda msg: None):
    """Stage 2. Yields (channel_result, passed_video_with_keyword) using each channel's real baseline."""
    by_channel = {}
    for c in candidates.values():
        by_channel.setdefault(c["channel_id"], []).append(c)
    # Strongest pre-filter ratio first, so the cap drops the weakest channels
    ranked = sorted(by_channel, key=lambda ch: max(c["views"] / c["subscribers"] for c in by_channel[ch]), reverse=True)
    for i, ch in enumerate(ranked[:MAX_CANDIDATE_CHANNELS], 1):
        progress(f"Checking baseline for {by_channel[ch][0]['channel_title']} ({i}/{min(len(ranked), MAX_CANDIDATE_CHANNELS)})")
        r = of.score_channel(ch, key, lookback)
        passed = {v["id"]: v for v in r["passed"]}
        for c in by_channel[ch]:
            if c["id"] in passed:
                yield r, {**passed[c["id"]], "keyword": c["keyword"]}
        if not any(c["id"] in passed for c in by_channel[ch]):
            yield r, None  # still record the channel's baseline
