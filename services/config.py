import json
import os
import shutil

from dotenv import load_dotenv

# ─────────────────────────────
# Environment
# ─────────────────────────────

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

APP_ENV = os.environ.get("APP_ENV", "dev")
load_dotenv(os.path.join(_PROJECT_ROOT, f"env/.env.{APP_ENV}"))

_missing = [v for v in ("BASE_URL", "INTERNAL_TOKEN", "DISCORD_TOKEN", "WHITELIST_IDS", "PERMITTED_CLEAR_IDS") if not os.environ.get(v)]
if _missing:
    raise SystemExit(f"Missing required env vars: {', '.join(_missing)}")

# ─────────────────────────────
# System checks
# ─────────────────────────────

if shutil.which("ffmpeg") is None:
    raise SystemExit("ffmpeg not found in PATH. Install ffmpeg before running.")

# ─────────────────────────────
# Credentials & endpoints
# ─────────────────────────────

BASE_URL = os.environ["BASE_URL"]
INTERNAL_TOKEN = os.environ["INTERNAL_TOKEN"]
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]

# ─────────────────────────────
# config.json
# ─────────────────────────────

try:
    with open(os.path.join(_PROJECT_ROOT, "config.json")) as f:
        config = json.load(f)
except FileNotFoundError:
    raise SystemExit("config.json not found")
except json.JSONDecodeError as e:
    raise SystemExit(f"config.json is invalid JSON: {e}")


def _parse_ids(env_var: str) -> list[int]:
    try:
        return [int(x.strip()) for x in os.environ[env_var].split(",") if x.strip()]
    except ValueError as e:
        raise SystemExit(f"Invalid integer in {env_var}: {e}")


config["whitelist"] = _parse_ids("WHITELIST_IDS")
config["permitted-ids-clear-all-messages"] = _parse_ids("PERMITTED_CLEAR_IDS")
CHANNEL_IDS: dict = config["channels"]

_bad_channel_ids = {k: v for k, v in CHANNEL_IDS.items() if not isinstance(v, int) or v <= 0}
if _bad_channel_ids:
    raise SystemExit(f"config.json channel IDs must be positive integers, invalid: {_bad_channel_ids}")

_missing_channels = {"bots"} - set(CHANNEL_IDS.keys())
if _missing_channels:
    raise SystemExit(f"config.json missing required channel IDs: {', '.join(sorted(_missing_channels))}")

# ─────────────────────────────
# Timeouts
# ─────────────────────────────

TIMEOUT_POST_SCHEDULE = int(os.environ.get("TIMEOUT_POST_SCHEDULE", "60"))
TIMEOUT_NOTIFY = int(os.environ.get("TIMEOUT_NOTIFY", "10"))
