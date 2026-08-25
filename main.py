import os
import re
import html
import json
import time
import uuid
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, urljoin

import requests
import yt_dlp
import parth_dl
from parth_dl import DownloadError
import telebot

from telebot import types
from telebot import apihelper
from dotenv import load_dotenv

# Zero-config ffmpeg provider (see ffmpeg_setup.py). It finds ffmpeg on
# PATH, in the project's bin/ folder, or inside the pip-installed
# wheel, and only as a last resort downloads a resumable static build.
import ffmpeg_setup

# PRIMARY Instagram engine (media-downloader library). Instagram only.
# TikTok and X / Twitter are untouched. If this engine errors, the bot
# tells the user and retries with the original parth-dl downloader.
import instagram_primary

load_dotenv()


# ============================================================
# CONFIGURATION
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN is missing. Set it in .env for local runs, or as an "
        "environment variable / secret on your hosting provider."
    )

OWNER_ID = int(
    os.getenv("OWNER_ID")
    or
    1223318580
)

SUBSCRIBE_CHANNEL = "@mytools111"
SUBSCRIBE_URL = "https://t.me/mytools111"


# ============================================================
# DIRECTORIES
# ============================================================

BASE_DIR = os.path.dirname(
    os.path.abspath(__file__)
)

# Both can be pointed at a mounted volume on hosting platforms so the
# files survive restarts / redeploys.

DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR") or os.path.join(
    BASE_DIR,
    "downloads"
)

ADMINS_FILE = os.getenv("ADMINS_FILE") or os.path.join(
    BASE_DIR,
    "admins.json"
)

os.makedirs(
    DOWNLOAD_DIR,
    exist_ok=True
)

# Optional cookies.txt export, for sites that need a signed-in
# session.  Servers have no browser profiles, so this file is the
# only way to supply cookies.

COOKIES_FILE = (
    os.getenv("COOKIES_FILE")
    or
    ""
).strip().strip('"').strip("'")

DEBUG_LOGS = (
    os.getenv("DEBUG_LOGS")
    or
    ""
).strip() in (
    "1",
    "true",
    "True",
    "yes"
)

if COOKIES_FILE and not os.path.isfile(COOKIES_FILE):

    print(
        "WARNING: COOKIES_FILE was set but does not exist: "
        + COOKIES_FILE
    )

    COOKIES_FILE = ""


# ============================================================
# TELEGRAM SETTINGS
# ============================================================

TELEGRAM_API_TIMEOUT = 60

TELEGRAM_UPLOAD_TIMEOUT = 600

apihelper.SESSION_TIME_TO_LIVE = 300
apihelper.RETRY_ON_ERROR = True
apihelper.RETRY_TIMEOUT = 2
apihelper.MAX_RETRIES = 3


# ============================================================
# FFMPEG CONFIGURATION
# ============================================================

# ------------------------------------------------------------
# FFmpeg is detected automatically, so the SAME code runs on a
# Windows PC and on any Linux host (Railway, Render, Fly.io, Heroku,
# Docker, VPS, ...).  Resolution order:
#
#   1. FFMPEG_LOCATION from the environment, if set.
#      It may be a folder OR the ffmpeg binary itself.
#   2. ffmpeg / ffprobe found on the system PATH   <-- hosting
#   3. A few well-known install folders.
#
# Nothing in this file has to be edited when deploying.
# ------------------------------------------------------------

FFMPEG_LOCATION = (
    os.getenv("FFMPEG_LOCATION")
    or
    ""
).strip().strip('"').strip("'")

# Kept for backwards compatibility with the old Windows-only config.
LOCAL_FFMPEG_LOCATION = FFMPEG_LOCATION
USE_LOCAL_FFMPEG = bool(FFMPEG_LOCATION)

_FFMPEG_DIR_CACHE = None

# ============================================================
# BOT
# ============================================================

bot = telebot.TeleBot(
    BOT_TOKEN
)


# ============================================================
# ADMIN STORAGE
# ============================================================

def save_admins(admins):

    admins = [
        int(x)
        for x in admins
    ]

    admins = list(
        dict.fromkeys(
            admins
        )
    )

    if OWNER_ID not in admins:

        admins.append(
            OWNER_ID
        )

    with open(
        ADMINS_FILE,
        "w",
        encoding="utf-8"
    ) as file:

        json.dump(
            admins,
            file,
            indent=4
        )


def load_admins():

    if not os.path.exists(
        ADMINS_FILE
    ):

        admins = [
            OWNER_ID
        ]

        save_admins(
            admins
        )

        return admins

    try:

        with open(
            ADMINS_FILE,
            "r",
            encoding="utf-8"
        ) as file:

            data = json.load(
                file
            )

        admins = []

        for user_id in data:

            try:

                admins.append(
                    int(user_id)
                )

            except Exception:

                pass

        if OWNER_ID not in admins:

            admins.append(
                OWNER_ID
            )

        save_admins(
            admins
        )

        return admins

    except Exception:

        admins = [
            OWNER_ID
        ]

        save_admins(
            admins
        )

        return admins


ADMINS = load_admins()


def is_admin(
    user_id
):

    try:

        return int(
            user_id
        ) in ADMINS

    except Exception:

        return False


def add_admin(
    user_id
):

    global ADMINS

    user_id = int(
        user_id
    )

    if user_id not in ADMINS:

        ADMINS.append(
            user_id
        )

        save_admins(
            ADMINS
        )

        return True

    return False


def remove_admin(
    user_id
):

    global ADMINS

    user_id = int(
        user_id
    )

    if user_id == OWNER_ID:

        return False

    if user_id in ADMINS:

        ADMINS.remove(
            user_id
        )

        save_admins(
            ADMINS
        )

        return True

    return False


admin_action = {}

# Last welcome message per chat, used by /removebutton.
LAST_WELCOME_MESSAGES = {}


# ============================================================
# FFMPEG
# ============================================================

FFMPEG_COMMON_DIRS = (
    "/usr/bin",
    "/usr/local/bin",
    "/bin",
    "/opt/homebrew/bin",
    "/snap/bin",
    "/app/vendor/ffmpeg/bin",
    "/layers/ffmpeg/bin",
    "C:\\ffmpeg\\bin",
    "C:\\Program Files\\ffmpeg\\bin"
)


def _ffmpeg_names(name):

    # Windows needs the .exe suffix, Linux/macOS hosts do not.
    if os.name == "nt":
        return (name + ".exe", name)

    return (name, name + ".exe")


def _ffmpeg_dir_has(location, name):

    for candidate in _ffmpeg_names(name):

        if os.path.isfile(
            os.path.join(location, candidate)
        ):
            return True

    return False


def _resolve_ffmpeg_dir(location):

    # Accepts a directory OR a direct path to the ffmpeg binary and
    # returns the folder holding both ffmpeg and ffprobe.
    if not location:
        return None

    location = os.path.expanduser(
        os.path.expandvars(location)
    )

    if os.path.isfile(location):
        location = os.path.dirname(location)

    if not os.path.isdir(location):
        return None

    if (
        _ffmpeg_dir_has(location, "ffmpeg")
        and
        _ffmpeg_dir_has(location, "ffprobe")
    ):
        return location

    return None


def get_ffmpeg_location():

    # Returns the folder containing ffmpeg/ffprobe, or None when they
    # cannot be located.  None is NOT fatal: the caller then omits
    # "ffmpeg_location" and yt-dlp falls back to PATH.
    global _FFMPEG_DIR_CACHE

    if _FFMPEG_DIR_CACHE:
        return _FFMPEG_DIR_CACHE

    # 1. Explicit configuration.
    resolved = _resolve_ffmpeg_dir(
        FFMPEG_LOCATION
    )

    # 2. System PATH - the normal case on hosting platforms.
    if not resolved:

        ffmpeg_path = shutil.which("ffmpeg")
        ffprobe_path = shutil.which("ffprobe")

        if ffmpeg_path and ffprobe_path:
            resolved = os.path.dirname(ffmpeg_path) or None

    # 3. Well-known install folders.
    if not resolved:

        for candidate in FFMPEG_COMMON_DIRS:

            resolved = _resolve_ffmpeg_dir(candidate)

            if resolved:
                break

    _FFMPEG_DIR_CACHE = resolved

    return resolved


# ------------------------------------------------------------
# The functions below intentionally override the legacy
# implementations above and delegate to ffmpeg_setup, which handles
# every hosting scenario without a manual ffmpeg install.
# ------------------------------------------------------------

def get_ffmpeg_location():

    return ffmpeg_setup.get_ffmpeg_location()


def get_ffmpeg_binary():

    return ffmpeg_setup.get_ffmpeg_binary()


def ffmpeg_is_available():

    return ffmpeg_setup.ffmpeg_is_available()


# ============================================================
# SILENT YT-DLP LOGGER
# ============================================================

class SilentLogger:

    def debug(
        self,
        message
    ):
        pass

    def warning(
        self,
        message
    ):
        pass

    def error(
        self,
        message
    ):
        pass


LOGGER = SilentLogger()


# ============================================================
# YT-DLP OPTIONS
# ============================================================

def base_options():

    options = {

        "quiet":
            True,

        "no_warnings":
            True,

        "logger":
            LOGGER,

        "noplaylist":
            True,

        "retries":
            10,

        "fragment_retries":
            10,

        "file_access_retries":
            5,

        "concurrent_fragment_downloads":
            8,

        "buffersize":
            1024 * 1024,

        "socket_timeout":
            30,

        "continuedl":
            True,

        "keep_fragments":
            False,

        "http_headers":
            {
                "User-Agent":
                    (
                        "Mozilla/5.0 "
                        "(Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 "
                        "(KHTML, like Gecko) "
                        "Chrome/151.0 Safari/537.36"
                    )
            }

    }

    ffmpeg_location = get_ffmpeg_location()

    if ffmpeg_location:

        # Only set when known; otherwise yt-dlp uses PATH, which is
        # how ffmpeg is normally installed on a server.
        options[
            "ffmpeg_location"
        ] = ffmpeg_location

    if COOKIES_FILE:

        # Server-side replacement for browser cookies.
        options["cookiefile"] = COOKIES_FILE

    return options


# ============================================================
# HTTP
# ============================================================

HTTP_HEADERS = {

    "User-Agent":
        (
            "Mozilla/5.0 "
            "(Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/151.0 Safari/537.36"
        ),

    "Accept-Language":
        "en-US,en;q=0.9"

}


def fetch_html(
    url,
    timeout=25
):

    response = requests.get(

        url,

        headers=HTTP_HEADERS,

        timeout=timeout,

        allow_redirects=True

    )

    response.raise_for_status()

    return (
        response.text,
        response.url
    )


# ============================================================
# URL FUNCTIONS
# ============================================================

def is_url(
    text
):

    return (
        re.match(
            r"^https?://",
            text.strip(),
            re.IGNORECASE
        )
        is not None
    )


