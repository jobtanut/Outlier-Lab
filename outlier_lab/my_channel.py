"""My channel: your own videos through the same outlier formula, joined with private
YouTube Analytics (CTR, AVD, subs gained), plus an audience profile:
what viewers search, where they come from, which channels' videos send them to you,
and who they are.

Needs the Google sign-in (youtube_auth). Data API calls (your video list, titles of the
videos that send you traffic) use the API key and cost ~10 units; Analytics has its own quota.
"""
import csv
import io
import urllib.error
from datetime import date, datetime, timedelta, timezone

from . import discover
from . import outlier_filter as of
from . import youtube_auth as ya

REPORTS = "https://youtubeanalytics.googleapis.com/v2/reports"
REPORTING = "https://youtubereporting.googleapis.com/v1"
REACH_REPORT = "channel_reach_basic_a1"
PROFILE_DAYS = 90      # window for search terms, traffic and audience
KPI_DAYS = 30
SINCE = "2015-01-01"   # before the channel existed, so per-video stats are lifetime


def report(start, end, metrics, **params):
    r = ya.get(REPORTS, ids="channel==MINE", startDate=start, endDate=end, metrics=metrics, **params)
    names = [h["name"] for h in r["columnHeaders"]]
    return [dict(zip(names, row)) for row in r.get("rows", [])]


def per_video_stats(end):
    rows = report(SINCE, end, "views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,subscribersGained",
                  dimensions="video", sort="-views", maxResults=200)
    return {r["video"]: r for r in rows}


def reach_stats():
    """Impressions and CTR per video. The Analytics API doesn't offer them, so they come from
    the Reporting API's daily 'channel_reach_basic_a1' files. The first refresh creates the
    report job; YouTube then publishes a file per day (first one within ~48 hours).
    Returns ({video_id: {"impressions", "clicks"}}, first_date, last_date) or ({}, None, None)."""
    jobs = ya.get(REPORTING + "/jobs").get("jobs", [])
    job = next((j for j in jobs if j["reportTypeId"] == REACH_REPORT), None)
    if not job:
        ya.post(REPORTING + "/jobs", {"reportTypeId": REACH_REPORT, "name": "Outlier Lab reach"})
        return {}, None, None
    reports = ya.get(REPORTING + f"/jobs/{job['id']}/reports", pageSize=500).get("reports", [])
    per_video, days = {}, []
    for rep in reports:
        rows = list(csv.DictReader(io.StringIO(ya.download(rep["downloadUrl"]))))
        days.append(rep["startTime"][:10])
        for r in rows:
            impressions = float(r.get("video_thumbnail_impressions") or 0)
            ctr = float(r.get("video_thumbnail_impressions_ctr") or 0)  # a ratio, e.g. 0.054
            v = per_video.setdefault(r["video_id"], {"impressions": 0, "clicks": 0})
            v["impressions"] += impressions
            v["clicks"] += impressions * ctr
    return per_video, (min(days) if days else None), (max(days) if days else None)


def my_videos(channel_id, key, end):
    """Every upload, long-form scored against your own same-period average."""
    _, _, vids = of.channel_videos(channel_id, key, need_before="1970-01-01T00:00:00+00:00")
    longform = [v for v in vids if v["seconds"] > of.SHORTS_MAX_SECONDS]
    of.score_videos(longform, longform)
    stats = per_video_stats(end)
    for v in longform:
        a = stats.get(v["id"], {})
        v.update(
            avd=a.get("averageViewDuration"),
            avd_pct=a.get("averageViewPercentage"),
            subs_gained=a.get("subscribersGained"),
            watch_minutes=a.get("estimatedMinutesWatched"),
        )
    return longform, len(vids) - len(longform)


