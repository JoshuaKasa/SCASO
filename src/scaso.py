#!/usr/bin/env python3
"""
SCASO Grabber
-----------------
    Fetch MuseScore score assets (SVG pages, optional MusicXML, MIDI, and MSCZ)
    from the
viewer’s space.jsonp manifest, then (by default) combine SVGs into one PDF.

Usage:
  python musescore_grabber.py <score_url>
  python musescore_grabber.py <url> -o out_dir
  python musescore_grabber.py <url> --no-pdf
  python musescore_grabber.py <url> --page-range 1-3,5
  python musescore_grabber.py <url> --pdf-engine cairosvg
  python musescore_grabber.py <url> --formats svg,mxl,mid,mscz

Notes:
- I do not condone any copyright infringement; this is for educational purposes only.
- MusicXML/MIDI/MuseScore may not exist for every score; we just try POLITELY.
"""

import argparse
import hashlib
import io
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import webbrowser
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

MUSESCORE_DOWNLOAD_URL = "https://musescore.org/en/download"


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


def resolve_output_dir(output_dir: Optional[str], base_name: str) -> Path:
    """
    Resolve final output directory path.

    Rules:
    - No custom output: use default `../scores/musescore_<Title - ID>`.
    - Existing directory passed via `-o`: treat it as parent and append
      `musescore_<Title - ID>`.
    - Non-existing path via `-o`: use it as exact output folder.
    """
    default_dir = Path(f"../scores/musescore_{base_name}")
    if not output_dir:
        return default_dir

    raw_output = output_dir.strip()
    if not raw_output:
        return default_dir

    custom = Path(raw_output)
    if custom.exists() and custom.is_dir():
        return custom / f"musescore_{base_name}"
    if raw_output.endswith(("/", "\\")):
        return custom / f"musescore_{base_name}"
    return custom


# ----------------------- page + manifest fetch -------------------------


def capture_space_jsonp_and_title(
    score_url: str,
    headless: bool,
    wait_ms: int = DEFAULT_WAIT_MS,
) -> Tuple[Optional[str], str, Optional[str], Optional[str], Dict[int, bytes]]:
    """
    Open a score page, capture first `space.jsonp` URL, and get a title.

    Args:
        score_url: MuseScore score URL.
        headless: Run browser headless if True.
        wait_ms: Extra wait for network settle.

    Returns:
        (
            space_jsonp_url or None,
            title,
            space_json_text if captured from browser response,
            first_svg_template_url if observed,
            map of preloaded SVG bytes by 0-based page index,
        ).

    Raises:
        RuntimeError: If navigation fails.
    """
    logging.debug("Launching browser (headless=%s)", headless)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(user_agent=USER_AGENT)
        page = context.new_page()

        space_url: Optional[str] = None
        space_json_text: Optional[str] = None
        first_svg_template_url: Optional[str] = None
        preloaded_svgs: Dict[int, bytes] = {}

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

        def on_response(resp: object) -> None:
            """
            Response hook to capture manifest body and any visible score SVGs.

            Args:
                resp: Playwright Response instance.
            """
            nonlocal space_url
            nonlocal space_json_text
            nonlocal first_svg_template_url

            try:
                url = resp.url  # type: ignore[attr-defined]
                status = int(resp.status)  # type: ignore[attr-defined]
            except Exception:
                return

            if "space.jsonp" in url and space_url is None:
                space_url = url

            if (
                "space.jsonp" in url
                and status == 200
                and space_json_text is None
            ):
                try:
                    space_json_text = resp.text()  # type: ignore[attr-defined]
                except Exception:
                    pass

            svg_match = re.search(r"/score_(\d+)\.svg(?:\?.*)?$", url)
            if svg_match and status == 200:
                page_idx = int(svg_match.group(1))
                if first_svg_template_url is None:
                    first_svg_template_url = url
                if page_idx not in preloaded_svgs:
                    try:
                        svg_bytes = resp.body()  # type: ignore[attr-defined]
                        if svg_bytes:
                            preloaded_svgs[page_idx] = svg_bytes
                    except Exception:
                        pass

        context.on("request", on_request)
        context.on("response", on_response)

        try:
            page.goto(score_url, wait_until="networkidle")
            page.wait_for_timeout(wait_ms)
            # Trigger lazy SVG requests when possible.
            for _ in range(10):
                page.mouse.wheel(0, 2400)
                page.wait_for_timeout(350)
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
        return (
            space_url,
            title,
            space_json_text,
            first_svg_template_url,
            preloaded_svgs,
        )


