#!/usr/bin/env python3
"""
Faceless science / Earth / astronomy video pipeline.

  topic -> script (Claude) -> fact-check (Claude + web search) -> your review
        -> voiceover (ElevenLabs) -> visuals (NASA Image Library + Pexels)
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
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
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
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")
FACTCHECK_MODEL = os.getenv("ANTHROPIC_FACTCHECK_MODEL", "claude-opus-5-5")
FACTCHECK_SEARCHES = int(os.getenv("FACTCHECK_SEARCHES", "8"))
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "")
ELEVENLABS_MODEL = os.getenv("ELEVENLABS_MODEL", "eleven_multilingual_v2")
PEXELS_API_KEY = os.getenv("PEXELS_API_KEY", "")
MUSIC_PATH = _path("MUSIC_PATH", "") if os.getenv("MUSIC_PATH") else None
MUSIC_VOLUME = float(os.getenv("MUSIC_VOLUME", "0.10"))
CROSSFADE = float(os.getenv("CROSSFADE", "0.5"))
YT_CLIENT_SECRETS = _path("YT_CLIENT_SECRETS", "client_secret.json")
YT_TOKEN = _path("YT_TOKEN", "youtube_token.json")
THUMB_FONT = str(_path("THUMB_FONT", "")) if os.getenv("THUMB_FONT") else ""
PROJECTS_DIR = _path("PROJECTS_DIR", "projects")
MAX_ATTEMPTS = int(os.getenv("MAX_ATTEMPTS", "3"))

W, H, FPS = 1920, 1080, 30
SCENE_PAD = 0.35           # seconds of breathing room after each narrated scene
WORDS_PER_MINUTE = 150
HTTP_TIMEOUT = 60
MAX_CUE_WORDS, MAX_CUE_CHARS = 8, 42   # caption cue size
ENCODE = ["-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p", "-r", str(FPS)]


# ----------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------
def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def run(cmd: list) -> str:
    cmd = [str(c) for c in cmd]
    result = subprocess.run(cmd, capture_output=True, text=True)
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


def slugify(text: str, maxlen: int = 60) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:maxlen].rstrip("-") or "untitled"


def load_json(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save_json(path: Path, data) -> None:
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def make_session() -> requests.Session:
    """HTTP session that retries transient failures (429 and 5xx) with backoff."""
    retry = Retry(total=4, backoff_factor=1.5, status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=frozenset({"GET", "POST"}), respect_retry_after_header=True,
                  raise_on_status=False)
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.mount("http://", HTTPAdapter(max_retries=retry))
    session.headers["User-Agent"] = "science-video-pipeline/1.1"
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
    source: Literal["nasa", "pexels_video", "pexels_photo"] = Field(
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


WRITER_SYSTEM = """You are the head writer for a faceless YouTube channel about science, planet Earth and astronomy.

Voice: curious, warm and clear, like a great science teacher who loves the subject. Explain with concrete comparisons and real numbers with units. Open with a hook in the first two sentences (a question or a surprising fact), then pay it off by the end.

Accuracy rules (non-negotiable):
- Only state things that are well established. Where scientists disagree or numbers are uncertain, say so plainly.
- No clickbait claims such as "scientists are baffled", "NASA is hiding this" or "this changes everything".
- Prefer figures from NASA, ESA, USGS, NOAA and peer-reviewed consensus. Round numbers sensibly.
- Never invent quotes, studies, people or missions.

Write for the ear: short sentences, no parentheses, no lists, and spell out symbols ("degrees Celsius", not "°C")."""

FACTCHECK_SYSTEM = """You are a meticulous science fact-checker for a YouTube channel about science, Earth and astronomy.

