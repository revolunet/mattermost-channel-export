# mattermost-channel-export

Four scripts to select, export a Mattermost channel history, and render it as a static HTML or PDF archive.

Tested against Mattermost **10.12.4**.

---

## Scripts

| Script       | Input                 | Output                                          |
| ------------ | --------------------- | ------------------------------------------------ |
| `export.py`  | Mattermost REST API   | `<channel-slug>/<channel-slug>.json` + downloaded images |
| `export_channels_list.py` | Mattermost REST API | `channels.tsv` |
| `to_html.py` | `<channel-slug>/<channel-slug>.json` | `<channel-slug>/index.html` |
| `to_pdf.py`  | `<channel-slug>/<channel-slug>.json` | `<channel-slug>/index.pdf` |

`export.py` writes each channel's JSON and its downloaded images into its own
`<channel-slug>/` directory, so `to_html.py`'s `index.html` can reference the
images as plain relative paths (see below). The archive is that whole
directory, not a single file.

---

## Setup

```bash
uv sync
cp .env.example .env
# Edit .env with your Mattermost URL and personal access token
```

PDF rendering (`to_pdf.py`, `export.py --pdf`) additionally requires a Chrome or Chromium binary on `PATH`
(see [`to_pdf.py`](#render-a-pdf-archive--to_pdfpy)). It is a system dependency, not installed by `uv sync`.

---

## Export Channels List — `export_channels_list.py`

Export a team's non-archived channels to a TSV file with columns: `name | display_name | id | Open or Private | member (yes/no) | members count`

Requires `MM_TEAM_NAME` (the team's URL slug, e.g. `my-team` in `https://mattermost.example.com/my-team/channels/...`) in `.env`.
Lists every non-archived public channel of the team (regardless of membership), plus the private channels of that
team the account is already a member of. Mattermost does not expose a team's other private channels (the ones the
account has not joined) to non-admin accounts, so those cannot be listed.

The `member` column tells whether the account running the export (`MM_TOKEN`) belongs to that channel — this is
the only reliable way to know which private channels are the ones you are a member of, since every listed private
channel technically is one. Rows where you are a member are listed first.
The `members count` column is `N/A` for public channels you have not joined: Mattermost restricts
`/channels/{id}/stats` (member count) to channel members, even though the channel itself is listable.

```bash
uv run --env-file .env export_channels_list.py [-o output.tsv]
```

---

## Export a channel's messages — `export.py`

Fetches the full history of a channel via the Mattermost API and writes a clean JSON file.

```bash
uv run --env-file .env export.py
```

### Environment variables

You can `cp .env.example .env` and edit the values there.
`.env` is gitignored. `MM_URL`, `MM_TOKEN`, `MM_TEAM_NAME` and `MM_COOKIE` are also used by
`export_channels_list.py`.

| Variable        | Required | Description                                                                                              |
| --------------- | -------- | -------------------------------------------------------------------------------------------------------- |
| `MM_URL`        | Yes      | Base URL of your instance, e.g. `https://mattermost.example.com`                                         |
| `MM_TOKEN`      | Yes      | Personal access token (Account Settings → Security → Personal Access Tokens)                             |
| `MM_TEAM_NAME`  | For `export_channels_list.py` and `export.py --channels-file` | Team's URL slug to list channels from, e.g. `my-team`    |
| `MM_CHANNEL_ID` | For `export.py` without `--channels-file` | ID of the channel(s) to export. Comma-separated for multiple channels, e.g. `abc123,def456` |
| `MM_OUTPUT_DIR` | No       | Directory where the output files are written (default: current directory). Overridden by `export.py -o` |
| `MM_COOKIE`     | No       | Cookie header to send with every request, needed when the instance sits behind an auth proxy (see below) |
| `MM_DOWNLOAD_IMAGES` | No  | Download shared images (`image/*` files) next to the JSON (default: `true`). Set to `false` to skip.     |


### Cookie-based authentication (`MM_COOKIE`)

If the Mattermost instance is behind `oauth2-proxy`, a personal access token alone may not be enough to get past
the proxy: requests are rejected (typically redirected to the proxy's login page) before reaching Mattermost. In
that case, the scripts must also send the proxy's session cookie, set in `MM_COOKIE`.

**Prerequisite:** log in to the Mattermost instance (through the proxy's login page) in the browser of your choice.
The cookie only exists in the browser you logged in with, so retrieve it from that same browser.

Then, on a Mattermost page of that browser:

1. Open the developer tools (`F12`, or `Ctrl+Shift+I` / `Cmd+Option+I` on macOS).
2. Open the cookie storage:
   - **Chrome / Edge / Chromium:** *Application* tab → *Storage* → *Cookies*
   - **Firefox:** *Storage* tab → *Cookies*
   - **Safari:** *Storage* tab → *Cookies* (enable *Show features for web developers* in Settings → Advanced first)
3. Select the instance's domain (e.g. `https://mattermost.example.com`).
4. Copy the *Value* of the `_oauth2_proxy` cookie, and put it in `.env`:

```bash
MM_COOKIE="_oauth2_proxy=<copied value>"
```

`MM_COOKIE` accepts a standard `key=value; key2=value2` cookie string, so you can pass additional cookies alongside
`_oauth2_proxy` if needed. The cookie expires with the proxy session (and is invalidated when you log out): if the
scripts start failing again with a redirect or `403`, log in again in the browser and copy the new value.

### Usage

If you have setup channel ID(s) and output dir in your `.env` you can simply run

```bash
uv run --env-file .env export.py
```

To export a list of channels in one go, and render them as PDF (and/or HTML) at the same time:

```bash
uv run --env-file .env export.py --channels-file channels.tsv --pdf [--html] [-o exports]
```

`--channels-file` takes a CSV or TSV file with a header row, e.g. the `channels.tsv` written by
`export_channels_list.py` (trimmed down to the channels you want), or a hand-written list. The channel is read
from the first of these columns that exists: `id`, `name`, `display_name`, `Canal`, `channel`. Entries are
resolved against the channels of `MM_TEAM_NAME`, by ID, name (URL slug), or display name — emojis, accents,
case and punctuation are ignored, and a `Display name (slug)` form is accepted, so `🚀 Déploiement (deploiement)`,
`Déploiement` and `deploiement` all match the same channel.

A channel that cannot be found or exported (e.g. a private channel you are not a member of) does not stop the
batch: it is reported in the summary at the end, and the script then exits with status 1.

With `--pdf`, the PDF is written as `<channel-slug>/<channel-slug>.pdf`; with `--html`, as
`<channel-slug>/index.html` (see `to_pdf.py` / `to_html.py` below).

| Argument               | Description                                                                                   |
| ---------------------- | --------------------------------------------------------------------------------------------- |
| `--channels-file FILE` | CSV/TSV listing the channels to export, instead of `MM_CHANNEL_ID` (requires `MM_TEAM_NAME`) |
| `--pdf`                | Also render each channel as a PDF (requires Chrome/Chromium, see `to_pdf.py`)                 |
| `--html`               | Also render each channel as an HTML page                                                      |
| `-o`, `--output-dir`   | Output directory, overrides `MM_OUTPUT_DIR`                                                   |

### What it exports

- All messages, chronologically ordered, fully paginated
- Per-message: author (username, display name), timestamp, message text, edit status
- Reactions with emoji and list of users
- File attachments (name, MIME type, size). Images (PNG, JPEG, GIF, …) are also
  downloaded next to the JSON, and get a `local_path` pointing to the file.
  Other file types (PDF, docs, …) are listed but not downloaded.
- Link embeds (URL, OpenGraph title and description)
- Thread replies nested under their root post
- System messages and deleted posts are excluded

Re-running `export.py` is safe and cheap: images already present on disk
(same size as reported by the server) are not re-downloaded.

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
        },
        {
          "id": "...",
          "name": "screenshot.png",
          "mime_type": "image/png",
          "size": 310647,
          "local_path": "abc123_screenshot.png"
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

## Render a proper HTML page with a channel's history — `to_html.py`

Converts the JSON export into a static HTML page (no external dependencies, works offline). It has no network dependency of its own; when the JSON references downloaded images (`local_path`), it expects them next to the JSON, which is where `export.py` put them.

```bash
uv run --env-file .env to_html.py [input.json] [output.html]

# defaults:
uv run --env-file .env to_html.py
# reads channel_export.json → writes index.html next to it
```

The input file must have a `.json` extension and the output file an `.html` one.

### Features

- Dark theme (Discord-style), clean and readable
- Sidebar with channel info, message count, and month-by-month navigation
- Messages grouped by date with dividers
- User avatars with initials, color-coded by username
- Real Unicode emoji in reactions and message text (`:thumbsup:` → 👍)
- Markdown rendering: bold, italic, strikethrough, inline code, code blocks, blockquotes, headings, horizontal rules, **tables**
- Reactions shown as pills with user list on hover
- Images shown inline (click to open full size); other file attachments shown as pills with size
- Link embeds with title and description
- Thread replies collapsed by default, expandable inline
- Live search bar — filters messages and highlights matches in real-time
- Zebra-striped, horizontally-scrollable tables

---

## Render a PDF archive — `to_pdf.py`

Converts the JSON export into a single PDF file, using the same rendering as `to_html.py`. Useful for platforms
that accept PDF but reject `.html` uploads (some hardened file-sharing platforms, e.g. Nextcloud-based ones, block
HTML for security reasons) or don't have a JSON viewer, e.g. the French government's
[Suite numérique des agents publics](https://fichiers.numerique.gouv.fr/).

```bash
uv run --env-file .env to_pdf.py [input.json] [output.pdf]

# defaults:
uv run --env-file .env to_pdf.py
# reads channel_export.json → writes index.pdf next to it
```

The input file must have a `.json` extension and the output file a `.pdf` one.

Requires a Chrome or Chromium binary on `PATH` (tries `google-chrome`, `google-chrome-stable`, `chromium`,
`chromium-browser` in that order), used headless to render the page and print it to PDF. This is an external
system dependency, not managed by `uv`/`pyproject.toml`.

Images are downscaled for the PDF (1600 px on the longest side, JPEG unless they have transparency, first frame
only for animated GIFs) into a hidden `.pdf-images/` directory next to the originals, which are left untouched.
Without this, Chrome embeds full-size phone photos as-is, which bloats the PDF and makes Chrome crash or hang on
image-heavy channels. Rendering a single PDF is aborted after 30 minutes.

Since a PDF is static (no click interaction), two things differ from the HTML output:
- Thread replies are always expanded, instead of collapsed behind a "N replies" toggle — otherwise their text
  would be invisible and not searchable in the PDF.
- The live search box is hidden, since it cannot work without JavaScript-driven filtering.

Everything else (dark theme, avatars, reactions, embedded images, markdown rendering, …) renders identically to
`to_html.py`, and the PDF's text remains searchable (Ctrl+F in a PDF viewer, or full-text search on platforms
that index uploaded documents).

