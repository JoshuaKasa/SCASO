#!/usr/bin/env python3
"""
SCASO Grabber
-----------------
Fetch MuseScore score assets (SVG pages, optional MusicXML & MIDI) from the
viewer’s space.jsonp manifest, then (by default) combine SVGs into one PDF.

Usage:
  python musescore_grabber.py <score_url>
  python musescore_grabber.py <url> -o out_dir
  python musescore_grabber.py <url> --no-pdf
  python musescore_grabber.py <url> --page-range 1-3,5
  python musescore_grabber.py <url> --pdf-engine cairosvg
  python musescore_grabber.py <url> --formats svg,mxl,mid

Notes:
- I do not condone any copyright infringement; this is for educational purposes only.
- MusicXML/MIDI may not exist for every score; we just try POLITELY.
"""

import argparse
import io
import json
import logging
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict
from typing import Optional
from typing import Tuple

import requests
from playwright.sync_api import Request as PWRequest
from playwright.sync_api import sync_playwright

from constants import DEFAULT_MAX_FILENAME
from constants import DEFAULT_RETRIES
from constants import DEFAULT_THROTTLE_MS
from constants import DEFAULT_WAIT_MS
from constants import PDF_ENGINES
from constants import REQUEST_TIMEOUT_SECONDS
from constants import SPACE_TIMEOUT_SECONDS
from constants import SUPPORTED_FORMATS
from constants import USER_AGENT


# ----------------------------- models ---------------------------------


@dataclass(frozen=True)
class Options:
    """
    Immutable CLI options container.

    Attributes:
        url: MuseScore score URL.
        output_dir: Optional output directory.
        no_pdf: Disable SVG->PDF merge.
        pdf_engine: One of PDF_ENGINES.
        formats: Tuple of wanted formats.
        page_range: Optional 1-based page range spec.
        retries: HTTP retries per file.
        throttle_ms: Sleep after each successful GET (ms).
        headful: Show the browser instead of headless.
    """
    url: str
    output_dir: Optional[str]
    no_pdf: bool
    pdf_engine: str
    formats: Tuple[str, ...]
    page_range: Optional[str]
    retries: int
    throttle_ms: int
    headful: bool


# --------------------------- utilities --------------------------------


def safe_print(*args: object) -> None:
    """
    Print text safely even if stdout encoding is limited.

    Args:
        *args: Objects to print, joined by spaces.
    """
    text = " ".join(str(a) for a in args)
    try:
        print(text)
    except UnicodeEncodeError:
        sys.stdout.buffer.write((text + "\n").encode("utf-8", "ignore"))


def notify_user(message: str, category: str = "error") -> None:
    """
    Try to flash via Flask if available; otherwise log an error.

    Args:
        message: Notification text.
        category: Flask flash category (default 'error').
    """
    try:
        from flask import flash  # type: ignore
        flash(message, category)
    except Exception:
        logging.error(message)


def sanitize_filename(name: str, max_len: int = DEFAULT_MAX_FILENAME) -> str:
    """
    Produce a filesystem-safe filename.

    Args:
        name: Raw name.
        max_len: Maximum length.

    Returns:
        Safe filename string.
    """
    cleaned = re.sub(r"[^\w\-. ]+", "_", name.strip())
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:max_len] if cleaned else "score"


def parse_page_range(spec: Optional[str], total_pages: int) -> Tuple[int, ...]:
    """
    Parse a 1-based page range spec into 0-based indices.

    Args:
        spec: "1-3,5,8-10" or None for all pages.
        total_pages: Total number of pages.

    Returns:
        Sorted, unique 0-based page indices (tuple).
    """
    if not spec:
        return tuple(range(total_pages))

    wanted = set()
    parts = [p.strip() for p in spec.split(",") if p.strip()]
    for part in parts:
        if "-" in part:
            left, right = part.split("-", 1)
            try:
                start = max(1, int(left))
                end = min(total_pages, int(right))
            except ValueError:
                continue
            if start <= end:
                # Keep explicit loop to match task "do not remove" filters
                for i in range(start, end + 1):
                    wanted.add(i - 1)
        else:
            try:
                one = int(part)
                if 1 <= one <= total_pages:
                    wanted.add(one - 1)
            except ValueError:
                continue

    indices = sorted(wanted)
    return tuple(indices) if indices else tuple(range(total_pages))


# ----------------------- page + manifest fetch -------------------------


