"""Common utilities for Mattermost scripts."""

import os
import re
import sys
import time
import unicodedata
from typing import Any

try:
    import requests
except ImportError:
    sys.exit("Missing dependency: requests")


def die(msg: str) -> None:
    """Exit with error message."""
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


REQUEST_TIMEOUT = 30  # seconds; without this, a stalled connection can hang the export forever
REQUEST_RETRIES = 3  # for network errors only (timeouts, connection drops); 429s are retried indefinitely


def get_mattermost_config_from_env_vars() -> tuple[str, str]:
    """Get MM_URL and MM_TOKEN from environment, exit if missing."""
    base_url = os.environ.get("MM_URL", "").rstrip("/")
    token = os.environ.get("MM_TOKEN", "")

    if not base_url:
        die("MM_URL is not set in .env")
    if not token:
        die("MM_TOKEN is not set in .env")

    return base_url, token


def _get_cookies_from_env() -> dict[str, str]:
    """Parse MM_COOKIE (e.g. '_oauth2_proxy=...; other=...') into a dict.

    Needed when the instance sits behind an auth proxy (see README).
    """
    cookie_header = os.environ.get("MM_COOKIE", "")
    cookies: dict[str, str] = {}
    for pair in cookie_header.split(";"):
        if "=" in pair:
            name, value = pair.split("=", 1)
            cookies[name.strip()] = value.strip()
    return cookies


def get(base_url: str, token: str, path: str, params: dict | None = None, attempt: int = 1) -> Any:
    """HTTP GET request to Mattermost API with rate limit handling."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    url = f"{base_url}/api/v4{path}"
    try:
        resp = requests.get(
            url, headers=headers, cookies=_get_cookies_from_env(), params=params or {}, timeout=REQUEST_TIMEOUT
        )
    except requests.RequestException as exc:
        if attempt >= REQUEST_RETRIES:
            die(f"GET {url} failed after {attempt} attempts: {exc}")
        print(f"  Network error ({exc}), retrying ({attempt}/{REQUEST_RETRIES}) …")
        time.sleep(2 * attempt)
        return get(base_url, token, path, params, attempt + 1)

    if resp.status_code == 429:
        retry_after = int(resp.headers.get("Retry-After", 2))
        print(f"  Rate limited, waiting {retry_after}s …")
        time.sleep(retry_after)
        return get(base_url, token, path, params)

    if not resp.ok:
        die(f"GET {url} returned {resp.status_code}: {resp.text[:200]}")

    return resp.json()


def get_or_none(base_url: str, token: str, path: str, params: dict | None = None, attempt: int = 1) -> Any | None:
    """Like get(), but returns None on 403 (Forbidden) instead of exiting.

    Useful for endpoints such as /channels/{id}/stats, which Mattermost
    restricts to channel members even when the channel itself is listable
    (e.g. a public channel the caller has not joined).
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    url = f"{base_url}/api/v4{path}"
    try:
        resp = requests.get(
            url, headers=headers, cookies=_get_cookies_from_env(), params=params or {}, timeout=REQUEST_TIMEOUT
        )
    except requests.RequestException as exc:
        if attempt >= REQUEST_RETRIES:
            die(f"GET {url} failed after {attempt} attempts: {exc}")
        print(f"  Network error ({exc}), retrying ({attempt}/{REQUEST_RETRIES}) …")
        time.sleep(2 * attempt)
        return get_or_none(base_url, token, path, params, attempt + 1)

    if resp.status_code == 429:
        retry_after = int(resp.headers.get("Retry-After", 2))
        print(f"  Rate limited, waiting {retry_after}s …")
        time.sleep(retry_after)
        return get_or_none(base_url, token, path, params)

    if resp.status_code == 403:
        return None

    if not resp.ok:
        die(f"GET {url} returned {resp.status_code}: {resp.text[:200]}")

    return resp.json()


