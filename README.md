A Discord bot ("JenniferBot") + FastAPI backend that accepts HTTP requests and posts media (images/videos) to Discord channels.

## Dependencies

- [discord.py](https://github.com/Rapptz/discord.py) — Discord bot client
- [fastapi](https://fastapi.tiangolo.com/) + [uvicorn](https://www.uvicorn.org/) — HTTP server
- [slowapi](https://github.com/laurentS/slowapi) — Rate limiting
- [prometheus-client](https://github.com/prometheus/client_python) — Metrics (`/metrics`)
- [pillow](https://python-pillow.org/) — Image EXIF scrubbing
- **ffmpeg** (system dependency) — Video metadata stripping

## Setup

### 1. Install Python dependencies

```sh
pip install -r requirements.txt
```

To update pinned versions after editing `requirements.in`:

```sh
pip install pip-tools
pip-compile requirements.in -o requirements.txt
pip-compile requirements-dev.in -o requirements-dev.txt
```

### 2. Install ffmpeg

- **Windows:** `winget install ffmpeg` or download from https://ffmpeg.org/download.html
- **Linux/macOS:** `apt install ffmpeg` / `brew install ffmpeg`

### 3. Configure environment

Copy `env/.env.prod.example` to `env/.env.prod` and fill in all values:

```sh
cp env/.env.prod.example env/.env.prod
```

Required variables:

| Variable | Description |
|---|---|
| `BASE_URL` | URL of the backend that serves files |
| `INTERNAL_TOKEN` | Shared secret for authenticating HTTP requests |
| `DISCORD_TOKEN` | Discord bot token |
| `WHITELIST_IDS` | Comma-separated Discord user IDs allowed to use slash commands |
| `PERMITTED_CLEAR_IDS` | Comma-separated channel IDs where `/clear_all_messages` is allowed |

Optional variables:

| Variable | Default | Description |
|---|---|---|
| `APP_ENV` | `dev` | Which `.env.<APP_ENV>` file to load |
| `TIMEOUT_POST_SCHEDULE` | `60` | Seconds before `/post-schedule` times out |
| `TIMEOUT_NOTIFY` | `10` | Seconds before notify endpoints time out |

### 4. Configure channels

Edit `config.json` to map channel names to Discord channel IDs:

```json
{
    "channels": {
        "bots": 123456789012345678
    }
}
```

The `bots` channel is required. Add additional channels as needed.

## Running

```sh
APP_ENV=prod python main.py
```

## Deploying (systemd)

Create `/etc/systemd/system/jenniferbot.service`:

```ini
[Unit]
Description=JenniferBot
After=network.target

[Service]
Type=simple
User=jenniferbot
WorkingDirectory=/opt/jenniferbot
Environment=APP_ENV=prod
ExecStart=/opt/jenniferbot/venv/bin/python main.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Then:

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now jenniferbot
sudo journalctl -u jenniferbot -f   # follow logs
```

## HTTP API

All endpoints except `/health` and `/ready` require the `X-Internal-Token` header.

| Method | Path | Description |
|---|---|---|
| GET | `/health` | Always 200 |
| GET | `/ready` | 200 if bot connected, 503 if not |
| GET | `/metrics` | Prometheus metrics |
| POST | `/post-schedule` | Download files and post to Discord (10 req/min) |
| POST | `/notify-session-expired` | Post session-expired alert to bots channel (20 req/min) |
| POST | `/notify-failure` | Post cron failure alert to bots channel (20 req/min) |

## Metrics

`/metrics` exposes Prometheus metrics:

- `jenniferbot_http_requests_total` — request count by endpoint and status code
- `jenniferbot_http_request_duration_seconds` — request latency by endpoint
- `jenniferbot_scrub_total` — metadata scrub outcomes by media type (`image`/`video`) and outcome (`success`/`failure`/`timeout`)
- `jenniferbot_discord_send_total` — Discord send outcomes (`success`/`failure`)
- `jenniferbot_bot_ready` — 1 if bot is connected, 0 otherwise