def capture_space_jsonp_and_title(
    score_url: str,
    headless: bool,
    wait_ms: int = DEFAULT_WAIT_MS,
) -> Tuple[Optional[str], str]:
    """
    Open a score page, capture first `space.jsonp` URL, and get a title.

    Args:
        score_url: MuseScore score URL.
        headless: Run browser headless if True.
        wait_ms: Extra wait for network settle.

    Returns:
        (space_jsonp_url or None, title).

    Raises:
        RuntimeError: If navigation fails.
    """
    logging.debug("Launching browser (headless=%s)", headless)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(user_agent=USER_AGENT)
        page = context.new_page()

        space_url: Optional[str] = None

        def on_request(req: PWRequest) -> None:
            """
            Request hook to capture the manifest URL.

            Args:
                req: Playwright Request instance.
            """
            nonlocal space_url
            url = req.url
            if "space.jsonp" in url and space_url is None:
                space_url = url

        context.on("request", on_request)

        try:
            page.goto(score_url, wait_until="networkidle")
            page.wait_for_timeout(wait_ms)
        except Exception as exc:
            browser.close()
            msg = f"Failed to load page: {exc}"
            notify_user(msg)
            raise RuntimeError(msg) from exc

        title = (
            page.locator('meta[property="og:title"]')
            .first.get_attribute("content")
            or page.title()
            or "MuseScore Score"
        )
        title = title.replace(" | MuseScore", "").strip()

        browser.close()
        return space_url, title


