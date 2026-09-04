#!/usr/bin/env python3
"""
Export a Mattermost channel history to a clean JSON file.

Usage:
    python export.py

Required environment variables:
    MM_URL          Base URL of your Mattermost instance (e.g. https://mattermost.example.com)
    MM_TOKEN        Personal access token or session token
    MM_CHANNEL_ID   Channel ID(s) to export, comma-separated for multiple channels

Optional:
    MM_OUTPUT_DIR   Directory where output files are written (default: current directory)
"""

import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone
from typing import Any

try:
    import requests
except ImportError:
    sys.exit("Missing dependency: pip install requests")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE_URL = os.environ.get("MM_URL", "").rstrip("/")
TOKEN = os.environ.get("MM_TOKEN", "")
CHANNEL_IDS = [c.strip() for c in os.environ.get("MM_CHANNEL_ID", "").split(",") if c.strip()]
OUTPUT_DIR = os.environ.get("MM_OUTPUT_DIR", ".")
COOKIE = os.environ.get("MM_COOKIE", "")

POSTS_PER_PAGE = 200  # max allowed by Mattermost


def die(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


if not BASE_URL:
    die("MM_URL is not set")
if not TOKEN:
    die("MM_TOKEN is not set")
if not CHANNEL_IDS:
    die("MM_CHANNEL_ID is not set")

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json",
}

# Optional cookies (e.g. MMAUTHTOKEN=...; other=...) sent with every request
COOKIES = {}
for _pair in COOKIE.split(";"):
    if "=" in _pair:
        _name, _value = _pair.split("=", 1)
        COOKIES[_name.strip()] = _value.strip()


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def get(path: str, params: dict | None = None) -> Any:
    url = f"{BASE_URL}/api/v4{path}"
    resp = requests.get(url, headers=HEADERS, cookies=COOKIES, params=params or {})
    if resp.status_code == 429:
        retry_after = int(resp.headers.get("Retry-After", 2))
        print(f"  Rate limited, waiting {retry_after}s …")
        time.sleep(retry_after)
        return get(path, params)
    if not resp.ok:
        die(f"GET {url} returned {resp.status_code}: {resp.text[:200]}")
    return resp.json()


# ---------------------------------------------------------------------------
# User cache
# ---------------------------------------------------------------------------

_user_cache: dict[str, dict] = {}


def get_user(user_id: str) -> dict:
    if user_id not in _user_cache:
        try:
            data = get(f"/users/{user_id}")
            _user_cache[user_id] = {
                "id": user_id,
                "username": data.get("username", ""),
                "first_name": data.get("first_name", ""),
                "last_name": data.get("last_name", ""),
                "nickname": data.get("nickname", ""),
                "email": data.get("email", ""),
            }
        except SystemExit:
            _user_cache[user_id] = {"id": user_id, "username": f"user_{user_id[:8]}"}
    return _user_cache[user_id]


# ---------------------------------------------------------------------------
# Fetch all posts (paginated, oldest first)
# ---------------------------------------------------------------------------

def fetch_all_posts(channel_id: str) -> list[dict]:
    all_posts: dict[str, dict] = {}
    order: list[str] = []
    page = 0

    print(f"Fetching posts from channel {channel_id} …")
    while True:
        data = get(
            f"/channels/{channel_id}/posts",
            params={"page": page, "per_page": POSTS_PER_PAGE},
        )
        batch: dict[str, dict] = data.get("posts", {})
        batch_order: list[str] = data.get("order", [])

        if not batch_order:
            break

        all_posts.update(batch)
        order.extend(batch_order)

        print(f"  Page {page + 1}: {len(batch_order)} posts (total so far: {len(order)})")

        # When the API returns fewer posts than requested we've reached the end
        if len(batch_order) < POSTS_PER_PAGE:
            break

        page += 1

    # order is newest-first; reverse to get chronological order
    order.reverse()
    return [all_posts[pid] for pid in order if pid in all_posts]


# ---------------------------------------------------------------------------
# Fetch thread replies for a root post
# ---------------------------------------------------------------------------

def fetch_thread(root_id: str) -> list[dict]:
    data = get(f"/posts/{root_id}/thread", params={"perPage": 200, "direction": "up"})
    posts: dict[str, dict] = data.get("posts", {})
    order: list[str] = data.get("order", list(posts.keys()))
    # Return replies only (exclude the root post itself), chronological
    return [posts[pid] for pid in order if pid in posts and pid != root_id]


# ---------------------------------------------------------------------------
# Shape a raw post into a clean dict
# ---------------------------------------------------------------------------

def ts_to_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def extract_files(metadata: dict) -> list[dict]:
    files = []
    for f in metadata.get("files", []) or []:
        files.append({
            "id": f.get("id"),
            "name": f.get("name"),
            "mime_type": f.get("mime_type"),
            "size": f.get("size"),
        })
    return files


