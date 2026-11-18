"""The final mix with real FFmpeg: the music level is relative to the voice whatever the track's mastering,
ducking works, levels follow MUSIC_VOLUME, nothing clips, and the voice tones shape the right bands."""
from __future__ import annotations

import array
import math
import subprocess
import wave
from pathlib import Path

import pytest

import pipeline as pl
from helpers import needs_ffmpeg

pytestmark = needs_ffmpeg
SECONDS = 15


def ff(*args):
    r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *map(str, args)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-1500:]


def read_left(path):
    with wave.open(str(path)) as w:
        assert w.getsampwidth() == 2
        rate, ch = w.getframerate(), w.getnchannels()
        data = array.array("h", w.readframes(w.getnframes()))
    return rate, data[0::ch]


def rms_db(samples, rate, t0, t1):
    seg = samples[int(t0 * rate):int(t1 * rate)]
    return 20 * math.log10(math.sqrt(sum(x * x for x in seg) / len(seg)) / 32768 + 1e-12)


def windows(on: bool):
    """Middle of each speech (on) or pause (off) window: the fake voice is 2 s on, 1 s off."""
    return [(t + 0.6, t + 1.6) if on else (t + 2.35, t + 2.85) for t in range(0, SECONDS - 2, 3)]


def level(samples, rate, wins):
    return sum(rms_db(samples, rate, a, b) for a, b in wins) / len(wins)


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    d = tmp_path_factory.mktemp("sound")
    # The "voice": a 1 kHz tone, 2 s on / 1 s off, as quiet as real ElevenLabs clips (about -36 LUFS)
    ff("-f", "lavfi", "-i", f"sine=frequency=1000:duration={SECONDS}:sample_rate=44100",
       "-af", "volume=0.02,volume='if(lt(mod(t,3),2),1,0)':eval=frame", "-ac", "2", d / "voice.wav")
    # Music: low-passed pink noise (all below 400 Hz), mastered loud and mastered quiet
    for name, amp in (("loud", 0.9), ("quiet", 0.04)):
        ff("-f", "lavfi", "-i", f"anoisesrc=color=pink:duration=7:amplitude={amp}:seed=7",
           "-af", "lowpass=f=300,lowpass=f=300", "-ac", "2", d / f"music_{name}.wav")
    ff("-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", SECONDS, d / "silent.wav")
    ff("-f", "lavfi", "-i", "anoisesrc=color=white:duration=6:amplitude=0.05:seed=3", "-ac", "2", d / "noise.wav")
    return d


def mix(media: Path, music, volume, monkeypatch, ducking=True, tone="soft"):
    monkeypatch.setattr(pl, "MUSIC_PATH", music)
    monkeypatch.setattr(pl, "MUSIC_VOLUME", volume)
    monkeypatch.setattr(pl, "MUSIC_DUCKING", ducking)
    gain = pl.music_gain_db(music)
    out = media / f"mix_{music.stem if music else 'none'}_{volume}_{ducking}_{tone}.wav"
    inputs = ["-i", media / "voice.wav"] + (["-stream_loop", "-1", "-i", music] if gain is not None else [])
    fc = pl.audio_filter("0:a", "1:a" if gain is not None else None, gain, tone)
    ff(*inputs, "-filter_complex", fc, "-map", "[a]", "-t", SECONDS, "-c:a", "pcm_s16le", out)
    low, high = out.with_name(out.stem + "_low.wav"), out.with_name(out.stem + "_high.wav")
    ff("-i", out, "-af", "lowpass=f=250,lowpass=f=250,lowpass=f=250", low)      # music band
    ff("-i", out, "-af", "highpass=f=700,highpass=f=700,highpass=f=700", high)  # voice band
    rate, lo = read_left(low)
    _, full = read_left(out)
    return {"gap": level(lo, rate, windows(False)), "under": level(lo, rate, windows(True)),
            "peak": max(abs(x) for x in full) / 32768, "lufs": pl.integrated_loudness(out)}


def test_every_voice_filter_is_available():
    assert len(pl.voice_filters("soft")) == 5 and pl.ffmpeg_has_filter("sidechaincompress")


def test_music_level_ignores_how_loud_the_track_was_mastered(media, monkeypatch):
    loud = mix(media, media / "music_loud.wav", 0.10, monkeypatch)
    quiet = mix(media, media / "music_quiet.wav", 0.10, monkeypatch)
    assert abs(loud["gap"] - quiet["gap"]) < 1.0, (loud, quiet)
    # ... lands at the voice target without clipping
    assert abs(loud["lufs"] - pl.VOICE_LUFS) < 1.5 and loud["peak"] < 0.95, loud
    # ... and ducks under speech
    assert 3 <= loud["gap"] - loud["under"] <= 10, loud


def test_without_ducking_the_music_stays_level(media, monkeypatch):
    flat = mix(media, media / "music_loud.wav", 0.10, monkeypatch, ducking=False)
    assert abs(flat["gap"] - flat["under"]) < 1.5, flat