def suggested_from(start, end, key):
    """Other creators' videos that send viewers to you, grouped by channel: a direct
    read on what your audience also watches."""
    rows = report(start, end, "views", dimensions="insightTrafficSourceDetail",
                  filters="insightTrafficSourceType==RELATED_VIDEO", sort="-views", maxResults=25)
    views = {r["insightTrafficSourceDetail"]: r["views"] for r in rows}
    videos = discover.video_details(views, key)
    names = discover.channel_info({v["channel_id"] for v in videos}, key)
    me = ya.my_channel()[0]
    channels = {}
    for v in videos:
        if v["channel_id"] == me:
            continue  # your own videos suggesting each other
        title, subs = names.get(v["channel_id"], ("(hidden)", 0))
        c = channels.setdefault(v["channel_id"], {"channel_id": v["channel_id"], "title": title,
                                                  "subscribers": subs, "views": 0, "videos": []})
        c["views"] += views.get(v["id"], 0)
        c["videos"].append({"id": v["id"], "title": v["title"], "views_sent": views.get(v["id"], 0)})
    return sorted(channels.values(), key=lambda c: -c["views"])


# Sections refresh independently (each has its own button on the board); a browser reload
# refreshes them all. Order matters: "videos" rebuilds the list that "ctr" fills in.
SECTIONS = ("kpis", "videos", "ctr", "search_terms", "traffic", "suggested_from", "audience")


def build(key, progress=lambda msg, frac=None: None, sections=SECTIONS, previous=None):
    """Refresh `sections`, keeping everything else from `previous` (the last saved copy)."""
    end_d = date.today() - timedelta(days=1)  # Analytics lags about a day
    end = end_d.isoformat()
    p_start = (end_d - timedelta(days=PROFILE_DAYS - 1)).isoformat()
    k_start = (end_d - timedelta(days=KPI_DAYS - 1)).isoformat()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cid, title, subs = ya.my_channel()
    out = dict(previous or {})
    out.update(channel_id=cid, title=title, subscribers=subs, bar=of.min_score_for(subs),
               profile_days=PROFILE_DAYS, kpi_days=KPI_DAYS, updated=now)
    errors, stamps = dict(out.get("errors", {})), dict(out.get("section_updated", {}))

    def videos():
        old = {v["id"]: v for v in out.get("videos", [])}
        out["videos"], out["shorts"] = my_videos(cid, key, end)
        for v in out["videos"]:  # keep CTR until the ctr section is refreshed
            v["ctr"], v["impressions"] = old.get(v["id"], {}).get("ctr"), old.get(v["id"], {}).get("impressions")

    def ctr():
        reach, out["ctr_from"], out["ctr_to"] = reach_stats()
        for v in out.get("videos", []):
            r = reach.get(v["id"])
            v["impressions"] = round(r["impressions"]) if r else None
            v["ctr"] = r["clicks"] / r["impressions"] * 100 if r and r["impressions"] else None

    def audience():
        out["age_gender"] = report(p_start, end, "viewerPercentage", dimensions="ageGroup,gender")
        out["countries"] = report(p_start, end, "views", dimensions="country", sort="-views", maxResults=8)

    fetchers = {
        "kpis": lambda: out.__setitem__("kpis", report(
            k_start, end, "views,estimatedMinutesWatched,averageViewDuration,subscribersGained")[0]),
        "videos": videos,
        "ctr": ctr,
        "search_terms": lambda: out.__setitem__("search_terms", report(
            p_start, end, "views", dimensions="insightTrafficSourceDetail",
            filters="insightTrafficSourceType==YT_SEARCH", sort="-views", maxResults=25)),
        "traffic": lambda: out.__setitem__("traffic", report(
            p_start, end, "views", dimensions="insightTrafficSourceType", sort="-views")),
        "suggested_from": lambda: out.__setitem__("suggested_from", suggested_from(p_start, end, key)),
        "audience": audience,
    }
    # One section failing (e.g. too little data) shouldn't sink the rest
    todo = [n for n in SECTIONS if n in sections]
    for i, name in enumerate(todo):
        progress(f"My channel: {name.replace('_', ' ')}", i / len(todo))
        try:
            fetchers[name]()
            errors.pop(name, None)
            stamps[name] = now
        except Exception as e:
            errors[name] = str(e)
    out["errors"], out["section_updated"] = errors, stamps
    return out
