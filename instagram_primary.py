"""
instagram_primary.py
====================

PRIMARY downloader, built on the `media-downloader` library:

    https://knuckles-team.github.io/media-downloader/usage/#as-a-python-api
    pip install media-downloader

Used as the FIRST engine for Instagram, TikTok and X / Twitter. The file
keeps its original name so existing imports do not break, but nothing in
here is Instagram-specific - `download()` accepts any supported URL.

If anything here fails, main.py catches it, tells the user an error
happened, and retries with the next engine as a fail-safe (yt-dlp, then
parth-dl for Instagram). So this module is allowed to fail loudly -
nothing is lost.

Public API:
    is_available()        -> bool, True when the library is installed
    download(...)         -> dict(files=[...], title=..., engine=...)
"""

import os
import time


# The library is optional: if it is not installed, main.py silently
# falls back to the old downloader instead of crashing the bot.
try:
    from media_downloader.media_downloader import MediaDownloader

    _IMPORT_ERROR = None

except Exception as error:  # pragma: no cover - depends on host install
    MediaDownloader = None

    _IMPORT_ERROR = error


ENGINE_NAME = "media-downloader"

MEDIA_EXTENSIONS = (
    ".mp4",
    ".mkv",
    ".webm",
    ".mov",
    ".m4v",
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".gif",
    ".mp3",
    ".m4a",
)

# Files yt-dlp leaves behind mid-download; never hand these to Telegram.
PARTIAL_SUFFIXES = (
    ".part",
    ".ytdl",
    ".temp",
    ".tmp",
)


def is_available():
    """True when the media-downloader library can actually be used."""
    return MediaDownloader is not None


def import_error():
    return _IMPORT_ERROR


def _list_media(directory):
    """Every finished media file inside `directory`, newest last."""
    found = []

    for root, _dirs, files in os.walk(directory):
        for name in files:
            lowered = name.lower()

            if lowered.endswith(PARTIAL_SUFFIXES):
                continue

            if not lowered.endswith(MEDIA_EXTENSIONS):
                continue

            path = os.path.join(root, name)

            if os.path.isfile(path) and os.path.getsize(path) > 0:
                found.append(path)

    found.sort(key=lambda item: os.path.getmtime(item))

    return found


def _build_downloader(url, output_path, audio):
    """
    Construct MediaDownloader the documented way, with tolerant
    fallbacks so a signature change in the library cannot break us.
    """
    attempts = (
        lambda: MediaDownloader(
            links=[url],
            download_directory=output_path,
            audio=audio,
        ),
        lambda: MediaDownloader(
            links=[url],
            download_directory=output_path,
        ),
        lambda: MediaDownloader(
            download_directory=output_path,
        ),
        lambda: MediaDownloader(),
    )

    last_error = None

    for build in attempts:
        try:
            return build()
        except TypeError as error:
            last_error = error
        except Exception as error:
            last_error = error

    raise RuntimeError(
        "Could not construct MediaDownloader: " + str(last_error)
    )


def _attach_progress(downloader, progress_callback):
    """
    Wire the library's progress callback to our Telegram UI.
    The documented callback signature is cb(progress, total=100).
    """
    if not progress_callback:
        return

    if not hasattr(downloader, "set_progress_callback"):
        return

    def on_progress(progress, total=100):
        try:
            if not total:
                total = 100

            percent = float(progress) * 100.0 / float(total)

            progress_callback(
                max(0.0, min(100.0, percent))
            )
        except Exception:
            # Progress is cosmetic - never let it kill a download.
            pass

    try:
        downloader.set_progress_callback(on_progress)
    except Exception:
        pass


def _run_download(downloader, url):
    """
    Trigger the download. Prefer download_all() as documented, and fall
    back to download_video(url) when the queue-based call is not
    available or returned nothing.
    """
    result = None

    if hasattr(downloader, "download_all"):
        try:
            result = downloader.download_all()
        except Exception:
            result = None

    if not result and hasattr(downloader, "download_video"):
        # Raise from here: a genuine failure must reach main.py so the
        # fail-safe downloader takes over.
        result = downloader.download_video(url)

    return result


def _collect_files(result, output_path, known_before):
    """
    Work out which files we actually produced. The library may return a
    path, a list of paths, or something else entirely, so the directory
    listing is the source of truth.
    """
    files = []

    if isinstance(result, str):
        if os.path.isfile(result):
            files.append(result)

    elif isinstance(result, (list, tuple, set)):
        for item in result:
            if isinstance(item, str) and os.path.isfile(item):
                files.append(item)

    # Anything new on disk counts too.
    for path in _list_media(output_path):
        if path not in known_before and path not in files:
            files.append(path)

    # De-duplicate while keeping order.
    unique = []

    for path in files:
        if path not in unique:
            unique.append(path)

    return unique


def download(
    url,
    output_path,
    progress_callback=None,
    audio=False,
    settle_seconds=1.0,
):
    """
    Download an Instagram / TikTok / X-Twitter URL with media-downloader.

    Returns dict(files=[paths], title=str|None, engine=str).
    Raises on failure so the caller can run the fail-safe downloader.
    """
    if MediaDownloader is None:
        raise RuntimeError(
            "media-downloader is not installed ("
            + str(_IMPORT_ERROR)
            + "). Add 'media-downloader' to requirements.txt."
        )

    os.makedirs(output_path, exist_ok=True)

    known_before = set(_list_media(output_path))

    downloader = _build_downloader(url, output_path, audio)

    _attach_progress(downloader, progress_callback)

    result = _run_download(downloader, url)

    # Give the library a moment to finish renaming .part files.
    if settle_seconds:
        time.sleep(settle_seconds)

    files = _collect_files(result, output_path, known_before)

    if not files:
        raise RuntimeError(
            "media-downloader finished without producing a media file."
        )

    title = None

    for attribute in ("title", "video_title", "name"):
        value = getattr(downloader, attribute, None)

        if isinstance(value, str) and value.strip():
            title = value.strip()
            break

    if not title:
        title = os.path.splitext(os.path.basename(files[0]))[0]

    return {
        "files": files,
        "title": title,
        "engine": ENGINE_NAME,
    }
