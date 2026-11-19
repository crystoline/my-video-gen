"""The web interface's JSON API, with every paid step faked."""
from __future__ import annotations

import io
import time

import pipeline as pl
import webui as wu
from helpers import H, job, new_project, png_bytes, project, run_job, wait


def test_only_localhost_and_changes_need_the_custom_header(client):
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 403
    assert client.get("/", headers={"Host": "127.0.0.1:8765"}).status_code == 200
    assert client.get("/", headers={"Host": "[::1]:8765"}).status_code == 200
    assert client.post("/api/projects", json={"topic": "x"}).status_code == 403
    assert client.put("/api/settings", json={"VOICE_TONE": "warm"}).status_code == 403
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/../webui.py").status_code == 404


def test_creating_from_the_queue_writes_and_fact_checks_and_removes_the_topic(client, fakes):
    r = client.post("/api/projects", json={"topic": "Topic A", "minutes": 3, "from_queue": True}, headers=H)
    assert r.status_code == 200, r.get_json()
    slug = r.get_json()["slug"]
    assert slug == "topic-a"
    wait(client, r.get_json()["job"]["id"])
    p = project(client, slug)
    assert p["step"] == "review" and p["scenes"] == 4 and len(p["factcheck"]["issues"]) == 1
    assert p["factcheck_notes"] == "Full notes" and p["voice_todo"] == [0, 1, 2, 3]
    assert wu.TOPICS_PATH.read_text(encoding="utf-8") == "# queue comment\nTopic B\n"
    assert client.post("/api/projects", json={"topic": "Topic A"}, headers=H).status_code == 409
    assert client.post("/api/projects", json={"topic": "  "}, headers=H).status_code == 400
    assert (pl.PROJECTS_DIR / slug / "review.md").exists()


def test_moving_adding_and_deleting_scenes_keeps_files_with_their_scenes(client, fakes):
    slug, P = new_project(client)
    for d in ("audio", "assets", "segments"):
        (P / d).mkdir()
    script = project(client, slug)["script"]
    manifest = []
    for k, s in enumerate(script["scenes"]):
        f = pl.audio_files(P, k)
        f["mp3"].write_bytes(f"audio {k}".encode())
        f["txt"].write_text(s["narration"], encoding="utf-8")
        f["json"].write_text("{}", encoding="utf-8")
        f["wav"].write_bytes(f"wav {k}".encode())
        (P / "assets" / f"scene_{k:02d}.jpg").write_bytes(f"img {k}".encode())
        (P / "segments" / f"seg_{k:02d}.mp4").write_bytes(f"seg {k}".encode())
        manifest.append({"kind": "image", "id": f"orig:{k}", "credit": f"credit {k}", "source": "nasa", "query": "q",
                         "url": "", "path": f"/somewhere/else/assets/scene_{k:02d}.jpg"})   # stale absolute path
    pl.save_json(P / "assets.json", manifest)
    assert project(client, slug)["visuals_ready"] == 4, "asset paths must be re-anchored to the project folder"

    old = script["scenes"]
    new_scenes = [old[0], old[3], {"narration": "A brand new scene.", "visual": {"source": "stock_video", "query": "ocean"}},
                  old[1]]
    url = f"/api/projects/{slug}/script"
    assert client.put(url, json={"script": dict(script, scenes=new_scenes), "origin": [0, 3, 3, 1]},
                      headers=H).status_code == 400
    bad = client.put(url, json={"script": dict(script, scenes=[dict(old[0], narration=" ")])}, headers=H)
    assert bad.status_code == 400 and "cannot be empty" in bad.get_json()["error"]
    r = client.put(url, json={"script": dict(script, scenes=new_scenes, thumbnail_scene=1), "origin": [0, 3, None, 1]},
                   headers=H)
    assert r.status_code == 200, r.get_json()
    p = r.get_json()
    assert [pl.audio_files(P, k)["mp3"].read_bytes() for k in (0, 1, 3)] == [b"audio 0", b"audio 3", b"audio 1"]
    assert not pl.audio_files(P, 2)["mp3"].exists()
    assert [(P / "assets" / f"scene_{k:02d}.jpg").read_bytes() for k in (0, 1, 3)] == [b"img 0", b"img 3", b"img 1"]
    assert [a and a["id"] for a in pl.load_json(P / "assets.json")] == ["orig:0", "orig:3", None, "orig:1"]
    assert (P / "segments" / "seg_00.mp4").exists() and not (P / "segments" / "seg_01.mp4").exists()
    assert not (P / "segments" / "seg_03.mp4").exists()
    trash = list((P / ".trash").glob("*/scene_02"))
    assert trash and (trash[0] / "audio.mp3").read_bytes() == b"audio 2" and (trash[0] / "asset.jpg").exists()
    assert pl.load_json(P / "factcheck.json")["issues"][0]["scene"] is None   # its scene was deleted
    assert p["voice_todo"] == [2] and p["step"] == "voiceover" and p["script"]["thumbnail_scene"] == 1
    assert [s["audio"]["state"] for s in p["scene_info"]] == ["current", "current", "missing", "current"]


