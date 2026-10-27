#!/usr/bin/env python3
"""Outlier Lab: local triage UI for the content scout.

Only videos that pass the outlier gate (outlier_filter.py) enter the board.
Sources: the watchlist sweep, and topic search for channels you don't track
yet (discover.py). Off-watchlist channels that keep producing outliers are
suggested for the watchlist.

Run:  /usr/bin/python3 server.py      then open http://localhost:8765
Data: data/scout.db (SQLite, gitignored).
"""
import json
import os
import re
import sqlite3
import threading
import urllib.error
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from outlier_lab import discover, my_channel, youtube_auth
from outlier_lab import outlier_filter as of

ROOT = Path(__file__).resolve().parent
# Env overrides exist for testing (e.g. a 90-day run into a throwaway db)
DB_PATH = Path(os.environ.get("SCOUT_DB", ROOT / "data" / "scout.db"))
INDEX = ROOT / "static" / "index.html"
PORT = int(os.environ.get("SCOUT_PORT", 8765))
LOOKBACK_DAYS = int(os.environ.get("SCOUT_LOOKBACK_DAYS", 90))
STATUSES = ("New", "Promoted", "Parked", "Killed")
GROUPS = ("Direct", "Adjacent", "Global")
LANGS = ("TH", "EN")
SCOUT_SCOPES = ("all", "channels", "keywords", "formats")
OUTDATED_DAYS = 365  # matches OUTDATED_DAYS in static/index.html
# An off-watchlist channel is suggested after this many outliers,
# or one outlier at least this many times its pass bar
SUGGEST_MIN_HITS = 2
SUGGEST_STRONG_RATIO = 2.0