def _parse_space_json_text(raw_text: str) -> Dict[str, object]:
    """
    Parse `space.jsonp` payload (supports both JSONP and plain JSON).

    Args:
        raw_text: Raw response body.

    Returns:
        Parsed JSON object.

    Raises:
        RuntimeError: If JSON cannot be parsed.
    """
    json_text = re.sub(r"^[^(]+\(|\);?$", "", raw_text.strip())
    try:
        return json.loads(json_text)
    except json.JSONDecodeError as exc:
        msg = "Could not parse space.jsonp JSON."
        notify_user(msg)
        raise RuntimeError(msg) from exc


def fetch_space(
    space_url: str,
    preloaded_space_text: Optional[str] = None,
) -> Dict[str, object]:
    """
    Fetch and parse the JSON inside the JSONP manifest.

    Args:
        space_url: URL of space.jsonp.
        preloaded_space_text: Optional manifest text captured by browser.

    Returns:
        Parsed JSON dict.

    Raises:
        RuntimeError: On HTTP or JSON parse failure.
    """
    if preloaded_space_text is not None:
        return _parse_space_json_text(preloaded_space_text)

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

    return _parse_space_json_text(resp.text)


def extract_total_pages(space_data: Dict[str, object]) -> int:
    """
    Extract total page count from parsed space payload.

    Supports historical 0-based and current 1-based page numbering.
    """
    try:
        space_list = space_data.get("space", [])  # type: ignore[assignment]
        pages = [int(item["page"]) for item in space_list]  # type: ignore
    except Exception as exc:
        msg = f"Could not extract pages: {exc}"
        notify_user(msg)
        raise RuntimeError(msg) from exc

    if not pages:
        return 0

    min_page = min(pages)
    max_page = max(pages)
    if min_page <= 0:
        return max_page + 1
    return max_page


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
    total_pages = extract_total_pages(space_data)

    base = space_url.rsplit("/", 1)[0] + "/"
    return base, total_pages


# --------------------------- downloading -------------------------------


def _split_svg_template(
    svg_template_url: Optional[str],
) -> Tuple[Optional[str], str]:
    """
    Split a captured score SVG URL into prefix and query.

    Example:
      https://.../score_0.svg?no-cache=123
      -> ("https://.../", "?no-cache=123")
    """
    if not svg_template_url:
        return None, ""

    match = re.search(
        r"^(.*?/)(?:score_\d+\.svg)(\?.*)?$",
        svg_template_url,
    )
    if not match:
        return None, ""

    prefix = match.group(1)
    query = match.group(2) or ""
    return prefix, query


def _looks_like_svg(data: bytes) -> bool:
    """
    Heuristic check for SVG payload.
    """
    head = data.lstrip()[:200].lower()
    return b"<svg" in head


def _looks_like_html(data: bytes) -> bool:
    """
    Heuristic check for HTML/challenge payload.
    """
    head = data.lstrip()[:240].lower()
    return head.startswith(b"<!doctype html") or head.startswith(b"<html")


def _browser_get_binary(
    context: object,
    referer_url: str,
    target_url: str,
) -> Optional[bytes]:
    """
    Use a browser page navigation to fetch binary content.

    This avoids direct client requests that may be blocked by anti-bot layers.
    """
    page = context.new_page()  # type: ignore[attr-defined]
    try:
        response = page.goto(
            target_url,
            wait_until="domcontentloaded",
            referer=referer_url,
            timeout=REQUEST_TIMEOUT_SECONDS * 1000,
        )
        if not response or response.status != 200:
            return None

        body = response.body()
        if not body or _looks_like_html(body):
            return None

        return body
    except Exception:
        return None
    finally:
        try:
            page.close()
        except Exception:
            pass


