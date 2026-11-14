"""Split a clip into drums / bass / other instruments / vocals (Demucs) and describe
how each part is *played*: its tone, rhythm pattern, note density, grit and dynamics."""
import json
import os
import threading

import numpy as np

STEMS = ["drums", "bass", "other", "vocals"]
WEIGHTS = {"drums": 0.30, "other": 0.30, "vocals": 0.25, "bass": 0.15}
SR = 44100
CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stem_library.json")

_model = None
_lock = threading.Lock()
_cache_lock = threading.Lock()


def _get_model():
    global _model
    if _model is None:
        import torch
        from demucs.pretrained import get_model

        # leave a couple of cores free so the PC stays responsive
        torch.set_num_threads(max(1, min(5, (os.cpu_count() or 4) // 2 - 1)))
        _model = get_model("htdemucs")
        _model.eval()
    return _model


def separate(stereo):
    """stereo: float32 array (2, n) at 44.1 kHz -> dict of mono stems."""
    import torch
    from demucs.apply import apply_model

    with _lock:  # one separation at a time keeps memory and CPU use sane
        model = _get_model()
        x = torch.from_numpy(np.ascontiguousarray(stereo))
        ref = x.mean(0)
        mean, std = ref.mean(), ref.std() + 1e-8
        with torch.no_grad():
            out = apply_model(model, ((x - mean) / std)[None], shifts=0, split=True, overlap=0.1, progress=False)[0]
        out = out * std + mean
        return {name: out[i].mean(0).numpy() for i, name in enumerate(model.sources)}


def _spec(x):
    n, hop = 1024, 256
    frames = np.lib.stride_tricks.sliding_window_view(x, n)[::hop] * np.hanning(n).astype(np.float32)
    S = np.abs(np.fft.rfft(frames, axis=1)).astype(np.float32)
    return S, np.fft.rfftfreq(n, 1 / SR), SR / hop


def _bands(S, freqs):
    edges = np.geomspace(40, 12000, 25)
    b = np.stack([S[:, (freqs >= lo) & (freqs < hi)].sum(1) for lo, hi in zip(edges[:-1], edges[1:])], axis=1)
    return np.log(b + 1e-5)


def _tempo(env, fps):
    ac = np.correlate(env, env, "full")[len(env) - 1:]
    lo, hi = int(60 * fps / 190), int(60 * fps / 62)
    k = lo + int(np.argmax(ac[lo:hi]))
    return 60 * fps / k


def _profile(x, mix_rms, beat):
    rms = float(np.sqrt(np.mean(x ** 2)))
    share = rms / (mix_rms + 1e-9)
    if share < 0.06:
        return {"share": share, "silent": True}
    S, freqs, fps = _spec(x)
    lb = _bands(S, freqs)
    level = lb.mean()
    lbn = lb - level
    sel = (freqs >= 100) & (freqs <= 8000)
    P = S[:, sel] + 1e-7
    flat = float(np.mean(np.exp(np.log(P).mean(1)) / P.mean(1)))
    flux = np.maximum(0, np.diff(lb, axis=0)).sum(1)
    env = flux - flux.mean()
    thr = 0.5 * flux.std()
    peaks = (env[1:-1] > env[:-2]) & (env[1:-1] >= env[2:]) & (env[1:-1] > thr)
    ac = np.correlate(env, env, "full")[len(env) - 1:]
    ac = ac / (ac[0] + 1e-9)
    lags = beat * fps * np.arange(1, 17) / 4.0   # sixteenth-note grid over four beats
    rhythm = np.interp(lags, np.arange(len(ac)), ac)
    energy = np.log((S ** 2).sum(1) + 1e-7)
    return {
        "share": share,
        "silent": False,
        "timbre": np.concatenate([lbn.mean(0), lbn.std(0)]),
        "flat": flat,
        "onsets": float(peaks.sum() / (len(env) / fps)),
        "rhythm": rhythm,
        "dyn": float(energy.std()),
    }


def analyse(stereo):
    """Full technique profile of a clip: tempo plus one profile per stem."""
    parts = separate(stereo)
    mix = stereo.mean(0)
    mix_rms = float(np.sqrt(np.mean(mix ** 2)))
    drums = parts["drums"]
    src = drums if np.sqrt(np.mean(drums ** 2)) > 0.1 * mix_rms else mix
    S, freqs, fps = _spec(src)
    flux = np.maximum(0, np.diff(_bands(S, freqs), axis=0)).sum(1)
    bpm = float(_tempo(flux - flux.mean(), fps))
    beat = 60.0 / bpm
    return {"bpm": bpm, "stems": {name: _profile(parts[name], mix_rms, beat) for name in STEMS}}


def _stem_sim(a, b):
    if a["silent"] and b["silent"]:
        return None
    if a["silent"] or b["silent"]:
        return 0.15
    ta, tb = np.asarray(a["timbre"]), np.asarray(b["timbre"])
    s_t = float(np.exp(-1.1 * np.linalg.norm(ta - tb) / np.sqrt(len(ta))))
    ra, rb = np.asarray(a["rhythm"]), np.asarray(b["rhythm"])
    s_r = max(0.0, float(np.dot(ra, rb) / (np.linalg.norm(ra) * np.linalg.norm(rb) + 1e-9)))
    s_o = float(np.exp(-abs(np.log2((a["onsets"] + 0.2) / (b["onsets"] + 0.2))) / 0.6))
    s_f = float(np.exp(-abs(a["flat"] - b["flat"]) / 0.12))
    s_d = float(np.exp(-abs(a["dyn"] - b["dyn"]) / 1.0))
    return 0.40 * s_t + 0.30 * s_r + 0.15 * s_o + 0.10 * s_f + 0.05 * s_d


def compare(a, b):
    """-> (overall 0-100, {stem: 0-100 or None})"""
    per, total, wsum = {}, 0.0, 0.0
    for name in STEMS:
        s = _stem_sim(a["stems"][name], b["stems"][name])
        per[name] = None if s is None else round(100 * s)
        if s is not None:
            total += WEIGHTS[name] * s
            wsum += WEIGHTS[name]
    r = abs(np.log2(a["bpm"] / b["bpm"]))
    r = min(r, abs(r - 1))
    tempo = float(np.exp(-((r / 0.12) ** 2)))
    overall = (total / wsum if wsum else 0.0) * (0.85 + 0.15 * tempo)
    return round(100 * overall), per


# ---------------------------------------------------------------- library on disk
# Every song that gets stem-analysed is remembered, so later searches compare against
# a growing library instead of starting from zero.

def _plain(o):
    if isinstance(o, dict):
        return {k: _plain(v) for k, v in o.items()}
    if isinstance(o, np.ndarray):
        return [round(float(v), 4) for v in o]
    if isinstance(o, (np.floating, np.bool_)):
        return o.item()
    return o


def load_library():
    with _cache_lock:
        try:
            with open(CACHE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}


def save_library(lib):
    with _cache_lock:
        tmp = CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_plain(lib), f)
        os.replace(tmp, CACHE_PATH)
