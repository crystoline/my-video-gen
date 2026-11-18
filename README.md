# Faceless science video pipeline

Turns a topic like "Why is Mars red?" into a finished, captioned 1080p YouTube video and uploads it (private by default, so you approve it before it goes public).

**How it works**

1. Claude writes the script and a shot list.
2. A second Claude pass fact-checks the script, searching the web for primary sources.
3. You review the script.
4. ElevenLabs narrates it.
5. Visuals come from NASA's image library (space, planets, Earth from orbit) and stock footage from Pexels and/or Pixabay (Earth subjects).
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

`.env`, `client_secret.json`, `youtube_token.json`, `channels.json`, `music/` and `projects/` are listed in `.gitignore`, so they stay out of version control.

## 2. Get your keys (put them in `.env`)

- **Anthropic:** create an API key at console.anthropic.com. `ANTHROPIC_MODEL` writes the script; `ANTHROPIC_FACTCHECK_MODEL` checks it and may run up to `FACTCHECK_SEARCHES` web searches per video (set it to 0 to check from memory only).
- **ElevenLabs:** create an API key, then pick or design a voice and copy its Voice ID. Pick one voice and keep it; it becomes your channel's identity.
- **Stock footage:** set `PIXABAY_API_KEY` (free; the key is shown at pixabay.com/api/docs once you log in) and/or `PEXELS_API_KEY` (pexels.com/api, which paused issuing new keys in 2026). Pexels is tried first when both are set. With neither, every scene uses NASA imagery, which is weak for Earth-bound subjects. The NASA library needs no key.
- **More visual sources (optional):** Wikimedia Commons (history, people, places, documentary footage) and Openverse (openly licensed photos from Flickr and museums) work without a key. Add `UNSPLASH_ACCESS_KEY` (unsplash.com/developers) for high-quality photos and `SMITHSONIAN_API_KEY` (free from api.data.gov) for public-domain museum images. Only files whose licence allows use in a monetised video are offered (public domain, CC0, CC BY; CC BY-SA only if you allow it), and every one is credited in the description. In the web interface's Settings you can switch each source on or off and set the order they are tried in.
- **AI visuals (optional):** add `FAL_KEY` from fal.ai and switch AI visuals on in Settings (images only, or images and short video clips). You then generate pictures from the visual picker's **AI-generated** source, or set a scene's source to AI-generated so "Fetch visuals" makes an image for it. Every generation shows its price first, generated files stay in the project's `ai` folder so you can pick them again without paying twice, and videos that use them are always marked as containing synthetic media when uploaded.
- **YouTube:** set this up in Google Cloud Console.
  1. Create a project and enable **YouTube Data API v3**. Also enable the **YouTube Analytics API** if you want watch time in the web interface's statistics.
  2. Configure the **OAuth consent screen** and add your Google account as a test user.
  3. Go to Credentials → Create **OAuth client ID** → type **Desktop app**.
  4. Download the JSON file and save it as `client_secret.json` next to `pipeline.py`.

  Sign in once with `python pipeline.py --login`, which opens a browser and saves the login token. Otherwise the first interactive upload asks you to sign in. Paths in `.env` are relative to `pipeline.py`, so the pipeline works from any working directory.

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

If you edit narration later, re-running regenerates only the changed voiceover clips and re-renders only the scenes whose length changed, so picture and sound stay in sync. To swap one scene's visual from the command line, delete its file in `assets/`; the next run fetches the next-best match and re-renders that scene. The web interface does this with one click.

**Your own script**

To narrate something you wrote, put the narration in a text file and give it a title:

```bash
python pipeline.py --script-file speech.txt --title "The day the Moon saved Earth" --no-upload
```

A blank line, or a line starting with `Scene 3:`, starts a new scene; text without any breaks is split into scenes of about two sentences. Under a scene you can add `Visual: ocean waves` (or `Visual: nasa: Saturn rings`, `Visual: photo: old map`) to choose its visual search. Your words are never changed: Claude only chooses visuals for the other scenes and writes the description and tags, then the fact-checker reports issues for you to accept or ignore.

**Other options**

- `--privacy unlisted|public` sets the upload's privacy.
- `--synthetic` ticks YouTube's AI-disclosure box. Use it if you add realistic AI-generated visuals.
- `--project projects/<folder>` resumes a project.
- `CROSSFADE` in `.env` is the cross-fade length in seconds (default 0.5). Set it to 0 for hard cuts.
- `RENDER_JOBS` in `.env` sets how many scenes render at the same time (default: a quarter of the CPU cores, 1 to 4).
- `SHOT_SECONDS` (also in Settings) gives long scenes extra shots: with 10, a 25-second scene shows two different visuals and a 35-second one three, so the picture keeps moving. The extra visuals come from the same search; on the Visuals tab you can change each one.