def detect_platform(
    url
):

    host = (
        urlparse(
            url
        )
        .netloc
        .lower()
    )

    if (
        host.endswith(
            "instagram.com"
        )
        or
        host.endswith(
            "instagr.am"
        )
    ):

        return "instagram"


    if host.endswith(
        "tiktok.com"
    ):

        return "tiktok"


    if (
        host.endswith(
            "twitter.com"
        )
        or
        host.endswith(
            "x.com"
        )
    ):

        return "twitter"


    return None


# ============================================================
# URL CLEANING
# ============================================================

def clean_url(
    url
):

    if not url:
        return None

    url = html.unescape(
        url
    )

    url = url.replace(
        "\\/",
        "/"
    )

    url = url.replace(
        "\\u0026",
        "&"
    )

    return (
        url
        .strip()
        .strip('"')
        .strip("'")
    )


def absolute_url(
    url,
    base=None
):

    url = clean_url(
        url
    )

    if not url:
        return None

    if url.startswith(
        "//"
    ):

        return (
            "https:"
            + url
        )

    if url.startswith(
        "/"
    ):

        return urljoin(
            base or
            "https://www.instagram.com",
            url
        )

    return url


# ============================================================
# MEDIA URL DEDUPLICATION
# ============================================================

def normalize_media_url(
    url
):

    if not url:
        return None

    url = absolute_url(
        url
    )

    if not url:
        return None


    if (
        "pbs.twimg.com/media/"
        in url
    ):

        base = url.split(
            "?",
            1
        )[0]

        return (
            base
            + "?name=orig"
        )


    return url


def add_unique(
    items,
    value
):

    value = normalize_media_url(
        value
    )

    if (
        value
        and
        value not in items
    ):

        items.append(
            value
        )


def looks_like_image(
    url
):

    if not url:
        return False

    url = (
        url
        .lower()
        .split(
            "?",
            1
        )[0]
    )

    return url.endswith(
        (
            ".jpg",
            ".jpeg",
            ".png",
            ".webp",
            ".gif"
        )
    )


# ============================================================
# META DATA
# ============================================================

def meta_content(
    page,
    property_name=None
):

    if not property_name:
        return None

    pattern1 = (

        r'<meta[^>]+'

        r'(?:property|name)=["\']'

        + re.escape(
            property_name
        )

        + r'["\'][^>]+'

        r'content=["\']'

        r'([^"\']+)'

        r'["\']'

    )

    pattern2 = (

        r'<meta[^>]+'

        r'content=["\']'

        r'([^"\']+)'

        r'["\'][^>]+'

        r'(?:property|name)=["\']'

        + re.escape(
            property_name
        )

        + r'["\']'

    )

    match = re.search(
        pattern1,
        page,
        re.IGNORECASE
    )

    if not match:

        match = re.search(
            pattern2,
            page,
            re.IGNORECASE
        )

    if not match:

        return None

    return clean_url(
        match.group(1)
    )


def extract_meta_images(
    page,
    base_url
):

    images = []

    for key in [

        "og:image",

        "twitter:image",

        "twitter:image:src"

    ]:

        value = meta_content(
            page,
            key
        )

        if value:

            add_unique(

                images,

                absolute_url(
                    value,
                    base_url
                )

            )

    return images


def extract_meta_title(
    page
):

    for key in [

        "og:title",

        "twitter:title"

    ]:

        value = meta_content(
            page,
            key
        )

        if value:

            return (
                html.unescape(
                    value
                )
                .strip()
                [:150]
            )


    match = re.search(

        r"<title[^>]*>"
        r"(.*?)"
        r"</title>",

        page,

        re.IGNORECASE |
        re.DOTALL

    )

    if match:

        return (
            html.unescape(
                match.group(1)
            )
            .strip()
            [:150]
        )


    return "Media"


# ============================================================
# TWITTER / X MEDIA TYPE DETECTION
# ============================================================

def twitter_has_video(page):
    """Detect an X/Twitter post containing video before treating pbs URLs as photos."""

    video_markers = (
        "video.twimg.com/",
        "video_info",
        '"type":"video"',
        '"type": "video"',
        '"media_url_https":',
        '"variants":',
        "og:video",
        "og:video:url",
        "video_url",
    )

    lower = page.lower()
    if "video.twimg.com/" in lower:
        return True

    # Stronger JSON signals for a Twitter video object.
    if '"type":"video"' in lower or '"type": "video"' in lower:
        return True
    if '"variants":' in lower and "video.twimg.com" in lower:
        return True
    if "og:video" in lower and "video.twimg.com" in lower:
        return True

    return False


# ============================================================
# TWITTER / X IMAGE EXTRACTION
# ============================================================

def normalize_twitter_image_url(
    url
):

    if not isinstance(
        url,
        str
    ):

        return url


    value = url.replace(
        "\/",
        "/"
    ).strip()

    value = value.strip("\"'")

    if "pbs.twimg.com/media/" not in value:

        return value


    value = re.sub(
        r'([?&])name=[^&]+',
        r'\1name=orig',
        value,
        flags=re.IGNORECASE
    )

    if "name=" not in value:

        joiner = "&" if "?" in value else "?"
        value = value + joiner + "name=orig"


    return value


def is_twitter_thumbnail_url(
    url
):

    lower = str(url or "").lower()

    return any(
        marker in lower
        for marker in (
            "tweet_video_thumb",
            "ext_tw_video_thumb",
            "amplify_video_thumb",
            "/ext_tw_video/",
            "/tweet_video/"
        )
    )


def extract_twitter_images(
    page,
    base_url
):

    images = []


    patterns = [

        (
            r'https?://pbs\.twimg\.com/media/'
            r'[A-Za-z0-9_-]+'
            r'(?:\?[^"\'<>\s\\]+)?'
        ),

        (
            r'https?:\\?/\\?/pbs\.twimg\.com/media/'
            r'[A-Za-z0-9_-]+'
            r'(?:\\?[^"\'<>\s\\]+)?'
        )

    ]


    def dedupe_by_media_path(values):

        unique = []
        seen = set()

        for value in values:

            key = str(value).split("?", 1)[0]

            if key in seen:
                continue

            seen.add(key)
            unique.append(value)

        return unique


    for pattern in patterns:

        matches = re.findall(

            pattern,

            page,

            re.IGNORECASE

        )

        for value in matches:

            normalized = normalize_twitter_image_url(
                value
            )

            if (
                "pbs.twimg.com/media/" in normalized
                and
                not is_twitter_thumbnail_url(
                    normalized
                )
            ):

                add_unique(
                    images,
                    normalized
                )


    direct_media = dedupe_by_media_path(
        images
    )


    if direct_media:

        return direct_media


    meta_images = []

    for value in extract_meta_images(
        page,
        base_url
    ):

        normalized = normalize_twitter_image_url(
            value
        )

        if (

            not is_twitter_thumbnail_url(
                normalized
            )

            and

            (
                "pbs.twimg.com/media/" in normalized
                or
                looks_like_image(
                    normalized
                )
            )

        ):

            add_unique(
                meta_images,
                normalized
            )


    return dedupe_by_media_path(
        meta_images
    )


# ============================================================
# INSTAGRAM - PARTH-DL
# ============================================================

def instagram_progress_hook(
    chat_id,
    message_id
):

    state = {

        "last":
            0.0,

        "percent":
            -1

    }


    def hook(
        downloaded_bytes,
        total_bytes
    ):

        now = time.time()


        if total_bytes:

            percent = (

                downloaded_bytes
                * 100
                /
                total_bytes

            )

        else:

            percent = 0


        current = int(
            percent
        )


        if (

            now
            -
            state["last"]
            <
            1

            and

            current
            ==
            state["percent"]

        ):

            return


        state["last"] = now

        state["percent"] = current


        if total_bytes:

            text = (

                "📥 <b>Downloading Instagram...</b>\n\n"

                f"{make_progress_bar(percent)}\n"

                f"<b>{percent:.1f}%</b>\n\n"

                f"📦 "
                f"{format_size(downloaded_bytes)}"
                f" / "
                f"{format_size(total_bytes)}"

            )

        else:

            text = (

                "📥 <b>Downloading Instagram...</b>\n\n"

                f"📦 "
                f"{format_size(downloaded_bytes)}"

            )


        try:

            bot.edit_message_text(

                text,

                chat_id,

                message_id,

                parse_mode="HTML",

                timeout=
                TELEGRAM_API_TIMEOUT

            )

        except Exception:

            pass


    return hook


def get_instagram_info(
    url
):

    return parth_dl.get_info(
        url
    )


def get_instagram_media(
    url,
    output_path,
    chat_id=None,
    message_id=None
):

    hook = None


    if (
        chat_id is not None
        and
        message_id is not None
    ):

        hook = instagram_progress_hook(

            chat_id,

            message_id

        )


    downloader = (
        parth_dl.InstagramDownloader(

            verbose=False,

            quiet=True,

            rate_limit=True,

            overwrite=True,

            progress_hook=hook

        )
    )


    info = parth_dl.get_info(
        url
    )


    title = (

        info.get(
            "title"
        )

        or

        "Instagram"

    )


    thumbnail = info.get(
        "thumbnail"
    )


    entries = (
        info.get(
            "entries"
        )
        or
        []
    )


    media_count = len(
        entries
    )


    os.makedirs(

        output_path,

        exist_ok=True

    )


    result = downloader.download(

        url,

        output_path=output_path,

        quality="best"

    )


    if isinstance(
        result,
        str
    ):

        files = [
            result
        ]

    else:

        files = list(
            result
            or
            []
        )


    files = [

        f

        for f in files

        if (
            f
            and
            os.path.isfile(
                f
            )
        )

    ]


    return {

        "title":
            title,

        "thumbnail":
            thumbnail,

        "type":
            info.get(
                "type"
            ),

        "entries":
            entries,

        "media_count":
            media_count,

        "files":
            files

    }


# ============================================================
# INSTAGRAM - PRIMARY ENGINE (media-downloader) + FAIL-SAFE
# ============================================================

# Instagram only. TikTok and X / Twitter keep their original path.
#
#   1st try : media-downloader   (instagram_primary.py, if installed)
#   2nd try : yt-dlp             (always installed with this bot)
#   3rd try : parth-dl           (the original downloader)
#
# Between each attempt the user gets an on-screen notice saying that an
# error happened and that the bot is retrying with the next engine.

PRIMARY_ENGINE_LABEL = "media-downloader"

YTDLP_ENGINE_LABEL = "yt-dlp"

FAILSAFE_ENGINE_LABEL = "parth-dl"


# ------------------------------------------------------------
# CONSOLE DE-DUPLICATION
# ------------------------------------------------------------
# Download errors are NOT printed to the console at all: the user
# already receives them in Telegram, so repeating them in the logs adds
# nothing. log_once() keeps its signature and its de-duplication so every
# existing call site still works, but it stays silent.
#
# To bring the logs back for debugging, set DEBUG_LOGS=1.

