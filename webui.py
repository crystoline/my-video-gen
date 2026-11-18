#!/usr/bin/env python3
"""
Local web interface for MyVideoGen: prepare topics, review scripts, generate and re-generate videos.

  .venv/bin/python webui.py            # then open http://localhost:8765 in your browser

It runs the same steps as pipeline.py, one job at a time, and only accepts connections from this computer.
Everything it does is saved in the project folders, so the command line and the web UI can be mixed freely.
"""

from __future__ import annotations

import argparse
import os
import queue
import re
import shutil
import threading
import time
import traceback
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from flask import Flask, abort, jsonify, request, send_file
from werkzeug.exceptions import HTTPException

import pipeline as pl

STATIC_DIR = pl.SCRIPT_DIR / "webui"
ENV_PATH = pl.SCRIPT_DIR / ".env"
TOPICS_PATH = pl.SCRIPT_DIR / "topics.txt"
ALLOWED_HOSTS = {"localhost", "127.0.0.1", "::1"}
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,80}$")
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv"}
SOURCES = ("nasa", "stock_video", "stock_photo", "archive", "ai")
MANUAL_TEXT = "my_script.txt"   # narration pasted in "I have the script", as typed

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 ** 3   # custom video clips can be large


# ----------------------------------------------------------------------------
# Jobs: one worker thread runs pipeline steps in order, capturing their log lines
# ----------------------------------------------------------------------------
class Job:
    def __init__(self, project: str | None, action: str, label: str, fn, params: dict):
        self.id = uuid.uuid4().hex[:12]
        self.project, self.action, self.label, self.fn, self.params = project, action, label, fn, params
        self.status = "queued"
        self.lines: list[str] = []
        self.error: str | None = None
        self.created, self.started, self.finished = time.time(), None, None

    def to_dict(self, since: int | None = None) -> dict:
        d = {"id": self.id, "project": self.project, "action": self.action, "label": self.label,
             "status": self.status, "error": self.error, "created": self.created, "started": self.started,
             "finished": self.finished, "line_count": len(self.lines),
             "last_line": self.lines[-1] if self.lines else ""}
        if since is not None:
            d["lines"] = self.lines[since:]
        return d


JOBS: dict[str, Job] = {}
JOB_QUEUE: "queue.Queue[Job]" = queue.Queue()
_current = threading.local()
_jobs_lock = threading.Lock()


def _capture(line: str) -> None:
    job = getattr(_current, "job", None)
    if job is not None:
        job.lines.append(line)


pl.LOG_HOOKS.append(_capture)


def worker() -> None:
    while True:
        job = JOB_QUEUE.get()
        job.status, job.started = "running", time.time()
        _current.job = job
        try:
            job.fn(**job.params)
            pl.log(f"Finished: {job.label}")
            job.status = "done"
        except (Exception, SystemExit) as e:
            job.error = str(e) or type(e).__name__
            job.lines.append(f"[{datetime.now():%H:%M:%S}] FAILED: {job.error}")
            job.status = "failed"
            traceback.print_exc()
        finally:
            job.finished = time.time()
            _current.job = None
            _write_activity(job)


def _write_activity(job: Job) -> None:
    if not job.project:
        return
    folder = pl.PROJECTS_DIR / job.project
    if not folder.is_dir():
        return
    took = (job.finished or time.time()) - (job.started or job.created)
    with open(folder / "activity.log", "a", encoding="utf-8") as f:
        f.write(f"=== {datetime.fromtimestamp(job.created):%Y-%m-%d %H:%M} {job.label}: {job.status} ({took:.0f} s)\n")
        f.write("".join(line + "\n" for line in job.lines))


def all_jobs() -> list[Job]:
    """Newest first."""
    with _jobs_lock:
        return sorted(JOBS.values(), key=lambda j: -j.created)


def busy(slug: str) -> Job | None:
    return next((j for j in all_jobs() if j.project == slug and j.status in ("queued", "running")), None)


def enqueue(project: str | None, action: str, label: str, fn, **params) -> Job:
    if project and busy(project):
        abort(409, "This video already has a job queued or running. Wait for it to finish.")
    job = Job(project, action, label, fn, params)
    with _jobs_lock:
        JOBS[job.id] = job
        finished = [j for j in JOBS.values() if j.status in ("done", "failed")]
        for old in finished[:-200]:   # keep memory bounded
            JOBS.pop(old.id, None)
    JOB_QUEUE.put(job)
    return job


# ----------------------------------------------------------------------------
# Project helpers
# ----------------------------------------------------------------------------
def project_dir(slug: str, must_exist: bool = True) -> Path:
    if not SLUG_RE.match(slug or ""):
        abort(404, "Unknown video")
    p = pl.PROJECTS_DIR / slug
    if must_exist and not p.is_dir():
        abort(404, "Unknown video")
    return p


def require_idle(slug: str) -> None:
    if busy(slug):
        abort(409, "A job is running for this video. Wait for it to finish before changing it.")


def load_script(p: Path) -> dict | None:
    path = p / "script.json"
    return pl.load_json(path) if path.exists() else None


def need_script(p: Path) -> dict:
    script = load_script(p)
    if not script:
        abort(409, "This video has no script yet")
    return script


def mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def file_url(slug: str, path: Path) -> str | None:
    if not path.exists():
        return None
    rel = path.relative_to(pl.PROJECTS_DIR / slug).as_posix()
    return f"/files/{slug}/{rel}?v={int(mtime(path))}"


def timeline_for(p: Path, script: dict) -> list[dict]:
    """Real scene lengths when the voiceover is complete, otherwise estimates from word counts."""
    tl_path = p / "timeline.json"
    if tl_path.exists() and not pl.voiceover_todo(p, script):
        tl = pl.load_json(tl_path)
        if len(tl) == len(script["scenes"]):
            return tl
    return pl.estimated_timeline(script)