# Starter watchlist and searches for a fresh database. Replace them in the board's
# Sources drawer (they're only inserted when the database is first created).
SEED_CHANNELS = [
    ("UCoOae5nYA7VqaXzerajD0lg", "Ali Abdaal", "Direct"),
    ("UCG-KntY7aVnIGXYEBQvmBAQ", "Thomas Frank", "Direct"),
    ("UCJ24N4O0bP7LGLBDvye7oCA", "Matt D'Avella", "Adjacent"),
]
# Draft keywords, to be edited in the board's Keywords panel
SEED_KEYWORDS = [
    ("how to be consistent", "EN", "Discipline"),
    ("self improvement", "EN", "Self-growth"),
    ("productivity system", "EN", "Productivity"),
    ("how to learn faster", "EN", "Learning"),
    ("personal finance for beginners", "EN", "Money"),
]
# Title formats that travel across niches ("How I manage time…" → "How I manage money…").
# Searched without a topic, so results come from any niche.
SEED_FORMATS = [
    ("how I manage", "EN"),
    ("I tried for 30 days", "EN"),
    ("things I wish I knew", "EN"),
    ("stop doing this", "EN"),
]
KINDS = ("topic", "format")
SOURCE_FOR_KIND = {"topic": "Search", "format": "Format"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    id TEXT PRIMARY KEY,
    title TEXT,
    grp TEXT DEFAULT 'Direct',
    subscribers INTEGER,
    median_views REAL,
    baseline_videos INTEGER,
    min_score REAL,
    active INTEGER DEFAULT 1,
    last_checked TEXT
);
CREATE TABLE IF NOT EXISTS signals (
    video_id TEXT PRIMARY KEY,
    channel_id TEXT,
    title TEXT,
    url TEXT,
    published TEXT,
    views INTEGER,
    score REAL,
    min_score REAL,
    median_views REAL,
    seconds INTEGER,
    status TEXT DEFAULT 'New',
    found TEXT,
    updated TEXT
);
CREATE TABLE IF NOT EXISTS keywords (
    id INTEGER PRIMARY KEY,
    term TEXT UNIQUE,
    lang TEXT DEFAULT 'TH',
    pillar TEXT DEFAULT '',
    active INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated TEXT
);
CREATE TABLE IF NOT EXISTS discovered (
    id TEXT PRIMARY KEY,
    title TEXT,
    subscribers INTEGER,
    median_views REAL,
    baseline_videos INTEGER,
    min_score REAL,
    last_checked TEXT,
    dismissed INTEGER DEFAULT 0
);
"""
# Columns added after the first version; added to existing databases on start
SIGNAL_COLUMNS = {
    "source": "TEXT DEFAULT 'Watchlist'",
    "keyword": "TEXT",
    "channel_title": "TEXT",
    "subscribers": "INTEGER",
    "published_at": "TEXT",  # exact publish time, for views per hour
}

scout_state = {"running": False, "last_run": None, "message": "", "error": None, "units": 0, "progress": None}
write_lock = threading.Lock()


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.executescript(SCHEMA)
        have = {r["name"] for r in conn.execute("PRAGMA table_info(signals)")}
        for col, decl in SIGNAL_COLUMNS.items():
            if col not in have:
                conn.execute(f"ALTER TABLE signals ADD COLUMN {col} {decl}")
        if not conn.execute("SELECT 1 FROM channels LIMIT 1").fetchone():
            conn.executemany("INSERT INTO channels (id, title, grp) VALUES (?, ?, ?)", SEED_CHANNELS)
        if not conn.execute("SELECT 1 FROM keywords LIMIT 1").fetchone():
            conn.executemany("INSERT INTO keywords (term, lang, pillar) VALUES (?, ?, ?)", SEED_KEYWORDS)
        # Format patterns arrived later: add the column, and seed formats only that first time
        if "kind" not in {r["name"] for r in conn.execute("PRAGMA table_info(keywords)")}:
            conn.execute("ALTER TABLE keywords ADD COLUMN kind TEXT DEFAULT 'topic'")
            conn.executemany("INSERT OR IGNORE INTO keywords (term, lang, pillar, kind) VALUES (?, ?, '', 'format')",
                             SEED_FORMATS)


def api_error(e):
    if isinstance(e, urllib.error.HTTPError):
        try:
            return json.load(e)["error"]["message"]
        except Exception:
            return f"YouTube API error {e.code}"
    return str(e)


# --- scout run -------------------------------------------------------------

def save_signal(conn, v, r, source, stamp):
    """Insert a passed video; on re-finds refresh numbers but keep status and source.
    Returns 1 if the video is new to the board."""
    is_new = not conn.execute("SELECT 1 FROM signals WHERE video_id=?", (v["id"],)).fetchone()
    conn.execute(
        "INSERT INTO signals (video_id, channel_id, title, url, published, views, score, min_score,"
        " median_views, seconds, found, updated, source, keyword, channel_title, subscribers, published_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(video_id) DO UPDATE SET views=excluded.views, score=excluded.score,"
        " median_views=excluded.median_views, subscribers=excluded.subscribers, updated=excluded.updated,"
        " published_at=excluded.published_at",
        (v["id"], r["channel_id"], v["title"], v["url"], v["published"], v["views"], v["score"],
         r["min_score"], v.get("baseline") or r["median_views"], v["seconds"], stamp, stamp, source, v.get("keyword"),
         r["channel"], r["subscribers"], v["published_at"]))
    return int(is_new)


def sweep_watchlist(key, progress):
    with db() as conn:
        channels = conn.execute("SELECT id, title FROM channels WHERE active = 1").fetchall()
    added = 0
    for i, ch in enumerate(channels, 1):
        progress(f"Scanning {ch['title'] or ch['id']} ({i}/{len(channels)})", (i - 1) / len(channels))
        r = of.score_channel(ch["id"], key, LOOKBACK_DAYS)
        stamp = now_iso()
        with write_lock, db() as conn:
            conn.execute(
                "UPDATE channels SET title=?, subscribers=?, median_views=?, baseline_videos=?,"
                " min_score=?, last_checked=? WHERE id=?",
                (r["channel"], r["subscribers"], r["median_views"], r["baseline_videos"],
                 r["min_score"], stamp, ch["id"]))
            for v in r["passed"]:
                added += save_signal(conn, v, r, "Watchlist", stamp)
    return len(channels), added


def search_keywords(key, progress, kind):
    """kind 'topic' (cards tagged Search) or 'format' (cards tagged Format)."""
    with db() as conn:
        keywords = [(k["term"], k["lang"]) for k in conn.execute(
            "SELECT term, lang FROM keywords WHERE active = 1 AND kind = ?", (kind,))]
        watched = {r["id"] for r in conn.execute("SELECT id FROM channels")}
    if not keywords:
        return 0, 0
    candidates = discover.find_candidates(keywords, key, LOOKBACK_DAYS, watched, progress)
    added = 0
    for r, v in discover.gate_candidates(candidates, key, LOOKBACK_DAYS, progress):
        stamp = now_iso()
        with write_lock, db() as conn:
            conn.execute(
                "INSERT INTO discovered (id, title, subscribers, median_views, baseline_videos, min_score, last_checked)"
                " VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET title=excluded.title,"
                " subscribers=excluded.subscribers, median_views=excluded.median_views,"
                " baseline_videos=excluded.baseline_videos, min_score=excluded.min_score,"
                " last_checked=excluded.last_checked",
                (r["channel_id"], r["channel"], r["subscribers"], r["median_views"],
                 r["baseline_videos"], r["min_score"], stamp))
            if v:
                added += save_signal(conn, v, r, SOURCE_FOR_KIND[kind], stamp)
    return len(keywords), added


class JobCancelled(BaseException):
    """Raised at the next progress step after Stop is pressed. A BaseException so the
    per-section `except Exception` handlers (e.g. in my_channel.build) don't swallow it."""


