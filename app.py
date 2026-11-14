"""SoundTwin - find the original recording behind a clip, and songs that sound like it.

Input: an .mp3/.mp4 (or any audio/video file), or a YouTube / TikTok link.
Output: the identified original recording (covers are detected and swapped for the
original), plus similar-sounding songs ranked by real audio comparison.
"""
import asyncio
import inspect
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
import wave
import webbrowser
from concurrent.futures import ThreadPoolExecutor

import imageio_ffmpeg
import numpy as np
from flask import Flask, jsonify, redirect, request, send_from_directory
from shazamio import Shazam

HERE = os.path.dirname(os.path.abspath(__file__))
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
PORT = 8765
SR = 16000          # working sample rate for fingerprinting
FSR = 11025         # sample rate for sound-similarity features
CLIP = 12           # seconds per fingerprint sample

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024 * 1024
JOBS = {}

# ---------------------------------------------------------------- cover detection

COVER_RE = re.compile(
    r"\b(cover(ed|s)?|karaoke|tribute|originally\s+(performed|by)|made\s+famous|"
    r"in\s+the\s+style\s+of|as\s+made\s+popular|instrumental|backing\s+track|"
    r"piano\s+version|acoustic\s+cover|lullab(y|ies)|music\s+box|8[\s-]?bit|"
    r"rendition|re-?creation|sound-?alike|tik\s?tok\s+(version|remix)|"
    r"sped[\s-]?up|speed\s+up|jersey\s+club|mash-?up|slowed|reverb|nightcore|bass\s+boosted|ringtone)\b",
    re.I,
)
HINT_RE = re.compile(
    r"(?:originally\s+performed|made\s+famous|in\s+the\s+style\s+of|as\s+made\s+popular|tribute\s+to)"
    r"\s*(?:by)?\s*([^)\]\-]+)",
    re.I,
)


def cover_reason(*texts):
    """Return the keyword that marks this as a cover / altered version, else None."""
    for t in texts:
        m = COVER_RE.search(t or "")
        if m:
            return m.group(0).lower()
    return None


def clean_title(title):
    t = re.sub(r"\(.*?\)|\[.*?\]", " ", title or "")
    t = re.split(r"\s[-–]\s", t)[0]
    return re.sub(r"\s+", " ", t).strip()


def norm(s):
    return re.sub(r"[^a-z0-9]+", "", clean_title(s).lower())


# ---------------------------------------------------------------- audio helpers

def ffmpeg(args, data=None):
    p = subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *args],
        input=data, capture_output=True, creationflags=NO_WINDOW,
    )
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode("utf-8", "ignore").strip()[-400:] or "ffmpeg failed")
    return p.stdout


def load_master(src, dst):
    """Convert any audio/video file to 16 kHz mono wav (max 10 min) and load it."""
    ffmpeg(["-i", src, "-vn", "-ac", "1", "-ar", str(SR), "-t", "600", dst])
    with wave.open(dst, "rb") as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)


def wav_bytes(samples, rate):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(rate))
        w.writeframes(samples.tobytes())
    return buf.getvalue()


def decode_bytes(data, rate=FSR):
    raw = ffmpeg(["-i", "pipe:0", "-vn", "-ac", "1", "-ar", str(rate), "-f", "s16le", "pipe:1"], data)
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def features(x, sr=FSR):
    """Small audio fingerprint of how a track *sounds*: timbre, harmony and tempo."""
    if x is None or len(x) < sr * 6:
        return None
    x = x[: sr * 40]
    n, hop = 1024, 256
    frames = np.lib.stride_tricks.sliding_window_view(x, n)[::hop] * np.hanning(n).astype(np.float32)
    S = np.abs(np.fft.rfft(frames, axis=1)).astype(np.float32)
    freqs = np.fft.rfftfreq(n, 1 / sr)

    edges = np.geomspace(60, 5000, 25)
    bands = np.stack(
        [S[:, (freqs >= lo) & (freqs < hi)].sum(1) for lo, hi in zip(edges[:-1], edges[1:])], axis=1
    )
    lb = np.log(bands + 1e-4)
    lb -= lb.mean()
    timbre = np.concatenate([lb.mean(0), lb.std(0)])

    sel = (freqs >= 100) & (freqs <= 2000)
    pc = (np.round(12 * np.log2(freqs[sel] / 440.0)).astype(int)) % 12
    power = (S[:, sel] ** 2).mean(0)
    chroma = np.array([power[pc == k].sum() for k in range(12)])
    chroma /= np.linalg.norm(chroma) + 1e-9

    flux = np.maximum(0, np.diff(lb, axis=0)).sum(1)
    env = flux - flux.mean()
    ac = np.correlate(env, env, "full")[len(env) - 1:]
    fps = sr / hop
    lo, hi = int(60 * fps / 190), int(60 * fps / 62)
    k = lo + int(np.argmax(ac[lo:hi]))
    if 0 < k < len(ac) - 1:  # parabolic refinement
        a, b, c = ac[k - 1], ac[k], ac[k + 1]
        d = a - 2 * b + c
        kf = k + (0.5 * (a - c) / d if d else 0)
    else:
        kf = k
    return {"timbre": timbre, "chroma": chroma, "bpm": float(60 * fps / kf)}