def project_state(slug: str, detail: bool = False) -> dict:
    p = pl.PROJECTS_DIR / slug
    script = load_script(p)
    meta = pl.load_meta(p)
    job = busy(slug)
    scenes = script["scenes"] if script else []
    n = len(scenes)
    todo = pl.voiceover_todo(p, script) if script else []
    manifest = pl.load_manifest(p)
    have_asset = [i < len(manifest) and bool(manifest[i]) and Path(manifest[i]["path"]).exists() for i in range(n)]
    final = p / "final.mp4"
    newest_input = max([mtime(f) for f in (p / "audio").glob("scene_*.mp3")]
                       + [mtime(f) for f in (p / "assets").glob("scene_*")]
                       + [float(meta.get("music_changed") or 0)])
    if not final.exists():
        video = "missing"
    elif mtime(final) < newest_input or todo or not all(have_asset):
        video = "outdated"
    else:
        video = "ready"
    youtube = pl.load_json(p / "youtube.json") if (p / "youtube.json").exists() else None
    short_yt = p / "short" / "youtube.json"
    stats_path = p / "stats.json"
    approved = (p / ".approved").exists()
    voiced_any = len(todo) < n
    if not script:
        step = "writing" if job else "empty"
    elif not approved and not voiced_any:
        step = "review"
    elif todo:
        step = "voiceover"
    elif not all(have_asset):
        step = "visuals"
    elif video != "ready":
        step = "render"
    elif not youtube:
        step = "upload"
    else:
        step = "uploaded"
    words, chars = pl.narration_stats(script) if script else (0, 0)
    todo_chars = sum(len(scenes[i]["narration"].strip()) for i in todo)
    thumb = p / "thumbnail.jpg"
    state = {
        "slug": slug,
        "title": (script or {}).get("title") or meta.get("topic") or slug,
        "topic": (script or {}).get("topic") or meta.get("topic", ""),
        "source": meta.get("source", "claude"),
        "step": step, "scenes": n, "words": words, "chars": chars,
        "minutes": round(words / pl.WORDS_PER_MINUTE, 1),
        "voice_todo": todo, "voice_todo_chars": todo_chars,
        "visuals_ready": sum(have_asset), "video": video, "approved": approved,
        "thumbnail_url": file_url(slug, thumb),
        "youtube": youtube,
        "short_youtube": pl.load_json(short_yt) if short_yt.exists() else None,
        "stats": pl.load_json(stats_path) if stats_path.exists() else None,
        "updated": max([mtime(f) for f in p.iterdir() if f.name != "stats.json"] + [mtime(p)]) if p.exists() else 0,
        "job": job.to_dict() if job else None,
    }
    if not detail:
        return state
    tl_path = p / "timeline.json"
    timeline = pl.load_json(tl_path) if tl_path.exists() else []
    scene_info = []
    for i, scene in enumerate(scenes):
        f = pl.audio_files(p, i)
        audio_state = "missing" if not f["mp3"].exists() else ("changed" if i in todo else "current")
        asset = manifest[i] if i < len(manifest) and manifest[i] else None
        scene_info.append({
            "audio": {"state": audio_state, "url": file_url(slug, f["mp3"]) if f["mp3"].exists() else None,
                      "seconds": timeline[i]["speech"] if i < len(timeline) and audio_state == "current" else None,
                      "text": f["txt"].read_text(encoding="utf-8") if f["txt"].exists() else None},
            "asset": None if not asset else {
                "kind": asset.get("kind"), "source": asset.get("source"), "query": asset.get("query"),
                "credit": asset.get("credit", ""), "id": asset.get("id"),
                "url": file_url(slug, Path(asset["path"]))},
        })
    check_path = p / "factcheck.json"
    notes_path = p / "factcheck_notes.md"
    attempts = p / "attempts.json"
    preview = p / "preview_mix.mp3"
    track = pl.project_music(p, save=False)
    state.update({
        "short": short_state(slug, p, script, timeline_for(p, script) if script else [], meta, newest_input),
        "music": {"choice": meta.get("music"), "auto": bool(meta.get("music_auto")),
                  "track": track.name if track else None, "default": pl.describe_default_music()},
        "script": script, "factcheck": pl.load_json(check_path) if check_path.exists() else None,
        "factcheck_notes": notes_path.read_text(encoding="utf-8") if notes_path.exists() else "",
        "scene_info": scene_info,
        "final_url": file_url(slug, final),
        "captions_url": file_url(slug, p / "captions.srt"),
        "preview_url": file_url(slug, preview),
        "thumbnail_outdated": thumb.exists() and mtime(thumb) < mtime(p / "script.json"),
        "attempts": pl.load_json(attempts) if attempts.exists() else None,
        "jobs": [j.to_dict() for j in all_jobs() if j.project == slug][:15],
        "activity": (p / "activity.log").read_text(encoding="utf-8")[-20000:] if (p / "activity.log").exists() else "",
    })
    return state


def short_state(slug: str, p: Path, script: dict | None, timeline: list[dict], meta: dict, newest_input: float):
    if not script:
        return None
    s = pl.short_settings(p, script, timeline)
    video = p / "short" / "short.mp4"
    changed = max(newest_input, float(meta.get("short_changed") or 0))
    yt = p / "short" / "youtube.json"
    return dict(s, url=file_url(slug, video), scene_seconds=[t["duration"] for t in timeline],
                state="missing" if not video.exists() else "outdated" if mtime(video) < changed else "ready",
                youtube=pl.load_json(yt) if yt.exists() else None, max_seconds=pl.SHORT_MAX_SECONDS,
                target_seconds=pl.SHORT_TARGET_SECONDS)


def list_projects() -> list[dict]:
    if not pl.PROJECTS_DIR.exists():
        return []
    out = []
    for d in pl.PROJECTS_DIR.iterdir():
        if d.is_dir() and SLUG_RE.match(d.name):
            try:
                out.append(project_state(d.name))
            except (OSError, ValueError, KeyError, TypeError) as e:   # e.g. a hand-edited script.json that no longer parses
                print(f"Skipping {d.name}: {type(e).__name__}: {e}")
    return sorted(out, key=lambda s: -s["updated"])


# ----------------------------------------------------------------------------
# Script editing (with files following their scenes when scenes are moved, added or deleted)
# ----------------------------------------------------------------------------
def clean_script(new: dict, old: dict) -> dict:
    def text(value, name, limit=None, required=False):
        if not isinstance(value, str):
            raise ValueError(f"{name} must be text")
        value = value.strip()
        if required and not value:
            raise ValueError(f"{name} cannot be empty")
        if limit and len(value) > limit:
            raise ValueError(f"{name} is longer than {limit} characters")
        return value

    scenes = new.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        raise ValueError("The script needs at least one scene")
    out_scenes = []
    for k, s in enumerate(scenes, start=1):
        visual = s.get("visual") or {}
        source = visual.get("source")
        if source not in SOURCES:
            raise ValueError(f"Scene {k}: visual source must be one of {', '.join(SOURCES)}")
        out_scenes.append({"narration": text(s.get("narration"), f"Scene {k} narration", 2000, True),
                           "visual": {"source": source,
                                      "query": text(visual.get("query", ""), f"Scene {k} visual search", 100, True)}})
    tags = new.get("tags", [])
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        raise ValueError("Tags must be a list of text")
    thumb_scene = new.get("thumbnail_scene")
    if thumb_scene is not None and (not isinstance(thumb_scene, int) or not 0 <= thumb_scene < len(out_scenes)):
        thumb_scene = None
    out = {"title": text(new.get("title", ""), "Title", 100, True),
           "description": text(new.get("description", ""), "Description", 4500),
           "tags": [t.strip() for t in tags if t.strip()],
           "thumbnail_text": text(new.get("thumbnail_text", ""), "Thumbnail text", 60),
           "scenes": out_scenes,
           "topic": old.get("topic", "")}
    if thumb_scene is not None:
        out["thumbnail_scene"] = thumb_scene
    return out