Check every number, date, name and causal claim. When you can search the web, verify against primary sources (NASA, ESA, USGS, NOAA, peer-reviewed papers, university pages) instead of relying on memory, and prefer the most recent figures. Flag substance only, never style. Corrected narration must keep the original length and tone."""

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


def previous_titles(exclude: Path, limit: int = 40) -> list[str]:
    """Titles of videos already made in projects/, newest first, so the writer avoids repeating them."""
    if not PROJECTS_DIR.exists():
        return []
    titles = []
    for path in sorted(PROJECTS_DIR.glob("*/script.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        if path.parent.resolve() == exclude.resolve():
            continue
        try:
            s = load_json(path)
            titles.append(f"{s.get('title', '')} (topic: {s.get('topic', '')})")
        except (OSError, ValueError, AttributeError):
            continue
        if len(titles) >= limit:
            break
    return titles


def write_script(topic: str, minutes: float, previous: list[str]) -> dict:
    words = int(minutes * WORDS_PER_MINUTE)
    avoid = ""
    if previous:
        avoid = ("\n\nVideos this channel has already published. Find a fresh angle and do not reuse "
                 "their titles, hooks or phrasing:\n" + "\n".join(f"- {t}" for t in previous))
    prompt = f"""Write a YouTube video script about: {topic}

Target length: about {words} words of narration (roughly {minutes:g} minutes).

Split the narration into scenes of 1-3 sentences each (about 15-45 words). For every scene choose ONE visual source:
- "nasa": NASA image library. Best for planets, moons, the Sun, stars, galaxies, nebulae, spacecraft, rockets, astronauts and Earth seen from orbit.
- "pexels_video": stock video. Best for things on Earth: oceans, volcanoes, storms, lightning, forests, glaciers, deserts, night skies, laboratories.
- "pexels_photo": stock photo, when a still image of an Earth subject fits better.
The query must be 2-5 concrete, searchable words describing what is visible (e.g. "Jupiter Great Red Spot", "lava flow ocean", "aurora night sky"). Never abstract ideas.

Also give a YouTube title (max 90 characters, curiosity-driven but accurate), a description of 2-3 short paragraphs, 10 to 15 short tags, and 2-5 punchy words of thumbnail text.{avoid}"""
    script = claude_json(ANTHROPIC_MODEL, WRITER_SYSTEM, prompt, Script).model_dump()
    if not script["scenes"]:
        raise ValueError("Claude returned a script with no scenes")
    script["topic"] = topic
    return script


def fact_check(script: dict, project: Path) -> dict:
    """Two passes: research with web search (free text), then convert the notes to structured data."""
    numbered = "\n".join(f"{i}. {s['narration']}" for i, s in enumerate(script["scenes"]))
    notes_prompt = f"""Fact-check this science video narration. Scenes are numbered from 0.

{numbered}

