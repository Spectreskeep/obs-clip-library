"""
Clip Library for OBS
--------------------
A clip gallery + trimmer that lives in its own OBS dock.

How it works:
  * This script runs a tiny web server on your PC only (127.0.0.1).
  * You add it to OBS as a Custom Browser Dock (http://127.0.0.1:8765).
  * When you save a replay, the dock shows "Clip saved" and the clip appears.
  * Click a clip to watch it, drag the handles to trim, and copy it for Discord.

Needs: Python 3.12 (64-bit) set in Tools > Scripts > Python Settings,
       ffmpeg (for thumbnails and trimming): winget install Gyan.FFmpeg
"""

import obspython as obs
import os
import re
import json
import time
import shutil
import hashlib
import socket
import threading
import subprocess
import urllib.parse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

VERSION = "1.30"               # bump on every update; the Clips tab reloads itself when this changes
VIDEO_EXTS = (".mp4", ".mkv", ".mov", ".webm", ".m4v")
DISCORD_LIMIT_MB = 20          # Discord free upload limit (Aug 2026)
FIT_TARGET_BYTES = 19_000_000  # aim a bit under the limit
STRIP_FRAMES = 12              # frames in the timeline filmstrip
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
BAD_NAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

state = {
    "folder_setting": "",
    "folder": "",
    "port": 8765,
    "ffmpeg_setting": "",
    "ffmpeg": None,
    "ffprobe": None,
    "sound": True,
    "sound_file": "",
}

_server = None
_server_port = None
_events = []
_event_id = 0
_event_lock = threading.Lock()
_thumb_sem = threading.Semaphore(2)
_meta = {}                      # "path|mtime|size" -> duration (seconds)
_meta_lock = threading.Lock()
_meta_busy = False
_conns = set()                  # open connections, closed on reload so no old code lingers
_conns_lock = threading.Lock()


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def log(msg):
    print("[Clip Library] " + msg)


def cache_dir(sub=""):
    base = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
                        "obs-clip-library", sub)
    os.makedirs(base, exist_ok=True)
    return base


def detect_recording_folder():
    """Read OBS's recording path from the current profile."""
    try:
        cfg = obs.obs_frontend_get_profile_config()
        mode = obs.config_get_string(cfg, "Output", "Mode") or "Simple"
        if mode == "Advanced":
            if obs.config_get_string(cfg, "AdvOut", "RecType") == "FFmpeg":
                path = obs.config_get_string(cfg, "AdvOut", "FFFilePath")
            else:
                path = obs.config_get_string(cfg, "AdvOut", "RecFilePath")
        else:
            path = obs.config_get_string(cfg, "SimpleOutput", "FilePath")
        if path and os.path.isdir(path):
            return path
    except Exception as e:
        log("Couldn't read recording path from OBS: %s" % e)
    return os.path.join(os.path.expanduser("~"), "Videos")


def find_ffmpeg():
    p = state["ffmpeg_setting"]
    if p and os.path.isfile(p):
        return p
    found = shutil.which("ffmpeg")
    if found:
        return found
    # winget installs aren't on OBS's PATH until OBS restarts, so look directly
    local = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        os.path.join(local, "Microsoft", "WinGet", "Packages"),
        r"C:\ffmpeg",
        r"C:\Program Files\ffmpeg",
    ]
    for base in candidates:
        if not os.path.isdir(base):
            continue
        for root, _dirs, files in os.walk(base):
            if "ffmpeg.exe" in files:
                return os.path.join(root, "ffmpeg.exe")
    return None


def find_ffprobe(ffmpeg):
    if ffmpeg:
        sib = os.path.join(os.path.dirname(ffmpeg), "ffprobe.exe" if os.name == "nt" else "ffprobe")
        if os.path.isfile(sib):
            return sib
    return shutil.which("ffprobe")


def resolve_clip(name):
    """Return the full path for a clip name, refusing anything outside the folder."""
    if not name or os.path.basename(name) != name or name.startswith("."):
        return None
    fp = os.path.join(state["folder"], name)
    if os.path.isfile(fp) and name.lower().endswith(VIDEO_EXTS):
        return fp
    return None


def run(cmd, timeout):
    return subprocess.run(cmd, capture_output=True, text=True,
                          creationflags=NO_WINDOW, timeout=timeout)


def unique_path(folder, base, ext):
    p = os.path.join(folder, base + ext)
    i = 2
    while os.path.exists(p):
        p = os.path.join(folder, "%s (%d)%s" % (base, i, ext))
        i += 1
    return p


def push_event(kind, name):
    global _event_id
    with _event_lock:
        _event_id += 1
        _events.append({"id": _event_id, "type": kind, "name": name, "t": time.time()})
        del _events[:-50]


def get_events(since):
    with _event_lock:
        return {"last": _event_id, "events": [e for e in _events if e["id"] > since]}


def play_sound():
    if os.name != "nt" or not state["sound"]:
        return
    try:
        import winsound
        f = state["sound_file"]
        if f and os.path.isfile(f):
            winsound.PlaySound(f, winsound.SND_FILENAME | winsound.SND_ASYNC)
        else:
            winsound.PlaySound("SystemAsterisk", winsound.SND_ALIAS | winsound.SND_ASYNC)
    except Exception as e:
        log("Sound failed: %s" % e)


# --------------------------------------------------------------------------
# Durations (cached, filled in the background)
# --------------------------------------------------------------------------

def meta_key(fp, st):
    return "%s|%d|%d" % (fp, int(st.st_mtime), st.st_size)


_trims = {}                     # clip name -> [start, end] the person last set
_trims_lock = threading.Lock()


def load_trims():
    try:
        with open(os.path.join(cache_dir(), "trims.json"), "r", encoding="utf-8") as f:
            data = json.load(f)
        with _trims_lock:
            _trims.clear()
            _trims.update(data)
    except Exception:
        pass