def similarity(a, b):
    d = np.linalg.norm(a["timbre"] - b["timbre"]) / np.sqrt(len(a["timbre"]))
    s_t = float(np.exp(-1.1 * d))
    c = max(float(np.dot(a["chroma"], np.roll(b["chroma"], r))) for r in range(12))
    s_c = float(np.clip((c - 0.6) / 0.4, 0, 1))
    r = abs(np.log2(a["bpm"] / b["bpm"]))
    r = min(r, abs(r - 1))  # half / double time counts as the same tempo
    s_b = float(np.exp(-((r / 0.09) ** 2)))
    return 0.5 * s_t + 0.2 * s_c + 0.3 * s_b


# ---------------------------------------------------------------- Deezer (catalog, no key needed)

def dz(path, **params):
    url = "https://api.deezer.com/" + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    for attempt in range(3):
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8"))
        if isinstance(data, dict) and data.get("error", {}).get("code") == 4:  # rate limit
            time.sleep(1.2)
            continue
        return data
    return {}


def dz_track(t):
    return {
        "id": t.get("id"),
        "title": t.get("title"),
        "artist": (t.get("artist") or {}).get("name"),
        "artist_id": (t.get("artist") or {}).get("id"),
        "album": (t.get("album") or {}).get("title"),
        "art": (t.get("album") or {}).get("cover_medium"),
        "preview": t.get("preview"),
        "link": t.get("link"),
        "rank": t.get("rank") or 0,
    }


def dz_find(title, artist, strict=False):
    first = (artist or "").split(",")[0].split("&")[0].split(" feat")[0].strip()
    queries = [
        f'artist:"{first}" track:"{clean_title(title)}"',
        f"{first} {clean_title(title)}",
    ]
    for q in queries:
        items = dz("search", q=q, limit=10).get("data") or []
        want = norm(title)
        for it in items:
            if norm(it.get("title")) == want:
                return dz_track(it)
        if items and not strict:
            return dz_track(items[0])
    return None


def find_original(title, artist_hint=None):
    """Given a cover's title, find the most likely original recording."""
    want = norm(title)
    queries = []
    if artist_hint:
        queries.append(f'artist:"{artist_hint.strip()}" track:"{clean_title(title)}"')
    queries.append(f'track:"{clean_title(title)}"')
    for q in queries:
        items = [dz_track(i) for i in (dz("search", q=q, limit=50).get("data") or [])]
        items = [
            i for i in items
            if norm(i["title"]) == want and not cover_reason(i["title"], i["artist"], i["album"])
        ]
        if items:
            return max(items, key=lambda i: i["rank"])
    return None


def similar_candidates(seed):
    out, seen = [], set()

    def add(items):
        for it in items or []:
            t = dz_track(it)
            key = (norm(t["title"]), (t["artist"] or "").lower())
            if key in seen or norm(t["title"]) == norm(seed["title"]):
                continue
            seen.add(key)
            t["cover"] = cover_reason(t["title"], t["artist"], t["album"])
            out.append(t)

    aid = seed.get("artist_id")
    if not aid:
        return out
    add(dz(f"artist/{aid}/radio", limit=40).get("data"))
    for rel in (dz(f"artist/{aid}/related", limit=10).get("data") or []):
        add(dz(f"artist/{rel['id']}/top", limit=5).get("data"))
    return out[:90]