def progress_reporter(start=0.0, span=1.0):
    """Progress callback for one phase of a job: a phase's own 0..1 is mapped onto the
    job's overall bar between start and start + span. Every job reports progress before
    each channel/keyword/section, which is also where a Stop takes effect."""
    def report(msg, frac=None):
        if scout_state.get("cancel"):
            raise JobCancelled()
        update = {"message": msg}
        if frac is not None:
            update["progress"] = round(min(1.0, start + span * frac), 4)
        scout_state.update(update)
    return report


# Rough relative cost of one unit of work in each scout phase, for the progress bar:
# a keyword (a search plus baseline checks on what it finds) takes far longer than a channel
PHASE_WEIGHT = {"channels": 1, "topic": 4, "format": 4}


def scout_thread(scope):
    """scope: 'all', 'channels' (watchlist sweep), 'keywords' (topic search) or 'formats'."""
    of.QUOTA["units"] = 0
    with db() as conn:
        counts = {"channels": conn.execute("SELECT COUNT(*) FROM channels WHERE active = 1").fetchone()[0]}
        for kind in KINDS:
            counts[kind] = conn.execute("SELECT COUNT(*) FROM keywords WHERE active = 1 AND kind = ?",
                                        (kind,)).fetchone()[0]
    phases = [p for p, on in (("channels", scope in ("all", "channels")), ("topic", scope in ("all", "keywords")),
                              ("format", scope in ("all", "formats"))) if on]
    weights = {p: counts[p] * PHASE_WEIGHT[p] for p in phases}
    total = sum(weights.values()) or 1
    spans, start = {}, 0.0
    for p in phases:
        spans[p] = progress_reporter(start, weights[p] / total)
        start += weights[p] / total
    try:
        key = of.load_key()
        parts = []
        if "channels" in spans:
            n_ch, from_watch = sweep_watchlist(key, spans["channels"])
            parts.append(f"{n_ch} channels scanned, {from_watch} new")
        if "topic" in spans:
            n_kw, from_search = search_keywords(key, spans["topic"], "topic")
            parts.append(f"{n_kw} topics searched, {from_search} new")
        if "format" in spans:
            n_fm, from_format = search_keywords(key, spans["format"], "format")
            parts.append(f"{n_fm} formats searched, {from_format} new")
        scout_state.update(progress=1.0)
        scout_state.update(error=None, message=" · ".join(parts))
    except JobCancelled:
        scout_state.update(error=None, message="Stopped. Outliers found before the stop are kept")
    except Exception as e:
        scout_state.update(error=api_error(e), message="Scout failed")
    finally:
        scout_state.update(running=False, last_run=now_iso(), units=of.QUOTA["units"])


# --- pasted videos -----------------------------------------------------------

def video_id_from(text):
    """watch?v=, youtu.be/, /shorts/, /live/ links, or a bare 11-character ID."""
    text = text.strip()
    m = (re.search(r"[?&]v=([\w-]{11})", text)
         or re.search(r"(?:youtu\.be/|/shorts/|/live/|/embed/)([\w-]{11})", text)
         or re.fullmatch(r"([\w-]{11})", text))
    if not m:
        raise ValueError("Paste a YouTube video link")
    return m.group(1)


