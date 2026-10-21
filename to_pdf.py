#!/usr/bin/env python3
"""
Convert a Mattermost channel export JSON (produced by export.py) to a
single-file PDF archive, using the same rendering as to_html.py.

Useful for uploading to platforms that accept PDF but reject .html files
(e.g. some hardened Nextcloud instances) or don't have a JSON viewer.

Unlike the interactive HTML page, thread replies are always expanded
(there is no click interaction in a static PDF), and the live search box
is hidden, since it cannot work without JavaScript-driven filtering.

Usage:
    uv run to_pdf.py [input.json] [output.pdf]

Defaults:
    input  : channel_export.json
    output : index.pdf, written next to the input JSON (so it can embed
             any images export.py downloaded into that same directory)

The input must be a .json file and the output a .pdf file.

Requires a Chrome/Chromium binary on PATH (google-chrome, google-chrome-stable,
chromium or chromium-browser), used headless to render the page and print it to PDF.
"""

import copy
import shutil
import subprocess
import sys
import json
from pathlib import Path

from PIL import Image, ImageOps

from to_html import build_html

# CSS injected on top of to_html.py's stylesheet: always show thread
# replies (no click interaction in a static PDF) and hide the live
# search box (non-functional without further interaction).
PDF_OVERRIDES_CSS = """
.thread-replies.hidden { display: block !important; }
.thread-toggle { display: none !important; }
.search-wrap { display: none !important; }
"""

# Chrome embeds images in the PDF at their original resolution: a channel
# with many full-size phone photos (10+ MB each) makes it run out of memory
# and crash or hang. Images are therefore downscaled for the PDF only, into
# a hidden directory next to the originals (which are left untouched).
PDF_IMAGE_MAX_SIZE = 1600  # px, longest side
PDF_IMAGE_JPEG_QUALITY = 85
PDF_IMAGES_DIR = ".pdf-images"

CHROME_TIMEOUT = 30 * 60  # seconds; Chrome can hang instead of crashing on oversized pages

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


def downscale_image(source: Path, dest_dir: Path) -> Path:
    """Write a reduced copy of `source` into `dest_dir` (cached), and return its path.

    Animated GIFs keep only their first frame, which is all a PDF can show.
    Images with transparency stay PNG, everything else becomes JPEG.
    """
    with Image.open(source) as img:
        img = ImageOps.exif_transpose(img)  # phone photos rely on EXIF for orientation
        has_alpha = img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info)
        suffix = ".png" if has_alpha else ".jpg"
        dest = dest_dir / (source.stem + suffix)
        if dest.exists() and dest.stat().st_mtime >= source.stat().st_mtime:
            return dest
        img.thumbnail((PDF_IMAGE_MAX_SIZE, PDF_IMAGE_MAX_SIZE))
        if has_alpha:
            img.convert("RGBA").save(dest, optimize=True)
        else:
            img.convert("RGB").save(dest, quality=PDF_IMAGE_JPEG_QUALITY, optimize=True)
    return dest


def with_downscaled_images(data: dict, base_dir: Path) -> dict:
    """Return a copy of `data` whose images' local_path point to downscaled copies."""
    data = copy.deepcopy(data)
    dest_dir = base_dir / PDF_IMAGES_DIR
    dest_dir.mkdir(exist_ok=True)

    def posts(messages: list[dict]):
        for message in messages:
            yield message
            yield from message.get("thread", [])

    for post in posts(data.get("messages", [])):
        for f in post.get("files", []):
            local_path = f.get("local_path")
            if not local_path or not (base_dir / local_path).exists():
                continue
            try:
                f["local_path"] = downscale_image(base_dir / local_path, dest_dir).relative_to(base_dir).as_posix()
            except Exception as exc:  # Pillow raises various types (OSError, SyntaxError …)
                # Unreadable/corrupt image: keep the original reference
                print(f"  Warning: could not downscale {local_path}: {exc}")
    return data


def render_pdf(data: dict, output_file: Path) -> None:
    data = with_downscaled_images(data, output_file.parent)
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
                "--no-pdf-header-footer",  # replaces the above in Chrome's new headless mode
                "--no-margins",
                "--virtual-time-budget=20000",
                "--run-all-compositor-stages-before-draw",
                f"file://{temp_html.resolve()}",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=CHROME_TIMEOUT,
        )
    except subprocess.CalledProcessError as exc:
        sys.exit(f"ERROR: Chrome failed to render the PDF:\n{exc.stderr}")
    except subprocess.TimeoutExpired:
        sys.exit(f"ERROR: Chrome did not render the PDF within {CHROME_TIMEOUT // 60} minutes")
    finally:
        temp_html.unlink(missing_ok=True)


def main() -> None:
    args = sys.argv[1:]
    if len(args) > 2:
        sys.exit(f"ERROR: too many arguments (expected at most 2, got {len(args)}): {args}")

    input_file = Path(args[0]) if args else Path("channel_export.json")
    output_file = Path(args[1]) if len(args) > 1 else input_file.parent / "index.pdf"

    if args and input_file.suffix.lower() != ".json":
        sys.exit(f"ERROR: input file must have a .json extension, got: {input_file}")
    if len(args) > 1 and output_file.suffix.lower() != ".pdf":
        sys.exit(f"ERROR: output file must have a .pdf extension, got: {output_file}")

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