def prefetch_assets_with_browser(
    score_url: str,
    headless: bool,
    page_indices: Tuple[int, ...],
    want_mxl: bool,
    want_mid: bool,
    svg_template_url: Optional[str],
) -> Tuple[Dict[int, bytes], Optional[bytes], Optional[bytes], Optional[str]]:
    """
    Best-effort browser-side asset fetch for URLs blocked in plain HTTP clients.

    Returns:
        (
            svg page bytes by 0-based index,
            mxl bytes or None,
            mid bytes or None,
            discovered/updated svg template url,
        )
    """
    need_svg = bool(page_indices)
    if not (need_svg or want_mxl or want_mid):
        return {}, None, None, svg_template_url

    prefetched_svgs: Dict[int, bytes] = {}
    mxl_data: Optional[bytes] = None
    mid_data: Optional[bytes] = None
    discovered_template = svg_template_url

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(user_agent=USER_AGENT)
        page = context.new_page()

        def on_response(resp: object) -> None:
            nonlocal discovered_template
            try:
                url = resp.url  # type: ignore[attr-defined]
                status = int(resp.status)  # type: ignore[attr-defined]
            except Exception:
                return

            svg_match = re.search(r"/score_(\d+)\.svg(?:\?.*)?$", url)
            if svg_match and status == 200:
                if discovered_template is None:
                    discovered_template = url
                idx = int(svg_match.group(1))
                if idx in prefetched_svgs:
                    return
                try:
                    data = resp.body()  # type: ignore[attr-defined]
                    if data and _looks_like_svg(data):
                        prefetched_svgs[idx] = data
                except Exception:
                    pass

        context.on("response", on_response)

        try:
            page.goto(score_url, wait_until="networkidle")
            page.wait_for_timeout(DEFAULT_WAIT_MS)
            for _ in range(8):
                page.mouse.wheel(0, 2400)
                page.wait_for_timeout(300)
        except Exception as exc:
            logging.warning("Browser prefetch warmup failed: %s", exc)

        prefix, query = _split_svg_template(discovered_template)

        if need_svg and prefix:
            for idx in page_indices:
                if idx in prefetched_svgs:
                    continue
                target = f"{prefix}score_{idx}.svg{query}"
                data = _browser_get_binary(context, score_url, target)
                if data and _looks_like_svg(data):
                    prefetched_svgs[idx] = data

        if prefix and want_mxl:
            for target in (
                f"{prefix}score.mxl{query}",
                f"{prefix}score.mxl",
            ):
                data = _browser_get_binary(context, score_url, target)
                if data:
                    mxl_data = data
                    break

        if prefix and want_mid:
            for target in (
                f"{prefix}score.mid{query}",
                f"{prefix}score.mid",
            ):
                data = _browser_get_binary(context, score_url, target)
                if data:
                    mid_data = data
                    break

        browser.close()

    return prefetched_svgs, mxl_data, mid_data, discovered_template


def _jmuse_auth_token(
    score_id: str,
    asset_type: str,
    index: int = 0,
) -> str:
    """
    Build JMuse authorization token used by MuseScore web player.
    """
    raw = f"{score_id}{asset_type}{index}uv"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:4]


def fetch_jmuse_signed_url(
    score_url: str,
    score_id: str,
    asset_type: str,
    headless: bool,
    index: int = 0,
) -> Optional[str]:
    """
    Ask JMuse API for a signed download URL by rewriting the first player call.

    Args:
        score_url: MuseScore score page URL.
        score_id: Numeric score id as string.
        asset_type: JMuse type ('midi', 'mxl', 'mp3', 'img', ...).
        headless: Whether to run browser headless.
        index: JMuse index parameter (page index for 'img').

    Returns:
        Signed URL if available, else None.
    """
    urls = collect_jmuse_signed_urls(
        score_url=score_url,
        score_id=score_id,
        asset_type=asset_type,
        indices=(index,),
        headless=headless,
    )
    return urls.get(index)