COVER_ARTIST_RE = re.compile(
    r"(all[\s-]?stars|hit\s+crew|cover\s+band|studio\s+(musicians|group|players)|"
    r"string\s+quartet|rockabye|ameritz|union\s+of\s+sound|sound-?a-?like|"
    r"\bkids\b|hits\s+(masters|unlimited)|party\s+(band|hits))",
    re.I,
)


def looks_like_cover(track):
    """Catch unlabeled covers: a cover-band style name, or a low-popularity track whose
    exact title is a much bigger hit by a different artist."""
    if COVER_ARTIST_RE.search(track.get("artist") or ""):
        return True
    if track["rank"] >= 350000:
        return False
    try:
        items = dz("search", q=f'track:"{clean_title(track["title"])}"', limit=25).get("data") or []
    except Exception:
        return False
    me = (track.get("artist") or "").lower()
    for it in items:
        other = dz_track(it)
        if (norm(other["title"]) == norm(track["title"]) and (other["artist"] or "").lower() != me
                and other["rank"] >= track["rank"] + 250000):
            return True
    return False


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read()


def preview_features(track):
    try:
        return features(decode_bytes(fetch(track["preview"]))) if track.get("preview") else None
    except Exception:
        return None


# ---------------------------------------------------------------- recognition

def shazam_info(t):
    meta = {}
    for s in t.get("sections") or []:
        for m in s.get("metadata") or []:
            meta[m.get("title")] = m.get("text")
    img = t.get("images") or {}
    return {
        "key": t.get("key"),
        "title": t.get("title"),
        "artist": t.get("subtitle"),
        "art": img.get("coverarthq") or img.get("coverart"),
        "shazam": t.get("url"),
        "album": meta.get("Album"),
        "year": meta.get("Released"),
        "label": meta.get("Label"),
        "genre": (t.get("genres") or {}).get("primary"),
    }


def popularity(track):
    """Deezer popularity of a Shazam match: -1 for labelled covers, 0 if not in the catalog."""
    info = shazam_info(track)
    if cover_reason(info["title"], info["artist"], info["album"]):
        return -1
    try:
        d = dz_find(info["title"], info["artist"], strict=True)
    except Exception:
        return 0
    return d["rank"] if d else 0


async def recognize(pcm, say):
    """Fingerprint several parts of the audio; if nothing matches, undo speed/pitch edits."""
    dur = len(pcm) / SR
    shazam = Shazam()

    def clip(frac, speed):
        length = int(min(dur, CLIP * speed) * SR)
        start = int(max(0, min(len(pcm) - length, frac * len(pcm) - length / 2)))
        # relabelling the sample rate plays the clip slower/faster, undoing the edit
        return wav_bytes(pcm[start:start + length], SR / speed)

    async def ask(frac, speed):
        for attempt in range(2):
            try:
                res = await shazam.recognize(clip(frac, speed))
                return res.get("track")
            except Exception:
                await asyncio.sleep(1.5)
        return None

    try:
        fracs = [0.5, 0.25, 0.75, 0.12, 0.88] if dur > CLIP * 1.5 else [0.5]
        votes, tracks = {}, {}
        say("Fingerprinting the audio")
        for f in fracs:
            t = await ask(f, 1.0)
            if t and t.get("key"):
                votes[t["key"]] = votes.get(t["key"], 0) + 1
                tracks[t["key"]] = t
                if votes[t["key"]] >= 2:
                    break
        speeds = [1.25, 1.15, 1.2, 1.1, 1.3, 0.85, 0.8, 0.9, 1.05, 1.35, 0.75, 1.5]
        if votes:
            best = tracks[max(votes, key=votes.get)]
            # An obscure or cover-labelled match is often a remix / sped-up re-upload of a
            # famous song. Undo speed edits and see if a much better-known recording appears.
            rank = await asyncio.to_thread(popularity, best)
            if rank < 300000:
                say("Match looks like a remix or re-upload - checking for the original underneath")
                for speed in speeds:
                    t = await ask(0.5, speed)
                    if t and t.get("key") and t["key"] != best["key"]:
                        if await asyncio.to_thread(popularity, t) >= max(rank, 0) + 200000:
                            return best, speed, t
            return best, 1.0, None

        say("No direct match - checking for sped-up / slowed versions")
        for speed in speeds:
            for f in fracs[:2]:
                t = await ask(f, speed)
                if t and t.get("key"):
                    return t, speed, None
        return None, 1.0, None
    finally:
        closing = getattr(shazam, "close", None)
        if closing:
            r = closing()
            if inspect.isawaitable(r):
                await r


