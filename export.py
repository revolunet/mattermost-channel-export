#!/usr/bin/env python3
"""
Export a Mattermost channel history to a clean JSON file.

Usage:
    uv run --env-file .env export.py [--channels-file FILE] [--pdf] [--html] [-o DIR]

Arguments:
    --channels-file  CSV/TSV listing the channels to export (see README), instead of MM_CHANNEL_ID.
                     Channels may be given by ID, name or display name; requires MM_TEAM_NAME.
    --pdf            Also render <channel-slug>.pdf next to each JSON (see to_pdf.py;
                     requires Chrome/Chromium on PATH)
    --html           Also render index.html next to each JSON (see to_html.py)
    -o, --output-dir Directory where output files are written (overrides MM_OUTPUT_DIR)

Required environment variables:
    MM_URL          Base URL of your Mattermost instance (e.g. https://mattermost.example.com)
    MM_TOKEN        Personal access token or session token
    MM_CHANNEL_ID   Channel ID(s) to export, comma-separated for multiple channels
                    (unless --channels-file is given)

Optional:
    MM_TEAM_NAME       Team to resolve --channels-file entries in
    MM_OUTPUT_DIR      Directory where output files are written (default: current directory)
    MM_DOWNLOAD_IMAGES Download shared images next to the JSON (default: true)
    MM_COOKIE          Cookie header sent with every request, needed behind an
                       auth proxy such as oauth2-proxy (see README)

Output layout (per channel):
    MM_OUTPUT_DIR/<channel-slug>/<channel-slug>.json
    MM_OUTPUT_DIR/<channel-slug>/<file-id>_<name>.png   (downloaded images)

to_html.py later writes an index.html into that same directory so the
image references resolve as plain relative paths.
"""

import argparse
import csv
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import requests
except ImportError:
    sys.exit("Missing dependency: pip install requests")

from mm_common import fetch_team_channels, resolve_channel


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

parser = argparse.ArgumentParser(description="Export Mattermost channel histories to JSON (and optionally PDF/HTML)")
parser.add_argument("--channels-file", help="CSV/TSV file listing the channels to export, instead of MM_CHANNEL_ID")
parser.add_argument("--pdf", action="store_true", help="Also render each channel as a PDF")
parser.add_argument("--html", action="store_true", help="Also render each channel as an HTML page")
parser.add_argument("-o", "--output-dir", help="Output directory (overrides MM_OUTPUT_DIR)")
args = parser.parse_args()

BASE_URL = os.environ.get("MM_URL", "").rstrip("/")
TOKEN = os.environ.get("MM_TOKEN", "")
CHANNEL_IDS = [c.strip() for c in os.environ.get("MM_CHANNEL_ID", "").split(",") if c.strip()]
TEAM_NAME = os.environ.get("MM_TEAM_NAME", "")
OUTPUT_DIR = args.output_dir or os.environ.get("MM_OUTPUT_DIR", ".")
COOKIE = os.environ.get("MM_COOKIE", "")
DOWNLOAD_IMAGES = os.environ.get("MM_DOWNLOAD_IMAGES", "true").strip().lower() not in ("false", "0", "no")

POSTS_PER_PAGE = 200  # max allowed by Mattermost
FILE_DOWNLOAD_RETRIES = 3  # for network errors only; 429s are retried indefinitely (server tells us when)
IMAGE_DOWNLOAD_DELAY = 0.3  # minimum seconds between image download requests, to stay well under rate limits
REQUEST_TIMEOUT = 30  # seconds; without this, a stalled connection can hang the export forever
REQUEST_RETRIES = 3  # for network errors only (timeouts, connection drops); 429s are retried indefinitely