def collect_jmuse_signed_urls(
    score_url: str,
    score_id: str,
    asset_type: str,
    indices: Tuple[int, ...],
    headless: bool,
) -> Dict[int, str]:
    """
    Collect multiple JMuse signed URLs by rewriting player MP3 bootstrap calls.

    Returns:
        Mapping index -> signed URL.
    """
    requested = tuple(dict.fromkeys(indices))
    if not requested:
        return {}

    signed_urls: Dict[int, str] = {}
    wait_ms = max(1200, DEFAULT_WAIT_MS // 2)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)

        for idx in requested:
            rewritten = False
            captured_url: Optional[str] = None
            context = browser.new_context(user_agent=USER_AGENT)

            def on_route(route: object, request: object) -> None:
                """
                Rewrite the first JMuse bootstrap call for this index.
                """
                nonlocal rewritten
                try:
                    req_url = request.url  # type: ignore[attr-defined]
                except Exception:
                    route.continue_()  # type: ignore[attr-defined]
                    return

                if not rewritten and "/api/jmuse" in req_url and "type=mp3" in req_url:
                    rewritten = True
                    new_url = req_url.replace("type=mp3", f"type={asset_type}", 1)
                    new_url = re.sub(
                        r"([?&]index=)\d+",
                        rf"\g<1>{idx}",
                        new_url,
                        count=1,
                    )
                    try:
                        headers = dict(request.headers)  # type: ignore[attr-defined]
                    except Exception:
                        headers = {}
                    headers["authorization"] = _jmuse_auth_token(
                        score_id,
                        asset_type,
                        index=idx,
                    )
                    route.continue_(url=new_url, headers=headers)  # type: ignore[attr-defined]
                    return

                route.continue_()  # type: ignore[attr-defined]

            def on_response(resp: object) -> None:
                """
                Capture signed URL from rewritten JMuse response.
                """
                nonlocal captured_url
                try:
                    resp_url = resp.url  # type: ignore[attr-defined]
                    status = int(resp.status)  # type: ignore[attr-defined]
                except Exception:
                    return

                if status != 200 or "/api/jmuse" not in resp_url:
                    return
                if f"type={asset_type}" not in resp_url:
                    return

                idx_match = re.search(r"[?&]index=(\d+)", resp_url)
                if not idx_match or int(idx_match.group(1)) != idx:
                    return

                try:
                    payload = json.loads(resp.text())  # type: ignore[attr-defined]
                except Exception:
                    return

                info = payload.get("info", {}) if isinstance(payload, dict) else {}
                signed_url = info.get("url") if isinstance(info, dict) else None
                if isinstance(signed_url, str) and signed_url.strip():
                    captured_url = signed_url

            context.route("**/*", on_route)
            context.on("response", on_response)
            page = context.new_page()
            try:
                page.goto(score_url, wait_until="networkidle")
                page.wait_for_timeout(wait_ms)
            except Exception as exc:
                logging.debug(
                    "JMuse signed URL probe failed for %s[%s]: %s",
                    asset_type,
                    idx,
                    exc,
                )
            finally:
                try:
                    page.close()
                except Exception:
                    pass
                context.close()

            if captured_url:
                signed_urls[idx] = captured_url

        browser.close()

    return signed_urls


def find_musescore_cli() -> Optional[str]:
    """
    Locate a MuseScore CLI binary.
    """
    cli_candidates: list[str] = ["musescore", "mscore", "MuseScore4", "MuseScore3"]
    if sys.platform.startswith("win"):
        roots = [
            os.environ.get("ProgramFiles"),
            os.environ.get("ProgramFiles(x86)"),
            r"C:\Program Files",
            r"C:\Program Files (x86)",
        ]
        rel_paths = (
            r"MuseScore 4\bin\MuseScore4.exe",
            r"MuseScore 4\bin\mscore.exe",
            r"MuseScore 3\bin\MuseScore3.exe",
            r"MuseScore 3\bin\mscore.exe",
        )
        for root in roots:
            if not root:
                continue
            for rel in rel_paths:
                exe_path = Path(root) / Path(rel)
                if exe_path.exists():
                    cli_candidates.append(str(exe_path))

    for cli in dict.fromkeys(cli_candidates):
        resolved = shutil.which(cli)
        if resolved:
            return resolved
        if any(sep in cli for sep in ("/", "\\", ":")) and Path(cli).exists():
            return cli

    return None


def find_winget_cli() -> Optional[str]:
    """
    Locate a winget executable when available.
    """
    candidates = [shutil.which("winget")]

    local_appdata = os.environ.get("LOCALAPPDATA")
    user_profile = os.environ.get("USERPROFILE")
    user_name = os.environ.get("USER")

    if local_appdata:
        candidates.append(
            str(Path(local_appdata) / "Microsoft" / "WindowsApps" / "winget.exe")
        )
    if user_profile:
        candidates.append(
            str(Path(user_profile) / "AppData" / "Local" / "Microsoft" / "WindowsApps" / "winget.exe")
        )
    if user_name:
        candidates.append(
            f"/mnt/c/Users/{user_name}/AppData/Local/Microsoft/WindowsApps/winget.exe"
        )

    for candidate in candidates:
        if not candidate:
            continue
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
        path = Path(candidate)
        if path.exists():
            return str(path)

    return None


def install_musescore() -> bool:
    """
    Try to install MuseScore, preferring winget on Windows.
    """
    winget = find_winget_cli()
    if winget:
        package_ids = (
            "Musescore.Musescore",
            "MuseScore.MuseScore",
            "Musescore.Musescore.3",
        )
        for package_id in package_ids:
            try:
                proc = subprocess.run(
                    [
                        winget,
                        "install",
                        "-e",
                        "--id",
                        package_id,
                        "--accept-source-agreements",
                        "--accept-package-agreements",
                    ],
                    timeout=20 * 60,
                )
            except Exception as exc:
                logging.debug("winget install failed (%s): %s", package_id, exc)
                continue

            if proc.returncode == 0 and find_musescore_cli():
                return True

    try:
        opened = webbrowser.open(MUSESCORE_DOWNLOAD_URL)
    except Exception as exc:
        logging.debug("Could not open MuseScore download page: %s", exc)
        opened = False

    if opened:
        safe_print(f"[i] Opened MuseScore download page: {MUSESCORE_DOWNLOAD_URL}")
    else:
        safe_print(f"[i] Install MuseScore from: {MUSESCORE_DOWNLOAD_URL}")

    return False


def prompt_install_musescore() -> None:
    """
    Ask the user whether MuseScore should be installed when missing.
    """
    if find_musescore_cli():
        return

    safe_print("[i] MuseScore is not installed. MSCZ generation needs local MuseScore.")
    if not sys.stdin.isatty():
        safe_print(f"[i] Install MuseScore from: {MUSESCORE_DOWNLOAD_URL}")
        return

    try:
        answer = input("Install MuseScore now? [Y/n]: ").strip().lower()
    except EOFError:
        safe_print(f"[i] Install MuseScore from: {MUSESCORE_DOWNLOAD_URL}")
        return

    if answer in ("", "y", "yes"):
        if install_musescore():
            safe_print("[+] MuseScore installation completed")
        else:
            safe_print("[i] MuseScore installation was not completed")


def convert_with_musescore(input_path: Path, output_path: Path) -> bool:
    """
    Convert a score file with MuseScore CLI.
    """
    cli = find_musescore_cli()
    if not cli:
        return False

    try:
        proc = subprocess.run(
            [cli, str(input_path), "-o", str(output_path)],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception as exc:
        logging.debug("MuseScore CLI conversion failed (%s): %s", cli, exc)
        return False

    return (
        proc.returncode == 0
        and output_path.exists()
        and output_path.stat().st_size > 0
    )


def convert_mid_to_mxl(mid_path: Path, mxl_path: Path) -> bool:
    """
    Best-effort MIDI -> MXL conversion fallback.

    Strategy:
    1) Try MuseScore CLI if installed.
    2) Try music21 if available.
    """
    if convert_with_musescore(mid_path, mxl_path):
        return True

    try:
        from music21 import converter  # type: ignore

        score = converter.parse(str(mid_path))
        score.write("mxl", fp=str(mxl_path))
        if mxl_path.exists() and mxl_path.stat().st_size > 0:
            return True
    except Exception as exc:
        logging.debug("MIDI->MXL music21 fallback failed: %s", exc)

    return False


def convert_to_mscz(source_path: Path, mscz_path: Path) -> bool:
    """
    Convert MusicXML or MIDI into a MuseScore .mscz file.
    """
    return convert_with_musescore(source_path, mscz_path)


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

        if resp.status_code == 403:
            logging.debug("HTTP 403 for %s", url)
        else:
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
    preloaded_svgs: Optional[Dict[int, bytes]] = None,
    preloaded_mxl: Optional[bytes] = None,
    preloaded_mid: Optional[bytes] = None,
    svg_template_url: Optional[str] = None,
    jmuse_svg_urls: Optional[Dict[int, str]] = None,
    jmuse_mxl_url: Optional[str] = None,
    jmuse_mid_url: Optional[str] = None,
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

    preloaded_svgs = preloaded_svgs or {}
    jmuse_svg_urls = jmuse_svg_urls or {}
    template_prefix, template_query = _split_svg_template(svg_template_url)

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

            data: Optional[bytes] = None
            if idx in preloaded_svgs:
                data = preloaded_svgs[idx]
            else:
                candidate_urls: list[str] = []
                if idx in jmuse_svg_urls:
                    candidate_urls.append(jmuse_svg_urls[idx])
                if template_prefix:
                    candidate_urls.append(
                        f"{template_prefix}score_{idx}.svg{template_query}"
                    )
                candidate_urls.append(page_url)

                for candidate in dict.fromkeys(candidate_urls):
                    fetched = _http_get(
                        candidate,
                        headers,
                        options.retries,
                        options.throttle_ms,
                    )
                    if fetched and _looks_like_svg(fetched):
                        data = fetched
                        break

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
        mxl_out = out_dir / f"{song_stem}.mxl"
        data = preloaded_mxl
        if data is None:
            mxl_candidates: list[str] = []
            if jmuse_mxl_url:
                mxl_candidates.append(jmuse_mxl_url)
            if template_prefix:
                mxl_candidates.append(
                    f"{template_prefix}score.mxl{template_query}"
                )
            mxl_candidates.append(f"{base}score.mxl")

            for candidate in dict.fromkeys(mxl_candidates):
                data = _http_get(
                    candidate,
                    headers,
                    options.retries,
                    options.throttle_ms,
                )
                if data and not _looks_like_html(data):
                    break
                data = None

        if data:
            with open(mxl_out, "wb") as f:
                f.write(data)
            mxl_path = mxl_out
            safe_print(f"[+] Saved MusicXML: {mxl_out.name}")
        else:
            safe_print("[i] MusicXML not available (or blocked)")

    mid_path: Optional[Path] = None
    if want_mid:
        mid_out = out_dir / f"{song_stem}.mid"
        data = preloaded_mid
        if data is None:
            mid_candidates: list[str] = []
            if jmuse_mid_url:
                mid_candidates.append(jmuse_mid_url)
            if template_prefix:
                mid_candidates.append(
                    f"{template_prefix}score.mid{template_query}"
                )
            mid_candidates.append(f"{base}score.mid")

            for candidate in dict.fromkeys(mid_candidates):
                data = _http_get(
                    candidate,
                    headers,
                    options.retries,
                    options.throttle_ms,
                )
                if data and not _looks_like_html(data):
                    break
                data = None

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
    prompt_install_musescore()
    safe_print(f"[•] Opening score: {options.url}")

    try:
        (
            space_url,
            title,
            preloaded_space_text,
            svg_template_url,
            initially_loaded_svgs,
        ) = capture_space_jsonp_and_title(
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
        space_data = fetch_space(
            space_url,
            preloaded_space_text=preloaded_space_text,
        )
        base, total_pages = derive_base_and_pages(space_url, space_data)
    except RuntimeError as exc:
        safe_print(f"[x] {exc}")
        return 1

    safe_print(f"[+] Base: {base}")
    safe_print(f"[+] Total pages reported: {total_pages}")

    score_id = options.url.rstrip("/").split("/")[-1]
    base_name = sanitize_filename(f"{title} - {score_id}")
    song_stem = sanitize_filename(title)

    out_dir = resolve_output_dir(options.output_dir, base_name)
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_print(f"[+] Output dir: {out_dir.resolve()}")

    page_indices = parse_page_range(options.page_range, total_pages)
    if page_indices and (max(page_indices) >= total_pages):
        safe_print("[!] Some requested pages exceed total; trimming")
        page_indices = tuple(i for i in page_indices if i < total_pages)

    want_mxl = "mxl" in options.formats
    want_mid = "mid" in options.formats or "midi" in options.formats
    want_mscz = "mscz" in options.formats
    browser_svgs, browser_mxl, browser_mid, discovered_template = (
        prefetch_assets_with_browser(
            score_url=options.url,
            headless=not options.headful,
            page_indices=page_indices if "svg" in options.formats else (),
            want_mxl=want_mxl,
            want_mid=want_mid,
            svg_template_url=svg_template_url,
        )
    )

    merged_svgs = dict(initially_loaded_svgs)
    merged_svgs.update(browser_svgs)

    effective_template_url = discovered_template or svg_template_url

    jmuse_svg_urls: Dict[int, str] = {}
    if "svg" in options.formats:
        missing_svg_indices = tuple(
            idx for idx in page_indices if idx not in merged_svgs
        )
        if missing_svg_indices:
            jmuse_svg_urls = collect_jmuse_signed_urls(
                score_url=options.url,
                score_id=score_id,
                asset_type="img",
                indices=missing_svg_indices,
                headless=not options.headful,
            )

    jmuse_mxl_url: Optional[str] = None
    jmuse_mid_url: Optional[str] = None
    if want_mxl and browser_mxl is None:
        jmuse_mxl_url = fetch_jmuse_signed_url(
            score_url=options.url,
            score_id=score_id,
            asset_type="mxl",
            headless=not options.headful,
            index=0,
        )
    if (want_mid or want_mxl) and browser_mid is None:
        jmuse_mid_url = fetch_jmuse_signed_url(
            score_url=options.url,
            score_id=score_id,
            asset_type="midi",
            headless=not options.headful,
            index=0,
        )

    svg_paths, mxl_path, mid_path = download_assets(
        options=options,
        base=base,
        out_dir=out_dir,
        total_pages=total_pages,
        page_indices=page_indices,
        song_stem=base_name,
        preloaded_svgs=merged_svgs,
        preloaded_mxl=browser_mxl,
        preloaded_mid=browser_mid,
        svg_template_url=effective_template_url,
        jmuse_svg_urls=jmuse_svg_urls,
        jmuse_mxl_url=jmuse_mxl_url,
        jmuse_mid_url=jmuse_mid_url,
    )

    if want_mxl and mxl_path is None:
        fallback_mid_path = mid_path
        created_temp_mid = False

        if fallback_mid_path is None and jmuse_mid_url:
            tmp_mid_path = out_dir / f"{base_name}.tmp_for_mxl.mid"
            headers = {"User-Agent": USER_AGENT, "Referer": base}
            tmp_mid_data = _http_get(
                jmuse_mid_url,
                headers,
                options.retries,
                options.throttle_ms,
            )
            if tmp_mid_data and not _looks_like_html(tmp_mid_data):
                with open(tmp_mid_path, "wb") as f:
                    f.write(tmp_mid_data)
                fallback_mid_path = tmp_mid_path
                created_temp_mid = True

        if fallback_mid_path is not None:
            converted_mxl = out_dir / f"{base_name}.mxl"
            if convert_mid_to_mxl(fallback_mid_path, converted_mxl):
                mxl_path = converted_mxl
                safe_print(f"[+] Converted MIDI -> MusicXML: {converted_mxl.name}")
            else:
                safe_print("[i] MusicXML conversion from MIDI unavailable")

        if created_temp_mid and fallback_mid_path and fallback_mid_path.exists():
            try:
                fallback_mid_path.unlink()
            except Exception:
                pass

    mscz_path: Optional[Path] = None
    if want_mscz:
        mscz_source = mxl_path or mid_path
        if mscz_source is not None:
            mscz_out = out_dir / f"{base_name}.mscz"
            if convert_to_mscz(mscz_source, mscz_out):
                mscz_path = mscz_out
                safe_print(
                    f"[+] Generated MuseScore file from "
                    f"{mscz_source.suffix.lower()}: {mscz_out.name}"
                )
            else:
                safe_print("[i] MuseScore file conversion unavailable")
        else:
            safe_print("[i] MuseScore file unavailable (need MusicXML or MIDI)")
	
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
    safe_print(f"MuseScore: {'yes -> ' + mscz_path.name if mscz_path else 'no'}")
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
            "MuseScore Grabber (SVG pages, optional MusicXML, MIDI, and MSCZ) "
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
        default="svg,mxl,mid,mscz",
        help="Comma separated formats (svg,mxl,mid|midi,mscz)",
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
