#!/usr/bin/env python3
"""
Export Mattermost channels of a team as TSV.

Edit the TSV file to select which channels to export, then use export.py.

Usage:
    uv run --env-file .env export_channels_list.py [-o output.tsv]

Arguments:
    -o, --output     Output TSV file (default: channels.tsv)

Environment (.env):
    MM_URL           Base URL of your Mattermost instance
    MM_TOKEN         Personal access token
    MM_TEAM_NAME     Name (URL slug) of the team to list channels from
    MM_COOKIE        Optional cookie header, needed behind an auth proxy
                     such as oauth2-proxy (see README)
"""

import os
import sys
import argparse
from datetime import datetime, timezone

from mm_common import get_mattermost_config_from_env_vars, die, get_or_none, fetch_team_channels


parser = argparse.ArgumentParser(description="Export a team's non-archived channels to TSV")
parser.add_argument(
    "-o",
    "--output",
    default="channels.tsv",
    help="Output TSV file (default: channels.tsv)",
)
args = parser.parse_args()

BASE_URL, TOKEN = get_mattermost_config_from_env_vars()
TEAM_NAME = os.environ.get("MM_TEAM_NAME", "")
OUTPUT_FILE = args.output

if not TEAM_NAME:
    die("MM_TEAM_NAME is not set in .env")


def fetch_channels() -> list[dict]:
    """Fetch the team's channels (see mm_common.fetch_team_channels) with their member count."""
    all_channels: list[dict] = []
    for channel in fetch_team_channels(BASE_URL, TOKEN, TEAM_NAME):
        # Members-only endpoint: returns 403 for public channels the caller
        # has not joined. member_count is then left as None (shown as N/A).
        stats = get_or_none(BASE_URL, TOKEN, f"/channels/{channel['id']}/stats")

        all_channels.append({
            "id": channel["id"],
            "name": channel.get("name"),
            "display_name": channel.get("display_name", ""),
            "type": channel["type"],
            "member": channel["member"],
            "member_count": stats.get("member_count") if stats else None,
        })

    print(f"Found {len(all_channels)} channels")
    return sorted(all_channels, key=lambda c: (not c["member"], c["name"]))


def export_tsv(channels: list[dict], output_file: str) -> None:
    """Export channels to TSV with a select column."""
    with open(output_file, "w", encoding="utf-8") as f:
        # Header
        f.write("name\tdisplay_name\tid\ttype\tmember\tmember_count\n")

        # Rows, sorted with joined channels first (see fetch_channels)
        for ch in channels:
            channel_type = "Open" if ch["type"] == "O" else "Private"
            member = "yes" if ch["member"] else "no"
            member_count = ch["member_count"] if ch["member_count"] is not None else "N/A"
            f.write(
                f"{ch['name']}\t{ch['display_name']}\t{ch['id']}\t{channel_type}\t{member}\t{member_count}\n"
            )

    print(f"Exported to {output_file}")
    print(f"Remove channels you do not want to export from {output_file}")
    print(f"Then run: uv run --env-file .env export.py --channels-file {output_file} --pdf")


if __name__ == "__main__":
    channels = fetch_channels()
    export_tsv(channels, OUTPUT_FILE)
