"""The music library: one track for everything, a different track per video, or each video's own choice."""
from __future__ import annotations

import io
import subprocess

import pytest

import pipeline as pl
import webui as wu
from helpers import H, needs_ffmpeg, new_project, project


@pytest.fixture()
def library(tmp_path, monkeypatch):
    """A music folder with three tracks (plus a file that is not music) and an empty projects folder."""
    lib = tmp_path / "music"
    lib.mkdir()
    for name in ("b-track.mp3", "a-track.mp3", "c-track.wav", "notes.txt"):
        (lib / name).write_bytes(b"x")
    monkeypatch.setattr(pl, "PROJECTS_DIR", tmp_path / "projects")
    return lib


def make_project(name: str):
    p = pl.PROJECTS_DIR / name
    p.mkdir(parents=True)
    return p


def test_tracks_and_the_default_follow_music_path(library, monkeypatch):
    p = make_project("one")
    monkeypatch.setattr(pl, "MUSIC_PATH", None)
    assert pl.project_music(p) is None and pl.describe_default_music() == "no music"
    monkeypatch.setattr(pl, "MUSIC_PATH", library / "a-track.mp3")
    assert pl.music_library() == library
    assert [t.name for t in pl.music_tracks()] == ["a-track.mp3", "b-track.mp3", "c-track.wav"]
    assert pl.project_music(p) == library / "a-track.mp3" and pl.describe_default_music() == "a-track.mp3"


def test_a_video_can_choose_its_own_track_or_none(library, monkeypatch):
    p = make_project("one")
    monkeypatch.setattr(pl, "MUSIC_PATH", library / "a-track.mp3")
    pl.save_meta(p, {"music": "b-track.mp3"})
    assert pl.project_music(p) == library / "b-track.mp3"
    pl.save_meta(p, {"music": "none"})
    assert pl.project_music(p) is None
    pl.save_meta(p, {"music": "deleted.mp3"})   # a track that was removed falls back to the default
    assert pl.project_music(p) == library / "a-track.mp3"


def test_a_music_folder_gives_each_video_the_least_used_track_and_it_keeps_it(library, monkeypatch):
    monkeypatch.setattr(pl, "MUSIC_PATH", library)
    assert pl.describe_default_music().startswith("a different track for each video")
    projects = [make_project(f"video-{k}") for k in range(4)]
    picks = [pl.project_music(p).name for p in projects]
    assert sorted(picks[:3]) == ["a-track.mp3", "b-track.mp3", "c-track.wav"], picks   # all used before any repeats
    assert all(pl.load_meta(p)["music_auto"] for p in projects)
    assert [pl.project_music(p).name for p in projects] == picks   # re-renders keep their track
    # A prediction (save=False) does not record anything
    fresh = make_project("not-yet")
    assert pl.project_music(fresh, save=False) and "music" not in pl.load_meta(fresh)
    # An automatic pick only applies while MUSIC_PATH is a folder
    monkeypatch.setattr(pl, "MUSIC_PATH", library / "c-track.wav")
    assert {pl.project_music(p).name for p in projects} == {"c-track.wav"}


def test_preflight_rejects_an_empty_music_folder(library, monkeypatch, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(pl, "MUSIC_PATH", empty)
    assert any("no music files" in problem for problem in pl.settings_problems())
    monkeypatch.setattr(pl, "MUSIC_PATH", library)
    assert pl.settings_problems() == []


def test_choosing_music_per_video_through_the_api(client, fakes, library, monkeypatch):
    monkeypatch.setattr(pl, "MUSIC_PATH", library / "a-track.mp3")
    slug, P = new_project(client)
    url = f"/api/projects/{slug}/music"
    assert project(client, slug)["music"] == {"choice": None, "auto": False, "track": "a-track.mp3",
                                              "default": "a-track.mp3"}
    r = client.put(url, json={"music": "c-track.wav"}, headers=H)
    assert r.status_code == 200 and r.get_json()["music"]["track"] == "c-track.wav"
    assert pl.load_meta(P)["music"] == "c-track.wav" and pl.load_meta(P)["music_changed"] > 0
    assert client.put(url, json={"music": "../../.env"}, headers=H).status_code == 400
    assert client.put(url, json={"music": "none"}, headers=H).get_json()["music"]["track"] is None
    r = client.put(url, json={"music": None}, headers=H)
    assert r.get_json()["music"]["track"] == "a-track.mp3" and "music" not in pl.load_meta(P)
    lib = client.get("/api/music").get_json()
    assert [t["name"] for t in lib["tracks"]] == ["a-track.mp3", "b-track.mp3", "c-track.wav"]
    assert client.get("/music/a-track.mp3").status_code == 200
    assert client.get("/music/notes.txt").status_code == 404 and client.get("/music/..%2f.env").status_code == 404


def test_settings_accept_a_music_folder_inside_the_app_folder(client, library, monkeypatch, tmp_path):
    monkeypatch.setattr(pl, "SCRIPT_DIR", tmp_path)
    r = client.put("/api/settings", json={"MUSIC_PATH": "music"}, headers=H)
    assert r.status_code == 200, r.get_json()
    assert pl.MUSIC_PATH == library and "MUSIC_PATH=music\n" in wu.ENV_PATH.read_text(encoding="utf-8")
    (tmp_path / "empty").mkdir()
    assert client.put("/api/settings", json={"MUSIC_PATH": "empty"}, headers=H).status_code == 400
    assert pl.MUSIC_PATH == library


@needs_ffmpeg
def test_adding_a_track_checks_that_it_is_audio(client, library, monkeypatch, tmp_path):
    monkeypatch.setattr(pl, "MUSIC_PATH", library)
    tone = tmp_path / "tone.mp3"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=duration=1", str(tone)], check=True)

    def add(data, name):
        return client.post("/api/music", headers=H, content_type="multipart/form-data",
                           data={"file": (io.BytesIO(data), name)})

    r = add(tone.read_bytes(), "My Song (2026).mp3")
    assert r.status_code == 200, r.get_json()
    assert (library / "My Song 2026.mp3").exists()
    assert add(tone.read_bytes(), "My Song (2026).mp3").status_code == 409
    assert add(b"not audio", "fake.mp3").status_code == 400 and not (library / "fake.mp3").exists()
    assert add(b"x", "script.sh").status_code == 400
    assert not list(library.glob(".upload-*"))
