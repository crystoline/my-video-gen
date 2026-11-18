#!/usr/bin/env python3
"""
Faceless science / Earth / astronomy video pipeline.

  topic -> script (Claude) -> fact-check (Claude + web search) -> your review
        -> voiceover (ElevenLabs) -> visuals (NASA Image Library + Pexels/Pixabay stock)
        -> render (FFmpeg) -> captions + thumbnail -> YouTube upload (private by default)

Every topic gets its own folder under projects/. Each step saves its output there,
so if anything fails you can simply re-run the same command and it resumes.

Usage:
  python pipeline.py "Why is Mars red?"
  python pipeline.py "How do auroras form?" --minutes 8 --publish-at 2026-11-05T15:00:00Z
  python pipeline.py --project projects/why-is-mars-red          # resume a project
  python pipeline.py --topics-file topics.txt --yes              # unattended (cron)
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import io
import itertools
import json
import math
import os
import re
import shutil
import subprocess
import sys
import textwrap
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import List, Literal
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

SCRIPT_DIR = Path(__file__).resolve().parent
load_dotenv(SCRIPT_DIR / ".env")


def _path(name: str, default: str) -> Path:
    """A path from .env, resolved next to this script unless it is absolute."""
    value = Path(os.getenv(name, default))
    return value if value.is_absolute() else SCRIPT_DIR / value


# ----------------------------------------------------------------------------
# Configuration (set these in .env)
# ----------------------------------------------------------------------------
def _flag(name: str, default: bool) -> bool:
    value = os.getenv(name, "").strip().lower()
    return default if not value else value not in ("0", "false", "no", "off")


# Post-processing applied to the narration in the final mix (free, no re-synthesis needed).
# "natural" leaves the ElevenLabs audio untouched; "soft" and "warm" tame harsh "s" sounds and
# the 3 kHz presence peak and add a little low-end body.
VOICE_TONES = {
    "natural": [],
    "soft": ["highpass=f=70", "deesser=i=0.4", "equalizer=f=3200:t=q:w=1.0:g=-4",
             "treble=g=-4:f=7000", "bass=g=1.5:f=150"],
    "warm": ["highpass=f=70", "deesser=i=0.6", "equalizer=f=3000:t=q:w=1.0:g=-6",
             "treble=g=-7:f=6000", "bass=g=3:f=160"],
}


DEFAULT_PROVIDERS = "pexels,pixabay,unsplash,wikimedia,openverse,smithsonian,nasa"


def configure() -> None:
    """Read settings from the environment (.env). Called at import, and again by the web UI after it saves settings."""
    global ANTHROPIC_MODEL, FACTCHECK_MODEL, FACTCHECK_SEARCHES, ELEVENLABS_API_KEY, ELEVENLABS_VOICE_ID
    global ELEVENLABS_MODEL, VOICE_STABILITY, VOICE_SIMILARITY, VOICE_STYLE, VOICE_SPEAKER_BOOST, VOICE_SPEED
    global VOICE_TONE, PEXELS_API_KEY, PIXABAY_API_KEY, MUSIC_PATH, MUSIC_VOLUME, MUSIC_DUCKING, CROSSFADE
    global UNSPLASH_ACCESS_KEY, SMITHSONIAN_API_KEY, VISUAL_PROVIDERS, ALLOW_SHARE_ALIKE, AUTO_PICK, VISION_MODEL
    global FAL_KEY, AI_VISUALS, AI_IMAGE_MODEL, AI_VIDEO_MODEL, AI_IMAGE_COST, AI_VIDEO_COST
    global RENDER_JOBS, SHOT_SECONDS, AUTO_CHAPTERS, CAPTION_LANGUAGES, YT_CLIENT_SECRETS, YT_TOKEN, THUMB_FONT, PROJECTS_DIR, MAX_ATTEMPTS
    ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")
    FACTCHECK_MODEL = os.getenv("ANTHROPIC_FACTCHECK_MODEL", "claude-opus-5-5")
    FACTCHECK_SEARCHES = int(os.getenv("FACTCHECK_SEARCHES", "8"))
    ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
    ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "")
    ELEVENLABS_MODEL = os.getenv("ELEVENLABS_MODEL", "eleven_multilingual_v2")
    # ElevenLabs voice settings; they only affect narration generated after a change
    VOICE_STABILITY = float(os.getenv("VOICE_STABILITY", "0.5"))
    VOICE_SIMILARITY = float(os.getenv("VOICE_SIMILARITY", "0.75"))
    VOICE_STYLE = float(os.getenv("VOICE_STYLE", "0"))
    VOICE_SPEAKER_BOOST = _flag("VOICE_SPEAKER_BOOST", True)
    VOICE_SPEED = float(os.getenv("VOICE_SPEED", "1.0"))
    VOICE_TONE = os.getenv("VOICE_TONE", "soft").strip().lower() or "soft"
    PEXELS_API_KEY = os.getenv("PEXELS_API_KEY", "")
    PIXABAY_API_KEY = os.getenv("PIXABAY_API_KEY", "")
    UNSPLASH_ACCESS_KEY = os.getenv("UNSPLASH_ACCESS_KEY", "")
    SMITHSONIAN_API_KEY = os.getenv("SMITHSONIAN_API_KEY", "")
    # Visual providers that are switched on, in the order they are tried (Settings in the web UI)
    VISUAL_PROVIDERS = [p.strip().lower() for p in (os.getenv("VISUAL_PROVIDERS") or DEFAULT_PROVIDERS).split(",")
                        if p.strip() and p.strip().lower() != "none"]
    ALLOW_SHARE_ALIKE = _flag("ALLOW_SHARE_ALIKE", False)
    # Claude looks at the search results' previews: off, rank (in the picker) or auto (also automatic fetches)
    AUTO_PICK = (os.getenv("AUTO_PICK") or "off").strip().lower()
    VISION_MODEL = os.getenv("ANTHROPIC_VISION_MODEL") or ANTHROPIC_MODEL
    # AI-generated visuals through fal.ai: off, images, or images_and_video
    FAL_KEY = os.getenv("FAL_KEY", "")
    AI_VISUALS = (os.getenv("AI_VISUALS") or "off").strip().lower()
    AI_IMAGE_MODEL = (os.getenv("AI_IMAGE_MODEL") or "fal-ai/flux/dev").strip()
    AI_VIDEO_MODEL = (os.getenv("AI_VIDEO_MODEL") or "fal-ai/kling-video/v2.5-turbo/pro/text-to-video").strip()
    AI_IMAGE_COST = float(os.getenv("AI_IMAGE_COST") or 0.025)   # USD per image, shown before generating
    AI_VIDEO_COST = float(os.getenv("AI_VIDEO_COST") or 0.35)    # USD per 5-second clip
    MUSIC_PATH = _path("MUSIC_PATH", "") if os.getenv("MUSIC_PATH") else None
    MUSIC_VOLUME = float(os.getenv("MUSIC_VOLUME", "0.10"))
    MUSIC_DUCKING = _flag("MUSIC_DUCKING", True)
    CROSSFADE = float(os.getenv("CROSSFADE", "0.5"))
    RENDER_JOBS = int(os.getenv("RENDER_JOBS") or 0)   # scenes rendered at once; 0 = automatic
    SHOT_SECONDS = float(os.getenv("SHOT_SECONDS") or 0)   # a long scene gets a new shot about this often; 0 = off
    AUTO_CHAPTERS = _flag("AUTO_CHAPTERS", True)   # Claude suggests chapters at render time if there are none
    # Extra caption languages: translated captions, title and description are added to uploaded videos
    CAPTION_LANGUAGES = [c.strip() for c in os.getenv("CAPTION_LANGUAGES", "").split(",") if c.strip()]
    YT_CLIENT_SECRETS = _path("YT_CLIENT_SECRETS", "client_secret.json")
    YT_TOKEN = _path("YT_TOKEN", "youtube_token.json")
    THUMB_FONT = str(_path("THUMB_FONT", "")) if os.getenv("THUMB_FONT") else ""
    PROJECTS_DIR = _path("PROJECTS_DIR", "projects")
    MAX_ATTEMPTS = int(os.getenv("MAX_ATTEMPTS", "3"))


configure()

VOICE_LUFS = -14.0         # narration loudness in the final mix (YouTube's reference level)
W, H, FPS = 1920, 1080, 30
SCENE_PAD = 0.35           # seconds of breathing room after each narrated scene
WORDS_PER_MINUTE = 150
HTTP_TIMEOUT = 60
MAX_CUE_WORDS, MAX_CUE_CHARS = 8, 42   # caption cue size
ENCODE = ["-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p", "-r", str(FPS)]


# ----------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------
LOG_HOOKS: list = []   # extra receivers for log lines (the web UI uses this to show job progress)


def log(msg: str) -> None:
    line = f"[{datetime.now():%H:%M:%S}] {msg}"
    print(line, flush=True)
    for hook in LOG_HOOKS:
        hook(line)


def run(cmd: list, cwd: Path | None = None) -> str:
    cmd = [str(c) for c in cmd]
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
    if result.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(cmd)}\n{result.stderr[-2000:]}")
    return result.stdout


def media_duration(path: Path) -> float:
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "default=nw=1:nk=1", path])
    return float(out.strip())


def video_frames(path: Path) -> int | None:
    """Number of video frames in a file, or None if the container does not say."""
    out = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=nb_frames",
               "-of", "default=nw=1:nk=1", path]).strip()
    return int(out) if out.isdigit() else None


_FILTERS: set | None = None


def ffmpeg_has_filter(name: str) -> bool:
    global _FILTERS
    if _FILTERS is None:
        try:
            out = run(["ffmpeg", "-hide_banner", "-filters"])
        except (RuntimeError, OSError):
            out = ""
        _FILTERS = {parts[1] for parts in (line.split() for line in out.splitlines()) if len(parts) > 2}
    return name in _FILTERS


_FILTER_OPTIONS: dict = {}


def ffmpeg_filter_has_option(name: str, option: str) -> bool:
    """Whether this FFmpeg's filter supports an option (options get added between versions)."""
    if name not in _FILTER_OPTIONS:
        try:
            out = run(["ffmpeg", "-hide_banner", "-h", f"filter={name}"])
        except (RuntimeError, OSError):
            out = ""
        _FILTER_OPTIONS[name] = {line.split()[0] for line in out.splitlines() if line.startswith("   ") and line.split()}
    return option in _FILTER_OPTIONS[name]


def slugify(text: str, maxlen: int = 60) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:maxlen].rstrip("-") or "untitled"


