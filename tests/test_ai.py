"""AI-generated visuals through fal.ai, with fal.ai faked (its queue protocol: submit, poll status, fetch result)."""
from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

import pipeline as pl
from helpers import H, needs_ffmpeg, new_project, png_bytes, project, run_job


class Resp:
    def __init__(self, data, status=200, text=""):
        self.data, self.status_code, self.text = data, status, text

    def json(self):
        return self.data


@pytest.fixture()
def fal(monkeypatch):
    """A fake fal.ai queue: the submission, two status polls and the result."""
    monkeypatch.setattr(pl, "FAL_KEY", "fal-secret")
    monkeypatch.setattr(pl, "AI_VISUALS", "images")
    monkeypatch.setattr(pl.time, "sleep", lambda s: None)
    calls = {"posts": [], "gets": []}
    state = {"result": {"images": [{"url": "https://v3.fal.media/files/a.jpg"}, {"url": "https://v3.fal.media/files/b.png"}]},
             "statuses": [{"status": "IN_QUEUE", "queue_position": 2}, {"status": "COMPLETED"}]}

    def post(url, headers=None, json=None, timeout=None):
        calls["posts"].append((url, headers, json))
        return Resp({"request_id": "r1", "status_url": "https://queue.fal.run/fal-ai/flux/requests/r1/status",
                     "response_url": "https://queue.fal.run/fal-ai/flux/requests/r1"})

    def get(url, headers=None, timeout=None, **kw):
        calls["gets"].append((url, headers))
        if url.endswith("/status"):
            return Resp(state["statuses"].pop(0))
        return Resp(state["result"])

    monkeypatch.setattr(pl.requests, "post", post)
    monkeypatch.setattr(pl.HTTP, "get", get)
    monkeypatch.setattr(pl, "download", lambda url, dest: dest.write_bytes(png_bytes()))
    return SimpleNamespace(calls=calls, state=state)


def test_fal_queue_protocol(fal):
    result = pl.fal_run("fal-ai/flux/dev", {"prompt": "x"})
    assert result == fal.state["result"]
    url, headers, body = fal.calls["posts"][0]
    assert url == "https://queue.fal.run/fal-ai/flux/dev" and headers["Authorization"] == "Key fal-secret"
    assert body == {"prompt": "x"} and len(fal.calls["posts"]) == 1   # submitted exactly once
    assert [u for u, _ in fal.calls["gets"]] == ["https://queue.fal.run/fal-ai/flux/requests/r1/status"] * 2 + [
        "https://queue.fal.run/fal-ai/flux/requests/r1"]
    fal.state["statuses"] = [{"status": "COMPLETED", "error": "NSFW content detected"}]
    with pytest.raises(RuntimeError, match="NSFW"):
        pl.fal_run("fal-ai/flux/dev", {"prompt": "x"})


def test_generated_images_are_kept_in_the_project(fal, tmp_path):
    items = pl.generate_ai_visual(tmp_path, "a lava river at night", "image", 2)
    assert [i["kind"] for i in items] == ["image", "image"] and items[0]["id"].startswith("ai:")
    body = fal.calls["posts"][0][2]
    assert body == {"prompt": "a lava river at night", "image_size": "landscape_16_9", "num_images": 2}
    assert all((tmp_path / "ai" / i["file"]).exists() for i in items) and items[1]["file"].endswith(".png")
    assert pl.ai_items(tmp_path) == items and "fal.ai" in items[0]["credit"]
    with pytest.raises(RuntimeError, match="video clips are switched off"):
        pl.generate_ai_visual(tmp_path, "x", "video")


@needs_ffmpeg
def test_video_clips_when_switched_on(fal, tmp_path, monkeypatch):
    monkeypatch.setattr(pl, "AI_VISUALS", "images_and_video")
    clip = tmp_path / "clip.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=duration=2:size=640x360:rate=24",
                    "-pix_fmt", "yuv420p", str(clip)], check=True)
    fal.state["result"] = {"video": {"url": "https://v3.fal.media/files/clip.mp4"}}
    monkeypatch.setattr(pl, "download", lambda url, dest: dest.write_bytes(clip.read_bytes()))
    [item] = pl.generate_ai_visual(tmp_path / "p", "waves", "video")
    assert item["kind"] == "video" and item["file"].endswith(".mp4") and item["thumb"].endswith(".jpg")
    assert fal.calls["posts"][0][2] == {"prompt": "waves", "aspect_ratio": "16:9", "duration": "5"}
    assert fal.calls["posts"][0][0] == f"https://queue.fal.run/{pl.AI_VIDEO_MODEL}"


