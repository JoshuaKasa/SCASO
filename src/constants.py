"""
Constants for SCASO Grabber.

Collections are immutable (tuples).
"""

from typing import Tuple

USER_AGENT: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

PDF_ENGINES: Tuple[str, ...] = ("cairosvg", "none")
SUPPORTED_FORMATS: Tuple[str, ...] = ("svg", "mxl", "mid", "midi")

DEFAULT_WAIT_MS: int = 4000
DEFAULT_THROTTLE_MS: int = 75
DEFAULT_RETRIES: int = 2
DEFAULT_MAX_FILENAME: int = 128

REQUEST_TIMEOUT_SECONDS: int = 60
SPACE_TIMEOUT_SECONDS: int = 30