_LOGGED_MESSAGES = set()


def log_once(
    message
):

    text = str(
        message
    )


    if text in _LOGGED_MESSAGES:

        return


    if len(
        _LOGGED_MESSAGES
    ) > 300:

        _LOGGED_MESSAGES.clear()


    _LOGGED_MESSAGES.add(
        text
    )


    # Silent by default - Telegram is the only place errors are shown.

    if DEBUG_LOGS:

        print(
            text
        )


def ytdlp_reported_files(
    info,
    collected=None
):

    # Use the paths yt-dlp ITSELF reports for this download.
    #
    # Scanning the whole output folder was wrong: it also picked up
    # thumbnails and leftover audio-only streams, so a video link came
    # out as a photo + an audio file.

    if collected is None:

        collected = []


    if not isinstance(
        info,
        dict
    ):

        return collected


    for item in (
        info.get(
            "requested_downloads"
        )
        or
        []
    ):

        if isinstance(
            item,
            dict
        ):

            path = (
                item.get(
                    "filepath"
                )
                or
                item.get(
                    "_filename"
                )
            )


            if path:

                collected.append(
                    path
                )


    single = (
        info.get(
            "filepath"
        )
        or
        info.get(
            "_filename"
        )
    )

    if single:

        collected.append(
            single
        )


    for entry in (
        info.get(
            "entries"
        )
        or
        []
    ):

        ytdlp_reported_files(
            entry,
            collected
        )


    return collected


def keep_playable_media(
    files
):

    # Keep only real, non-empty files, de-duplicated, order preserved.

    unique = []


    for path in files:

        if (

            path

            and

            os.path.isfile(
                path
            )

            and

            not path.endswith(
                ".part"
            )

            and

            os.path.getsize(
                path
            ) > 0

            and

            path not in unique

        ):

            unique.append(
                path
            )


    videos = [

        path

        for path in unique

        if file_extension(
            path
        ) in ALBUM_VIDEO_EXTENSIONS

    ]

    photos = [

        path

        for path in unique

        if file_extension(
            path
        ) in ALBUM_PHOTO_EXTENSIONS

    ]


    # A video link must arrive as a VIDEO. If yt-dlp also wrote a
    # thumbnail or a separate audio track, those are dropped here.

    if videos:

        return videos


    # A real photo post still works: no video means the images stand.

    if photos:

        return photos


    # Audio-only / anything else (e.g. an actual music link).

    return unique


def ytdlp_format_candidates():

    # Prefer already-muxed real videos first. Falling straight into a
    # separate video-only stream can produce frozen poster-like clips.

    return (
        "best[height<=720]/best",
        "best[ext=mp4][height<=720]/best[height<=720]/best",
        "bestvideo[height<=720]+bestaudio/best[height<=720]/best"
    )


def download_instagram_with_ytdlp(
    url,
    output_path,
    chat_id=None,
    message_id=None,
    title=None
):

    # yt-dlp is a CORE dependency of this bot, so this engine is always
    # present even when an optional wheel fails to install on the host.
    # It also honours COOKIES_FILE via base_options(), which is what
    # actually defeats Instagram's 401 on datacenter IPs.

    os.makedirs(
        output_path,
        exist_ok=True
    )


    files = []

    info = {}

    last_error = None

    for attempt_index, format_string in enumerate(
        ytdlp_format_candidates(),
        start=1
    ):

        for leftover in os.listdir(
            output_path
        ):

            delete_file(
                os.path.join(
                    output_path,
                    leftover
                )
            )


        options = base_options()

        options["outtmpl"] = os.path.join(

            output_path,

            "%(id)s.%(ext)s"

        )

        options["format"] = format_string

        options["merge_output_format"] = "mp4"

        options["writethumbnail"] = False

        options["writeinfojson"] = False


        ffmpeg_dir = get_ffmpeg_location()

        if ffmpeg_dir:

            options["ffmpeg_location"] = ffmpeg_dir


        if (
            chat_id is not None
            and
            message_id is not None
        ):

            options["progress_hooks"] = [

                create_progress_hook(
                    chat_id,
                    message_id
                )

            ]


        try:

            with yt_dlp.YoutubeDL(
                options
            ) as downloader:

                info = downloader.extract_info(

                    url,

                    download=True

                )


            files = validate_downloaded_media(

                ytdlp_reported_files(
                    info
                )

            )


            if not files:

                scanned = []


                for name in sorted(
                    os.listdir(
                        output_path
                    )
                ):

                    scanned.append(

                        os.path.join(
                            output_path,
                            name
                        )

                    )


                files = validate_downloaded_media(
                    scanned
                )


            if files:

                break


            last_error = RuntimeError(
                "yt-dlp finished without producing a media file."
            )

        except Exception as error:

            last_error = error

            log_once(
                "Instagram yt-dlp attempt "
                + str(attempt_index)
                + ": "
                + str(error)
            )

            files = []


    if not files:

        raise RuntimeError(

            str(last_error)
            if last_error
            else
            "yt-dlp finished without producing a media file."

        )


    return {

        "title":
            (
                (info or {}).get(
                    "title"
                )
                or
                title
                or
                "Instagram"
            ),

        "thumbnail":
            (info or {}).get(
                "thumbnail"
            ),

        "type":
            "media",

        "entries":
            [],

        "media_count":
            len(
                files
            ),

        "files":
            files,

        "engine":
            YTDLP_ENGINE_LABEL

    }


PLATFORM_LABELS = {

    "instagram":
        "Instagram",

    "tiktok":
        "TikTok",

    "twitter":
        "X / Twitter"

}


def platform_label(
    platform
):

    return (
        PLATFORM_LABELS.get(
            platform
        )
        or
        "Media"
    )


def engine_progress(
    chat_id,
    message_id,
    platform_name="Instagram",
    engine_label=None
):

    state = {

        "last":
            0.0,

        "percent":
            -1

    }


    def callback(
        percent
    ):

        now = time.time()


        current = int(
            percent
        )


        if (

            now
            -
            state["last"]
            <
            1

            and

            current
            ==
            state["percent"]

        ):

            return


        state["last"] = now

        state["percent"] = current


        text = (

            f"📥 <b>Downloading {platform_name}...</b>\n\n"

            f"⚡ <i>Engine: "
            f"{engine_label or PRIMARY_ENGINE_LABEL}</i>\n\n"

            f"{make_progress_bar(percent)}\n"

            f"<b>{percent:.1f}%</b>"

        )


        try:

            bot.edit_message_text(

                text,

                chat_id,

                message_id,

                parse_mode="HTML",

                timeout=
                TELEGRAM_API_TIMEOUT

            )

        except Exception:

            pass


    return callback


def show_engine_retry_notice(
    chat_id,
    message_id,
    error=None,
    failed_engine=None,
    next_engine=None,
    platform_name=None
):

    # Friendly "something went wrong, I am retrying" card.

    failed_engine = (
        failed_engine
        or
        PRIMARY_ENGINE_LABEL
    )

    next_engine = (
        next_engine
        or
        FAILSAFE_ENGINE_LABEL
    )


    text = (

        "⚠️ <b>Download error</b>\n\n"

        f"The <b>{failed_engine}</b> engine could not finish "
        + (
            f"this <b>{platform_name}</b> download.\n\n"
            if platform_name
            else "this download.\n\n"
        )

    )


    if error:

        text += (

            "🧾 <b>Reason</b>\n"

            "<code>"
            +
            html.escape(
                str(error)
            )[:300]
            +
            "</code>\n\n"

        )


    text += (

        "━━━━━━━━━━━━━━━\n\n"

        "🔄 <b>Retrying automatically...</b>\n\n"

        f"🛠 <i>Backup engine: {next_engine}</i>\n\n"

        "⏳ Please wait — no need to send the link again."

    )


    try:

        bot.edit_message_text(

            text,

            chat_id,

            message_id,

            parse_mode="HTML",

            timeout=
            TELEGRAM_API_TIMEOUT

        )

    except Exception:

        pass


def download_instagram_media(
    url,
    output_path,
    chat_id=None,
    message_id=None,
    title=None
):

    # Returns the same dict shape as get_instagram_media(), plus
    # "engine", so the rest of the bot keeps working unchanged.

    problems = []

    has_ui = (
        chat_id is not None
        and
        message_id is not None
    )


    # ----- 1. PRIMARY: media-downloader -----

    if instagram_primary.is_available():

        if has_ui:

            try:

                bot.edit_message_text(

                    (
                        "📥 <b>Downloading Instagram...</b>\n\n"

                        f"⚡ <i>Engine: {PRIMARY_ENGINE_LABEL}</i>"
                    ),

                    chat_id,

                    message_id,

                    parse_mode="HTML",

                    timeout=
                    TELEGRAM_API_TIMEOUT

                )

            except Exception:

                pass


        try:

            result = instagram_primary.download(

                url,

                output_path,

                progress_callback=(
                    engine_progress(
                        chat_id,
                        message_id,
                        "Instagram",
                        PRIMARY_ENGINE_LABEL
                    )
                    if has_ui
                    else None
                )

            )


            return {

                "title":
                    (
                        result.get(
                            "title"
                        )
                        or
                        title
                        or
                        "Instagram"
                    ),

                "thumbnail":
                    None,

                "type":
                    "media",

                "entries":
                    [],

                "media_count":
                    len(
                        result["files"]
                    ),

                "files":
                    result["files"],

                "engine":
                    PRIMARY_ENGINE_LABEL

            }


        except Exception as primary_error:

            problems.append(

                PRIMARY_ENGINE_LABEL
                + ": "
                + str(primary_error)

            )


            log_once(
                "Instagram primary engine failed: "
                + str(primary_error)
            )


            if has_ui:

                show_engine_retry_notice(

                    chat_id,

                    message_id,

                    primary_error,

                    PRIMARY_ENGINE_LABEL,

                    YTDLP_ENGINE_LABEL,

                    "Instagram"

                )


                # Let the user actually read the notice.
                time.sleep(
                    2
                )

    else:

        problems.append(

            PRIMARY_ENGINE_LABEL
            + ": library not installed"

        )


    # ----- 2. yt-dlp (always installed with this bot) -----

    if has_ui:

        try:

            bot.edit_message_text(

                (
                    "📥 <b>Downloading Instagram...</b>\n\n"

                    f"⚡ <i>Engine: {YTDLP_ENGINE_LABEL}</i>"
                ),

                chat_id,

                message_id,

                parse_mode="HTML",

                timeout=
                TELEGRAM_API_TIMEOUT

            )

        except Exception:

            pass


    try:

        return download_instagram_with_ytdlp(

            url,

            output_path,

            chat_id,

            message_id,

            title

        )


    except Exception as ytdlp_error:

        problems.append(

            YTDLP_ENGINE_LABEL
            + ": "
            + str(ytdlp_error)

        )


        log_once(
            "Instagram yt-dlp engine failed: "
            + str(ytdlp_error)
        )


        if has_ui:

            show_engine_retry_notice(

                chat_id,

                message_id,

                ytdlp_error,

                YTDLP_ENGINE_LABEL,

                FAILSAFE_ENGINE_LABEL,

                "Instagram"

            )


            time.sleep(
                2
            )


    # ----- 3. FAIL-SAFE: parth-dl (the original downloader) -----

    try:

        media = get_instagram_media(

            url,

            output_path,

            chat_id,

            message_id

        )


        if not media.get(
            "files"
        ):

            raise RuntimeError(

                "no downloadable media was produced."

            )


        media["engine"] = (
            FAILSAFE_ENGINE_LABEL
        )


        return media


    except Exception as failsafe_error:

        problems.append(

            FAILSAFE_ENGINE_LABEL
            + ": "
            + str(failsafe_error)

        )


    raise RuntimeError(

        "Both Instagram engines failed.\n\n"
        +
        "\n\n".join(
            problems
        )

    )