## 4. Web interface

For reviewing and re-working videos, a local web interface is easier than editing JSON files:

```bash
.venv/bin/python webui.py
```

Then open http://localhost:8765. From WSL, open it in your normal Windows browser. It only accepts connections from this computer. Stop it with Ctrl+C.

- **Videos:** start a video from a topic (**Claude writes the script**), or paste your own narration (**I have the script**) with the same scene and `Visual:` rules as `--script-file`; you choose whether Claude picks visuals and whether it is fact-checked. See where every video stands. Once videos are uploaded, a table shows each one's views, watch time, average view duration and likes (plus its Short's views), so you can see which topics work. Click **Refresh statistics** to update it.
- **Script:** edit the title, description, tags and each scene's narration and visual search. Apply the fact-checker's fixes with one click, and move, add, delete, split (✂ at the text cursor) or join scenes. Narration clips and visuals follow their scene when you reorder, so nothing is narrated twice. Each scene shows whether its narration clip is current or will be regenerated, with its character count.
- **Visuals:** see every scene's image or clip. **Choose…** opens a grid of search results from Pexels, Pixabay or NASA with preview images (video clips play when you hover), so you can compare a dozen options, change the search words, and pick one. Only the one you pick is downloaded. **Next result** swaps in the next search result without looking, and you can upload your own image or video. Claude can act as picture editor: it looks at the previews and marks the result that best fits the narration (**Ask Claude to pick**, or automatically on every search with `AUTO_PICK=rank`). With `AUTO_PICK=auto`, automatic fetches and **Next result** use Claude's pick too. Pick the thumbnail background here too.
- **Video:** render, watch, choose this video's music track, preview the sound, edit the chapters, update the thumbnail, and upload to YouTube with privacy and schedule options. Chapters are suggested by Claude when the video renders (`AUTO_CHAPTERS`) and added to the description as timestamps, as long as they meet YouTube's rules (at least 3, the first at 0:00, each at least 10 seconds).
- **Other languages (Video tab):** with caption languages chosen in Settings (`CAPTION_LANGUAGES`), uploads get translated captions, titles and descriptions, so viewers can watch with subtitles and find the video in their language; already-uploaded videos can get them with one click. **Make this version** creates a dubbed copy in another language: same visuals and music, a translated script to review, then its own voiceover (ElevenLabs characters, as for any video) and upload in that language.
- **Short:** make a vertical YouTube Short from a run of scenes (by default the opening scenes, up to about a minute), with large captions and the title at the top, then upload it. It reuses the narration and visuals, so it costs nothing extra; its description links to the full video if that is uploaded first.
- **Topic queue:** reorder `topics.txt`, start a video from any topic, and send failed topics back to the queue.
- **Channels (Settings):** each video belongs to a channel: what it is about, its writing voice, accuracy rules and preferred sources, tips for visuals, YouTube category, narration language, its own ElevenLabs voice and music, and a default length. The script writer, fact-checker and visual planner follow it, so the same tool can make history, nature or finance videos, or serve several YouTube channels. The built-in "Science & space" channel is used until you add others; channels are saved in `channels.json`. On the command line, pick one with `--channel <id>`.
- **Settings:** default music (one track, or a different track per video), the music library (listen to tracks, add new ones), music level, ducking, voice tone and ElevenLabs voice settings, with a 45-second preview mixed from a real video. It also shows how many ElevenLabs characters you have left this month. Settings are saved to `.env`, so the command line uses them too.

Anything that spends money asks first and shows the ElevenLabs character count next to what is left on your plan. Jobs run one at a time, and their logs are kept in each project's `activity.log`. The web interface and the command line work on the same project folders, so you can mix them.

## 5. Automate

Put topics in `topics.txt`, one per line. Each run takes the top topic, builds and uploads it, then moves it to `topics_done.txt`. The `--yes` flag skips the review pause and auto-applies the fact-checker's fixes.

**Linux/macOS (cron):** this example runs Monday, Wednesday and Friday at 09:00. The PATH line matters: cron's default PATH skips `/usr/local/bin`, where a manually installed FFmpeg usually lives.
```
PATH=/usr/local/bin:/usr/bin:/bin
0 9 * * 1,3,5 cd /path/to/MyVideoGen && .venv/bin/python pipeline.py --topics-file topics.txt --yes >> pipeline.log 2>&1
```

**Windows:** use Task Scheduler to run the same command. Inside WSL, cron only fires while the distro is running and the `cron` service has been started.