def test_find_another_skips_swapped_out_visuals_and_own_uploads_are_checked(client, fakes):
    slug, P = new_project(client)
    run_job(client, slug, action="visuals")
    first = pl.load_json(P / "assets.json")[1]["id"]
    run_job(client, slug, action="find_visual", scene=1)
    m = pl.load_json(P / "assets.json")
    assert m[1]["id"] != first and (P / "assets" / "scene_01.png").exists()
    assert first in pl.load_json(P / "rejected_assets.json")
    second = m[1]["id"]
    run_job(client, slug, action="find_visual", scene=1)
    assert set(pl.load_json(P / "rejected_assets.json")) == {first, second}

    url = f"/api/projects/{slug}/scenes/3/visual"
    r = client.post(url, headers=H, content_type="multipart/form-data",
                    data={"file": (io.BytesIO(png_bytes((200, 30, 30))), "mine.png"), "credit": "Photo by me"})
    assert r.status_code == 200, r.get_json()
    info = r.get_json()["scene_info"][3]["asset"]
    assert info["source"] == "custom" and info["credit"] == "Photo by me"
    assert info["url"].startswith(f"/files/{slug}/assets/scene_03.png")
    r = client.post(url, headers=H, content_type="multipart/form-data", data={"file": (io.BytesIO(b"x"), "evil.exe")})
    assert r.status_code == 400
    r = client.post(url, headers=H, content_type="multipart/form-data",
                    data={"file": (io.BytesIO(b"not an image"), "x.png")})
    assert r.status_code == 400


def test_the_visual_picker_shows_results_and_uses_only_what_the_server_found(client, fakes, monkeypatch):
    slug, P = new_project(client)
    run_job(client, slug, action="visuals")
    ids = [a["id"] for a in pl.load_json(P / "assets.json")]
    searches = []

    def fake_search(source, query, page=1):
        searches.append((source, query, page))
        return [{"kind": "video", "url": "https://cdn.example/clip.mp4", "id": "pixabay-video:42", "credit": "Video by Ann",
                 "thumb": "https://cdn.example/clip.jpg", "preview": "https://cdn.example/tiny.mp4", "duration": 3,
                 "provider": "Pixabay", "source": "stock_video"},
                {"kind": "image", "url": "https://cdn.example/a.jpg", "id": ids[0], "credit": "Already in scene 1",
                 "provider": "Pexels", "source": "stock_photo"},
                {"kind": "image", "url": "https://cdn.example/b.jpg", "id": ids[1], "credit": "Current one",
                 "provider": "Pexels", "source": "stock_photo"}], ["Pexels search failed: timeout"]

    monkeypatch.setattr(pl, "search_candidates", fake_search)
    r = client.get(f"/api/projects/{slug}/scenes/1/candidates?q=molten%20lava&source=stock_video&page=2")
    assert r.status_code == 200, r.get_json()
    data = r.get_json()
    assert searches[-1] == ("stock_video", "molten lava", 2) and data["errors"] == ["Pexels search failed: timeout"]
    first, other, current = data["results"]
    assert "url" not in first and first["preview"] == "https://cdn.example/tiny.mp4" and not first["current"]
    assert other["used_in"] == 1 and not other["current"] and current["current"] and current["used_in"] is None
    assert data["scene_seconds"] > 0
    assert client.get(f"/api/projects/{slug}/scenes/1/candidates?source=bogus").status_code == 400
    assert client.get(f"/api/projects/{slug}/scenes/9/candidates").status_code == 404

    # A pick must name a result the server found; a URL from the browser is never used
    url = f"/api/projects/{slug}/jobs"
    assert client.post(url, json={"action": "pick_visual", "scene": 1, "candidate": "pixabay-video:999"},
                       headers=H).status_code == 400
    downloaded = []
    monkeypatch.setattr(pl, "download", lambda u, dest: downloaded.append(u) or dest.write_bytes(
        png_bytes() if u.endswith(".jpg") else b"clip"))
    run_job(client, slug, action="pick_visual", scene=1, candidate="pixabay-video:42", query="molten lava",
            url="https://evil.example/x.mp4")
    assert downloaded == ["https://cdn.example/clip.mp4"]
    m = pl.load_json(P / "assets.json")
    assert m[1]["id"] == "pixabay-video:42" and m[1]["kind"] == "video" and m[1]["source"] == "stock_video"
    assert (P / "assets" / "scene_01.mp4").exists() and not list((P / "assets").glob("scene_01.png"))
    assert ids[1] in pl.load_json(P / "rejected_assets.json")
    assert pl.load_json(P / "script.json")["scenes"][1]["visual"] == {"source": "stock_video", "query": "molten lava"}

    # Picking a result you skipped before un-skips it
    client.get(f"/api/projects/{slug}/scenes/1/candidates?q=x&source=stock_photo")
    run_job(client, slug, action="pick_visual", scene=1, candidate=ids[1], query="")
    assert pl.load_json(P / "assets.json")[1]["id"] == ids[1]
    assert ids[1] not in pl.load_json(P / "rejected_assets.json")
    assert "pixabay-video:42" in pl.load_json(P / "rejected_assets.json")