def save_trims():
    try:
        with _trims_lock:
            data = dict(_trims)
        tmp = os.path.join(cache_dir(), "trims.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, os.path.join(cache_dir(), "trims.json"))
    except Exception:
        pass


def set_trim(name, start, end):
    if not resolve_clip(name):
        raise ValueError("Clip not found.")
    with _trims_lock:
        if start is None or end is None:
            _trims.pop(name, None)
        else:
            _trims[name] = [round(max(0.0, float(start)), 3), round(float(end), 3)]
    save_trims()
    log("Saved trim for %s: %s" % (name, _trims.get(name, "cleared")))
    return {"ok": True}


def forget_trim(name, new_name=None):
    with _trims_lock:
        t = _trims.pop(name, None)
        if t and new_name:
            _trims[new_name] = t
        m = _mixes.pop(name, None)
        if m and new_name:
            _mixes[new_name] = m
    save_trims()
    save_mixes()


_mixes = {}                     # clip name -> {"vols": [...], "mutes": [...]} per audio track


def load_mixes():
    try:
        with open(os.path.join(cache_dir(), "mix.json"), "r", encoding="utf-8") as f:
            data = json.load(f)
        with _trims_lock:
            _mixes.clear()
            _mixes.update(data)
    except Exception:
        pass


def save_mixes():
    try:
        with _trims_lock:
            data = dict(_mixes)
        tmp = os.path.join(cache_dir(), "mix.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, os.path.join(cache_dir(), "mix.json"))
    except Exception:
        pass


def clean_mix(mix):
    """{"vols": [...], "mutes": [...]} with sane values, or None for 'leave the audio as recorded'."""
    if not isinstance(mix, dict):
        return None
    vols = mix.get("vols") or []
    mutes = mix.get("mutes") or []
    try:
        vols = [min(3.0, max(0.0, float(v))) for v in vols][:8]
    except (TypeError, ValueError):
        return None
    mutes = [bool(m) for m in mutes][:8]
    while len(mutes) < len(vols):
        mutes.append(False)
    if all(abs(v - 1.0) < 1e-3 for v in vols) and not any(mutes):
        return None
    return {"vols": [round(v, 3) for v in vols], "mutes": mutes[:len(vols)]}


def set_mix(name, mix):
    if not resolve_clip(name):
        raise ValueError("Clip not found.")
    m = clean_mix(mix)
    with _trims_lock:
        if m:
            _mixes[name] = m
        else:
            _mixes.pop(name, None)
    save_mixes()
    return {"ok": True}


def load_meta():
    try:
        with open(os.path.join(cache_dir(), "meta.json"), "r", encoding="utf-8") as f:
            data = json.load(f)
        with _meta_lock:
            _meta.update(data)
    except Exception:
        pass


def save_meta():
    try:
        with _meta_lock:
            data = dict(list(_meta.items())[-2000:])
        with open(os.path.join(cache_dir(), "meta.json"), "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception:
        pass


def probe_duration(fp):
    probe = state["ffprobe"]
    if not probe:
        return None
    try:
        r = run([probe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=nw=1:nk=1", fp], 20)
        return round(float(r.stdout.strip()), 3)
    except Exception:
        return None


def duration_of(fp):
    key = meta_key(fp, os.stat(fp))
    with _meta_lock:
        if key in _meta:
            return _meta[key]
    d = probe_duration(fp)
    if d:
        with _meta_lock:
            _meta[key] = d
        save_meta()
    return d


def fill_durations(paths):
    global _meta_busy
    try:
        for fp in paths:
            try:
                key = meta_key(fp, os.stat(fp))
            except OSError:
                continue
            d = probe_duration(fp)
            if d:
                with _meta_lock:
                    _meta[key] = d
        save_meta()
    finally:
        _meta_busy = False


# --------------------------------------------------------------------------
# Clip operations
# --------------------------------------------------------------------------

def list_clips():
    global _meta_busy
    folder = state["folder"]
    clips, missing = [], []
    if folder and os.path.isdir(folder):
        for entry in os.scandir(folder):
            if entry.is_file() and entry.name.lower().endswith(VIDEO_EXTS) \
                    and not entry.name.startswith("."):
                st = entry.stat()
                with _meta_lock:
                    d = _meta.get(meta_key(entry.path, st))
                if d is None and time.time() - st.st_mtime > 2:
                    missing.append(entry.path)
                with _trims_lock:
                    tr = _trims.get(entry.name)
                    mx = _mixes.get(entry.name)
                clips.append({"name": entry.name, "size": st.st_size,
                              "mtime": st.st_mtime, "duration": d, "trim": tr, "mix": mx})
    clips.sort(key=lambda c: c["mtime"], reverse=True)
    if missing and state["ffprobe"] and not _meta_busy:
        _meta_busy = True
        threading.Thread(target=fill_durations, args=(missing,), daemon=True).start()
    return {"folder": folder, "clips": clips, "ffmpeg": bool(state["ffmpeg"]),
            "limitMB": DISCORD_LIMIT_MB, "windows": os.name == "nt", "version": VERSION,
            "trackNames": load_prefs().get("track_names", {}), "obsTrackNames": dict(_obs_track_names)}


def thumb_for(fp):
    st = os.stat(fp)
    key = hashlib.sha1(meta_key(fp, st).encode()).hexdigest()
    out = os.path.join(cache_dir("thumbs"), key + ".jpg")
    if os.path.isfile(out):
        return out
    ff = state["ffmpeg"]
    if not ff:
        return None
    with _thumb_sem:
        for seek in ("1", "0"):
            r = run([ff, "-hide_banner", "-loglevel", "error", "-y", "-ss", seek, "-i", fp,
                     "-frames:v", "1", "-vf", "scale=480:-2", "-q:v", "4", out], 30)
            if r.returncode == 0 and os.path.isfile(out) and os.path.getsize(out) > 0:
                return out
    return None


_pkt_cache = {}                 # meta key -> (video packets, audio packets); last few clips only
_pkt_lock = threading.Lock()


def packets_for(fp):
    """Every packet's time and size, read from the file index (no decoding, fast)."""
    key = meta_key(fp, os.stat(fp))
    with _pkt_lock:
        if key in _pkt_cache:
            return _pkt_cache[key]
    probe = state["ffprobe"]
    if not probe:
        return None
    out = []
    for sel in ("v:0", "a"):
        r = run([probe, "-v", "error", "-select_streams", sel, "-show_entries",
                 "packet=pts_time,dts_time,size,flags", "-of", "csv=p=0", fp], 60)
        pk = []
        for line in r.stdout.splitlines():
            parts = line.strip().split(",")
            if len(parts) < 4:
                continue
            t = parts[0] if parts[0] not in ("", "N/A") else parts[1]
            try:
                pk.append((float(t), int(parts[2]), "K" in parts[3]))
            except ValueError:
                continue
        pk.sort()
        out.append(pk)
    with _pkt_lock:
        _pkt_cache[key] = (out[0], out[1])
        while len(_pkt_cache) > 4:
            _pkt_cache.pop(next(iter(_pkt_cache)))
    return out[0], out[1]


def estimate_trim(name, start, end):
    """Bytes a lossless trim of [start, end] will take, the same way ffmpeg cuts it."""
    fp = resolve_clip(name)
    if not fp:
        raise ValueError("Clip not found.")
    pk = packets_for(fp)
    if not pk or not pk[0]:
        raise ValueError("Can't measure this clip.")
    video, audio = pk
    start, end = max(0.0, float(start)), float(end)
    first = video[0][0]
    kf = first
    for t, _size, key in video:          # a lossless cut has to begin on the keyframe before start
        if t > start + first + 1e-3:
            break
        if key:
            kf = t
    lo, hi = kf, start + first + (end - start)
    total = n = 0
    for t, size, _k in video:
        if lo - 1e-3 <= t < hi:
            total += size; n += 1
    for t, size, _k in audio:
        if lo - 0.05 <= t < hi:
            total += size; n += 1
    total = int(total * 1.003 + n * 24 + 8192)   # container overhead
    return {"bytes": total, "start": round(kf - first, 3)}


_track_cache = {}


def audio_tracks(fp):
    """The clip's audio tracks in order, with any names OBS wrote into the file."""
    key = meta_key(fp, os.stat(fp))
    if key in _track_cache:
        return _track_cache[key]
    tracks = []
    probe = state["ffprobe"]
    if probe:
        r = run([probe, "-v", "error", "-select_streams", "a", "-show_entries",
                 "stream=index,codec_name:stream_tags=title,handler_name", "-of", "json", fp], 30)
        try:
            for st in json.loads(r.stdout or "{}").get("streams", []):
                tags = st.get("tags") or {}
                nm = (tags.get("title") or tags.get("handler_name") or "").strip()
                low = nm.lower()
                if low in ("soundhandler", "sound media handler", "core media audio", "audio", "stereo", "") or \
                        low.startswith(("isomedia", "gpac")) or re.fullmatch(r"(audio\s*)?track\s*\d*", low):
                    nm = ""
                tracks.append({"name": nm, "codec": st.get("codec_name", "")})
        except ValueError:
            pass
    if len(_track_cache) > 50:
        _track_cache.clear()
    _track_cache[key] = tracks
    return tracks


def editable_tracks(n):
    """With one track it's the whole mix. With several, OBS convention: track 1 = everything, 2+ = separate sources."""
    return [0] if n <= 1 else list(range(1, n))


def audio_plan(mix, n):
    """How to build the exported audio: ('keep',), ('none',) or ('filter', filter_complex)."""
    mix = clean_mix(mix)
    if not mix or n == 0:
        return ("keep",)
    vols, mutes = mix["vols"], mix["mutes"]
    active = []
    for k in editable_tracks(n):
        v = vols[k] if k < len(vols) else 1.0
        m = mutes[k] if k < len(mutes) else False
        if not m and v > 0.001:
            active.append((k, v))
    if not active:
        return ("none",)
    boosted = any(v > 1.0 for _k, v in active) or len(active) > 1
    tail = ",alimiter=limit=0.97:level=0" if boosted else ""
    if len(active) == 1:
        k, v = active[0]
        return ("filter", "[0:a:%d]volume=%.3f%s[aout]" % (k, v, tail))
    parts = ["[0:a:%d]volume=%.3f[t%d]" % (k, v, k) for k, v in active]
    ins = "".join("[t%d]" % k for k, _v in active)
    return ("filter", ";".join(parts) + ";%samix=inputs=%d:normalize=0:duration=longest%s[aout]"
            % (ins, len(active), tail))


def audio_preview(name, k):
    """One audio track on its own, so the editor can play and mix tracks separately."""
    fp = resolve_clip(name)
    if not fp:
        return None
    n = len(audio_tracks(fp))
    if k < 0 or k >= n:
        return None
    key = hashlib.sha1(("audio|%d|" % k + meta_key(fp, os.stat(fp))).encode()).hexdigest()
    out = os.path.join(cache_dir("audio"), key + ".m4a")
    if os.path.isfile(out) and os.path.getsize(out) > 0:
        return out
    ff = state["ffmpeg"]
    if not ff:
        return None
    tmp = out + ".part.m4a"
    for codec in (["-c:a", "copy"], ["-c:a", "aac", "-b:a", "160k"]):
        r = run([ff, "-hide_banner", "-loglevel", "error", "-y", "-i", fp, "-map", "0:a:%d" % k, "-vn"]
                + codec + ["-movflags", "+faststart", tmp], 120)
        if r.returncode == 0 and os.path.isfile(tmp) and os.path.getsize(tmp) > 0:
            os.replace(tmp, out)
            return out
    try:
        os.remove(tmp)
    except OSError:
        pass
    return None


def wave_for(name, k):
    fp = resolve_clip(name)
    ff = state["ffmpeg"]
    if not fp or not ff or k < 0 or k >= len(audio_tracks(fp)):
        return None
    key = hashlib.sha1(("wave|%d|" % k + meta_key(fp, os.stat(fp))).encode()).hexdigest()
    out = os.path.join(cache_dir("thumbs"), key + ".png")
    if os.path.isfile(out) and os.path.getsize(out) > 0:
        return out
    with _thumb_sem:
        r = run([ff, "-hide_banner", "-loglevel", "error", "-y", "-i", fp, "-filter_complex",
                 "[0:a:%d]aformat=channel_layouts=mono,showwavespic=s=1600x80:colors=white:scale=sqrt" % k,
                 "-frames:v", "1", out], 120)
    if r.returncode == 0 and os.path.isfile(out) and os.path.getsize(out) > 0:
        return out
    return None


def packets_json(name):
    fp = resolve_clip(name)
    if not fp:
        raise ValueError("Clip not found.")
    pk = packets_for(fp)
    if not pk or not pk[0]:
        raise ValueError("Can't measure this clip.")
    video, audio = pk
    return {"tracks": audio_tracks(fp),
            "vt": [round(t, 4) for t, _s, _k in video], "vs": [s for _t, s, _k in video],
            "vk": [round(t, 4) for t, _s, k in video if k],
            "at": [round(t, 4) for t, _s, _k in audio], "as": [s for _t, s, _k in audio]}


def strip_for(fp):
    """A row of frames across the whole clip, shown behind the trim timeline."""
    ff = state["ffmpeg"]
    if not ff:
        return None
    st = os.stat(fp)
    key = hashlib.sha1(("strip|" + meta_key(fp, st)).encode()).hexdigest()
    out = os.path.join(cache_dir("thumbs"), key + ".jpg")
    if os.path.isfile(out):
        return out
    dur = duration_of(fp)
    if not dur:
        return None
    n = STRIP_FRAMES
    # Fast path: seek straight to n evenly spaced points and stitch them side by side
    fast = [ff, "-hide_banner", "-loglevel", "error", "-y"]
    for i in range(n):
        fast += ["-ss", "%.3f" % (dur * (i + 0.5) / n), "-i", fp]
    chains = ";".join("[%d:v:0]scale=192:108:force_original_aspect_ratio=increase,"
                      "crop=192:108,setsar=1,trim=end_frame=1[v%d]" % (i, i) for i in range(n))
    fast += ["-filter_complex", chains + ";" + "".join("[v%d]" % i for i in range(n)) +
             "hstack=inputs=%d" % n, "-frames:v", "1", "-q:v", "5", out]
    # Slow fallback: decode the whole clip
    slow = [ff, "-hide_banner", "-loglevel", "error", "-y", "-i", fp, "-an",
            "-vf", "fps=%d/%.3f,scale=192:108:force_original_aspect_ratio=increase,"
                   "crop=192:108,tile=%dx1" % (n, dur, n),
            "-frames:v", "1", "-q:v", "5", out]
    with _thumb_sem:
        for cmd in (fast, slow):
            r = run(cmd, 120)
            if r.returncode == 0 and os.path.isfile(out) and os.path.getsize(out) > 0:
                return out
    return None


_encoder = {"name": None}


def pick_encoder():
    """Use the graphics card's encoder when it works (much faster), otherwise the CPU (libx264)."""
    if _encoder["name"]:
        return _encoder["name"]
    ff = state["ffmpeg"]
    choice = "libx264"
    if ff:
        for enc in ("h264_nvenc", "h264_amf", "h264_qsv"):
            try:
                r = run([ff, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                         "-i", "color=c=black:s=640x360:r=30:d=0.5", "-c:v", enc, "-f", "null", "-"], 20)
                if r.returncode == 0:
                    choice = enc
                    break
            except Exception:
                pass
    _encoder["name"] = choice
    log("Compression encoder: %s" % choice)
    return choice


def encoder_args(enc, v):
    rate = ["-b:v", "%dk" % v, "-maxrate", "%dk" % int(v * 1.1), "-bufsize", "%dk" % (v * 2)]
    if enc == "h264_nvenc":
        return ["-c:v", enc, "-preset", "p5", "-rc", "cbr"] + rate
    if enc == "h264_amf":
        return ["-c:v", enc, "-quality", "quality", "-rc", "cbr"] + rate
    if enc == "h264_qsv":
        return ["-c:v", enc, "-preset", "medium"] + rate
    return ["-c:v", "libx264", "-preset", "veryfast"] + rate


def do_trim(d, out_path=None):
    fp = resolve_clip(d.get("name", ""))
    if not fp:
        raise ValueError("Clip not found.")
    ff = state["ffmpeg"]
    if not ff:
        raise ValueError("ffmpeg isn't installed, so trimming is unavailable.")
    start = max(0.0, float(d.get("start", 0)))
    end = float(d.get("end", 0))
    dur = end - start
    if dur < 0.3:
        raise ValueError("Selection is too short.")
    mode = d.get("mode", "copy")

    stem = os.path.splitext(os.path.basename(fp))[0]
    folder = os.path.dirname(fp)
    plan = audio_plan(d.get("mix"), len(audio_tracks(fp)))

    if mode == "fit":
        total_kbps = float(d.get("target_bytes") or FIT_TARGET_BYTES) * 8 / 1000 / dur * 0.94
        audio_kbps = 128 if total_kbps > 1000 else 64
        v = int(total_kbps - audio_kbps)
        if v < 250:
            raise ValueError("That selection is too long to fit under %d MB. Trim it shorter."
                             % DISCORD_LIMIT_MB)
        if v < 1200:
            height = 480
        elif v < 3000:
            height = 720
        else:
            height = 1080
        out = out_path or unique_path(folder, stem + " - discord", ".mp4")
        if plan[0] == "filter":
            amap = ["-filter_complex", plan[1], "-map", "0:v:0", "-map", "[aout]"]
        elif plan[0] == "none":
            amap = ["-map", "0:v:0"]
        else:
            amap = ["-map", "0:v:0", "-map", "0:a:0?"]
        acodec = ["-an"] if plan[0] == "none" else ["-c:a", "aac", "-b:a", "%dk" % audio_kbps]
        cmd = [ff, "-hide_banner", "-loglevel", "error", "-y",
               "-ss", "%.3f" % start, "-i", fp, "-t", "%.3f" % dur] + amap + [
               "-vf", "scale=-2:'min(%d,ih)'" % height] + encoder_args(pick_encoder(), v) + [
               "-pix_fmt", "yuv420p"] + acodec + ["-movflags", "+faststart", out]
        timeout = 900
    else:
        out = out_path or unique_path(folder, stem + " - trim", ".webm" if fp.lower().endswith(".webm") else ".mp4")
        webm = out.endswith(".webm")
        if plan[0] != "keep":
            # the copied video starts on the keyframe before `start`; start the remade audio there too
            try:
                kf = estimate_trim(d.get("name", ""), start, end)["start"]
                if 0 <= kf <= start:
                    start, dur = kf + 0.001, end - kf - 0.001
            except Exception:
                pass
        if plan[0] == "filter":      # video stays a lossless copy; only the audio is re-made
            amap = ["-filter_complex", plan[1], "-map", "0:v:0", "-map", "[aout]", "-c:v", "copy"] + \
                   (["-c:a", "libopus", "-b:a", "128k"] if webm else ["-c:a", "aac", "-b:a", "160k"])
        elif plan[0] == "none":
            amap = ["-map", "0:v:0", "-c:v", "copy", "-an"]
        else:
            amap = ["-map", "0:v:0", "-map", "0:a?", "-c", "copy"]
        cmd = [ff, "-hide_banner", "-loglevel", "error", "-y",
               "-ss", "%.3f" % start, "-i", fp, "-t", "%.3f" % dur] + amap + \
              ["-avoid_negative_ts", "make_zero"] + \
              ([] if webm else ["-movflags", "+faststart"]) + [out]
        timeout = 120

    r = run(cmd, timeout)
    if r.returncode != 0 or not os.path.isfile(out):
        try:
            os.remove(out)
        except OSError:
            pass
        msg = (r.stderr or "").strip().splitlines()
        raise ValueError("ffmpeg failed: " + (msg[-1] if msg else "unknown error"))
    if not out_path:
        push_event("created", os.path.basename(out))
    return {"ok": True, "name": os.path.basename(out), "size": os.path.getsize(out)}


def rename_clip(name, new):
    fp = resolve_clip(name)
    if not fp:
        raise ValueError("Clip not found.")
    ext = os.path.splitext(name)[1]
    new = BAD_NAME_CHARS.sub("", (new or "").strip()).strip().lstrip(".").rstrip(". ")
    if new.lower().endswith(ext.lower()):
        new = new[:-len(ext)].rstrip(". ")
    if not new:
        raise ValueError("Enter a name.")
    target = os.path.join(os.path.dirname(fp), new + ext)
    if os.path.normcase(target) == os.path.normcase(fp):
        return {"ok": True, "name": os.path.basename(target)}
    if os.path.exists(target):
        raise ValueError("A clip with that name already exists.")
    last = None
    for _ in range(6):  # the player may still hold the file for a moment
        try:
            os.rename(fp, target)
            forget_trim(name, os.path.basename(target))
            push_event("renamed", os.path.basename(target))
            return {"ok": True, "name": os.path.basename(target)}
        except OSError as e:
            last = e
            time.sleep(0.5)
    raise ValueError("Couldn't rename (file in use?) %s" % last)


def find_app_browser():
    """Chrome if installed, otherwise Edge (ships with Windows). App mode = no tabs or address bar."""
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    local = os.environ.get("LOCALAPPDATA", "")
    for p in [os.path.join(pf, "Google", "Chrome", "Application", "chrome.exe"),
              os.path.join(pf86, "Google", "Chrome", "Application", "chrome.exe"),
              os.path.join(local, "Google", "Chrome", "Application", "chrome.exe"),
              os.path.join(pf86, "Microsoft", "Edge", "Application", "msedge.exe"),
              os.path.join(pf, "Microsoft", "Edge", "Application", "msedge.exe")]:
        if os.path.isfile(p):
            return p
    for name in ("chrome", "google-chrome", "chromium", "msedge"):
        found = shutil.which(name)
        if found:
            return found
    return None


def open_fullscreen(name, t):
    """Play a clip fullscreen on the whole monitor (a dock can only fill itself)."""
    if not resolve_clip(name):
        raise ValueError("Clip not found.")
    exe = find_app_browser()
    if not exe:
        raise ValueError("No Edge or Chrome found for fullscreen.")
    url = "http://127.0.0.1:%d/watch?c=%s&t=%.2f" % (state["port"], urllib.parse.quote(name), float(t or 0))
    subprocess.Popen([exe, "--app=" + url, "--start-fullscreen",
                      "--user-data-dir=" + cache_dir("player"),
                      "--no-first-run", "--no-default-browser-check",
                      "--autoplay-policy=no-user-gesture-required"])
    return {"ok": True}


def ps_quote(path):
    return "'" + path.replace("'", "''") + "'"


_last_export = {"path": None}


def load_prefs():
    try:
        with open(os.path.join(cache_dir(), "prefs.json"), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def set_track_name(k, name):
    p = load_prefs()
    names = p.get("track_names", {})
    name = re.sub(r"[\x00-\x1f]", "", (name or "").strip())[:30]
    if name:
        names[str(int(k))] = name
    else:
        names.pop(str(int(k)), None)
    p["track_names"] = names
    save_prefs(p)
    return {"ok": True, "trackNames": names}


def save_prefs(p):
    try:
        with open(os.path.join(cache_dir(), "prefs.json"), "w", encoding="utf-8") as f:
            json.dump(p, f)
    except Exception:
        pass


def ask_save_path(default_dir, default_name, ext):
    """Show the normal Windows Save As window on top of everything; returns the chosen path or None."""
    if os.name != "nt":
        raise ValueError("The Save As window only works on Windows.")
    kind = "WebM video" if ext == ".webm" else "MP4 video"
    ps = ("[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
          "Add-Type -AssemblyName System.Windows.Forms; Add-Type -AssemblyName System.Drawing; "
          "$owner = New-Object System.Windows.Forms.Form; $owner.TopMost = $true; $owner.Opacity = 0; "
          "$owner.ShowInTaskbar = $false; $owner.StartPosition = 'CenterScreen'; "
          "$owner.Size = New-Object System.Drawing.Size(1,1); $owner.Show(); $owner.Activate(); "
          "$d = New-Object System.Windows.Forms.SaveFileDialog; "
          "$d.Title = 'Save trimmed clip'; "
          "$d.Filter = %s; $d.DefaultExt = %s; $d.AddExtension = $true; $d.OverwritePrompt = $true; "
          "$d.InitialDirectory = %s; $d.FileName = %s; "
          "if ($d.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) { [Console]::Out.Write($d.FileName) }; "
          "$owner.Close()") % (ps_quote("%s (*%s)|*%s" % (kind, ext, ext)), ps_quote(ext.lstrip(".")),
                                ps_quote(default_dir), ps_quote(default_name))
    r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-STA", "-Command", ps],
                       capture_output=True, encoding="utf-8", errors="replace",
                       creationflags=NO_WINDOW, timeout=1800)
    path = (r.stdout or "").strip()
    return path or None


def save_trim_as(d):
    fp = resolve_clip(d.get("name", ""))
    if not fp:
        raise ValueError("Clip not found.")
    ext = ".webm" if fp.lower().endswith(".webm") else ".mp4"
    prefs = load_prefs()
    start_dir = prefs.get("export_dir")
    if not start_dir or not os.path.isdir(start_dir):
        start_dir = os.path.dirname(fp)
    stem = os.path.splitext(os.path.basename(fp))[0]
    path = ask_save_path(start_dir, stem + " - trim" + ext, ext)
    if not path:
        return {"ok": True, "cancelled": True}
    if not path.lower().endswith(ext):
        path += ext
    if os.path.normcase(os.path.abspath(path)) == os.path.normcase(os.path.abspath(fp)):
        raise ValueError("Pick a different name. Saving over the original clip isn't allowed.")
    tmp = path + ".part" + ext                       # write beside it first, so a failure never wipes an existing file
    try:
        r = do_trim(d, out_path=tmp)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    prefs["export_dir"] = os.path.dirname(path)
    save_prefs(prefs)
    _last_export["path"] = path
    if os.path.normcase(os.path.dirname(os.path.abspath(path))) == os.path.normcase(os.path.abspath(state["folder"])):
        push_event("created", os.path.basename(path))   # saved into the clips folder: show it in the library
    return {"ok": True, "path": path, "name": os.path.basename(path), "size": r["size"]}


def reveal_last_export():
    p = _last_export["path"]
    if p and os.path.exists(p) and os.name == "nt":
        subprocess.Popen('explorer /select,"%s"' % p)
    return {"ok": True}


def set_clipboard_file(path):
    if os.name != "nt":
        raise ValueError("Copying files to the clipboard only works on Windows.")
    # Windows PowerShell 5.1 (powershell.exe) supports Set-Clipboard -LiteralPath
    r = run(["powershell.exe", "-NoProfile", "-NonInteractive", "-STA", "-Command",
             "Set-Clipboard -LiteralPath " + ps_quote(path)], 20)
    if r.returncode != 0:
        raise ValueError("Couldn't copy: " + (r.stderr or "").strip()[:200])


def copy_trimmed(d):
    """Trim into a temporary file (not the clip library) and put it on the clipboard."""
    fp = resolve_clip(d.get("name", ""))
    if not fp:
        raise ValueError("Clip not found.")
    share = cache_dir("share")
    now = time.time()
    for entry in os.scandir(share):                 # tidy up copies older than a day
        try:
            if now - entry.stat().st_mtime > 86400:
                if entry.is_dir():
                    shutil.rmtree(entry.path, ignore_errors=True)
                else:
                    os.remove(entry.path)
        except OSError:
            pass
    folder = os.path.join(share, str(int(now * 1000)))  # own folder so the file keeps the clip's name
    os.makedirs(folder, exist_ok=True)
    ext = ".webm" if fp.lower().endswith(".webm") else ".mp4"
    stem = os.path.splitext(os.path.basename(fp))[0]
    d = dict(d)
    if d.get("end") is None:                        # whole clip
        d["start"], d["end"] = 0, duration_of(fp) or 0
    limit = DISCORD_LIMIT_MB * 1_000_000
    out = os.path.join(folder, stem + ext)
    r = do_trim(dict(d, mode="copy"), out_path=out)   # instant, full quality
    compressed = False
    if r["size"] > limit:                            # too big: re-encode just this copy to fit
        os.remove(out)
        out = os.path.join(folder, stem + ".mp4")
        target = FIT_TARGET_BYTES
        for _attempt in range(3):
            r = do_trim(dict(d, mode="fit", target_bytes=target), out_path=out)
            if r["size"] <= limit:
                break
            target = int(target * 0.85)              # overshot a little: try again a bit smaller
        if r["size"] > limit:
            shutil.rmtree(folder, ignore_errors=True)
            raise ValueError("Couldn't get it under %d MB. Trim it shorter." % DISCORD_LIMIT_MB)
        compressed = True
    set_clipboard_file(out)
    return {"ok": True, "size": r["size"], "compressed": compressed}


def copy_to_clipboard(name):
    fp = resolve_clip(name)
    if not fp:
        raise ValueError("Clip not found.")
    if os.name != "nt":
        raise ValueError("Copying files to the clipboard only works on Windows.")
    # Windows PowerShell 5.1 (powershell.exe) supports Set-Clipboard -LiteralPath
    r = run(["powershell.exe", "-NoProfile", "-NonInteractive", "-STA", "-Command",
             "Set-Clipboard -LiteralPath " + ps_quote(fp)], 20)
    if r.returncode != 0:
        raise ValueError("Couldn't copy: " + (r.stderr or "").strip()[:200])
    return {"ok": True}


def reveal(name):
    fp = resolve_clip(name)
    if not fp:
        raise ValueError("Clip not found.")
    if os.name == "nt":
        subprocess.Popen('explorer /select,"%s"' % fp)
    return {"ok": True}


def delete_clips(names):
    """Move several clips to the Recycle Bin at once."""
    items = [(n, resolve_clip(n)) for n in (names or [])]
    items = [(n, fp) for n, fp in items if fp]
    if not items:
        raise ValueError("No clips to delete.")
    for i in range(0, len(items), 40):           # keep each PowerShell command a sane length
        chunk = items[i:i + 40]
        if os.name == "nt":
            run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                 "Add-Type -AssemblyName Microsoft.VisualBasic; foreach ($p in @(" +
                 ",".join(ps_quote(fp) for _n, fp in chunk) + ")) { try { "
                 "[Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile($p,'OnlyErrorDialogs','SendToRecycleBin') "
                 "} catch {} }"], 120)
        else:
            for _n, fp in chunk:
                try:
                    os.remove(fp)
                except OSError:
                    pass
    deleted = [n for n, fp in items if not os.path.exists(fp)]
    for n in deleted:
        forget_trim(n)
    failed = [n for n, fp in items if os.path.exists(fp)]
    if deleted:
        push_event("deleted", ", ".join(deleted)[:200])
    return {"ok": True, "deleted": len(deleted), "failed": failed}


def delete_clip(name):
    fp = resolve_clip(name)
    if not fp:
        raise ValueError("Clip not found.")
    last_err = ""
    for _ in range(6):  # the player may still hold the file for a moment
        if os.name == "nt":
            r = run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                     "Add-Type -AssemblyName Microsoft.VisualBasic; "
                     "[Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile(%s,"
                     "'OnlyErrorDialogs','SendToRecycleBin')" % ps_quote(fp)], 20)
            last_err = (r.stderr or "").strip()
        else:
            try:
                os.remove(fp)
            except OSError as e:
                last_err = str(e)
        if not os.path.exists(fp):
            forget_trim(name)
            push_event("deleted", name)
            return {"ok": True}
        time.sleep(0.5)
    raise ValueError("Couldn't delete (file in use?) " + last_err[:150])


# --------------------------------------------------------------------------
# Web server
# --------------------------------------------------------------------------

class QuietServer(ThreadingHTTPServer):
    """The player cancels downloads when you switch clips; that's normal, so don't report it."""
    daemon_threads = True
    allow_reuse_address = os.name != "nt"   # on Windows this would let an old copy share the port

    def handle_error(self, request, client_address):
        import sys
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
            return
        log("Request error: %s" % exc)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def setup(self):
        super().setup()
        with _conns_lock:
            _conns.add(self.connection)

    def handle(self):
        try:
            super().handle()
        except (ConnectionError, TimeoutError, OSError):
            pass

    def finish(self):
        try:
            super().finish()
        except (ConnectionError, TimeoutError, OSError):
            pass
        with _conns_lock:
            _conns.discard(self.connection)

    def _send(self, code, body=b"", ctype="application/json", headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode("utf-8"))

    def _image(self, path):
        if not path:
            return self._send(404, b"{}")
        with open(path, "rb") as f:
            data = f.read()
        self._send(200, data, "image/jpeg")

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(u.path)
        q = urllib.parse.parse_qs(u.query)
        try:
            if path in ("/", "/index.html"):
                self._send(200, PAGE.replace("__VERSION__", VERSION).encode("utf-8"), "text/html; charset=utf-8")
            elif path == "/api/clips":
                self._json(list_clips())
            elif path == "/api/events":
                self._json(get_events(int(q.get("since", ["0"])[0])))
            elif path.startswith("/video/"):
                self._serve_video(path[len("/video/"):])
            elif path.startswith("/thumb/"):
                fp = resolve_clip(path[len("/thumb/"):])
                self._image(thumb_for(fp) if fp else None)
            elif path == "/watch":
                self._send(200, WATCH_PAGE.encode("utf-8"), "text/html; charset=utf-8")
            elif path.startswith("/wave/"):
                wp = wave_for(path[len("/wave/"):], int(q.get("k", ["0"])[0]))
                if not wp:
                    self._send(404, b"{}")
                else:
                    with open(wp, "rb") as f:
                        self._send(200, f.read(), "image/png", headers={"Cache-Control": "max-age=86400"})
            elif path.startswith("/audio/"):
                k = int(q.get("k", ["0"])[0])
                ap = audio_preview(path[len("/audio/"):], k)
                if not ap:
                    self._send(404, b"{}")
                else:
                    self._serve_file(ap, "audio/mp4")
            elif path.startswith("/packets/"):
                self._json(packets_json(path[len("/packets/"):]))
            elif path.startswith("/strip/"):
                fp = resolve_clip(path[len("/strip/"):])
                self._image(strip_for(fp) if fp else None)
            else:
                self._send(404, b"{}")
        except (ConnectionError, BrokenPipeError):
            pass
        except Exception as e:
            try:
                self._json({"error": str(e)}, 500)
            except Exception:
                pass

    do_HEAD = do_GET

    def do_POST(self):
        # Custom header blocks other websites from triggering these actions
        if self.headers.get("X-ClipLib") != "1":
            return self._json({"error": "forbidden"}, 403)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(n) or b"{}")
            routes = {
                "/api/trim": lambda: do_trim(data),
                "/api/rename": lambda: rename_clip(data.get("name", ""), data.get("newName", "")),
                "/api/copy": lambda: copy_to_clipboard(data.get("name", "")),
                "/api/copy-trim": lambda: copy_trimmed(data),
                "/api/saveas": lambda: save_trim_as(data),
                "/api/reveal-export": reveal_last_export,
                "/api/trackname": lambda: set_track_name(data.get("k", 0), data.get("name", "")),
                "/api/setmix": lambda: set_mix(data.get("name", ""), data.get("mix")),
                "/api/settrim": lambda: set_trim(data.get("name", ""), data.get("start"), data.get("end")),
                "/api/reveal": lambda: reveal(data.get("name", "")),
                "/api/delete": lambda: delete_clip(data.get("name", "")),
                "/api/delete-many": lambda: delete_clips(data.get("names", [])),
                "/api/estimate": lambda: estimate_trim(data.get("name", ""), data.get("start", 0), data.get("end", 0)),
                "/api/watch": lambda: open_fullscreen(data.get("name", ""), data.get("t", 0)),
            }
            fn = routes.get(self.path)
            if not fn:
                return self._json({"error": "not found"}, 404)
            self._json(fn())
        except (ConnectionError, BrokenPipeError):
            pass
        except Exception as e:
            try:
                self._json({"error": str(e)}, 400)
            except Exception:
                pass

    def _serve_video(self, name):
        fp = resolve_clip(name)
        if not fp:
            return self._send(404, b"{}")
        ctype = "video/mp4"
        if fp.lower().endswith(".mkv"):
            ctype = "video/x-matroska"
        elif fp.lower().endswith(".webm"):
            ctype = "video/webm"
        elif fp.lower().endswith(".mov"):
            ctype = "video/quicktime"
        self._serve_file(fp, ctype)

    def _serve_file(self, fp, ctype):
        size = os.path.getsize(fp)
        start, end, code = 0, size - 1, 200
        rng = self.headers.get("Range")
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng.strip())
            if m and (m.group(1) or m.group(2)):
                s, e = m.groups()
                if s == "":
                    start = max(0, size - int(e))
                else:
                    start = int(s)
                    if e:
                        end = min(int(e), size - 1)
                code = 206
        if start > end or start >= size:
            return self._send(416, b"", headers={"Content-Range": "bytes */%d" % size})
        length = end - start + 1
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if code == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(fp, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(1 << 20, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)


def start_server():
    global _server, _server_port
    stop_server()
    try:
        _server = QuietServer(("127.0.0.1", state["port"]), Handler)
    except OSError as e:
        log("Couldn't open port %d (%s). Pick another port in the script settings."
            % (state["port"], e))
        _server = None
        return
    _server.daemon_threads = True
    _server_port = state["port"]
    threading.Thread(target=_server.serve_forever, daemon=True).start()
    log("v%s running at http://127.0.0.1:%d  (folder: %s, ffmpeg: %s)"
        % (VERSION, state["port"], state["folder"], state["ffmpeg"] or "NOT FOUND"))


def stop_server():
    global _server, _server_port
    if _server:
        try:
            _server.shutdown()
            _server.server_close()
        except Exception:
            pass
    with _conns_lock:
        conns = list(_conns)
        _conns.clear()
    for c in conns:
        try:
            c.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            c.close()
        except OSError:
            pass
    _server = None
    _server_port = None


# --------------------------------------------------------------------------
# OBS hooks
# --------------------------------------------------------------------------

def on_frontend_event(event):
    if event == obs.OBS_FRONTEND_EVENT_REPLAY_BUFFER_SAVED:
        path = ""
        try:
            path = obs.obs_frontend_get_last_replay() or ""
        except Exception:
            pass
        if path and not state["folder_setting"]:
            state["folder"] = os.path.dirname(path)
        push_event("saved", os.path.basename(path))
        play_sound()
    elif event == obs.OBS_FRONTEND_EVENT_PROFILE_CHANGED and not state["folder_setting"]:
        state["folder"] = detect_recording_folder()


def script_description():
    return ("<b>Clip Library v" + VERSION + "</b><br>A clip gallery and trimmer in its own OBS dock.<br><br>"
            "Add it via <b>Docks → Custom Browser Docks</b> with the URL "
            "<b>http://127.0.0.1:8765</b> (or whatever port you set below).")


def script_properties():
    props = obs.obs_properties_create()
    obs.obs_properties_add_path(props, "folder", "Clips folder (blank = OBS recording folder)",
                                obs.OBS_PATH_DIRECTORY, "", None)
    obs.obs_properties_add_int(props, "port", "Port", 1024, 65535, 1)
    obs.obs_properties_add_path(props, "ffmpeg", "ffmpeg.exe (blank = auto-detect)",
                                obs.OBS_PATH_FILE, "ffmpeg (ffmpeg.exe);;All files (*.*)", None)
    obs.obs_properties_add_bool(props, "sound", "Play a sound when a clip is saved")
    obs.obs_properties_add_path(props, "sound_file", "Custom sound (.wav, optional)",
                                obs.OBS_PATH_FILE, "WAV files (*.wav)", None)
    return props


def script_defaults(settings):
    obs.obs_data_set_default_int(settings, "port", 8765)
    obs.obs_data_set_default_bool(settings, "sound", True)


def script_update(settings):
    state["folder_setting"] = obs.obs_data_get_string(settings, "folder") or ""
    state["folder"] = state["folder_setting"] or detect_recording_folder()
    state["port"] = obs.obs_data_get_int(settings, "port") or 8765
    state["ffmpeg_setting"] = obs.obs_data_get_string(settings, "ffmpeg") or ""
    state["ffmpeg"] = find_ffmpeg()
    state["ffprobe"] = find_ffprobe(state["ffmpeg"])
    state["sound"] = obs.obs_data_get_bool(settings, "sound")
    state["sound_file"] = obs.obs_data_get_string(settings, "sound_file") or ""
    if _server is None or _server_port != state["port"]:
        start_server()


_obs_track_names = {}           # audio stream index -> names of the OBS sources on that track


def refresh_track_names():
    """Runs on OBS's own timer (main thread). Track t holds every audio source with mixer bit t-1 set."""
    try:
        found = {}
        seen = set()
        srcs = []
        for ch in range(1, 7):                       # Desktop Audio, Mic/Aux and friends
            src = obs.obs_get_output_source(ch)
            if src:
                srcs.append((src, True))
        listed = obs.obs_enum_sources() or []
        for src in listed:
            if obs.obs_source_get_output_flags(src) & obs.OBS_SOURCE_AUDIO and obs.obs_source_active(src):
                srcs.append((src, False))
        for src, _owned in srcs:
            name = obs.obs_source_get_name(src) or ""
            if not name or name in seen:
                continue
            seen.add(name)
            mixers = obs.obs_source_get_audio_mixers(src)
            for t in range(2, 7):
                if mixers & (1 << (t - 1)):
                    found.setdefault(str(t - 1), []).append(name)
        for src, owned in srcs:
            if owned:
                obs.obs_source_release(src)
        obs.source_list_release(listed)
        new = {k: " + ".join(v) for k, v in found.items()}
        if new != _obs_track_names:
            log("Track names from OBS: " + (", ".join("track %d = %s" % (int(k) + 1, v) for k, v in sorted(new.items()))
                                            or "none found (no source is on tracks 2-6)"))
        _obs_track_names.clear()
        _obs_track_names.update(new)
    except Exception:
        pass


def script_load(settings):
    load_meta()
    load_trims()
    load_mixes()
    obs.obs_frontend_add_event_callback(on_frontend_event)
    try:
        refresh_track_names()
        obs.timer_add(refresh_track_names, 5000)
    except Exception:
        pass


def script_unload():
    try:
        obs.timer_remove(refresh_track_names)
    except Exception:
        pass
    stop_server()


# --------------------------------------------------------------------------
# The dock page
# --------------------------------------------------------------------------

WATCH_PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>Clip</title>
<style>
html,body{margin:0;height:100%;background:#000;overflow:hidden;font:14px "Segoe UI",system-ui,sans-serif;color:#fff}
video{width:100%;height:100%;object-fit:contain;display:block;outline:none}
video::-webkit-media-controls-fullscreen-button{display:none}
#bar{position:fixed;top:0;left:0;right:0;display:flex;align-items:center;gap:12px;padding:16px 18px;
 background:linear-gradient(rgba(0,0,0,.7),transparent);transition:opacity .3s;z-index:2}
#name{flex:1;font-size:15px;font-weight:600;text-shadow:0 1px 3px #000;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#exit{background:rgba(30,33,40,.9);border:1px solid #555;color:#fff;padding:8px 16px;border-radius:8px;cursor:pointer;font:inherit}
#exit:hover{border-color:#476be6}
body.idle #bar{opacity:0}body.idle{cursor:none}
#go{position:fixed;inset:0;display:none;align-items:center;justify-content:center;background:rgba(0,0,0,.55);
 cursor:pointer;font-size:18px;z-index:3}
