from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
CONFIG_DIR = ROOT / "config"

load_dotenv(ROOT / ".env")

PIXIV_OAUTH_URL = "https://oauth.secure.pixiv.net/auth/token"
PIXIV_API_BASE = "https://app-api.pixiv.net/v1"
PIXIV_CLIENT_ID = os.getenv("PIXIV_CLIENT_ID") or "MOBrBDS8blbauoSck0ZfDbtuzpyT"
PIXIV_CLIENT_SECRET = os.getenv("PIXIV_CLIENT_SECRET") or "lsACyCD94FhDUtGTXi3QzcFE2uU1hqtDaKeqrdwj"
PIXIV_USER_AGENT = "PixivAndroidApp/5.0.234 (Android 11; Pixel 5)"

TELEGRAM_API_BASE = "https://api.telegram.org"

MAX_ALBUM_SIZE = 10
MAX_IMAGE_EDGE = 1920
JPEG_QUALITY = 80
MAX_PIXIV_PAGES = 4
TELEGRAM_MAX_BYTES = 9_500_000
REQUEST_TIMEOUT = 20.0


def env_config(batch: dict) -> dict:
    """Config for one run: secrets from env, everything else (chat, artists, dry) from the batch."""
    refresh = (os.getenv("PIXIV_REFRESH_TOKEN") or "").strip()
    bot = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    chat = str(batch.get("target_chat_id") or "").strip()
    dry_run = bool(batch.get("dry"))

    missing = [n for n, v in [("PIXIV_REFRESH_TOKEN", refresh), ("target_chat_id (batch)", chat)] if not v]
    if not bot and not dry_run:
        missing.append("TELEGRAM_BOT_TOKEN")
    if missing:
        raise SystemExit("Missing required: " + ", ".join(missing))

    return {
        "pixiv_refresh_token": refresh,
        "telegram_bot_token": bot,
        "telegram_chat_id": chat,
        "dry_run": dry_run,
        "artists": list(batch.get("artists") or []),
        "dictionary_path": DATA_DIR / "character_dictionary.csv",
        "content_tags_path": DATA_DIR / "content_tags.csv",
        "adult_tags_path": DATA_DIR / "adult_tags.csv",
        "drop_tags_path": DATA_DIR / "drop_tags.csv",
    }