def test_project_files_stream_with_ranges_and_cannot_escape_the_folder(client, fakes):
    slug, P = new_project(client)
    (P / "assets").mkdir()
    data = png_bytes((200, 30, 30))
    (P / "assets" / "scene_03.png").write_bytes(data)
    r = client.get(f"/files/{slug}/assets/scene_03.png", headers={"Range": "bytes=0-7"})
    assert r.status_code == 206 and r.data == data[:8]
    assert client.get(f"/files/{slug}/../../.env").status_code == 404
    assert client.get(f"/files/{slug}/%2e%2e/%2e%2e/.env").status_code == 404
    assert client.get("/files/..%2f/x", follow_redirects=True).status_code == 404
    assert client.get("/api/projects/NOT_A_SLUG").status_code == 404


def test_project_files_are_kept_by_the_browser_only_under_their_current_version(client, fakes):
    slug, P = new_project(client)
    run_job(client, slug, action="visuals")
    scene_url = lambda: client.get(f"/api/projects/{slug}").get_json()["scene_info"][2]["asset"]["url"]
    url = scene_url()
    r = client.get(url)
    assert r.status_code == 200
    assert "immutable" in r.headers["Cache-Control"] and "max-age=31536000" in r.headers["Cache-Control"]
    # Without a version, or with one that is not the file's, the browser has to ask again
    bare = url.split("?")[0]
    for other in (bare, bare + "?v=1-2"):
        cc = client.get(other).headers["Cache-Control"]
        assert "immutable" not in cc and ("no-cache" in cc or "max-age=0" in cc)
    # A replaced file gets a new URL, and the old URL is no longer kept
    path = P / bare.split(f"/files/{slug}/", 1)[1]
    path.write_bytes(path.read_bytes() + b"\0" * 16)
    assert scene_url() != url
    assert "immutable" not in client.get(url).headers["Cache-Control"]


def test_a_video_cannot_be_edited_or_get_a_second_job_while_one_runs(client, fakes):
    slug, P = new_project(client)
    fakes.factcheck_delay = 1.5
    jid = job(client, slug, action="factcheck")
    time.sleep(0.3)
    assert client.put(f"/api/projects/{slug}/script", json={"script": project(client, slug)["script"]},
                      headers=H).status_code == 409
    assert client.post(f"/api/projects/{slug}/jobs", json={"action": "visuals"}, headers=H).status_code == 409
    wait(client, jid)