</style></head><body>
<video id="v" controls controlslist="nofullscreen nodownload noremoteplayback" disablepictureinpicture></video>
<div id="bar"><div id="name"></div><button id="exit">Exit fullscreen  (Esc)</button></div>
<div id="go">Click to go fullscreen</div>
<script>
const p = new URLSearchParams(location.search), v = document.getElementById('v');
const name = p.get('c') || '', t = parseFloat(p.get('t') || '0');
document.title = name.replace(/\.[^.]+$/, ''); document.getElementById('name').textContent = document.title;
v.src = '/video/' + encodeURIComponent(name);
v.addEventListener('loadedmetadata', () => { if (t > 0 && t < v.duration) v.currentTime = t; v.play().catch(() => {}); });
let wentFs = false;
function quit() {
  if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
  window.close();
  setTimeout(() => { document.body.innerHTML = '<p style="padding:30px;color:#aaa">You can close this window.</p>'; }, 400);
}
document.getElementById('exit').onclick = quit;
v.addEventListener('dblclick', quit);
document.addEventListener('keydown', e => {
  const k = e.key.toLowerCase();
  if (k === 'escape') { quit(); e.preventDefault(); return; }
  if (document.activeElement === v) return;  // the player's own controls handle keys
  if (k === ' ') { v.paused ? v.play() : v.pause(); e.preventDefault(); }
  else if (k === 'arrowleft') v.currentTime = Math.max(0, v.currentTime - 5);
  else if (k === 'arrowright') v.currentTime = Math.min(v.duration || 0, v.currentTime + 5);
});
document.addEventListener('fullscreenchange', () => { if (!document.fullscreenElement && wentFs) quit(); });
// if the window didn't open fullscreen by itself, one click gets it there
setTimeout(() => {
  if (innerHeight < screen.height - 40 && !document.fullscreenElement) {
    const g = document.getElementById('go'); g.style.display = 'flex';
    g.onclick = async () => { g.style.display = 'none';
      try { await document.documentElement.requestFullscreen(); wentFs = true; } catch (e) {} };
  }
}, 900);
let idle; function wake() { document.body.classList.remove('idle'); clearTimeout(idle);
  idle = setTimeout(() => document.body.classList.add('idle'), 2500); }