def test_scenes_set_to_ai_get_an_image_or_fall_back_to_stock(fal, tmp_path, monkeypatch):
    scene = {"narration": "Lava meets the sea.", "visual": {"source": "ai", "query": "lava ocean"}}
    asset = pl.fetch_into(tmp_path, [], 0, scene, set(), 5)
    assert asset["source"] == "ai" and (tmp_path / "assets" / "scene_00.jpg").exists()
    assert fal.calls["posts"][0][2]["prompt"].startswith("lava ocean. Photorealistic")
    assert fal.calls["posts"][0][2]["num_images"] == 1
    monkeypatch.setattr(pl, "AI_VISUALS", "off")
    monkeypatch.setitem(pl.SEARCHERS, "stock_photo", lambda q, used, s: {
        "kind": "image", "url": "https://cdn/p.jpg", "id": "x:1", "credit": "stock"})
    asset = pl.fetch_into(tmp_path, [], 1, scene, set(), 5)
    assert asset["id"] == "x:1" and asset["source"] == "stock_photo" and len(fal.calls["posts"]) == 1


def test_ai_visuals_are_always_disclosed_to_youtube():
    args = SimpleNamespace(privacy="private", synthetic=False, publish_at=None)
    assert not pl.upload_status(args, [{"source": "nasa"}])["containsSyntheticMedia"]
    assert pl.upload_status(args, [{"source": "nasa"}, {"source": "ai"}])["containsSyntheticMedia"]
    assert pl.upload_status(args, [{"source": "nasa", "shots": [{"source": "ai"}]}])["containsSyntheticMedia"]


def test_generating_and_picking_in_the_web_ui(client, fakes, fal):
    slug, P = new_project(client)
    url = f"/api/projects/{slug}/jobs"
    assert client.post(url, headers=H, json={"action": "generate_visual", "scene": 1, "prompt": "x",
                                             "kind": "video"}).status_code == 400   # video is off
    assert client.post(url, headers=H, json={"action": "generate_visual", "scene": 1, "prompt": " "}).status_code == 400
    run_job(client, slug, action="generate_visual", scene=1, prompt="a lava river", count=2)
    r = client.get(f"/api/projects/{slug}/scenes/1/candidates?source=ai").get_json()
    assert len(r["results"]) == 2 and r["results"][0]["thumb"].startswith(f"/files/{slug}/ai/")
    assert r["ai_prompt"].endswith("no text, no watermark.")
    picked = r["results"][1]["id"]
    run_job(client, slug, action="pick_visual", scene=1, candidate=picked, query="")
    m = pl.load_json(P / "assets.json")
    assert m[1]["id"] == picked and m[1]["source"] == "ai"
    assert project(client, slug)["scene_info"][1]["asset"]["source"] == "ai"
    assert client.post(url, headers=H, json={"action": "pick_visual", "scene": 1,
                                             "candidate": "ai:doesnotexist"}).status_code == 400


def test_ai_settings_are_validated(client):
    r = client.put("/api/settings", headers=H, json={"AI_VISUALS": "images_and_video", "AI_IMAGE_COST": 0.03,
                                                     "AI_IMAGE_MODEL": "fal-ai/flux-pro/v1.1"})
    assert r.status_code == 200, r.get_json()
    assert pl.AI_VISUALS == "images_and_video" and pl.AI_IMAGE_COST == 0.03 and pl.AI_IMAGE_MODEL == "fal-ai/flux-pro/v1.1"
    for bad in ({"AI_VISUALS": "everything"}, {"AI_VIDEO_COST": -1}, {"AI_IMAGE_MODEL": "rm -rf /"}):
        assert client.put("/api/settings", headers=H, json=bad).status_code == 400, bad