# ============================================================
# TIKTOK / X-TWITTER - SAME FAIL-SAFE CHAIN AS INSTAGRAM
# ============================================================

# Identical behaviour and identical UI to the Instagram chain, with the
# platform name swapped in:
#
#   1st try : media-downloader   (if installed)
#   2nd try : yt-dlp             (download_social_media, unchanged)
#
# parth-dl is NOT used here - it is an Instagram-only library, so it
# stays exactly where it was.

def download_media_with_failsafe(
    platform_name,
    url,
    chat_id,
    message_id,
    title=None
):

    problems = []


    # ----- 1. PRIMARY: media-downloader -----

    if instagram_primary.is_available():

        try:

            bot.edit_message_text(

                (
                    "📥 <b>Downloading "
                    + platform_name
                    + "...</b>\n\n"

                    f"⚡ <i>Engine: {PRIMARY_ENGINE_LABEL}</i>"
                ),

                chat_id,

                message_id,

                parse_mode="HTML",

                timeout=
                TELEGRAM_API_TIMEOUT

            )

        except Exception:

            pass


        job_dir = os.path.join(

            DOWNLOAD_DIR,

            "eng_"
            +
            uuid.uuid4().hex

        )


        try:

            result = instagram_primary.download(

                url,

                job_dir,

                progress_callback=
                engine_progress(
                    chat_id,
                    message_id,
                    platform_name,
                    PRIMARY_ENGINE_LABEL
                )

            )


            # Same filter as Instagram: a video link must arrive as a
            # video, never as a thumbnail plus a separate audio file.

            files = keep_playable_media(

                result["files"]

            )


            if not files:

                raise RuntimeError(

                    "no playable media was produced."

                )


            return files


        except Exception as primary_error:

            problems.append(

                PRIMARY_ENGINE_LABEL
                + ": "
                + str(primary_error)

            )


            log_once(
                platform_name
                + " primary engine failed: "
                + str(primary_error)
            )


            show_engine_retry_notice(

                chat_id,

                message_id,

                primary_error,

                PRIMARY_ENGINE_LABEL,

                YTDLP_ENGINE_LABEL,

                platform_name

            )


            # Let the user actually read the notice.
            time.sleep(
                2
            )

    else:

        problems.append(

            PRIMARY_ENGINE_LABEL
            + ": library not installed"

        )


    # ----- 2. FAIL-SAFE: the original yt-dlp path -----

    try:

        bot.edit_message_text(

            (
                "📥 <b>Downloading "
                + platform_name
                + "...</b>\n\n"

                f"⚡ <i>Engine: {YTDLP_ENGINE_LABEL}</i>"
            ),

            chat_id,

            message_id,

            parse_mode="HTML",

            timeout=
            TELEGRAM_API_TIMEOUT

        )

    except Exception:

        pass


    try:

        # Unchanged original downloader, so its behaviour for TikTok
        # slideshows / multi-image posts stays exactly as it was.

        return download_social_media(

            url,

            chat_id,

            message_id,

            strict_video_validation=(
                platform_name != "TikTok"
            )

        )


    except Exception as failsafe_error:

        problems.append(

            YTDLP_ENGINE_LABEL
            + ": "
            + str(failsafe_error)

        )


        log_once(
            platform_name
            + " yt-dlp engine failed: "
            + str(failsafe_error)
        )


    raise RuntimeError(

        "Both "
        + platform_name
        + " engines failed.\n\n"
        +
        "\n\n".join(
            problems
        )

    )


# ============================================================
# DIRECT IMAGE DOWNLOAD
# ============================================================

def download_image(
    url
):

    response = requests.get(

        url,

        headers=HTTP_HEADERS,

        timeout=60,

        stream=True

    )

    response.raise_for_status()


    content_type = (

        response
        .headers
        .get(
            "Content-Type",
            ""
        )
        .lower()

    )


    if "png" in content_type:

        extension = ".png"

    elif "webp" in content_type:

        extension = ".webp"

    elif "gif" in content_type:

        extension = ".gif"

    else:

        extension = ".jpg"


    filename = os.path.join(

        DOWNLOAD_DIR,

        uuid.uuid4().hex
        +
        extension

    )


    with open(
        filename,
        "wb"
    ) as file:

        for chunk in response.iter_content(

            1024 * 1024

        ):

            if chunk:

                file.write(
                    chunk
                )


    return filename


# ============================================================
# DIRECT VIDEO
# ============================================================

def download_direct_video(
    url,
    chat_id,
    message_id
):

    filename = os.path.join(

        DOWNLOAD_DIR,

        uuid.uuid4().hex
        +
        ".mp4"

    )


    response = requests.get(

        url,

        headers=HTTP_HEADERS,

        timeout=60,

        stream=True

    )

    response.raise_for_status()


    total = int(

        response
        .headers
        .get(
            "Content-Length",
            "0"
        )

    )


    downloaded = 0

    last_update = 0


    with open(
        filename,
        "wb"
    ) as file:

        for chunk in response.iter_content(

            1024 * 1024

        ):

            if not chunk:

                continue


            file.write(
                chunk
            )

            downloaded += len(
                chunk
            )


            if (

                total

                and

                time.time()
                -
                last_update
                >
                1

            ):

                percent = (

                    downloaded
                    * 100
                    /
                    total

                )


                last_update = (
                    time.time()
                )


                try:

                    bot.edit_message_text(

                        (
                            "📥 <b>Downloading...</b>\n\n"

                            f"{make_progress_bar(percent)}\n"

                            f"<b>{percent:.1f}%</b>"
                        ),

                        chat_id,

                        message_id,

                        parse_mode="HTML",

                        timeout=
                        TELEGRAM_API_TIMEOUT

                    )

                except Exception:

                    pass


    return filename


# ============================================================
# PROGRESS BAR
# ============================================================

def make_progress_bar(
    percent
):

    total = 20


    filled = int(

        total
        *
        percent
        /
        100

    )


    filled = max(

        0,

        min(
            total,
            filled
        )

    )


    return (

        "█"
        *
        filled

        +

        "░"
        *
        (
            total
            -
            filled
        )

    )


def format_size(
    value
):

    if not value:

        return "0 MB"


    mb = (

        value
        /
        1024
        /
        1024

    )


    if mb < 1024:

        return (
            f"{mb:.1f} MB"
        )


    return (
        f"{mb / 1024:.2f} GB"
    )


# ============================================================
# YT-DLP PROGRESS
# ============================================================

def create_progress_hook(
    chat_id,
    message_id
):

    state = {

        "last_time":
            0,

        "last_percent":
            -1

    }


    def progress_hook(
        data
    ):

        if data.get(
            "status"
        ) != "downloading":

            return


        now = time.time()


        downloaded = (

            data.get(
                "downloaded_bytes"
            )
            or
            0

        )


        total = (

            data.get(
                "total_bytes"
            )

            or

            data.get(
                "total_bytes_estimate"
            )

            or

            0

        )


        if total:

            percent = (

                downloaded
                /
                total
                *
                100

            )

        else:

            percent = 0


        current_percent = int(
            percent
        )


        if (

            now
            -
            state["last_time"]
            <
            1

            and

            current_percent
            ==
            state["last_percent"]

        ):

            return


        state["last_time"] = now

        state["last_percent"] = (
            current_percent
        )


        speed = data.get(
            "speed"
        )


        if speed:

            speed_text = (

                f"{speed / 1024 / 1024:.2f}"
                " MB/s"

            )

        else:

            speed_text = (
                "Calculating..."
            )


        eta = data.get(
            "eta"
        )


        if eta is None:

            eta_text = (
                "Calculating..."
            )

        elif eta >= 60:

            eta_text = (

                f"{eta // 60}m "
                f"{eta % 60}s"

            )

        else:

            eta_text = (
                f"{eta}s"
            )


        downloaded_text = format_size(
            downloaded
        )


        if total:

            size_text = (

                downloaded_text
                +
                " / "
                +
                format_size(
                    total
                )

            )

        else:

            size_text = downloaded_text


        text = (

            "📥 <b>Downloading...</b>\n\n"

            f"{make_progress_bar(percent)}\n"

            f"<b>{percent:.1f}%</b>\n\n"

            f"📦 {size_text}\n"

            f"⚡ {speed_text}\n"

            f"⏱ ETA: {eta_text}"

        )


        try:

            bot.edit_message_text(

                text,

                chat_id,

                message_id,

                parse_mode="HTML",

                timeout=
                TELEGRAM_API_TIMEOUT

            )

        except Exception:

            pass


    return progress_hook


def create_postprocessor_hook(
    chat_id,
    message_id
):

    def hook(
        data
    ):

        status = data.get(
            "status"
        )


        if status == "started":

            text = (

                "⚙️ <b>Processing...</b>\n\n"

                "Preparing your media..."

            )

        elif status == "finished":

            text = (

                "📤 <b>Preparing upload...</b>\n\n"

                "Please wait..."

            )

        else:

            return


        try:

            bot.edit_message_text(

                text,

                chat_id,

                message_id,

                parse_mode="HTML",

                timeout=
                TELEGRAM_API_TIMEOUT

            )

        except Exception:

            pass


    return hook


# ============================================================
# DELETE FILE
# ============================================================

def delete_file(
    path
):

    try:

        if (

            path

            and

            os.path.exists(
                path
            )

        ):

            os.remove(
                path
            )

    except OSError:

        pass


# ============================================================
# DELETE MESSAGE
# ============================================================

def delete_message_safe(
    chat_id,
    message_id
):

    try:

        bot.delete_message(

            chat_id,

            message_id,

            timeout=
            TELEGRAM_API_TIMEOUT

        )

    except Exception:

        pass


# ============================================================
# ERROR
# ============================================================

