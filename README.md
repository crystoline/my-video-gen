# Faceless science video pipeline

Turns a topic like "Why is Mars red?" into a finished, captioned 1080p YouTube video and uploads it (private by default, so you approve it before it goes public).

**How it works**

1. Claude writes the script and a shot list.
2. A second Claude pass fact-checks the script, searching the web for primary sources.
3. You review the script.
4. ElevenLabs narrates it.
5. Visuals come from NASA's image library (space, planets, Earth from orbit) and Pexels (Earth footage).
6. FFmpeg renders the video with slow zooms on still images and cross-fades between scenes.
7. The script makes word-accurate captions and a thumbnail.
8. It uploads to YouTube.

Each topic gets a folder in `projects/`. If anything fails, run the same command again and it resumes where it stopped.

## 1. Install

You need Python 3.10+ and FFmpeg. FFmpeg 4.3 or newer gives you cross-fades; older versions still work, with hard cuts.

- **Windows:** `winget install ffmpeg`
- **macOS:** `brew install ffmpeg`
- **Ubuntu:** `sudo apt install ffmpeg`

Then create a virtual environment and install the Python packages:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt    # Windows: .venv\Scripts\pip install -r requirements.txt
cp .env.example .env
```

If your system Python is older than 3.10 (Ubuntu 20.04 ships 3.8), get a newer one without touching the system Python via [uv](https://docs.astral.sh/uv/): run `curl -LsSf https://astral.sh/uv/install.sh | sh`, then `uv python install 3.12`, and create the venv with `python3.12 -m venv .venv` instead.

`.env`, `client_secret.json`, `youtube_token.json` and `projects/` are listed in `.gitignore`, so they stay out of version control.

## 2. Get your keys (put them in `.env`)

- **Anthropic:** create an API key at console.anthropic.com. `ANTHROPIC_MODEL` writes the script; `ANTHROPIC_FACTCHECK_MODEL` checks it and may run up to `FACTCHECK_SEARCHES` web searches per video (set it to 0 to check from memory only).
- **ElevenLabs:** create an API key, then pick or design a voice and copy its Voice ID. Pick one voice and keep it; it becomes your channel's identity.
- **Pexels:** get a free API key at pexels.com/api. The NASA library needs no key.
- **YouTube:** set this up in Google Cloud Console.
  1. Create a project and enable **YouTube Data API v3**.
  2. Configure the **OAuth consent screen** and add your Google account as a test user.
  3. Go to Credentials → Create **OAuth client ID** → type **Desktop app**.
  4. Download the JSON file and save it as `client_secret.json` next to `pipeline.py`.

  The first upload opens a browser for you to log in. After that, the login token is saved. Paths in `.env` are relative to `pipeline.py`, so the pipeline works from any working directory.

## 3. Run

Activate the venv first (`source .venv/bin/activate`, on Windows `.venv\Scripts\activate`) or call `.venv/bin/python` directly instead of `python` below.

```bash
# Build the video only (good for your first tries)
python pipeline.py "Why is Mars red?" --no-upload

# Build and upload as private
python pipeline.py "How do auroras form?" --minutes 8

# Build, upload and schedule
python pipeline.py "Why does Earth have seasons?" --publish-at 2026-11-05T15:00:00Z
```

The script pauses after writing. At that point:

1. Read `projects/<topic>/review.md`. It shows the narration's character count (what ElevenLabs will bill), the fact-checker's issues with their sources, and a list of claims to double-check. The fact-checker's full notes are in `factcheck_notes.md`.
2. Edit `script.json` if you want to change anything.
3. Press Enter to continue.

If you edit narration later, re-running regenerates only the changed voiceover clips and re-renders only the scenes whose length changed, so picture and sound stay in sync. To swap one scene's visual, delete its file in `assets/` and its segment in `segments/`.

**Other options**

- `--privacy unlisted|public` sets the upload's privacy.
- `--synthetic` ticks YouTube's AI-disclosure box. Use it if you add realistic AI-generated visuals.
- `--project projects/<folder>` resumes a project.
- `CROSSFADE` in `.env` is the cross-fade length in seconds (default 0.5). Set it to 0 for hard cuts, which also skips a re-encode and renders faster.

## 4. Automate

Put topics in `topics.txt`, one per line. Each run takes the top topic, builds and uploads it, then moves it to `topics_done.txt`. The `--yes` flag skips the review pause and auto-applies the fact-checker's fixes.

**Linux/macOS (cron):** this example runs Monday, Wednesday and Friday at 09:00. The PATH line matters: cron's default PATH skips `/usr/local/bin`, where a manually installed FFmpeg usually lives.
```
PATH=/usr/local/bin:/usr/bin:/bin
0 9 * * 1,3,5 cd /path/to/MyVideoGen && .venv/bin/python pipeline.py --topics-file topics.txt --yes >> pipeline.log 2>&1
```

**Windows:** use Task Scheduler to run the same command. Inside WSL, cron only fires while the distro is running and the `cron` service has been started.

A run that fails keeps its topic at the top of the queue and resumes next time. After `MAX_ATTEMPTS` failed runs (default 3) the topic is moved to `topics_failed.txt` with the error, so one bad topic cannot block the queue. Unattended runs also check the YouTube login before generating anything; if it has expired, the run stops with a message instead of spending money on narration.

In unattended mode, videos still upload as **private**. Watch each one in YouTube Studio before making it public. This review step is what keeps the channel on the right side of YouTube's "inauthentic content" policy, and it catches the occasional science error or mismatched visual.

## Things to know

- **API audit.** Google locks videos uploaded through an *unverified* API project to private. To publish from the API, apply for the YouTube API compliance audit. Until then, upload as private and switch videos to public manually in Studio, if your project allows it.
- **Login expiry.** If your OAuth app stays in "Testing" mode, the login expires about every 7 days. Set the consent screen's publishing status to "In production" to avoid this. For personal use you can click through the "unverified app" warning. When the saved login has expired, an interactive run opens the browser to sign in again; an unattended run stops and asks you to do that.
- **Upload quota.** The default daily quota is enough for a few uploads per day. Each upload, caption and thumbnail uses some of it.
- **Custom thumbnails** require a verified channel (youtube.com/verify).
- **Image rights.** NASA imagery is generally free to use, but some images in the library belong to partners such as ESA or universities. Check the credit, and never imply NASA endorses your channel. Pexels footage is free to use. Credits for both are added to the video description automatically.
- **Costs.** Pexels and NASA are free. The script costs a few cents; the fact-check costs a few cents more plus about one cent per web search. ElevenLabs is the main cost: a 6-minute video is roughly 5,000–6,000 characters, and `review.md` shows the exact count before you commit.
- **Transient errors.** Calls to ElevenLabs, Pexels and NASA retry automatically on rate limits and server errors before the run gives up.
- **Music.** Use only tracks you have the rights to, such as the YouTube Audio Library.

## Ideas for later

- Add NASA/ESA video clips, or charts made with matplotlib for data-heavy topics.
- Build a Shorts version: crop to 9:16 and use the hook scene only.
- Have Claude write the pinned comment and community posts.


```bash
cd ~/projects/crysto/MyVideoGen
cp .env.example .env        # then fill in your API keys
.venv/bin/python pipeline.py "Why is Mars red?" --no-upload

```


