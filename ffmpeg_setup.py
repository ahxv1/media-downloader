"""
ffmpeg_setup.py
===============

ZERO-CONFIG FFMPEG FOR ANY HOST.

You never have to install or download ffmpeg by hand again.
This module finds (or provides) ffmpeg + ffprobe automatically, in this
order, and stops at the first thing that works:

  1. FFMPEG_LOCATION env var          (folder or the binary itself)
  2. ffmpeg already on the system PATH (Docker / apt / VPS)
  3. Binaries you dropped in ./bin or ./vendor/ffmpeg inside this project
  4. PIP-PROVIDED BINARIES  <-- the important one
       imageio-ffmpeg / ffmpeg-binaries / static-ffmpeg wheels already
       CONTAIN the ffmpeg executable. They are installed during the
       host's "pip install -r requirements.txt" build step, which hosts
       do not kill, and which is cached between deploys.
       => no runtime download, nothing to interrupt.
  5. LAST RESORT: a *resumable* download of a static build.
       Downloads in small chunks to a .part file and resumes with an
       HTTP Range request if the host kills it. Restarting the bot
       continues where it stopped instead of starting over.

Everything is cached, so this costs nothing after the first start.

Public API:
    ensure_ffmpeg()        -> dict(dir=..., ffmpeg=..., ffprobe=...)
    get_ffmpeg_location()  -> folder to hand to yt-dlp, or None
    ffmpeg_is_available()  -> bool
"""

import os
import sys
import shutil
import stat
import subprocess
import tarfile
import zipfile


IS_WINDOWS = os.name == "nt"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Where we place / link the binaries we end up using.
LOCAL_BIN_DIR = (
    os.getenv("FFMPEG_CACHE_DIR")
    or os.path.join(BASE_DIR, ".ffmpeg")
)

QUIET = os.getenv("FFMPEG_SETUP_QUIET", "").strip().lower() in (
    "1", "true", "yes"
)

# Set FFMPEG_ALLOW_DOWNLOAD=0 to forbid step 5 entirely.
ALLOW_DOWNLOAD = os.getenv("FFMPEG_ALLOW_DOWNLOAD", "1").strip().lower() not in (
    "0", "false", "no"
)

COMMON_DIRS = (
    os.path.join(BASE_DIR, "bin"),
    os.path.join(BASE_DIR, "vendor", "ffmpeg"),
    os.path.join(BASE_DIR, "vendor", "ffmpeg", "bin"),
    LOCAL_BIN_DIR,
    "/usr/bin",
    "/usr/local/bin",
    "/bin",
    "/opt/homebrew/bin",
    "/snap/bin",
    "/app/vendor/ffmpeg/bin",
    "/layers/ffmpeg/bin",
    "/opt/ffmpeg/bin",
    "C:\\ffmpeg\\bin",
    "C:\\Program Files\\ffmpeg\\bin",
)

_CACHE = None


# ------------------------------------------------------------------
# small helpers
# ------------------------------------------------------------------

def _log(message):
    if not QUIET:
        print(message, flush=True)


def _exe_names(name):
    if IS_WINDOWS:
        return (name + ".exe", name)
    return (name, name + ".exe")


def _make_executable(path):
    try:
        mode = os.stat(path).st_mode
        os.chmod(
            path,
            mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH,
        )
    except Exception:
        pass


