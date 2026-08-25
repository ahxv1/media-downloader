FFMPEG - HOW IT WORKS NOW (read this once, then forget it)
=========================================================

WHAT CHANGED
------------
You no longer install or download ffmpeg on the hosting website.

"imageio-ffmpeg" was added to requirements.txt. That package's wheel
ALREADY CONTAINS the ffmpeg program inside it. So ffmpeg arrives during
the host's normal build step:

    pip install -r requirements.txt

That step is the one hosts never kill, and it is cached between
deploys. There is no more "downloading ffmpeg" phase while the bot is
running, which is exactly the phase your host kept killing.

The new file ffmpeg_setup.py then LOCATES ffmpeg by itself at startup,
so you never set a path anywhere.


HOW TO DEPLOY
-------------
1. Upload / push this ZIP's contents to your host as-is.
2. Set ONE environment variable:  BOT_TOKEN = <token from @BotFather>
3. Run it as a Worker / Background service, start command:

       python main.py

4. Done. Do NOT set FFMPEG_LOCATION. Do NOT install ffmpeg.

On startup you will see which ffmpeg it picked, for example:

    FFmpeg ready: /app/.ffmpeg/ffmpeg   (via pip-installed ffmpeg wheel)
    FFmpeg directory: /app/.ffmpeg
    Instagram engine: media-downloader (fail-safe: parth-dl)
    Media Downloader Bot is running...


THE FULL SEARCH ORDER (automatic)
---------------------------------
1. FFMPEG_LOCATION env var, if you set one (folder OR the binary).
2. ffmpeg already on the host's PATH (Docker image, apt, VPS).
3. A binary you dropped in this project's bin/ folder.
4. The pip wheel (imageio-ffmpeg / ffmpeg-binaries / static-ffmpeg).
   <-- this is what will normally be used on your host
5. Last resort only: a static build downloaded in small chunks to a
   .part file. If the host kills it, the next start RESUMES from the
   bytes already on disk with an HTTP Range request instead of
   starting over. Set FFMPEG_ALLOW_DOWNLOAD=0 to disable this step.

Whatever is found is copied into a local .ffmpeg/ folder, made
executable automatically (no chmod needed), added to PATH, and handed
to yt-dlp as its ffmpeg_location.


TEST IT WITHOUT RUNNING THE BOT
-------------------------------
    python ffmpeg_setup.py

It prints the resolved paths and exits 0 when ffmpeg is usable.


ABOUT FFPROBE
-------------
The pip wheel provides ffmpeg but not always ffprobe. That is fine:
yt-dlp only needs ffmpeg to merge video+audio and to convert to MP3.
If ffprobe is missing you will see a harmless one-line note at startup.
If your host has apt ffmpeg available, both are found in step 2 anyway.


IF IT STILL SAYS FFMPEG WAS NOT FOUND
-------------------------------------
- Check the build log actually ran "pip install -r requirements.txt"
  and that imageio-ffmpeg installed.
- Or download a static Linux x86_64 ffmpeg once on your own PC and put
  the "ffmpeg" file in the bin/ folder, then re-upload. That makes it
  fully offline: nothing is fetched on the host at all.