def get_team_id_by_name(base_url: str, token: str, team_name: str) -> str:
    """Resolve a team's ID from its name (the URL slug, e.g. 'my-team'), exit if not found."""
    team = get(base_url, token, f"/teams/name/{team_name}")
    team_id = team.get("id")
    if not team_id:
        die(f"Could not resolve team '{team_name}' to an ID")
    return team_id


def get_all_pages(
    base_url: str,
    token: str,
    path: str,
    params: dict | None = None,
    per_page: int = 200,
) -> list[Any]:
    """Fetch every page of a paginated list endpoint (page/per_page query params).

    Stops as soon as a page returns fewer than `per_page` items, same
    end-of-pagination heuristic as export.py's fetch_all_posts().
    """
    all_items: list[Any] = []
    page = 0
    base_params = dict(params or {})
    while True:
        page_params = {**base_params, "page": page, "per_page": per_page}
        batch = get(base_url, token, path, page_params)
        if not isinstance(batch, list):
            break
        all_items.extend(batch)
        if len(batch) < per_page:
            break
        page += 1
    return all_items


def fetch_team_channels(base_url: str, token: str, team_name: str) -> list[dict]:
    """Fetch every non-archived open/private channel of a team.

    Combines two sources:
    - all public channels of the team (GET /teams/{id}/channels), regardless
      of membership;
    - the caller's own channels in that team (GET /users/me/channels),
      which also surfaces private channels the caller is a member of.
    Mattermost does not expose a team's private channels to non-admin
    accounts otherwise, so private channels the caller has not joined
    cannot be listed.

    Each returned channel is the raw API object, tagged with "member":
    whether the caller (MM_TOKEN's account) belongs to it.
    """
    print(f"Resolving team '{team_name}'…")
    team_id = get_team_id_by_name(base_url, token, team_name)

    print("Fetching channels…")
    public_channels = get_all_pages(base_url, token, f"/teams/{team_id}/channels")

    my_channels = get(base_url, token, "/users/me/channels")
    if not isinstance(my_channels, list):
        my_channels = []
    my_channel_ids = {
        c["id"] for c in my_channels
        if c.get("team_id") == team_id and not c.get("delete_at")
    }
    my_private_channels = [c for c in my_channels if c["id"] in my_channel_ids and c.get("type") == "P"]

    channels_by_id: dict[str, dict] = {}
    for channel in public_channels + my_private_channels:
        # Only include open (O) and private (P) channels, skip direct (D) messages
        if channel.get("type") in ("O", "P"):
            channels_by_id[channel["id"]] = {**channel, "member": channel["id"] in my_channel_ids}
    return list(channels_by_id.values())


def _normalize_channel_label(value: str) -> str:
    """Reduce a channel label to lowercase ASCII alphanumerics, so that
    '🚀 Déploiement' and 'deploiement' compare equal (emojis, accents,
    punctuation and spacing are all dropped)."""
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]", "", value.lower())


def resolve_channel(label: str, channels: list[dict]) -> dict | None:
    """Find the channel matching a human-written label, or None.

    `label` may be a channel ID, its name (URL slug), its display name
    (with or without emojis/accents), or "Display name (slug)" as shown in
    some Mattermost lists. Exact matches win over normalized ones.
    """
    label = label.strip()
    by_id = {c["id"]: c for c in channels}
    by_name = {c["name"]: c for c in channels}
    if label in by_id:
        return by_id[label]
    if label in by_name:
        return by_name[label]

    # "🚀 Déploiement (deploiement)" → the slug in parentheses is authoritative
    slug_match = re.search(r"\(([^()\s]+)\)\s*$", label)
    if slug_match and slug_match.group(1) in by_name:
        return by_name[slug_match.group(1)]

    for c in channels:
        if c.get("display_name", "").strip() == label:
            return c

    wanted = _normalize_channel_label(label)
    candidates = [
        c for c in channels
        if wanted in (_normalize_channel_label(c.get("display_name", "")), _normalize_channel_label(c["name"]))
    ]
    return candidates[0] if len(candidates) == 1 else None
