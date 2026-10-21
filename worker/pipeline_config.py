"""Pipeline throughput profiles (stock vs turbo).

Set PIPELINE_PROFILE=turbo for faster catalog fill + uploads; stock to revert.
Explicit env vars (PARSER_MAX_VIDEOS, UPLOADER_MAX_PER_RUN, …) always win.

Rough budget — stock (30 days, private GHA ~2000 min/month):
  parser-latest  ~48 runs/day × 2 min  ≈  960 min/month  (cron :00/:30)
  parser-backlog ~24 runs/day × 2 min  ≈  480 min/month  (cron :20)
  uploader       ~24 runs/day          ≈ 600–1200 min/month

Turbo roughly doubles parser cadence and uploads 2 videos/run when queue allows.
Hyper: aggressive fill — use while Vidara/Dood capacity holds (public GHA minutes).
"""
from __future__ import annotations

import os

_PROFILES: dict[str, dict[str, str]] = {
    "stock": {
        "PARSER_MAX_VIDEOS": "4",
        "PARSER_MAX_NEW": "2",
        "PARSER_MAX_BACKLOG_PAGES": "3",
        "PARSER_MAX_EMPTY_PAGES": "1",
        "PARSER_REQUEST_DELAY_SEC": "0.8",
        "UPLOADER_MAX_PER_RUN": "1",
        "UPLOADER_PRE_MAX_SEC": "65",
        "UPLOADER_PRE_CLIP_SEC": "25",
        "VIDARA_POLL_SEC": "10",
    },
    "turbo": {
        "PARSER_MAX_VIDEOS": "8",
        "PARSER_MAX_NEW": "5",
        "PARSER_MAX_BACKLOG_PAGES": "8",
        "PARSER_MAX_EMPTY_PAGES": "2",
        "PARSER_REQUEST_DELAY_SEC": "0.4",
        "UPLOADER_MAX_PER_RUN": "2",
        "UPLOADER_PRE_MAX_SEC": "65",
        "UPLOADER_PRE_CLIP_SEC": "25",
        "VIDARA_POLL_SEC": "5",
    },
    "hyper": {
        "PARSER_MAX_VIDEOS": "15",
        "PARSER_MAX_NEW": "8",
        "PARSER_MAX_BACKLOG_PAGES": "12",
        "PARSER_MAX_EMPTY_PAGES": "3",
        "PARSER_REQUEST_DELAY_SEC": "0.25",
        "UPLOADER_MAX_PER_RUN": "6",
        "UPLOADER_PRE_MAX_SEC": "65",
        "UPLOADER_PRE_CLIP_SEC": "25",
        "VIDARA_POLL_SEC": "5",
    },
}


def active_profile() -> str:
    name = os.environ.get("PIPELINE_PROFILE", "stock").strip().lower()
    return name if name in _PROFILES else "stock"


def _cfg(key: str) -> str:
    if key in os.environ:
        return os.environ[key]
    profile = active_profile()
    defaults = _PROFILES[profile]
    return defaults.get(key, _PROFILES["stock"][key])


# Parser (per cron run)
MAX_VIDEOS = int(_cfg("PARSER_MAX_VIDEOS"))
MAX_NEW = int(_cfg("PARSER_MAX_NEW"))
MAX_BACKLOG_PAGES = int(_cfg("PARSER_MAX_BACKLOG_PAGES"))
MAX_EMPTY_PAGES = int(_cfg("PARSER_MAX_EMPTY_PAGES"))
REQUEST_DELAY_SEC = float(_cfg("PARSER_REQUEST_DELAY_SEC"))

# Uploader (per cron run)
MAX_PER_RUN = int(_cfg("UPLOADER_MAX_PER_RUN"))
PRE_MAX_SEC = int(_cfg("UPLOADER_PRE_MAX_SEC"))
PRE_CLIP_SEC = int(_cfg("UPLOADER_PRE_CLIP_SEC"))
VIDARA_POLL_SEC = int(_cfg("VIDARA_POLL_SEC"))

# Uploader: only namevids preview + caption (no Vidara/DoodStream host).
NAMEVIDS_ONLY = os.environ.get("UPLOADER_NAMEVIDS_ONLY", "1").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)

# namevids/oakroot: ~200 publishes/day before rate limits (observed). Hard stop in uploader.
NAMEVIDS_DAILY_CAP = int(os.environ.get("NAMEVIDS_DAILY_CAP", "200"))
