# Media Downloader Telegram Bot

A single-file Python Telegram bot that downloads media from **Instagram**, **TikTok** and **X / Twitter** and sends it straight back into the chat, with a built-in channel-subscription gate and an admin panel.

Every video platform now runs a **multi-engine fail-safe chain**: if one downloader fails, the bot tells the user what happened, shows that it is retrying, and automatically tries the next engine.

---

## Features

- **Instagram** — reels, posts, photos and carousels (3-engine chain)
- **TikTok** — videos without watermark, plus photo posts (2-engine chain)
- **X / Twitter** — videos, GIFs and images, including multi-image posts (2-engine chain)
- **Automatic fail-safe + retry UI** — on any engine error the user sees a card naming the platform, the reason, and the backup engine being tried
- **Zero-config FFmpeg** — nothing to install on the host; see [FFmpeg](#ffmpeg-zero-configuration)
- **Subscription gate** — users must join your channel before the bot responds
- **Admin panel** — add / remove / list admins from inside Telegram
- **Clean output** — no thumbnails or preview cards. You get the media itself, with the title as the caption
- **Albums** — posts with more than one photo are grouped into a single album message sharing one caption
- **Live progress bar** — percentage, size, speed and ETA, updated in place
- **Plays on phones** — the file is handed to Telegram exactly as downloaded, with no re-encoding and no self-declared metadata. This is what mobile Telegram needs to play it
- **Quiet startup** — only the FFmpeg lines and `Bot is running...` are printed
- **Quiet console** — download errors are reported in Telegram only, never printed to the logs (set `DEBUG_LOGS=1` if you want them)
- **Automatic cleanup** — downloaded files are deleted after upload

---

## Mobile playback (why videos used to freeze on phones)

Instagram, TikTok and X increasingly serve **AV1** and **VP9** video. Telegram
Desktop decodes those in software, so a laptop plays them fine — but the phone
apps use the hardware decoder, which cannot handle AV1/VP9. The result was a
video that looked like a still photo with audio playing over it.

Checking the codec name alone was **not enough**. These files are all still
"h264", yet phone decoders reject them:

- **10-bit H.264** (`High 10` / `yuv420p10le`) — the most common culprit
- High **4:2:2 / 4:4:4** profiles
- **50-60 fps**
- 1080p and larger

So the bot no longer tries to be clever. **Every** video is re-encoded to one
conservative profile that every phone made in the last decade can decode:

| Setting | Value |
| --- | --- |
| Video | H.264, **Main** profile, **level 4.0** |
| Pixel format | **yuv420p** (8-bit) |
| Size | long side capped at **1280**, dimensions forced even |
| Frame rate | capped at **30 fps** |
| Audio | AAC 128 kbps, stereo, 44.1 kHz |
| Container | MP4 with **`+faststart`** |

Telegram also receives the real **width, height and duration** with
`supports_streaming`, which it needs to treat the upload as a playable video.

There are three fallback levels, so a download is never lost: full conversion →
conversion without the fps cap (for older FFmpeg builds) → plain remux → the
original file untouched.

> Trade-off: converting costs a few seconds of CPU per video. That is the price
> of guaranteed playback, and it applies to Instagram, TikTok and X alike, for
> single videos and albums.

---

## Download engines

Each platform tries its engines in order. The next engine is only used if the
previous one raised an error.

| Platform | 1st (primary) | 2nd | 3rd (last resort) |
|---|---|---|---|
| **Instagram** | `media-downloader` | `yt-dlp` | `parth-dl` |
| **TikTok** | `media-downloader` | `yt-dlp` | — |
| **X / Twitter** | `media-downloader` | `yt-dlp` | — |

- **`media-downloader`** — [Knuckles-Team/media-downloader](https://knuckles-team.github.io/media-downloader/usage/#as-a-python-api). Optional: if the wheel is missing on your host, the bot logs the reason at startup and simply starts at `yt-dlp`.
- **`yt-dlp`** — a core dependency, so it can never fail to install. This is the engine that honours `COOKIES_FILE`.
- **`parth-dl`** — the original Instagram downloader, kept as the final Instagram fail-safe. It is Instagram-only, so it is not used for TikTok or X.

### What the user sees

Normal download:

```
🔎 Checking Instagram...

📥 Downloading Instagram...
⚡ Engine: media-downloader
████████░░░░░░░░░░░░ 47.0%

📤 Uploading Instagram media...
```

If an engine fails, the platform name is swapped into the same card:

```
⚠️ Download error

The media-downloader engine could not finish this TikTok download.

🧾 Reason
HTTP 403: Forbidden

━━━━━━━━━━━━━━━

🔄 Retrying automatically...

🛠 Backup engine: yt-dlp

⏳ Please wait — no need to send the link again.
```

The download then continues with the next engine. Only if **all** engines fail
does the red `❌ Download failed.` card appear, listing what each engine reported.

---

## Quick start (local)

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

pip install -r requirements.txt

cp .env.example .env             # Windows: copy .env.example .env
# open .env and paste your BOT_TOKEN

python main.py
```

On a successful start you will see:

```
🎬 FFmpeg directory: /app/.ffmpeg
📦 Build: instagram-engines v4
📸 Instagram engines: 1) media-downloader  →  2) yt-dlp  →  3) parth-dl
🤖 Media Downloader Bot is running...
```

> **Check the `Build:` line after every deploy.** If it does not say `v4`, your
> host is still running old code — restart or redeploy it.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| **Python 3.12** | Pinned in `runtime.txt`. 3.10+ works. |
| **Bot token** | Create a bot with [@BotFather](https://t.me/BotFather) and copy the token. |
| **FFmpeg** | **Nothing to do.** Provided automatically — see below. |

---

## FFmpeg (zero configuration)

You do **not** install or download FFmpeg on your host. `imageio-ffmpeg` is in
`requirements.txt`, and that wheel already contains the FFmpeg binary, so it
arrives during the normal `pip install` build step — the step hosts never kill.

`ffmpeg_setup.py` then locates it at startup, in this order:

1. `FFMPEG_LOCATION`, if you set one
2. FFmpeg already on the host's `PATH` (Docker, apt, VPS)
3. A binary you dropped in the project's `bin/` folder
4. The pip wheel ��� **normally used on hosting platforms**
5. Last resort: a resumable static download, which continues from where it left
   off if the host kills it (disable with `FFMPEG_ALLOW_DOWNLOAD=0`)

Whatever is found is cached in `.ffmpeg/`, made executable, added to `PATH`, and
passed to `yt-dlp`. To test it without running the bot:

```bash
python ffmpeg_setup.py
```

Full details in `FFMPEG_README.txt`.

---

## Configuration

All settings come from environment variables. Locally, put them in `.env`. On a
host, set them as **Environment Variables / Secrets** — do **not** commit `.env`.

| Variable | Required | Default | Description |
|---|---|---|---|
| `BOT_TOKEN` | ✅ **Yes** | — | Bot token from @BotFather. |
| `OWNER_ID` | No | `1223######` | Your numeric Telegram user ID. The owner can never be removed as admin. |
| `COOKIES_FILE` | No | *(unset)* | Path to a `cookies.txt` export. **The real fix for Instagram `HTTP 401`** — see [Troubleshooting](#troubleshooting). |
| `DOWNLOAD_DIR` | No | `/tmp/downloads` | Temporary working directory. |
| `ADMINS_FILE` | No | `./admins.json` | Where the admin list is stored. Put it on a volume to survive restarts. |
| `FFMPEG_LOCATION` | No | auto-detected | Leave unset. Only to force a specific binary; accepts a folder or the binary itself. |
| `FFMPEG_CACHE_DIR` | No | `<project>/.ffmpeg` | Where resolved binaries are cached. |
| `FFMPEG_ALLOW_DOWNLOAD` | No | `1` | Set to `0` to forbid the last-resort static download. |
| `DEBUG_LOGS` | No | `0` | Download errors are **not** printed to the console — they are only sent to the user in Telegram. Set to `1` to print them for debugging. |

### Changing the required channel

The subscription gate is defined near the top of `main.py`:

```python
SUBSCRIBE_CHANNEL = "@mytools111"
SUBSCRIBE_URL     = "https://t.me/mytools111"
```

Replace both with your own channel, then **add the bot to that channel as an
administrator** — otherwise it cannot read membership and every check fails.

---

## Usage

| Action | Result |
|---|---|
| `/start` | Welcome screen. Admins get the 🔐 **Admin** button; everyone else gets subscribe / check-subscription buttons. |
| Send any supported link | The bot verifies your subscription, downloads through the engine chain, and uploads the media back. Multi-photo posts arrive as one grouped album. |
| 🔐 **Admin** | Admin panel: ➕ Add Admin �� ➖ Remove Admin · 📋 List Admins · ❌ Close. |

Supported link formats:

```
https://www.instagram.com/reel/...
https://www.instagram.com/p/...
https://www.tiktok.com/@user/video/...
https://vm.tiktok.com/...
https://x.com/user/status/...
https://twitter.com/user/status/...
```

Anything else gets a polite **Unsupported platform** reply.

---

## Deployment

> ⚠️ **Run exactly one instance.** Telegram allows a single long-polling consumer
> per token. A second instance causes `409 Conflict` and dropped messages.

### Any host (Railway / Render / Fly.io / Koyeb)

1. Upload or push this project.
2. Deploy as a **Worker / Background Service**, **not** a Web Service — the bot
   uses long polling and never opens a port.
3. Set `BOT_TOKEN` (and optionally `OWNER_ID`).
4. Keep the replica count at **1**.

`Procfile` already declares the process type:

```
worker: python main.py
```

### Docker

```bash
docker build -t media-downloader-bot .
docker run -d --name media-bot --restart unless-stopped \
  -e BOT_TOKEN=123456:ABC... \
  -e OWNER_ID=123456789 \
  media-downloader-bot
```

To keep the admin list across rebuilds, mount a volume:

```bash
docker run -d --name media-bot --restart unless-stopped \
  -e BOT_TOKEN=123456:ABC... \
  -e ADMINS_FILE=/data/admins.json \
  -v media-bot-data:/data \
  media-downloader-bot
```

### Heroku

```bash
heroku create your-app-name
heroku buildpacks:add --index 1 heroku-community/apt
heroku buildpacks:add --index 2 heroku/python
heroku config:set BOT_TOKEN=123456:ABC...
git push heroku main
heroku ps:scale web=0 worker=1
```

### VPS with systemd

```ini
# /etc/systemd/system/media-bot.service
[Unit]
Description=Media Downloader Telegram Bot
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/media-bot
EnvironmentFile=/opt/media-bot/.env
ExecStart=/opt/media-bot/.venv/bin/python main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now media-bot
sudo journalctl -u media-bot -f
```

---

## Repository layout

```
.
├── main.py                  # the entire bot
├── instagram_primary.py     # media-downloader wrapper (primary engine)
├── ffmpeg_setup.py          # zero-config FFmpeg resolver
├── requirements.txt         # Python dependencies
├── runtime.txt              # Python version pin (python-3.12.6)
├── Procfile                 # worker: python main.py
├── Aptfile                  # system packages (ffmpeg) for apt buildpacks
├── Dockerfile               # container image
├── bin/                     # optional: drop your own ffmpeg binary here
├── .env.example             # template for your .env
├── FFMPEG_README.txt        # how FFmpeg is provided, and how to test it
├── INSTAGRAM_ENGINES.txt    # engine chain internals + changelog
└── README.md
```

### Dependencies

| Package | Purpose |
|---|---|
| `pyTelegramBotAPI` | Telegram Bot API client |
| `media-downloader` | Primary download engine for all platforms *(optional)* |
| `yt-dlp` | Second engine everywhere; honours `COOKIES_FILE` |
| `parth-dl` | Final Instagram fail-safe |
| `imageio-ffmpeg` | Ships the FFmpeg binary inside a pip wheel |
| `requests` | Direct HTTP media downloads and page scraping |
| `python-dotenv` | Loads `.env` during local development |

---

## Hosting notes & limits

- **50 MB upload cap.** Bots cannot send files larger than 50 MB via the Bot API.
  Larger downloads are rejected before upload.
- **Ephemeral disks.** Most PaaS filesystems reset on deploy. `DOWNLOAD_DIR` is
  temporary by design, but mount a volume for `ADMINS_FILE` to persist admins.
- **Datacenter IPs.** Instagram and X rate-limit and challenge cloud IP ranges.
  This is the single most common cause of failures on a host. See below.
- **One instance only.** See the `409 Conflict` warning above.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Failed after 3 attempts: HTTP 401: Unauthorized` on Instagram | Instagram is refusing your host's IP because the request is not signed in. **Set `COOKIES_FILE`** — see below. No library can work around an IP/session block. |
| Logs do not show `Build: instagram-engines v4` | The host is still running old code. Restart / redeploy. |
| `note: 'media-downloader' is not installed here → ...` | The optional wheel failed to install. The bot still works, starting from `yt-dlp`. The printed reason tells you why. |
| `WARNING: ffmpeg could not be provided automatically.` | Confirm `pip install -r requirements.txt` ran and installed `imageio-ffmpeg`, or drop a static `ffmpeg` binary into `bin/`. |
| `409 Conflict` in the logs | Another copy of the bot is polling the same token. Stop it. |
| Subscription check always fails | Add the bot to `SUBSCRIBE_CHANNEL` as an administrator. |
| Admin button never appears | Your Telegram ID is not in `admins.json`. Set `OWNER_ID` to your numeric ID. |
| A video arrives as a photo + separate audio | Fixed in v3. Run `DEBUG_LOGS=1` once and confirm the `Build:` line says `v6`. |
| Video plays on Telegram Desktop but is a frozen image with sound on the phone | Fixed in v7 by reverting to the original sending mechanism. Do **not** re-add `width` / `height` / `duration` to `send_video`, and do not add codec filters to the yt-dlp format string — either one brings this bug back. |
| Startup log looks empty | That is intentional in v6. Set `DEBUG_LOGS=1` to print the build tag and engine chain again. |

### Fixing Instagram `HTTP 401` with cookies

TikTok serves anonymous requests, which is why it keeps working. Instagram
requires a logged-in session and blocks datacenter IPs, so **every** engine gets
refused on a host until you supply cookies.

1. Log into Instagram in a browser — use a **throwaway account**.
2. Export cookies with a "Get cookies.txt" browser extension.
3. Upload `cookies.txt` next to `main.py`.
4. Set `COOKIES_FILE=/app/cookies.txt` (match your host's path).
5. Restart the bot.

Cookies expire, so refresh them if the 401 returns. If cookies are not an
option, run the bot from a residential IP or a proxy.

---

## Changelog

- **v7** — **Reverted the sending mechanism to the version that plays on phones.**
  v5/v6 were the wrong diagnosis: re-encoding the video and declaring our own
  `width` / `height` / `duration` on `send_video` is what made mobile Telegram
  show a still image with sound. Telegram now receives the merged file exactly
  as yt-dlp produced it, with `supports_streaming` and nothing else, and the
  yt-dlp format chain is back to `bestvideo[height<=720]+bestaudio`. Conversion
  is kept as an opt-in escape hatch (`FORCE_VIDEO_CONVERT=1`), off by default.
  All fail-safe engines, retry cards, progress bars and quiet logs are unchanged.
- **v6** — Videos still appeared as a still image on phones because v5 trusted
  the codec name: 10-bit / 4:2:2 / 60 fps files are "h264" too and were passed
  through untouched. Now **every** video is re-encoded to H.264 Main / 8-bit /
  ≤30 fps / long side ≤1280 + AAC, with three fallback levels. The startup
  banner and engine list are no longer printed (`DEBUG_LOGS=1` restores them).
- **v5** — Fixed videos showing as a still image with audio on Telegram mobile.
  Downloads now prefer H.264, and anything else (AV1 / VP9 / HEVC) is converted
  to H.264 + AAC. Every upload gets `+faststart` and real width / height /
  duration so Telegram treats it as a streamable video.
- **v4** — Same fail-safe chain and retry UI extended to **TikTok** and
  **X / Twitter** videos, with the platform name shown in every message.
  Download errors are no longer printed to the console at all — Telegram is
  the only place they appear (`DEBUG_LOGS=1` re-enables printing).
- **v3** — Fixed video links arriving as a photo plus a separate audio file:
  engines now use the paths the downloader reports and filter to playable media.
- **v2** — Added `yt-dlp` as an always-installed second engine, made the
  Instagram metadata lookup non-fatal, de-duplicated console logging, and added
  the startup build banner.
- **v1** — Added `media-downloader` as the primary Instagram engine with
  `parth-dl` as fail-safe, and the retry notice UI.
- **v0** — Zero-config FFmpeg via pip wheel, replacing the runtime download that
  hosts kept killing.
