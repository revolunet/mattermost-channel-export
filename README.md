# mattermost-channel-export

Two scripts to export a Mattermost channel history and render it as a self-contained static HTML archive.

Tested against Mattermost **10.12.4**.

---

## Scripts

| Script       | Input                 | Output                 |
| ------------ | --------------------- | ---------------------- |
| `export.py`  | Mattermost REST API   | `channel_export.json`  |
| `to_html.py` | `channel_export.json` | `channel_archive.html` |

---

## Requirements

```bash
pip install requests emoji
```

---

## 1. Export — `export.py`

Fetches the full history of a channel via the Mattermost API and writes a clean JSON file.

### Environment variables

| Variable        | Required | Description                                                                                              |
| --------------- | -------- | -------------------------------------------------------------------------------------------------------- |
| `MM_URL`        | Yes      | Base URL of your instance, e.g. `https://mattermost.example.com`                                         |
| `MM_TOKEN`      | Yes      | Personal access token (Account Settings → Security → Personal Access Tokens)                             |
| `MM_CHANNEL_ID` | Yes      | ID of the channel(s) to export. Comma-separated for multiple channels, e.g. `abc123,def456`              |
| `MM_OUTPUT_DIR` | No       | Directory where the output files are written (default: current directory)                                |
| `MM_COOKIE`     | No       | Cookie header to send with every request, needed when the instance sits behind an auth proxy (see below) |

**Finding the channel ID:** in the Mattermost web or desktop app, open the channel → **View Info** — the ID is displayed at the bottom, or visible in the URL.

### Cookie-based authentication (`MM_COOKIE`)

If the Mattermost instance is behind `oauth2-proxy`, a personal access token alone may not be enough to get past the proxy. In that case, grab the `_oauth2_proxy` cookie value from your browser's dev tools (Application/Storage → Cookies) after logging in, and export it:

```bash
export MM_COOKIE="_oauth2_proxy=XXXX"
```

`MM_COOKIE` accepts a standard `key=value; key2=value2` cookie string, so you can pass additional cookies alongside `_oauth2_proxy` if needed.

### Usage

```bash
export MM_URL="https://mattermost.example.com"
export MM_TOKEN="your-personal-access-token"
export MM_CHANNEL_ID="abc123def456"
# Optional: only needed behind an oauth2-proxy
export MM_COOKIE="_oauth2_proxy=XXXX"

python export.py
```

To export several channels in one run, pass multiple comma-separated IDs — one output file is written per channel:

```bash
export MM_CHANNEL_ID="abc123def456,ghi789jkl012"
python export.py
```

### What it exports

- All messages, chronologically ordered, fully paginated
- Per-message: author (username, display name), timestamp, message text, edit status
- Reactions with emoji and list of users
- File attachments (name, MIME type, size)
- Link embeds (URL, OpenGraph title and description)
- Thread replies nested under their root post
- System messages and deleted posts are excluded

### Output format

```json
{
  "exported_at": "2026-04-21T10:00:00+00:00",
  "channel": {
    "id": "...",
    "name": "general",
    "display_name": "General",
    "type": "O",
    "purpose": "...",
    "team": "Acme Corp",
    "created_at": "..."
  },
  "message_count": 1234,
  "messages": [
    {
      "id": "...",
      "created_at": "2026-01-15T09:32:00+00:00",
      "updated_at": null,
      "user": {
        "id": "...",
        "username": "alice",
        "first_name": "Alice",
        "last_name": "Smith",
        "nickname": "",
        "email": "alice@example.com"
      },
      "message": "Hello everyone! :wave:",
      "reactions": [
        { "emoji": "thumbsup", "count": 3, "users": ["bob", "carol", "dave"] }
      ],
      "files": [
        {
          "id": "...",
          "name": "report.pdf",
          "mime_type": "application/pdf",
          "size": 48320
        }
      ],
      "links": [{ "type": "opengraph", "url": "https://...", "title": "..." }],
      "thread": [
        {
          "id": "...",
          "created_at": "...",
          "user": { "username": "bob", "...": "..." },
          "message": "Great!",
          "reactions": [],
          "files": [],
          "links": []
        }
      ]
    }
  ]
}
```

---

## 2. Render — `to_html.py`

Converts the JSON export into a fully self-contained HTML file (no external dependencies, works offline).

### Usage

```bash
python to_html.py [input.json] [output.html]

# defaults:
python to_html.py
# reads channel_export.json → writes channel_archive.html
```

### Features

- Dark theme (Discord-style), clean and readable
- Sidebar with channel info, message count, and month-by-month navigation
- Messages grouped by date with dividers
- User avatars with initials, color-coded by username
- Real Unicode emoji in reactions and message text (`:thumbsup:` → 👍)
- Markdown rendering: bold, italic, strikethrough, inline code, code blocks, blockquotes, headings, horizontal rules, **tables**
- Reactions shown as pills with user list on hover
- File attachments displayed as pills with size
- Link embeds with title and description
- Thread replies collapsed by default, expandable inline
- Live search bar — filters messages and highlights matches in real-time
- Zebra-striped, horizontally-scrollable tables

---

## Full workflow

```bash
# 1. Export
export MM_URL="https://mattermost.example.com"
export MM_TOKEN="your-token"
export MM_CHANNEL_ID="your-channel-id"
# export MM_COOKIE="_oauth2_proxy=XXXX"  # only if behind oauth2-proxy
python export.py

# 2. Render
python to_html.py

# Open in browser
open channel_archive.html
```