def add_playlist(list_id, key, progress=lambda msg, frac=None: None):
    """Import every long-form video in a public or unlisted playlist as Manual cards.
    Costs ~1 unit per 50 videos plus one baseline check per distinct channel."""
    found = of.get("playlists", key, part="snippet", id=list_id).get("items", [])
    if not found:
        raise ValueError("Can't read that playlist. If it's private, set it to Unlisted and try again")
    name = found[0]["snippet"]["title"]
    ids, token = [], None
    while True:
        params = {"part": "contentDetails", "playlistId": list_id, "maxResults": 50}
        if token:
            params["pageToken"] = token
        page = of.get("playlistItems", key, **params)
        ids += [i["contentDetails"]["videoId"] for i in page["items"]]
        token = page.get("nextPageToken")
        if not token:
            break
    with db() as conn:
        have = {r[0] for r in conn.execute("SELECT video_id FROM signals")}
    videos = discover.video_details([i for i in ids if i not in have], key)
    longform = [v for v in videos if v["seconds"] > of.SHORTS_MAX_SECONDS]
    by_channel = {}
    for v in longform:
        by_channel.setdefault(v["channel_id"], []).append(v)
    added = below = 0
    for i, (ch, group) in enumerate(by_channel.items(), 1):
        progress(f"Importing “{name}”: channel baseline {i}/{len(by_channel)}", (i - 1) / len(by_channel))
        r = of.score_channel(ch, key, LOOKBACK_DAYS, extra=group)
        for v in r["extra"]:
            if not v["baseline"]:
                continue  # no earlier long-form videos to compare against
            v["keyword"] = name  # shown on the card as "From playlist · <name>"
            with write_lock, db() as conn:
                save_signal(conn, v, r, "Manual", now_iso())
            added += 1
            below += v["score"] < r["min_score"]
    skipped = len(ids) - added
    return (f"Imported {added} videos from “{name}” ({below} below the bar)"
            + (f", skipped {skipped} (Shorts, unavailable or already on the board)" if skipped else ""))


def rescore_all(progress):
    """Re-score every card with fresh views and the current formula (after a formula
    change). Untriaged non-Manual cards that no longer pass are dropped."""
    key = of.load_key()
    with db() as conn:
        ids = [r[0] for r in conn.execute("SELECT video_id FROM signals")]
    by_channel = {}
    for v in discover.video_details(ids, key):
        by_channel.setdefault(v["channel_id"], []).append(v)
    for i, (ch, group) in enumerate(by_channel.items(), 1):
        progress(f"Rescoring: channel {i}/{len(by_channel)}", (i - 1) / len(by_channel))
        r = of.score_channel(ch, key, LOOKBACK_DAYS, extra=group)
        with write_lock, db() as conn:
            for v in r["extra"]:
                if v["baseline"]:
                    conn.execute("UPDATE signals SET views=?, score=?, median_views=?, min_score=?,"
                                 " subscribers=?, updated=? WHERE video_id=?",
                                 (v["views"], v["score"], v["baseline"], r["min_score"], r["subscribers"],
                                  now_iso(), v["id"]))
    with write_lock, db() as conn:
        dropped = conn.execute("DELETE FROM signals WHERE status = 'New' AND score < min_score"
                               " AND source != 'Manual'").rowcount
    return f"Rescored {len(ids)} cards across {len(by_channel)} channels, dropped {dropped} that no longer pass"


def refresh_my_channel(progress, sections=my_channel.SECTIONS):
    """Refresh some or all My channel sections, merged into the saved copy."""
    with db() as conn:
        row = conn.execute("SELECT value FROM kv WHERE key='my_channel'").fetchone()
    data = my_channel.build(of.load_key(), progress, sections, json.loads(row[0]) if row else None)
    with write_lock, db() as conn:
        conn.execute("INSERT INTO kv (key, value, updated) VALUES ('my_channel', ?, ?)"
                     " ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated=excluded.updated",
                     (json.dumps(data, ensure_ascii=False), now_iso()))
    failed = [n for n in sections if n in data["errors"]]
    what = "everything" if len(sections) == len(my_channel.SECTIONS) else ", ".join(n.replace("_", " ") for n in sections)
    return f"My channel: refreshed {what}" + (f" ({', '.join(failed)} unavailable)" if failed else "")


def job_thread(fn, *args):
    """Long jobs (playlist import, rescore) run like a scout: in the background,
    reporting progress through scout_state."""
    of.QUOTA["units"] = 0
    try:
        msg = fn(*args, progress=progress_reporter())
        scout_state.update(progress=1.0)
        scout_state.update(error=None, message=msg)
    except JobCancelled:
        scout_state.update(error=None, message="Stopped. Anything saved before the stop is kept")
    except Exception as e:
        scout_state.update(error=api_error(e), message="Job failed")
    finally:
        scout_state.update(running=False, last_run=now_iso(), units=of.QUOTA["units"], job=None, sections=[])