A run that fails keeps its topic at the top of the queue and resumes next time. After `MAX_ATTEMPTS` failed runs (default 3) the topic is moved to `topics_failed.txt` with the error, so one bad topic cannot block the queue. Unattended runs also check the YouTube login before generating anything; if it has expired, the run stops with a message instead of spending money on narration.

In unattended mode, videos still upload as **private**. Watch each one in YouTube Studio before making it public. This review step is what keeps the channel on the right side of YouTube's "inauthentic content" policy, and it catches the occasional science error or mismatched visual.

## 6. Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

The tests replace Claude, ElevenLabs, the stock searches and YouTube with fakes and work in a temporary folder, so they cost nothing and never touch your `.env`, `topics.txt` or `projects/`. Rendering and sound tests use your real FFmpeg and are skipped if it is not installed. A full run takes a minute or two.

## Things to know

- **API audit.** Google locks videos uploaded through an *unverified* API project to private. To publish from the API, apply for the YouTube API compliance audit. Until then, upload as private and switch videos to public manually in Studio, if your project allows it.
- **Login expiry.** If your OAuth app stays in "Testing" mode, the login expires about every 7 days. Set the consent screen's publishing status to "In production" to avoid this. For personal use you can click through the "unverified app" warning. When the saved login has expired, run `python pipeline.py --login` to sign in again. Unattended runs and the web interface stop with that instruction instead of spending money on narration.
- **Upload quota.** The default daily quota is enough for a few uploads per day. Each upload, caption and thumbnail uses some of it.
- **Custom thumbnails** require a verified channel (youtube.com/verify).
- **Image rights.** NASA imagery is generally free to use, but some images in the library belong to partners such as ESA or universities. Check the credit, and never imply NASA endorses your channel. Pexels and Pixabay footage is free to use under their content licences. Credits for all sources are added to the video description automatically. Standard Pixabay keys serve photos at most 1280 px wide, so Pixabay stills are upscaled to 1080p; Pexels photos, when available, come at full size.
- **Costs.** Pexels, Pixabay and NASA are free. The script costs a few cents; the fact-check costs a few cents more plus about one cent per web search. ElevenLabs is the main cost: a 6-minute video is roughly 5,000–6,000 characters, and `review.md` shows the exact count before you commit.
- **Transient errors.** Calls to ElevenLabs, Pexels, Pixabay and NASA retry automatically on rate limits and server errors before the run gives up.
- **Music.** Set `MUSIC_PATH` to an audio file and it loops under every video's narration. Set it to a folder of tracks (for example `music`) and each new video gets the track used least so far, then keeps it, so videos don't all sound the same. In the web interface any video can pick its own track or none. The pipeline measures the track's loudness, so `MUSIC_VOLUME` means the same thing with any track: 0.10 puts the music 20 dB below the voice, 0.05 puts it 26 dB below. With `MUSIC_DUCKING` on (the default), the music also dips about 6 dB while the narrator speaks and comes back up in pauses. A wrong path stops the run at startup rather than silently leaving the music out. Use only tracks you have the rights to, such as the YouTube Audio Library.
- **Voice sound.** `VOICE_TONE` shapes the narration in the final mix: `soft` (the default) tames harsh "s" sounds and the 3 kHz edge and adds a little body, `warm` does more of the same, and `natural` leaves the ElevenLabs audio untouched. The ElevenLabs settings `VOICE_STABILITY`, `VOICE_SIMILARITY`, `VOICE_STYLE`, `VOICE_SPEAKER_BOOST` and `VOICE_SPEED` change how new narration is generated; existing clips keep their sound until they are re-voiced.
- **Re-rendering is quick.** Scenes render several at a time. Each scene is rendered with keyframes where its cross-fades start and end, so joining copies the scenes as they are and encodes only the half-second fades. Changing one scene re-renders that scene and its two fades, and the join takes seconds. Music and voice-tone changes apply when the final video is mixed, so they don't re-render anything. After updating MyVideoGen, the first render of an existing video renders every scene once.
- **Statistics.** Views and likes come from the YouTube Data API. Watch time and average view duration come from the YouTube Analytics API, which needs that API enabled in your Google Cloud project, and lag a day or two. If you signed in before statistics existed, run `python pipeline.py --login` once more to grant the extra read permission.

## Ideas for later

- Add NASA/ESA video clips, or charts made with matplotlib for data-heavy topics.
- Have Claude write the pinned comment and community posts.


```bash
cd ~/projects/crysto/MyVideoGen
cp .env.example .env        # then fill in your API keys
.venv/bin/python pipeline.py "Why is Mars red?" --no-upload

```