def remap_scenes(p: Path, origin: list, old_n: int) -> None:
    """Move each scene's narration clip and visual to its new position. origin[j] = old index of new scene j, or None."""
    if origin == list(range(old_n)):
        return
    if (not isinstance(origin, list)
            or any(o is not None and (not isinstance(o, int) or not 0 <= o < old_n) for o in origin)
            or len([o for o in origin if o is not None]) != len({o for o in origin if o is not None})):
        raise ValueError("Scene order is inconsistent; reload the page and try again")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    staging = p / ".remap"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir()
    manifest = pl.load_manifest(p)
    for i in range(old_n):
        d = staging / str(i)
        d.mkdir()
        for ext, f in pl.audio_files(p, i).items():
            if f.exists():
                f.replace(d / f"audio.{ext}")
        for f in (p / "assets").glob(f"scene_{i:02d}.*"):
            f.replace(d / f"asset{f.suffix}")
    new_manifest = []
    for j, o in enumerate(origin):
        entry = None
        if o is not None:
            d = staging / str(o)
            for f in d.iterdir():
                if f.name.startswith("audio."):
                    f.replace(pl.audio_files(p, j)[f.suffix[1:]])
                elif f.name.startswith("asset."):
                    dest = p / "assets" / f"scene_{j:02d}{f.suffix}"
                    f.replace(dest)
                    if o < len(manifest) and manifest[o]:
                        entry = dict(manifest[o], path=str(dest))
        new_manifest.append(entry)
    removed = [i for i in range(old_n) if i not in origin and any((staging / str(i)).iterdir())]
    if removed:
        trash = p / ".trash" / stamp
        trash.mkdir(parents=True, exist_ok=True)
        for i in removed:
            (staging / str(i)).replace(trash / f"scene_{i:02d}")
    shutil.rmtree(staging, ignore_errors=True)
    if (p / "assets.json").exists() or any(new_manifest):
        pl.save_json(p / "assets.json", new_manifest)
    # Rendered scenes that moved are rebuilt (their zoom direction and cross-fade depend on position)
    for j, o in enumerate(origin):
        if o != j:
            (p / "segments" / f"seg_{j:02d}.mp4").unlink(missing_ok=True)
    (p / "timeline.json").unlink(missing_ok=True)
    # Keep fact-check issues pointing at the right scenes
    check_path = p / "factcheck.json"
    if check_path.exists():
        check = pl.load_json(check_path)
        for issue in check.get("issues", []):
            s = issue.get("scene")
            issue["scene"] = origin.index(s) if s in origin else None
        pl.save_json(check_path, check)


def move_to_trash(p: Path, names: list[str]) -> None:
    trash = p / ".trash" / datetime.now().strftime("%Y%m%d-%H%M%S")
    for name in names:
        src = p / name
        if src.exists():
            trash.mkdir(parents=True, exist_ok=True)
            src.replace(trash / name)


# ----------------------------------------------------------------------------
# Job bodies (they run in the worker thread)
# ----------------------------------------------------------------------------
def run_factcheck(p: Path, script: dict) -> None:
    pl.log("Fact-checking (web search)")
    check = pl.fact_check(script, p)
    pl.save_json(p / "factcheck.json", check)
    pl.write_review(p, script, check)
    words, chars = pl.narration_stats(script)
    pl.log(f"Fact-check found {len(check.get('issues', []))} issue(s). Narration: {words} words, {chars} characters")


def job_create(slug: str, topic: str, minutes: float) -> None:
    p = pl.PROJECTS_DIR / slug
    pl.log(f"Writing script: {topic}")
    script = pl.write_script(topic, minutes, pl.previous_titles(p))
    pl.save_json(p / "script.json", script)
    run_factcheck(p, script)


def job_create_manual(slug: str, title: str, suggest: bool, factcheck: bool) -> None:
    p = pl.PROJECTS_DIR / slug
    script = pl.script_from_text(title, (p / MANUAL_TEXT).read_text(encoding="utf-8"), suggest)
    words, chars = pl.narration_stats(script)
    pl.log(f"Your script: {len(script['scenes'])} scenes, {words} words, {chars} characters (about "
           f"{words / pl.WORDS_PER_MINUTE:.1f} min)")
    pl.save_json(p / "script.json", script)
    if factcheck:
        run_factcheck(p, script)
    else:
        pl.write_review(p, script, {})
        pl.log("Fact-check skipped. You can run it any time with “Fact-check again” on the Script tab.")


def job_factcheck(slug: str) -> None:
    p = pl.PROJECTS_DIR / slug
    run_factcheck(p, need_script(p))


def job_rewrite(slug: str, minutes: float) -> None:
    p = pl.PROJECTS_DIR / slug
    old = need_script(p)
    topic = old.get("topic") or old.get("title")
    pl.log(f"Rewriting the script from scratch: {topic}")
    script = pl.write_script(topic, minutes, pl.previous_titles(p))
    # The old narration, visuals and renders no longer match; keep them in .trash rather than deleting
    move_to_trash(p, ["script.json", "factcheck.json", "factcheck_notes.md", "review.md", "audio", "assets",
                      "assets.json", "rejected_assets.json", "segments", "timeline.json", "video_only.mp4",
                      "narration.wav", "final.mp4", "thumbnail.jpg", "thumb_frame.jpg", "captions.srt",
                      ".approved", "preview_mix.mp3", "short"])
    pl.save_json(p / "script.json", script)
    run_factcheck(p, script)


def job_voiceover(slug: str) -> None:
    p = pl.PROJECTS_DIR / slug
    script = need_script(p)
    (p / ".approved").touch()   # the command line will not pause for review on this project again
    try:
        pl.make_voiceover(p, script)
    finally:
        _usage_cache["at"] = 0.0   # characters were spent: read the balance again next time