def playlist_id_from(text):
    """A playlist link without a specific video (v=) in it, else None."""
    m = re.search(r"[?&]list=([\w-]+)", text)
    return m.group(1) if m and not re.search(r"[?&]v=", text) else None




def add_pasted_video(text):
    """Score one video against its channel's average and add it as a Manual card,
    whether or not it passes the bar (the card says so). A playlist link imports
    the whole playlist."""
    key = of.load_key()
    vid = video_id_from(text)
    with db() as conn:
        if conn.execute("SELECT 1 FROM signals WHERE video_id=?", (vid,)).fetchone():
            return "Already on the board"
    found = discover.video_details([vid], key)
    if not found:
        raise ValueError("Video not found on YouTube")
    v = found[0]
    if v["seconds"] <= of.SHORTS_MAX_SECONDS:
        raise ValueError("That's a Short. Scores compare long-form videos, so Shorts aren't added")
    r = of.score_channel(v["channel_id"], key, LOOKBACK_DAYS, extra=[v])
    v = r["extra"][0]
    if not v["baseline"]:
        raise ValueError("No earlier long-form videos on that channel to compare against")
    with write_lock, db() as conn:
        save_signal(conn, v, r, "Manual", now_iso())
    verdict = "passes" if v["score"] >= r["min_score"] else "is below"
    return f"Added: {v['score']:.2f}× {verdict} the {r['min_score']:g}× bar"


# --- channels --------------------------------------------------------------

def resolve_channel(text, key):
    """Accept a channel ID, @handle, or channel URL."""
    text = text.strip()
    m = re.search(r"(UC[\w-]{22})", text)
    params = {"id": m.group(1)} if m else None
    if not params:
        m = re.search(r"@([\w.\-]+)", text)
        if not m:
            raise ValueError("Paste a channel URL, @handle or UC… channel ID")
        params = {"forHandle": "@" + m.group(1)}
    items = of.get("channels", key, part="snippet,statistics", **params).get("items", [])
    if not items:
        raise ValueError("Channel not found on YouTube")
    ch = items[0]
    return ch["id"], ch["snippet"]["title"], int(ch["statistics"].get("subscriberCount", 0))


def state():
    with db() as conn:
        channels = [dict(r) for r in conn.execute("SELECT * FROM channels ORDER BY grp, title")]
        signals = [dict(r) for r in conn.execute(
            "SELECT s.*, COALESCE(c.title, s.channel_title) AS channel,"
            " COALESCE(c.subscribers, s.subscribers) AS subscribers, c.grp FROM signals s"
            " LEFT JOIN channels c ON c.id = s.channel_id ORDER BY s.score DESC")]
        keywords = [dict(r) for r in conn.execute("SELECT * FROM keywords ORDER BY kind DESC, pillar, lang, term")]
        suggested = [dict(r) for r in conn.execute(
            "SELECT d.*, COUNT(s.video_id) AS hits, MAX(s.score / s.min_score) AS best_ratio,"
            " MAX(s.score) AS best_score FROM discovered d"
            " JOIN signals s ON s.channel_id = d.id AND s.source = 'Search'"
            " WHERE d.dismissed = 0 AND d.id NOT IN (SELECT id FROM channels)"
            " GROUP BY d.id HAVING hits >= ? OR best_ratio >= ?"
            " ORDER BY hits DESC, best_ratio DESC", (SUGGEST_MIN_HITS, SUGGEST_STRONG_RATIO))]
    return {"channels": channels, "signals": signals, "keywords": keywords, "suggested": suggested,
            "scout": scout_state, "lookback_days": LOOKBACK_DAYS}