Write your findings as notes with two sections:
1. Issues: for each factual error, outdated figure or misleading simplification give the scene number, what is wrong, the full corrected narration for that scene, and the URL of the source you used.
2. Claims to verify: specific numbers or claims a human should double-check against a primary source, each with the source you consulted, if any.
If the narration is sound, say "No issues found" and still list the claims worth verifying."""
    notes, searches = claude_research(FACTCHECK_MODEL, FACTCHECK_SYSTEM, notes_prompt, FACTCHECK_SEARCHES)
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
def tts(text: str, mp3: Path, align_path: Path, prev_text: str = "", next_text: str = "") -> None:
    body = {
        "text": text,
        "model_id": ELEVENLABS_MODEL,
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
    }
    # Neighbouring text helps the voice keep a natural flow between separate clips.
    if prev_text:
        body["previous_text"] = prev_text
    if next_text:
        body["next_text"] = next_text
    # The with-timestamps variant costs the same and returns per-character timing for exact captions.
    r = HTTP.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}/with-timestamps",
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


def make_voiceover(project: Path, script: dict) -> list[dict]:
    audio_dir = project / "audio"
    audio_dir.mkdir(exist_ok=True)
    scenes = script["scenes"]
    files = [{ext: audio_dir / f"scene_{i:02d}.{ext}" for ext in ("mp3", "wav", "txt", "json")}
             for i in range(len(scenes))]
    # Regenerate a clip only if the narration was edited since the last run.
    todo = [i for i, f in enumerate(files)
            if not f["mp3"].exists() or not f["txt"].exists()
            or f["txt"].read_text(encoding="utf-8") != scenes[i]["narration"].strip()]
    if todo:
        chars = sum(len(scenes[i]["narration"].strip()) for i in todo)
        log(f"Voiceover: {len(todo)} scene(s) to synthesize, {chars} characters")
    timeline = []
    for i, (scene, f) in enumerate(zip(scenes, files)):
        text = scene["narration"].strip()
        if i in todo:
            log(f"Voiceover {i + 1}/{len(scenes)}")
            prev_text = scenes[i - 1]["narration"] if i > 0 else ""
            next_text = scenes[i + 1]["narration"] if i + 1 < len(scenes) else ""
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


# ----------------------------------------------------------------------------
# Step 3: visuals (NASA Image and Video Library + Pexels)
# ----------------------------------------------------------------------------
def nasa_search(query: str, used: set, min_seconds: float = 0):
    r = HTTP.get("https://images-api.nasa.gov/search",
                 params={"q": query, "media_type": "image"}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    for item in r.json().get("collection", {}).get("items", [])[:20]:
        data = (item.get("data") or [{}])[0]
        asset_id = f"nasa:{data.get('nasa_id')}"
        if not data.get("nasa_id") or asset_id in used:
            continue
        try:
            files = HTTP.get(item["href"], timeout=HTTP_TIMEOUT).json()
        except (requests.RequestException, ValueError, KeyError):
            continue
        url = None
        for suffix in ("~large.jpg", "~orig.jpg", "~medium.jpg", "~large.png", "~orig.png"):
            url = next((u for u in files if u.lower().endswith(suffix)), None)
            if url:
                break
        if not url:
            continue
        who = data.get("photographer") or data.get("secondary_creator") or data.get("center") or "NASA"
        return {"kind": "image", "url": url.replace("http://", "https://", 1), "id": asset_id,
                "credit": f"{data.get('title', 'NASA image')} ({who}; NASA image {data['nasa_id']})"}
    return None


def pexels_video_search(query: str, used: set, min_seconds: float = 0):
    if not PEXELS_API_KEY:
        return None
    r = HTTP.get("https://api.pexels.com/videos/search",
                 headers={"Authorization": PEXELS_API_KEY},
                 params={"query": query, "orientation": "landscape", "per_page": 15},
                 timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    videos = [v for v in r.json().get("videos", []) if f"pexels-video:{v['id']}" not in used]
    videos.sort(key=lambda v: (v.get("duration") or 0) < min_seconds)  # long-enough clips first
    for v in videos:
        files = [f for f in v.get("video_files", [])
                 if f.get("file_type") == "video/mp4"
                 and (f.get("width") or 0) >= 1280
                 and (f.get("width") or 0) > (f.get("height") or 0)]
        if not files:
            continue
        best = min(files, key=lambda f: abs(f["width"] - W))
        return {"kind": "video", "url": best["link"], "id": f"pexels-video:{v['id']}",
                "credit": f"Video by {v['user']['name']} on Pexels"}
    return None


def pexels_photo_search(query: str, used: set, min_seconds: float = 0):
    if not PEXELS_API_KEY:
        return None
    r = HTTP.get("https://api.pexels.com/v1/search",
                 headers={"Authorization": PEXELS_API_KEY},
                 params={"query": query, "orientation": "landscape", "per_page": 15},
                 timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    for p in r.json().get("photos", []):
        asset_id = f"pexels-photo:{p['id']}"
        if asset_id in used:
            continue
        return {"kind": "image", "url": p["src"]["original"], "id": asset_id,
                "credit": f"Photo by {p['photographer']} on Pexels"}
    return None


SEARCHERS = {"nasa": nasa_search, "pexels_video": pexels_video_search, "pexels_photo": pexels_photo_search}
FALLBACK_QUERIES = ["earth from space", "milky way", "planet earth", "stars night sky"]


def find_visual(scene: dict, used: set, min_seconds: float) -> dict:
    visual = scene.get("visual") or {}
    preferred = visual.get("source")
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


def gather_visuals(project: Path, script: dict, timeline: list[dict]) -> list[dict]:
    asset_dir = project / "assets"
    asset_dir.mkdir(exist_ok=True)
    manifest_path = project / "assets.json"
    manifest = load_json(manifest_path) if manifest_path.exists() else []
    used = {a["id"] for a in manifest if a}
    for i, scene in enumerate(script["scenes"]):
        if i < len(manifest) and manifest[i] and Path(manifest[i]["path"]).exists():
            continue
        log(f"Visual {i + 1}/{len(script['scenes'])}: {scene.get('visual', {}).get('query')}")
        asset = find_visual(scene, used, timeline[i]["duration"])
        suffix = Path(urlparse(asset["url"]).path).suffix.lower()
        if suffix not in (".jpg", ".jpeg", ".png", ".mp4", ".webp"):
            suffix = ".mp4" if asset["kind"] == "video" else ".jpg"
        dest = asset_dir / f"scene_{i:02d}{suffix}"
        download(asset["url"], dest)
        asset["path"] = str(dest)
        used.add(asset["id"])
        while len(manifest) <= i:
            manifest.append(None)
        manifest[i] = asset
        save_json(manifest_path, manifest)  # save after each so a crash loses nothing
    return manifest[:len(script["scenes"])]


# ----------------------------------------------------------------------------
# Step 4: render with FFmpeg
# ----------------------------------------------------------------------------
def render_scene(asset: dict, frames: int, out: Path, index: int) -> None:
    src = asset["path"]
    if asset["kind"] == "image":
        # Slow "Ken Burns" zoom; alternate zoom-in and zoom-out for variety.
        step = 0.20 / max(frames, 1)
        z = f"1+{step:.7f}*on" if index % 2 == 0 else f"1.20-{step:.7f}*on"
        vf = (f"scale={W * 2}:{H * 2}:force_original_aspect_ratio=increase,crop={W * 2}:{H * 2},"
              f"zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
              f":d={frames}:s={W}x{H}:fps={FPS},setsar=1,format=yuv420p")
        run(["ffmpeg", "-y", "-i", src, "-vf", vf, "-frames:v", frames, *ENCODE, out])
    else:
        vf = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps={FPS},setsar=1,format=yuv420p"
        run(["ffmpeg", "-y", "-stream_loop", "-1", "-i", src, "-vf", vf,
             "-frames:v", frames, "-an", *ENCODE, out])


def crossfade_frames() -> int:
    if CROSSFADE <= 0:
        return 0
    if not ffmpeg_has_filter("xfade"):
        log("Note: this FFmpeg has no xfade filter (needs 4.3 or newer), so scenes use hard cuts")
        return 0
    return round(CROSSFADE * FPS)


def segment_frames(path: Path) -> int | None:
    """Frames in a rendered segment, or None if it is missing or unreadable."""
    if not path.exists():
        return None
    try:
        n = video_frames(path)
        return n if n is not None else round(media_duration(path) * FPS)
    except (RuntimeError, ValueError):
        return None


def join_segments(seg_dir: Path, timeline: list[dict], xf: int, out: Path) -> None:
    n = len(timeline)
    if xf == 0:
        # Hard cuts: concatenate without re-encoding (relative paths, resolved from the list file's folder)
        (seg_dir / "list.txt").write_text(
            "".join(f"file 'seg_{i:02d}.mp4'\n" for i in range(n)), encoding="utf-8")
        run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", seg_dir / "list.txt", "-c", "copy", out])
        return
    # Cross-fades: every segment except the last was rendered xf frames long, and each
    # transition consumes exactly those frames, so the result is as long as the narration.
    log(f"Joining {n} scenes with {xf / FPS:.2f}s cross-fades (one re-encode)")
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


def assemble(project: Path, timeline: list[dict], assets: list[dict]) -> Path:
    seg_dir = project / "segments"
    seg_dir.mkdir(exist_ok=True)
    n = len(assets)
    xf = crossfade_frames() if n > 1 else 0
    for i, (tl, asset) in enumerate(zip(timeline, assets)):
        out = seg_dir / f"seg_{i:02d}.mp4"
        want = tl["frames"] + (xf if i < n - 1 else 0)
        have = segment_frames(out)
        if have == want:
            continue
        if have is not None:
            log(f"Scene {i + 1} changed length ({have} -> {want} frames), re-rendering")
        else:
            log(f"Rendering scene {i + 1}/{n} ({asset['kind']}, {tl['duration']:.1f}s)")
        render_scene(asset, want, out, i)

    video_only = project / "video_only.mp4"
    join_segments(seg_dir, timeline, xf, video_only)

    # Concatenate narration
    audio_dir = project / "audio"
    (audio_dir / "list.txt").write_text(
        "".join(f"file 'scene_{i:02d}.wav'\n" for i in range(n)), encoding="utf-8")
    narration = project / "narration.wav"
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", audio_dir / "list.txt", "-c", "copy", narration])

    # Final mix: narration (+ optional music bed), loudness-normalised for YouTube
    final = project / "final.mp4"
    inputs = ["-i", video_only, "-i", narration]
    loudnorm = "loudnorm=I=-14:TP=-1.5:LRA=11"
    if MUSIC_PATH and MUSIC_PATH.exists():
        inputs += ["-stream_loop", "-1", "-i", MUSIC_PATH]
        fc = (f"[2:a]volume={MUSIC_VOLUME}[m];"
              f"[1:a][m]amix=inputs=2:duration=first:dropout_transition=0,volume=2,{loudnorm}[a]")
    else:
        fc = f"[1:a]{loudnorm}[a]"
    log("Mixing final video")
    run(["ffmpeg", "-y", *inputs, "-filter_complex", fc, "-map", "0:v", "-map", "[a]",
         "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
         "-movflags", "+faststart", "-shortest", final])
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


def build_cues(project: Path, script: dict, timeline: list[dict]) -> list[tuple[float, float, str]]:
    cues, offset = [], 0.0
    for i, (scene, tl) in enumerate(zip(script["scenes"], timeline)):
        text = scene["narration"].strip()
        align_path = project / "audio" / f"scene_{i:02d}.json"
        words = None
        if align_path.exists():
            try:
                words = word_timings(load_json(align_path))
            except (ValueError, KeyError, TypeError):
                words = None
        if not words:
            words = estimated_timings(text, tl["speech"])
        group: list[tuple[str, float, float]] = []
        for w, s, e in words:
            if group and (len(group) >= MAX_CUE_WORDS
                          or sum(len(g[0]) for g in group) + len(group) + len(w) > MAX_CUE_CHARS):
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


def write_srt(project: Path, script: dict, timeline: list[dict]) -> Path:
    entries = [f"{n}\n{srt_time(s)} --> {srt_time(e)}\n{text}\n"
               for n, (s, e, text) in enumerate(build_cues(project, script, timeline), start=1)]
    path = project / "captions.srt"
    path.write_text("\n".join(entries), encoding="utf-8")
    return path


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
# Step 6: YouTube upload
# ----------------------------------------------------------------------------
YT_SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
             "https://www.googleapis.com/auth/youtube.force-ssl"]  # force-ssl is needed for captions


def youtube_credentials(interactive: bool):
    """Load the saved login, refresh it if needed, or sign in through the browser (interactive runs only)."""
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    creds = None
    if YT_TOKEN.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(YT_TOKEN), YT_SCOPES)
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
            raise RuntimeError("YouTube login is missing or expired. Run the pipeline once without --yes "
                               "to sign in again (see 'Login expiry' in the README).")
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


def build_description(script: dict, assets: list[dict]) -> str:
    credits = list(dict.fromkeys(a["credit"] for a in assets if a))
    desc = clean_text(script.get("description", "")).strip()
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


def upload(project: Path, script: dict, assets: list[dict], args) -> str:
    from googleapiclient.http import MediaFileUpload
    youtube = get_youtube(interactive=not args.yes)
    status = {"privacyStatus": args.privacy, "selfDeclaredMadeForKids": False,
              "containsSyntheticMedia": bool(args.synthetic)}
    if args.publish_at:
        status["privacyStatus"] = "private"  # required by YouTube for scheduled videos
        status["publishAt"] = args.publish_at
    body = {
        "snippet": {
            "title": clean_text(script["title"])[:100],
            "description": build_description(script, assets),
            "tags": trim_tags(script.get("tags", [])),
            "categoryId": "28",  # Science & Technology
            "defaultLanguage": "en",
            "defaultAudioLanguage": "en",
        },
        "status": status,
    }
    log("Uploading to YouTube...")
    request = youtube.videos().insert(
        part="snippet,status", body=body,
        media_body=MediaFileUpload(str(project / "final.mp4"), chunksize=-1, resumable=True))
    response = None
    while response is None:
        progress, response = request.next_chunk()
        if progress:
            log(f"  {int(progress.progress() * 100)}%")
    video_id = response["id"]
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
            body={"snippet": {"videoId": video_id, "language": "en", "name": "English", "isDraft": False}},
            media_body=MediaFileUpload(str(project / "captions.srt"), mimetype="application/octet-stream"),
        ).execute()
        log("Captions uploaded")
    except Exception as e:
        log(f"Captions not uploaded ({e})")
    return video_id


# ----------------------------------------------------------------------------
# Orchestration
# ----------------------------------------------------------------------------
def preflight(upload_enabled: bool, unattended: bool) -> None:
    problems = []
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            problems.append(f"{tool} is not installed or not on PATH")
    for name in ("ANTHROPIC_API_KEY", "ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID"):
        if not os.getenv(name):
            problems.append(f"{name} is missing from .env")
    if not PEXELS_API_KEY:
        log("Note: PEXELS_API_KEY not set, so only NASA images will be used")
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


def process(project: Path, topic: str | None, args) -> None:
    project.mkdir(parents=True, exist_ok=True)
    script_path = project / "script.json"
    log(f"Project: {project}")

    if script_path.exists():
        script = load_json(script_path)
        log("Using existing script.json")
    else:
        if not topic:
            sys.exit("No script.json in this project and no topic given.")
        log(f"Writing script: {topic}")
        script = write_script(topic, args.minutes, previous_titles(project))
        log("Fact-checking")
        check = fact_check(script, project)
        save_json(project / "factcheck.json", check)
        if args.yes:
            n = apply_fixes(script, check)
            log(f"Applied {n} fact-check fix(es) automatically")
        save_json(script_path, script)
        review = write_review(project, script, check)
        words, chars = narration_stats(script)
        log(f"Narration: {words} words, {chars} characters. Review notes: {review}")

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
    p.add_argument("--yes", action="store_true", help="Unattended: auto-apply fact-check fixes, skip review pause")
    p.add_argument("--no-upload", action="store_true", help="Build the video but don't upload")
    p.add_argument("--privacy", choices=["private", "unlisted", "public"], default="private")
    p.add_argument("--publish-at", help="Schedule publish time, ISO 8601 UTC, e.g. 2026-11-05T15:00:00Z")
    p.add_argument("--synthetic", action="store_true",
                   help="Tick YouTube's altered/synthetic content disclosure (use if you add realistic AI visuals)")
    args = p.parse_args()

    preflight(upload_enabled=not args.no_upload, unattended=args.yes)

    if args.topics_file:
        run_from_topics_file(args)
    elif args.project:
        process(args.project, args.topic, args)
    elif args.topic:
        process(PROJECTS_DIR / slugify(args.topic), args.topic, args)
    else:
        p.error("Give a topic, --project or --topics-file")


if __name__ == "__main__":
    main()