def job_stats() -> None:
    """Fetch views and watch time for every uploaded video and Short, saved as stats.json in each project."""
    uploads: dict[str, tuple[str, str]] = {}   # video id -> (project, "video" or "short")
    for d in sorted(pl.PROJECTS_DIR.iterdir()) if pl.PROJECTS_DIR.exists() else []:
        for kind, path in (("video", d / "youtube.json"), ("short", d / "short" / "youtube.json")):
            if d.is_dir() and path.exists():
                uploads[pl.load_json(path)["video_id"]] = (d.name, kind)
    if not uploads:
        pl.log("Nothing has been uploaded yet, so there are no statistics to fetch")
        return
    pl.log(f"Fetching statistics for {len(uploads)} upload(s)")
    stats, notes = pl.youtube_stats(list(uploads))
    for note in notes:
        pl.log(note)
    per_project: dict[str, dict] = {}
    for video_id, (slug, kind) in uploads.items():
        per_project.setdefault(slug, {})[kind] = dict(stats.get(video_id) or {}, video_id=video_id)
    fetched = datetime.now().isoformat(timespec="seconds")
    for slug, data in per_project.items():
        pl.save_json(pl.PROJECTS_DIR / slug / "stats.json", dict(data, fetched=fetched))
    pl.log(f"Statistics saved for {len(per_project)} video(s)")


def job_revoice(slug: str, scene: int) -> None:
    p = pl.PROJECTS_DIR / slug
    for f in pl.audio_files(p, scene).values():
        f.unlink(missing_ok=True)
    job_voiceover(slug)


def job_visuals(slug: str) -> None:
    p = pl.PROJECTS_DIR / slug
    script = need_script(p)
    pl.gather_visuals(p, script, timeline_for(p, script))


def job_find_visual(slug: str, scene: int) -> None:
    p = pl.PROJECTS_DIR / slug
    script = need_script(p)
    pl.refetch_visual(p, script, timeline_for(p, script), scene)


def job_pick_visual(slug: str, scene: int, candidate: dict, query: str) -> None:
    p = pl.PROJECTS_DIR / slug
    script = need_script(p)
    pl.use_visual(p, scene, dict(candidate, query=query))
    # Remember the search that found it, so "Next result" continues from there
    visual = script["scenes"][scene].setdefault("visual", {})
    if query and (visual.get("query") != query or visual.get("source") != candidate["source"]):
        visual.update(source=candidate["source"], query=query)
        pl.save_json(p / "script.json", script)


def job_render(slug: str) -> None:
    p = pl.PROJECTS_DIR / slug
    script = need_script(p)
    todo = pl.voiceover_todo(p, script)
    if todo:
        raise RuntimeError(f"{len(todo)} scene(s) have new or changed narration. Generate the voiceover first.")
    (p / ".approved").touch()
    timeline = pl.make_voiceover(p, script)   # nothing to synthesize; this only measures the clips
    assets = pl.gather_visuals(p, script, timeline)
    pl.assemble(p, timeline, assets)
    pl.write_srt(p, script, timeline)
    pl.make_thumbnail(p, script, timeline, assets)
    pl.log("Captions and thumbnail updated")


def job_short(slug: str) -> None:
    p = pl.PROJECTS_DIR / slug
    script = need_script(p)
    if pl.voiceover_todo(p, script):
        raise RuntimeError("Some scenes have new or changed narration. Generate the voiceover first.")
    timeline = pl.make_voiceover(p, script)   # nothing to synthesize; this only measures the clips
    assets = pl.gather_visuals(p, script, timeline)
    pl.make_short(p, script, timeline, assets)


def job_upload_short(slug: str, privacy: str, publish_at: str | None, synthetic: bool) -> None:
    p = pl.PROJECTS_DIR / slug
    script = need_script(p)
    done = p / "short" / "youtube.json"
    if done.exists():
        raise RuntimeError(f"This Short is already uploaded: {pl.load_json(done)['url']}")
    if (project_state(slug, detail=True)["short"] or {}).get("state") != "ready":
        raise RuntimeError("The Short is missing or out of date. Make it first.")
    args = SimpleNamespace(yes=True, privacy=privacy, publish_at=publish_at, synthetic=synthetic)
    video_id = pl.upload_short(p, script, pl.load_manifest(p)[:len(script["scenes"])], args)
    pl.save_json(done, {"video_id": video_id, "url": f"https://youtube.com/shorts/{video_id}",
                        "uploaded_at": datetime.now().isoformat()})


def job_thumbnail(slug: str) -> None:
    p = pl.PROJECTS_DIR / slug
    script = need_script(p)
    n = len(script["scenes"])
    if not all((p / "segments" / f"seg_{i:02d}.mp4").exists() for i in range(n)):
        raise RuntimeError("Render the video first; the thumbnail is taken from a rendered scene")
    timeline = pl.load_json(p / "timeline.json")
    pl.make_thumbnail(p, script, timeline, pl.load_manifest(p)[:n])
    pl.log("Thumbnail updated")


def job_preview(slug: str, seconds: float) -> None:
    p = pl.PROJECTS_DIR / slug
    pl.preview_mix(p, p / "preview_mix.mp3", seconds)
    pl.log(f"Sound preview ready ({seconds:g} s, voice tone {pl.VOICE_TONE}, "
           f"{pl.describe_music(pl.project_music(p, save=False))})")


def job_upload(slug: str, privacy: str, publish_at: str | None, synthetic: bool) -> None:
    p = pl.PROJECTS_DIR / slug
    script = need_script(p)
    if (p / "youtube.json").exists():
        raise RuntimeError(f"Already uploaded: {pl.load_json(p / 'youtube.json')['url']}")
    if project_state(slug)["video"] != "ready":
        raise RuntimeError("The video is missing or outdated. Render it first.")
    args = SimpleNamespace(yes=True, privacy=privacy, publish_at=publish_at, synthetic=synthetic)
    video_id = pl.upload(p, script, pl.load_manifest(p)[:len(script["scenes"])], args)
    pl.save_json(p / "youtube.json", {"video_id": video_id, "url": f"https://youtu.be/{video_id}",
                                      "uploaded_at": datetime.now().isoformat()})


# ----------------------------------------------------------------------------
# Request guard and error format
# ----------------------------------------------------------------------------
@app.before_request
def guard():
    # Only answer requests addressed to this computer (blocks DNS-rebinding tricks), and require a
    # custom header on changes, which other websites' pages cannot send (blocks cross-site requests).
    raw = request.host or ""
    host = raw[1:raw.find("]")] if raw.startswith("[") else raw.split(":")[0]
    if host not in ALLOWED_HOSTS:
        abort(403, "This interface only answers on localhost")
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get("X-Requested-By") != "myvideogen":
        abort(403, "Missing X-Requested-By header")