def die(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


if not BASE_URL:
    die("MM_URL is not set")
if not TOKEN:
    die("MM_TOKEN is not set")
if args.channels_file and not TEAM_NAME:
    die("MM_TEAM_NAME must be set to resolve the channels of --channels-file")
if not args.channels_file and not CHANNEL_IDS:
    die("MM_CHANNEL_ID is not set (or use --channels-file)")

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

def get(path: str, params: dict | None = None, attempt: int = 1) -> Any:
    url = f"{BASE_URL}/api/v4{path}"
    try:
        resp = requests.get(url, headers=HEADERS, cookies=COOKIES, params=params or {}, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        if attempt >= REQUEST_RETRIES:
            die(f"GET {url} failed after {attempt} attempts: {exc}")
        print(f"  Network error ({exc}), retrying ({attempt}/{REQUEST_RETRIES}) …")
        time.sleep(2 * attempt)
        return get(path, params, attempt + 1)
    if resp.status_code == 429:
        retry_after = int(resp.headers.get("Retry-After", 2))
        print(f"  Rate limited, waiting {retry_after}s …")
        time.sleep(retry_after)
        return get(path, params)
    if not resp.ok:
        die(f"GET {url} returned {resp.status_code}: {resp.text[:200]}")
    return resp.json()


def download_file(file_id: str, dest_path: str, attempt: int = 1) -> bool:
    """Download a Mattermost file to dest_path. Returns False (never raises) on failure
    so a single bad image doesn't abort the whole export."""
    if attempt == 1:
        # Self-throttle: space out requests before we ever get a 429, not just after.
        time.sleep(IMAGE_DOWNLOAD_DELAY)

    url = f"{BASE_URL}/api/v4/files/{file_id}"
    try:
        resp = requests.get(url, headers=HEADERS, cookies=COOKIES, timeout=30)
    except requests.RequestException as exc:
        if attempt >= FILE_DOWNLOAD_RETRIES:
            print(f"  Warning: giving up downloading file {file_id}: {exc}")
            return False
        time.sleep(2 * attempt)
        return download_file(file_id, dest_path, attempt + 1)

    if resp.status_code == 429:
        retry_after = int(resp.headers.get("Retry-After", 2))
        print(f"  Rate limited, waiting {retry_after}s …")
        time.sleep(retry_after)
        return download_file(file_id, dest_path, attempt)

    if not resp.ok:
        print(f"  Warning: could not download file {file_id}: HTTP {resp.status_code}")
        return False

    with open(dest_path, "wb") as fh:
        fh.write(resp.content)
    return True


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


def sanitize_filename(name: str) -> str:
    name = re.sub(r"[^\w.\-]", "_", name or "")
    return name or "file"


def extract_files(metadata: dict, channel_dir: str, stats: dict) -> list[dict]:
    files = []
    for f in metadata.get("files", []) or []:
        entry = {
            "id": f.get("id"),
            "name": f.get("name"),
            "mime_type": f.get("mime_type"),
            "size": f.get("size"),
        }
        mime = entry["mime_type"] or ""
        if DOWNLOAD_IMAGES and mime.startswith("image/") and entry["id"]:
            local_name = f"{entry['id']}_{sanitize_filename(entry['name'])}"
            dest_path = os.path.join(channel_dir, local_name)
            if os.path.exists(dest_path) and (
                entry["size"] is None or os.path.getsize(dest_path) == entry["size"]
            ):
                stats["cached"] += 1
                entry["local_path"] = local_name
            elif download_file(entry["id"], dest_path):
                stats["downloaded"] += 1
                entry["local_path"] = local_name
            else:
                stats["failed"] += 1
        files.append(entry)
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


def shape_post(raw: dict, channel_dir: str, stats: dict, include_thread: bool = True) -> dict | None:
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
        "files": extract_files(metadata, channel_dir, stats),
        "links": extract_embeds(metadata),
    }

    if raw.get("props", {}).get("attachments"):
        post["attachments"] = raw["props"]["attachments"]

    # Resolve thread (only for root posts during main pass)
    if include_thread and not raw.get("root_id"):
        try:
            replies_raw = fetch_thread(raw["id"])
            replies = [
                r for r in (
                    shape_post(rp, channel_dir, stats, include_thread=False) for rp in replies_raw
                ) if r
            ]
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

def export_channel(channel_id: str) -> Path:
    print(f"Mattermost Channel Export")
    print(f"  Server  : {BASE_URL}")
    print(f"  Channel : {channel_id}")
    print()

    channel_info = fetch_channel_info(channel_id)
    print(f"  Channel name : {channel_info['display_name']} (#{channel_info['name']})")
    print()

    slug = slugify(channel_info["display_name"] or channel_info["name"] or channel_id)
    channel_dir = os.path.join(OUTPUT_DIR, slug)
    os.makedirs(channel_dir, exist_ok=True)
    image_stats = {"downloaded": 0, "cached": 0, "failed": 0}

    raw_posts = fetch_all_posts(channel_id)
    print(f"\nShaping {len(raw_posts)} posts …")

    messages = []
    for i, raw in enumerate(raw_posts, 1):
        if i % 50 == 0:
            print(f"  {i}/{len(raw_posts)} …")
        if raw.get("root_id"):
            continue  # thread reply — will appear nested under its parent
        try:
            shaped = shape_post(raw, channel_dir, image_stats)
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

    output_file = os.path.join(channel_dir, f"{slug}.json")

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\nDone. {len(messages)} messages written to {output_file}")
    if DOWNLOAD_IMAGES:
        print(
            f"Images: {image_stats['downloaded']} downloaded, "
            f"{image_stats['cached']} already present, "
            f"{image_stats['failed']} failed"
        )
    return Path(output_file)


# ---------------------------------------------------------------------------
# Channels file
# ---------------------------------------------------------------------------

# Column holding the channel, by order of preference (case-insensitive).
# "canal" covers hand-written French lists.
CHANNEL_COLUMNS = ["id", "name", "display_name", "canal", "channel"]


def read_channels_file(path: str) -> list[str]:
    """Read channel labels (IDs, names or display names) from a CSV/TSV with a header row."""
    with open(path, encoding="utf-8-sig", newline="") as fh:
        content = fh.read()
    delimiter = "\t" if "\t" in content.splitlines()[0] else ","
    rows = list(csv.DictReader(content.splitlines(), delimiter=delimiter))
    if not rows:
        die(f"No channels listed in {path}")

    columns = {c.strip().lower(): c for c in rows[0].keys() if c}
    column = next((columns[c] for c in CHANNEL_COLUMNS if c in columns), None)
    if column is None:
        die(f"{path} has none of the expected columns: {', '.join(CHANNEL_COLUMNS)}")
    return [row[column].strip() for row in rows if (row.get(column) or "").strip()]


def resolve_channels_file(path: str) -> tuple[list[str], list[str]]:
    """Map each entry of the channels file to a channel ID of MM_TEAM_NAME.

    Returns (channel IDs, entries that matched no channel).
    """
    labels = read_channels_file(path)
    channels = fetch_team_channels(BASE_URL, TOKEN, TEAM_NAME)
    channel_ids: list[str] = []
    unresolved: list[str] = []
    for label in labels:
        channel = resolve_channel(label, channels)
        if channel is None:
            unresolved.append(label)
        elif channel["id"] not in channel_ids:
            channel_ids.append(channel["id"])
    print(f"{len(channel_ids)}/{len(labels)} channels of {path} found in team '{TEAM_NAME}'")
    for label in unresolved:
        print(f"  Warning: no channel matches '{label}'")
    print()
    return channel_ids, unresolved


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def render(json_file: Path) -> None:
    """Render the requested PDF/HTML versions of an exported channel."""
    with json_file.open(encoding="utf-8") as fh:
        data = json.load(fh)
    if args.html:
        from to_html import build_html
        html_file = json_file.parent / "index.html"
        html_file.write_text(build_html(data), encoding="utf-8")
        print(f"HTML written to {html_file}")
    if args.pdf:
        from to_pdf import render_pdf
        pdf_file = json_file.with_suffix(".pdf")
        print("Rendering PDF …")
        render_pdf(data, pdf_file)
        print(f"PDF written to {pdf_file} ({pdf_file.stat().st_size // 1024} KB)")


def main() -> None:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    unresolved: list[str] = []
    channel_ids = CHANNEL_IDS
    if args.channels_file:
        channel_ids, unresolved = resolve_channels_file(args.channels_file)

    # One failing channel (no access, Chrome crash …) must not abort the
    # whole batch: die()/sys.exit() are caught and reported at the end.
    failed: list[tuple[str, str]] = []
    for i, channel_id in enumerate(channel_ids, 1):
        print(f"[{i}/{len(channel_ids)}]")
        try:
            render(export_channel(channel_id))
        except (Exception, SystemExit) as exc:
            print(f"  Error: export of channel {channel_id} failed: {exc}", file=sys.stderr)
            failed.append((channel_id, str(exc)))
        print()

    print(f"Exported {len(channel_ids) - len(failed)}/{len(channel_ids)} channels")
    for channel_id, reason in failed:
        print(f"  Failed: {channel_id} ({reason})")
    for label in unresolved:
        print(f"  Not found: {label}")
    if failed or unresolved:
        sys.exit(1)


if __name__ == "__main__":
    main()
