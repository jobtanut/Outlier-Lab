"""Google sign-in for reading your own channel's private data (YouTube Analytics).

Read-only scopes. The OAuth client file and the saved login live next to the API key:
    ~/.config/content-scout/client_secret.json   (downloaded from Google Cloud)
    ~/.config/content-scout/token.json           (written here after you sign in)

Flow: /oauth/start redirects to Google -> you approve -> Google redirects back to
/oauth/callback with a one-time code -> it's swapped for tokens. The refresh token
keeps OL signed in (in Testing mode Google expires it after 7 days; just reconnect).
"""
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CONFIG = Path.home() / ".config/content-scout"
CLIENT_FILE = CONFIG / "client_secret.json"
TOKEN_FILE = CONFIG / "token.json"
SCOPES = [
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]
_pending_states = set()  # CSRF protection for the callback


class NotConnected(Exception):
    pass


def _client():
    if not CLIENT_FILE.exists():
        raise NotConnected(f"Missing {CLIENT_FILE}")
    data = json.loads(CLIENT_FILE.read_text())
    return data.get("web") or data.get("installed")


def redirect_uri(port):
    return f"http://localhost:{port}/oauth/callback"


def auth_url(port):
    c = _client()
    state = secrets.token_urlsafe(16)
    _pending_states.add(state)
    return c["auth_uri"] + "?" + urllib.parse.urlencode({
        "client_id": c["client_id"],
        "redirect_uri": redirect_uri(port),
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",   # ask for a refresh token
        "prompt": "consent",        # make sure Google returns one every time
        "include_granted_scopes": "true",
        "state": state,
    })


def _post_token(params):
    c = _client()
    body = urllib.parse.urlencode({**params, "client_id": c["client_id"],
                                   "client_secret": c["client_secret"]}).encode()
    with urllib.request.urlopen(urllib.request.Request(c["token_uri"], data=body), timeout=30) as r:
        return json.load(r)


def _save(token):
    token["expires_at"] = time.time() + token.get("expires_in", 3600) - 60
    CONFIG.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps(token))
    os.chmod(TOKEN_FILE, 0o600)


def handle_callback(query, port):
    """query: parsed query dict from Google's redirect. Returns a message for the page."""
    if "error" in query:
        return False, f"Google said: {query['error'][0]}"
    state = query.get("state", [""])[0]
    if state not in _pending_states:
        return False, "This sign-in link expired. Start again from Outlier Lab."
    _pending_states.discard(state)
    token = _post_token({"code": query["code"][0], "redirect_uri": redirect_uri(port),
                         "grant_type": "authorization_code"})
    _save(token)
    return True, "Connected. You can close this tab and go back to Outlier Lab."


def access_token():
    """A valid access token, refreshing it when expired."""
    if not TOKEN_FILE.exists():
        raise NotConnected("Not connected to YouTube yet")
    token = json.loads(TOKEN_FILE.read_text())
    if time.time() < token.get("expires_at", 0):
        return token["access_token"]
    if not token.get("refresh_token"):
        raise NotConnected("Login expired, reconnect YouTube")
    try:
        fresh = _post_token({"refresh_token": token["refresh_token"], "grant_type": "refresh_token"})
    except urllib.error.HTTPError:
        raise NotConnected("Login expired, reconnect YouTube")
    token.update(fresh)
    _save(token)
    return token["access_token"]


def get(url, **params):
    """GET a Google API with the signed-in user's token."""
    req = urllib.request.Request(url + "?" + urllib.parse.urlencode(params),
                                 headers={"Authorization": f"Bearer {access_token()}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def post(url, payload):
    """POST JSON to a Google API with the signed-in user's token."""
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST", headers={
        "Authorization": f"Bearer {access_token()}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def download(url):
    """Raw text of a file Google serves to the signed-in user (e.g. a Reporting API CSV)."""
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token()}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8")


def my_channel():
    """(id, title, subscribers) of the signed-in channel."""
    items = get("https://www.googleapis.com/youtube/v3/channels", part="snippet,statistics", mine="true")["items"]
    ch = items[0]
    return ch["id"], ch["snippet"]["title"], int(ch["statistics"].get("subscriberCount", 0))


def status():
    try:
        cid, title, subs = my_channel()
        return {"connected": True, "channel_id": cid, "title": title, "subscribers": subs}
    except NotConnected as e:
        return {"connected": False, "reason": str(e)}
    except Exception as e:
        return {"connected": False, "reason": f"Couldn't reach YouTube: {e}"}


def disconnect():
    TOKEN_FILE.unlink(missing_ok=True)