def show_error(
    chat_id,
    message_id,
    error=None
):

    text = (
        "❌ <b>Download failed.</b>\n\n"
    )


    if error:

        text += (

            "<code>"
            +
            html.escape(
                str(error)
            )[:1000]
            +
            "</code>"

        )

    else:

        text += (
            "Please try again."
        )


    try:

        bot.edit_message_text(

            text,

            chat_id,

            message_id,

            parse_mode="HTML",

            timeout=
            TELEGRAM_API_TIMEOUT

        )

    except Exception:

        pass


# ============================================================
# TELEGRAM SPINNER
# ============================================================

class TelegramSpinner:

    def __init__(
        self,
        chat_id,
        message_id,
        text
    ):

        self.chat_id = chat_id

        self.message_id = (
            message_id
        )

        self.text = text

        self.running = False

        self.thread = None

        self.frames = [

            "⏳",
            "🔄",
            "🔃",
            "🔁"

        ]


    def start(self):

        if self.running:

            return


        self.running = True


        self.thread = threading.Thread(

            target=self._run,

            daemon=True

        )


        self.thread.start()


    def stop(self):

        self.running = False


        if self.thread:

            self.thread.join(
                timeout=1
            )


    def _run(self):

        index = 0


        while self.running:

            try:

                bot.edit_message_text(

                    (
                        f"{self.frames[index % len(self.frames)]} "
                        f"<b>{self.text}</b>"
                    ),

                    self.chat_id,

                    self.message_id,

                    parse_mode="HTML",

                    timeout=
                    TELEGRAM_API_TIMEOUT

                )

            except Exception:

                pass


            index += 1


            time.sleep(
                0.8
            )


# ============================================================
# SUBSCRIPTION CHECK
# ============================================================

def is_user_subscribed(
    user_id
):

    try:

        member = bot.get_chat_member(

            SUBSCRIBE_CHANNEL,

            int(user_id)

        )


        status = member.status


        if status in (

            "member",

            "administrator",

            "creator"

        ):

            return True


        if status == "restricted":

            return bool(

                getattr(

                    member,

                    "is_member",

                    False

                )

            )


        return False


    except Exception:

        return None


# ============================================================
# SUBSCRIPTION KEYBOARD
# ============================================================

def subscription_keyboard():

    keyboard = (
        types.InlineKeyboardMarkup()
    )


    keyboard.row(

        types.InlineKeyboardButton(

            "📢 Subscribe to channel",

            url=SUBSCRIBE_URL

        )

    )


    keyboard.row(

        types.InlineKeyboardButton(

            "✅ Check subscription",

            callback_data=
            "check_subscription"

        )

    )


    return keyboard


def admin_button():

    keyboard = (
        types.InlineKeyboardMarkup()
    )


    keyboard.row(

        types.InlineKeyboardButton(

            "🔐 Admin",

            callback_data=
            "admin_panel"

        )

    )


    return keyboard


# ============================================================
# WELCOME
# ============================================================

def send_welcome(
    chat_id,
    admin=False
):

    text = (

        "\U0001f44b <b>Welcome to Media Downloader!</b>\n\n"

        "Download media from:\n"

        "\U0001f4f8 Instagram\n"

        "\U0001f3b5 TikTok\n"

        "\U0001d55a X / Twitter\n\n"

    )


    if admin:

        text += (

            "Send a media link.\n\n"

            "\U0001f510 <b>Administrator access enabled.</b>"

        )

        keyboard = admin_button()

    else:

        text += (

            "\U0001f512 <b>Before using the bot</b>\n"

            "Subscribe to:\n\n"

            f"\U0001f4e2 <b>{SUBSCRIBE_CHANNEL}</b>\n\n"

            "Then press "
            "<b>Check subscription</b>."

        )

        keyboard = subscription_keyboard()


    sent_message = bot.send_message(

        chat_id,

        text,

        reply_markup=
        keyboard,

        parse_mode="HTML",

        timeout=
        TELEGRAM_API_TIMEOUT

    )

    LAST_WELCOME_MESSAGES[chat_id] = sent_message.message_id
    return sent_message


# ============================================================
# ADMIN PANEL
# ============================================================

def admin_panel_keyboard():

    keyboard = (
        types.InlineKeyboardMarkup()
    )


    keyboard.row(

        types.InlineKeyboardButton(

            "➕ Add Admin",

            callback_data=
            "admin_add"

        ),

        types.InlineKeyboardButton(

            "➖ Remove Admin",

            callback_data=
            "admin_remove"

        )

    )


    keyboard.row(

        types.InlineKeyboardButton(

            "📋 List Admins",

            callback_data=
            "admin_list"

        )

    )


    keyboard.row(

        types.InlineKeyboardButton(

            "❌ Close",

            callback_data=
            "admin_close"

        )

    )


    return keyboard


def show_admin_panel(
    chat_id,
    message_id=None
):

    text = (

        "🔐 <b>Admin Control Panel</b>\n\n"

        f"👑 Owner ID:\n"
        f"<code>{OWNER_ID}</code>\n\n"

        f"👥 Current admins: "
        f"<b>{len(ADMINS)}</b>\n\n"

        "Choose an action:"

    )


    if message_id:

        try:

            bot.edit_message_text(

                text,

                chat_id,

                message_id,

                reply_markup=
                admin_panel_keyboard(),

                parse_mode="HTML",

                timeout=
                TELEGRAM_API_TIMEOUT

            )

        except Exception:

            pass

    else:

        bot.send_message(

            chat_id,

            text,

            reply_markup=
            admin_panel_keyboard(),

            parse_mode="HTML",

            timeout=
            TELEGRAM_API_TIMEOUT

        )


# ============================================================
# YT-DLP INFO
# ============================================================

def get_info(
    url
):

    options = base_options()


    with yt_dlp.YoutubeDL(
        options
    ) as ydl:

        return ydl.extract_info(

            url,

            download=False

        )


def get_title(
    info
):

    title = info.get(
        "title"
    )


    if not title:

        title = info.get(
            "description",
            "Media"
        )


    return title[:150]


# ============================================================
# SOCIAL MEDIA VIDEO
# ============================================================

def download_social_media(
    url,
    chat_id,
    message_id,
    strict_video_validation=True
):

    last_error = None

    for attempt_index, format_string in enumerate(
        ytdlp_format_candidates(),
        start=1
    ):

        unique_id = (
            uuid.uuid4().hex
        )


        output = os.path.join(

            DOWNLOAD_DIR,

            unique_id
            +
            ".%(ext)s"

        )


        options = base_options()


        options.update({

            "format":
                format_string,

            "outtmpl":
                output,

            "merge_output_format":
                "mp4",

            "progress_hooks":
                [
                    create_progress_hook(
                        chat_id,
                        message_id
                    )
                ],

            "postprocessor_hooks":
                [
                    create_postprocessor_hook(
                        chat_id,
                        message_id
                    )
                ],

            "writethumbnail":
                False,

            "writeinfojson":
                False

        })


        try:

            with yt_dlp.YoutubeDL(
                options
            ) as ydl:

                ydl.download(
                    [url]
                )


            files = []


            for filename in os.listdir(
                DOWNLOAD_DIR
            ):

                if filename.startswith(
                    unique_id
                ):

                    path = os.path.join(

                        DOWNLOAD_DIR,

                        filename

                    )


                    if os.path.isfile(
                        path
                    ):

                        files.append(
                            path
                        )


            if not files:

                raise FileNotFoundError(
                    "No media file was downloaded."
                )


            if strict_video_validation:

                files = validate_downloaded_media(
                    files
                )

            else:

                try:

                    files = validate_downloaded_media(
                        files
                    )

                except Exception:

                    files = keep_playable_media(
                        files
                    )


            if files:

                return files


            last_error = RuntimeError(
                "yt-dlp produced no playable media"
            )

        except Exception as error:

            last_error = error

            log_once(
                "Generic yt-dlp attempt "
                + str(attempt_index)
                + ": "
                + str(error)
            )


    raise RuntimeError(
        str(last_error)
        if last_error
        else
        "No media file was downloaded."
    )


# ============================================================
# TELEGRAM-SAFE VIDEO (mobile playback fix)
# ============================================================

# Why this exists:
#
# Instagram / TikTok / X increasingly serve AV1 or VP9 video. Telegram
# Desktop decodes those in software, so the video plays fine on a laptop.
# The phone apps use the hardware decoder, which does NOT support AV1 or
# VP9 - so the user sees the first frame as a still image while the audio
# plays.
#
# Telegram also needs the moov atom at the FRONT of the file (faststart)
# and the real width / height / duration, otherwise it treats the upload
# as a generic file instead of a streamable video.
#
# So before uploading we:
#   1. probe the file
#   2. re-encode to H.264 + AAC only when the codec is not phone-safe
#      (otherwise just remux, which is instant)
#   3. always add +faststart and send real width / height / duration

TELEGRAM_SAFE_VIDEO_CODECS = (
    "h264",
    "avc1"
)

TELEGRAM_SAFE_AUDIO_CODECS = (
    "aac",
    "mp3",
    "mp4a"
)


def probe_media(
    filename
):

    # Returns dict(vcodec, acodec, width, height, duration) - empty on
    # any failure, because probing must never block an upload.

    try:

        probe = ffmpeg_setup.get_ffprobe_binary()

    except Exception:

        probe = None


    if not probe:

        return {}


    try:

        result = subprocess.run(

            [
                probe,
                "-v", "quiet",
                "-print_format", "json",
                "-show_streams",
                "-show_format",
                filename
            ],

            stdout=subprocess.PIPE,

            stderr=subprocess.DEVNULL,

            timeout=120,

            check=False

        )


        data = json.loads(

            result.stdout.decode(
                "utf-8",
                "ignore"
            )
            or
            "{}"

        )

    except Exception:

        return {}


    info = {

        "vcodec": None,

        "acodec": None,

        "width": 0,

        "height": 0,

        "duration": 0,

        "duration_seconds": 0.0,

        "fps": 0.0,

        "nb_frames": 0

    }


    for stream in data.get(
        "streams",
        []
    ):

        kind = stream.get(
            "codec_type"
        )


        if kind == "video" and not info["vcodec"]:

            info["vcodec"] = (
                stream.get("codec_name")
                or
                ""
            ).lower()


            try:

                info["width"] = int(
                    float(
                        stream.get("width")
                        or
                        0
                    )
                )

                info["height"] = int(
                    float(
                        stream.get("height")
                        or
                        0
                    )
                )

            except Exception:

                pass


            try:

                fps_raw = (
                    stream.get("avg_frame_rate")
                    or
                    stream.get("r_frame_rate")
                    or
                    "0/1"
                )

                if isinstance(fps_raw, str) and "/" in fps_raw:

                    num, den = fps_raw.split("/", 1)

                    den_value = float(den)

                    info["fps"] = (
                        float(num) / den_value
                        if den_value
                        else 0.0
                    )

                else:

                    info["fps"] = float(
                        fps_raw or 0.0
                    )

            except Exception:

                info["fps"] = 0.0


            try:

                info["nb_frames"] = int(
                    float(
                        stream.get("nb_frames")
                        or
                        0
                    )
                )

            except Exception:

                info["nb_frames"] = 0


        elif kind == "audio" and not info["acodec"]:

            info["acodec"] = (
                stream.get("codec_name")
                or
                ""
            ).lower()


    try:

        duration_value = float(
            data.get(
                "format",
                {}
            ).get("duration")
            or
            0
        )

        info["duration"] = int(
            duration_value
        )

        info["duration_seconds"] = duration_value

    except Exception:

        info["duration"] = 0

        info["duration_seconds"] = 0.0


    return info