@pytest.mark.parametrize("volume", [0.10, 0.04])
def test_the_music_bed_sits_music_volume_below_the_voice(media, monkeypatch, volume):
    music = media / "music_loud.wav"
    monkeypatch.setattr(pl, "MUSIC_PATH", music)
    monkeypatch.setattr(pl, "MUSIC_VOLUME", volume)
    out = media / f"bed_{volume}.wav"
    ff("-i", media / "silent.wav", "-stream_loop", "-1", "-i", music, "-filter_complex",
       pl.audio_filter("0:a", "1:a", pl.music_gain_db(music)), "-map", "[a]", "-t", SECONDS, "-c:a", "pcm_s16le", out)
    want = pl.VOICE_LUFS + 20 * math.log10(volume)
    assert abs(pl.integrated_loudness(out) - want) < 1.0


def test_halving_the_volume_lowers_the_music_by_6_db(media, monkeypatch):
    full = mix(media, media / "music_loud.wav", 0.10, monkeypatch)
    half = mix(media, media / "music_loud.wav", 0.05, monkeypatch)
    assert 5 <= full["gap"] - half["gap"] <= 7


def test_without_music_the_pauses_are_silent(media, monkeypatch):
    none = mix(media, None, 0.10, monkeypatch)
    loud = mix(media, media / "music_loud.wav", 0.10, monkeypatch)
    assert none["gap"] < loud["gap"] - 15


def test_soft_and_warm_tones_cut_harshness(media):
    def bands(tone):
        out = media / f"tone_{tone}.wav"
        ff("-i", media / "noise.wav", "-filter_complex", pl.audio_filter("0:a", None, None, tone), "-map", "[a]",
           "-c:a", "pcm_s16le", out)
        res = {}
        for name, af in (("1k", "bandpass=f=1000:width_type=o:w=0.5"), ("3k", "bandpass=f=3200:width_type=o:w=0.5"),
                         ("9k", "bandpass=f=9000:width_type=o:w=0.5"), ("150", "bandpass=f=150:width_type=o:w=0.5")):
            b = out.with_name(f"{out.stem}_{name}.wav")
            ff("-i", out, "-af", af, b)
            rate, s = read_left(b)
            res[name] = rms_db(s, rate, 1, 5)
        return {k: v - res["1k"] for k, v in res.items()}

    nat, soft, warm = bands("natural"), bands("soft"), bands("warm")
    assert nat["3k"] - soft["3k"] >= 2.5 and nat["9k"] - soft["9k"] >= 2.5 and soft["150"] - nat["150"] >= 0.5
    assert warm["3k"] < soft["3k"] and warm["9k"] < soft["9k"]


def preflight_error():
    try:
        pl.preflight(upload_enabled=False, unattended=False)
    except SystemExit as e:
        return str(e)
    return None


def test_preflight_catches_bad_music_settings(media, monkeypatch, tmp_path):
    monkeypatch.setattr(pl, "MUSIC_PATH", None)
    assert preflight_error() is None
    monkeypatch.setattr(pl, "MUSIC_PATH", tmp_path / "missing.mp3")
    err = preflight_error()
    assert err and "MUSIC_PATH points to" in err and "missing.mp3" in err
    monkeypatch.setattr(pl, "MUSIC_PATH", media / "music_loud.wav")
    monkeypatch.setattr(pl, "MUSIC_VOLUME", 1.5)
    assert "MUSIC_VOLUME is 1.5" in (preflight_error() or "")
    monkeypatch.setattr(pl, "MUSIC_VOLUME", 0.10)
    assert preflight_error() is None


def test_a_short_music_clip_loops_to_the_end_of_the_video(media, monkeypatch, tmp_path):
    proj = tmp_path / "proj"
    (proj / "audio").mkdir(parents=True)
    (proj / "segments").mkdir()
    ff("-f", "lavfi", "-i", "testsrc=duration=6:size=1920x1080:rate=30", "-pix_fmt", "yuv420p", "-c:v", "libx264",
       proj / "clip.mp4")
    ff("-f", "lavfi", "-i", "sine=frequency=880:duration=6", "-ar", "44100", "-ac", "1", proj / "audio" / "scene_00.wav")
    music = tmp_path / "bed.wav"
    ff("-f", "lavfi", "-i", "sine=frequency=220:duration=2", "-ac", "2", music)
    monkeypatch.setattr(pl, "MUSIC_PATH", music)
    monkeypatch.setattr(pl, "CROSSFADE", 0)
    final = pl.assemble(proj, [{"duration": 6.0, "frames": 180}], [{"kind": "video", "path": str(proj / "clip.mp4")}])
    assert abs(pl.media_duration(final) - 6.0) < 0.1
    out = subprocess.run(["ffmpeg", "-hide_banner", "-ss", "5", "-i", str(final), "-af",
                          "bandpass=f=220:width_type=h:w=40,volumedetect", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    assert float(out.split("mean_volume:")[1].split("dB")[0]) > -60, "the 2 s music clip must loop"