def load_json(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save_json(path: Path, data) -> None:
    """Write via a temporary file so a reader (or a crash) never sees half a file."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def make_session() -> requests.Session:
    """HTTP session that retries transient failures (429 and 5xx) with backoff."""
    retry = Retry(total=4, backoff_factor=1.5, status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=frozenset({"GET", "POST"}), respect_retry_after_header=True,
                  raise_on_status=False)
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.mount("http://", HTTPAdapter(max_retries=retry))
    session.headers["User-Agent"] = "MyVideoGen/1.2 (self-hosted video tool)"   # Wikimedia asks for a descriptive one
    return session


HTTP = make_session()


def download(url: str, dest: Path) -> None:
    with HTTP.get(url, stream=True, timeout=HTTP_TIMEOUT) as r:
        r.raise_for_status()
        tmp = dest.with_name(dest.name + ".part")
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
        tmp.replace(dest)


# ----------------------------------------------------------------------------
# Step 1: script + fact-check (Claude)
# ----------------------------------------------------------------------------
class Visual(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: Literal["nasa", "stock_video", "stock_photo", "archive"] = Field(
        description="Where to look for this scene's visual")
    query: str = Field(description="2-5 concrete, searchable words describing what is visible, "
                                   "e.g. 'Jupiter Great Red Spot' or 'aurora night sky'. Never abstract ideas.")


class Scene(BaseModel):
    model_config = ConfigDict(extra="forbid")
    narration: str = Field(description="1-3 spoken sentences, about 15-45 words")
    visual: Visual


class Script(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(description="YouTube title, at most 90 characters, curiosity-driven but accurate")
    description: str = Field(description="2-3 short paragraphs for the YouTube description")
    tags: List[str] = Field(description="10 to 15 short tags")
    thumbnail_text: str = Field(description="2-5 punchy words for the thumbnail")
    scenes: List[Scene]


class Issue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scene: int = Field(description="Scene number, counting from 0")
    problem: str = Field(description="What is wrong or misleading")
    fixed_narration: str = Field(description="The full corrected narration for that scene, same length and tone")
    source: str = Field(description="URL or reference that supports the correction, or an empty string")


class FactCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    issues: List[Issue]
    claims_to_verify: List[str] = Field(
        description="Specific numbers or claims a human should double-check against a primary source, "
                    "each followed by the source consulted if there was one")


# ----------------------------------------------------------------------------
# Channels: what a channel is about and how it sounds. Every video belongs to one; the writer, fact-checker,
# visual planner, voice, music and YouTube upload follow it. Kept in channels.json (edited in Settings).
# ----------------------------------------------------------------------------
DEFAULT_CHANNEL = {
    "id": "science", "name": "Science & space",
    "about": "science, planet Earth and astronomy",
    "voice": "Curious, warm and clear, like a great science teacher who loves the subject. Explain with concrete "
             "comparisons and real numbers with units. Open with a hook in the first two sentences (a question or a "
             "surprising fact), then pay it off by the end.",
    "accuracy": "Prefer figures from NASA, ESA, USGS, NOAA and peer-reviewed consensus. Round numbers sensibly.",
    "visuals": "", "category": "28", "language": "en", "voice_id": "", "music": "", "minutes": 6,
}
YT_CATEGORIES = {"1": "Film & Animation", "2": "Autos & Vehicles", "10": "Music", "15": "Pets & Animals",
                 "17": "Sports", "19": "Travel & Events", "20": "Gaming", "22": "People & Blogs", "23": "Comedy",
                 "24": "Entertainment", "25": "News & Politics", "26": "Howto & Style", "27": "Education",
                 "28": "Science & Technology", "29": "Nonprofits & Activism"}
LANGUAGES = {"en": "English", "es": "Spanish", "pt": "Portuguese", "fr": "French", "de": "German", "it": "Italian",
             "nl": "Dutch", "pl": "Polish", "tr": "Turkish", "ru": "Russian", "uk": "Ukrainian", "ar": "Arabic",
             "hi": "Hindi", "bn": "Bengali", "id": "Indonesian", "vi": "Vietnamese", "th": "Thai", "ja": "Japanese",
             "ko": "Korean", "zh-Hans": "Chinese (Simplified)", "sw": "Swahili", "yo": "Yoruba", "fil": "Filipino"}


def channels_path() -> Path:
    return _path("CHANNELS_FILE", "channels.json")


def load_channels() -> dict:
    """{"default": id, "channels": [...]}, with the built-in science channel when nothing is saved yet."""
    path = channels_path()
    try:
        data = load_json(path) if path.exists() else {}
    except ValueError:
        data = {}
    channels = [dict(DEFAULT_CHANNEL, **c) for c in data.get("channels") or [] if isinstance(c, dict) and c.get("id")]
    if not channels:
        channels = [dict(DEFAULT_CHANNEL)]
    default = data.get("default") if any(c["id"] == data.get("default") for c in channels) else channels[0]["id"]
    return {"default": default, "channels": channels}


def save_channels(data: dict) -> None:
    save_json(channels_path(), data)


def get_channel(channel_id: str | None = None) -> dict:
    data = load_channels()
    return next((c for c in data["channels"] if c["id"] == channel_id), None) or \
        next(c for c in data["channels"] if c["id"] == data["default"])


def project_channel(project: Path) -> dict:
    return get_channel(load_meta(project).get("channel"))


def clean_channel(c: dict) -> dict:
    """Check a channel edited in the web UI."""
    def text(key, limit, required=False):
        value = " ".join(str(c.get(key) or "").split()) if key != "voice" else str(c.get(key) or "").strip()
        if required and not value:
            raise ValueError(f"Channel {key} cannot be empty")
        if len(value) > limit:
            raise ValueError(f"Channel {key} is longer than {limit} characters")
        return value
    cid = slugify(str(c.get("id") or c.get("name") or ""), 40)
    out = {"id": cid, "name": text("name", 60, True), "about": text("about", 300, True), "voice": text("voice", 1500),
           "accuracy": text("accuracy", 800), "visuals": text("visuals", 800), "voice_id": text("voice_id", 64),
           "music": text("music", 200)}
    out["category"] = str(c.get("category") or "28")
    out["language"] = str(c.get("language") or "en")
    if out["category"] not in YT_CATEGORIES:
        raise ValueError("Unknown YouTube category")
    if out["language"] not in LANGUAGES:
        raise ValueError("Unknown language")
    if out["voice_id"] and not re.fullmatch(r"[A-Za-z0-9]{8,64}", out["voice_id"]):
        raise ValueError("An ElevenLabs voice ID is letters and digits only")
    try:
        out["minutes"] = float(c.get("minutes") or 6)
    except (TypeError, ValueError):
        raise ValueError("Default length must be a number of minutes")
    if not 1 <= out["minutes"] <= 30:
        raise ValueError("Default length must be between 1 and 30 minutes")
    return out


def writer_system(channel: dict | None = None) -> str:
    ch = channel or get_channel()
    language = LANGUAGES.get(ch.get("language") or "en", "English")
    return f"""You are the head writer for a faceless YouTube channel about {ch['about']}.

Voice: {ch['voice'] or 'Clear, warm and engaging. Open with a hook in the first two sentences, then pay it off by the end.'}

Accuracy rules (non-negotiable):
- Only state things that are well established. Where experts disagree or facts are uncertain, say so plainly.
- No clickbait claims such as "experts are baffled", "they don't want you to know" or "this changes everything".
- {ch['accuracy'] or 'Prefer primary and authoritative sources. Round numbers sensibly.'}
- Never invent quotes, studies, people or events.

Write the narration in {language}, for the ear: short sentences, no parentheses, no lists, and spell out symbols ("degrees Celsius", not "°C")."""


def factcheck_system(channel: dict | None = None) -> str:
    ch = channel or get_channel()
    return f"""You are a meticulous fact-checker for a YouTube channel about {ch['about']}.

Check every number, date, name, quote and causal claim. When you can search the web, verify against primary and authoritative sources instead of relying on memory, and prefer the most recent figures. {ch['accuracy']} Flag substance only, never style. Corrected narration must keep the original length, tone and language."""

_client = None


def anthropic_client():
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from the environment
    return _client


def check_stop(resp, task: str) -> None:
    """Turn a declined or truncated answer into a clear error instead of a JSON parsing failure."""
    if resp.stop_reason == "refusal":
        details = getattr(resp, "stop_details", None)
        why = getattr(details, "explanation", None) or "no reason given"
        raise RuntimeError(f"Claude declined to {task}: {why}")
    if resp.stop_reason == "max_tokens":
        raise RuntimeError(f"Claude's answer was cut off while trying to {task}. Raise max_tokens in pipeline.py.")


def response_text(resp) -> str:
    return "".join(block.text for block in resp.content if block.type == "text")


def claude_json(model: str, system: str, prompt: str, schema: type[BaseModel], max_tokens: int = 16000):
    """Ask Claude for an answer that must match a Pydantic schema (structured outputs)."""
    resp = anthropic_client().messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": prompt}],
        output_config={"format": {"type": "json_schema", "schema": schema.model_json_schema()}},
    )
    check_stop(resp, f"produce the {schema.__name__.lower()}")
    text = response_text(resp)
    try:
        return schema.model_validate_json(text)
    except ValueError as e:
        raise RuntimeError(f"Claude's answer did not match the {schema.__name__} schema: {e}\n{text[:500]}") from e


def claude_research(model: str, system: str, prompt: str, max_searches: int,
                    effort: str = "high", max_tokens: int = 16000) -> tuple[str, int]:
    """Free-text answer, with Anthropic's web search tool when max_searches > 0. Returns (text, searches)."""
    extra = {}
    if max_searches > 0:
        extra["tools"] = [{"type": "web_search_20260209", "name": "web_search", "max_uses": max_searches}]
    messages = [{"role": "user", "content": prompt}]
    searches = 0
    for _ in range(8):
        resp = anthropic_client().messages.create(
            model=model, max_tokens=max_tokens, system=system, messages=messages,
            output_config={"effort": effort}, **extra)
        searches += sum(1 for block in resp.content if block.type == "server_tool_use")
        if resp.stop_reason == "pause_turn":   # server-side search loop paused; send it back to continue
            messages.append({"role": "assistant", "content": resp.content})
            continue
        break
    check_stop(resp, "fact-check the script")
    return response_text(resp), searches


def previous_titles(exclude: Path, limit: int = 40, channel_id: str | None = None) -> list[str]:
    """Titles of videos already made in projects/ (for one channel, if given), newest first, so the writer
    avoids repeating them."""
    if not PROJECTS_DIR.exists():
        return []
    default = load_channels()["default"]
    titles = []
    for path in sorted(PROJECTS_DIR.glob("*/script.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        if path.parent.resolve() == exclude.resolve():
            continue
        if channel_id and (load_meta(path.parent).get("channel") or default) != channel_id:
            continue
        try:
            s = load_json(path)
            titles.append(f"{s.get('title', '')} (topic: {s.get('topic', '')})")
        except (OSError, ValueError, AttributeError):
            continue
        if len(titles) >= limit:
            break
    return titles


def write_script(topic: str, minutes: float, previous: list[str], channel: dict | None = None) -> dict:
    channel = channel or get_channel()
    words = int(minutes * WORDS_PER_MINUTE)
    avoid = ""
    if previous:
        avoid = ("\n\nVideos this channel has already published. Find a fresh angle and do not reuse "
                 "their titles, hooks or phrasing:\n" + "\n".join(f"- {t}" for t in previous))
    tips = f"\nChannel guidance for visuals: {channel['visuals']}" if channel.get("visuals") else ""
    prompt = f"""Write a YouTube video script about: {topic}

Target length: about {words} words of narration (roughly {minutes:g} minutes).

Split the narration into scenes of 1-3 sentences each (about 15-45 words). For every scene choose ONE visual source:
- "nasa": NASA image library. Best for planets, moons, the Sun, stars, galaxies, nebulae, spacecraft, rockets, astronauts and Earth seen from orbit.
- "stock_video": stock footage. Best for the real world today: nature, oceans, weather, cities, people, animals, technology, everyday life.
- "stock_photo": stock photo, when a still image fits better.
- "archive": historical photos, artworks, documents and museum objects (Wikimedia Commons, Openverse, the Smithsonian). Best for history, famous people, real places, inventions and artefacts.
The query must be 2-5 concrete, searchable English words describing what is visible (e.g. "Jupiter Great Red Spot", "lava flow ocean", "aurora night sky"). Never abstract ideas.{tips}

Also give a YouTube title (max 90 characters, curiosity-driven but accurate), a description of 2-3 short paragraphs, 10 to 15 short tags, and 2-5 punchy words of thumbnail text.{avoid}"""
    script = claude_json(ANTHROPIC_MODEL, writer_system(channel), prompt, Script).model_dump()
    if not script["scenes"]:
        raise ValueError("Claude returned a script with no scenes")
    script["topic"] = topic
    return script


# ----------------------------------------------------------------------------
# Scripts you write yourself: your narration is kept word for word
# ----------------------------------------------------------------------------
class VisualPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: str = Field(description="2-3 short paragraphs for the YouTube description")
    tags: List[str] = Field(description="10 to 15 short tags")
    thumbnail_text: str = Field(description="2-5 punchy words for the thumbnail")
    visuals: List[Visual] = Field(description="Exactly one visual per scene, in scene order")


PLANNER_SYSTEM = """You are the editor of a faceless YouTube channel. The narration you are given is final: never change, correct or comment on it. Your job is to choose a fitting visual for every scene and to write the YouTube description, tags and thumbnail text."""

_SCENE_LABEL = re.compile(r"^\s*[\[(]?\s*scene\s*\d+\s*[\])]?\s*[:.)\-–—]?\s*", re.I)
_VISUAL_LINE = re.compile(r"^\s*[\[(]?\s*visual\s*[:\-–—]\s*(.*?)\s*[\])]?\s*$", re.I)
_HINT_SOURCE = re.compile(r"^\s*(nasa|stock[ _]video|stock[ _]photo|video|photo)\s*:\s*(.+)$", re.I)
_HINT_SOURCES = {"nasa": "nasa", "stock video": "stock_video", "video": "stock_video",
                 "stock photo": "stock_photo", "photo": "stock_photo"}
_STOPWORDS = set("""a an the and or but if of to in on at by for with from as is are was were be been being it its
this that these those there their they them we our you your he she his her i me my not no so than then too very can
could would should will just about into over under more most such what which who whom how why when where while also
only even because each every some many much like after before during through between until upon""".split())


def _visual_hint(hint: str | None) -> dict:
    """'ocean waves', 'nasa: Jupiter' or 'photo: old library' -> a scene's visual."""
    hint = (hint or "").strip()
    m = _HINT_SOURCE.match(hint)
    if m:
        return {"source": _HINT_SOURCES[m.group(1).lower().replace("_", " ")], "query": m.group(2).strip()[:100]}
    return {"source": "stock_video", "query": hint[:100]}


def split_sentences(text: str) -> list[str]:
    out, start = [], 0
    for m in re.finditer(r'[.!?…]+["”’)\]]*\s+(?=["“‘(\[]?[A-Z0-9])', text):
        out.append(text[start:m.end()].strip())
        start = m.end()
    if text[start:].strip():
        out.append(text[start:].strip())
    return out


def group_sentences(sentences: list[str], target: int = 28, limit: int = 50) -> list[str]:
    """Scenes of about two sentences: close a scene once it reaches `target` words, never pass `limit`."""
    scenes, current, n = [], [], 0
    for s in sentences:
        w = len(s.split())
        if current and n + w > limit:
            scenes.append(" ".join(current))
            current, n = [], 0
        current.append(s)
        n += w
        if n >= target:
            scenes.append(" ".join(current))
            current, n = [], 0
    if current:
        if scenes and n < 8:   # no tiny last scene
            scenes[-1] += " " + " ".join(current)
        else:
            scenes.append(" ".join(current))
    return scenes


def parse_manual_script(text: str) -> list[dict]:
    """Scenes from narration you wrote yourself:
    - a blank line, or a line starting with "Scene 3:", starts a new scene;
    - a line "Visual: ocean waves" (or "Visual: nasa: Jupiter", "Visual: photo: old map") sets the scene's visual search;
    - text without any scene breaks is split into scenes of about two sentences."""
    scenes: list[dict] = []
    lines: list[str] = []
    visual: str | None = None

    def flush():
        nonlocal lines, visual
        narration = " ".join(" ".join(lines).split())
        if narration:
            scenes.append({"narration": narration, "visual": _visual_hint(visual)})
        elif visual and scenes and not scenes[-1]["visual"]["query"]:   # a "Visual:" line on its own after a scene
            scenes[-1]["visual"] = _visual_hint(visual)
        lines, visual = [], None

    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if not raw.strip():
            flush()
            continue
        m = _VISUAL_LINE.match(raw)
        if m:
            visual = m.group(1)
            continue
        if _SCENE_LABEL.match(raw):
            flush()
            raw = _SCENE_LABEL.sub("", raw, count=1)
        if raw.strip():
            lines.append(raw.strip())
    flush()
    if len(scenes) == 1 and len(scenes[0]["narration"].split()) > 50:
        only = scenes[0]
        parts = group_sentences(split_sentences(only["narration"]))
        scenes = [{"narration": t, "visual": dict(only["visual"]) if k == 0 else _visual_hint(None)}
                  for k, t in enumerate(parts)]
    return scenes


def keyword_query(text: str, fallback: str) -> str:
    """A rough visual search from a scene's own words (used when Claude does not choose visuals)."""
    words = [w.strip(".,;:!?\"'()[]{}—–-…“”‘’").lower() for w in text.split()]
    candidates = [w for w in words if len(w) > 3 and w.isalpha() and w not in _STOPWORDS]
    best = set(sorted(dict.fromkeys(candidates), key=lambda w: -len(w))[:3])
    return " ".join(w for w in dict.fromkeys(candidates) if w in best) or fallback[:100]


def plan_visuals(title: str, narrations: list[str], channel: dict | None = None) -> dict:
    """Claude chooses a visual for each scene and writes the description, tags and thumbnail text."""
    channel = channel or get_channel()
    numbered = "\n".join(f"{i}. {t}" for i, t in enumerate(narrations))
    tips = f"\nChannel guidance for visuals: {channel['visuals']}" if channel.get("visuals") else ""
    language = LANGUAGES.get(channel.get("language") or "en", "English")
    prompt = f"""This video is for a YouTube channel about {channel['about']}.
Video title: {title}

Narration, numbered from 0, one scene per line:
{numbered}

For every scene, in order, choose ONE visual source:
- "nasa": NASA image library. Only for space: planets, moons, the Sun, stars, galaxies, spacecraft, rockets, astronauts, Earth from orbit.
- "stock_video": stock footage. The best choice for almost everything else: people, places, cities, nature, animals, history, technology, everyday life.
- "stock_photo": stock photo, when a still image fits better (objects, portraits, modern places).
- "archive": historical photos, artworks, documents and museum objects (Wikimedia Commons, Openverse, the Smithsonian). Best for history, famous people, real named places, inventions and artefacts.
The query must be 2-5 concrete, searchable English words describing what is visible on screen (e.g. "busy city street night", "old printing press", "lava flow ocean"). Never abstract ideas, never names of people who are not famous.{tips}

Return exactly {len(narrations)} visuals. Also write, in {language}, a YouTube description of 2-3 short paragraphs, 10 to 15 short tags and 2-5 punchy words of thumbnail text."""
    return claude_json(ANTHROPIC_MODEL, PLANNER_SYSTEM, prompt, VisualPlan).model_dump()


def script_from_text(title: str, text: str, suggest: bool = True, channel: dict | None = None) -> dict:
    """A script from your own narration. With suggest=True, Claude fills in the visuals you did not choose,
    the description, tags and thumbnail text; the narration is never changed."""
    scenes = parse_manual_script(text)
    if not scenes:
        raise ValueError("The script has no narration")
    script = {"title": title.strip(), "description": "", "tags": [], "thumbnail_text": "", "topic": title.strip(),
              "scenes": scenes}
    if suggest:
        log("Claude is choosing visuals and writing the description and tags (your narration stays as written)")
        plan = plan_visuals(script["title"], [s["narration"] for s in scenes], channel=channel)
        for scene, visual in zip(scenes, plan.get("visuals") or []):
            if not scene["visual"]["query"] and visual.get("query"):
                scene["visual"] = {"source": visual["source"], "query": visual["query"][:100]}
        script.update(description=plan.get("description", ""), tags=plan.get("tags", []),
                      thumbnail_text=plan.get("thumbnail_text", ""))
    for scene in scenes:
        if not scene["visual"]["query"]:
            scene["visual"]["query"] = keyword_query(scene["narration"], script["title"])
    return script


def fact_check(script: dict, project: Path, channel: dict | None = None) -> dict:
    """Two passes: research with web search (free text), then convert the notes to structured data."""
    channel = channel or project_channel(project)
    numbered = "\n".join(f"{i}. {s['narration']}" for i, s in enumerate(script["scenes"]))
    notes_prompt = f"""Fact-check this video narration. Scenes are numbered from 0.

{numbered}

Write your findings as notes with two sections:
1. Issues: for each factual error, outdated figure or misleading simplification give the scene number, what is wrong, the full corrected narration for that scene, and the URL of the source you used.
2. Claims to verify: specific numbers or claims a human should double-check against a primary source, each with the source you consulted, if any.
If the narration is sound, say "No issues found" and still list the claims worth verifying."""
    notes, searches = claude_research(FACTCHECK_MODEL, factcheck_system(channel), notes_prompt, FACTCHECK_SEARCHES)
    (project / "factcheck_notes.md").write_text(notes, encoding="utf-8")
    log(f"Fact-checker ran {searches} web search(es)")

    structure_prompt = f"""Convert these fact-check notes into the required structure. Keep every issue and every claim, and do not add new ones. The narration has {len(script['scenes'])} scenes numbered from 0.

{notes}"""
    check = claude_json(ANTHROPIC_MODEL, "You convert fact-check notes into structured data without changing their substance.",
                        structure_prompt, FactCheck).model_dump()
    check["searches"] = searches
    return check


def apply_fixes(script: dict, check: dict) -> int:
    applied = 0
    for issue in check.get("issues", []):
        i, fixed = issue.get("scene"), issue.get("fixed_narration")
        if isinstance(i, int) and 0 <= i < len(script["scenes"]) and fixed:
            script["scenes"][i]["narration"] = fixed
            applied += 1
    return applied


def narration_stats(script: dict) -> tuple[int, int]:
    """(words, characters) of the narration. ElevenLabs bills per character."""
    text = " ".join(s["narration"].strip() for s in script["scenes"])
    return len(text.split()), len(text)


def write_review(project: Path, script: dict, check: dict) -> Path:
    words, chars = narration_stats(script)
    lines = [f"# {script['title']}", "",
             f"Thumbnail text: **{script.get('thumbnail_text', '')}**", "",
             f"Narration: {words} words, **{chars} characters** (ElevenLabs bills per character), "
             f"about {words / WORDS_PER_MINUTE:.1f} minutes.", ""]
    lines += ["## Fact-check issues", ""]
    for issue in check.get("issues", []) or []:
        lines += [f"- Scene {issue.get('scene')}: {issue.get('problem')}",
                  f"  - Suggested: {issue.get('fixed_narration')}"]
        if issue.get("source"):
            lines.append(f"  - Source: {issue['source']}")
    if not check.get("issues"):
        lines.append("None found.")
    lines += ["", "## Claims to double-check yourself", ""]
    lines += [f"- {c}" for c in check.get("claims_to_verify", [])] or ["None listed."]
    lines += ["", f"The fact-checker's full notes are in factcheck_notes.md "
                  f"({check.get('searches', 0)} web search(es) used).", ""]
    lines += ["## Script", ""]
    for i, s in enumerate(script["scenes"]):
        v = s.get("visual", {})
        lines.append(f"{i}. [{v.get('source')}: {v.get('query')}] {s['narration']}")
    path = project / "review.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# ----------------------------------------------------------------------------
# Step 2: voiceover (ElevenLabs), one file per scene so visuals line up exactly
# ----------------------------------------------------------------------------
def tts(text: str, mp3: Path, align_path: Path, prev_text: str = "", next_text: str = "",
        voice_id: str | None = None) -> None:
    body = {
        "text": text,
        "model_id": ELEVENLABS_MODEL,
        "voice_settings": {"stability": VOICE_STABILITY, "similarity_boost": VOICE_SIMILARITY,
                           "style": VOICE_STYLE, "use_speaker_boost": VOICE_SPEAKER_BOOST,
                           "speed": VOICE_SPEED},
    }
    # Neighbouring text helps the voice keep a natural flow between separate clips.
    if prev_text:
        body["previous_text"] = prev_text
    if next_text:
        body["next_text"] = next_text
    # The with-timestamps variant costs the same and returns per-character timing for exact captions.
    r = HTTP.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id or ELEVENLABS_VOICE_ID}/with-timestamps",
        params={"output_format": "mp3_44100_128"},
        headers={"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"},
        json=body,
        timeout=180,
    )
    if r.status_code != 200:
        raise RuntimeError(f"ElevenLabs error {r.status_code}: {r.text[:500]}")
    data = r.json()
    mp3.write_bytes(base64.b64decode(data["audio_base64"]))
    align = data.get("alignment") or data.get("normalized_alignment")
    align_path.unlink(missing_ok=True)
    if align:
        save_json(align_path, {"characters": align["characters"],
                               "start": align["character_start_times_seconds"],
                               "end": align["character_end_times_seconds"]})


def audio_files(project: Path, i: int) -> dict:
    return {ext: project / "audio" / f"scene_{i:02d}.{ext}" for ext in ("mp3", "wav", "txt", "json")}


def voiceover_todo(project: Path, script: dict) -> list[int]:
    """Scenes whose narration has no clip yet or was edited since its clip was made."""
    todo = []
    for i, scene in enumerate(script["scenes"]):
        f = audio_files(project, i)
        if (not f["mp3"].exists() or not f["txt"].exists()
                or f["txt"].read_text(encoding="utf-8") != scene["narration"].strip()):
            todo.append(i)
    return todo


def make_voiceover(project: Path, script: dict) -> list[dict]:
    audio_dir = project / "audio"
    audio_dir.mkdir(exist_ok=True)
    scenes = script["scenes"]
    files = [audio_files(project, i) for i in range(len(scenes))]
    # Regenerate a clip only if the narration was edited since the last run.
    todo = voiceover_todo(project, script)
    voice_id = None
    if todo:
        chars = sum(len(scenes[i]["narration"].strip()) for i in todo)
        log(f"Voiceover: {len(todo)} scene(s) to synthesize, {chars} characters")
        voice_id = project_channel(project).get("voice_id") or None   # a channel can have its own voice
    timeline = []
    for i, (scene, f) in enumerate(zip(scenes, files)):
        text = scene["narration"].strip()
        if i in todo:
            log(f"Voiceover {i + 1}/{len(scenes)}")
            prev_text = scenes[i - 1]["narration"] if i > 0 else ""
            next_text = scenes[i + 1]["narration"] if i + 1 < len(scenes) else ""
            if voice_id:
                tts(text, f["mp3"], f["json"], prev_text, next_text, voice_id=voice_id)
            else:
                tts(text, f["mp3"], f["json"], prev_text, next_text)
            f["txt"].write_text(text, encoding="utf-8")
            f["wav"].unlink(missing_ok=True)
        speech = media_duration(f["mp3"])
        frames = round((speech + SCENE_PAD) * FPS)
        duration = frames / FPS
        if not f["wav"].exists():
            run(["ffmpeg", "-y", "-i", f["mp3"], "-af", "apad", "-t", f"{duration:.4f}",
                 "-ar", "44100", "-ac", "2", f["wav"]])
        timeline.append({"speech": speech, "duration": duration, "frames": frames})
    save_json(project / "timeline.json", timeline)
    log(f"Narration length: {sum(t['duration'] for t in timeline) / 60:.1f} min")
    return timeline


def estimated_timeline(script: dict) -> list[dict]:
    """Scene lengths guessed from word counts, for picking visuals before the voiceover exists."""
    out = []
    for scene in script["scenes"]:
        speech = len(scene["narration"].split()) * 60 / WORDS_PER_MINUTE
        frames = round((speech + SCENE_PAD) * FPS)
        out.append({"speech": speech, "duration": frames / FPS, "frames": frames})
    return out


# ----------------------------------------------------------------------------
# Step 3: visuals (NASA Image and Video Library + stock footage from Pexels and/or Pixabay)
# ----------------------------------------------------------------------------
# Each provider has a *_candidates function that lists its usable results with preview images (the web UI's
# visual picker shows these), and a *_search function that takes the first one not used yet (automatic picks).
ASSET_FIELDS = ("kind", "url", "id", "credit")   # what assets.json keeps of a search result
PER_PAGE = 15


def _https(url):
    return url.replace("http://", "https://", 1) if isinstance(url, str) else url


def _first_unused(candidates: list[dict], used: set, min_seconds: float = 0):
    """The first result not used yet, preferring clips at least min_seconds long (the order is otherwise kept)."""
    fresh = [c for c in candidates if c["id"] not in used]
    fresh.sort(key=lambda c: c["kind"] == "video" and (c.get("duration") or 0) < min_seconds)
    if not fresh:
        return None
    # "track" is a URL some providers (Unsplash) ask to be called when a result is actually used
    return {k: fresh[0][k] for k in (*ASSET_FIELDS, "track") if k in fresh[0]}


def nasa_candidates(query: str, per_page: int = PER_PAGE, page: int = 1) -> list[dict]:
    """NASA results carry a preview image; the full-size image URL is looked up only when one is used."""
    r = HTTP.get("https://images-api.nasa.gov/search",
                 params={"q": query, "media_type": "image", "page": page, "page_size": per_page},
                 timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    out = []
    for item in r.json().get("collection", {}).get("items", [])[:per_page]:
        data = (item.get("data") or [{}])[0]
        if not data.get("nasa_id") or not item.get("href"):
            continue
        who = data.get("photographer") or data.get("secondary_creator") or data.get("center") or "NASA"
        thumb = next((link.get("href") for link in item.get("links") or [] if link.get("render") == "image"), None)
        out.append({"kind": "image", "url": None, "collection": _https(item["href"]), "id": f"nasa:{data['nasa_id']}",
                    "credit": f"{data.get('title', 'NASA image')} ({who}; NASA image {data['nasa_id']})",
                    "thumb": _https(thumb), "title": data.get("title", ""), "provider": "NASA", "source": "nasa"})
    return out


def nasa_image_url(collection_url: str) -> str | None:
    """The best large JPEG or PNG listed in a NASA item's file list."""
    files = HTTP.get(collection_url, timeout=HTTP_TIMEOUT).json()
    for suffix in ("~large.jpg", "~orig.jpg", "~medium.jpg", "~large.png", "~orig.png"):
        url = next((u for u in files if isinstance(u, str) and u.lower().endswith(suffix)), None)
        if url:
            return _https(url)
    return None


def nasa_search(query: str, used: set, min_seconds: float = 0):
    for c in nasa_candidates(query, per_page=20):
        if c["id"] in used:
            continue
        try:
            url = nasa_image_url(c["collection"])
        except (requests.RequestException, ValueError, KeyError, TypeError):
            continue
        if url:
            return {"kind": "image", "url": url, "id": c["id"], "credit": c["credit"]}
    return None


def pexels_video_candidates(query: str, per_page: int = PER_PAGE, page: int = 1) -> list[dict]:
    if not PEXELS_API_KEY:
        return []
    r = HTTP.get("https://api.pexels.com/videos/search",
                 headers={"Authorization": PEXELS_API_KEY},
                 params={"query": query, "orientation": "landscape", "per_page": per_page, "page": page},
                 timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    out = []
    for v in r.json().get("videos", []):
        mp4 = [f for f in v.get("video_files", []) if f.get("file_type") == "video/mp4" and f.get("link")]
        files = [f for f in mp4 if (f.get("width") or 0) >= 1280 and (f.get("width") or 0) > (f.get("height") or 0)]
        if not files:
            continue
        best = min(files, key=lambda f: abs(f["width"] - W))
        small = min(mp4, key=lambda f: f.get("width") or 10 ** 6)
        who = (v.get("user") or {}).get("name") or "a Pexels contributor"
        out.append({"kind": "video", "url": best["link"], "id": f"pexels-video:{v['id']}",
                    "credit": f"Video by {who} on Pexels", "thumb": v.get("image"), "preview": small["link"],
                    "duration": v.get("duration"), "width": best.get("width"), "height": best.get("height"),
                    "provider": "Pexels", "source": "stock_video"})
    return out


def pexels_video_search(query: str, used: set, min_seconds: float = 0):
    return _first_unused(pexels_video_candidates(query), used, min_seconds)


def pexels_photo_candidates(query: str, per_page: int = PER_PAGE, page: int = 1) -> list[dict]:
    if not PEXELS_API_KEY:
        return []
    r = HTTP.get("https://api.pexels.com/v1/search",
                 headers={"Authorization": PEXELS_API_KEY},
                 params={"query": query, "orientation": "landscape", "per_page": per_page, "page": page},
                 timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    out = []
    for p in r.json().get("photos", []):
        src = p.get("src") or {}
        if not src.get("original"):
            continue
        out.append({"kind": "image", "url": src["original"], "id": f"pexels-photo:{p['id']}",
                    "credit": f"Photo by {p.get('photographer') or 'a Pexels contributor'} on Pexels",
                    "thumb": src.get("medium") or src.get("small"), "width": p.get("width"), "height": p.get("height"),
                    "provider": "Pexels", "source": "stock_photo"})
    return out


def pexels_photo_search(query: str, used: set, min_seconds: float = 0):
    return _first_unused(pexels_photo_candidates(query), used)


def pixabay_video_candidates(query: str, per_page: int = PER_PAGE, page: int = 1) -> list[dict]:
    if not PIXABAY_API_KEY:
        return []
    r = HTTP.get("https://pixabay.com/api/videos/",
                 params={"key": PIXABAY_API_KEY, "q": query[:100], "video_type": "film",
                         "safesearch": "true", "min_width": 1280, "per_page": per_page, "page": page},
                 timeout=HTTP_TIMEOUT)
    if page > 1 and getattr(r, "status_code", 200) == 400:   # Pixabay's answer to a page past the last result
        return []
    r.raise_for_status()
    out = []
    for v in r.json().get("hits", []):
        sizes = v.get("videos") or {}
        files = [f for f in sizes.values() if f.get("url") and (f.get("width") or 0) >= 1280
                 and (f.get("width") or 0) > (f.get("height") or 0)]
        if not files:
            continue
        best = min(files, key=lambda f: abs(f["width"] - W))
        small = next((sizes[k] for k in ("tiny", "small", "medium") if (sizes.get(k) or {}).get("url")), best)
        thumb = next((sizes[k]["thumbnail"] for k in ("medium", "small", "large", "tiny")
                      if (sizes.get(k) or {}).get("thumbnail")), None)
        if not thumb and v.get("picture_id"):   # older API responses
            thumb = f"https://i.vimeocdn.com/video/{v['picture_id']}_640x360.jpg"
        out.append({"kind": "video", "url": best["url"], "id": f"pixabay-video:{v['id']}",
                    "credit": f"Video by {v.get('user') or 'a Pixabay contributor'} on Pixabay",
                    "thumb": thumb, "preview": small["url"], "duration": v.get("duration"),
                    "width": best.get("width"), "height": best.get("height"),
                    "provider": "Pixabay", "source": "stock_video"})
    return out


def pixabay_video_search(query: str, used: set, min_seconds: float = 0):
    return _first_unused(pixabay_video_candidates(query), used, min_seconds)


def pixabay_photo_candidates(query: str, per_page: int = PER_PAGE, page: int = 1) -> list[dict]:
    """Standard Pixabay keys get images up to 1280 px wide, so these are upscaled to 1080p."""
    if not PIXABAY_API_KEY:
        return []
    r = HTTP.get("https://pixabay.com/api/",
                 params={"key": PIXABAY_API_KEY, "q": query[:100], "image_type": "photo",
                         "orientation": "horizontal", "safesearch": "true", "min_width": 1280,
                         "per_page": per_page, "page": page},
                 timeout=HTTP_TIMEOUT)
    if page > 1 and getattr(r, "status_code", 200) == 400:
        return []
    r.raise_for_status()
    out = []
    for p in r.json().get("hits", []):
        if not p.get("largeImageURL"):
            continue
        out.append({"kind": "image", "url": p["largeImageURL"], "id": f"pixabay-photo:{p['id']}",
                    "credit": f"Photo by {p.get('user') or 'a Pixabay contributor'} on Pixabay",
                    "thumb": p.get("webformatURL") or p.get("previewURL"),
                    "width": p.get("imageWidth"), "height": p.get("imageHeight"),
                    "provider": "Pixabay", "source": "stock_photo"})
    return out


def pixabay_photo_search(query: str, used: set, min_seconds: float = 0):
    return _first_unused(pixabay_photo_candidates(query), used)


# --- Open archives: free to use, licence checked per file -------------------------------------------
_OPEN_LICENSE = re.compile(r"^(public domain|pd([- ].*)?|no restrictions|cc0( 1\.0)?|cc[ -]by( \d\.\d)?)$", re.I)
_SHARE_ALIKE = re.compile(r"^cc[ -]by[ -]sa( \d\.\d)?$", re.I)


def license_ok(name: str) -> bool:
    """Public domain, CC0 and CC BY are fine for a monetised video. CC BY-SA only when ALLOW_SHARE_ALIKE is on,
    because share-alike can require the video itself to carry the same licence."""
    name = " ".join((name or "").split())
    return bool(_OPEN_LICENSE.match(name) or (ALLOW_SHARE_ALIKE and _SHARE_ALIKE.match(name)))


def _strip_html(s) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", html.unescape(s or "")).split())


def _commons_files(query: str, filetype: str, per_page: int, page: int) -> list[dict]:
    r = HTTP.get("https://commons.wikimedia.org/w/api.php", timeout=HTTP_TIMEOUT, params={
        "action": "query", "format": "json", "formatversion": 2, "generator": "search", "gsrnamespace": 6,
        "gsrsearch": f"{query[:200]} filetype:{filetype}", "gsrlimit": per_page, "gsroffset": (page - 1) * per_page,
        "prop": "imageinfo", "iiprop": "url|size|mime|extmetadata", "iiurlwidth": 1920,
        "iiextmetadatafilter": "LicenseShortName|Artist|ObjectName"})
    r.raise_for_status()
    pages = r.json().get("query", {}).get("pages", [])
    out = []
    for p in sorted(pages, key=lambda p: p.get("index", 0)):
        info = (p.get("imageinfo") or [{}])[0]
        meta = info.get("extmetadata") or {}
        lic = (meta.get("LicenseShortName") or {}).get("value", "")
        if not info.get("url") or not license_ok(lic):
            continue
        title = _strip_html((meta.get("ObjectName") or {}).get("value")) or Path(p.get("title", "")).stem.removeprefix("File:")
        artist = _strip_html((meta.get("Artist") or {}).get("value")) or "unknown author"
        credit = f"{title} by {artist} ({lic}), via Wikimedia Commons: {info.get('descriptionurl', '')}"
        out.append({"info": info, "pageid": p.get("pageid"), "title": title, "credit": credit[:400], "license": lic})
    return out


def commons_image_candidates(query: str, per_page: int = PER_PAGE, page: int = 1) -> list[dict]:
    out = []
    for f in _commons_files(query, "bitmap", per_page, page):
        info = f["info"]
        w, h = info.get("width") or 0, info.get("height") or 0
        if info.get("mime") not in ("image/jpeg", "image/png", "image/webp") or w < 1000 or w < h:
            continue
        full = info.get("thumburl") or info["url"]   # at most 1920 px wide
        thumb = full.replace("/1920px-", "/640px-") if "/1920px-" in full else full
        out.append({"kind": "image", "url": full, "id": f"commons:{f['pageid']}", "credit": f["credit"], "thumb": thumb,
                    "width": w, "height": h, "title": f["title"], "provider": "Wikimedia Commons",
                    "source": "archive", "license": f["license"]})
    return out


def commons_video_candidates(query: str, per_page: int = PER_PAGE, page: int = 1) -> list[dict]:
    out = []
    for f in _commons_files(query, "video", per_page, page):
        info = f["info"]
        w, h = info.get("width") or 0, info.get("height") or 0
        if (info.get("mime") not in ("video/webm", "video/ogg", "video/mp4") or w < 1280 or w < h
                or (info.get("size") or 0) > 300 * 1024 ** 2):
            continue
        out.append({"kind": "video", "url": info["url"], "id": f"commons:{f['pageid']}", "credit": f["credit"],
                    "thumb": info.get("thumburl"), "duration": round(info.get("duration") or 0) or None,
                    "width": w, "height": h, "title": f["title"], "provider": "Wikimedia Commons",
                    "source": "stock_video", "license": f["license"]})
    return out


def openverse_candidates(query: str, per_page: int = PER_PAGE, page: int = 1) -> list[dict]:
    """Openly licensed images from Flickr, museums and others. Anonymous use allows 20 searches a minute."""
    r = HTTP.get("https://api.openverse.org/v1/images/", timeout=HTTP_TIMEOUT,
                 params={"q": query[:200], "license_type": "commercial,modification", "page_size": per_page,
                         "page": page, "mature": "false"})
    if page > 1 and getattr(r, "status_code", 200) in (400, 404):   # past the last page
        return []
    r.raise_for_status()
    out = []
    for x in r.json().get("results", []):
        code = (x.get("license") or "").lower()
        lic = {"cc0": "CC0", "pdm": "Public domain"}.get(code, f"CC {code.upper()} {x.get('license_version') or ''}".strip())
        w, h = x.get("width") or 0, x.get("height") or 0
        if x.get("mature") or not x.get("url") or not license_ok(lic) or (w and w < 1000) or (w and h and w < h):
            continue
        credit = x.get("attribution") or f"{x.get('title') or 'Image'} by {x.get('creator') or 'unknown'} ({lic})"
        out.append({"kind": "image", "url": x["url"], "id": f"openverse:{x['id']}", "credit": credit[:400],
                    "thumb": x.get("thumbnail") or x["url"], "width": w or None, "height": h or None,
                    "title": x.get("title") or "", "provider": f"Openverse ({x.get('source') or x.get('provider')})",
                    "source": "archive", "license": lic})
    return out


def _si_sized(media: dict, size: int) -> str:
    """A Smithsonian image URL at most `size` px, following the pattern of its own thumbnail URL."""
    content, thumb = media.get("content") or "", media.get("thumbnail") or ""
    if content and thumb.startswith(content):
        tail = thumb[len(content):]
        sized = re.sub(r"\d+$", str(size), tail)
        if sized != tail:
            return content + sized
    return content


def smithsonian_candidates(query: str, per_page: int = PER_PAGE, page: int = 1) -> list[dict]:
    """CC0 (public domain) images from the Smithsonian's museums. Needs a free key from api.data.gov."""
    if not SMITHSONIAN_API_KEY:
        return []
    r = HTTP.get("https://api.si.edu/openaccess/api/v1.0/search", timeout=HTTP_TIMEOUT,
                 params={"q": f'{query[:200]} AND online_media_type:"Images" AND media_usage:"CC0"',
                         "rows": per_page, "start": (page - 1) * per_page, "api_key": SMITHSONIAN_API_KEY})
    r.raise_for_status()
    out = []
    for row in r.json().get("response", {}).get("rows", []):
        dnr = (row.get("content") or {}).get("descriptiveNonRepeating") or {}
        media = ((dnr.get("online_media") or {}).get("media")) or []
        m = next((m for m in media if m.get("type") == "Images" and (m.get("usage") or {}).get("access") == "CC0"
                  and m.get("content")), None)
        if not m:
            continue
        w, h = m.get("width") or 0, m.get("height") or 0
        if w and h and w < h:
            continue
        title = _strip_html(row.get("title")) or "Smithsonian collection object"
        museum = dnr.get("data_source") or "Smithsonian Institution"
        out.append({"kind": "image", "url": _si_sized(m, 1920), "id": f"si:{m.get('id') or row.get('id')}",
                    "credit": f"{title} ({museum}, Smithsonian Open Access, CC0)"[:400],
                    "thumb": _si_sized(m, 640), "width": w or None, "height": h or None, "title": title,
                    "provider": "Smithsonian", "source": "archive", "license": "CC0"})
    return out


def unsplash_candidates(query: str, per_page: int = PER_PAGE, page: int = 1) -> list[dict]:
    """Unsplash photos. Its API rules: credit the photographer and report each photo you actually use."""
    if not UNSPLASH_ACCESS_KEY:
        return []
    r = HTTP.get("https://api.unsplash.com/search/photos", timeout=HTTP_TIMEOUT,
                 headers={"Authorization": f"Client-ID {UNSPLASH_ACCESS_KEY}", "Accept-Version": "v1"},
                 params={"query": query[:200], "orientation": "landscape", "per_page": per_page, "page": page,
                         "content_filter": "high"})
    r.raise_for_status()
    out = []
    for p in r.json().get("results", []):
        urls = p.get("urls") or {}
        if not urls.get("raw"):
            continue
        name = (p.get("user") or {}).get("name") or "an Unsplash photographer"
        sep = "&" if "?" in urls["raw"] else "?"
        out.append({"kind": "image", "url": f"{urls['raw']}{sep}w=1920&fm=jpg&q=85", "id": f"unsplash:{p['id']}",
                    "credit": f"Photo by {name} on Unsplash", "thumb": urls.get("small") or urls.get("thumb"),
                    "width": p.get("width"), "height": p.get("height"), "title": p.get("alt_description") or "",
                    "provider": "Unsplash", "source": "stock_photo",
                    "track": (p.get("links") or {}).get("download_location")})
    return out


def report_use(asset: dict) -> None:
    """Tell a provider its result was used, where its API rules ask for that (Unsplash)."""
    url = asset.pop("track", None)
    if url and UNSPLASH_ACCESS_KEY and url.startswith("https://api.unsplash.com/"):
        try:
            HTTP.get(url, headers={"Authorization": f"Client-ID {UNSPLASH_ACCESS_KEY}"}, timeout=20)
        except requests.RequestException:
            pass


# --- Provider registry: which providers are on, in which order, and what each one can search -------
PROVIDERS = {
    "pexels": {"name": "Pexels", "key": "PEXELS_API_KEY", "about": "Stock video and photos",
               "kinds": {"stock_video": "pexels_video_candidates", "stock_photo": "pexels_photo_candidates"}},
    "pixabay": {"name": "Pixabay", "key": "PIXABAY_API_KEY", "about": "Stock video and photos",
                "kinds": {"stock_video": "pixabay_video_candidates", "stock_photo": "pixabay_photo_candidates"}},
    "unsplash": {"name": "Unsplash", "key": "UNSPLASH_ACCESS_KEY", "about": "High-quality photos",
                 "kinds": {"stock_photo": "unsplash_candidates"}},
    "wikimedia": {"name": "Wikimedia Commons", "key": None,
                  "about": "History, people, places and documentary footage (free licences, credited)",
                  "kinds": {"archive": "commons_image_candidates", "stock_video": "commons_video_candidates"}},
    "openverse": {"name": "Openverse", "key": None, "about": "Openly licensed images from Flickr and museums",
                  "kinds": {"archive": "openverse_candidates"}},
    "smithsonian": {"name": "Smithsonian", "key": "SMITHSONIAN_API_KEY", "about": "Museum objects and art (CC0)",
                    "kinds": {"archive": "smithsonian_candidates"}},
    "nasa": {"name": "NASA", "key": None, "about": "Space, planets, missions",
             "kinds": {"nasa": "nasa_candidates"}},
}


def provider_ready(pid: str) -> bool:
    p = PROVIDERS.get(pid)
    return bool(p) and pid in VISUAL_PROVIDERS and (not p["key"] or bool(globals().get(p["key"])))


def providers_for(source: str) -> list[tuple[str, str]]:
    """(name, function name) of the providers that are on and can search this source, in the configured order."""
    return [(PROVIDERS[pid]["name"], PROVIDERS[pid]["kinds"][source]) for pid in VISUAL_PROVIDERS
            if pid in PROVIDERS and source in PROVIDERS[pid]["kinds"] and provider_ready(pid)]


def search_source(source: str, query: str, used: set, min_seconds: float = 0):
    """The first unused result for one kind of source, trying each provider in turn so one failing never hides
    the others."""
    for name, fn in providers_for(source):
        try:
            found = nasa_search(query, used, min_seconds) if fn == "nasa_candidates" else \
                _first_unused(globals()[fn](query), used, min_seconds)
        except (requests.RequestException, ValueError, KeyError, TypeError) as e:
            log(f"  {name} search failed for '{query}': {e}")
            continue
        if found:
            return found
    return None


def stock_video_search(query: str, used: set, min_seconds: float = 0):
    return search_source("stock_video", query, used, min_seconds)


def stock_photo_search(query: str, used: set, min_seconds: float = 0):
    return search_source("stock_photo", query, used, min_seconds)


def archive_search(query: str, used: set, min_seconds: float = 0):
    return search_source("archive", query, used, min_seconds)


def nasa_source_search(query: str, used: set, min_seconds: float = 0):
    return search_source("nasa", query, used, min_seconds)


def stock_providers() -> list[str]:
    """Names of the stock-footage providers that are on and have an API key."""
    return [PROVIDERS[p]["name"] for p in VISUAL_PROVIDERS if p in ("pexels", "pixabay", "unsplash") and provider_ready(p)]


def search_candidates(source: str, query: str, page: int = 1) -> tuple[list[dict], list[str]]:
    """Every provider's results for one kind of source, interleaved, for the visual picker. Returns (results, errors)."""
    providers = [(name, globals()[fn]) for name, fn in providers_for(source)]
    if not providers:
        return [], []
    with ThreadPoolExecutor(max_workers=len(providers)) as pool:
        futures = [(name, pool.submit(fn, query, PER_PAGE, page)) for name, fn in providers]
    lists, errors = [], []
    for name, future in futures:
        try:
            lists.append(future.result())
        except (requests.RequestException, ValueError, KeyError, TypeError) as e:
            errors.append(f"{name} search failed: {e}")
    results = [c for group in itertools.zip_longest(*lists) for c in group if c]
    return results, errors


SEARCHERS = {"nasa": nasa_source_search, "stock_video": stock_video_search, "stock_photo": stock_photo_search,
             "archive": archive_search}
# Source names used by older script.json files
SOURCE_ALIASES = {"pexels_video": "stock_video", "pexels_photo": "stock_photo",
                  "pixabay_video": "stock_video", "pixabay_photo": "stock_photo"}
FALLBACK_QUERIES = ["earth from space", "milky way", "planet earth", "stars night sky"]


# --- Claude as picture editor: it looks at the results' previews and ranks the ones that fit -----------
class VisualChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ranking: List[int] = Field(description="Numbers of the images that suit this moment of the narration, best first. "
                                           "Leave out any that do not fit, are misleading, or show watermarks, "
                                           "captions or logos.")
    reason: str = Field(description="One short sentence: why the first one fits best, or why none fits")


VISION_SYSTEM = """You are the picture editor of a faceless YouTube channel. For each moment of narration you choose the image that best shows what the narrator is talking about. Prefer images that are accurate for the subject (the right planet, animal, place, person or era), clear at a glance and uncluttered. Reject images with watermarks, burned-in captions or logos, and anything that would mislead viewers."""


def preview_data(url: str, size: int = 512) -> str | None:
    """A small JPEG of a result's preview image, base64-encoded for Claude, or None if it cannot be loaded."""
    try:
        r = HTTP.get(url, timeout=20)
        r.raise_for_status()
        from PIL import Image
        with Image.open(io.BytesIO(r.content)) as img:
            img = img.convert("RGB")
            img.thumbnail((size, size))
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=80)
        return base64.b64encode(buf.getvalue()).decode()
    except Exception:   # any broken or blocked preview is simply left out
        return None


def rank_visuals(narration: str, query: str, candidates: list[dict], limit: int = 12) -> tuple[list[int], str]:
    """Claude ranks the results whose previews fit the narration. Returns (indices into candidates, reason)."""
    pairs = [(k, c) for k, c in enumerate(candidates) if c.get("thumb")][:limit]
    if not pairs:
        return [], "None of the results has a preview image"
    with ThreadPoolExecutor(max_workers=8) as pool:
        datas = list(pool.map(lambda c: preview_data(c["thumb"]), [c for _, c in pairs]))
    shown = [(k, c, data) for (k, c), data in zip(pairs, datas) if data]
    if not shown:
        return [], "None of the preview images could be loaded"
    content = [{"type": "text", "text": f"Narration at this moment:\n{narration}\n\nSearch words: {query}\n\n"
                                        f"{len(shown)} candidate images follow, numbered from 1. A video is shown "
                                        "as one still frame."}]
    for n, (k, c, data) in enumerate(shown, start=1):
        label = f"Image {n}" + (f" (video, {c['duration']} s)" if c["kind"] == "video" and c.get("duration") else "")
        if c.get("title"):
            label += f": {str(c['title'])[:120]}"
        content += [{"type": "text", "text": label},
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}}]
    content.append({"type": "text", "text": "Rank the images that suit this moment of the narration, best first."})
    choice = claude_json(VISION_MODEL, VISION_SYSTEM, content, VisualChoice, max_tokens=1000)
    order = list(dict.fromkeys(shown[n - 1][0] for n in choice.ranking if 1 <= n <= len(shown)))
    return order, choice.reason


def claude_pick(scene: dict, used: set, min_seconds: float) -> dict | None:
    """Claude's choice among the unused results for this scene's own search (AUTO_PICK=auto), or None."""
    visual = scene.get("visual") or {}
    source = SOURCE_ALIASES.get(visual.get("source"), visual.get("source"))
    query = (visual.get("query") or "").strip()
    if source not in SEARCHERS or not query:
        return None
    results, _ = search_candidates(source, query)
    fresh = [c for c in results if c["id"] not in used]
    if not fresh:
        return None
    order, reason = rank_visuals(scene.get("narration", ""), query, fresh)
    for k in order:
        c = fresh[k]
        url = c.get("url") or (nasa_image_url(c["collection"]) if c.get("collection") else None)
        if url:
            log(f"  Claude chose: {c['credit'][:90]} ({reason})")
            picked = {key: c[key] for key in (*ASSET_FIELDS, "track") if key in c}
            return dict(picked, url=url, query=query, source=source)
    log(f"  Claude found none of the results suitable ({reason}); using the first result instead")
    return None


def find_visual(scene: dict, used: set, min_seconds: float) -> dict:
    if AUTO_PICK == "auto" and os.getenv("ANTHROPIC_API_KEY"):
        try:
            picked = claude_pick(scene, used, min_seconds)
            if picked:
                return picked
        except Exception as e:   # Claude unavailable: fall back to the usual order rather than stopping the render
            log(f"  Claude could not choose a visual ({type(e).__name__}: {str(e)[:150]}); using the first result")
    visual = scene.get("visual") or {}
    preferred = SOURCE_ALIASES.get(visual.get("source"), visual.get("source"))
    if preferred not in SEARCHERS:   # script.json may have been edited by hand
        if preferred:
            log(f"  Unknown visual source '{preferred}', trying nasa first")
        preferred = "nasa"
    order = [preferred] + [s for s in SEARCHERS if s != preferred]
    query = (visual.get("query") or "").strip() or "earth from space"
    short_query = " ".join(query.split()[:2])
    attempts = [(src, query) for src in order]
    if short_query != query:
        attempts += [(src, short_query) for src in order]
    attempts += [("nasa", q) for q in FALLBACK_QUERIES]
    for source, q in attempts:
        try:
            result = SEARCHERS[source](q, used, min_seconds)
        except requests.RequestException as e:
            log(f"  {source} search failed for '{q}': {e}")
            continue
        if result:
            result["query"], result["source"] = q, source
            return result
    raise RuntimeError(f"No visual found for scene: {query}")


def load_manifest(project: Path) -> list:
    """assets.json, with each file path re-anchored to this project folder (so a moved project still works)."""
    path = project / "assets.json"
    manifest = load_json(path) if path.exists() else []
    for asset in manifest:
        for a in ([asset] + (asset.get("shots") or [])) if asset else []:
            if a and a.get("path"):
                a["path"] = str(project / "assets" / Path(a["path"]).name)
    return manifest


def shots_wanted(duration: float) -> int:
    """How many different visuals a scene shows: one, or with SHOT_SECONDS set, one per that many seconds (max 3)."""
    if SHOT_SECONDS <= 0:
        return 1
    return min(3, max(1, int(duration // SHOT_SECONDS)))


def scene_shots(asset: dict, duration: float) -> list[dict]:
    """The visuals a scene shows, in order: its main visual plus the extra shots it needs and has."""
    extras = [s for s in (asset.get("shots") or []) if s and Path(s.get("path", "")).exists()]
    return [asset] + extras[:shots_wanted(duration) - 1]


def store_shot(project: Path, manifest: list, i: int, shot: int, asset: dict, download_from: str | None = None,
               copy_from: Path | None = None) -> None:
    """Save extra shot number `shot` (2 or 3) of scene i as assets/scene_XX_sN.<ext>."""
    asset_dir = project / "assets"
    asset_dir.mkdir(exist_ok=True)
    source = download_from or str(copy_from)
    suffix = Path(urlparse(source).path if download_from else source).suffix.lower()
    if suffix not in (".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov", ".webm", ".mkv", ".ogv"):
        suffix = ".mp4" if asset["kind"] == "video" else ".jpg"
    dest = asset_dir / f"scene_{i:02d}_s{shot}{suffix}"
    if download_from:
        download(download_from, dest)
        if asset["kind"] == "image":
            try:
                from PIL import Image
                with Image.open(dest) as img:
                    img.verify()
            except Exception:
                dest.unlink(missing_ok=True)
                raise RuntimeError(f"The picture downloaded for scene {i + 1}, shot {shot} could not be read")
        report_use(asset)
    else:
        shutil.copyfile(copy_from, dest)
    for old in asset_dir.glob(f"scene_{i:02d}_s{shot}.*"):
        if old != dest and not old.name.endswith(".part"):
            old.unlink()
    asset["path"] = str(dest)
    shots = manifest[i].setdefault("shots", [])
    while len(shots) < shot - 1:
        shots.append(None)
    shots[shot - 2] = asset
    save_json(project / "assets.json", manifest)


def gather_shots(project: Path, script: dict, timeline: list[dict], manifest: list, used: set) -> None:
    """Fetch the extra shots that long scenes need (SHOT_SECONDS), from the same search as their main visual."""
    for i, scene in enumerate(script["scenes"]):
        want = shots_wanted(timeline[i]["duration"])
        if want < 2 or i >= len(manifest) or not manifest[i]:
            continue
        if (scene.get("visual") or {}).get("source") == "ai":
            continue   # AI scenes only get extra shots you generate and pick yourself
        for shot in range(2, want + 1):
            have = (manifest[i].get("shots") or [])[shot - 2:shot - 1]
            if have and have[0] and Path(have[0].get("path", "")).exists():
                continue
            log(f"Scene {i + 1}: extra shot {shot} of {want}")
            for attempt in range(3):
                asset = find_visual(scene, used, timeline[i]["duration"] / want)
                used.add(asset["id"])
                try:
                    store_shot(project, manifest, i, shot, asset, download_from=asset["url"])
                    break
                except (RuntimeError, requests.RequestException) as e:
                    if attempt == 2:
                        raise
                    log(f"  {str(e)[:200]}; trying the next result")


def rejected_assets(project: Path) -> set:
    path = project / "rejected_assets.json"
    return set(load_json(path)) if path.exists() else set()


def reject_asset(project: Path, asset_id: str, rejected: bool = True) -> None:
    """Remember a visual you swapped out so 'find another' never offers it again (rejected=False forgets that)."""
    before = rejected_assets(project)
    ids = before | {asset_id} if rejected else before - {asset_id}
    if ids != before:
        save_json(project / "rejected_assets.json", sorted(ids))


def gather_visuals(project: Path, script: dict, timeline: list[dict]) -> list[dict]:
    manifest = load_manifest(project)
    used = {a["id"] for a in manifest if a} | {s["id"] for a in manifest if a for s in a.get("shots") or [] if s} \
        | rejected_assets(project)
    for i, scene in enumerate(script["scenes"]):
        if i < len(manifest) and manifest[i] and Path(manifest[i]["path"]).exists():
            continue
        log(f"Visual {i + 1}/{len(script['scenes'])}: {scene.get('visual', {}).get('query')}")
        fetch_into(project, manifest, i, scene, used, timeline[i]["duration"])
    gather_shots(project, script, timeline, manifest, used)
    return manifest[:len(script["scenes"])]


def fetch_into(project: Path, manifest: list, i: int, scene: dict, used: set, min_seconds: float) -> dict:
    """Find and download a visual for scene i, moving on to the next result if a download is broken.
    Scenes set to "ai" get a newly generated image when AI visuals are on, and a stock photo otherwise."""
    if (scene.get("visual") or {}).get("source") == "ai":
        if ai_enabled():
            item = generate_ai_visual(project, ai_prompt(scene), "image", 1)[0]
            return use_visual(project, i, item, manifest=manifest)
        log(f"  Scene {i + 1} is set to an AI image, but AI visuals are off in Settings; using a stock photo")
        scene = dict(scene, visual=dict(scene["visual"], source="stock_photo"))
    for attempt in range(3):
        asset = find_visual(scene, used, min_seconds)
        used.add(asset["id"])
        try:
            store_asset(project, manifest, i, asset, download_from=asset["url"])
            return asset
        except (RuntimeError, requests.RequestException) as e:
            if attempt == 2:
                raise
            log(f"  {str(e)[:200]}; trying the next result")
    raise RuntimeError("unreachable")


def store_asset(project: Path, manifest: list, i: int, asset: dict, download_from: str | None = None,
                copy_from: Path | None = None) -> None:
    """Save a scene's visual as assets/scene_XX.<ext> and record it in assets.json."""
    asset_dir = project / "assets"
    asset_dir.mkdir(exist_ok=True)
    source = download_from or str(copy_from)
    suffix = Path(urlparse(source).path if download_from else source).suffix.lower()
    if suffix not in (".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov", ".webm", ".mkv", ".ogv"):
        suffix = ".mp4" if asset["kind"] == "video" else ".jpg"
    dest = asset_dir / f"scene_{i:02d}{suffix}"
    if download_from:
        download(download_from, dest)
        if asset["kind"] == "image":   # some archives answer with an error page instead of the picture
            try:
                from PIL import Image
                with Image.open(dest) as img:
                    img.verify()
            except Exception:
                dest.unlink(missing_ok=True)
                raise RuntimeError(f"The picture downloaded for scene {i + 1} could not be read ({download_from[:120]})")
        report_use(asset)
    else:
        shutil.copyfile(copy_from, dest)
    for old in asset_dir.glob(f"scene_{i:02d}.*"):   # a swapped visual may have had another file type
        if old != dest and not old.name.endswith(".part"):
            old.unlink()
    asset["path"] = str(dest)
    while len(manifest) <= i:
        manifest.append(None)
    if manifest[i] and manifest[i].get("shots"):   # a new main visual keeps the scene's extra shots
        asset["shots"] = manifest[i]["shots"]
    manifest[i] = asset
    save_json(project / "assets.json", manifest)   # saved after each scene so a crash loses nothing


def refetch_visual(project: Path, script: dict, timeline: list[dict], i: int) -> dict:
    """Replace one scene's visual with the next-best search result, never offering a swapped-out one again."""
    manifest = load_manifest(project)
    old = manifest[i] if i < len(manifest) else None
    used = {a["id"] for a in manifest if a} | rejected_assets(project)
    log(f"Searching for another visual for scene {i + 1}: {script['scenes'][i]['visual']['query']}")
    asset = fetch_into(project, manifest, i, script["scenes"][i], used, timeline[i]["duration"])
    if old:
        reject_asset(project, old["id"])
    log(f"Scene {i + 1} now uses: {asset['credit']}")
    return asset


def use_visual(project: Path, i: int, picked: dict, manifest: list | None = None, shot: int = 1) -> dict:
    """Use a picked result for scene i (shot 1 = its main visual, 2 or 3 = an extra shot): a search result
    (downloaded; NASA results get their full-size image looked up first) or an AI-generated file in ai/."""
    local = project / "ai" / picked["file"] if picked.get("file") else None
    url = None if local else picked.get("url") or (
        nasa_image_url(picked["collection"]) if picked.get("collection") else None)
    if not url and not (local and local.is_file()):
        raise RuntimeError("That result has no downloadable file; pick another one")
    manifest = load_manifest(project) if manifest is None else manifest
    if shot > 1 and (i >= len(manifest) or not manifest[i]):
        raise RuntimeError(f"Scene {i + 1} needs its main visual before extra shots")
    current = manifest[i] if i < len(manifest) else None
    old = current if shot == 1 else ((current.get("shots") or [])[shot - 2:shot - 1] or [None])[0]
    asset = {"kind": picked["kind"], "url": url or "", "id": picked["id"], "credit": picked.get("credit", ""),
             "query": picked.get("query", ""), "source": picked.get("source", "")}
    if picked.get("track"):
        asset["track"] = picked["track"]
    store = store_asset if shot == 1 else (lambda *a, **kw: store_shot(a[0], a[1], a[2], shot, *a[3:], **kw))
    if local:
        store(project, manifest, i, asset, copy_from=local)
    else:
        log(f"Downloading the {asset['kind']} you picked for scene {i + 1}{f', shot {shot}' if shot > 1 else ''} "
            f"({picked.get('provider', asset['source'])})")
        store(project, manifest, i, asset, download_from=url)
    if old and old.get("id") != asset["id"]:
        reject_asset(project, old["id"])
    reject_asset(project, asset["id"], rejected=False)
    log(f"Scene {i + 1}{f' (shot {shot})' if shot > 1 else ''} now uses: {asset['credit']}")
    return asset


# ----------------------------------------------------------------------------
# AI-generated visuals (fal.ai). Generated files are kept in the project's ai/ folder, so each one is paid once
# and can be picked (again) later.
# ----------------------------------------------------------------------------
AI_STYLE = " Photorealistic, cinematic documentary style, natural lighting, no text, no watermark."


def ai_enabled(kind: str = "image") -> bool:
    if not FAL_KEY or AI_VISUALS == "off":
        return False
    return kind == "image" or AI_VISUALS == "images_and_video"


def ai_prompt(scene: dict) -> str:
    """A prompt from the scene's visual search words (the narration adds context)."""
    query = ((scene.get("visual") or {}).get("query") or "").strip()
    return f"{query or scene.get('narration', '')[:200]}.{AI_STYLE}"


def fal_run(model: str, arguments: dict, timeout: float = 900) -> dict:
    """Run a fal.ai model through its queue and return its result."""
    if not FAL_KEY:
        raise RuntimeError("FAL_KEY is not set in .env (get a key at fal.ai)")
    headers = {"Authorization": f"Key {FAL_KEY}", "Content-Type": "application/json"}
    # A plain POST, not the retrying session: a retried submission could be generated (and billed) twice
    r = requests.post(f"https://queue.fal.run/{model}", headers=headers, json=arguments, timeout=60)
    if r.status_code >= 400:
        raise RuntimeError(f"fal.ai refused the request ({r.status_code}): {r.text[:300]}")
    submitted = r.json()
    status_url, response_url = submitted["status_url"], submitted["response_url"]
    end, last = time.time() + timeout, None
    while True:
        s = HTTP.get(status_url, headers=headers, timeout=30).json()
        state = s.get("status")
        if state == "COMPLETED":
            if s.get("error"):
                raise RuntimeError(f"fal.ai could not generate it: {s['error']}")
            break
        if state != last:
            pos = f" (position {s['queue_position']})" if s.get("queue_position") is not None else ""
            log(f"  fal.ai: {str(state).lower().replace('_', ' ')}{pos}")
            last = state
        if time.time() > end:
            raise RuntimeError(f"fal.ai did not finish within {timeout / 60:.0f} minutes")
        time.sleep(2)
    res = HTTP.get(response_url, headers=headers, timeout=60)
    if res.status_code >= 400:
        raise RuntimeError(f"fal.ai result error {res.status_code}: {res.text[:300]}")
    return res.json()


def ai_items(project: Path) -> list[dict]:
    path = project / "ai" / "index.json"
    return load_json(path) if path.exists() else []


def generate_ai_visual(project: Path, prompt: str, kind: str = "image", count: int = 1) -> list[dict]:
    """Generate images (or one video clip) for a project and keep them in ai/. Returns the new items."""
    if not ai_enabled(kind):
        raise RuntimeError("AI video clips are switched off in Settings" if kind == "video" and ai_enabled()
                           else "AI visuals are switched off in Settings, or FAL_KEY is not set in .env")
    prompt = " ".join(prompt.split())[:1500]
    if kind == "image":
        model, args = AI_IMAGE_MODEL, {"prompt": prompt, "image_size": "landscape_16_9", "num_images": count}
        log(f"Generating {count} AI image(s) with {model} (about ${AI_IMAGE_COST * count:.2f})")
    else:
        model, args = AI_VIDEO_MODEL, {"prompt": prompt, "aspect_ratio": "16:9", "duration": "5"}
        log(f"Generating a 5-second AI video clip with {model} (about ${AI_VIDEO_COST:.2f}); this can take minutes")
    result = fal_run(model, args)
    urls = [im["url"] for im in result.get("images") or [] if im.get("url")] if kind == "image" else \
        [(result.get("video") or {}).get("url")]
    folder = project / "ai"
    folder.mkdir(parents=True, exist_ok=True)
    items, new = ai_items(project), []
    for url in filter(None, urls):
        gid = uuid.uuid4().hex[:10]
        suffix = Path(urlparse(url).path).suffix.lower()
        suffix = suffix if suffix in ((".jpg", ".jpeg", ".png", ".webp") if kind == "image" else (".mp4", ".webm", ".mov")) \
            else (".jpg" if kind == "image" else ".mp4")
        dest = folder / f"{gid}{suffix}"
        download(url, dest)
        thumb = dest.name
        if kind == "video":
            try:
                run(["ffmpeg", "-y", "-ss", "1", "-i", dest, "-frames:v", "1", "-vf", "scale=640:-2", folder / f"{gid}.jpg"])
                thumb = f"{gid}.jpg"
            except RuntimeError:
                thumb = None
        item = {"id": f"ai:{gid}", "kind": kind, "file": dest.name, "thumb": thumb, "prompt": prompt, "model": model,
                "created": datetime.now().isoformat(timespec="seconds"), "source": "ai", "provider": "AI (fal.ai)",
                "credit": f"AI-generated {kind} ({model} via fal.ai)", **({"duration": 5} if kind == "video" else {})}
        items.append(item)
        new.append(item)
    if not new:
        raise RuntimeError("fal.ai returned no file")
    save_json(folder / "index.json", items)
    log(f"Generated {len(new)} AI {kind}(s); they are kept in the project's ai folder")
    return new


def uses_ai(assets: list) -> bool:
    """Whether any scene shows an AI-generated visual (YouTube's synthetic-content box must be ticked then)."""
    return any(a and (a.get("source") == "ai" or any(s.get("source") == "ai" for s in a.get("shots") or []))
               for a in assets)


def set_custom_visual(project: Path, i: int, path: Path, credit: str = "") -> dict:
    """Use your own image or video for a scene."""
    kind = "video" if path.suffix.lower() in (".mp4", ".mov", ".webm", ".mkv") else "image"
    if kind == "image":
        from PIL import Image
        with Image.open(path) as img:   # fail early on a broken or unsupported file
            img.verify()
    else:
        media_duration(path)
    manifest = load_manifest(project)
    asset = {"kind": kind, "url": "", "id": f"custom:{path.name}", "credit": credit,
             "query": "", "source": "custom"}
    store_asset(project, manifest, i, asset, copy_from=path)
    return asset


# ----------------------------------------------------------------------------
# Step 4: render with FFmpeg
# ----------------------------------------------------------------------------
def render_jobs() -> int:
    """How many scenes render at once: RENDER_JOBS from .env, or a quarter of the CPU cores (1 to 4)."""
    return RENDER_JOBS if RENDER_JOBS > 0 else max(1, min(4, (os.cpu_count() or 2) // 4))


# Every scene is converted to and tagged as HD colour (BT.709, limited range), so scenes from different
# sources join without colour shifts or mid-stream format changes.
RENDER_VERSION = 2   # bump when render_scene's output changes, so cached scenes are rendered again
TAG_BT709 = "setparams=colorspace=bt709:color_primaries=bt709:color_trc=bt709:range=tv"
_SCALE_MATRIX = {"bt709": "bt709", "bt470bg": "bt601", "smpte170m": "smpte170m", "bt601": "bt601",
                 "smpte240m": "smpte240m", "fcc": "fcc", "bt2020nc": "bt2020", "bt2020c": "bt2020"}


def source_matrix(path) -> str:
    """The colour matrix of a video file, for converting it correctly. Untagged files are assumed to be
    BT.709 when they are HD and BT.601 when smaller, which is what players assume too."""
    try:
        out = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=color_space,height",
                   "-of", "default=nw=1", path])
    except RuntimeError:
        return "bt709"
    info = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    if info.get("color_space") in _SCALE_MATRIX:
        return _SCALE_MATRIX[info["color_space"]]
    return "bt709" if int(info.get("height") or 1080) >= 720 else "bt601"


def _shot_chain(asset: dict, frames: int, w: int, h: int, index: int) -> str:
    """Filters that turn one visual into exactly `frames` frames of w x h video (an input's chain in a graph)."""
    src = asset["path"]
    if asset["kind"] == "image":
        step = 0.20 / max(frames, 1)
        z = f"1+{step:.7f}*on" if index % 2 == 0 else f"1.20-{step:.7f}*on"
        return (f"scale={w * 2}:{h * 2}:force_original_aspect_ratio=increase,crop={w * 2}:{h * 2},"
                f"zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
                f":d={frames}:s={w}x{h}:fps={FPS},setsar=1,"
                f"scale=out_color_matrix=bt709:out_range=tv,format=yuv420p,{TAG_BT709}")
    return (f"scale={w}:{h}:force_original_aspect_ratio=increase:in_color_matrix={source_matrix(src)}"
            f":out_color_matrix=bt709:out_range=tv,crop={w}:{h},fps={FPS},setsar=1,format=yuv420p,"
            f"trim=end_frame={frames},setpts=PTS-STARTPTS,{TAG_BT709}")


def render_scene(asset: dict, frames: int, out: Path, index: int, size: tuple[int, int] | None = None,
                 keyframes: tuple[int, ...] | list[int] = (), shots: list[dict] | None = None) -> None:
    """Render one scene's visual to exactly `frames` frames. `keyframes` are frame numbers where the
    segment will later be cut without re-encoding (where its cross-fades start and end). With `shots`
    (several visuals), the scene shows each in turn, split evenly, as one segment."""
    w, h = size or (W, H)
    if shots and len(shots) > 1:
        n = len(shots)
        counts = [frames // n] * (n - 1) + [frames - frames // n * (n - 1)]
        inputs, chains = [], []
        for k, (shot, count) in enumerate(zip(shots, counts)):
            inputs += (["-i", shot["path"]] if shot["kind"] == "image" else ["-stream_loop", "-1", "-i", shot["path"]])
            chains.append(f"[{k}:v]{_shot_chain(shot, count, w, h, index + k)}[s{k}]")
        graph = ";".join(chains) + ";" + "".join(f"[s{k}]" for k in range(n)) + f"concat=n={n}:v=1:a=0[v]"
        cmd = ["ffmpeg", "-y", *inputs, "-filter_complex", graph, "-map", "[v]", "-an"]
        if keyframes:
            cmd += ["-force_key_frames", "expr:" + "+".join(f"eq(n,{k})" for k in keyframes), "-forced-idr", "1"]
        tmp = out.with_name(out.stem + ".part.mp4")
        run([*cmd, "-frames:v", frames, *ENCODE, tmp])
        tmp.replace(out)
        return
    src = asset["path"]
    if asset["kind"] == "image":
        # Slow "Ken Burns" zoom; alternate zoom-in and zoom-out for variety.
        step = 0.20 / max(frames, 1)
        z = f"1+{step:.7f}*on" if index % 2 == 0 else f"1.20-{step:.7f}*on"
        vf = (f"scale={w * 2}:{h * 2}:force_original_aspect_ratio=increase,crop={w * 2}:{h * 2},"
              f"zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
              f":d={frames}:s={w}x{h}:fps={FPS},setsar=1,"
              f"scale=out_color_matrix=bt709:out_range=tv,format=yuv420p,{TAG_BT709}")
        cmd = ["ffmpeg", "-y", "-i", src, "-vf", vf]
    else:
        vf = (f"scale={w}:{h}:force_original_aspect_ratio=increase:in_color_matrix={source_matrix(src)}"
              f":out_color_matrix=bt709:out_range=tv,crop={w}:{h},fps={FPS},setsar=1,format=yuv420p,{TAG_BT709}")
        cmd = ["ffmpeg", "-y", "-stream_loop", "-1", "-i", src, "-vf", vf, "-an"]
    if keyframes:
        cmd += ["-force_key_frames", "expr:" + "+".join(f"eq(n,{k})" for k in keyframes), "-forced-idr", "1"]
    tmp = out.with_name(out.stem + ".part.mp4")   # a half-written file must never look like a finished scene
    run([*cmd, "-frames:v", frames, *ENCODE, tmp])
    tmp.replace(out)


def crossfade_frames() -> int:
    if CROSSFADE <= 0:
        return 0
    if not ffmpeg_has_filter("xfade"):
        log("Note: this FFmpeg has no xfade filter (needs 4.3 or newer), so scenes use hard cuts")
        return 0
    return round(CROSSFADE * FPS)


def packet_count(path: Path) -> int | None:
    out = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets",
               "-show_entries", "stream=nb_read_packets", "-of", "default=nw=1:nk=1", path])
    # MPEG-TS files list the stream twice (once inside its program), so take the first number
    return next((int(line) for line in out.split() if line.isdigit()), None)


def _file_sig(path: Path) -> list[int]:
    st = path.stat()
    return [st.st_mtime_ns, st.st_size]


def fade_cuts(i: int, n: int, frames: int, xf: int) -> list[int]:
    """Frame numbers where scene i's segment is cut: after the fade-in it shares with the scene
    before it (the first xf frames) and where its fade-out into the next scene starts."""
    return ([xf] if i > 0 else []) + ([frames] if i < n - 1 else [])


def _join_chain(seg_dir: Path, timeline: list[dict], xf: int, out: Path) -> None:
    """Cross-fade every segment in one filter graph. Re-encodes the whole video; only used as a fallback."""
    n = len(timeline)
    inputs, chain = [], []
    for i in range(n):
        inputs += ["-i", seg_dir / f"seg_{i:02d}.mp4"]
        chain.append(f"[{i}:v]settb=AVTB,fps={FPS}[s{i}]")
    prev, offset = "[s0]", 0
    for i in range(1, n):
        offset += timeline[i - 1]["frames"]
        label = "[v]" if i == n - 1 else f"[x{i}]"
        chain.append(f"{prev}[s{i}]xfade=transition=fade:duration={xf / FPS:.6f}:offset={offset / FPS:.6f}{label}")
        prev = label
    run(["ffmpeg", "-y", *inputs, "-filter_complex", ";".join(chain), "-map", "[v]", *ENCODE, out])


def _split_segment(seg: Path, cuts: list[int], dests: list[Path], expect: list[int]) -> None:
    """Cut a segment at its keyframes without re-encoding."""
    parts = dests[0].parent
    pattern = parts / f"split_{seg.stem}_%d.ts"
    run(["ffmpeg", "-y", "-i", seg, "-map", "0:v", "-c", "copy", "-f", "segment",
         "-segment_frames", ",".join(map(str, cuts)), "-reset_timestamps", "1", "-segment_format", "mpegts", pattern])
    pieces = [parts / f"split_{seg.stem}_{k}.ts" for k in range(len(dests))]
    try:
        counts = [packet_count(p) if p.exists() else None for p in pieces]
        if counts != expect or (parts / f"split_{seg.stem}_{len(dests)}.ts").exists():
            raise RuntimeError(f"{seg.name} did not split into {expect} frames (got {counts})")
        for piece, dest in zip(pieces, dests):
            piece.replace(dest)
    finally:
        for leftover in parts.glob(f"split_{seg.stem}_*.ts"):
            leftover.unlink()


def _make_fade(tail: Path, head: Path, xf: int, out: Path) -> None:
    tmp = out.with_name(out.stem + ".part.ts")
    run(["ffmpeg", "-y", "-i", tail, "-i", head, "-filter_complex",
         f"[0:v]settb=AVTB,fps={FPS}[a];[1:v]settb=AVTB,fps={FPS}[b];"
         f"[a][b]xfade=transition=fade:duration={xf / FPS:.6f}:offset=0,{TAG_BT709}[v]",
         "-map", "[v]", "-frames:v", xf, *ENCODE, "-f", "mpegts", tmp])
    tmp.replace(out)


def _join_pieces(seg_dir: Path, timeline: list[dict], xf: int, out: Path) -> None:
    """Cross-fades without re-encoding the whole video: each segment is cut at its keyframes into
    head / body / tail, only the short fades (tail of one scene + head of the next) are encoded, and
    everything is concatenated as is. Pieces are cached, so after changing one scene only its own
    pieces and its two fades are redone."""
    n = len(timeline)
    parts = seg_dir / "parts"
    parts.mkdir(exist_ok=True)
    index_path = parts / "index.json"
    index = load_json(index_path) if index_path.exists() else {}
    keep = {index_path.name}
    body_frames = []
    for i, tl in enumerate(timeline):
        seg = seg_dir / f"seg_{i:02d}.mp4"
        cuts = fade_cuts(i, n, tl["frames"], xf)
        names = (["head"] if i > 0 else []) + ["body"] + (["tail"] if i < n - 1 else [])
        dests = [parts / f"seg_{i:02d}_{name}.ts" for name in names]
        start = xf if i > 0 else 0
        expect = ([xf] if i > 0 else []) + [tl["frames"] - start] + ([xf] if i < n - 1 else [])
        sig = {"seg": _file_sig(seg), "cuts": cuts}
        keep.update(d.name for d in dests)
        body_frames.append(tl["frames"] - start)
        if index.get(seg.stem) != sig or not all(d.exists() for d in dests):
            _split_segment(seg, cuts, dests, expect)
            index[seg.stem] = sig
            save_json(index_path, index)
    fades = 0
    for i in range(n - 1):
        tail, head = parts / f"seg_{i:02d}_tail.ts", parts / f"seg_{i + 1:02d}_head.ts"
        fade = parts / f"fade_{i:02d}.ts"
        sig = {"tail": _file_sig(tail), "head": _file_sig(head), "xf": xf}
        keep.add(fade.name)
        if index.get(fade.stem) != sig or not fade.exists():
            _make_fade(tail, head, xf, fade)
            index[fade.stem] = sig
            save_json(index_path, index)
            fades += 1
    lines = ["ffconcat version 1.0"]
    for i in range(n):
        lines += [f"file 'seg_{i:02d}_body.ts'", f"duration {body_frames[i] / FPS:.6f}"]
        if i < n - 1:
            lines += [f"file 'fade_{i:02d}.ts'", f"duration {xf / FPS:.6f}"]
    (parts / "join.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    keep.add("join.txt")
    log(f"Joining {n} scenes with {xf / FPS:.2f}s cross-fades ({fades} of {n - 1} fades re-made)")
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", parts / "join.txt", "-map", "0:v", "-c", "copy", out])
    for f in parts.iterdir():   # pieces of scenes that no longer exist
        if f.name not in keep:
            f.unlink()


def join_segments(seg_dir: Path, timeline: list[dict], xf: int, out: Path) -> None:
    n = len(timeline)
    if xf == 0:
        # Hard cuts: concatenate without re-encoding (relative paths, resolved from the list file's folder)
        (seg_dir / "list.txt").write_text(
            "".join(f"file 'seg_{i:02d}.mp4'\n" for i in range(n)), encoding="utf-8")
        run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", seg_dir / "list.txt", "-c", "copy", out])
        return
    # Every segment except the last was rendered xf frames longer, and each cross-fade consumes
    # exactly those frames, so the result is as long as the narration.
    if all(t["frames"] > xf for t in timeline[1:]):
        try:
            _join_pieces(seg_dir, timeline, xf, out)
            if video_frames(out) == sum(t["frames"] for t in timeline):
                return
            log("Note: the quick join came out the wrong length; joining again the slow way")
        except RuntimeError as e:
            log(f"Note: the quick join failed ({str(e).splitlines()[0][:200]}); joining the slow way")
    log(f"Joining {n} scenes with {xf / FPS:.2f}s cross-fades (one re-encode of the whole video)")
    _join_chain(seg_dir, timeline, xf, out)


def render_segments(project: Path, timeline: list[dict], assets: list[dict], seg_dir: Path | None = None,
                    size: tuple[int, int] | None = None, zoom: list[int] | None = None) -> int:
    """Render each scene that is missing, has the wrong length, or whose visual was swapped, several at
    a time. Returns the cross-fade length in frames. `zoom` sets each scene's zoom direction (by default
    its position)."""
    seg_dir = seg_dir or project / "segments"
    seg_dir.mkdir(parents=True, exist_ok=True)
    size = tuple(size or (W, H))
    n = len(assets)
    zoom = zoom or list(range(n))
    xf = crossfade_frames() if n > 1 else 0
    index_path = seg_dir / "render.json"
    index = load_json(index_path) if index_path.exists() else {}
    todo = []
    for i, (tl, asset) in enumerate(zip(timeline, assets)):
        out = seg_dir / f"seg_{i:02d}.mp4"
        want = tl["frames"] + (xf if i < n - 1 else 0)
        shots = scene_shots(asset, tl["duration"])
        sigs = [[Path(s["path"]).name, *_file_sig(Path(s["path"]))] for s in shots]
        key = {"frames": want, "keyframes": fade_cuts(i, n, tl["frames"], xf) if xf else [],
               "asset": sigs[0] if len(sigs) == 1 else sigs, "zoom": zoom[i] % 2, "size": list(size),
               "v": RENDER_VERSION}
        have = index.get(out.stem) or {}
        if out.exists() and have.get("segment") == _file_sig(out) and all(have.get(k) == v for k, v in key.items()):
            continue
        if out.exists() and have.get("asset") and have["asset"] != key["asset"]:
            log(f"Scene {i + 1} has a new visual, re-rendering")
        elif out.exists() and have.get("frames") and have["frames"] != want:
            log(f"Scene {i + 1} changed length ({have['frames']} -> {want} frames), re-rendering")
        else:
            log(f"Rendering scene {i + 1}/{n} ({asset['kind']}, {tl['duration']:.1f}s)")
        todo.append((i, asset, want, out, key))
    if todo:
        workers = min(render_jobs(), len(todo))
        if workers > 1:
            log(f"Rendering {len(todo)} scene(s), {workers} at a time")
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(render_scene, asset, want, out, zoom[i], size, key["keyframes"],
                                   scene_shots(asset, timeline[i]["duration"])): (i, out, key)
                       for i, asset, want, out, key in todo}
            try:
                for done, future in enumerate(as_completed(futures), start=1):
                    future.result()
                    i, out, key = futures[future]
                    index[out.stem] = dict(key, segment=_file_sig(out))
                    save_json(index_path, index)   # saved after each scene so a crash loses nothing
                    if len(todo) > 1:
                        log(f"  {done}/{len(todo)} scenes rendered")
            except BaseException:
                for future in futures:
                    future.cancel()
                raise
    for extra in seg_dir.glob("seg_*.mp4"):   # left over from scenes that were deleted
        m = re.fullmatch(r"seg_(\d+)\.mp4", extra.name)
        if m and int(m.group(1)) >= n:
            extra.unlink()
            index.pop(extra.stem, None)
            save_json(index_path, index)
    return xf


def join_needed(seg_dir: Path, timeline: list[dict], video_only: Path) -> bool:
    """Skip the join when no segment changed since the last one."""
    if not video_only.exists() or video_frames(video_only) != sum(t["frames"] for t in timeline):
        return True
    joined = video_only.stat().st_mtime
    return any((seg_dir / f"seg_{i:02d}.mp4").stat().st_mtime > joined for i in range(len(timeline)))


def concat_narration(project: Path, n: int, out: Path, name: str = "list.txt") -> Path:
    audio_dir = project / "audio"
    (audio_dir / name).write_text("".join(f"file 'scene_{i:02d}.wav'\n" for i in range(n)), encoding="utf-8")
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", audio_dir / name, "-c", "copy", out])
    return out


_LOUDNESS: dict = {}


def integrated_loudness(path: Path) -> float | None:
    """Integrated loudness of an audio file in LUFS (None if silent or unreadable), measured once per file version."""
    st = path.stat()
    key = (str(path), st.st_size, st.st_mtime)
    if key not in _LOUDNESS:
        result = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-vn",
                                 "-af", "loudnorm=print_format=json", "-f", "null", "-"],
                                capture_output=True, text=True)
        err = result.stderr
        try:
            value = float(json.loads(err[err.rindex("{"):err.rindex("}") + 1])["input_i"])
        except (ValueError, KeyError):
            value = None
        _LOUDNESS[key] = value if value is not None and value > -70 else None
    return _LOUDNESS[key]


def voice_filters(tone: str | None = None) -> list[str]:
    chain = VOICE_TONES.get(tone or VOICE_TONE, VOICE_TONES["soft"])
    return [f for f in chain if ffmpeg_has_filter(f.split("=", 1)[0])]


# ----------------------------------------------------------------------------
# Music: MUSIC_PATH is one track for every video, or a folder of tracks (each video then gets the
# track used least so far, and keeps it). A video can also choose its own track in the web UI.
# ----------------------------------------------------------------------------
MUSIC_EXTS = (".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac")


def load_meta(project: Path) -> dict:
    """project.json: the topic plus per-video choices made in the web UI (music, Short)."""
    path = Path(project) / "project.json"
    try:
        return load_json(path) if path.exists() else {}
    except ValueError:
        return {}


def save_meta(project: Path, meta: dict) -> None:
    Path(project).mkdir(parents=True, exist_ok=True)
    save_json(Path(project) / "project.json", meta)


def music_library() -> Path:
    """The folder of tracks: MUSIC_PATH if it is a folder, the folder of the MUSIC_PATH track, or music/."""
    if MUSIC_PATH and MUSIC_PATH.is_dir():
        return MUSIC_PATH
    if MUSIC_PATH and MUSIC_PATH.is_file():
        return MUSIC_PATH.parent
    return SCRIPT_DIR / "music"


def music_tracks() -> list[Path]:
    lib = music_library()
    if not lib.is_dir():
        return []
    return sorted((f for f in lib.iterdir() if f.is_file() and f.suffix.lower() in MUSIC_EXTS
                   and not f.name.startswith(".")), key=lambda f: f.name.lower())


def describe_default_music() -> str:
    if MUSIC_PATH and MUSIC_PATH.is_dir():
        return f"a different track for each video from {MUSIC_PATH.name}/"
    if MUSIC_PATH and MUSIC_PATH.is_file():
        return MUSIC_PATH.name
    return "no music"


def project_music(project: Path, save: bool = True) -> Path | None:
    """The track for this video. save=False only predicts an automatic pick without recording it."""
    project = Path(project)
    meta = load_meta(project)
    choice = meta.get("music")
    rotating = bool(MUSIC_PATH and MUSIC_PATH.is_dir())
    if choice == "none":
        return None
    if choice and (rotating or not meta.get("music_auto")):   # automatic picks only count while rotating
        track = music_library() / choice
        if track.is_file():
            return track
        if save:
            log(f"Note: this video's music track {choice} is no longer in {music_library()}; using the default")
    channel_music = get_channel(meta.get("channel")).get("music")   # a channel can set its own track (or none)
    if channel_music == "none":
        return None
    if channel_music and (music_library() / channel_music).is_file():
        return music_library() / channel_music
    if MUSIC_PATH and MUSIC_PATH.is_file():
        return MUSIC_PATH
    if not rotating:
        return None
    tracks = music_tracks()
    if not tracks:
        return None
    used: dict[str, int] = {}
    if PROJECTS_DIR.is_dir():
        for d in PROJECTS_DIR.iterdir():
            other = load_meta(d).get("music") if d.is_dir() and d.resolve() != project.resolve() else None
            if other:
                used[other] = used.get(other, 0) + 1
    # The least-used track; ties are broken by a hash of the video's name so the pick is stable
    track = min(tracks, key=lambda t: (used.get(t.name, 0),
                                       hashlib.sha1(f"{project.name}/{t.name}".encode()).hexdigest()))
    if save:
        meta.update(music=track.name, music_auto=True)
        save_meta(project, meta)
        log(f"Music for this video: {track.name} (picked automatically; change it on the Video tab)")
    return track


def music_gain_db(music: Path | None) -> float | None:
    """Gain that puts the music MUSIC_VOLUME below the narration, whatever loudness the track was mastered at."""
    if not (music and Path(music).is_file()):
        return None
    lufs = integrated_loudness(Path(music))
    if lufs is None:
        log(f"Note: {Path(music).name} seems silent or unreadable, so the music is left out")
        return None
    return VOICE_LUFS + 20 * math.log10(MUSIC_VOLUME) - lufs


def audio_filter(voice_in: str, music_in: str | None, gain_db: float | None, tone: str | None = None) -> str:
    """Filter graph: narration (tone-shaped, normalised to VOICE_LUFS) + optional music bed, output label [a]."""
    fmt = "aresample=48000,aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo"
    voice = ",".join([*voice_filters(tone), f"loudnorm=I={VOICE_LUFS:g}:TP=-1.5:LRA=11", fmt])
    if music_in is None or gain_db is None:
        return f"[{voice_in}]{voice}[a]"
    music = f"[{music_in}]{fmt},volume={gain_db:.2f}dB"
    duck = MUSIC_DUCKING and ffmpeg_has_filter("sidechaincompress")
    if duck:   # the music dips by about 6 dB while the narrator speaks and comes back up in pauses
        graph = [f"[{voice_in}]{voice},asplit=2[v][sc]", f"{music}[m0]",
                 "[m0][sc]sidechaincompress=threshold=0.05:ratio=3:attack=50:release=600[m]"]
    else:
        graph = [f"[{voice_in}]{voice}[v]", f"{music}[m]"]
    limiter = "alimiter=limit=0.89" + (":level=false" if ffmpeg_filter_has_option("alimiter", "level") else "")
    graph.append(f"[v][m]amix=inputs=2:duration=first:dropout_transition=0,volume=2,{limiter}[a]")
    return ";".join(graph)


def describe_music(music: Path | None) -> str:
    if not (music and Path(music).is_file()):
        return "no music"
    duck = ", ducked under speech" if MUSIC_DUCKING else ""
    return f"music {Path(music).name} {-20 * math.log10(MUSIC_VOLUME):.0f} dB below the voice{duck}"


def mix_final(video_only: Path, narration: Path, final: Path, music: Path | None = None,
              subtitles: str | None = None) -> None:
    """Final mix: narration (+ optional music bed), loudness-normalised for YouTube. The video is copied as is,
    unless `subtitles` names an .ass file (relative to the output's folder) to burn in, which re-encodes it."""
    # Absolute paths: FFmpeg runs in the output's folder so the subtitles filter needs no path escaping
    video_only, narration, final = Path(video_only).resolve(), Path(narration).resolve(), Path(final).resolve()
    music = Path(music).resolve() if music else None
    gain = music_gain_db(music)
    inputs = ["-i", video_only, "-i", narration]
    if gain is not None:
        inputs += ["-stream_loop", "-1", "-i", music]
    fc = audio_filter("1:a", "2:a" if gain is not None else None, gain)
    log(f"Mixing {'the Short' if subtitles else 'final video'} (voice tone: {VOICE_TONE}, "
        f"{describe_music(music if gain is not None else None)})")
    if subtitles:   # (the audio graph uses the labels [v] and [m] itself)
        video = ["-filter_complex", f"[0:v]subtitles={subtitles}[burned];{fc}", "-map", "[burned]", *ENCODE]
    else:
        video = ["-filter_complex", fc, "-map", "0:v", "-c:v", "copy"]
    run(["ffmpeg", "-y", *inputs, *video, "-map", "[a]", "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
         "-movflags", "+faststart", "-shortest", final], cwd=Path(final).parent)


def preview_mix(project: Path, out: Path, seconds: float = 45, tone: str | None = None,
                music: bool = True) -> Path:
    """A short MP3 of the narration with the current tone and music settings, for tuning the sound quickly."""
    wavs = []
    for i in range(10_000):
        if not audio_files(project, i)["wav"].exists():
            break
        wavs.append(i)
    if not wavs:
        raise RuntimeError("This project has no voiceover yet, so there is nothing to preview")
    narration = concat_narration(project, len(wavs), project / "audio" / "preview_narration.wav", "preview_list.txt")
    track = project_music(project) if music else None
    gain = music_gain_db(track)
    inputs = ["-i", narration]
    if gain is not None:
        inputs += ["-stream_loop", "-1", "-i", track]
    fc = audio_filter("0:a", "1:a" if gain is not None else None, gain, tone)
    run(["ffmpeg", "-y", *inputs, "-filter_complex", fc, "-map", "[a]", "-t", f"{seconds:g}",
         "-c:a", "libmp3lame", "-b:a", "192k", out])
    narration.unlink(missing_ok=True)
    return out


def assemble(project: Path, timeline: list[dict], assets: list[dict]) -> Path:
    n = len(assets)
    xf = render_segments(project, timeline, assets)
    video_only = project / "video_only.mp4"
    if join_needed(project / "segments", timeline, video_only):
        join_segments(project / "segments", timeline, xf, video_only)
    narration = concat_narration(project, n, project / "narration.wav")
    final = project / "final.mp4"
    mix_final(video_only, narration, final, project_music(project))
    log(f"Video ready: {final} ({media_duration(final) / 60:.1f} min)")
    return final


# ----------------------------------------------------------------------------
# Step 5: captions + thumbnail
# ----------------------------------------------------------------------------
def srt_time(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def word_timings(align: dict) -> list[tuple[str, float, float]]:
    """(word, start, end) built from ElevenLabs per-character timing."""
    words, current, start, end = [], [], None, 0.0
    for ch, s, e in zip(align["characters"], align["start"], align["end"]):
        if ch.isspace():
            if current:
                words.append(("".join(current), start, end))
                current, start = [], None
            continue
        if start is None:
            start = s
        current.append(ch)
        end = e
    if current:
        words.append(("".join(current), start, end))
    return words


def estimated_timings(text: str, speech: float) -> list[tuple[str, float, float]]:
    """Fallback when no timing was saved: spread the words over the speech by character count."""
    words = text.split()
    total = sum(len(w) for w in words) or 1
    out, t = [], 0.0
    for w in words:
        d = speech * len(w) / total
        out.append((w, t, t + d))
        t += d
    return out


def scene_words(project: Path, i: int, scene: dict, tl: dict, estimate: bool = False) -> list[tuple[str, float, float]]:
    """(word, start, end) for one scene, from the saved ElevenLabs timing or estimated (always for translations)."""
    align_path = project / "audio" / f"scene_{i:02d}.json"
    words = None
    if align_path.exists() and not estimate:
        try:
            words = word_timings(load_json(align_path))
        except (ValueError, KeyError, TypeError):
            words = None
    return words or estimated_timings(scene["narration"].strip(), tl["speech"])


def build_cues(project: Path, script: dict, timeline: list[dict], scenes: list[int] | None = None,
               max_words: int = MAX_CUE_WORDS, max_chars: int = MAX_CUE_CHARS,
               estimate: bool = False) -> list[tuple[float, float, str]]:
    """Caption cues for the given scenes (all by default), timed from the start of the first one."""
    cues, offset = [], 0.0
    for i in scenes if scenes is not None else range(len(script["scenes"])):
        tl = timeline[i]
        group: list[tuple[str, float, float]] = []
        for w, s, e in scene_words(project, i, script["scenes"][i], tl, estimate):
            if group and (len(group) >= max_words or sum(len(g[0]) for g in group) + len(group) + len(w) > max_chars):
                cues.append((offset + group[0][1], offset + group[-1][2] + 0.15, " ".join(g[0] for g in group)))
                group = []
            group.append((w, s, e))
        if group:
            cues.append((offset + group[0][1], offset + group[-1][2] + 0.15, " ".join(g[0] for g in group)))
        offset += tl["duration"]
    # A cue must not run into the next one
    for k in range(len(cues) - 1):
        start, end, text = cues[k]
        cues[k] = (start, max(min(end, cues[k + 1][0]), start + 0.3), text)
    return cues


def write_srt(project: Path, script: dict, timeline: list[dict], path: Path | None = None,
              estimate: bool = False) -> Path:
    entries = [f"{n}\n{srt_time(s)} --> {srt_time(e)}\n{text}\n"
               for n, (s, e, text) in enumerate(build_cues(project, script, timeline, estimate=estimate), start=1)]
    path = path or project / "captions.srt"
    path.write_text("\n".join(entries), encoding="utf-8")
    return path


# ----------------------------------------------------------------------------
# Other languages: translated captions, title and description for an uploaded video, and dubbed versions
# ----------------------------------------------------------------------------
class Translation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(description="The translated YouTube title, at most 90 characters")
    description: str = Field(description="The translated description")
    tags: List[str] = Field(description="The tags, translated")
    thumbnail_text: str = Field(description="The thumbnail text, translated, 2-5 punchy words")
    scenes: List[str] = Field(description="Every scene's narration translated, in the same order")


def script_hash(script: dict) -> str:
    parts = [script.get("title", ""), script.get("description", ""), script.get("thumbnail_text", ""),
             *script.get("tags", []), *(s["narration"] for s in script["scenes"])]
    return hashlib.sha1("\n".join(parts).encode()).hexdigest()


def translate_script(project: Path, script: dict, language: str) -> dict:
    """Claude translates the narration (scene by scene), title, description, tags and thumbnail text.
    Kept in translations/<language>.json and reused until the script changes."""
    if language not in LANGUAGES:
        raise ValueError(f"Unknown language: {language}")
    cache = project / "translations" / f"{language}.json"
    digest = script_hash(script)
    if cache.exists():
        saved = load_json(cache)
        if saved.get("hash") == digest:
            return saved["translation"]
    name = LANGUAGES[language]
    n = len(script["scenes"])
    numbered = "\n".join(f"{i}. {s['narration']}" for i, s in enumerate(script["scenes"]))
    prompt = f"""Translate this YouTube video into {name}.

Title: {script.get('title', '')}
Thumbnail text: {script.get('thumbnail_text', '')}
Tags: {', '.join(script.get('tags', []))}
Description:
{script.get('description', '')}

Narration, {n} scenes numbered from 0:
{numbered}

The narration will be read aloud by a narrator, so write natural spoken {name}, not a word-for-word rendering. Keep every fact, number and unit exactly, keep proper names, and keep each scene about the same length. Return exactly {n} scene translations, in order."""
    log(f"Translating into {name}")
    result = claude_json(ANTHROPIC_MODEL, f"You are a professional translator of documentaries into {name}.",
                         prompt, Translation).model_dump()
    if len(result["scenes"]) != n:
        raise RuntimeError(f"The {name} translation has {len(result['scenes'])} scenes instead of {n}; try again")
    cache.parent.mkdir(exist_ok=True)
    save_json(cache, {"hash": digest, "translation": result})
    return result


def translated_script(script: dict, tr: dict) -> dict:
    return dict(script, title=tr["title"][:100], description=tr["description"], tags=tr["tags"],
                thumbnail_text=tr["thumbnail_text"],
                scenes=[dict(s, narration=t.strip()) for s, t in zip(script["scenes"], tr["scenes"])])


def write_translated_captions(project: Path, script: dict, timeline: list[dict], language: str) -> Path:
    """captions_<language>.srt: each scene's translation spread over the time its narration is spoken."""
    tr = translate_script(project, script, language)
    return write_srt(project, translated_script(script, tr), timeline, project / f"captions_{language}.srt",
                     estimate=True)


def upload_translations(project: Path, script: dict, assets: list[dict], languages: list[str], interactive=False) -> dict:
    """Add translated captions and a translated title and description to the uploaded video."""
    from googleapiclient.http import MediaFileUpload
    video_id = load_json(project / "youtube.json")["video_id"]
    timeline = load_json(project / "timeline.json")
    youtube = get_youtube(interactive=interactive)
    own = video_language(project)
    localizations, done = {}, {}
    for lang in languages:
        if lang == own:
            continue
        srt = write_translated_captions(project, script, timeline, lang)
        tr = translate_script(project, script, lang)
        try:
            youtube.captions().insert(
                part="snippet", body={"snippet": {"videoId": video_id, "language": lang, "name": LANGUAGES[lang],
                                                  "isDraft": False}},
                media_body=MediaFileUpload(str(srt), mimetype="application/octet-stream")).execute()
            done[lang] = "captions"
            log(f"{LANGUAGES[lang]} captions uploaded")
        except Exception as e:
            log(f"{LANGUAGES[lang]} captions not uploaded ({str(e)[:200]})")
        localizations[lang] = {"title": clean_text(tr["title"])[:100],
                               "description": build_description(dict(script, description=tr["description"],
                                                                     chapters=None), assets)}
    if localizations:
        try:
            youtube.videos().update(part="localizations",
                                    body={"id": video_id, "localizations": localizations}).execute()
            log(f"Translated title and description added: {', '.join(LANGUAGES[l] for l in localizations)}")
            done.update({lang: done.get(lang, "") + "+title" for lang in localizations})
        except Exception as e:
            log(f"Translated titles not added ({str(e)[:200]})")
    record = load_json(project / "youtube.json")
    record["translations"] = dict(record.get("translations") or {}, **done)
    save_json(project / "youtube.json", record)
    return done


def make_dub(project: Path, language: str) -> Path:
    """A new video in another language: same visuals and music, translated script (to review before it is
    narrated). Returns the new project folder."""
    script = load_json(project / "script.json")
    if video_language(project) == language:
        raise ValueError(f"This video is already in {LANGUAGES.get(language, language)}")
    dest = project.parent / f"{project.name}-{language.lower()}"
    if (dest / "script.json").exists():
        raise RuntimeError(f"There is already a {LANGUAGES[language]} version: {dest.name}")
    tr = translate_script(project, script, language)
    dest.mkdir(parents=True, exist_ok=True)
    if (project / "assets").exists():
        shutil.copytree(project / "assets", dest / "assets", dirs_exist_ok=True)
    manifest = load_manifest(project)
    for asset in manifest:
        for a in ([asset] + (asset.get("shots") or [])) if asset else []:
            if a and a.get("path"):
                a["path"] = str(dest / "assets" / Path(a["path"]).name)
    save_json(dest / "assets.json", manifest)
    new = translated_script(script, tr)
    new.pop("chapters", None)   # chapter titles are suggested again in the new language
    save_json(dest / "script.json", new)
    meta = load_meta(project)
    save_meta(dest, {k: v for k, v in meta.items() if k in ("channel", "music", "music_auto")} |
              {"topic": tr["title"], "language": language, "translation_of": project.name, "source": "translation",
               "created": datetime.now().isoformat()})
    write_review(dest, new, {})
    log(f"{LANGUAGES[language]} version created: {dest.name}. Review it, then generate its voiceover.")
    return dest


# ----------------------------------------------------------------------------
# Chapters: timestamps in the description that split the video into titled parts
# ----------------------------------------------------------------------------
class Chapter(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scene: int = Field(description="Number of the scene the chapter starts at, counting from 0")
    title: str = Field(description="2-6 words, specific to what this part covers")


class ChapterPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chapters: List[Chapter]


def scene_starts(timeline: list[dict]) -> list[float]:
    """When each scene starts in the finished video, in seconds."""
    return list(itertools.accumulate([0.0] + [t["duration"] for t in timeline[:-1]]))


def clock(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def clean_chapters(chapters: list, n: int) -> list[dict]:
    """Chapters in order, one per starting scene, the first at scene 0."""
    out: dict[int, str] = {}
    for c in chapters or []:
        scene, title = (c.get("scene"), c.get("title")) if isinstance(c, dict) else (None, None)
        title = " ".join(str(title or "").split())[:60]
        if isinstance(scene, int) and 0 <= scene < n and title and scene not in out:
            out[scene] = title
    items = [{"scene": s, "title": t} for s, t in sorted(out.items())]
    if items:
        items[0]["scene"] = 0
    return items


def chapter_lines(script: dict, timeline: list[dict]) -> list[str]:
    """YouTube chapter lines ("1:23 Title"), or [] when they would break YouTube's rules
    (at least 3 chapters, the first at 0:00, each at least 10 seconds long)."""
    n = min(len(script["scenes"]), len(timeline))
    starts = scene_starts(timeline[:n])
    total = sum(t["duration"] for t in timeline[:n])
    kept: list[dict] = []
    for c in clean_chapters(script.get("chapters"), n):
        if kept and starts[c["scene"]] - starts[kept[-1]["scene"]] < 10:
            continue   # too soon after the previous chapter
        kept.append(c)
    if kept and total - starts[kept[-1]["scene"]] < 10:
        kept.pop()
    if len(kept) < 3:
        return []
    return [f"{clock(starts[c['scene']])} {c['title']}" for c in kept]


def suggest_chapters(script: dict, timeline: list[dict]) -> list[dict]:
    starts = scene_starts(timeline)
    numbered = "\n".join(f"{i}. [{clock(starts[i])}] {s['narration']}" for i, s in enumerate(script["scenes"]))
    total = sum(t["duration"] for t in timeline)
    prompt = f"""Video title: {script.get('title', '')}

Narration by scene, numbered from 0, with each scene's start time:
{numbered}

The video lasts {clock(total)}. Split it into YouTube chapters: {'3 to 5' if total < 300 else '4 to 8'} chapters, the first starting at scene 0, each at least 40 seconds long, each starting where the narration moves to a new part of the story. Titles: 2-6 words, specific to that part, no clickbait, no numbering, no emoji."""
    plan = claude_json(ANTHROPIC_MODEL, "You write clear, accurate YouTube chapter titles.", prompt, ChapterPlan,
                       max_tokens=2000)
    return clean_chapters([c.model_dump() for c in plan.chapters], len(script["scenes"]))


def ensure_chapters(project: Path, script: dict, timeline: list[dict]) -> None:
    """With AUTO_CHAPTERS on, suggest chapters for a rendered video that has none (failures only log a note)."""
    if not AUTO_CHAPTERS or script.get("chapters") or not os.getenv("ANTHROPIC_API_KEY"):
        return
    try:
        script["chapters"] = suggest_chapters(script, timeline)
        save_json(project / "script.json", script)
        log(f"Chapters suggested: {', '.join(c['title'] for c in script['chapters'])}")
    except Exception as e:
        log(f"Note: chapters could not be suggested ({type(e).__name__}: {str(e)[:150]})")


def load_font(size: int):
    from PIL import ImageFont
    candidates = [THUMB_FONT,
                  "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                  "C:/Windows/Fonts/arialbd.ttf",
                  "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
                  "/Library/Fonts/Arial Bold.ttf"]
    for path in candidates:
        if path and Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)


def make_thumbnail(project: Path, script: dict, timeline: list[dict], assets: list[dict]) -> Path:
    from PIL import Image, ImageDraw
    idx = script.get("thumbnail_scene")   # chosen in the web UI; otherwise the first still image
    if not isinstance(idx, int) or not 0 <= idx < len(assets):
        idx = next((i for i, a in enumerate(assets) if a["kind"] == "image"), 0)
    frame = project / "thumb_frame.jpg"
    run(["ffmpeg", "-y", "-ss", f"{timeline[idx]['duration'] / 2:.2f}",
         "-i", project / "segments" / f"seg_{idx:02d}.mp4", "-frames:v", "1", "-q:v", "2", frame])

    img = Image.open(frame).convert("RGB").resize((1280, 720))
    # Dark gradient on the left so the text stays readable on any image
    mask = Image.new("L", img.size, 0)
    md = ImageDraw.Draw(mask)
    for x in range(img.width):
        md.line([(x, 0), (x, img.height)], fill=int(210 * max(0.0, 1 - x / 850)))
    img = Image.composite(Image.new("RGB", img.size, "black"), img, mask)

    draw = ImageDraw.Draw(img)
    font = load_font(104)
    lines = textwrap.wrap((script.get("thumbnail_text") or script["title"]).upper(), width=12)[:3]
    y = (720 - len(lines) * 118) // 2
    for line in lines:
        draw.text((60, y), line, font=font, fill=(255, 214, 0), stroke_width=6, stroke_fill="black")
        y += 118
    path = project / "thumbnail.jpg"
    img.save(path, "JPEG", quality=90)
    return path


# ----------------------------------------------------------------------------
# Shorts: a vertical cut of a run of scenes with large burned-in captions. It reuses the narration
# and visuals, so it costs nothing extra.
# ----------------------------------------------------------------------------
SHORT_W, SHORT_H = 1080, 1920
SHORT_TARGET_SECONDS = 58    # the default Short: the opening scenes, up to about a minute
SHORT_MAX_SECONDS = 180      # YouTube's limit for a Short
SHORT_CUE_WORDS, SHORT_CUE_CHARS = 3, 18


def short_settings(project: Path, script: dict, timeline: list[dict]) -> dict:
    """The Short's scenes (a continuous range) and title: chosen in the web UI, or the opening scenes."""
    n = len(script["scenes"])
    saved = load_meta(project).get("short") or {}
    first = saved.get("first")
    first = first if isinstance(first, int) and 0 <= first < n else 0
    last = saved.get("last")
    if isinstance(last, int) and last >= first:
        last = min(last, n - 1)
    else:
        last, total = first, timeline[first]["duration"]
        for k in range(first + 1, n):
            if total + timeline[k]["duration"] > SHORT_TARGET_SECONDS:
                break
            total += timeline[k]["duration"]
            last = k
    title = " ".join(str(saved.get("title") or script.get("title") or "").split())
    seconds = round(sum(timeline[k]["duration"] for k in range(first, last + 1)), 2)
    return {"first": first, "last": last, "title": title, "seconds": seconds}


def ass_time(t: float) -> str:
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360_000)
    m, cs = divmod(cs, 6_000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def ass_text(s: str) -> str:
    return re.sub(r"[{}\\]", "", s).replace("\n", " ")


def write_short_captions(project: Path, script: dict, timeline: list[dict], scenes: list[int], title: str,
                         out: Path) -> Path:
    """Subtitles for the Short: the title near the top for the whole Short, and 1-3 word captions below the middle."""
    total = sum(timeline[i]["duration"] for i in scenes)
    font = "DejaVu Sans"   # libass falls back to another bold sans font if this one is missing
    style = ("Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
             "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
             "MarginL, MarginR, MarginV, Encoding")
    lines = ["[Script Info]", "ScriptType: v4.00+", f"PlayResX: {SHORT_W}", f"PlayResY: {SHORT_H}", "WrapStyle: 0",
             "ScaledBorderAndShadow: yes", "", "[V4+ Styles]", style,
             f"Style: Caption,{font},84,&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,-1,0,0,0,100,100,0,0,1,7,3,2,90,90,640,1",
             f"Style: Title,{font},66,&H0000D6FF,&H00FFFFFF,&H00000000,&H96000000,-1,0,0,0,100,100,0,0,1,6,3,8,80,80,260,1",
             "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]
    if title:
        lines.append(f"Dialogue: 0,{ass_time(0)},{ass_time(total)},Title,,0,0,0,,{ass_text(title)}")
    for start, end, text in build_cues(project, script, timeline, scenes, SHORT_CUE_WORDS, SHORT_CUE_CHARS):
        lines.append(f"Dialogue: 1,{ass_time(start)},{ass_time(end)},Caption,,0,0,0,,{ass_text(text)}")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def make_short(project: Path, script: dict, timeline: list[dict], assets: list[dict]) -> Path:
    s = short_settings(project, script, timeline)
    if s["seconds"] > SHORT_MAX_SECONDS:
        raise RuntimeError(f"The Short would be {s['seconds']:.0f} s long; YouTube allows at most "
                           f"{SHORT_MAX_SECONDS} s. Pick fewer scenes.")
    scenes = list(range(s["first"], s["last"] + 1))
    d = project / "short"
    d.mkdir(exist_ok=True)
    log(f"Making the Short: scenes {s['first'] + 1} to {s['last'] + 1} ({s['seconds']:.0f} s)")
    sub_tl = [timeline[i] for i in scenes]
    seg_dir = d / "segments"
    xf = render_segments(project, sub_tl, [assets[i] for i in scenes], seg_dir=seg_dir,
                         size=(SHORT_W, SHORT_H), zoom=scenes)
    video_only = d / "video_only.mp4"
    if join_needed(seg_dir, sub_tl, video_only):
        join_segments(seg_dir, sub_tl, xf, video_only)
    (d / "narration.txt").write_text("".join(f"file '../audio/scene_{i:02d}.wav'\n" for i in scenes), encoding="utf-8")
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", d / "narration.txt", "-c", "copy", d / "narration.wav"])
    subtitles = None
    if ffmpeg_has_filter("subtitles"):
        write_short_captions(project, script, timeline, scenes, s["title"], d / "captions.ass")
        subtitles = "captions.ass"
    else:
        log("Note: this FFmpeg cannot burn in captions (it was built without libass), so the Short has none")
    final = d / "short.mp4"
    mix_final(video_only, d / "narration.wav", final, project_music(project), subtitles=subtitles)
    log(f"Short ready: {final} ({media_duration(final):.0f} s)")
    return final


# ----------------------------------------------------------------------------
# Step 6: YouTube upload
# ----------------------------------------------------------------------------
ANALYTICS_SCOPE = "https://www.googleapis.com/auth/yt-analytics.readonly"
YT_SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
             "https://www.googleapis.com/auth/youtube.force-ssl",   # needed for captions and reading statistics
             ANALYTICS_SCOPE]                                       # watch time per video


def youtube_credentials(interactive: bool, force: bool = False):
    """Load the saved login, refresh it if needed, or sign in through the browser (interactive runs only).
    force=True always signs in again (python pipeline.py --login)."""
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    creds = None
    if YT_TOKEN.exists() and not force:
        try:
            # The permissions the login was granted (a login saved before statistics were added lacks one)
            creds = Credentials.from_authorized_user_file(str(YT_TOKEN))
        except ValueError as e:
            log(f"Ignoring unreadable {YT_TOKEN}: {e}")
    if creds and not creds.valid and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError as e:
            log(f"The saved YouTube login could not be refreshed: {e}")
            creds = None
    if not creds or not creds.valid:
        if not interactive:
            raise RuntimeError("YouTube login is missing or expired. Sign in again with: python pipeline.py --login "
                               "(see 'Login expiry' in the README).")
        if not YT_CLIENT_SECRETS.exists():
            raise RuntimeError(f"{YT_CLIENT_SECRETS} not found (needed to sign in to YouTube)")
        from google_auth_oauthlib.flow import InstalledAppFlow
        flow = InstalledAppFlow.from_client_secrets_file(str(YT_CLIENT_SECRETS), YT_SCOPES)
        creds = flow.run_local_server(port=0)  # opens a browser
    YT_TOKEN.write_text(creds.to_json(), encoding="utf-8")
    return creds


def get_youtube(interactive: bool):
    from googleapiclient.discovery import build
    return build("youtube", "v3", credentials=youtube_credentials(interactive))


def clean_text(s: str) -> str:
    return s.replace("<", "").replace(">", "")  # YouTube rejects angle brackets


def build_description(script: dict, assets: list[dict], timeline: list[dict] | None = None) -> str:
    credits = list(dict.fromkeys(v["credit"] for a in assets if a for v in [a, *(a.get("shots") or [])]
                                 if v and v.get("credit")))
    desc = clean_text(script.get("description", "")).strip()
    chapters = chapter_lines(script, timeline) if timeline else []
    if chapters:
        desc += "\n\nChapters:\n" + "\n".join(clean_text(line) for line in chapters)
    if credits:
        desc += "\n\nVisual credits:\n" + "\n".join(f"- {clean_text(c)}" for c in credits)
    desc += "\n\nNarration uses an AI-generated voice. Script researched and reviewed by the channel."
    return desc[:4900]


def trim_tags(tags: list[str], limit: int = 450) -> list[str]:
    out, total = [], 0
    for t in tags:
        t = clean_text(t).strip()
        if t and total + len(t) + 2 <= limit:
            out.append(t)
            total += len(t) + 2
    return out


def upload_status(args, assets: list | None = None) -> dict:
    synthetic = bool(args.synthetic) or uses_ai(assets or [])   # AI visuals are always disclosed
    status = {"privacyStatus": args.privacy, "selfDeclaredMadeForKids": False, "containsSyntheticMedia": synthetic}
    if args.publish_at:
        status["privacyStatus"] = "private"  # required by YouTube for scheduled videos
        status["publishAt"] = args.publish_at
    return status


def upload_file(youtube, path: Path, body: dict) -> str:
    from googleapiclient.http import MediaFileUpload
    request = youtube.videos().insert(part="snippet,status", body=body,
                                      media_body=MediaFileUpload(str(path), chunksize=-1, resumable=True))
    response = None
    while response is None:
        progress, response = request.next_chunk()
        if progress:
            log(f"  {int(progress.progress() * 100)}%")
    return response["id"]


def video_language(project: Path) -> str:
    """The language a video is narrated in: its own (a translated version) or its channel's."""
    return load_meta(project).get("language") or project_channel(project).get("language") or "en"


def upload(project: Path, script: dict, assets: list[dict], args) -> str:
    from googleapiclient.http import MediaFileUpload
    youtube = get_youtube(interactive=not args.yes)
    language = video_language(project)
    body = {
        "snippet": {
            "title": clean_text(script["title"])[:100],
            "description": build_description(script, assets, load_json(project / "timeline.json")
                                             if (project / "timeline.json").exists() else None),
            "tags": trim_tags(script.get("tags", [])),
            "categoryId": project_channel(project).get("category") or "28",
            "defaultLanguage": language,
            "defaultAudioLanguage": language,
        },
        "status": upload_status(args, assets),
    }
    log("Uploading to YouTube...")
    video_id = upload_file(youtube, project / "final.mp4", body)
    log(f"Uploaded: https://youtu.be/{video_id}")

    try:
        youtube.thumbnails().set(videoId=video_id, media_body=MediaFileUpload(
            str(project / "thumbnail.jpg"), mimetype="image/jpeg")).execute()
        log("Thumbnail set")
    except Exception as e:  # custom thumbnails need a verified channel
        log(f"Thumbnail not set ({e}). Verify your channel at youtube.com/verify")

    try:
        youtube.captions().insert(
            part="snippet",
            body={"snippet": {"videoId": video_id, "language": language, "name": LANGUAGES.get(language, language),
                              "isDraft": False}},
            media_body=MediaFileUpload(str(project / "captions.srt"), mimetype="application/octet-stream"),
        ).execute()
        log("Captions uploaded")
    except Exception as e:
        log(f"Captions not uploaded ({e})")
    return video_id


def upload_short(project: Path, script: dict, assets: list[dict], args) -> str:
    """Upload short/short.mp4. Its description links to the full video when that is uploaded already."""
    s = short_settings(project, script, load_json(project / "timeline.json"))
    youtube = get_youtube(interactive=not args.yes)
    tag = " #Shorts"
    title = clean_text(s["title"] or script["title"]).strip()[:100 - len(tag)] + tag
    desc = clean_text(script.get("description", "")).strip().split("\n\n")[0]
    if (project / "youtube.json").exists():
        desc += f"\n\nFull video: {load_json(project / 'youtube.json')['url']}"
    credits = list(dict.fromkeys(v["credit"] for a in assets[s["first"]:s["last"] + 1] if a
                                 for v in [a, *(a.get("shots") or [])] if v and v.get("credit")))
    if credits:
        desc += "\n\nVisual credits:\n" + "\n".join(f"- {clean_text(c)}" for c in credits)
    desc += "\n\nNarration uses an AI-generated voice."
    language = video_language(project)
    body = {"snippet": {"title": title, "description": desc[:4900],
                        "tags": trim_tags(list(script.get("tags", [])) + ["Shorts"]),
                        "categoryId": project_channel(project).get("category") or "28",
                        "defaultLanguage": language, "defaultAudioLanguage": language},
            "status": upload_status(args, assets[s["first"]:s["last"] + 1])}
    log("Uploading the Short to YouTube...")
    video_id = upload_file(youtube, project / "short" / "short.mp4", body)
    log(f"Uploaded: https://youtube.com/shorts/{video_id}")
    return video_id


def iso_seconds(value) -> int | None:
    """Seconds in an ISO 8601 duration such as PT6M5S."""
    m = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", value or "")
    if not m or not value:
        return None
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return ((d * 24 + h) * 60 + mi) * 60 + s


def youtube_stats(video_ids: list[str]) -> tuple[dict, list[str]]:
    """Views, likes and comments (YouTube Data API) plus watch time and average view duration (YouTube
    Analytics API, which lags a day or two) for your uploads. Returns ({video id: stats}, notes)."""
    from googleapiclient.discovery import build
    creds = youtube_credentials(interactive=False)
    youtube = build("youtube", "v3", credentials=creds)
    out: dict[str, dict] = {}
    notes: list[str] = []
    for k in range(0, len(video_ids), 50):
        resp = youtube.videos().list(part="statistics,contentDetails,snippet",
                                     id=",".join(video_ids[k:k + 50])).execute()
        for item in resp.get("items", []):
            st = item.get("statistics") or {}
            out[item["id"]] = {"views": int(st.get("viewCount", 0)), "likes": int(st.get("likeCount", 0)),
                               "comments": int(st.get("commentCount", 0)),
                               "seconds": iso_seconds((item.get("contentDetails") or {}).get("duration")),
                               "published": (item.get("snippet") or {}).get("publishedAt")}
    missing = [v for v in video_ids if v not in out]
    if missing:
        notes.append(f"{len(missing)} upload(s) were not found on YouTube (deleted?): {', '.join(missing)}")
    if ANALYTICS_SCOPE not in (getattr(creds, "scopes", None) or [ANALYTICS_SCOPE]):
        notes.append("Watch time needs one more permission than your saved login has. "
                     "Sign in again with: python pipeline.py --login")
        return out, notes
    try:
        analytics = build("youtubeAnalytics", "v2", credentials=creds)
        for k in range(0, len(video_ids), 200):
            resp = analytics.reports().query(
                ids="channel==MINE", startDate="2005-02-14", endDate=datetime.now().strftime("%Y-%m-%d"),
                metrics="views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage",
                dimensions="video", filters="video==" + ",".join(video_ids[k:k + 200]),
                sort="-views", maxResults=200).execute()
            cols = [c["name"] for c in resp.get("columnHeaders", [])]
            for row in resp.get("rows") or []:
                r = dict(zip(cols, row))
                out.setdefault(r["video"], {}).update(minutes_watched=r.get("estimatedMinutesWatched"),
                                                      avg_view_seconds=r.get("averageViewDuration"),
                                                      avg_view_percent=r.get("averageViewPercentage"))
    except Exception as e:   # usually: the YouTube Analytics API is not enabled in the Google Cloud project
        notes.append(f"Watch time is unavailable ({str(e)[:200]}). Enable the YouTube Analytics API in your "
                     "Google Cloud project (next to the YouTube Data API v3).")
    return out, notes


def elevenlabs_usage() -> dict:
    """Characters used and left on the ElevenLabs plan this billing period, or {"error": ...}."""
    if not ELEVENLABS_API_KEY:
        return {"error": "ELEVENLABS_API_KEY is not set"}
    try:
        r = HTTP.get("https://api.elevenlabs.io/v1/user/subscription",
                     headers={"xi-api-key": ELEVENLABS_API_KEY}, timeout=20)
    except requests.RequestException as e:
        return {"error": f"Could not reach ElevenLabs ({type(e).__name__})"}
    if r.status_code in (401, 403):
        return {"error": "Your API key cannot read the subscription. In ElevenLabs, give the key the "
                         "User: Read permission to see your balance here."}
    if r.status_code != 200:
        return {"error": f"ElevenLabs answered with error {r.status_code}"}
    d = r.json()
    used, limit = int(d.get("character_count") or 0), int(d.get("character_limit") or 0)
    return {"used": used, "limit": limit, "left": max(limit - used, 0),
            "resets": d.get("next_character_count_reset_unix"), "tier": d.get("tier")}


# ----------------------------------------------------------------------------
# Orchestration
# ----------------------------------------------------------------------------
def settings_problems() -> list[str]:
    """Sound and voice settings that are out of range (shared by the CLI preflight and the web UI)."""
    problems = []
    if MUSIC_PATH and not MUSIC_PATH.exists():
        problems.append(f"MUSIC_PATH points to {MUSIC_PATH}, which does not exist "
                        "(fix the path, or leave MUSIC_PATH empty for narration only)")
    elif MUSIC_PATH and MUSIC_PATH.is_dir() and not music_tracks():
        problems.append(f"MUSIC_PATH is the folder {MUSIC_PATH}, which has no music files "
                        f"({', '.join(e[1:] for e in MUSIC_EXTS)})")
    if not 0 < MUSIC_VOLUME <= 0.5:
        problems.append(f"MUSIC_VOLUME is {MUSIC_VOLUME:g}; use a value above 0 and up to 0.5, e.g. 0.10")
    if VOICE_TONE not in VOICE_TONES:
        problems.append(f"VOICE_TONE is '{VOICE_TONE}'; use one of: {', '.join(VOICE_TONES)}")
    for name, value in (("VOICE_STABILITY", VOICE_STABILITY), ("VOICE_SIMILARITY", VOICE_SIMILARITY),
                        ("VOICE_STYLE", VOICE_STYLE)):
        if not 0 <= value <= 1:
            problems.append(f"{name} is {value:g}; use a value from 0 to 1")
    if not 0.7 <= VOICE_SPEED <= 1.2:
        problems.append(f"VOICE_SPEED is {VOICE_SPEED:g}; ElevenLabs accepts 0.7 to 1.2")
    if CROSSFADE < 0 or CROSSFADE > 2:
        problems.append(f"CROSSFADE is {CROSSFADE:g}; use 0 (hard cuts) up to 2 seconds")
    if SHOT_SECONDS and not 4 <= SHOT_SECONDS <= 60:
        problems.append(f"SHOT_SECONDS is {SHOT_SECONDS:g}; use 0 (one shot per scene) or 4 to 60 seconds")
    if AUTO_PICK not in ("off", "rank", "auto"):
        problems.append(f"AUTO_PICK is '{AUTO_PICK}'; use off, rank or auto")
    if AI_VISUALS not in ("off", "images", "images_and_video"):
        problems.append(f"AI_VISUALS is '{AI_VISUALS}'; use off, images or images_and_video")
    if AI_IMAGE_COST < 0 or AI_VIDEO_COST < 0:
        problems.append("AI_IMAGE_COST and AI_VIDEO_COST cannot be negative")
    for name, model in (("AI_IMAGE_MODEL", AI_IMAGE_MODEL), ("AI_VIDEO_MODEL", AI_VIDEO_MODEL)):
        if not re.fullmatch(r"[a-z0-9][a-z0-9._/-]{2,120}", model):
            problems.append(f"{name} '{model}' does not look like a fal.ai model id (e.g. fal-ai/flux/dev)")
    bad_langs = [c for c in CAPTION_LANGUAGES if c not in LANGUAGES]
    if bad_langs:
        problems.append(f"CAPTION_LANGUAGES has unknown codes ({', '.join(bad_langs)}); use: {', '.join(LANGUAGES)}")
    unknown = [p for p in VISUAL_PROVIDERS if p not in PROVIDERS]
    if unknown:
        problems.append(f"VISUAL_PROVIDERS has unknown names ({', '.join(unknown)}); use: {', '.join(PROVIDERS)}")
    return problems


def preflight(upload_enabled: bool, unattended: bool) -> None:
    problems = []
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            problems.append(f"{tool} is not installed or not on PATH")
    for name in ("ANTHROPIC_API_KEY", "ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID"):
        if not os.getenv(name):
            problems.append(f"{name} is missing from .env")
    providers = stock_providers()
    if providers:
        log(f"Stock footage: {' and '.join(providers)}")
    else:
        log("Note: neither PEXELS_API_KEY nor PIXABAY_API_KEY is set, so only NASA images will be used")
    problems += settings_problems()
    if not problems:
        duck = ", ducked under speech" if MUSIC_DUCKING else ""
        level = f" {-20 * math.log10(MUSIC_VOLUME):.0f} dB below the voice{duck}" if MUSIC_PATH else ""
        log(f"Sound: voice tone {VOICE_TONE}, music: {describe_default_music()}{level}")
    if upload_enabled:
        if unattended:
            try:
                youtube_credentials(interactive=False)  # fail now, before any money is spent on narration
            except Exception as e:
                problems.append(str(e))
        elif not YT_CLIENT_SECRETS.exists() and not YT_TOKEN.exists():
            problems.append(f"{YT_CLIENT_SECRETS} not found (needed for YouTube upload)")
    if problems:
        sys.exit("Setup problems:\n- " + "\n- ".join(problems))


def create_script(project: Path, topic: str | None, args) -> dict:
    """Write the script (or take yours from --script-file), fact-check it and save it with review notes."""
    script_file = getattr(args, "script_file", None)
    channel = get_channel(getattr(args, "channel", None) or load_meta(project).get("channel"))
    meta = dict(load_meta(project), channel=channel["id"])
    if script_file:
        if not topic:
            sys.exit("--script-file needs a title: add --title \"Your video title\"")
        log(f"Using your script from {script_file} (channel: {channel['name']})")
        script = script_from_text(topic, Path(script_file).read_text(encoding="utf-8"), suggest=True, channel=channel)
        save_meta(project, dict(meta, topic=topic, source="manual", created=datetime.now().isoformat()))
    else:
        if not topic:
            sys.exit("No script.json in this project and no topic given.")
        log(f"Writing script: {topic} (channel: {channel['name']})")
        save_meta(project, dict(meta, topic=meta.get("topic") or topic))
        script = write_script(topic, args.minutes, previous_titles(project, channel_id=channel["id"]), channel=channel)
    log("Fact-checking")
    check = fact_check(script, project, channel=channel)
    save_json(project / "factcheck.json", check)
    if args.yes:
        n = apply_fixes(script, check)
        log(f"Applied {n} fact-check fix(es) automatically")
    save_json(project / "script.json", script)
    review = write_review(project, script, check)
    words, chars = narration_stats(script)
    log(f"Narration: {words} words, {chars} characters. Review notes: {review}")
    return script


def process(project: Path, topic: str | None, args) -> None:
    project.mkdir(parents=True, exist_ok=True)
    script_path = project / "script.json"
    log(f"Project: {project}")

    if script_path.exists():
        script = load_json(script_path)
        log("Using existing script.json")
    else:
        script = create_script(project, topic, args)

    approved = project / ".approved"
    if not approved.exists():
        if not args.yes:
            check = load_json(project / "factcheck.json") if (project / "factcheck.json").exists() else {}
            if check.get("issues"):
                answer = input(f"\nThe fact-checker flagged {len(check['issues'])} issue(s) (see review.md). "
                               "Apply its suggested fixes? [y/N] ").strip().lower()
                if answer == "y":
                    log(f"Applied {apply_fixes(script, check)} fix(es)")
                    save_json(script_path, script)
            input(f"\nReview {project / 'review.md'} and edit {script_path} if needed.\n"
                  "Press Enter to continue, or Ctrl+C to stop... ")
            script = load_json(script_path)  # pick up your edits
        approved.touch()

    timeline = make_voiceover(project, script)
    assets = gather_visuals(project, script, timeline)
    assemble(project, timeline, assets)
    write_srt(project, script, timeline)
    ensure_chapters(project, script, timeline)
    make_thumbnail(project, script, timeline, assets)

    if args.no_upload:
        log("Done (upload skipped).")
        return
    uploaded = project / "youtube.json"
    if uploaded.exists():
        log(f"Already uploaded: {load_json(uploaded)['url']}")
        return
    video_id = upload(project, script, assets, args)
    save_json(uploaded, {"video_id": video_id, "url": f"https://youtu.be/{video_id}",
                         "uploaded_at": datetime.now().isoformat()})
    if CAPTION_LANGUAGES:
        try:
            upload_translations(project, script, assets, CAPTION_LANGUAGES, interactive=not args.yes)
        except Exception as e:   # the video itself is uploaded; translations can be added later
            log(f"Translations not added ({type(e).__name__}: {str(e)[:200]})")
    log("All done. The video is private/scheduled: watch it in YouTube Studio before it goes public.")


# ----------------------------------------------------------------------------
# Topic queue (unattended runs)
# ----------------------------------------------------------------------------
def next_topic(path: Path) -> str | None:
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.strip().startswith("#"):
            return line.strip()
    return None


def move_topic(path: Path, topic: str, suffix: str, note: str = "") -> None:
    """Remove a topic from the queue file and append it to <queue>_<suffix>.txt with today's date."""
    lines = path.read_text(encoding="utf-8").splitlines()
    lines = [l for l in lines if l.strip() != topic]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with open(path.with_name(f"{path.stem}_{suffix}.txt"), "a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%Y-%m-%d}\t{topic}" + (f"\t{note}" if note else "") + "\n")


def record_failure(project: Path, error: BaseException) -> int:
    project.mkdir(parents=True, exist_ok=True)
    path = project / "attempts.json"
    data = load_json(path) if path.exists() else {"count": 0, "errors": []}
    data["count"] += 1
    data["errors"].append(f"{datetime.now():%Y-%m-%d %H:%M} {type(error).__name__}: {str(error)[:300]}")
    save_json(path, data)
    return data["count"]


def run_from_topics_file(args) -> None:
    topic = next_topic(args.topics_file)
    if not topic:
        log("No topics left in the file.")
        return
    project = PROJECTS_DIR / slugify(topic)
    try:
        process(project, topic, args)
    except Exception as e:
        n = record_failure(project, e)
        if n >= MAX_ATTEMPTS:
            note = f"{type(e).__name__}: {str(e)[:200]}".replace("\n", " ")
            move_topic(args.topics_file, topic, "failed", note)
            log(f"'{topic}' failed {n} times and was moved to {args.topics_file.stem}_failed.txt")
        else:
            log(f"'{topic}' failed (attempt {n} of {MAX_ATTEMPTS}). It stays in the queue and resumes next run.")
        raise
    (project / "attempts.json").unlink(missing_ok=True)
    move_topic(args.topics_file, topic, "done")


def main() -> None:
    p = argparse.ArgumentParser(description="Faceless science video pipeline")
    p.add_argument("topic", nargs="?", help="Video topic, e.g. 'Why is Mars red?'")
    p.add_argument("--project", type=Path, help="Resume an existing project folder")
    p.add_argument("--topics-file", type=Path, help="Take the next topic from this file (for scheduling)")
    p.add_argument("--minutes", type=float, default=6, help="Target video length (default 6)")
    p.add_argument("--script-file", type=Path,
                   help="Use your own narration from a text file (blank lines separate scenes) instead of writing one")
    p.add_argument("--title", help="Video title, used with --script-file")
    p.add_argument("--channel", help="Channel id from channels.json (default: the default channel)")
    p.add_argument("--yes", action="store_true", help="Unattended: auto-apply fact-check fixes, skip review pause")
    p.add_argument("--no-upload", action="store_true", help="Build the video but don't upload")
    p.add_argument("--privacy", choices=["private", "unlisted", "public"], default="private")
    p.add_argument("--publish-at", help="Schedule publish time, ISO 8601 UTC, e.g. 2026-11-05T15:00:00Z")
    p.add_argument("--synthetic", action="store_true",
                   help="Tick YouTube's altered/synthetic content disclosure (use if you add realistic AI visuals)")
    p.add_argument("--login", action="store_true", help="Sign in to YouTube in the browser, save the login, and exit")
    args = p.parse_args()

    if args.login:
        youtube_credentials(interactive=True, force=True)
        log(f"YouTube login saved to {YT_TOKEN}")
        return
    if args.channel and args.channel not in {c["id"] for c in load_channels()["channels"]}:
        p.error(f"Unknown channel '{args.channel}'; channels: "
                f"{', '.join(c['id'] for c in load_channels()['channels'])}")
    preflight(upload_enabled=not args.no_upload, unattended=args.yes)

    if args.topics_file:
        run_from_topics_file(args)
    elif args.script_file:
        title = args.title or args.topic
        if not title:
            p.error('--script-file needs --title "Your video title"')
        process(args.project or PROJECTS_DIR / slugify(title), title, args)
    elif args.project:
        process(args.project, args.topic, args)
    elif args.topic:
        process(PROJECTS_DIR / slugify(args.topic), args.topic, args)
    else:
        p.error("Give a topic, --project or --topics-file")


if __name__ == "__main__":
    main()