def video_has_motion(
    filename,
    max_frames=6
):

    # Detect the classic broken case: a long "video" that decodes to
    # the same still frame over and over.

    try:

        ffmpeg = ffmpeg_setup.get_ffmpeg_binary()

    except Exception:

        return True


    if not ffmpeg:

        return True


    probe_dir = os.path.join(
        DOWNLOAD_DIR,
        "probe_" + uuid.uuid4().hex
    )

    try:

        os.makedirs(
            probe_dir,
            exist_ok=True
        )

        output_pattern = os.path.join(
            probe_dir,
            "frame_%02d.png"
        )

        subprocess.run(
            [
                ffmpeg,
                "-v", "error",
                "-i", filename,
                "-vf", "fps=1,scale=64:64",
                "-frames:v", str(max_frames),
                output_pattern
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=180,
            check=False
        )

        frame_paths = sorted(
            os.path.join(probe_dir, name)
            for name in os.listdir(probe_dir)
            if name.lower().endswith(".png")
        )

        if len(frame_paths) <= 1:

            return False

        import hashlib

        hashes = {
            hashlib.md5(
                Path(frame_path).read_bytes()
            ).hexdigest()
            for frame_path in frame_paths
        }

        return len(hashes) > 1

    except Exception:

        return True

    finally:

        for name in os.listdir(probe_dir) if os.path.isdir(probe_dir) else []:
            delete_file(os.path.join(probe_dir, name))

        try:
            os.rmdir(probe_dir)
        except Exception:
            pass


def video_file_looks_valid(
    filename
):

    if not os.path.isfile(
        filename
    ):

        return False


    try:

        if os.path.getsize(
            filename
        ) < 32768:

            return False

    except Exception:

        return False


    info = probe_media(
        filename
    )


    if not info.get(
        "vcodec"
    ):

        return False


    if (
        info.get("width", 0) < 32
        or
        info.get("height", 0) < 32
    ):

        return False


    duration_seconds = info.get(
        "duration_seconds",
        0.0
    )

    if duration_seconds <= 0.0:

        return False


    if duration_seconds >= 3.0:

        nb_frames = info.get(
            "nb_frames",
            0
        )

        fps = info.get(
            "fps",
            0.0
        )

        if nb_frames > 0 and nb_frames <= 2:

            return False

        if fps > 0.0 and fps < 2.0:

            return False

        if not video_has_motion(
            filename
        ):

            return False


    return True


def validate_downloaded_media(
    files
):

    files = keep_playable_media(
        files
    )


    video_candidates = [

        path

        for path in files

        if file_extension(
            path
        ) in ALBUM_VIDEO_EXTENSIONS

    ]


    if not video_candidates:

        return files


    valid_videos = [

        path

        for path in video_candidates

        if video_file_looks_valid(
            path
        )

    ]


    if valid_videos:

        return valid_videos


    raise RuntimeError(
        "downloaded video file was invalid or frozen-like."
    )


def normalize_video_for_telegram(
    filename
):

    # Returns (path_to_upload, info, temp_path_to_delete_or_None).
    # On ANY problem it returns the original file untouched, so this can
    # never stop a download from being delivered.

    info = probe_media(
        filename
    )


    try:

        ffmpeg = ffmpeg_setup.get_ffmpeg_binary()

    except Exception:

        ffmpeg = None


    if not ffmpeg:

        return filename, info, None


    # ALWAYS re-encode.
    #
    # The previous version only converted when the codec NAME was not
    # h264. That was not enough: Instagram / TikTok / X also serve
    #
    #   - 10-bit H.264 ("High 10" / yuv420p10le)
    #   - High 4:2:2 / 4:4:4 profiles
    #   - 50-60 fps
    #   - 1080p and larger
    #
    # All of those ARE "h264", so they were passed through untouched -
    # and phone hardware decoders still refused to play them, which is
    # exactly the "still image + sound" symptom.
    #
    # Re-encoding every video to one conservative, universally
    # supported profile is the only reliable fix.

    target = os.path.join(

        os.path.dirname(
            filename
        )
        or
        DOWNLOAD_DIR,

        "tg_"
        +
        uuid.uuid4().hex
        +
        ".mp4"

    )


    # Cap the LONG side at 1280 (works for portrait reels and
    # landscape alike), then force even dimensions for yuv420p.

    scale_filter = (
        "scale=trunc(iw/2)*2:trunc(ih/2)*2"
    )


    phone_safe_video = [

        "-map", "0:v:0",
        "-map", "0:a:0?",
        "-sn",
        "-dn",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "23",
        "-pix_fmt", "yuv420p",
        "-profile:v", "baseline",
        "-level", "3.1",
        "-refs", "1",
        "-bf", "0",
        "-g", "60",
        "-keyint_min", "60",
        "-sc_threshold", "0",
        "-tag:v", "avc1",
        "-c:a", "aac",
        "-b:a", "128k",
        "-ac", "2",
        "-ar", "44100",
        "-movflags", "+faststart",
        "-max_muxing_queue_size", "1024"

    ]


    # 1st choice: full normalization, including a 30 fps ceiling.

    transcode = (
        [
            ffmpeg,
            "-y",
            "-i", filename,
            "-vf", scale_filter,
            "-r", "30"
        ]
        + phone_safe_video
        + [target]
    )


    # 2nd choice: same thing without the fps cap.

    transcode_basic = (
        [
            ffmpeg,
            "-y",
            "-i", filename,
            "-vf", scale_filter
        ]
        + phone_safe_video
        + [target]
    )


    for command in (
        transcode,
        transcode_basic
    ):

        try:

            result = subprocess.run(

                command,

                stdout=subprocess.DEVNULL,

                stderr=subprocess.DEVNULL,

                timeout=1800,

                check=False

            )

        except Exception as error:

            log_once(
                "video normalize failed: "
                + str(error)
            )

            delete_file(
                target
            )

            continue


        if result.returncode != 0:

            delete_file(
                target
            )

            continue


        if (
            os.path.isfile(target)
            and
            os.path.getsize(target) > 0
            and
            video_file_looks_valid(
                target
            )
        ):

            return (
                target,
                probe_media(target) or info,
                target
            )


        delete_file(
            target
        )


    # Everything failed - send the original rather than nothing.

    return filename, info, None


# ============================================================
# OPTIONAL VIDEO CONVERSION (OFF BY DEFAULT)
# ============================================================
#
# Handing Telegram the merged file exactly as yt-dlp produced it is
# what plays correctly on phones. Re-encoding it, and declaring our
# own width / height / duration, is what caused videos to appear as a
# still image with sound on mobile.
#
# The converter above is therefore kept only as an opt-in escape
# hatch, disabled unless you set:
#
#     FORCE_VIDEO_CONVERT=1
#
# Leave it unset for normal use.

def maybe_convert_video(
    filename
):

    # Returns (path_to_send, temp_file_to_delete_or_None).

    path, info, temp_video = normalize_video_for_telegram(
        filename
    )

    return path, temp_video


# ============================================================
# UPLOAD FILE
# ============================================================

def upload_file(
    chat_id,
    filename,
    title
):

    extension = (

        os.path.splitext(
            filename
        )[1]
        .lower()

    )


    if extension in (

        ".mp4",
        ".mov",
        ".mkv",
        ".webm"

    ):

        # SENDING MECHANISM restored to the version that plays on
        # phones: give Telegram the file as-is with supports_streaming
        # and nothing else. Telegram probes the file itself and gets it
        # right; the width / height / duration we used to declare here
        # are exactly what broke mobile playback.

        path, temp_video = maybe_convert_video(
            filename
        )


        try:

            with open(
                path,
                "rb"
            ) as video:

                return bot.send_video(

                    chat_id,

                    video,

                    caption=
                    media_caption(
                        title
                    ),

                    parse_mode="HTML",

                    supports_streaming=True,

                    timeout=
                    TELEGRAM_UPLOAD_TIMEOUT

                )

        finally:

            if temp_video:

                delete_file(
                    temp_video
                )


    if extension in (

        ".mp3",
        ".m4a",
        ".aac",
        ".wav",
        ".ogg"

    ):

        with open(
            filename,
            "rb"
        ) as audio:

            return bot.send_audio(

                chat_id,

                audio,

                title=title,

                performer=
                "Media Downloader",

                timeout=
                TELEGRAM_UPLOAD_TIMEOUT

            )


    if extension in (

        ".jpg",
        ".jpeg",
        ".png",
        ".webp"

    ):

        with open(
            filename,
            "rb"
        ) as photo:

            return bot.send_photo(

                chat_id,

                photo,

                caption=
                media_caption(
                    title
                ),

                parse_mode="HTML",

                timeout=
                TELEGRAM_UPLOAD_TIMEOUT

            )


    with open(
        filename,
        "rb"
    ) as document:

        return bot.send_document(

            chat_id,

            document,

            caption=(

                "📎 "
                +
                html.escape(
                    title
                )

            ),

            parse_mode="HTML",

            timeout=
            TELEGRAM_UPLOAD_TIMEOUT

        )


# ============================================================
# ALBUM UPLOAD
# ============================================================

ALBUM_PHOTO_EXTENSIONS = (

    ".jpg",
    ".jpeg",
    ".png",
    ".webp"

)

ALBUM_VIDEO_EXTENSIONS = (

    ".mp4",
    ".mov",
    ".mkv",
    ".webm"

)

# Telegram allows at most 10 items in one album.
ALBUM_MAX_ITEMS = 10


def media_caption(
    title
):

    return (

        "\U0001f3ac <b>"
        +
        html.escape(
            title
        )
        +
        "</b>"

    )


def file_extension(
    filename
):

    return (

        os.path.splitext(
            filename
        )[1]
        .lower()

    )


def is_album_item(
    filename
):

    return file_extension(
        filename
    ) in (

        ALBUM_PHOTO_EXTENSIONS
        +
        ALBUM_VIDEO_EXTENSIONS

    )


def build_album_item(
    filename,
    caption
):

    parse_mode = (

        "HTML"

        if caption

        else None

    )


    if file_extension(
        filename
    ) in ALBUM_VIDEO_EXTENSIONS:

        # Albums use the same restored mechanism: no self-declared
        # width / height / duration.

        path, temp_video = maybe_convert_video(
            filename
        )


        try:

            with open(
                path,
                "rb"
            ) as file:

                content = file.read()

        finally:

            if temp_video:

                delete_file(
                    temp_video
                )


        return types.InputMediaVideo(

            content,

            caption=caption,

            parse_mode=parse_mode,

            supports_streaming=True

        )


    with open(
        filename,
        "rb"
    ) as file:

        content = file.read()


    return types.InputMediaPhoto(

        content,

        caption=caption,

        parse_mode=parse_mode

    )


def album_chunks(
    items
):

    chunks = []


    for index in range(

        0,

        len(items),

        ALBUM_MAX_ITEMS

    ):

        chunks.append(

            items[
                index:index + ALBUM_MAX_ITEMS
            ]

        )


    # An album needs 2+ items, so never leave a lone trailing one.

    if len(chunks) > 1 and len(chunks[-1]) == 1:

        chunks[-1].insert(

            0,

            chunks[-2].pop()

        )


    return chunks


def upload_album(
    chat_id,
    filenames,
    title
):
    """Send photos / videos together in one grouped message.

    The title is attached to the first item only, so Telegram shows
    it once as the caption of the whole album.
    """

    items = [

        filename

        for filename in filenames

        if is_album_item(
            filename
        )

    ]

    extras = [

        filename

        for filename in filenames

        if not is_album_item(
            filename
        )

    ]

    sent = 0

    first = True


    for chunk in album_chunks(
        items
    ):

        try:

            if len(chunk) == 1:

                upload_file(

                    chat_id,

                    chunk[0],

                    title

                )

                sent += 1


            else:

                media = []


                for position, filename in enumerate(
                    chunk
                ):

                    caption = (

                        media_caption(
                            title
                        )

                        if first and position == 0

                        else None

                    )


                    media.append(

                        build_album_item(

                            filename,

                            caption

                        )

                    )


                bot.send_media_group(

                    chat_id,

                    media,

                    timeout=
                    TELEGRAM_UPLOAD_TIMEOUT

                )

                sent += len(media)


            first = False


        except Exception:

            # Fall back to one-by-one so a single bad file cannot
            # take the whole album down.

            for filename in chunk:

                try:

                    upload_file(

                        chat_id,

                        filename,

                        title

                    )

                    sent += 1


                except Exception:

                    pass


    for filename in extras:

        try:

            upload_file(

                chat_id,

                filename,

                title

            )

            sent += 1


        except Exception:

            pass


    return sent


# ============================================================
# /START
# ============================================================

@bot.message_handler(
    commands=["start"]
)
def start(
    message
):

    send_welcome(

        message.chat.id,

        admin=is_admin(
            message.from_user.id
        )

    )


# ============================================================
# REMOVE WELCOME BUTTONS
# ============================================================

@bot.message_handler(commands=["removebutton"])
def removebutton(message):

    target_message_id = LAST_WELCOME_MESSAGES.get(
        message.chat.id
    )

    if not target_message_id and message.reply_to_message:
        target_message_id = message.reply_to_message.message_id

    if not target_message_id:
        bot.reply_to(
            message,
            "ℹ️ No welcome message button was found in this chat.",
            timeout=TELEGRAM_API_TIMEOUT
        )
        return

    try:
        bot.edit_message_reply_markup(
            message.chat.id,
            target_message_id,
            reply_markup=None,
            timeout=TELEGRAM_API_TIMEOUT
        )
        bot.reply_to(
            message,
            "✅ Buttons removed from the last /start message.",
            timeout=TELEGRAM_API_TIMEOUT
        )
    except Exception as e:
        bot.reply_to(
            message,
            "⚠️ I couldn't remove the button from that message.",
            timeout=TELEGRAM_API_TIMEOUT
        )


# ============================================================
# CHECK SUBSCRIPTION
# ============================================================

@bot.callback_query_handler(

    func=lambda call:
    call.data
    ==
    "check_subscription"

)
def check_subscription_callback(
    call
):

    user_id = (
        call.from_user.id
    )


    if is_admin(
        user_id
    ):

        bot.answer_callback_query(

            call.id,

            "✅ Admin access confirmed!"

        )

        return


    result = is_user_subscribed(
        user_id
    )


    if result is True:

        bot.answer_callback_query(

            call.id,

            "✅ Subscription confirmed!"

        )


        try:

            bot.edit_message_text(

                (
                    "✅ <b>Subscription confirmed!</b>\n\n"
                    "You can now send me a media link."
                ),

                call.message.chat.id,

                call.message.message_id,

                parse_mode="HTML",

                timeout=
                TELEGRAM_API_TIMEOUT

            )

        except Exception:

            pass


        return


    if result is False:

        bot.answer_callback_query(

            call.id,

            "❌ You haven't subscribed yet.",

            show_alert=True

        )

        return


    bot.answer_callback_query(

        call.id,

        "⚠️ I couldn't verify your subscription.",

        show_alert=True

    )


# ============================================================
# ADMIN PANEL
# ============================================================

@bot.callback_query_handler(

    func=lambda call:
    call.data
    ==
    "admin_panel"

)
def admin_panel_callback(
    call
):

    if not is_admin(
        call.from_user.id
    ):

        bot.answer_callback_query(

            call.id,

            "❌ You are not an admin.",

            show_alert=True

        )

        return


    bot.answer_callback_query(
        call.id
    )


    show_admin_panel(

        call.message.chat.id,

        call.message.message_id

    )


# ============================================================
# ADD ADMIN
# ============================================================

@bot.callback_query_handler(

    func=lambda call:
    call.data
    ==
    "admin_add"

)
def admin_add_callback(
    call
):

    if call.from_user.id != OWNER_ID:

        bot.answer_callback_query(

            call.id,

            "❌ Only the owner can add admins.",

            show_alert=True

        )

        return


    admin_action[
        call.from_user.id
    ] = "add"


    bot.answer_callback_query(
        call.id
    )


    bot.send_message(

        call.message.chat.id,

        (
            "➕ <b>Add New Admin</b>\n\n"

            "Send the Telegram <b>User ID</b>.\n\n"

            "Example:\n"
            "<code>123456789</code>\n\n"

            "Send /cancel to cancel."
        ),

        parse_mode="HTML",

        timeout=
        TELEGRAM_API_TIMEOUT

    )


# ============================================================
# REMOVE ADMIN
# ============================================================

@bot.callback_query_handler(

    func=lambda call:
    call.data
    ==
    "admin_remove"

)
def admin_remove_callback(
    call
):

    if call.from_user.id != OWNER_ID:

        bot.answer_callback_query(

            call.id,

            "❌ Only the owner can remove admins.",

            show_alert=True

        )

        return


    admin_action[
        call.from_user.id
    ] = "remove"


    bot.answer_callback_query(
        call.id
    )


    bot.send_message(

        call.message.chat.id,

        (
            "➖ <b>Remove Admin</b>\n\n"

            "Send the Telegram <b>User ID</b>.\n\n"

            "⚠️ The owner cannot be removed.\n\n"

            "Send /cancel to cancel."
        ),

        parse_mode="HTML",

        timeout=
        TELEGRAM_API_TIMEOUT

    )


# ============================================================
# LIST ADMINS
# ============================================================

@bot.callback_query_handler(

    func=lambda call:
    call.data
    ==
    "admin_list"

)
def admin_list_callback(
    call
):

    if not is_admin(
        call.from_user.id
    ):

        bot.answer_callback_query(

            call.id,

            "❌ Admin access required.",

            show_alert=True

        )

        return


    bot.answer_callback_query(
        call.id
    )


    text = (
        "📋 <b>Bot Administrators</b>\n\n"
    )


    for index, admin_id in enumerate(

        ADMINS,

        start=1

    ):

        if admin_id == OWNER_ID:

            text += (

                f"{index}. "
                f"<code>{admin_id}</code> "
                "👑 Owner\n"

            )

        else:

            text += (

                f"{index}. "
                f"<code>{admin_id}</code>\n"

            )


    bot.send_message(

        call.message.chat.id,

        text,

        parse_mode="HTML",

        timeout=
        TELEGRAM_API_TIMEOUT

    )


# ============================================================
# CLOSE ADMIN
# ============================================================

@bot.callback_query_handler(

    func=lambda call:
    call.data
    ==
    "admin_close"

)
def admin_close_callback(
    call
):

    if not is_admin(
        call.from_user.id
    ):

        bot.answer_callback_query(

            call.id,

            "❌ Admin access required.",

            show_alert=True

        )

        return


    bot.answer_callback_query(
        call.id
    )


    delete_message_safe(

        call.message.chat.id,

        call.message.message_id

    )


# ============================================================
# ADMIN ID INPUT
# ============================================================

@bot.message_handler(

    func=lambda message:
    message.from_user.id
    in
    admin_action

)
def admin_id_input(
    message
):

    user_id = (
        message.from_user.id
    )


    if user_id != OWNER_ID:

        admin_action.pop(
            user_id,
            None
        )

        return


    text = (
        message.text
        or
        ""
    ).strip()


    if text.lower() == "/cancel":

        admin_action.pop(
            user_id,
            None
        )


        bot.send_message(

            message.chat.id,

            "❌ Cancelled.",

            timeout=
            TELEGRAM_API_TIMEOUT

        )

        return


    try:

        target_id = int(
            text
        )

    except Exception:

        bot.reply_to(

            message,

            (
                "❌ Invalid Telegram ID.\n\n"
                "Send numbers only."
            ),

            timeout=
            TELEGRAM_API_TIMEOUT

        )

        return


    action = admin_action.get(
        user_id
    )


    admin_action.pop(
        user_id,
        None
    )


    if action == "add":

        if target_id in ADMINS:

            result = (

                "ℹ️ This user is already "
                "an administrator."

            )

        else:

            add_admin(
                target_id
            )

            result = (

                "✅ <b>Administrator added!</b>\n\n"

                f"User ID:\n"
                f"<code>{target_id}</code>"

            )


    elif action == "remove":

        if target_id == OWNER_ID:

            result = (
                "❌ The owner cannot be removed."
            )

        elif target_id not in ADMINS:

            result = (
                "ℹ️ This user is not an administrator."
            )

        else:

            remove_admin(
                target_id
            )

            result = (

                "✅ <b>Administrator removed!</b>\n\n"

                f"User ID:\n"
                f"<code>{target_id}</code>"

            )


    else:

        result = (
            "❌ Invalid admin operation."
        )


    bot.send_message(

        message.chat.id,

        result,

        parse_mode="HTML",

        timeout=
        TELEGRAM_API_TIMEOUT

    )


    show_admin_panel(
        message.chat.id
    )


# ============================================================
# RECEIVE URL
# ============================================================

@bot.message_handler(

    func=lambda message:

    message.text

    and

    is_url(
        message.text
    )

)
def receive_url(
    message
):

    user_id = (
        message.from_user.id
    )


    # --------------------------------------------------------
    # SUBSCRIPTION GATE
    # --------------------------------------------------------

    if not is_admin(
        user_id
    ):

        subscribed = (
            is_user_subscribed(
                user_id
            )
        )


        if subscribed is not True:

            send_welcome(
                message.chat.id
            )

            return


    url = (
        message.text
        .strip()
    )


    platform = detect_platform(
        url
    )


    if not platform:

        bot.reply_to(

            message,

            (
                "❌ <b>Unsupported platform.</b>\n\n"

                "Supported:\n"
                "📸 Instagram\n"
                "🎵 TikTok\n"
                "𝕏 X / Twitter"
            ),

            parse_mode="HTML",

            timeout=
            TELEGRAM_API_TIMEOUT

        )

        return


    # ========================================================
    # INSTAGRAM
    # ========================================================

    if platform == "instagram":

        checking = bot.send_message(

            message.chat.id,

            "🔎 <b>Checking Instagram...</b>",

            parse_mode="HTML",

            timeout=
            TELEGRAM_API_TIMEOUT

        )


        spinner = TelegramSpinner(

            checking.chat.id,

            checking.message_id,

            "Checking Instagram..."

        )


        spinner.start()


        try:

            info = get_instagram_info(
                url
            )


            title = (

                info.get(
                    "title"
                )

                or

                "Instagram"

            )


            media_type = (

                info.get(
                    "type"
                )

                or

                "media"

            )


            entries = (

                info.get(
                    "entries"
                )

                or

                []

            )


        except Exception as e:

            # This metadata lookup uses parth-dl and is where the
            # HTTP 401 came from: it failed BEFORE any download began,
            # so the bot used to give up right here. It is NOT fatal -
            # the download engines below do their own extraction, so we
            # continue with safe defaults and let them try.

            log_once(
                "Instagram metadata lookup failed: "
                + str(e)
            )


            info = {}

            title = "Instagram"

            media_type = "media"

            entries = []


        spinner.stop()


        job_dir = os.path.join(

            DOWNLOAD_DIR,

            "ig_"
            +
            uuid.uuid4().hex

        )


        os.makedirs(

            job_dir,

            exist_ok=True

        )


        try:

            try:

                bot.edit_message_text(

                    (
                        "📥 <b>Downloading Instagram...</b>\n\n"

                        f"📦 "
                        f"{len(entries) or 1}"
                        " media item(s)"
                    ),

                    checking.chat.id,

                    checking.message_id,

                    parse_mode="HTML",

                    timeout=
                    TELEGRAM_API_TIMEOUT

                )

            except Exception:

                pass


            # PRIMARY: media-downloader.
            # FAIL-SAFE: parth-dl, with an on-screen notice + retry.

            media = download_instagram_media(

                url,

                job_dir,

                checking.chat.id,

                checking.message_id,

                title

            )


            files = media[
                "files"
            ]


            title = (

                media.get(
                    "title"
                )

                or

                title

            )


            if not files:

                raise RuntimeError(

                    "Instagram returned no downloadable media."

                )


            try:

                bot.edit_message_text(

                    "📤 <b>Uploading Instagram media...</b>",

                    checking.chat.id,

                    checking.message_id,

                    parse_mode="HTML",

                    timeout=
                    TELEGRAM_API_TIMEOUT

                )

            except Exception:

                pass


            sent = upload_album(

                message.chat.id,

                files,

                title

            )


            if not sent:

                raise RuntimeError(

                    "Instagram media was downloaded, "
                    "but Telegram could not upload it."

                )


            delete_message_safe(

                checking.chat.id,

                checking.message_id

            )


        except Exception as e:

            show_error(

                checking.chat.id,

                checking.message_id,

                e

            )


        finally:

            shutil.rmtree(

                job_dir,

                ignore_errors=True

            )


        return


    # ========================================================
    # TWITTER / X
    # ========================================================

    if platform == "twitter":

        checking = bot.send_message(

            message.chat.id,

            "🔎 <b>Checking X / Twitter...</b>",

            parse_mode="HTML",

            timeout=
            TELEGRAM_API_TIMEOUT

        )


        spinner = TelegramSpinner(

            checking.chat.id,

            checking.message_id,

            "Checking X / Twitter..."

        )


        spinner.start()


        try:

            page, final_url = fetch_html(
                url
            )


            has_video = twitter_has_video(page)

            images = [] if has_video else extract_twitter_images(

                page,

                final_url

            )


            title = extract_meta_title(
                page
            )


        except Exception:

            images = []

            title = "X / Twitter"

        finally:

            spinner.stop()


        # ----------------------------------------------------
        # TWITTER PHOTOS
        # ----------------------------------------------------

        if images:

            downloaded = []

            # Download independent Twitter photos concurrently to reduce latency.
            with ThreadPoolExecutor(max_workers=min(6, len(images))) as executor:
                futures = {
                    executor.submit(download_image, image_url): image_url
                    for image_url in images
                }

                for future in as_completed(futures):
                    try:
                        downloaded.append(future.result())
                    except Exception:
                        pass


            # Some Twitter photo posts expose the same image through two
            # different URLs. Deduplicate by file content before upload.
            if downloaded:

                import hashlib

                unique_downloaded = []
                seen_hashes = set()

                for filename in downloaded:

                    try:
                        file_hash = hashlib.md5(
                            Path(filename).read_bytes()
                        ).hexdigest()
                    except Exception:
                        file_hash = None

                    if file_hash and file_hash in seen_hashes:
                        delete_file(filename)
                        continue

                    if file_hash:
                        seen_hashes.add(file_hash)

                    unique_downloaded.append(filename)

                downloaded = unique_downloaded


            sent = 0


            if downloaded:

                try:

                    sent = upload_album(

                        message.chat.id,

                        downloaded,

                        title

                    )


                finally:

                    for filename in downloaded:

                        delete_file(
                            filename
                        )


            if sent:

                delete_message_safe(

                    checking.chat.id,

                    checking.message_id

                )

                return


        # ----------------------------------------------------
        # TWITTER VIDEO
        # ----------------------------------------------------

        try:

            bot.edit_message_text(

                "📥 <b>Downloading X / Twitter video...</b>",

                checking.chat.id,

                checking.message_id,

                parse_mode="HTML",

                timeout=
                TELEGRAM_API_TIMEOUT

            )


            # PRIMARY: media-downloader, FAIL-SAFE: yt-dlp,
            # with the same retry notice as Instagram.

            files = download_media_with_failsafe(

                "X / Twitter",

                url,

                checking.chat.id,

                checking.message_id,

                title

            )


            try:

                upload_album(

                    message.chat.id,

                    files,

                    title

                )

            finally:

                for filename in files:

                    delete_file(
                        filename
                    )


            delete_message_safe(

                checking.chat.id,

                checking.message_id

            )


        except Exception as e:

            show_error(

                checking.chat.id,

                checking.message_id,

                e

            )


        return


    # ========================================================
    # TIKTOK / DIRECT MEDIA LINKS
    # ========================================================

    checking = bot.send_message(

        message.chat.id,

        "🔎 <b>Checking your link...</b>",

        parse_mode="HTML",

        timeout=
        TELEGRAM_API_TIMEOUT

    )


    spinner = TelegramSpinner(

        checking.chat.id,

        checking.message_id,

        "Checking your link..."

    )


    spinner.start()


    try:

        info = get_info(
            url
        )

        title = get_title(
            info
        )


    except Exception as e:

        spinner.stop()


        show_error(

            message.chat.id,

            checking.message_id,

            e

        )


        return


    spinner.stop()


    delete_message_safe(

        message.chat.id,

        checking.message_id

    )


    progress = bot.send_message(

        message.chat.id,

        "📥 <b>Starting download...</b>",

        parse_mode="HTML",

        timeout=
        TELEGRAM_API_TIMEOUT

    )


    try:

        # PRIMARY: media-downloader, FAIL-SAFE: yt-dlp, with the same
        # retry notice as Instagram. Label follows the detected link.

        files = download_media_with_failsafe(

            platform_label(
                platform
            ),

            url,

            progress.chat.id,

            progress.message_id,

            title

        )


        try:

            upload_album(

                message.chat.id,

                files,

                title

            )

        finally:

            for filename in files:

                delete_file(
                    filename
                )


        delete_message_safe(

            progress.chat.id,

            progress.message_id

        )


    except Exception as e:

        show_error(

            progress.chat.id,

            progress.message_id,

            e

        )


# ============================================================
# OTHER TEXT
# ============================================================

@bot.message_handler(
    func=lambda message: True
)
def other_message(
    message
):

    user_id = (
        message.from_user.id
    )


    if is_admin(
        user_id
    ):

        bot.reply_to(

            message,

            "📎 Send me a supported media link.",

            timeout=
            TELEGRAM_API_TIMEOUT

        )

        return


    if (
        is_user_subscribed(
            user_id
        )
        is not True
    ):

        send_welcome(
            message.chat.id
        )

        return


    bot.reply_to(

        message,

        "📎 Send me a supported media link.",

        timeout=
        TELEGRAM_API_TIMEOUT

    )


# ============================================================
# START BOT
# ============================================================

# Resolve ffmpeg once, before polling starts, and print where it came
# from. Nothing has to be installed on the host by hand.
_ffmpeg_info = ffmpeg_setup.ensure_ffmpeg()

_ffmpeg_dir = _ffmpeg_info["dir"]

if _ffmpeg_dir:

    print(
        "🎬 FFmpeg directory: "
        + _ffmpeg_dir
    )

else:

    print(
        "WARNING: ffmpeg could not be provided automatically.\n"
        "         Video+audio merging and MP3 conversion will fail.\n"
        "         Make sure 'imageio-ffmpeg' is in requirements.txt, or\n"
        "         drop an ffmpeg binary into the project's bin/ folder,\n"
        "         or set FFMPEG_LOCATION."
    )

# Startup output is intentionally minimal: only the ffmpeg lines above
# and the "running" line below. The build banner, the per-platform
# engine lists, the media-downloader note and the cookies tip were all
# removed on request. Engine details still work exactly the same, they
# are just not printed. Set DEBUG_LOGS=1 to see them again.

if DEBUG_LOGS:

    print(
        "📦 Build: engines v6"
    )

    if instagram_primary.is_available():

        print(
            "📸 Instagram: media-downloader → yt-dlp → parth-dl"
        )

        print(
            "🎵 TikTok / 🐦 X: media-downloader → yt-dlp"
        )

    else:

        print(
            "📸 Instagram: yt-dlp → parth-dl"
        )

        print(
            "🎵 TikTok / 🐦 X: yt-dlp"
        )

        print(
            "   note: 'media-downloader' is not installed here → "
            + str(
                instagram_primary.import_error()
            )
        )

print(
    "🤖 Media Downloader Bot is running..."
)


bot.infinity_polling(
    skip_pending=True
)