def test_the_topic_queue_can_be_reordered_cleaned_and_failed_topics_requeued(client):
    q = client.get("/api/queue").get_json()
    assert q["topics"] == ["Topic A", "Topic B"]
    q = client.put("/api/queue", json={"topics": ["Topic C", "  Topic   B ", "Topic C", "", "# not a topic"]},
                   headers=H).get_json()
    assert q["topics"] == ["Topic C", "Topic B"]
    assert wu.TOPICS_PATH.read_text(encoding="utf-8").startswith("# queue comment\n")
    wu.TOPICS_PATH.with_name("topics_failed.txt").write_text("2026-11-01\tTopic Z\tRuntimeError: boom\n", encoding="utf-8")
    q = client.get("/api/queue").get_json()
    assert q["failed"] == [{"date": "2026-11-01", "topic": "Topic Z", "note": "RuntimeError: boom"}]
    q = client.post("/api/queue/requeue", json={"topic": "Topic Z"}, headers=H).get_json()
    assert q["topics"][-1] == "Topic Z" and q["failed"] == []


def test_sound_settings_are_validated_saved_in_place_and_applied(client):
    r = client.put("/api/settings", json={"MUSIC_VOLUME": 0.07, "VOICE_TONE": "warm", "MUSIC_DUCKING": False,
                                          "VOICE_SPEED": 0.95}, headers=H)
    assert r.status_code == 200, r.get_json()
    env = wu.ENV_PATH.read_text(encoding="utf-8")
    assert "\nMUSIC_VOLUME=0.07\n" in env and "\nVOICE_TONE=warm\n" in env
    assert "MUSIC_DUCKING=false" in env and "VOICE_SPEED=0.95" in env
    assert env.count("MUSIC_VOLUME=") == 1 and "# Optional background music" in env
    assert pl.VOICE_TONE == "warm" and pl.MUSIC_VOLUME == 0.07 and pl.MUSIC_DUCKING is False and pl.VOICE_SPEED == 0.95
    for bad in ({"VOICE_TONE": "loud"}, {"VOICE_SPEED": 2}, {"MUSIC_VOLUME": 0.9}, {"ANTHROPIC_API_KEY": "x"},
                {"MUSIC_PATH": "../../../etc/passwd"}, {"MUSIC_PATH": "music/does-not-exist.mp3"}):
        r = client.put("/api/settings", json=bad, headers=H)
        assert r.status_code == 400, (bad, r.get_json())
    assert pl.VOICE_TONE == "warm" and pl.VOICE_SPEED == 0.95
    assert "VOICE_TONE=loud" not in wu.ENV_PATH.read_text(encoding="utf-8")
    st = client.get("/api/status").get_json()
    assert st["settings"]["VOICE_TONE"] == "warm" and st["voice_tones"] == ["natural", "soft", "warm"]


def test_elevenlabs_requests_carry_every_voice_setting(monkeypatch, tmp_path):
    sent = {}

    class Resp:
        status_code = 200

        def json(self):
            return {"audio_base64": "", "alignment": None}

    monkeypatch.setattr(pl.HTTP, "post", lambda url, **kw: sent.update(kw["json"]) or Resp())
    monkeypatch.setattr(pl, "VOICE_STABILITY", 0.65)
    monkeypatch.setattr(pl, "VOICE_SPEAKER_BOOST", False)
    pl.tts("Hello", tmp_path / "x.mp3", tmp_path / "x.json")
    assert sent["voice_settings"] == {"stability": 0.65, "similarity_boost": 0.75, "style": 0.0,
                                      "use_speaker_boost": False, "speed": 1.0}


def test_upload_refuses_an_unrendered_video_and_bad_requests(client, fakes):
    slug, P = new_project(client)
    j = run_job(client, slug, want="failed", action="upload", privacy="unlisted")
    assert "Render it first" in j["error"] and not fakes.uploads
    url = f"/api/projects/{slug}/jobs"
    assert client.post(url, json={"action": "upload", "publish_at": "2020-01-01T00:00:00Z"}, headers=H).status_code == 400
    assert client.post(url, json={"action": "nope"}, headers=H).status_code == 400
    assert client.post(url, json={"action": "revoice", "scene": 99}, headers=H).status_code == 400


def test_the_page_and_project_list_load_and_jobs_are_logged(client, fakes):
    slug, P = new_project(client, "Listed topic")
    assert b"/static/app.js" in client.get("/").data
    listed = {x["slug"]: x for x in client.get("/api/projects").get_json()}
    assert listed[slug]["title"] == "Title for Listed topic"
    assert "Write and fact-check the script: done" in (P / "activity.log").read_text(encoding="utf-8")
