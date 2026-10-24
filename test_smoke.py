"""
Real dry test against three Pixiv artists. No Telegram traffic.
Loads config from .env.example.
Run:  python test_smoke.py
"""
import logging
import os
import sys
from dotenv import load_dotenv
from pathlib import Path

# 1. لود کردن دات‌انوی اگزَمپل قبل از هر ایمپورتی
env_path = Path(__file__).resolve().parent / ".env.example"
if env_path.exists():
    load_dotenv(env_path, override=True)
else:
    print(f"WARNING: {env_path} not found!")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

if not (os.getenv("PIXIV_REFRESH_TOKEN") or "").strip() or os.getenv("PIXIV_REFRESH_TOKEN") == "your_refresh_token_here":
    print("ERROR: PIXIV_REFRESH_TOKEN is missing or still a placeholder in .env.example")
    print("Please edit .env.example and put your actual Pixiv token.")
    sys.exit(1)

if any(os.getenv(k) for k in ("TG_API_ID", "TG_API_HASH", "TG_SESSION_STRING", "STORAGE_CHAT_ID")):
    print("WARNING: Telegram env vars are set. Dry mode will not send anything,")
    print("         but userbot_configured() will try to read the scheduled queue.")
    print()

from waifu_lab.config import env_config
from waifu_lab.plan import run_plan
import waifu_lab.plan as _plan

ARTISTS = [
    {"user_id": "120281993", "name": "Artist-120281993", "rules": {}},
    {"user_id": "110199108", "name": "Artist-110199108", "rules": {}},
    {"user_id": "123069765", "name": "Artist-123069765", "rules": {}},
]

cfg = env_config({
    "dry": True,
    "target_chat_id": "0",
    "artists": ARTISTS,
})
cfg["dry_run"] = True
cfg.pop("state", None)

# ساخت دیکشنری خالی اگر وجود نداشت
if not cfg["dictionary_path"].exists():
    print(f"WARNING: {cfg['dictionary_path']} missing; creating an empty stub")
    cfg["dictionary_path"].parent.mkdir(parents=True, exist_ok=True)
    cfg["dictionary_path"].write_text(
        "character,franchise,franchise_en,franchise_jp,studio,source,seen_count\n",
        encoding="utf-8",
    )

# محدود کردن به ۳ اسلات اول برای تست سریع
_orig_slots = _plan.upcoming_slots
def _first_three(conf, now):
    real = _orig_slots(conf, now)
    out = real[:3]
    print(f"[test] limiting to first {len(out)} slot(s): {[s.label() for s in out]}")
    return out
_plan.upcoming_slots = _first_three

print()
print("=" * 70)
print("DRY plan for artists:", [a["user_id"] for a in ARTISTS])
print("=" * 70)
print()

try:
    rc = run_plan(cfg, dry=True)
finally:
    _plan.upcoming_slots = _orig_slots

print()
print("=" * 70)
print(f"finished, rc = {rc}")
print("=" * 70)