def fetch_space(space_url: str) -> Dict[str, object]:
    """
    Fetch and parse the JSON inside the JSONP manifest.

    Args:
        space_url: URL of space.jsonp.

    Returns:
        Parsed JSON dict.

    Raises:
        RuntimeError: On HTTP or JSON parse failure.
    """
    headers = {"User-Agent": USER_AGENT, "Referer": space_url}
    try:
        resp = requests.get(
            space_url,
            headers=headers,
            timeout=SPACE_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        msg = f"Failed to GET space.jsonp: {exc}"
        notify_user(msg)
        raise RuntimeError(msg) from exc

    if resp.status_code != 200:
        msg = f"space.jsonp HTTP {resp.status_code}"
        notify_user(msg)
        raise RuntimeError(msg)

    json_text = re.sub(r"^[^(]+\(|\);?$", "", resp.text)
    try:
        data: Dict[str, object] = json.loads(json_text)
    except json.JSONDecodeError as exc:
        msg = "Could not parse space.jsonp JSON."
        notify_user(msg)
        raise RuntimeError(msg) from exc

    return data


def derive_base_and_pages(
    space_url: str,
    space_data: Dict[str, object],
) -> Tuple[str, int]:
    """
    Derive base URL for assets and total page count.

    Args:
        space_url: URL of the JSONP manifest.
        space_data: Parsed JSON dict.

    Returns:
        (base_url_with_trailing_slash, total_pages).

    Raises:
        RuntimeError: If pages cannot be extracted.
    """
    try:
        space_list = space_data.get("space", [])  # type: ignore[assignment]
        pages = [int(item["page"]) for item in space_list]  # type: ignore
        total_pages = max(pages) + 1 if pages else 0
    except Exception as exc:
        msg = f"Could not extract pages: {exc}"
        notify_user(msg)
        raise RuntimeError(msg) from exc

    base = space_url.rsplit("/", 1)[0] + "/"
    return base, total_pages


# --------------------------- downloading -------------------------------


def _http_get(
    url: str,
    headers: Dict[str, str],
    retries: int,
    throttle_ms: int,
) -> Optional[bytes]:
    """
    GET a URL with retries and optional throttle.

    Args:
        url: Target URL.
        headers: HTTP headers.
        retries: Retry attempts on failure.
        throttle_ms: Sleep after success (ms).

    Returns:
        Content bytes or None if failed.
    """
    for attempt in range(retries + 1):
        try:
            resp = requests.get(
                url,
                headers=headers,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            logging.warning("GET error: %s -> %s", url, exc)
            if attempt < retries:
                time.sleep(0.75 * (attempt + 1))
            continue

        if resp.status_code == 200 and resp.content:
            if throttle_ms:
                time.sleep(throttle_ms / 1000.0)
            return resp.content

        logging.warning("HTTP %s for %s", resp.status_code, url)
        if attempt < retries:
            time.sleep(0.75 * (attempt + 1))

    return None


def download_assets(
    options: Options,
    base: str,
    out_dir: Path,
    total_pages: int,
    page_indices: Tuple[int, ...],
    song_stem: str,
) -> Tuple[Tuple[Path, ...], Optional[Path], Optional[Path]]:
    """
    Download SVG pages and optional MusicXML/MIDI.

    Args:
        options: Immutable Options.
        base: Base URL for assets.
        out_dir: Output directory path.
        total_pages: Reported page count.
        page_indices: 0-based page indices to fetch.

    Returns:
        (svg_paths, mxl_path, mid_path)
    """
    headers = {"User-Agent": USER_AGENT, "Referer": base}
    want_svg = "svg" in options.formats
    want_mxl = "mxl" in options.formats
    want_mid = "mid" in options.formats or "midi" in options.formats

    svg_paths_list: list[Path] = []

    if want_svg and total_pages > 0:
        safe_print(f"[+] Downloading {len(page_indices)} SVG page(s)")
        for idx in page_indices:
            page_url = f"{base}score_{idx}.svg"
            out_file = out_dir / f"{song_stem} - page - {idx + 1:02d}.svg"

            if out_file.exists() and out_file.stat().st_size > 0:
                safe_print(f"  [skip] {out_file.name} (exists)")
                svg_paths_list.append(out_file)
                continue

            data = _http_get(
                page_url,
                headers,
                options.retries,
                options.throttle_ms,
            )
            if data:
                with open(out_file, "wb") as f:
                    f.write(data)
                safe_print(f"  [+] Saved {out_file.name}")
                svg_paths_list.append(out_file)
            else:
                safe_print(f"  [x] Failed page {idx + 1}")
    else:
        safe_print("[i] Skipping SVG pages")

    mxl_path: Optional[Path] = None
    if want_mxl:
        mxl_url = f"{base}score.mxl"
        mxl_out = out_dir / f"{song_stem}.mxl"
        data = _http_get(
            mxl_url,
            headers,
            options.retries,
            options.throttle_ms,
        )
        if data:
            with open(mxl_out, "wb") as f:
                f.write(data)
            mxl_path = mxl_out
            safe_print(f"[+] Saved MusicXML: {mxl_out.name}")
        else:
            safe_print("[i] MusicXML not available (or blocked)")

    mid_path: Optional[Path] = None
    if want_mid:
        mid_url = f"{base}score.mid"
        mid_out = out_dir / f"{song_stem}.mid"
        data = _http_get(
            mid_url,
            headers,
            options.retries,
            options.throttle_ms,
        )
        if data:
            with open(mid_out, "wb") as f:
                f.write(data)
            mid_path = mid_out
            safe_print(f"[+] Saved MIDI: {mid_out.name}")
        else:
            safe_print("[i] MIDI not available (or blocked)")

    return tuple(svg_paths_list), mxl_path, mid_path


# --------------------------- PDF combining -----------------------------


def combine_svgs_to_pdf(
    svg_paths: Tuple[Path, ...],
    pdf_path: Path,
    engine: str = "cairosvg",
) -> bool:
    """
    Combine SVG pages into a single PDF.

    Args:
        svg_paths: Ordered tuple of SVG files.
        pdf_path: Output PDF file path.
        engine: 'cairosvg' or 'none'.

    Returns:
        True if a PDF was written; False otherwise.
    """
    if not svg_paths:
        safe_print("[i] No SVGs to combine")
        return False

    if engine == "none":
        safe_print("[i] PDF combine disabled (engine=none)")
        return False

    if engine != "cairosvg":
        safe_print(f"[x] Unknown pdf engine: {engine}")
        return False

    try:
        # Lazy optional deps; no module-level globals.
        import cairosvg  # type: ignore
        from pypdf import PdfReader  # type: ignore
        from pypdf import PdfWriter  # type: ignore
    except Exception as exc:
        safe_print(
            "[x] PDF combine requires 'cairosvg' and 'pypdf'. "
            "Install: pip install cairosvg pypdf"
        )
        logging.error("Optional dependency import failed: %s", exc)
        return False

    writer = PdfWriter()
    for svg_file in svg_paths:
        try:
            svg_bytes = svg_file.read_bytes()
            pdf_bytes = cairosvg.svg2pdf(bytestring=svg_bytes)
            reader = PdfReader(io.BytesIO(pdf_bytes)) # type: ignore
            for page in reader.pages:
                writer.add_page(page)
        except Exception as exc:
            logging.error("SVG->PDF failed for %s: %s", svg_file.name, exc)
            safe_print(f"  [x] Failed to convert {svg_file.name}")
            continue

    with open(pdf_path, "wb") as f:
        writer.write(f)

    safe_print(f"[✅] PDF saved: {pdf_path.name}")
    return True


# ------------------------------- run -----------------------------------


def run(options: Options) -> int:
    """
    Orchestrate the full grab flow.

    Args:
        options: Immutable Options.

    Returns:
        Exit code (0 success, 1 failure).
    """
    safe_print(f"[•] Opening score: {options.url}")

    try:
        space_url, title = capture_space_jsonp_and_title(
            options.url,
            headless=not options.headful,
            wait_ms=DEFAULT_WAIT_MS,
        )
    except RuntimeError as exc:
        safe_print(f"[x] {exc}")
        return 1

    if not space_url:
        safe_print(
            "[x] Could not capture space.jsonp — may be private/blocked."
        )
        return 1

    safe_print(f"[+] space.jsonp: {space_url}")
    safe_print(f"[+] Title: {title}")

    try:
        space_data = fetch_space(space_url)
        base, total_pages = derive_base_and_pages(space_url, space_data)
    except RuntimeError as exc:
        safe_print(f"[x] {exc}")
        return 1

    safe_print(f"[+] Base: {base}")
    safe_print(f"[+] Total pages reported: {total_pages}")

    score_id = options.url.rstrip("/").split("/")[-1]
    base_name = sanitize_filename(f"{title} - {score_id}")
    song_stem = sanitize_filename(title)

    out_dir = (
        Path(options.output_dir)
        if options.output_dir
        else Path(f"../scores/musescore_{base_name}")
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_print(f"[+] Output dir: {out_dir.resolve()}")

    page_indices = parse_page_range(options.page_range, total_pages)
    if page_indices and (max(page_indices) >= total_pages):
        safe_print("[!] Some requested pages exceed total; trimming")
        page_indices = tuple(i for i in page_indices if i < total_pages)

    svg_paths, mxl_path, mid_path = download_assets(
        options=options,
        base=base,
        out_dir=out_dir,
        total_pages=total_pages,
        page_indices=page_indices,
        song_stem=base_name,
    )

    if (not options.no_pdf) and ("svg" in options.formats):
        pdf_path = out_dir / f"{base_name}.pdf"
        combine_svgs_to_pdf(svg_paths, pdf_path, engine=options.pdf_engine)
    else:
        safe_print("[i] PDF combine skipped")

    safe_print("\n--- Summary ---")
    safe_print(f"SVG pages: {len(svg_paths)}")
    safe_print(
        f"MusicXML:  {'yes -> ' + mxl_path.name if mxl_path else 'no'}"
    )
    safe_print(f"MIDI:      {'yes -> ' + mid_path.name if mid_path else 'no'}")
    safe_print(f"Folder:    {out_dir.resolve()}")

    return 0


# ------------------------------- cli -----------------------------------


def _build_parser() -> argparse.ArgumentParser:
    """
    Build the CLI argument parser.

    Returns:
        Configured ArgumentParser instance.
    """
    ap = argparse.ArgumentParser(
        description=(
            "MuseScore Grabber (SVG pages, optional MusicXML & MIDI) "
            "with auto-PDF merge"
        )
    )
    ap.add_argument("url", help="MuseScore score URL")
    ap.add_argument(
        "-o",
        "--output",
        help="Output directory (default: musescore_<Title - ID>)",
    )
    ap.add_argument(
        "--no-pdf",
        action="store_true",
        help="Disable SVG->PDF merge (default: enabled)",
    )
    ap.add_argument(
        "--pdf-engine",
        default="cairosvg",
        choices=list(PDF_ENGINES),
        help="PDF engine to use (default: cairosvg)",
    )
    ap.add_argument(
        "--formats",
        default="svg,mxl,mid",
        help="Comma separated formats (svg,mxl,mid|midi)",
    )
    ap.add_argument(
        "--page-range",
        help="Pages to fetch (1-based), e.g. '1-3,5'. Default: all",
    )
    ap.add_argument(
        "--retries",
        type=int,
        default=DEFAULT_RETRIES,
        help=f"HTTP retries per file (default: {DEFAULT_RETRIES})",
    )
    ap.add_argument(
        "--throttle",
        type=int,
        default=DEFAULT_THROTTLE_MS,
        help=(
            "ms sleep after each successful GET "
            f"(default: {DEFAULT_THROTTLE_MS}ms)"
        ),
    )
    ap.add_argument(
        "--headful",
        action="store_true",
        help="Show the browser (debug) instead of headless",
    )
    return ap


def main() -> None:
    """
    CLI entrypoint. Parse args, build Options, run, exit with code.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    parser = _build_parser()
    args = parser.parse_args()

    fmts = tuple(
        f.strip().lower()
        for f in args.formats.split(",")
        if f.strip()
    )
    for fmt in fmts:
        if fmt not in SUPPORTED_FORMATS:
            parser.error(f"Unsupported format: {fmt}")

    opts = Options(
        url=args.url,
        output_dir=args.output,
        no_pdf=args.no_pdf,
        pdf_engine=args.pdf_engine,
        formats=fmts,
        page_range=args.page_range,
        retries=args.retries,
        throttle_ms=args.throttle,
        headful=args.headful,
    )

    code = run(opts)
    sys.exit(code)


if __name__ == "__main__":
    main()