def _works(path):
    """True if `path` is an executable that answers -version."""
    if not path or not os.path.isfile(path):
        return False

    _make_executable(path)

    try:
        result = subprocess.run(
            [path, "-version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
        return result.returncode == 0
    except Exception:
        return False


def _find_in_dir(directory, name):
    if not directory or not os.path.isdir(directory):
        return None

    for candidate in _exe_names(name):
        path = os.path.join(directory, candidate)
        if os.path.isfile(path):
            return path

    return None


def _search_tree(root, name):
    """Find a binary anywhere under root (extracted archives are nested)."""
    wanted = set(_exe_names(name))

    for current, _dirs, files in os.walk(root):
        for filename in files:
            if filename in wanted or filename == name:
                return os.path.join(current, filename)

    return None


def _install_into_local_bin(source, name):
    """Copy a found binary into our own bin dir under a normalised name."""
    if not source or not os.path.isfile(source):
        return None

    os.makedirs(LOCAL_BIN_DIR, exist_ok=True)

    target = os.path.join(
        LOCAL_BIN_DIR,
        name + (".exe" if IS_WINDOWS else ""),
    )

    try:
        if os.path.abspath(source) != os.path.abspath(target):
            shutil.copy2(source, target)
    except Exception:
        return source if _works(source) else None

    _make_executable(target)

    return target if _works(target) else None


# ------------------------------------------------------------------
# 1-3. explicit config, PATH, known folders
# ------------------------------------------------------------------

def _from_env():
    raw = (os.getenv("FFMPEG_LOCATION") or "").strip().strip('"').strip("'")

    if not raw:
        return (None, None)

    location = os.path.expanduser(os.path.expandvars(raw))

    if os.path.isfile(location):
        directory = os.path.dirname(location)
        return (location, _find_in_dir(directory, "ffprobe"))

    return (
        _find_in_dir(location, "ffmpeg"),
        _find_in_dir(location, "ffprobe"),
    )


def _from_path():
    return (shutil.which("ffmpeg"), shutil.which("ffprobe"))


def _from_common_dirs():
    ffmpeg = None
    ffprobe = None

    for directory in COMMON_DIRS:
        if ffmpeg is None:
            ffmpeg = _find_in_dir(directory, "ffmpeg")

        if ffprobe is None:
            ffprobe = _find_in_dir(directory, "ffprobe")

        if ffmpeg and ffprobe:
            break

    return (ffmpeg, ffprobe)


# ------------------------------------------------------------------
# 4. pip wheels that already ship the binary
# ------------------------------------------------------------------

def _from_pip_packages():
    """
    These packages carry real ffmpeg executables inside their wheels,
    so `pip install -r requirements.txt` on the host is all that is
    needed. Each one is optional - missing packages are skipped.
    """
    ffmpeg = None
    ffprobe = None

    # ffmpeg-binaries: ships BOTH ffmpeg and ffprobe.
    try:
        import ffmpeg as ffmpeg_binaries  # type: ignore

        if hasattr(ffmpeg_binaries, "init"):
            try:
                ffmpeg_binaries.init()
            except Exception:
                pass

        candidate = getattr(ffmpeg_binaries, "FFMPEG_PATH", None)
        probe = getattr(ffmpeg_binaries, "FFPROBE_PATH", None)

        if candidate and os.path.isfile(candidate):
            ffmpeg = candidate

        if probe and os.path.isfile(probe):
            ffprobe = probe
    except Exception:
        pass

    # imageio-ffmpeg: ships ffmpeg (no ffprobe). Very reliable wheel.
    if not ffmpeg:
        try:
            import imageio_ffmpeg  # type: ignore

            candidate = imageio_ffmpeg.get_ffmpeg_exe()

            if candidate and os.path.isfile(candidate):
                ffmpeg = candidate
        except Exception:
            pass

    # static-ffmpeg: exposes both when already provisioned.
    if not ffmpeg or not ffprobe:
        try:
            import static_ffmpeg.run as static_run  # type: ignore

            paths = static_run.get_or_fetch_platform_executables_else_raise()

            if paths:
                if not ffmpeg and os.path.isfile(paths[0]):
                    ffmpeg = paths[0]

                if len(paths) > 1 and not ffprobe and os.path.isfile(paths[1]):
                    ffprobe = paths[1]
        except Exception:
            pass

    # Normalise the names into our own bin folder so yt-dlp can be
    # pointed at a single directory.
    if ffmpeg:
        ffmpeg = _install_into_local_bin(ffmpeg, "ffmpeg") or ffmpeg

    if ffprobe:
        ffprobe = _install_into_local_bin(ffprobe, "ffprobe") or ffprobe

    return (ffmpeg, ffprobe)


# ------------------------------------------------------------------
# 5. last resort: resumable download of a static build
# ------------------------------------------------------------------

DOWNLOAD_SOURCES = (
    # (url, archive kind)
    (
        "https://johnvansickle.com/ffmpeg/releases/"
        "ffmpeg-release-amd64-static.tar.xz",
        "tar",
    ),
    (
        "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
        "ffmpeg-master-latest-linux64-gpl.tar.xz",
        "tar",
    ),
)

WINDOWS_SOURCES = (
    (
        "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
        "ffmpeg-master-latest-win64-gpl.zip",
        "zip",
    ),
)

CHUNK = 512 * 1024


def _resumable_download(url, destination, attempts=40):
    """
    Download `url` to `destination`, resuming a partial .part file with
    an HTTP Range request. Being killed mid-way is harmless: the next
    run continues from the bytes already on disk.
    """
    try:
        import requests
    except Exception:
        return False

    part = destination + ".part"

    for attempt in range(1, attempts + 1):
        have = os.path.getsize(part) if os.path.exists(part) else 0

        headers = {"User-Agent": "ffmpeg-setup/1.0"}

        if have:
            headers["Range"] = "bytes=%d-" % have

        try:
            response = requests.get(
                url,
                headers=headers,
                stream=True,
                timeout=60,
                allow_redirects=True,
            )

            if response.status_code in (200, 206):
                mode = "ab" if (have and response.status_code == 206) else "wb"

                if mode == "wb":
                    have = 0

                with open(part, mode) as handle:
                    for block in response.iter_content(chunk_size=CHUNK):
                        if block:
                            handle.write(block)
                            handle.flush()

                total = response.headers.get("Content-Length")

                # Range requests report the remaining length only, so a
                # simple "did the stream end" check is what we rely on.
                if total is None or os.path.getsize(part) >= have + int(total):
                    os.replace(part, destination)
                    return True

            elif response.status_code == 416:
                # Already complete.
                os.replace(part, destination)
                return True

        except Exception as error:
            _log(
                "   ffmpeg download attempt %d interrupted (%s), resuming..."
                % (attempt, type(error).__name__)
            )

    return False


def _extract(archive, kind, target_dir):
    os.makedirs(target_dir, exist_ok=True)

    try:
        if kind == "zip":
            with zipfile.ZipFile(archive) as handle:
                handle.extractall(target_dir)
        else:
            with tarfile.open(archive) as handle:
                handle.extractall(target_dir)

        return True
    except Exception:
        return False


def _from_download():
    if not ALLOW_DOWNLOAD:
        return (None, None)

    sources = WINDOWS_SOURCES if IS_WINDOWS else DOWNLOAD_SOURCES

    work_dir = os.path.join(LOCAL_BIN_DIR, "_dl")

    os.makedirs(work_dir, exist_ok=True)

    for url, kind in sources:
        archive = os.path.join(work_dir, os.path.basename(url))

        _log("   fetching a static ffmpeg build (resumable): " + url)

        if not (
            os.path.exists(archive)
            or _resumable_download(url, archive)
        ):
            continue

        extract_dir = archive + ".x"

        if not _extract(archive, kind, extract_dir):
            continue

        ffmpeg = _install_into_local_bin(
            _search_tree(extract_dir, "ffmpeg"), "ffmpeg"
        )

        ffprobe = _install_into_local_bin(
            _search_tree(extract_dir, "ffprobe"), "ffprobe"
        )

        # Free the disk again - ephemeral hosts are small.
        shutil.rmtree(extract_dir, ignore_errors=True)

        try:
            os.remove(archive)
        except Exception:
            pass

        if ffmpeg:
            return (ffmpeg, ffprobe)

    return (None, None)


# ------------------------------------------------------------------
# public API
# ------------------------------------------------------------------

def ensure_ffmpeg(verbose=True):
    """
    Resolve ffmpeg/ffprobe once and cache the result.

    Returns dict(dir=..., ffmpeg=..., ffprobe=..., source=...).
    `ffmpeg` may be None only if every strategy failed.
    """
    global _CACHE

    if _CACHE is not None:
        return _CACHE

    strategies = (
        ("FFMPEG_LOCATION env var", _from_env),
        ("system PATH", _from_path),
        ("folder bundled with the project", _from_common_dirs),
        ("pip-installed ffmpeg wheel", _from_pip_packages),
        ("resumable static download", _from_download),
    )

    ffmpeg = None
    ffprobe = None
    source = None

    for label, strategy in strategies:
        try:
            found_ffmpeg, found_ffprobe = strategy()
        except Exception:
            found_ffmpeg, found_ffprobe = (None, None)

        if found_ffprobe and not ffprobe and _works(found_ffprobe):
            ffprobe = found_ffprobe

        if found_ffmpeg and not ffmpeg and _works(found_ffmpeg):
            ffmpeg = found_ffmpeg
            source = label

        if ffmpeg and ffprobe:
            break

    # Make sure yt-dlp gets a directory that really holds the binaries.
    directory = None

    if ffmpeg:
        if ffprobe and os.path.dirname(ffprobe) == os.path.dirname(ffmpeg):
            directory = os.path.dirname(ffmpeg)
        else:
            ffmpeg = _install_into_local_bin(ffmpeg, "ffmpeg") or ffmpeg

            if ffprobe:
                ffprobe = (
                    _install_into_local_bin(ffprobe, "ffprobe") or ffprobe
                )

            directory = os.path.dirname(ffmpeg)

    # Put it on PATH too, so any library that shells out finds it.
    if directory:
        os.environ["PATH"] = directory + os.pathsep + os.environ.get("PATH", "")
        os.environ.setdefault("FFMPEG_BINARY", ffmpeg or "")

        if ffprobe:
            os.environ.setdefault("FFPROBE_BINARY", ffprobe)

    _CACHE = {
        "dir": directory,
        "ffmpeg": ffmpeg,
        "ffprobe": ffprobe,
        "source": source,
    }

    if verbose:
        if ffmpeg:
            _log("FFmpeg ready: " + ffmpeg + "   (via " + str(source) + ")")

            if ffprobe:
                _log("FFprobe ready: " + ffprobe)
            else:
                _log(
                    "FFprobe not found - not required, ffmpeg alone "
                    "handles merging."
                )
        else:
            _log(
                "WARNING: ffmpeg could not be provided automatically.\n"
                "         Add 'imageio-ffmpeg' to requirements.txt, or drop "
                "an ffmpeg binary into the project's bin/ folder."
            )

    return _CACHE


def get_ffmpeg_location():
    return ensure_ffmpeg(verbose=False)["dir"]


def get_ffmpeg_binary():
    return ensure_ffmpeg(verbose=False)["ffmpeg"]


def get_ffprobe_binary():
    return ensure_ffmpeg(verbose=False)["ffprobe"]


def ffmpeg_is_available():
    return bool(ensure_ffmpeg(verbose=False)["ffmpeg"])


if __name__ == "__main__":
    info = ensure_ffmpeg()

    print("")
    print("dir     : " + str(info["dir"]))
    print("ffmpeg  : " + str(info["ffmpeg"]))
    print("ffprobe : " + str(info["ffprobe"]))
    print("source  : " + str(info["source"]))

    sys.exit(0 if info["ffmpeg"] else 1)
