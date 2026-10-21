#!/usr/bin/env python3
"""
Convert a Mattermost channel export JSON (produced by export.py) to a
single-file PDF archive, using the same rendering as to_html.py.

Useful for uploading to platforms that accept PDF but reject .html files
(e.g. some hardened Nextcloud instances) or don't have a JSON viewer.

Unlike the interactive HTML page, thread replies are always expanded
(there is no click interaction in a static PDF), and the live search box
is hidden, since it cannot work without JavaScript-driven pagination.

Usage:
    uv run to_pdf.py [input.json] [output.pdf]

Defaults:
    input  : channel_export.json
    output : index.pdf, written next to the input JSON (so it can embed
             any images export.py downloaded into that same directory)

Requires a Chrome/Chromium binary on PATH (google-chrome, google-chrome-stable,
or chromium), used headless to render the page and print it to PDF.
"""

import shutil
import subprocess
import sys
import json
from pathlib import Path

from to_html import build_html

# CSS injected on top of to_html.py's stylesheet: always show thread
# replies (no click interaction in a static PDF) and hide the live
# search box (non-functional without further interaction).
PDF_OVERRIDES_CSS = """
.thread-replies.hidden { display: block !important; }
.thread-toggle { display: none !important; }
.search-wrap { display: none !important; }
"""

CHROME_CANDIDATES = ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"]


def find_chrome() -> str:
    for name in CHROME_CANDIDATES:
        path = shutil.which(name)
        if path:
            return path
    sys.exit(
        "ERROR: no Chrome/Chromium binary found on PATH "
        f"(tried: {', '.join(CHROME_CANDIDATES)}). Install one to use to_pdf.py."
    )


def render_pdf(data: dict, output_file: Path) -> None:
    html_content = build_html(data).replace("</style>", PDF_OVERRIDES_CSS + "</style>", 1)

    # Chrome resolves the image src="..." relative paths against the HTML
    # file's own location, so the temp file must sit next to the images
    # (i.e. in the same directory export.py wrote them to).
    temp_html = output_file.parent / f".{output_file.stem}.pdf-source.html"
    temp_html.write_text(html_content, encoding="utf-8")

    chrome = find_chrome()
    try:
        subprocess.run(
            [
                chrome,
                "--headless=new",
                "--disable-gpu",
                "--no-sandbox",
                f"--print-to-pdf={output_file.resolve()}",
                "--print-to-pdf-no-header",
                "--no-margins",
                "--virtual-time-budget=20000",
                "--run-all-compositor-stages-before-draw",
                f"file://{temp_html.resolve()}",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        sys.exit(f"ERROR: Chrome failed to render the PDF:\n{exc.stderr}")
    finally:
        temp_html.unlink(missing_ok=True)


def main() -> None:
    args = sys.argv[1:]
    input_file = Path(args[0]) if args else Path("channel_export.json")
    output_file = Path(args[1]) if len(args) > 1 else input_file.parent / "index.pdf"

    if not input_file.exists():
        sys.exit(f"ERROR: input file not found: {input_file}")

    print(f"Reading {input_file} …")
    with input_file.open(encoding="utf-8") as f:
        data = json.load(f)

    print("Rendering PDF (threads expanded, search box hidden) …")
    render_pdf(data, output_file)

    size_kb = output_file.stat().st_size // 1024
    print(f"Done. Written to {output_file} ({size_kb} KB)")


if __name__ == "__main__":
    main()