@app.errorhandler(HTTPException)
def http_error(e):
    return jsonify({"error": e.description}), e.code


@app.errorhandler(ValueError)
def value_error(e):
    return jsonify({"error": str(e)}), 400


def body() -> dict:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        abort(400, "Expected a JSON object")
    return data


# ----------------------------------------------------------------------------
# Routes: pages and files
# ----------------------------------------------------------------------------
@app.get("/")
def index():
    return send_file(STATIC_DIR / "index.html", max_age=0)


@app.get("/static/<path:name>")
def static_file(name):
    path = (STATIC_DIR / name).resolve()
    if STATIC_DIR.resolve() not in path.parents or not path.is_file():
        abort(404)
    return send_file(path, max_age=0)


@app.get("/files/<slug>/<path:rel>")
def project_file(slug, rel):
    p = project_dir(slug).resolve()
    path = (p / rel).resolve()
    if p not in path.parents or not path.is_file():
        abort(404)
    return send_file(path, max_age=0, conditional=True)


# ----------------------------------------------------------------------------
# Routes: projects
# ----------------------------------------------------------------------------
@app.get("/api/projects")
def api_projects():
    return jsonify(list_projects())


@app.post("/api/projects")
def api_create_project():
    data = body()
    if data.get("mode") == "manual":
        return create_manual_project(data)
    topic = str(data.get("topic", "")).strip()
    if not topic:
        raise ValueError("Enter a topic")
    minutes = float(data.get("minutes") or 6)
    if not 1 <= minutes <= 30:
        raise ValueError("Length must be between 1 and 30 minutes")
    slug = pl.slugify(topic)
    p = project_dir(slug, must_exist=False)
    if (p / "script.json").exists() or busy(slug):
        abort(409, f"A video for this topic already exists ({slug})")
    p.mkdir(parents=True, exist_ok=True)
    pl.save_json(p / "project.json", {"topic": topic, "minutes": minutes, "created": datetime.now().isoformat()})
    job = enqueue(slug, "create", "Write and fact-check the script", job_create, slug=slug, topic=topic, minutes=minutes)
    if data.get("from_queue") and TOPICS_PATH.exists():
        comments, topics = read_queue()
        write_queue(comments, [t for t in topics if t != topic])
    return jsonify({"slug": slug, "job": job.to_dict()})


def create_manual_project(data: dict):
    """A video from narration you wrote: kept word for word, then (optionally) visuals and fact-check."""
    title = " ".join(str(data.get("title") or "").split())
    text = str(data.get("script") or "")
    if not title:
        raise ValueError("Enter a title")
    if len(title) > 100:
        raise ValueError("The title is longer than 100 characters")
    if len(text) > 60_000:
        raise ValueError("The script is longer than 60,000 characters")
    scenes = pl.parse_manual_script(text)
    if not scenes:
        raise ValueError("Paste or type the narration")
    if len(scenes) > 200:
        raise ValueError(f"That is {len(scenes)} scenes; use at most 200")
    slug = pl.slugify(title)
    p = project_dir(slug, must_exist=False)
    if (p / "script.json").exists() or busy(slug):
        abort(409, f"A video with this title already exists ({slug})")
    p.mkdir(parents=True, exist_ok=True)
    (p / MANUAL_TEXT).write_text(text, encoding="utf-8")   # kept, so nothing is lost if preparing it fails
    words = len(" ".join(s["narration"] for s in scenes).split())
    suggest, factcheck = bool(data.get("suggest", True)), bool(data.get("factcheck", True))
    pl.save_meta(p, {"topic": title, "minutes": round(words / pl.WORDS_PER_MINUTE, 1), "source": "manual",
                     "manual": {"suggest": suggest, "factcheck": factcheck}, "created": datetime.now().isoformat()})
    job = start_manual_job(slug, title, suggest, factcheck)
    return jsonify({"slug": slug, "job": job.to_dict(), "scenes": len(scenes)})


def start_manual_job(slug: str, title: str, suggest: bool, factcheck: bool) -> Job:
    label = "Prepare your script" + (" and fact-check it" if factcheck else "")
    return enqueue(slug, "create", label, job_create_manual, slug=slug, title=title, suggest=suggest,
                   factcheck=factcheck)


@app.post("/api/projects/<slug>/prepare")
def api_prepare_again(slug):
    """Try again to prepare a script you pasted, after a failure (the text is in my_script.txt)."""
    p = project_dir(slug)
    meta = pl.load_meta(p)
    if (p / "script.json").exists() or not (p / MANUAL_TEXT).exists():
        abort(409, "There is no pasted script waiting to be prepared")
    opts = meta.get("manual") or {}
    job = start_manual_job(slug, meta.get("topic") or slug, bool(opts.get("suggest", True)),
                           bool(opts.get("factcheck", True)))
    return jsonify(job.to_dict())


@app.get("/api/projects/<slug>")
def api_project(slug):
    project_dir(slug)
    return jsonify(project_state(slug, detail=True))


@app.put("/api/projects/<slug>/script")
def api_save_script(slug):
    p = project_dir(slug)
    require_idle(slug)
    data = body()
    old = need_script(p)
    new = clean_script(data.get("script") or {}, old)
    origin = data.get("origin")
    if origin is not None:
        if len(origin) != len(new["scenes"]):
            raise ValueError("Scene order does not match the scenes; reload the page and try again")
        remap_scenes(p, origin, len(old["scenes"]))
    pl.save_json(p / "script.json", new)
    check_path = p / "factcheck.json"
    pl.write_review(p, new, pl.load_json(check_path) if check_path.exists() else {})
    return jsonify(project_state(slug, detail=True))


ACTIONS = {
    "factcheck": ("Fact-check the script again", job_factcheck),
    "voiceover": ("Generate the voiceover", job_voiceover),
    "visuals": ("Fetch visuals", job_visuals),
    "render": ("Render the video", job_render),
    "thumbnail": ("Update the thumbnail", job_thumbnail),
    "short": ("Make the Short", job_short),
}