# ---------------------------------------------------------------- pipeline

def download(url, folder):
    import yt_dlp

    opts = {
        "format": "bestaudio[abr<=160]/bestaudio/best[height<=480]/best",
        "outtmpl": os.path.join(folder, "src.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "ffmpeg_location": FFMPEG,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return ydl.prepare_filename(info), info.get("title")


SHORTLIST = 14   # candidates that get the (slow) stem-by-stem analysis


def decode_stereo(args, data=None):
    raw = ffmpeg([*args, "-vn", "-ac", "2", "-ar", "44100", "-f", "f32le", "pipe:1"], data)
    return np.frombuffer(raw, dtype=np.float32).reshape(-1, 2).T


def technique_rank(job, src_path, dur, speed, seed, shortlist, say):
    """Split the clip and each shortlisted song into drums / bass / instruments / vocals
    and rank by how similarly each part is played. Returns the top 10."""
    import stems

    say("Splitting your clip into drums, bass, instruments and vocals")
    length = 15 * speed
    # two different sections of a full song (e.g. verse and solo); one for short clips
    spots = [0.3, 0.65] if dur > length * 3 else [0.5]
    refs = []
    for frac in spots:
        start = max(0.0, dur * frac - length / 2)
        args = ["-ss", f"{start:.2f}", "-t", f"{length:.2f}", "-i", src_path]
        if speed != 1:
            args += ["-af", f"aresample=44100,asetrate={44100 / speed:.0f},aresample=44100"]
        refs.append(stems.analyse(decode_stereo(args)))

    lib = stems.load_library()
    todo = [c for c in shortlist if c.get("preview") and str(c["id"]) not in lib]
    say(f"Analysing how each part is played: 0/{len(todo)} songs")
    for i, c in enumerate(todo):
        try:
            audio = decode_stereo(["-i", "pipe:0", "-ss", "9", "-t", "12"], fetch(c["preview"]))
            lib[str(c["id"])] = {
                "meta": {k: c.get(k) for k in ("id", "title", "artist", "album", "art", "preview", "link", "rank")},
                "prof": stems.analyse(audio),
            }
        except Exception:
            pass
        job["steps"][-1] = f"Analysing how each part is played: {i + 1}/{len(todo)} songs"
    if todo:
        stems.save_library(lib)

    # compare against the shortlist plus everything analysed in earlier searches
    skip = norm(seed["title"]) if seed else ""
    out, seen = [], set()
    for entry in lib.values():
        m = entry["meta"]
        key = (norm(m["title"]), (m["artist"] or "").lower())
        if norm(m["title"]) == skip or key in seen or cover_reason(m["title"], m["artist"], m["album"]):
            continue
        seen.add(key)
        score, per = max((stems.compare(ref, entry["prof"]) for ref in refs), key=lambda r: r[0])
        out.append({**m, "score": score, "stems": per, "bpm": round(entry["prof"]["bpm"])})
    out.sort(key=lambda t: t["score"], reverse=True)
    return out[:10]


def run_job(job, src_path=None, url=None):
    def say(msg):
        job["steps"].append(msg)

    work = job["dir"]
    try:
        label = job.get("name")
        if url:
            say("Downloading audio from the link")
            src_path, label = download(url, work)
        say("Converting audio")
        pcm = load_master(src_path, os.path.join(work, "master.wav"))
        if len(pcm) < SR * 3:
            raise RuntimeError("That clip is too short - it needs at least 3 seconds of audio.")

        track, speed, under = asyncio.run(recognize(pcm, say))
        result = {"source": label, "speed": speed, "duration": round(len(pcm) / SR)}
        if not track:
            result["match"] = None
            job["result"] = result
            return

        match = shazam_info(track)
        reason = cover_reason(match["title"], match["artist"], match["album"])
        match["cover"] = reason
        seed = dz_find(match["title"], match["artist"])
        if seed:
            match.update(preview=seed["preview"], deezer=seed["link"], id=seed["id"])
            match["art"] = match["art"] or seed["art"]
        result["match"] = match

        if under:  # the real recording found underneath a remix / re-upload
            u = shazam_info(under)
            orig = dz_find(u["title"], u["artist"], strict=True) or {
                "title": u["title"], "artist": u["artist"], "album": u["album"], "art": u["art"],
                "link": u["shazam"], "preview": None, "rank": 0,
            }
            match["cover"] = reason or "remix / re-upload"
            result["original"] = orig
            seed = orig
        elif reason:
            say("That recording is a cover - looking for the original")
            hint = HINT_RE.search(f"{match['title']} {match['artist']}")
            orig = find_original(match["title"], hint.group(1) if hint else None)
            if orig:
                result["original"] = orig
                seed = orig

        say("Finding songs that sound like it")
        cands = similar_candidates(seed) if seed else []
        hidden = sum(1 for c in cands if c["cover"])
        cands = [c for c in cands if not c["cover"]]

        say(f"Comparing the sound of {len(cands)} candidates")
        # reference sound = the input itself, speed-corrected, taken from the middle
        mid = len(pcm) // 2
        seg = pcm[max(0, mid - SR * 20): mid + SR * 20]
        ref = features(decode_bytes(wav_bytes(seg, SR / speed)))
        with ThreadPoolExecutor(8) as pool:
            feats = list(pool.map(preview_features, cands))
        scored = []
        for c, f in zip(cands, feats):
            if ref and f:
                c["score"] = round(100 * similarity(ref, f))
                c["bpm"] = round(f["bpm"])
            else:
                c["score"] = None
            scored.append(c)
        scored.sort(key=lambda c: (c["score"] is not None, c["score"] or 0, c["rank"]), reverse=True)
        top = scored[:30]
        with ThreadPoolExecutor(5) as pool:
            flags = list(pool.map(looks_like_cover, top))
        hidden += sum(flags)
        shortlist = [c for c, bad in zip(top, flags) if not bad]
        result["similar"] = shortlist[:10]
        try:
            ranked = technique_rank(job, src_path, len(pcm) / SR, speed, seed, shortlist[:SHORTLIST], say)
            if ranked:
                result["similar"] = ranked
                result["stems"] = True
        except Exception as e:
            say(f"Stem comparison unavailable ({str(e)[:80]}) - showing overall sound match instead")
        result["hidden_covers"] = hidden
        result["bpm"] = round(ref["bpm"]) if ref else None
        job["result"] = result
    except Exception as e:  # surface a readable message to the page
        msg = str(e)
        msg = re.sub(r"\x1b\[[0-9;]*m", "", msg).replace("ERROR: ", "")
        job["error"] = msg[:400] or "Something went wrong."
    finally:
        job["done"] = True
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------- web

@app.get("/")
def index():
    return send_from_directory(HERE, "index.html")


@app.post("/api/analyze")
def analyze():
    job_id = uuid.uuid4().hex
    work = tempfile.mkdtemp(prefix="soundtwin_")
    job = {"steps": [], "done": False, "dir": work}
    f = request.files.get("file")
    url = (request.form.get("url") or "").strip()
    if f and f.filename:
        ext = os.path.splitext(f.filename)[1][:8] or ".bin"
        path = os.path.join(work, "upload" + re.sub(r"[^.\w]", "", ext))
        f.save(path)
        job["name"] = f.filename
        args = {"src_path": path}
    elif re.match(r"^https?://", url, re.I):
        args = {"url": url}
    else:
        shutil.rmtree(work, ignore_errors=True)
        return jsonify(error="Add a file or paste a link that starts with http:// or https://"), 400
    JOBS[job_id] = job
    threading.Thread(target=run_job, args=(job,), kwargs=args, daemon=True).start()
    return jsonify(id=job_id)


@app.get("/api/preview/<int:track_id>")
def preview(track_id):
    """Deezer preview links expire after a few minutes, so look up a fresh one per play."""
    try:
        url = dz(f"track/{track_id}").get("preview")
    except Exception:
        url = None
    if not url or not url.startswith("https://"):
        return jsonify(error="No preview available for this song"), 404
    resp = redirect(url, code=302)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/api/job/<job_id>")
def job_status(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify(error="Unknown job"), 404
    return jsonify(steps=job["steps"], done=job["done"], error=job.get("error"), result=job.get("result"))


if __name__ == "__main__":
    if not os.environ.get("SOUNDTWIN_NO_BROWSER"):
        threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{PORT}")).start()
    print(f"SoundTwin is running at http://127.0.0.1:{PORT}  (close this window to stop)")
    app.run(host="127.0.0.1", port=PORT, threaded=True)