# --- http ------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def send_json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def send_page(self, html, code=200):
        body = html.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        if url.path == "/oauth/start":
            try:
                target = youtube_auth.auth_url(PORT)
            except youtube_auth.NotConnected as e:
                return self.send_page(f"<p>{e}</p>", 400)
            print("Sign-in: sending browser to Google", flush=True)
            self.send_response(302)
            self.send_header("Location", target)
            self.end_headers()
            return
        if url.path == "/oauth/callback":
            try:
                ok, msg = youtube_auth.handle_callback(urllib.parse.parse_qs(url.query), PORT)
            except Exception as e:
                ok, msg = False, f"Sign-in failed: {api_error(e)}"
            print(f"Sign-in callback: {'OK' if ok else 'FAILED'} - {msg}", flush=True)
            return self.send_page(
                f'<meta http-equiv="refresh" content="{2 if ok else 30};url=/">'
                f'<body style="font:16px system-ui;padding:40px">{"✅" if ok else "⚠️"} {msg}</body>')
        if url.path == "/api/youtube":
            return self.send_json(youtube_auth.status())
        if url.path == "/api/mychannel":
            with db() as conn:
                row = conn.execute("SELECT value FROM kv WHERE key='my_channel'").fetchone()
            return self.send_json(json.loads(row[0]) if row else {})
        if self.path in ("/", "/index.html"):
            body = INDEX.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/state":
            self.send_json(state())
        else:
            self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        parts = self.path.strip("/").split("/")
        try:
            if parts == ["api", "scout"]:
                scope = self.body().get("scope", "all")
                if scope not in SCOUT_SCOPES:
                    return self.send_json({"error": "bad scope"}, 400)
                if not scout_state["running"]:
                    scout_state.update(running=True, error=None, progress=0, cancel=False, message="Starting…")
                    threading.Thread(target=scout_thread, args=(scope,), daemon=True).start()
                return self.send_json(state())

            if parts == ["api", "videos"]:
                url = self.body().get("url", "")
                list_id = playlist_id_from(url)
                if list_id:
                    if scout_state["running"]:
                        return self.send_json({"error": "Wait for the current run to finish"}, 400)
                    scout_state.update(running=True, error=None, progress=0, cancel=False, message="Reading playlist…")
                    threading.Thread(target=job_thread, args=(lambda progress: add_playlist(
                        list_id, of.load_key(), progress),), daemon=True).start()
                    return self.send_json({**state(), "message": "Importing playlist… progress is in the status line", "job": True})
                return self.send_json({**state(), "message": add_pasted_video(url)})

            if parts == ["api", "youtube", "disconnect"]:
                youtube_auth.disconnect()
                return self.send_json(youtube_auth.status())

            if parts == ["api", "mychannel", "refresh"]:
                b = self.body()
                sections = tuple(n for n in b.get("sections") or my_channel.SECTIONS if n in my_channel.SECTIONS)
                if b.get("auto") and (scout_state["running"] or not youtube_auth.TOKEN_FILE.exists()):
                    return self.send_json(state())  # page-load refresh: skip quietly if busy or not connected
                if scout_state["running"]:
                    return self.send_json({"error": "Wait for the current run to finish"}, 400)
                scout_state.update(running=True, error=None, progress=0, cancel=False, message="My channel: starting…", job="my_channel",
                                   sections=list(sections))
                threading.Thread(target=job_thread, args=(
                    lambda progress: refresh_my_channel(progress, sections),), daemon=True).start()
                return self.send_json(state())

            if parts == ["api", "stop"]:
                if scout_state["running"]:
                    scout_state.update(cancel=True, message="Stopping after the current step…")
                return self.send_json(state())

            if parts == ["api", "rescore"]:
                if scout_state["running"]:
                    return self.send_json({"error": "Wait for the current run to finish"}, 400)
                scout_state.update(running=True, error=None, progress=0, cancel=False, message="Rescoring…")
                threading.Thread(target=job_thread, args=(rescore_all,), daemon=True).start()
                return self.send_json(state())

            if parts[:2] == ["api", "signals"] and len(parts) == 3:
                status = self.body().get("status")
                if status not in STATUSES:
                    return self.send_json({"error": "bad status"}, 400)
                with write_lock, db() as conn:
                    conn.execute("UPDATE signals SET status=? WHERE video_id=?", (status, parts[2]))
                return self.send_json(state())

            if parts == ["api", "channels"]:
                b = self.body()
                grp = b.get("group") if b.get("group") in GROUPS else "Direct"
                cid, title, subs = resolve_channel(b.get("input", ""), of.load_key())
                with write_lock, db() as conn:
                    conn.execute(
                        "INSERT INTO channels (id, title, grp, subscribers, min_score) VALUES (?, ?, ?, ?, ?)"
                        " ON CONFLICT(id) DO UPDATE SET active=1, grp=excluded.grp",
                        (cid, title, grp, subs, of.min_score_for(subs)))
                return self.send_json(state())

            if parts[:2] == ["api", "channels"] and len(parts) == 3:
                b = self.body()
                with write_lock, db() as conn:
                    if "active" in b:
                        conn.execute("UPDATE channels SET active=? WHERE id=?", (int(bool(b["active"])), parts[2]))
                    if b.get("group") in GROUPS:
                        conn.execute("UPDATE channels SET grp=? WHERE id=?", (b["group"], parts[2]))
                return self.send_json(state())

            if parts[:2] == ["api", "suggested"] and len(parts) == 4 and parts[3] == "dismiss":
                with write_lock, db() as conn:
                    conn.execute("UPDATE discovered SET dismissed=1 WHERE id=?", (parts[2],))
                return self.send_json(state())

            if parts == ["api", "keywords"]:
                b = self.body()
                term = (b.get("term") or "").strip()
                if not term:
                    return self.send_json({"error": "Type a keyword"}, 400)
                lang = b.get("lang") if b.get("lang") in LANGS else "TH"
                kind = b.get("kind") if b.get("kind") in KINDS else "topic"
                with write_lock, db() as conn:
                    conn.execute(
                        "INSERT INTO keywords (term, lang, pillar, kind) VALUES (?, ?, ?, ?)"
                        " ON CONFLICT(term) DO UPDATE SET active=1, lang=excluded.lang, pillar=excluded.pillar,"
                        " kind=excluded.kind",
                        (term, lang, (b.get("pillar") or "").strip() if kind == "topic" else "", kind))
                return self.send_json(state())

            if parts[:2] == ["api", "keywords"] and len(parts) == 3:
                b = self.body()
                with write_lock, db() as conn:
                    if "active" in b:
                        conn.execute("UPDATE keywords SET active=? WHERE id=?", (int(bool(b["active"])), parts[2]))
                return self.send_json(state())
        except Exception as e:
            return self.send_json({"error": api_error(e)}, 400)
        self.send_json({"error": "not found"}, 404)

    def do_DELETE(self):
        parts = self.path.strip("/").split("/")
        if parts == ["api", "signals", "outdated"]:
            # Same rule as the board's red Outdated tag: published more than a year ago
            with write_lock, db() as conn:
                n = conn.execute("DELETE FROM signals WHERE julianday('now') - julianday("
                                 f"COALESCE(published_at, published)) > {OUTDATED_DAYS}").rowcount
            return self.send_json({**state(), "message": f"Removed {n} outdated cards"})
        table = {"channels": "channels", "keywords": "keywords"}.get(parts[1] if len(parts) == 3 else "")
        if parts[0] == "api" and table:
            with write_lock, db() as conn:
                conn.execute(f"DELETE FROM {table} WHERE id=?", (parts[2],))
            return self.send_json(state())
        self.send_json({"error": "not found"}, 404)