def publish_time(value) -> str | None:
    if not value:
        return None
    try:
        when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("Publish time must look like 2026-11-05T15:00:00Z")
    if when.tzinfo is None or when <= datetime.now(timezone.utc):
        raise ValueError("Publish time must be in the future and include a time zone")
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@app.post("/api/projects/<slug>/jobs")
def api_start_job(slug):
    p = project_dir(slug)
    data = body()
    action = data.get("action")
    script = need_script(p)
    n = len(script["scenes"])

    def scene_index() -> int:
        i = data.get("scene")
        if not isinstance(i, int) or not 0 <= i < n:
            raise ValueError("Unknown scene")
        return i

    if action in ACTIONS:
        label, fn = ACTIONS[action]
        job = enqueue(slug, action, label, fn, slug=slug)
    elif action == "rewrite":
        minutes = float(data.get("minutes") or 6)
        job = enqueue(slug, action, "Rewrite the script from scratch", job_rewrite, slug=slug, minutes=minutes)
    elif action == "revoice":
        i = scene_index()
        job = enqueue(slug, action, f"Re-voice scene {i + 1}", job_revoice, slug=slug, scene=i)
    elif action == "find_visual":
        i = scene_index()
        job = enqueue(slug, action, f"Find another visual for scene {i + 1}", job_find_visual, slug=slug, scene=i)
    elif action == "pick_visual":
        i = scene_index()
        candidate = CANDIDATES.get(str(data.get("candidate", "")))
        if not candidate:   # only results this server found are used, never a URL sent by the browser
            raise ValueError("That search result has expired (the web UI may have restarted). Search again.")
        query = " ".join(str(data.get("query") or "").split())[:100]
        job = enqueue(slug, action, f"Use the picked visual for scene {i + 1}", job_pick_visual, slug=slug, scene=i,
                      candidate=dict(candidate), query=query)
    elif action == "preview":
        seconds = float(data.get("seconds") or 45)
        job = enqueue(slug, action, "Preview the sound", job_preview, slug=slug, seconds=min(max(seconds, 5), 300))
    elif action in ("upload", "upload_short"):
        privacy = data.get("privacy", "private")
        if privacy not in ("private", "unlisted", "public"):
            raise ValueError("Unknown privacy setting")
        label, fn = ("Upload to YouTube", job_upload) if action == "upload" else ("Upload the Short", job_upload_short)
        job = enqueue(slug, action, label, fn, slug=slug, privacy=privacy,
                      publish_at=publish_time(data.get("publish_at")), synthetic=bool(data.get("synthetic")))
    else:
        raise ValueError(f"Unknown action: {action}")
    return jsonify(job.to_dict())


@app.put("/api/projects/<slug>/short")
def api_project_short(slug):
    """Which scenes go into the Short (a continuous range) and its title."""
    p = project_dir(slug)
    require_idle(slug)
    script = need_script(p)
    data = body()
    first, last = data.get("first"), data.get("last")
    n = len(script["scenes"])
    if not isinstance(first, int) or not isinstance(last, int) or not 0 <= first <= last < n:
        raise ValueError("Pick a first and last scene, in order")
    title = " ".join(str(data.get("title") or "").split())
    if len(title) > 90:
        raise ValueError("The Short's title is longer than 90 characters")
    meta = pl.load_meta(p)
    meta["short"] = {"first": first, "last": last, "title": title}
    meta["short_changed"] = time.time()
    pl.save_meta(p, meta)
    return jsonify(project_state(slug, detail=True))


@app.put("/api/projects/<slug>/music")
def api_project_music(slug):
    """This video's music: a track name from the library, "none", or null for the default."""
    p = project_dir(slug)
    require_idle(slug)
    choice = body().get("music")
    meta = pl.load_meta(p)
    if choice is None:
        for key in ("music", "music_auto"):
            meta.pop(key, None)
    elif choice == "none" or choice in {t.name for t in pl.music_tracks()}:
        meta.update(music=choice, music_auto=False)
    else:
        raise ValueError("That track is not in the music folder")
    meta["music_changed"] = time.time()   # the video needs a (quick) re-mix
    pl.save_meta(p, meta)
    return jsonify(project_state(slug, detail=True))


CANDIDATES: "OrderedDict[str, dict]" = OrderedDict()   # search results by id, with their download URLs
_candidates_lock = threading.Lock()
PUBLIC_FIELDS = ("id", "kind", "provider", "source", "thumb", "preview", "credit", "duration", "width", "height", "title")


@app.get("/api/projects/<slug>/scenes/<int:scene>/candidates")
def api_candidates(slug, scene):
    """Search results for one scene's visual picker. Nothing is downloaded until one is picked."""
    p = project_dir(slug)
    script = need_script(p)
    if not 0 <= scene < len(script["scenes"]):
        abort(404, "Unknown scene")
    visual = script["scenes"][scene].get("visual") or {}
    source = request.args.get("source") or pl.SOURCE_ALIASES.get(visual.get("source"), visual.get("source"))
    if source not in SOURCES:
        raise ValueError("Unknown source")
    query = " ".join((request.args.get("q") or visual.get("query") or "").split())[:100]
    if not query:
        raise ValueError("Enter some search words")
    try:
        page = min(max(int(request.args.get("page", 1)), 1), 20)
    except ValueError:
        page = 1
    results, errors = pl.search_candidates(source, query, page)
    with _candidates_lock:
        for c in results:
            CANDIDATES[c["id"]] = c
            CANDIDATES.move_to_end(c["id"])
        while len(CANDIDATES) > 5000:
            CANDIDATES.popitem(last=False)
    used = {a["id"]: k for k, a in enumerate(pl.load_manifest(p)) if a}
    rejected = pl.rejected_assets(p)
    out = []
    for c in results:
        item = {k: c.get(k) for k in PUBLIC_FIELDS}
        k = used.get(c["id"])
        item.update(current=k == scene, used_in=None if k is None or k == scene else k + 1,
                    rejected=c["id"] in rejected)
        out.append(item)
    timeline = timeline_for(p, script)
    return jsonify({"query": query, "source": source, "page": page, "results": out, "errors": errors,
                    "scene_seconds": timeline[scene]["duration"] if scene < len(timeline) else None,
                    "stock_providers": pl.stock_providers()})


@app.post("/api/projects/<slug>/scenes/<int:scene>/visual")
def api_upload_visual(slug, scene):
    p = project_dir(slug)
    require_idle(slug)
    script = need_script(p)
    if not 0 <= scene < len(script["scenes"]):
        abort(404, "Unknown scene")
    f = request.files.get("file")
    if not f or not f.filename:
        raise ValueError("Choose an image or video file")
    suffix = Path(f.filename).suffix.lower()
    if suffix not in IMAGE_EXTS | VIDEO_EXTS:
        raise ValueError("Use a JPG, PNG or WebP image, or an MP4, MOV, WebM or MKV video")
    upload_dir = p / "uploads"
    upload_dir.mkdir(exist_ok=True)
    tmp = upload_dir / f"scene_{scene:02d}_{int(time.time())}{suffix}"
    f.save(tmp)
    credit = (request.form.get("credit") or "").strip()[:200]
    try:
        pl.set_custom_visual(p, scene, tmp, credit)
    except (OSError, RuntimeError, ValueError) as e:
        abort(400, f"Could not read that file as an image or video ({type(e).__name__})")
    finally:
        tmp.unlink(missing_ok=True)
    return jsonify(project_state(slug, detail=True))