def extract_reactions(metadata: dict) -> list[dict]:
    reactions: dict[str, dict] = {}
    for r in metadata.get("reactions", []) or []:
        emoji = r.get("emoji_name", "")
        user = get_user(r["user_id"])
        if emoji not in reactions:
            reactions[emoji] = {"emoji": emoji, "count": 0, "users": []}
        reactions[emoji]["count"] += 1
        reactions[emoji]["users"].append(user["username"])
    return list(reactions.values())


def extract_embeds(metadata: dict) -> list[dict]:
    embeds = []
    for e in metadata.get("embeds", []) or []:
        if not isinstance(e, dict):
            continue
        entry: dict = {"type": e.get("type"), "url": e.get("url")}
        # "data" is not always a dict (e.g. some embed types carry a plain string)
        og = e.get("data")
        if not isinstance(og, dict):
            og = {}
        if og.get("title"):
            entry["title"] = og["title"]
        if og.get("description"):
            entry["description"] = og["description"]
        embeds.append(entry)
    return embeds


def shape_post(raw: dict, include_thread: bool = True) -> dict | None:
    # Skip deleted posts and system messages unless you want them
    if raw.get("delete_at", 0) > 0:
        return None
    if raw.get("type", "") not in ("", "slack_attachment"):
        # system messages (joins, leaves, channel renames …)
        return None

    metadata = raw.get("metadata") or {}
    user = get_user(raw["user_id"])

    post: dict = {
        "id": raw["id"],
        "created_at": ts_to_iso(raw["create_at"]),
        "updated_at": ts_to_iso(raw["update_at"]) if raw.get("update_at") else None,
        "user": user,
        "message": raw.get("message", ""),
        "reactions": extract_reactions(metadata),
        "files": extract_files(metadata),
        "links": extract_embeds(metadata),
    }

    if raw.get("props", {}).get("attachments"):
        post["attachments"] = raw["props"]["attachments"]

    # Resolve thread (only for root posts during main pass)
    if include_thread and not raw.get("root_id"):
        try:
            replies_raw = fetch_thread(raw["id"])
            replies = [r for r in (shape_post(rp, include_thread=False) for rp in replies_raw) if r]
            if replies:
                post["thread"] = replies
        except Exception as exc:
            print(f"  Warning: could not fetch thread for {raw['id']}: {exc}")

    return post


# ---------------------------------------------------------------------------
# Channel metadata
# ---------------------------------------------------------------------------

def fetch_channel_info(channel_id: str) -> dict:
    data = get(f"/channels/{channel_id}")
    team_id = data.get("team_id", "")
    team_name = ""
    if team_id:
        try:
            team = get(f"/teams/{team_id}")
            team_name = team.get("display_name", team.get("name", ""))
        except Exception:
            pass
    return {
        "id": channel_id,
        "name": data.get("name", ""),
        "display_name": data.get("display_name", ""),
        "type": data.get("type", ""),
        "purpose": data.get("purpose", ""),
        "header": data.get("header", ""),
        "team": team_name,
        "created_at": ts_to_iso(data["create_at"]) if data.get("create_at") else None,
    }


# ---------------------------------------------------------------------------
# Slug helper
# ---------------------------------------------------------------------------

def slugify(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^\w\s-]", "", value).strip().lower()
    return re.sub(r"[-\s]+", "-", value)


# ---------------------------------------------------------------------------
# Export a single channel
# ---------------------------------------------------------------------------

def export_channel(channel_id: str) -> None:
    print(f"Mattermost Channel Export")
    print(f"  Server  : {BASE_URL}")
    print(f"  Channel : {channel_id}")
    print()

    channel_info = fetch_channel_info(channel_id)
    print(f"  Channel name : {channel_info['display_name']} (#{channel_info['name']})")
    print()

    raw_posts = fetch_all_posts(channel_id)
    print(f"\nShaping {len(raw_posts)} posts …")

    messages = []
    for i, raw in enumerate(raw_posts, 1):
        if i % 50 == 0:
            print(f"  {i}/{len(raw_posts)} …")
        if raw.get("root_id"):
            continue  # thread reply — will appear nested under its parent
        try:
            shaped = shape_post(raw)
        except Exception as exc:
            print(f"  Warning: could not shape post {raw.get('id')}: {exc}")
            continue
        if shaped:
            messages.append(shaped)

    output = {
        "exported_at": datetime.now(tz=timezone.utc).isoformat(),
        "channel": channel_info,
        "message_count": len(messages),
        "messages": messages,
    }

    slug = slugify(channel_info["display_name"] or channel_info["name"] or channel_id)
    output_file = os.path.join(OUTPUT_DIR, f"{slug}.json")

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\nDone. {len(messages)} messages written to {output_file}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    for channel_id in CHANNEL_IDS:
        export_channel(channel_id)
        print()


if __name__ == "__main__":
    main()
