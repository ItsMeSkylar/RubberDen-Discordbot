import os

# Set required env vars before main.py is imported in any test module.
# load_dotenv (called at main.py import time) does not override vars that are
# already present in the environment, so these act as safe test defaults.
os.environ.setdefault("BASE_URL", "http://localhost")
os.environ.setdefault("INTERNAL_TOKEN", "test-secret")
os.environ.setdefault("DISCORD_TOKEN", "fake-discord-token")
os.environ.setdefault("WHITELIST_IDS", "123")
os.environ.setdefault("PERMITTED_CLEAR_IDS", "456")