# ----------------------------------------------------------------------------
# Routes: jobs
# ----------------------------------------------------------------------------
@app.get("/api/jobs")
def api_jobs():
    jobs = all_jobs()
    if request.args.get("active"):
        jobs = [j for j in jobs if j.status in ("queued", "running")]
    return jsonify([j.to_dict() for j in jobs[:50]])


@app.get("/api/jobs/<job_id>")
def api_job(job_id):
    job = JOBS.get(job_id)
    if not job:
        abort(404, "Unknown job (the web UI may have restarted)")
    return jsonify(job.to_dict(since=int(request.args.get("since", 0))))


# ----------------------------------------------------------------------------
# Routes: topic queue
# ----------------------------------------------------------------------------
def read_queue() -> tuple[list[str], list[str]]:
    if not TOPICS_PATH.exists():
        return [], []
    comments, topics = [], []
    for line in TOPICS_PATH.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s.startswith("#"):
            comments.append(line)
        elif s:
            topics.append(s)
    return comments, topics


def write_queue(comments: list[str], topics: list[str]) -> None:
    TOPICS_PATH.write_text("\n".join(comments + topics) + "\n", encoding="utf-8")


def read_log(path: Path, limit: int = 50) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            out.append({"date": parts[0], "topic": parts[1], "note": parts[2] if len(parts) > 2 else ""})
    return out[-limit:][::-1]


@app.get("/api/queue")
def api_queue():
    _, topics = read_queue()
    return jsonify({"topics": topics,
                    "done": read_log(TOPICS_PATH.with_name(f"{TOPICS_PATH.stem}_done.txt")),
                    "failed": read_log(TOPICS_PATH.with_name(f"{TOPICS_PATH.stem}_failed.txt")),
                    "existing": {t: pl.slugify(t) for t in topics if (pl.PROJECTS_DIR / pl.slugify(t)).exists()}})


@app.put("/api/queue")
def api_save_queue():
    topics = body().get("topics")
    if not isinstance(topics, list) or not all(isinstance(t, str) for t in topics):
        raise ValueError("Topics must be a list of text")
    comments, _ = read_queue()
    clean = []
    for t in topics:
        t = " ".join(t.split())
        if t and not t.startswith("#") and t not in clean:
            clean.append(t)
    write_queue(comments, clean)
    return api_queue()


@app.post("/api/queue/requeue")
def api_requeue():
    topic = str(body().get("topic", "")).strip()
    failed = TOPICS_PATH.with_name(f"{TOPICS_PATH.stem}_failed.txt")
    if failed.exists():
        lines = [l for l in failed.read_text(encoding="utf-8").splitlines()
                 if not (len(l.split("\t")) > 1 and l.split("\t")[1] == topic)]
        failed.write_text("".join(l + "\n" for l in lines), encoding="utf-8")
    comments, topics = read_queue()
    if topic and topic not in topics:
        write_queue(comments, topics + [topic])
    return api_queue()


# ----------------------------------------------------------------------------
# Routes: settings and status
# ----------------------------------------------------------------------------
SETTINGS = {   # .env name -> kind; only sound, voice, video and visual settings are editable here, never keys
    "MUSIC_PATH": "path", "MUSIC_VOLUME": "float", "MUSIC_DUCKING": "bool", "VOICE_TONE": "choice",
    "VOICE_STABILITY": "float", "VOICE_SIMILARITY": "float", "VOICE_STYLE": "float",
    "VOICE_SPEAKER_BOOST": "bool", "VOICE_SPEED": "float", "CROSSFADE": "float",
    "VISUAL_PROVIDERS": "providers", "ALLOW_SHARE_ALIKE": "bool",
}


def providers_status() -> list[dict]:
    """Every visual provider: on or off (in the configured order, then the ones that are off) and its key."""
    labels = {"stock_video": "stock video", "stock_photo": "stock photos", "archive": "archive & museums",
              "nasa": "NASA images"}
    order = [p for p in pl.VISUAL_PROVIDERS if p in pl.PROVIDERS] + [p for p in pl.PROVIDERS if p not in pl.VISUAL_PROVIDERS]
    return [{"id": pid, "name": pl.PROVIDERS[pid]["name"], "about": pl.PROVIDERS[pid]["about"],
             "kinds": [labels[k] for k in pl.PROVIDERS[pid]["kinds"]], "key": pl.PROVIDERS[pid]["key"],
             "key_set": not pl.PROVIDERS[pid]["key"] or bool(getattr(pl, pl.PROVIDERS[pid]["key"], "")),
             "enabled": pid in pl.VISUAL_PROVIDERS, "ready": pl.provider_ready(pid)} for pid in order]


def current_settings() -> dict:
    music = ""
    if pl.MUSIC_PATH:
        try:
            music = pl.MUSIC_PATH.relative_to(pl.SCRIPT_DIR).as_posix()
        except ValueError:
            music = str(pl.MUSIC_PATH)
    return {"MUSIC_PATH": music, "MUSIC_VOLUME": pl.MUSIC_VOLUME, "MUSIC_DUCKING": pl.MUSIC_DUCKING,
            "VOICE_TONE": pl.VOICE_TONE, "VOICE_STABILITY": pl.VOICE_STABILITY,
            "VOICE_SIMILARITY": pl.VOICE_SIMILARITY, "VOICE_STYLE": pl.VOICE_STYLE,
            "VOICE_SPEAKER_BOOST": pl.VOICE_SPEAKER_BOOST, "VOICE_SPEED": pl.VOICE_SPEED,
            "CROSSFADE": pl.CROSSFADE, "VISUAL_PROVIDERS": pl.VISUAL_PROVIDERS,
            "ALLOW_SHARE_ALIKE": pl.ALLOW_SHARE_ALIKE}


def write_env(updates: dict[str, str]) -> None:
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    remaining = dict(updates)
    for k, line in enumerate(lines):
        m = re.match(r"^([A-Z0-9_]+)=", line)
        if m and m.group(1) in remaining:
            lines[k] = f"{m.group(1)}={remaining.pop(m.group(1))}"
    if remaining:
        lines += ["", "# Set from the web UI"] + [f"{key}={value}" for key, value in remaining.items()]
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def rel_to_app(path: Path) -> str:
    try:
        return path.relative_to(pl.SCRIPT_DIR).as_posix()
    except ValueError:
        return str(path)