def backfill_published_at():
    """Older rows only stored the publish date; fetch exact times (1 unit per 50 videos)."""
    with db() as conn:
        ids = [r["video_id"] for r in conn.execute("SELECT video_id FROM signals WHERE published_at IS NULL")]
    if not ids:
        return
    try:
        key = of.load_key()
        for v in discover.video_details(ids, key):
            with write_lock, db() as conn:
                conn.execute("UPDATE signals SET published_at=? WHERE video_id=?", (v["published_at"], v["id"]))
    except Exception as e:
        print("Could not backfill publish times:", api_error(e))


def apply_current_bars():
    """Re-apply outlier_filter's pass bars to stored rows, so a bar change takes effect
    without a rescan. Untriaged (New) cards that no longer pass are dropped; cards you
    already promoted, parked or killed are kept."""
    with write_lock, db() as conn:
        for table, subs_col in (("signals", "subscribers"), ("channels", "subscribers"), ("discovered", "subscribers")):
            rows = conn.execute(f"SELECT rowid, {subs_col} FROM {table} WHERE {subs_col} IS NOT NULL").fetchall()
            conn.executemany(f"UPDATE {table} SET min_score=? WHERE rowid=?",
                             [(of.min_score_for(r[1]), r[0]) for r in rows])
        # Manual cards are allowed below the bar, so they're never dropped
        dropped = conn.execute(
            "DELETE FROM signals WHERE status = 'New' AND score < min_score AND source != 'Manual'").rowcount
    if dropped:
        print(f"Dropped {dropped} New cards that no longer pass the current bars")


if __name__ == "__main__":
    init_db()
    backfill_published_at()
    apply_current_bars()
    print(f"Outlier Lab running at http://localhost:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