document.addEventListener('mousemove', wake); wake();
</script></body></html>
"""

PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Clips</title>
<style>
:root{--bg:#16181d;--panel:#1f2229;--panel2:#272b34;--panel3:#30353f;--line:#343944;--text:#e8eaf0;--dim:#9299a8;
--accent:#476be6;--accent2:#5c7cf0;--accentbg:rgba(71,107,230,.16);--ok:#2fbf71;--bad:#e5484d;--warn:#e8a33d}
*{box-sizing:border-box}html,body{margin:0;height:100%}
body{background:var(--bg);color:var(--text);font:13px/1.4 "Segoe UI",system-ui,sans-serif;overflow:hidden}
button{font:inherit;color:var(--text);background:var(--panel2);border:1px solid var(--line);border-radius:7px;
 padding:7px 11px;cursor:pointer;white-space:nowrap;transition:background .12s,border-color .12s}
button:hover{border-color:var(--accent)}button:disabled{opacity:.45;cursor:default;border-color:var(--line)}
button.primary{background:var(--accent);border-color:var(--accent)}button.primary:hover:not(:disabled){background:var(--accent2)}
button.danger{color:#ffb4b6}button.danger.armed{background:var(--bad);color:#fff;border-color:var(--bad)}
button.icon{padding:5px 9px}
button small{display:block;font-size:11px;opacity:.75;font-weight:400}
button.wide{width:100%;text-align:left;padding:9px 12px;white-space:normal}
kbd{font:11px "Segoe UI",sans-serif;background:var(--panel3);border:1px solid var(--line);border-bottom-width:2px;
 border-radius:4px;padding:0 4px;margin-left:4px;color:var(--dim)}
input{font:inherit;color:var(--text);background:var(--panel2);border:1px solid var(--line);border-radius:7px;padding:6px 9px;outline:none}
input:focus{border-color:var(--accent)}
.view{position:absolute;inset:0;display:flex;flex-direction:column}
.view[hidden]{display:none}
.top{display:flex;align-items:center;gap:8px;padding:8px 12px;border-bottom:1px solid var(--line);background:var(--panel);flex-wrap:wrap}
.top h1{font-size:15px;margin:0;font-weight:600}.top .count{color:var(--dim)}.sp{flex:1}
.top .ttl{font-weight:600;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}
#search{width:200px}
.ver{color:var(--dim);font-size:11px;opacity:.7}
.lrow{display:flex;justify-content:space-between;align-items:center}
button.link{background:none;border:0;padding:0;color:var(--dim);font-size:11px;letter-spacing:0;text-transform:none}
button.link:hover{color:var(--text)}button.link[hidden]{display:none}
.tl{display:flex;flex-direction:column;gap:6px;flex:none}
.tlrow{display:flex;gap:8px;align-items:stretch}
.tlsp{width:var(--lw,0px);flex:none}
.tlab{flex:none;display:flex;align-items:center;gap:6px;padding:0 8px;background:var(--panel);
 border:1px solid var(--line);border-radius:8px;min-width:0;cursor:default}
.tlab.vid{justify-content:space-between}
.tlab .tn{font-size:12px;flex:none;width:var(--nw,auto);max-width:140px;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;border-radius:4px;padding:1px 3px}
.tlab .tn.edit{cursor:text}.tlab .tn.edit:hover{background:var(--panel2)}
.tlab input.tname{flex:1;min-width:0;padding:1px 5px;font-size:12px}
.tlab input[type=range]{width:64px;flex:none;margin:0;padding:0;height:16px;background:transparent;-webkit-appearance:none;appearance:none;cursor:pointer;--f:.5}
.tlab input[type=range]:focus,.tlab input[type=range]:focus-visible{outline:none;box-shadow:none;border:0}
.tlab input[type=range]::-webkit-slider-runnable-track{height:4px;border-radius:2px;border:0;
 background:linear-gradient(to right,var(--tc,var(--accent)) 0,var(--tc,var(--accent)) calc(6px + (100% - 12px) * var(--f)),var(--panel3) calc(6px + (100% - 12px) * var(--f)),var(--panel3) 100%)}
.tlab input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;appearance:none;width:12px;height:12px;margin-top:-4px;border-radius:50%;
 background:#fff;border:2px solid var(--tc,var(--accent));box-shadow:0 1px 3px rgba(0,0,0,.4)}
.tlab .tv{font-size:11px;color:var(--dim);font-variant-numeric:tabular-nums;width:34px;text-align:right;flex:none}
.spk{flex:none;width:26px;height:24px;padding:0;display:flex;align-items:center;justify-content:center;border-radius:6px;
 background:transparent;border:1px solid transparent;color:var(--tc,var(--text))}
.spk:hover{border-color:var(--line);background:var(--panel2)}
.spk svg{width:16px;height:16px}
.muted .spk{color:var(--bad)}
.muted .tn,.muted .tv,.muted input[type=range]{opacity:.45}
.tlrow #track{flex:1 1 auto;min-width:0;width:auto}
#lanes{display:flex;flex-direction:column;gap:6px}
.lane{position:relative;width:100%;min-width:0;height:36px;border-radius:7px;background:var(--panel);border:1px solid var(--line);
 overflow:hidden;cursor:pointer}
.lane .wv{position:absolute;inset:3px 0;background:var(--tc);opacity:.18;transform-origin:center;transition:transform .08s,opacity .15s}
.lane .wv.ready{opacity:.95;-webkit-mask-size:100% 100%;mask-size:100% 100%;-webkit-mask-repeat:no-repeat;mask-repeat:no-repeat}
.lane.muted .wv{opacity:.22;filter:grayscale(1)}
.lane .lsh{position:absolute;top:0;bottom:0;background:rgba(10,11,14,.68);pointer-events:none}
.lane .lsh.l{left:0}.lane .lsh.r{right:0}
.lane .lph{position:absolute;top:0;bottom:0;width:2px;margin-left:-1px;background:#fff;pointer-events:none;box-shadow:0 0 4px rgba(0,0,0,.6)}
@media (max-width:760px){.tlab .tn{max-width:80px}.tlab input[type=range]{width:46px}.tlab .tv{display:none}}
.notice{margin:10px 12px 0;padding:9px 12px;border-radius:7px;background:#3a2f1a;color:#f3d39b;font-size:12px}
.notice[hidden]{display:none}
.scroll{flex:1;overflow:auto}
#grid{padding:4px 14px 20px}
.day{font-size:12px;text-transform:uppercase;letter-spacing:.07em;color:var(--dim);margin:16px 2px 8px;font-weight:600}
.day span{font-weight:400;text-transform:none;letter-spacing:0;margin-left:6px}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:12px}
@media (max-width:620px){.cards{grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:10px}#search{width:100%;order:9}}
.card{background:var(--panel);border:1px solid var(--line);border-radius:9px;overflow:hidden;cursor:pointer;
 transition:border-color .15s,transform .15s}
.card:hover{border-color:var(--accent);transform:translateY(-2px)}
.sel{position:absolute;top:7px;left:7px;width:22px;height:22px;border-radius:6px;border:2px solid rgba(255,255,255,.9);
 background:rgba(0,0,0,.45);z-index:2;opacity:0;transition:opacity .12s,background .12s;cursor:pointer;
 display:flex;align-items:center;justify-content:center}
.sel:hover{background:rgba(71,107,230,.55)}
.card:hover .sel,.selecting .sel{opacity:1}
.card.picked .sel{background:var(--accent);border-color:var(--accent)}
.card.picked .sel::after{content:"";width:5px;height:10px;border:solid #fff;border-width:0 2px 2px 0;transform:translateY(-1px) rotate(45deg)}
.card.picked{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent)}
.selecting .thumb::after{display:none}
#selBar{display:flex;align-items:center;gap:8px}#selBar[hidden]{display:none}
#selCount{color:var(--text);font-weight:600}
.card.new{animation:pulse 1.6s ease-out 2}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(47,191,113,.8)}100%{box-shadow:0 0 0 12px rgba(47,191,113,0)}}
.thumb{position:relative;aspect-ratio:16/9;background:#000 center/36% no-repeat}
.thumb img{width:100%;height:100%;object-fit:cover;display:block}
.noimg{background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='%23555'%3E%3Cpath d='M8 5v14l11-7z'/%3E%3C/svg%3E")}
.noimg img{display:none}
.thumb::after{content:"";position:absolute;inset:0;background:rgba(0,0,0,.35) url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='white'%3E%3Cpath d='M8 5v14l11-7z'/%3E%3C/svg%3E") center/42px no-repeat;opacity:0;transition:opacity .15s}
.card:hover .thumb::after{opacity:1}
.badge{position:absolute;bottom:6px;background:rgba(0,0,0,.78);padding:1px 7px;border-radius:5px;font-size:11px;z-index:1}
.badge.size{right:6px}.badge.dur{left:6px}.badge.over{background:var(--bad)}
.meta{padding:8px 10px}.name{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-weight:500}
.sub{color:var(--dim);font-size:11px;margin-top:2px}
.menu{position:fixed;z-index:60;background:var(--panel3);border:1px solid var(--line);border-radius:8px;padding:4px;
 min-width:180px;box-shadow:0 10px 30px rgba(0,0,0,.55)}
.menu button{display:block;width:100%;text-align:left;border:0;background:transparent;padding:7px 10px;border-radius:5px}
.menu button:hover{background:var(--accentbg)}.menu button.danger:hover,.menu button.danger.armed{background:rgba(229,72,77,.25);color:#fff}
.menu hr{border:0;border-top:1px solid var(--line);margin:4px 2px}
input.rename{width:100%;padding:2px 6px;font:inherit;font-weight:500}
.empty{text-align:center;color:var(--dim);padding:60px 10px}

/* ---- editor ---- */
.ed{flex:1;min-height:0;display:grid;grid-template-columns:minmax(0,1fr);overflow:auto}
.list{display:none}
.main{display:flex;flex-direction:column;gap:10px;padding:12px;min-width:0;overflow-x:hidden}
.stage{background:#000;border-radius:9px;overflow:hidden;display:flex;align-items:center;justify-content:center;aspect-ratio:16/9;position:relative}
video{width:100%;height:100%;object-fit:contain;display:block;cursor:pointer}
video::-webkit-media-controls-fullscreen-button{display:none}
.stage:fullscreen{border-radius:0;background:#000}
.stage.theater{position:fixed;inset:0;z-index:30;border-radius:0;aspect-ratio:auto}
.side{padding:0 12px 14px;display:flex;flex-direction:column;gap:18px;min-width:0}
#grip{height:10px;margin:-6px 0 -4px;cursor:row-resize;display:flex;align-items:center;justify-content:center;flex:none;touch-action:none}
#grip span{width:46px;height:4px;border-radius:2px;background:var(--line);transition:background .12s}
#grip:hover span,#grip.drag span{background:var(--accent)}
.stage.sized{flex:none!important;aspect-ratio:auto!important;height:var(--sh)}
@media (min-width:880px){
 .ed{grid-template-columns:minmax(0,1fr) 300px;grid-template-rows:minmax(0,1fr);overflow:hidden}
 .main{min-height:0;overflow-y:auto;overflow-x:hidden;scrollbar-width:none}
 .main::-webkit-scrollbar{display:none;width:0;height:0}
 .stage{flex:1;min-height:0;aspect-ratio:auto}
 .side{padding:12px 12px 12px 0;overflow:auto}
}
@media (min-width:1250px){.ed{grid-template-columns:minmax(0,1fr) 340px}}

#track{position:relative;height:60px;flex:none;background:var(--panel);border:1px solid var(--line);border-radius:8px;
 cursor:pointer;touch-action:none;user-select:none;overflow:visible}
#strip{position:absolute;inset:0;width:100%;height:100%;object-fit:fill;border-radius:7px;opacity:.9;pointer-events:none}
#strip[hidden]{display:none}
.shade{position:absolute;top:0;bottom:0;background:rgba(10,11,14,.72);pointer-events:none}
#shL{left:0;border-radius:7px 0 0 7px}#shR{right:0;border-radius:0 7px 7px 0}
#selbox{position:absolute;top:-1px;bottom:-1px;border-top:3px solid var(--accent);border-bottom:3px solid var(--accent);pointer-events:none}
.handle{position:absolute;top:-4px;bottom:-4px;width:14px;margin-left:-7px;background:var(--accent);border-radius:5px;
 cursor:ew-resize;z-index:2;box-shadow:0 0 0 1px rgba(0,0,0,.3)}
.handle::after{content:"";position:absolute;left:6px;top:32%;bottom:32%;width:2px;background:rgba(255,255,255,.8);border-radius:1px}
#ph{position:absolute;top:-6px;bottom:-6px;width:2px;margin-left:-1px;background:#fff;pointer-events:none;z-index:3;box-shadow:0 0 4px rgba(0,0,0,.6)}
#hov{position:absolute;top:-26px;transform:translateX(-50%);background:var(--panel3);border:1px solid var(--line);
 padding:1px 6px;border-radius:5px;font-size:11px;pointer-events:none;display:none;z-index:4;font-variant-numeric:tabular-nums}
.transport{display:flex;flex-wrap:nowrap;gap:6px;align-items:center}
.transport .time{flex:none;white-space:nowrap}
.tright{display:flex;align-items:center;gap:6px;flex:none}
#play{width:42px;height:36px;padding:0;font-size:14px}
.time{font-variant-numeric:tabular-nums;color:var(--dim);margin:0 6px}.time b{color:var(--text);font-weight:600}
label.chk{display:flex;align-items:center;gap:5px;color:var(--dim);cursor:pointer;margin-left:4px}
.box{background:var(--panel);border:1px solid var(--line);border-radius:9px;padding:12px;display:flex;flex-direction:column;gap:9px}
.label{font-size:11px;text-transform:uppercase;letter-spacing:.07em;color:var(--dim);font-weight:600}
#nameInput{width:100%}
.meta2{color:var(--dim);font-size:12px;display:flex;flex-wrap:wrap;gap:4px 12px}
.stats{display:grid;grid-template-columns:1fr 1fr;gap:6px}
.stat{background:var(--panel2);border-radius:7px;padding:6px 9px}
.stat div:first-child{font-size:11px;color:var(--dim)}.stat div+div{font-weight:600;font-variant-numeric:tabular-nums}
.stat.over div+div{color:var(--bad)}
.bar{height:6px;background:var(--panel3);border-radius:4px;overflow:hidden}
#barFill{height:100%;background:var(--ok);width:0;transition:width .15s,background .15s}
#barFill.over{background:var(--bad)}
.hint{color:var(--dim);font-size:11px}
.row{display:flex;gap:6px;flex-wrap:wrap}.row button{flex:1}
.keys{color:var(--dim);font-size:11px;line-height:1.8;padding:0 2px}
#toasts{position:fixed;top:10px;left:50%;transform:translateX(-50%);display:flex;flex-direction:column;gap:6px;
 z-index:50;width:min(440px,calc(100% - 16px))}
.toast{background:var(--panel3);border:1px solid var(--line);border-left:4px solid var(--accent);border-radius:9px;
 padding:9px 11px;box-shadow:0 8px 24px rgba(0,0,0,.5);display:flex;gap:8px;align-items:center;animation:in .2s ease-out}
.toast.ok{border-left-color:var(--ok)}.toast.bad{border-left-color:var(--bad)}
.toast .t{font-weight:600}.toast .s{color:var(--dim);font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.toast .txt{flex:1;min-width:0}
@keyframes in{from{opacity:0;transform:translateY(-6px)}to{opacity:1;transform:none}}
.busy{position:fixed;inset:0;background:rgba(0,0,0,.55);display:flex;align-items:center;justify-content:center;
 z-index:40;flex-direction:column;gap:12px}
.busy[hidden]{display:none}
.spin{width:30px;height:30px;border:3px solid #fff3;border-top-color:#fff;border-radius:50%;animation:sp 1s linear infinite}
@keyframes sp{to{transform:rotate(360deg)}}
</style></head><body>

<div id="toasts"></div>
<div class="busy" id="busy" hidden><div class="spin"></div><div id="busyText">Working…</div></div>

<section class="view" id="library">
  <div class="top">
    <span class="count" id="count"></span><span class="ver">v__VERSION__</span><span class="sp"></span>
    <input id="search" type="search" placeholder="Search clips…" spellcheck="false">
    <span id="selBar" hidden><span id="selCount"></span><button id="selClear">Clear</button>
      <button class="danger" id="selDel">Delete</button></span>
    <button id="refresh" title="Refresh">Refresh</button>
  </div>
  <div class="notice" id="ffNotice" hidden>ffmpeg wasn't found, so thumbnails and trimming are off.
    Install it with <b>winget install Gyan.FFmpeg</b>, then reload the script in Tools → Scripts.</div>
  <div class="scroll"><div id="grid"></div></div>
</section>

<section class="view" id="editor" hidden>
  <div class="top">
    <button class="icon" id="back" title="Back to library (Esc)">← Library</button>
  </div>
  <div class="ed">
    <div class="main">
      <div class="stage" id="stage"><video id="video" playsinline preload="auto" controlslist="nofullscreen nodownload" disablepictureinpicture></video></div>
      <div id="grip" title="Drag to resize the video. Double-click to fit."><span></span></div>
      <div class="tl">
        <div class="tlrow"><div class="tlsp"></div>
          <div id="track"><img id="strip" alt="" hidden><div class="shade" id="shL"></div><div class="shade" id="shR"></div>
          <div id="selbox"></div><div class="handle" id="hA" title="Drag to set start"></div><div class="handle" id="hB" title="Drag to set end"></div>
          <div id="ph"></div><div id="hov"></div></div></div>
        <div id="lanes"></div>
      </div>
      <div class="transport">
        <button class="primary" id="play" title="Play / pause (Space)">▶</button>
        <span class="time" id="time"></span>
        <span class="sp"></span>
        <span class="tright"><label class="chk"><input type="checkbox" id="loop" checked> Loop</label>
        <button id="fs">Fullscreen</button></span>
      </div>
    </div>
    <aside class="side">
      <div class="box">
        <div class="label">Name</div>
        <input id="nameInput" spellcheck="false" title="Click to rename, Enter to save">
        <div class="meta2" id="meta2"></div>
      </div>
      <div class="box">
        <div class="label">Trim</div>
        <div class="stats" id="stats"></div>
        <div class="bar"><div id="barFill"></div></div>
        <div class="hint" id="barHint"></div>
        <button class="primary wide" id="saveCopy">Save trim as…</button>
      </div>
      <div class="box">
        <div class="label">Share</div>
        <button class="primary wide" id="copy">Copy for Discord<small id="copyHint">Then press Ctrl+V in Discord</small></button>
        <div class="row"><button id="reveal">Show in folder</button><button class="danger" id="del">Delete</button></div>
      </div>
    </aside>
  </div>
</section>

<script>
const $ = s => document.querySelector(s);
const video = $('#video'), track = $('#track');
let info = {clips: [], limitMB: 20, ffmpeg: true, windows: true};
let lastSig = '', lastEvent = null, current = null, dur = 0, sel = {a: 0, b: 0}, newNames = new Set(), restore = null;
let query = '', renaming = false;
const LIMIT = () => info.limitMB * 1e6;
const PAGE_VERSION = '__VERSION__';
let updateOffered = false;
function checkVersion() {
  if (!info.version || info.version === PAGE_VERSION) return;
  if (!current) { location.reload(); return; }          // in the library: just refresh to the new version
  if (updateOffered) return; updateOffered = true;       // mid-edit: let them finish first
  toast('Clip Library updated to v' + info.version, 'Reload to use the new version.', '', {label: 'Reload', fn: () => location.reload()}, 600000);
}

async function api(path, body) {
  const opt = body ? {method: 'POST', headers: {'Content-Type': 'application/json', 'X-ClipLib': '1'},
                      body: JSON.stringify(body)} : {cache: 'no-store'};
  const r = await fetch(path, opt);
  let j = {}; try { j = await r.json(); } catch (e) {}
  if (!r.ok || j.error) throw new Error(j.error || ('HTTP ' + r.status));
  return j;
}
const enc = encodeURIComponent;
const stem = n => n.replace(/\.[^.]+$/, '');
const mb = b => b >= 1e9 ? (b / 1e9).toFixed(2) + ' GB' : (b / 1e6).toFixed(1) + ' MB';
function tfmt(s, dec = 1) { if (!isFinite(s) || s < 0) s = 0; const m = Math.floor(s / 60), x = s - m * 60;
  return m + ':' + (x < 10 ? '0' : '') + x.toFixed(dec); }
const dfmt = s => s ? tfmt(s, 0) : '';
function clock(t) { return new Date(t * 1000).toLocaleTimeString(undefined, {hour: 'numeric', minute: '2-digit'}); }
function dayLabel(t) {
  const d = new Date(t * 1000), n = new Date();
  const diff = Math.round((new Date(n.getFullYear(), n.getMonth(), n.getDate()) - new Date(d.getFullYear(), d.getMonth(), d.getDate())) / 864e5);
  if (diff === 0) return 'Today'; if (diff === 1) return 'Yesterday';
  return d.toLocaleDateString(undefined, {weekday: 'long', month: 'short', day: 'numeric'});
}
function el(tag, cls, text) { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }

function toast(title, sub, kind, action, ms) {
  const t = el('div', 'toast ' + (kind || '')), txt = el('div', 'txt');
  txt.append(el('div', 't', title)); if (sub) txt.append(el('div', 's', sub)); t.append(txt);
  if (action) { const b = el('button', '', action.label); b.onclick = () => { t.remove(); action.fn(); }; t.append(b); }
  $('#toasts').prepend(t); setTimeout(() => t.remove(), ms || 4500);
}
function busy(text) { $('#busyText').textContent = text || ''; $('#busy').hidden = !text; }

function thumbEl(c, cls) {
  const th = el('div', cls + (info.ffmpeg ? '' : ' noimg'));
  if (info.ffmpeg) {
    const img = el('img'); img.loading = 'lazy'; img.alt = '';
    let tries = 0; const src = '/thumb/' + enc(c.name) + '?m=' + c.mtime;
    img.onerror = () => { if (tries++ < 2) setTimeout(() => img.src = src + '&r=' + tries, 1500); else th.classList.add('noimg'); };
    img.src = src; th.append(img);
  }
  return th;
}

// ---------------- Library ----------------
function visible() {
  const q = query.toLowerCase();
  return info.clips.filter(c => !q || c.name.toLowerCase().includes(q));
}
function groups(list) {
  const out = []; let cur = null;
  for (const c of list) { const d = dayLabel(c.mtime); if (!cur || cur.label !== d) { cur = {label: d, items: []}; out.push(cur); } cur.items.push(c); }
  return out;
}
async function loadClips(force) {
  if (renaming) return;
  try { info = await api('/api/clips'); } catch (e) { return; }
  checkVersion();
  $('#ffNotice').hidden = info.ffmpeg;
  $('#count').textContent = info.clips.length ? info.clips.length + (info.clips.length === 1 ? ' clip' : ' clips') : '';
  if (current) { const c = info.clips.find(x => x.name === current.name); if (c) { current = c; drawInfo(); } }
  const sig = info.clips.map(c => c.name + c.size + c.mtime + c.duration).join('|');
  if (sig === lastSig && !force) return;
  lastSig = sig; render();
}
// ---------- multi-select ----------
const picked = new Set();
function togglePick(name, card) {
  if (picked.has(name)) picked.delete(name); else picked.add(name);
  if (card) card.classList.toggle('picked', picked.has(name));
  updateSel();
}
function clearPicks() { picked.clear(); document.querySelectorAll('.card.picked').forEach(c => c.classList.remove('picked')); updateSel(); }
let selArm = null;
function disarmSel() { clearTimeout(selArm); selArm = null; $('#selDel').classList.remove('armed'); $('#selDel').textContent = 'Delete'; }
function updateSel() {
  for (const n of [...picked]) if (!info.clips.some(c => c.name === n)) picked.delete(n);
  $('#library').classList.toggle('selecting', picked.size > 0);
  $('#selBar').hidden = !picked.size;
  $('#selCount').textContent = picked.size + ' selected';
  disarmSel();
}
$('#selClear').onclick = clearPicks;
$('#selDel').onclick = async () => {
  const b = $('#selDel'), n = picked.size; if (!n) return;
  if (!selArm) { b.classList.add('armed'); b.textContent = 'Delete ' + n + (n === 1 ? ' clip?' : ' clips?');
    selArm = setTimeout(disarmSel, 3000); return; }
  disarmSel(); busy('Moving ' + n + (n === 1 ? ' clip' : ' clips') + ' to Recycle Bin…');
  try {
    const r = await api('/api/delete-many', {names: [...picked]}); busy('');
    toast('Moved ' + r.deleted + (r.deleted === 1 ? ' clip' : ' clips') + ' to Recycle Bin',
          r.failed.length ? r.failed.length + ' couldn\'t be deleted (in use?)' : '', r.failed.length ? 'bad' : 'ok');
    picked.clear(); r.failed.forEach(f => picked.add(f));
    await loadClips(true); updateSel();
  } catch (e) { busy(''); toast('Delete failed', e.message, 'bad'); }
};

function render() {
  const g = $('#grid'), sc = g.parentNode.scrollTop; g.innerHTML = '';
  const list = visible();
  if (!list.length) {
    const e = el('div', 'empty', info.clips.length ? 'No clips match.' : 'No clips yet. Press your Save Replay hotkey and they will show up here.');
    if (info.folder && !info.clips.length) { const f = el('div', 'hint', 'Watching: ' + info.folder); f.style.marginTop = '8px'; e.append(f); }
    g.append(e); return;
  }
  for (const grp of groups(list)) {
    const h = el('div', 'day', grp.label); h.append(el('span', '', grp.items.length + (grp.items.length === 1 ? ' clip' : ' clips')));
    const cards = el('div', 'cards');
    for (const c of grp.items) {
      const card = el('div', 'card' + (newNames.has(c.name) ? ' new' : ''));
      const th = thumbEl(c, 'thumb');
      if (picked.has(c.name)) card.classList.add('picked');
      const box = el('div', 'sel'); box.title = 'Select';
      box.onclick = ev => { ev.stopPropagation(); togglePick(c.name, card); };
      th.append(box);
      if (c.duration) th.append(el('span', 'badge dur', dfmt(c.duration)));
      const bd = el('span', 'badge size' + (c.size > LIMIT() ? ' over' : ''), mb(c.size));
      bd.title = c.size > LIMIT() ? 'Over Discord\'s free ' + info.limitMB + ' MB limit' : 'Fits on Discord'; th.append(bd);
      const meta = el('div', 'meta'); const n = el('div', 'name', stem(c.name)); n.title = c.name;
      meta.append(n, el('div', 'sub', clock(c.mtime)));
      card.append(th, meta);
      card.onclick = () => { if (renaming) return; if (picked.size) togglePick(c.name, card); else openClip(c.name); };
      card.oncontextmenu = ev => showMenu(ev, c, n);
      cards.append(card);
    }
    g.append(h, cards);
  }
  g.parentNode.scrollTop = sc;
  updateSel();
  setTimeout(() => newNames.clear(), 4000);
}
$('#search').oninput = e => { query = e.target.value; render(); };
$('#refresh').onclick = () => loadClips(true);

// ---------------- Right-click menu ----------------
let menuEl = null;
function hideMenu() { if (menuEl) { menuEl.remove(); menuEl = null; } }
function showMenu(ev, c, nameEl) {
  ev.preventDefault(); hideMenu();
  const m = el('div', 'menu'); menuEl = m;
  const item = (label, fn, cls) => { const b = el('button', cls || '', label);
    b.onclick = e => { e.stopPropagation(); fn(b); }; m.append(b); return b; };
  item('Open', () => { hideMenu(); openClip(c.name); });
  item('Rename', () => { hideMenu(); startRename(c, nameEl); });
  if (info.windows) item('Copy for Discord', () => { hideMenu(); copyClip(c.name); });
  item('Show in folder', () => { hideMenu(); api('/api/reveal', {name: c.name}).catch(e => toast('Error', e.message, 'bad')); });
  m.append(el('hr'));
  item('Delete', b => {
    if (!b.classList.contains('armed')) { b.classList.add('armed'); b.textContent = 'Click again to delete'; return; }
    hideMenu(); deleteClip(c.name);
  }, 'danger');
  document.body.append(m);
  const r = m.getBoundingClientRect();
  m.style.left = Math.min(ev.clientX, innerWidth - r.width - 6) + 'px';
  m.style.top = Math.min(ev.clientY, innerHeight - r.height - 6) + 'px';
}
document.addEventListener('mousedown', e => { if (menuEl && !menuEl.contains(e.target)) hideMenu(); });
document.addEventListener('scroll', hideMenu, true);
window.addEventListener('resize', hideMenu); window.addEventListener('blur', hideMenu);

function startRename(c, nameEl) {
  if (!nameEl || renaming) return;
  renaming = true;
  const input = el('input', 'rename'); input.value = stem(c.name); input.spellcheck = false;
  const old = nameEl.textContent; nameEl.textContent = ''; nameEl.append(input);
  input.focus(); input.select();
  let done = false;
  const finish = async save => {
    if (done) return; done = true;
    const v = input.value.trim();
    renaming = false;
    if (!save || !v || v === stem(c.name)) { nameEl.textContent = old; return; }
    nameEl.textContent = v;
    if (current && current.name === c.name) unloadVideo();
    try {
      const r = await api('/api/rename', {name: c.name, newName: v});
      toast('Renamed', r.name, 'ok');
      await loadClips(true);
      if (current && current.name === c.name) openClip(r.name);
    } catch (e) { nameEl.textContent = old; toast('Rename failed', e.message, 'bad');
      if (current && current.name === c.name) openClip(c.name); }
  };
  input.onclick = e => e.stopPropagation();
  input.onkeydown = e => { e.stopPropagation(); if (e.key === 'Enter') finish(true); if (e.key === 'Escape') finish(false); };
  input.onblur = () => finish(true);
}

async function copyClip(name) {
  try { await api('/api/copy', {name}); toast('Copied! Paste it in Discord with Ctrl+V', name, 'ok'); }
  catch (e) { toast('Copy failed', e.message, 'bad'); }
}
async function deleteClip(name) {
  busy('Moving to Recycle Bin…');
  try { await api('/api/delete', {name}); busy(''); toast('Moved to Recycle Bin', name); await loadClips(true); }
  catch (e) { busy(''); toast('Delete failed', e.message, 'bad'); }
}

// ---------------- Editor ----------------
function openClip(name, keep) {
  flushTrim(); flushMix(); ensureCtx();
  const c = info.clips.find(x => x.name === name); if (!c) return;
  current = c; dur = 0; sel = {a: 0, b: 0}; restore = keep || null;
  $('#nameInput').value = stem(c.name);
  $('#library').hidden = true; $('#editor').hidden = false;
  $('#saveCopy').disabled = !info.ffmpeg;
  $('#copy').disabled = !info.windows;
  disarmDelete();
  const strip = $('#strip'); strip.hidden = true;
  if (info.ffmpeg) { strip.onload = () => strip.hidden = false; strip.onerror = () => strip.hidden = true;
    strip.src = '/strip/' + enc(c.name) + '?m=' + c.mtime; }
  video.src = '/video/' + enc(c.name) + '?m=' + c.mtime; video.load();
  resetMixer(c);
  loadPackets(c);
  draw(); drawInfo();
}
function unloadVideo() { video.pause(); video.removeAttribute('src'); video.load(); unloadTracks(); }
function closeEditor() { flushTrim(); flushMix(); if (updateOffered) { location.reload(); return; } if (isFs()) exitFs(); unloadVideo(); current = null; $('#editor').hidden = true; $('#library').hidden = false; loadClips(true); }
$('#back').onclick = closeEditor;

video.addEventListener('loadedmetadata', () => {
  dur = video.duration || 0; sel = {a: 0, b: dur};
  if (restore) { sel = {a: Math.min(restore.a, dur), b: Math.min(restore.b, dur)}; video.currentTime = Math.min(restore.t, dur); restore = null; }
  else {
    const t = current && (current.name in localTrims ? localTrims[current.name] : current.trim);
    if (t && t[0] < t[1] && t[0] < dur) { sel = {a: t[0], b: Math.min(t[1], dur)}; video.currentTime = sel.a; }
    video.play().catch(() => {});
  }
  draw(); drawInfo();
});
video.addEventListener('error', () => { if (current && video.getAttribute('src'))
  toast('Can\'t play this clip here', 'MKV files may not preview. Set OBS to record MP4 / Hybrid MP4.', 'bad'); });
video.addEventListener('play', () => $('#play').textContent = '❚❚');
video.addEventListener('pause', () => $('#play').textContent = '▶');
video.addEventListener('click', () => { if (!video.controls) togglePlay(); });

// fullscreen: opens the clip on the whole monitor; falls back to filling the panel
const stage = $('.stage');
function isFs() { return stage.classList.contains('theater'); }
function exitFs() { stage.classList.remove('theater'); video.controls = false; $('#fs').textContent = 'Fullscreen'; }
async function toggleFs() {
  if (!current) return;
  if (isFs()) return exitFs();
  video.pause();
  try { await api('/api/watch', {name: current.name, t: video.currentTime}); }
  catch (e) { video.controls = true; stage.classList.add('theater'); $('#fs').textContent = 'Exit fullscreen'; }
}
$('#fs').onclick = toggleFs;

// drag the bar under the video to make it taller or shorter; remembered per viewer
(function () {
  const st = $('#stage'), grip = $('#grip');
  const apply = h => { if (h) { st.style.setProperty('--sh', h + 'px'); st.classList.add('sized'); } else st.classList.remove('sized'); };
  try { const h = +localStorage.getItem('cl_video_h'); if (h > 0) apply(h); } catch (e) {}
  let y0 = 0, h0 = 0, on = false;
  grip.addEventListener('pointerdown', e => { on = true; y0 = e.clientY; h0 = st.getBoundingClientRect().height;
    grip.setPointerCapture(e.pointerId); grip.classList.add('drag'); e.preventDefault(); });
  grip.addEventListener('pointermove', e => { if (on) apply(Math.round(Math.max(150, Math.min(2400, h0 + e.clientY - y0)))); });
  const end = () => { if (!on) return; on = false; grip.classList.remove('drag');
    try { localStorage.setItem('cl_video_h', Math.round(st.getBoundingClientRect().height)); } catch (e) {} };
  grip.addEventListener('pointerup', end); grip.addEventListener('lostpointercapture', end); window.addEventListener('mouseup', end);
  grip.addEventListener('dblclick', () => { apply(0); try { localStorage.removeItem('cl_video_h'); } catch (e) {} });
})();

const pct = t => dur ? (t / dur * 100) + '%' : '0%';
function draw() {
  $('#hA').style.left = pct(sel.a); $('#hB').style.left = pct(sel.b);
  $('#shL').style.width = pct(sel.a); $('#shR').style.left = pct(sel.b);
  $('#selbox').style.left = pct(sel.a); $('#selbox').style.width = dur ? ((sel.b - sel.a) / dur * 100) + '%' : '0';
  drawLanes();
  if (!current) return;
  const len = Math.max(0, sel.b - sel.a), full = isFull() && mixDefault();
  const exact = full ? current.size : exactTrimSize(isFull() ? 0 : sel.a, isFull() ? dur : sel.b);
  const est = exact != null ? exact : (dur ? current.size * len / dur : 0), over = est > LIMIT();
  const S = $('#stats'); S.innerHTML = '';
  [['Length', len.toFixed(1) + 's'], [full ? 'File size' : 'Trimmed size', (exact == null ? '≈ ' : '') + mb(est), over]].forEach(([k, v, o]) => {
    const s = el('div', 'stat' + (o ? ' over' : '')); s.append(el('div', '', k), el('div', '', v)); S.append(s); });
  $('#barFill').style.width = Math.min(100, est / LIMIT() * 100) + '%'; $('#barFill').classList.toggle('over', over);
  $('#barHint').textContent = over ? 'Over Discord\'s limit. Copy for Discord will compress it to fit.'
                                   : 'Fits Discord\'s free ' + info.limitMB + ' MB limit.';
  $('#copyHint').textContent = over ? 'Compresses it to fit, then press Ctrl+V'
                             : full ? 'Then press Ctrl+V in Discord' : 'Copies just the trimmed part';
}
const isFull = () => !dur || (sel.a < 0.05 && sel.b > dur - 0.05);
// Exact trimmed size: the same packets ffmpeg will copy, summed live as the handles move.
let pk = null;
function lowerBound(arr, x) { let lo = 0, hi = arr.length; while (lo < hi) { const m = (lo + hi) >> 1; if (arr[m] < x) lo = m + 1; else hi = m; } return lo; }
function prefix(arr) { const p = new Float64Array(arr.length + 1); for (let i = 0; i < arr.length; i++) p[i + 1] = p[i] + arr[i]; return p; }
async function loadPackets(c) {
  pk = null;
  try {
    const r = await api('/packets/' + enc(c.name) + '?m=' + c.mtime);
    if (!current || current.name !== c.name || !r.vt.length) return;
    pk = {name: c.name, vt: r.vt, vk: r.vk, at: r.at, vp: prefix(r.vs), ap: prefix(r.as)};
    setupTracks(c, r.tracks || []);
    draw();
  } catch (e) { $('#lanes').innerHTML = ''; }
}
function exactTrimSize(a, b) {
  if (!pk || !current || pk.name !== current.name) return null;
  const first = pk.vt[0], ki = lowerBound(pk.vk, a + first + 1e-3 + 1e-9) - 1;
  const lo = ki >= 0 ? pk.vk[ki] : first, hi = a + first + (b - a);
  const v0 = lowerBound(pk.vt, lo - 1e-3), v1 = lowerBound(pk.vt, hi);
  if (!mixDefault()) {
    const audio = allMuted() ? 0 : 160000 / 8 * (hi - lo);
    return Math.floor((pk.vp[v1] - pk.vp[v0]) * 1.003 + Math.max(0, v1 - v0) * 24 + audio * 1.02 + 8192);
  }
  const a0 = lowerBound(pk.at, lo - 0.05), a1 = lowerBound(pk.at, hi);
  const bytes = (pk.vp[v1] - pk.vp[v0]) + (pk.ap[a1] - pk.ap[a0]), n = Math.max(0, v1 - v0) + Math.max(0, a1 - a0);
  return Math.floor(bytes * 1.003 + n * 24 + 8192);
}
function drawInfo() {
  if (!current) return;
  const M = $('#meta2'); M.innerHTML = '';
  const d = new Date(current.mtime * 1000);
  [d.toLocaleDateString(undefined, {month: 'short', day: 'numeric'}) + ', ' + clock(current.mtime),
   dfmt(current.duration || dur), video.videoWidth ? video.videoWidth + '×' + video.videoHeight : '', mb(current.size)]
    .filter(Boolean).forEach(t => M.append(el('span', '', t)));
}
(function tick() {
  if (current && dur) {
    if ($('#loop').checked && !video.paused && loopFrom < sel.b - 0.05 && video.currentTime >= sel.b - 0.03) video.currentTime = sel.a;
    $('#ph').style.left = pct(video.currentTime);
    const lp = pct(video.currentTime); document.querySelectorAll('#lanes .lph').forEach(e => e.style.left = lp);
    if (performance.now() - lastSync > 250) { lastSync = performance.now(); syncTracks(false); }
    const t = $('#time'); t.innerHTML = ''; t.append(el('b', '', tfmt(video.currentTime)), ' / ' + tfmt(dur));
  }
  requestAnimationFrame(tick);
})();

// ---------------- Audio mixer ----------------
// One track = the whole clip's audio. Several = OBS convention: track 1 is everything, 2+ are separate sources.
let tracks = [], mix = {vols: [], mutes: []}, tracksReady = false, lastSync = 0, mixTimer = null, mixDirty = false;
const localMixes = {}, pool = [];
let actx = null, vGain = null;
const editable = () => tracks.length <= 1 ? [0] : tracks.map((_t, k) => k).slice(1);
const mixDefault = () => !tracks.length || editable().every(k => Math.abs((mix.vols[k] ?? 1) - 1) < 0.005 && !mix.mutes[k]);
const allMuted = () => tracks.length && editable().every(k => mix.mutes[k] || (mix.vols[k] ?? 1) < 0.005);
const mixOut = () => mixDefault() ? null : {vols: tracks.map((_t, k) => mix.vols[k] ?? 1), mutes: tracks.map((_t, k) => !!mix.mutes[k])};
function trackLabel(k) {
  const n = tracks.length;
  if (n <= 1) return 'Audio';
  const mine = (info.trackNames || {})[String(k)], live = (info.obsTrackNames || {})[String(k)];
  const file = (tracks[k] && tracks[k].name) || '';
  if (mine || live || file) return mine || live || file;
  // unnamed: borrow the usual names if no other lane already has them
  const others = editable().filter(j => j !== k).map(j => ((info.trackNames || {})[String(j)] || (info.obsTrackNames || {})[String(j)] || (tracks[j] && tracks[j].name) || '').toLowerCase());
  const taken = w => others.some(o => o.includes(w));
  if (!taken('mic')) return 'Mic';
  if (!taken('desktop')) return 'Desktop';
  return 'Track ' + (k + 1);
}
function editTrackName(k, span) {
  const input = el('input', 'tname'); input.value = trackLabel(k); input.spellcheck = false;
  span.replaceWith(input); input.focus(); input.select();
  let done = false;
  const finish = async save => {
    if (done) return; done = true;
    if (save) { try { const r = await api('/api/trackname', {k, name: input.value}); info.trackNames = r.trackNames; } catch (e) {} }
    renderMixer();
  };
  input.onkeydown = e => { e.stopPropagation(); if (e.key === 'Enter') finish(true); if (e.key === 'Escape') finish(false); };
  input.onblur = () => finish(true);
}
function ensureCtx() {
  try {
    if (!actx) {
      const C = window.AudioContext || window.webkitAudioContext; if (!C) return;
      actx = new C();
      const src = actx.createMediaElementSource(video);
      vGain = actx.createGain(); src.connect(vGain); vGain.connect(actx.destination);
      pool.forEach(connectTrack);
    }
    if (actx.state === 'suspended') actx.resume().catch(() => {});
  } catch (e) {}
}
['pointerdown', 'keydown'].forEach(t => document.addEventListener(t, () => { if (actx && actx.state === 'suspended') actx.resume().catch(() => {}); }, true));
function connectTrack(p) {
  if (!actx || p.gain) return;
  try { const src = actx.createMediaElementSource(p.el); p.gain = actx.createGain(); p.gain.gain.value = 0;
    src.connect(p.gain); p.gain.connect(actx.destination); p.el.muted = false; } catch (e) {}
}
function unloadTracks() {
  tracksReady = false;
  pool.forEach(p => { p.el.pause(); p.k = -1; if (p.el.getAttribute('src')) { p.el.removeAttribute('src'); p.el.load(); } });
}
function resetMixer(c) {
  unloadTracks(); tracks = [];
  const saved = c.name in localMixes ? localMixes[c.name] : c.mix;
  mix = saved ? {vols: [...saved.vols], mutes: [...saved.mutes]} : {vols: [], mutes: []};
  $('#lanes').innerHTML = ''; $('.tl').style.setProperty('--lw', '0px'); applyMix();
}
function setupTracks(c, list) {
  tracks = list;
  for (let k = 0; k < tracks.length; k++) { if (mix.vols[k] == null) mix.vols[k] = 1; if (mix.mutes[k] == null) mix.mutes[k] = false; }
  renderMixer();
  if (tracks.length >= 2) {
    const want = editable(); let ready = 0;
    want.forEach((k, i) => {
      if (!pool[i]) { const el = new Audio(); el.preload = 'auto'; el.muted = true; pool[i] = {el, gain: null, k: -1}; connectTrack(pool[i]); }
      const p = pool[i]; p.k = k;
      p.el.oncanplay = () => { if (p.k !== k || !current || current.name !== c.name) return;
        p.el.oncanplay = null; if (++ready === want.length) { tracksReady = true; syncTracks(true); applyMix(); } };
      p.el.src = '/audio/' + enc(c.name) + '?k=' + k + '&m=' + c.mtime; p.el.load();
    });
  }
  applyMix();
}
function applyMix() {
  if (!actx || !vGain) return;
  const n = tracks.length;
  if (n <= 1) { vGain.gain.value = n === 0 ? 1 : (mix.mutes[0] ? 0 : (mix.vols[0] ?? 1)); pool.forEach(p => p.gain && (p.gain.gain.value = 0)); return; }
  const separate = tracksReady && !mixDefault();
  vGain.gain.value = separate ? 0 : 1;                     // as recorded: play track 1 untouched
  pool.forEach(p => { if (!p.gain) return; p.gain.gain.value = separate && p.k > 0 ? (mix.mutes[p.k] ? 0 : (mix.vols[p.k] ?? 1)) : 0; });
}
function syncTracks(force) {
  if (!tracksReady) return;
  for (const p of pool) {
    if (p.k < 0 || !p.el.getAttribute('src')) continue;
    if (force || Math.abs(p.el.currentTime - video.currentTime) > 0.12) { try { p.el.currentTime = video.currentTime; } catch (e) {} }
    if (video.paused || video.seeking) { if (!p.el.paused) p.el.pause(); }
    else if (p.el.paused) p.el.play().catch(() => {});
  }
}
const LANE_COLORS = ['#22b38e', '#3b8fd9', '#e0952e', '#b56be0', '#d9534f', '#8bb83a'];
const SPK_ON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 5 6 9H3v6h3l5 4z" fill="currentColor"/><path d="M15.5 8.5a5 5 0 0 1 0 7M18.5 5.5a9 9 0 0 1 0 13"/></svg>';
const SPK_OFF = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 5 6 9H3v6h3l5 4z" fill="currentColor"/><path d="m16 9 5 6M21 9l-5 6"/></svg>';
function renderMixer() {
  const L = $('#lanes'); L.innerHTML = '';
  if (!tracks.length || !current) return;
  editable().forEach((k, i) => {
    const color = LANE_COLORS[i % LANE_COLORS.length], muted = !!mix.mutes[k], vol = mix.vols[k] ?? 1;
    const row = el('div', 'tlrow' + (muted ? ' muted' : '')); row.style.setProperty('--tc', color);
    const lab = el('div', 'tlab'), spk = el('button', 'spk'); spk.innerHTML = muted ? SPK_OFF : SPK_ON;
    spk.title = muted ? 'Unmute' : 'Mute';
    const name = el('span', 'tn', trackLabel(k));
    if (tracks.length > 1) { name.classList.add('edit'); name.title = 'Click to rename (applies to every clip)'; name.onclick = () => editTrackName(k, name); }
    else name.title = 'Desktop audio and mic are on one track in this clip. Record them on separate tracks in OBS to adjust them separately.';
    const range = el('input'), val = el('span', 'tv', Math.round(vol * 100) + '%');
    range.type = 'range'; range.min = 0; range.max = 200; range.step = 1; range.value = Math.round(vol * 100);
    range.title = 'Volume (double-click for 100%)';
    range.style.setProperty('--f', range.value / 200);
    lab.append(spk, name, range, val);
    const lane = el('div', 'lane' + (muted ? ' muted' : '')), wv = el('div', 'wv');
    lane.style.setProperty('--tc', color);
    lane.append(wv, el('div', 'lsh l'), el('div', 'lsh r'), el('div', 'lph'));
    const img = new Image(), src = '/wave/' + enc(current.name) + '?k=' + k + '&m=' + current.mtime;
    img.onload = () => { wv.style.webkitMaskImage = wv.style.maskImage = 'url("' + src + '")'; wv.classList.add('ready'); };
    img.src = src;
    const setVol = v => { mix.vols[k] = v; val.textContent = Math.round(v * 100) + '%'; wv.style.transform = 'scaleY(' + Math.min(1.6, Math.max(0.04, v)) + ')'; mixChanged(); };
    wv.style.transform = 'scaleY(' + Math.min(1.6, Math.max(0.04, vol)) + ')';
    range.oninput = () => { range.style.setProperty('--f', range.value / 200); setVol(range.value / 100); };
    range.ondblclick = () => { range.value = 100; range.oninput(); };
    spk.onclick = () => { mix.mutes[k] = !mix.mutes[k]; const m = mix.mutes[k];
      row.classList.toggle('muted', m); lane.classList.toggle('muted', m); spk.innerHTML = m ? SPK_OFF : SPK_ON; spk.title = m ? 'Unmute' : 'Mute'; mixChanged(); };
    lane.onpointerdown = ev => { if (!dur) return; const r = lane.getBoundingClientRect(); video.currentTime = Math.min(dur, Math.max(0, (ev.clientX - r.left) / r.width * dur)); };
    row.append(lab, lane); L.append(row);
  });
  // same width for every panel: as wide as the longest name (capped)
  L.style.removeProperty('--nw');
  const w = Math.max(0, ...[...L.querySelectorAll('.tn')].map(n => n.scrollWidth));
  if (w) L.style.setProperty('--nw', Math.min(140, Math.ceil(w) + 2) + 'px');
  const first = L.querySelector('.tlab');
  $('.tl').style.setProperty('--lw', first ? first.offsetWidth + 'px' : '0px');
  drawLanes();
}
function drawLanes() {
  document.querySelectorAll('#lanes .lane').forEach(l => {
    l.children[1].style.width = pct(sel.a); l.children[2].style.left = pct(sel.b);
    l.children[3].style.left = pct(video.currentTime || 0);
  });
}
function mixChanged() {
  applyMix(); draw();
  if (current) localMixes[current.name] = mixOut();
  mixDirty = true; clearTimeout(mixTimer); mixTimer = setTimeout(flushMix, 500);
}
function flushMix() {
  clearTimeout(mixTimer); if (!mixDirty || !current) return; mixDirty = false;
  const m = mixOut(); localMixes[current.name] = m; current.mix = m;
  api('/api/setmix', {name: current.name, mix: m}).catch(() => {});
}

// timeline dragging
function timeAt(x) { const r = track.getBoundingClientRect(); return Math.min(dur, Math.max(0, (x - r.left) / r.width * dur)); }
let drag = null;
track.addEventListener('pointerdown', ev => {
  if (!dur) return; track.setPointerCapture(ev.pointerId);
  if (ev.target.id === 'hA' || ev.target.id === 'hB') { drag = ev.target.id; video.pause(); }
  else { drag = 'seek'; video.currentTime = timeAt(ev.clientX); }
  ev.preventDefault();
});
track.addEventListener('pointermove', ev => {
  if (!dur) return; const t = timeAt(ev.clientX), h = $('#hov');
  h.style.display = 'block'; h.textContent = tfmt(t);
  const tw = track.clientWidth, hw = h.offsetWidth, x = dur ? t / dur * tw : 0;
  h.style.left = Math.max(hw / 2, Math.min(tw - hw / 2, x)) + 'px';
  if (!drag) return;
  if (drag === 'hA') { sel.a = Math.min(t, sel.b - 0.3); video.currentTime = sel.a; scheduleTrimSave(); }
  else if (drag === 'hB') { sel.b = Math.max(t, sel.a + 0.3); video.currentTime = sel.b; scheduleTrimSave(); }
  else video.currentTime = t;
  draw();
});
track.addEventListener('pointerleave', () => { if (!drag) $('#hov').style.display = 'none'; });
function endDrag() { if (drag === 'hA' || drag === 'hB') rememberTrim(); drag = null; }
track.addEventListener('pointerup', endDrag);
track.addEventListener('pointercancel', endDrag);
track.addEventListener('lostpointercapture', endDrag);
window.addEventListener('mouseup', endDrag);

function togglePlay() {
  if (!dur) return;
  if (video.paused) video.play(); else video.pause();
}
let loopFrom = 0;   // where playback last started; loop only kicks in if that was before the end handle
video.addEventListener('play', () => { loopFrom = video.currentTime; ensureCtx(); syncTracks(true); });
video.addEventListener('seeked', () => { loopFrom = video.currentTime; syncTracks(true); });
video.addEventListener('seeking', () => syncTracks(true));
video.addEventListener('pause', () => pool.forEach(p => { if (!p.el.paused) p.el.pause(); }));
function setA() { if (!dur) return; sel.a = Math.min(video.currentTime, sel.b - 0.3); draw(); trimDirty = true; rememberTrim(); }
function setB() { if (!dur) return; sel.b = Math.max(video.currentTime, sel.a + 0.3); draw(); trimDirty = true; rememberTrim(); }
const localTrims = {};   // trims set this session, so a list refresh mid-save can't lose them
let trimTimer = null, trimDirty = false;
function scheduleTrimSave() { trimDirty = true; clearTimeout(trimTimer); trimTimer = setTimeout(rememberTrim, 400); }
function flushTrim() { if (trimDirty) rememberTrim(); }
function rememberTrim() {
  clearTimeout(trimTimer); trimDirty = false;
  if (!current || !dur) return;
  const full = isFull(), t = full ? null : [+sel.a.toFixed(3), +sel.b.toFixed(3)];
  current.trim = t; localTrims[current.name] = t;
  api('/api/settrim', {name: current.name, start: t ? t[0] : null, end: t ? t[1] : null}).catch(() => {});
}
$('#play').onclick = togglePlay;

async function saveTrim() {
  if (!current || !dur) return;
  flushTrim(); video.pause();
  busy('Choose where to save it…');
  try {
    flushMix();
    const r = await api('/api/saveas', {name: current.name, start: sel.a, end: sel.b, mix: mixOut()});
    busy('');
    if (r.cancelled) return;
    toast('Saved (' + mb(r.size) + ')', r.path, 'ok', {label: 'Show', fn: () => api('/api/reveal-export', {}).catch(() => {})}, 7000);
    loadClips(true);
  } catch (e) { busy(''); toast('Couldn\'t save', e.message, 'bad', null, 8000); }
}
$('#saveCopy').onclick = () => saveTrim();

// rename
const nameIn = $('#nameInput');
nameIn.addEventListener('keydown', e => {
  if (e.key === 'Enter') nameIn.blur();
  if (e.key === 'Escape') { nameIn.value = stem(current.name); nameIn.blur(); }
  e.stopPropagation();
});
nameIn.addEventListener('blur', async () => {
  if (!current) return;
  const v = nameIn.value.trim(), old = current.name;
  if (!v || v === stem(old)) { nameIn.value = stem(old); return; }
  const keep = {t: video.currentTime, a: sel.a, b: sel.b};
  unloadVideo();
  try { const r = await api('/api/rename', {name: old, newName: v}); await loadClips(true);
    openClip(r.name, keep); toast('Renamed', r.name, 'ok'); }
  catch (e) { toast('Rename failed', e.message, 'bad'); openClip(old, keep); }
});

$('#copy').onclick = async () => {
  if (!current || !dur) return;
  flushMix();
  const name = current.name, size = current.size, full = isFull(), plain = full && mixDefault();
  const est = plain ? size : exactTrimSize(full ? 0 : sel.a, full ? dur : sel.b);
  if (!plain || size > LIMIT()) {
    const big = est == null || est > LIMIT(), secs = Math.max(0, sel.b - sel.a);
    video.pause();
    busy(big ? 'Compressing ' + secs.toFixed(0) + 's to fit Discord… this can take a bit' : 'Preparing your copy…');
    try { const r = await api('/api/copy-trim', full ? {name, mix: mixOut()} : {name, start: sel.a, end: sel.b, mix: mixOut()});
      busy('');
      toast('Copied ' + mb(r.size) + '! Paste it in Discord with Ctrl+V',
            r.compressed ? 'Compressed to fit Discord. Your original clip is untouched.' : 'Nothing added to your library', 'ok', null, 6000); }
    catch (e) { busy(''); toast('Couldn\'t copy', e.message, 'bad', null, 7000); }
    return;
  }
  if (size > LIMIT()) { toast('Too big for Discord (' + mb(size) + ')', 'Trim it shorter to get under ' + info.limitMB + ' MB.', 'bad', null, 6000); return; }
  try { await api('/api/copy', {name}); toast('Copied ' + mb(size) + '! Paste it in Discord with Ctrl+V', name, 'ok'); }
  catch (e) { toast('Copy failed', e.message, 'bad'); }
};
$('#reveal').onclick = () => current && api('/api/reveal', {name: current.name}).catch(e => toast('Error', e.message, 'bad'));

let armTimer = null;
function disarmDelete() { clearTimeout(armTimer); $('#del').classList.remove('armed'); $('#del').textContent = 'Delete'; }
$('#del').onclick = async () => {
  const b = $('#del');
  if (!b.classList.contains('armed')) { b.classList.add('armed'); b.textContent = 'Click again'; armTimer = setTimeout(disarmDelete, 3000); return; }
  disarmDelete();
  const name = current.name;
  unloadVideo(); busy('Moving to Recycle Bin…');
  try { await api('/api/delete', {name}); busy(''); toast('Moved to Recycle Bin', name);
    closeEditor(); }
  catch (e) { busy(''); toast('Delete failed', e.message, 'bad'); openClip(name); }
};

document.addEventListener('keydown', ev => {
  if (ev.target.tagName === 'INPUT' && ev.target.type !== 'checkbox') return;
  if (!current) {
    if (ev.key === '/') { $('#search').focus(); ev.preventDefault(); }
    else if (ev.key === 'Escape' && picked.size) clearPicks();
    return;
  }
  const k = ev.key.toLowerCase(), fps = 1 / 60;
  if (isFs()) {
    if (k === 'escape') { exitFs(); ev.preventDefault(); return; }
    if (document.activeElement === video) return;
  }
  if (k === ' ') togglePlay();
  else if (k === 'i') setA();
  else if (k === 'o') setB();
  else if (k === 'escape') closeEditor();
  else if (k === 'arrowleft') video.currentTime = Math.max(0, video.currentTime - (ev.shiftKey ? 5 : 1));
  else if (k === 'arrowright') video.currentTime = Math.min(dur, video.currentTime + (ev.shiftKey ? 5 : 1));
  else if (k === ',') { video.pause(); video.currentTime = Math.max(0, video.currentTime - fps); }
  else if (k === '.') { video.pause(); video.currentTime = Math.min(dur, video.currentTime + fps); }
  else return;
  ev.preventDefault();
});

// ---------------- Live updates ----------------
async function poll() {
  try {
    const r = await api('/api/events?since=' + (lastEvent ?? 0));
    if (lastEvent === null || r.last < lastEvent) lastEvent = r.last;
    else if (r.events.length) {
      for (const e of r.events) if (e.type === 'saved') {
        newNames.add(e.name);
        toast('Clip saved', e.name, 'ok', {label: 'Open', fn: () => openClip(e.name)});
      }
      lastEvent = r.last; await loadClips(true);
    }
  } catch (e) {}
  setTimeout(poll, 1000);
}
loadClips(true); poll();
setInterval(() => loadClips(), 8000);
</script></body></html>
"""