def music_info() -> dict:
    """The music library: its folder, its tracks and how many videos use each."""
    tracks = pl.music_tracks()
    used: dict[str, int] = {}
    if pl.PROJECTS_DIR.exists():
        for d in pl.PROJECTS_DIR.iterdir():
            choice = pl.load_meta(d).get("music") if d.is_dir() else None
            if choice:
                used[choice] = used.get(choice, 0) + 1
    lib = pl.music_library()
    return {"library": rel_to_app(lib), "default": pl.describe_default_music(),
            "tracks": [{"name": t.name, "path": rel_to_app(t), "url": f"/music/{t.name}", "videos": used.get(t.name, 0)}
                       for t in tracks]}


@app.get("/api/music")
def api_music():
    return jsonify(music_info())


@app.post("/api/music")
def api_add_music():
    f = request.files.get("file")
    if not f or not f.filename:
        raise ValueError("Choose an audio file")
    name = re.sub(r"[^A-Za-z0-9._ -]+", "", Path(f.filename).name).strip(" .") or "track"
    if Path(name).suffix.lower() not in AUDIO_EXTS:
        raise ValueError("Use an MP3, WAV, M4A, AAC, OGG or FLAC file")
    lib = pl.music_library()
    lib.mkdir(parents=True, exist_ok=True)
    dest = lib / name
    if dest.exists():
        abort(409, f"{name} is already in the music folder")
    tmp = lib / f".upload-{uuid.uuid4().hex}{dest.suffix}"
    f.save(tmp)
    try:
        if pl.media_duration(tmp) <= 0:
            raise ValueError
    except (RuntimeError, ValueError):
        tmp.unlink(missing_ok=True)
        abort(400, "Could not read that file as audio")
    tmp.replace(dest)
    return jsonify(music_info())


@app.get("/music/<name>")
def music_file(name):
    track = next((t for t in pl.music_tracks() if t.name == name), None)
    if not track:
        abort(404)
    return send_file(track, max_age=0, conditional=True)


_usage_cache: dict = {"at": 0.0, "data": None}


@app.get("/api/elevenlabs")
def api_elevenlabs():
    """Characters used and left on the ElevenLabs plan (cached for a minute)."""
    if request.args.get("refresh") or time.time() - _usage_cache["at"] > 60 or _usage_cache["data"] is None:
        _usage_cache.update(at=time.time(), data=pl.elevenlabs_usage())
    return jsonify(_usage_cache["data"])


@app.post("/api/stats/refresh")
def api_refresh_stats():
    running = next((j for j in all_jobs() if j.action == "stats" and j.status in ("queued", "running")), None)
    job = running or enqueue(None, "stats", "Refresh YouTube statistics", job_stats)
    return jsonify(job.to_dict())


@app.get("/api/status")
def api_status():
    try:
        ffmpeg = pl.run(["ffmpeg", "-version"]).splitlines()[0]
    except (RuntimeError, OSError):
        ffmpeg = ""
    music = music_info()
    music_files = [t["path"] for t in music["tracks"]]
    return jsonify({
        "keys": {"anthropic": bool(os.getenv("ANTHROPIC_API_KEY")), "elevenlabs": bool(pl.ELEVENLABS_API_KEY),
                 "voice_id": bool(pl.ELEVENLABS_VOICE_ID), "pexels": bool(pl.PEXELS_API_KEY),
                 "pixabay": bool(pl.PIXABAY_API_KEY), "youtube_client": pl.YT_CLIENT_SECRETS.exists(),
                 "youtube_login": pl.YT_TOKEN.exists()},
        "models": {"writer": pl.ANTHROPIC_MODEL, "factcheck": pl.FACTCHECK_MODEL, "voice": pl.ELEVENLABS_MODEL},
        "ffmpeg": ffmpeg, "xfade": pl.ffmpeg_has_filter("xfade"),
        "settings": current_settings(), "music_files": music_files, "music_library": music["library"],
        "voice_tones": list(pl.VOICE_TONES),
        "problems": pl.settings_problems(), "projects_dir": str(pl.PROJECTS_DIR),
        "stock_providers": pl.stock_providers(), "providers": providers_status(),
    })


@app.put("/api/settings")
def api_save_settings():
    data = body()
    updates = {}
    for key, value in data.items():
        kind = SETTINGS.get(key)
        if kind is None:
            raise ValueError(f"{key} cannot be changed here")
        if kind == "bool":
            updates[key] = "true" if value else "false"
        elif kind == "float":
            try:
                updates[key] = f"{float(value):g}"
            except (TypeError, ValueError):
                raise ValueError(f"{key} must be a number")
        elif kind == "choice":
            updates[key] = str(value).strip().lower()
        elif kind == "providers":
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ValueError(f"{key} must be a list of provider names")
            names = list(dict.fromkeys(v.strip().lower() for v in value if v.strip()))
            unknown = [v for v in names if v not in pl.PROVIDERS]
            if unknown:
                raise ValueError(f"Unknown visual provider(s): {', '.join(unknown)}")
            updates[key] = ",".join(names) or "none"
        else:   # path: a file or folder inside the MyVideoGen folder, or empty for none
            value = str(value or "").strip().rstrip("/")
            if value:
                path = (pl.SCRIPT_DIR / value).resolve()
                if pl.SCRIPT_DIR.resolve() not in path.parents or not path.exists():
                    raise ValueError(f"{value} is not a file or folder in the MyVideoGen folder")
            updates[key] = value
    saved = {k: os.environ.get(k) for k in updates}
    os.environ.update(updates)
    pl.configure()
    problems = pl.settings_problems()
    if problems:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        pl.configure()
        raise ValueError("; ".join(problems))
    write_env(updates)
    return api_status()


def main() -> None:
    global TOPICS_PATH
    parser = argparse.ArgumentParser(description="MyVideoGen web interface")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--topics-file", type=Path, default=TOPICS_PATH, help="Topic queue file (default topics.txt)")
    args = parser.parse_args()
    TOPICS_PATH = args.topics_file.resolve()
    pl.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=worker, daemon=True, name="jobs").start()
    for problem in pl.settings_problems():
        print(f"Setting problem: {problem}")
    print(f"MyVideoGen web UI: http://localhost:{args.port}  (Ctrl+C to stop)")
    app.run(host="127.0.0.1", port=args.port, threaded=True, debug=False)


if __name__ == "__main__":
    main()
