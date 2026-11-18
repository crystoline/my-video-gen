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
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from flask import Flask, abort, jsonify, request, send_file
from werkzeug.exceptions import HTTPException

import pipeline as pl

STATIC_DIR = pl.SCRIPT_DIR / "webui"
ENV_PATH = pl.SCRIPT_DIR / ".env"
TOPICS_PATH = pl.SCRIPT_DIR / "topics.txt"
MUSIC_DIR = pl.SCRIPT_DIR / "music"
ALLOWED_HOSTS = {"localhost", "127.0.0.1", "::1"}
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,80}$")
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv"}
SOURCES = ("nasa", "stock_video", "stock_photo")

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
    meta = pl.load_json(p / "project.json") if (p / "project.json").exists() else {}
    job = busy(slug)
    scenes = script["scenes"] if script else []
    n = len(scenes)
    todo = pl.voiceover_todo(p, script) if script else []
    manifest = pl.load_manifest(p)
    have_asset = [i < len(manifest) and bool(manifest[i]) and Path(manifest[i]["path"]).exists() for i in range(n)]
    final = p / "final.mp4"
    newest_input = max([mtime(f) for f in (p / "audio").glob("scene_*.mp3")]
                       + [mtime(f) for f in (p / "assets").glob("scene_*")] + [0.0])
    if not final.exists():
        video = "missing"
    elif mtime(final) < newest_input or todo or not all(have_asset):
        video = "outdated"
    else:
        video = "ready"
    youtube = pl.load_json(p / "youtube.json") if (p / "youtube.json").exists() else None
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
        "step": step, "scenes": n, "words": words, "chars": chars,
        "minutes": round(words / pl.WORDS_PER_MINUTE, 1),
        "voice_todo": todo, "voice_todo_chars": todo_chars,
        "visuals_ready": sum(have_asset), "video": video, "approved": approved,
        "thumbnail_url": file_url(slug, thumb),
        "youtube": youtube,
        "updated": max([mtime(f) for f in p.iterdir()] + [mtime(p)]) if p.exists() else 0,
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
    state.update({
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
                      ".approved", "preview_mix.mp3"])
    pl.save_json(p / "script.json", script)
    run_factcheck(p, script)


def job_voiceover(slug: str) -> None:
    p = pl.PROJECTS_DIR / slug
    script = need_script(p)
    (p / ".approved").touch()   # the command line will not pause for review on this project again
    pl.make_voiceover(p, script)


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
    pl.log(f"Sound preview ready ({seconds:g} s, voice tone {pl.VOICE_TONE}, {pl.describe_music()})")


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
}


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
    elif action == "preview":
        seconds = float(data.get("seconds") or 45)
        job = enqueue(slug, action, "Preview the sound", job_preview, slug=slug, seconds=min(max(seconds, 5), 300))
    elif action == "upload":
        privacy = data.get("privacy", "private")
        if privacy not in ("private", "unlisted", "public"):
            raise ValueError("Unknown privacy setting")
        publish_at = data.get("publish_at") or None
        if publish_at:
            try:
                when = datetime.fromisoformat(publish_at.replace("Z", "+00:00"))
            except ValueError:
                raise ValueError("Publish time must look like 2026-11-05T15:00:00Z")
            if when.tzinfo is None or when <= datetime.now(timezone.utc):
                raise ValueError("Publish time must be in the future and include a time zone")
            publish_at = when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        job = enqueue(slug, action, "Upload to YouTube", job_upload, slug=slug, privacy=privacy,
                      publish_at=publish_at, synthetic=bool(data.get("synthetic")))
    else:
        raise ValueError(f"Unknown action: {action}")
    return jsonify(job.to_dict())


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
SETTINGS = {   # .env name -> kind; only sound, voice and video settings are editable here, never keys
    "MUSIC_PATH": "path", "MUSIC_VOLUME": "float", "MUSIC_DUCKING": "bool", "VOICE_TONE": "choice",
    "VOICE_STABILITY": "float", "VOICE_SIMILARITY": "float", "VOICE_STYLE": "float",
    "VOICE_SPEAKER_BOOST": "bool", "VOICE_SPEED": "float", "CROSSFADE": "float",
}


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
            "CROSSFADE": pl.CROSSFADE}


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


@app.get("/api/status")
def api_status():
    try:
        ffmpeg = pl.run(["ffmpeg", "-version"]).splitlines()[0]
    except (RuntimeError, OSError):
        ffmpeg = ""
    music_files = sorted(f.relative_to(pl.SCRIPT_DIR).as_posix() for f in MUSIC_DIR.glob("*")
                         if f.suffix.lower() in AUDIO_EXTS) if MUSIC_DIR.exists() else []
    return jsonify({
        "keys": {"anthropic": bool(os.getenv("ANTHROPIC_API_KEY")), "elevenlabs": bool(pl.ELEVENLABS_API_KEY),
                 "voice_id": bool(pl.ELEVENLABS_VOICE_ID), "pexels": bool(pl.PEXELS_API_KEY),
                 "pixabay": bool(pl.PIXABAY_API_KEY), "youtube_client": pl.YT_CLIENT_SECRETS.exists(),
                 "youtube_login": pl.YT_TOKEN.exists()},
        "models": {"writer": pl.ANTHROPIC_MODEL, "factcheck": pl.FACTCHECK_MODEL, "voice": pl.ELEVENLABS_MODEL},
        "ffmpeg": ffmpeg, "xfade": pl.ffmpeg_has_filter("xfade"),
        "settings": current_settings(), "music_files": music_files, "voice_tones": list(pl.VOICE_TONES),
        "problems": pl.settings_problems(), "projects_dir": str(pl.PROJECTS_DIR),
        "stock_providers": pl.stock_providers(),
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
        else:   # path: must be a file inside the project folder, or empty for none
            value = str(value or "").strip()
            if value:
                path = (pl.SCRIPT_DIR / value).resolve()
                if pl.SCRIPT_DIR.resolve() not in path.parents or not path.is_file():
                    raise ValueError(f"{value} is not a file in the MyVideoGen folder")
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
