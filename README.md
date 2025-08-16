# SCASO Grabber
<p align="center">
  <img src="logo.png" alt="SCASO Grabber" width="400"/>
</p>

<p align="center">
  <a href="https://github.com/yourname/SCASO/blob/main/LICENSE">
    <img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License">
  </a>
  <img src="https://img.shields.io/badge/python-3.9%2B-yellow.svg" alt="Python">
  <img src="https://img.shields.io/badge/status-active-success.svg" alt="Status">
    <img src="https://img.shields.io/badge/version-1.0.0-orange.svg" alt="Version">
</p>

Fetch MuseScore score assets from the viewer manifest (SVG pages; optional
**MusicXML** and **MIDI**) and (optionally) merge SVGs into a single PDF.

> ⚠️ I DO NOT condone or support the use of this tool for piracy or any other illegal activities. Use responsibly and respect copyright laws.

## 📑 Table of Contents
- [Features](#features)
- [Install](#install)
- [Usage](#usage)
- [Common Examples](#common-examples)
- [Tips](#tips)
- [Legal](#legal)

## ✨ Features
- 🎼 Grabs `svg`, `mxl`, `mid` (pick what you want)
- 📄 Page filtering for SVGs (`--page-range 1-3,5`)
- 📝 Optional SVG→PDF merge (via `cairosvg` + `pypdf`)
- 🕵️ Headless Playwright capture of `space.jsonp`

## 🔮 Install
```bash
python -m venv .venv && . .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install
```

## ⚙️ Usage
```bash
python musescore_grabber.py <musescore_url> [options]
```
<details><summary>Full CLI options</summary>

```bash
scaso.py [-h] [-o OUTPUT] [--no-pdf] [--pdf-engine {cairosvg,none}] [--formats FORMATS] [--page-range PAGE_RANGE] [--retries RETRIES] [--throttle THROTTLE] [--headful] url

MuseScore Grabber (SVG pages, optional MusicXML & MIDI) with auto-PDF merge

positional arguments:
  url                   MuseScore score URL

options:
  -h, --help            show this help message and exit
  -o OUTPUT, --output OUTPUT
                        Output directory (default: musescore_<Title - ID>)
  --no-pdf              Disable SVG->PDF merge (default: enabled)
  --pdf-engine {cairosvg,none}
                        PDF engine to use (default: cairosvg)
  --formats FORMATS     Comma separated formats (svg,mxl,mid|midi)
  --page-range PAGE_RANGE
                        Pages to fetch (1-based), e.g. '1-3,5'. Default: all
  --retries RETRIES     HTTP retries per file (default: 2)
  --throttle THROTTLE   ms sleep after each successful GET (default: 75ms)
  --headful             Show the browser (debug) instead of headless
```
</details>

## 📜 Common Examples
```bash
# only MusicXML + MIDI (whole score)
python musescore_grabber.py <url> --no-pdf --formats mxl,mid

# only pages 1–3 as SVGs (no pdf merge)
python musescore_grabber.py <url> --formats svg --page-range 1-3 --no-pdf

# pages 1–3 + merge into a PDF
python musescore_grabber.py <url> --formats svg --page-range 1-3

# everything (SVG + MXL + MID), with page filter for SVGs
python musescore_grabber.py <url> --formats svg,mxl,mid --page-range 2-6
```

## ❗ Tips

- Page ranges are 1-based.
- If `space.jsonp` doesn't show up, the score may be private/blocked.
- Use `--headful` to debug Playwright network traffic.
- If playwright fails to launch, ensure you have the necessary browser binaries installed:
  ```bash
  python -m playwright install
  ```
    if still flaky, try running with `--headful` to see the browser UI.
- Some scores may be private/blocked, if we can't fetch `space.jsonp`, we can't download the score.

## 🖼️ Demo
![demo](screenshot.png)