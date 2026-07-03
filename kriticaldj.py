#!/usr/bin/env python3
"""KriticalDJ -- a lean LAN karaoke player.

One stdlib-only server, three surfaces: / (singers, BYOD), /kj (KJ console),
/screen (TV output). Queue, singer rotation, and playback phase live in a
crash-safe JSON journal. See PLAN.md for the full architecture.

Author: Spike Graham, with Claude (Anthropic) as co-author.
"""
from __future__ import annotations

import json
import os
import re
import socket
import sys
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Empty, Queue
from urllib.parse import parse_qs, urlparse

APP = "KriticalDJ"
ROOT = Path(__file__).resolve().parent

DEFAULT_CONFIG = {
    "music_root": "",
    "host": "0.0.0.0",
    "port": 8080,
    "party_name": "Karaoke Night",
    "intermission_seconds": 30,
    "start_now_countdown_seconds": 3,
    # Rendering leads Bluetooth audio by the sink's latency; this shifts CDG
    # frames relative to audio.currentTime. Calibrate from the KJ console.
    "lyrics_offset_ms": 0,
    "public_url": "",
}


# --------------------------------------------------------------------------
# Config

def load_config(path: Path) -> dict:
    """Read config, creating it with defaults on first run."""
    if not path.exists():
        path.write_text(json.dumps(DEFAULT_CONFIG, indent=2), encoding="utf-8")
        print(f"[{APP}] wrote default config to {path} -- set music_root and rerun")
        sys.exit(1)
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(json.loads(path.read_text(encoding="utf-8")))
    if not cfg["music_root"]:
        print(f"[{APP}] music_root is not set in {path}")
        sys.exit(1)
    return cfg


def lan_url(cfg: dict) -> str:
    """URL for the QR code: config override, else best-guess LAN address."""
    if cfg.get("public_url"):
        return cfg["public_url"]
    ip = "127.0.0.1"
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))  # no traffic sent; just picks the LAN iface
        ip = s.getsockname()[0]
        s.close()
    except OSError:
        pass
    return f"http://{ip}:{cfg['port']}/"


# --------------------------------------------------------------------------
# Library scan

_CATALOG_RE = re.compile(r"^[A-Za-z]{1,6}[\d][\w-]*$")  # SC8121-03, EZH-31, ...


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def parse_title(stem: str, artist_hint: str) -> str:
    """Best-effort song title from a filename stem like
    'SC8121-03 - Some Artist - Song Title'."""
    parts = [p.strip() for p in stem.split(" - ") if p.strip()]
    if parts and _CATALOG_RE.fullmatch(parts[0].replace(" ", "")):
        parts = parts[1:]
    if not parts:
        return stem
    if len(parts) == 1:
        return parts[0]
    hint = _norm(artist_hint)
    if hint and _norm(parts[0]) == hint:
        return " - ".join(parts[1:])
    if hint and _norm(parts[-1]) == hint:
        return " - ".join(parts[:-1])
    return " - ".join(parts[1:])


def scan_library(music_root: str) -> dict:
    """Index mp3+cdg pairs and zips under music_root.

    Returns {song_id: {artist, title, search, mp3, cdg, zip}} where media
    values are absolute paths (zip singles carry the archive path instead).
    Song ids are stable across rescans (hash of the relative path)."""
    import hashlib

    root = Path(music_root)
    songs: dict = {}

    def add(rel: str, artist: str, title: str, **media) -> None:
        sid = hashlib.sha1(rel.encode("utf-8")).hexdigest()[:12]
        songs[sid] = {
            "artist": artist,
            "title": title,
            "search": _norm(f"{artist} {title}"),
            **media,
        }

    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = str(p.relative_to(root))
        parent = p.parent.name if p.parent != root else ""
        # single-char parents are letter buckets (a/, b/, #/), not artists
        artist = parent.title() if len(parent) > 1 else ""
        ext = p.suffix.lower()
        if ext == ".mp3":
            cdg = p.with_suffix(".cdg")
            if not cdg.exists():
                cdg = p.with_suffix(".CDG")
            if cdg.exists():
                title = parse_title(p.stem, artist)
                if not artist:  # flat layout: fall back to stem parsing
                    bits = [b.strip() for b in p.stem.split(" - ") if b.strip()]
                    artist = bits[-2] if len(bits) >= 2 else "Unknown"
                add(rel, artist, title, mp3=str(p), cdg=str(cdg))
        elif ext == ".zip":
            try:
                with zipfile.ZipFile(p) as z:
                    names = z.namelist()
                mp3s = [n for n in names if n.lower().endswith(".mp3")]
                cdgs = [n for n in names if n.lower().endswith(".cdg")]
                if mp3s and cdgs:
                    title = parse_title(p.stem, artist)
                    if not artist:
                        artist = "Unknown"
                    add(rel, artist, title, zip=str(p), zip_mp3=mp3s[0], zip_cdg=cdgs[0])
            except (zipfile.BadZipFile, OSError):
                continue
    return songs


# --------------------------------------------------------------------------
# State (crash-safe journal) + rotation engine

def pick_next(singers: list, cursor: int, entries: list):
    """Round-robin: from `cursor`, find the first singer with a queued entry.

    Returns (entry, new_cursor) or (None, cursor). Entries are dicts with a
    'singer' key; per-singer order is the list order (FIFO). Singers with
    nothing queued are skipped but keep their rotation slot."""
    if not singers:
        return None, cursor
    for i in range(len(singers)):
        idx = (cursor + i) % len(singers)
        for e in entries:
            if e["singer"] == singers[idx]:
                return e, (idx + 1) % len(singers)
    return None, cursor


class State:
    """All mutable party state, guarded by one lock, journaled to disk."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        self.listeners: list[Queue] = []
        self.singers: list[str] = []
        self.queue: list[dict] = []      # {id, singer, song_id}
        self.cursor = 0                  # rotation position in self.singers
        self.now: dict | None = None     # entry currently on stage
        self.phase = "idle"              # idle|playing|intermission|countdown
        self.deadline = 0.0              # epoch when intermission/countdown ends
        self.transport = {"cmd": "play", "seq": 0}
        self.next_entry_id = 1
        self._load()

    # -- persistence -------------------------------------------------------
    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return
        for k in ("singers", "queue", "cursor", "now", "phase",
                  "deadline", "transport", "next_entry_id"):
            if k in d:
                setattr(self, k, d[k])
        # A power failure mid-song resumes at the intermission board rather
        # than mid-track: honest, and nobody loses their place in line.
        if self.now is not None:
            self.queue.insert(0, self.now)
            self.now = None
        if self.phase in ("playing", "countdown", "intermission"):
            self.phase = "intermission"
            self.deadline = time.time() + 5

    def _save(self) -> None:
        d = {k: getattr(self, k) for k in
             ("singers", "queue", "cursor", "now", "phase",
              "deadline", "transport", "next_entry_id")}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    # -- change notification -----------------------------------------------
    def _broadcast(self, songs: dict) -> None:
        payload = json.dumps(self.snapshot(songs))
        for q in list(self.listeners):
            try:
                q.put_nowait(payload)
            except Exception:
                pass

    def mutate(self, songs: dict, fn) -> None:
        """Run fn() under the lock, then journal + broadcast."""
        with self.lock:
            fn()
            self._save()
            self._broadcast(songs)

    # -- views ---------------------------------------------------------------
    def rotation_preview(self, limit: int = 12) -> list:
        """Upcoming (singer, entry) order, simulated without mutating."""
        entries = list(self.queue)
        cursor = self.cursor
        out = []
        while len(out) < limit:
            e, cursor = pick_next(self.singers, cursor, entries)
            if e is None:
                break
            entries.remove(e)
            out.append(e)
        return out

    def snapshot(self, songs: dict) -> dict:
        def song_view(e):
            s = songs.get(e["song_id"], {})
            return {"id": e["id"], "singer": e["singer"], "song_id": e["song_id"],
                    "artist": s.get("artist", "?"), "title": s.get("title", "?")}
        with self.lock:
            up = self.rotation_preview()
            return {
                "phase": self.phase,
                "deadline": self.deadline,
                "server_time": time.time(),
                "now": song_view(self.now) if self.now else None,
                "next": song_view(up[0]) if up else None,
                "upcoming": [song_view(e) for e in up],
                "queue": [song_view(e) for e in self.queue],
                "singers": list(self.singers),
                "transport": dict(self.transport),
            }


# --------------------------------------------------------------------------
# Flow control: the server owns the clock

class Flow:
    def __init__(self, state: State, songs: dict, cfg: dict):
        self.state, self.songs, self.cfg = state, songs, cfg

    def _begin_next(self) -> None:
        """Move the next rotation entry on stage (caller holds no lock)."""
        st = self.state

        def fn():
            e, st.cursor = pick_next(st.singers, st.cursor, st.queue)
            if e is None:
                st.phase, st.now = "idle", None
                return
            st.queue.remove(e)
            st.now = e
            st.phase = "playing"
            st.transport = {"cmd": "play", "seq": st.transport["seq"] + 1}
        st.mutate(self.songs, fn)

    def song_ended(self) -> None:
        st = self.state

        def fn():
            st.now = None
            st.phase = "intermission"
            st.deadline = time.time() + self.cfg["intermission_seconds"]
        st.mutate(self.songs, fn)

    def start_now(self) -> None:
        st = self.state

        def fn():
            if st.phase in ("intermission", "idle"):
                st.phase = "countdown"
                st.deadline = time.time() + self.cfg["start_now_countdown_seconds"]
        st.mutate(self.songs, fn)

    def transport_cmd(self, cmd: str) -> None:
        st = self.state

        def fn():
            st.transport = {"cmd": cmd, "seq": st.transport["seq"] + 1}
        st.mutate(self.songs, fn)

    def skip(self) -> None:
        self.song_ended()

    def tick_forever(self) -> None:
        """Background thread: advance phases whose deadline has passed, and
        wake an idle stage when songs arrive."""
        while True:
            time.sleep(0.5)
            st = self.state
            with st.lock:
                due = st.phase in ("intermission", "countdown") and time.time() >= st.deadline
                idle_ready = st.phase == "idle" and st.rotation_preview(1)
            if due:
                self._begin_next()
            elif idle_ready:
                # first song of the night gets the intermission board + QR
                def fn():
                    st.phase = "intermission"
                    st.deadline = time.time() + self.cfg["intermission_seconds"]
                st.mutate(self.songs, fn)


# --------------------------------------------------------------------------
# HTTP

_PLACEHOLDER = ("<!DOCTYPE html><meta charset='utf-8'><title>KriticalDJ</title>"
                "<body style='font-family:sans-serif;background:#14161a;color:#eee'>"
                "<h1>KriticalDJ</h1><p>{page} UI arrives in a later phase. The API "
                "is live: <a style='color:#4fc3f7' href='/api/state'>/api/state</a></p>")


def make_handler(cfg: dict, state: State, songs: dict, flow: Flow):
    media_cache = ROOT / ".media-cache"

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # quiet
            pass

        # ---- helpers -----------------------------------------------------
        def _json(self, obj, code=200):
            body = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _html(self, text, code=200):
            body = text.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            try:
                return json.loads(self.rfile.read(n).decode("utf-8"))
            except ValueError:
                return {}

        # ---- media -------------------------------------------------------
        def _media_path(self, song_id: str, kind: str) -> Path | None:
            s = songs.get(song_id)
            if not s or kind not in ("mp3", "cdg"):
                return None
            if kind in s:
                return Path(s[kind])
            if "zip" in s:  # extract once into the cache
                media_cache.mkdir(exist_ok=True)
                out = media_cache / f"{song_id}.{kind}"
                if not out.exists():
                    with zipfile.ZipFile(s["zip"]) as z:
                        out.write_bytes(z.read(s[f"zip_{kind}"]))
                return out
            return None

        def _serve_file(self, path: Path, ctype: str) -> None:
            size = path.stat().st_size
            start, end = 0, size - 1
            rng = self.headers.get("Range")
            m = re.match(r"bytes=(\d*)-(\d*)$", rng or "")
            partial = bool(m and (m.group(1) or m.group(2)))
            if partial:
                if m.group(1):
                    start = int(m.group(1))
                    if m.group(2):
                        end = min(int(m.group(2)), size - 1)
                else:  # suffix range: last N bytes
                    start = max(0, size - int(m.group(2)))
            length = end - start + 1
            self.send_response(206 if partial else 200)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            if partial:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            with open(path, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)

        # ---- SSE ---------------------------------------------------------
        def _events(self) -> None:
            q: Queue = Queue(maxsize=32)
            state.listeners.append(q)
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                first = json.dumps(state.snapshot(songs))
                self.wfile.write(f"data: {first}\n\n".encode("utf-8"))
                self.wfile.flush()
                while True:
                    try:
                        payload = q.get(timeout=20)
                        self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                    except Empty:
                        self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
            except OSError:
                pass
            finally:
                try:
                    state.listeners.remove(q)
                except ValueError:
                    pass

        # ---- routes ------------------------------------------------------
        def do_GET(self):
            u = urlparse(self.path)
            parts = [p for p in u.path.split("/") if p]
            if u.path == "/":
                return self._html(_PLACEHOLDER.format(page="Singer"))
            if u.path == "/kj":
                return self._html(_PLACEHOLDER.format(page="KJ console"))
            if u.path == "/screen":
                return self._html(_PLACEHOLDER.format(page="Screen"))
            if u.path == "/events":
                return self._events()
            if u.path == "/api/state":
                return self._json(state.snapshot(songs))
            if u.path == "/api/config":
                return self._json({"party_name": cfg["party_name"],
                                   "public_url": lan_url(cfg),
                                   "intermission_seconds": cfg["intermission_seconds"],
                                   "start_now_countdown_seconds": cfg["start_now_countdown_seconds"],
                                   "lyrics_offset_ms": cfg["lyrics_offset_ms"]})
            if u.path == "/api/songs":
                qs = parse_qs(u.query)
                terms = _norm(" ".join(qs.get("q", [""])))
                limit = int(qs.get("limit", ["50"])[0])
                out = []
                for sid, s in songs.items():
                    if terms and terms not in s["search"]:
                        continue
                    out.append({"song_id": sid, "artist": s["artist"], "title": s["title"]})
                    if len(out) >= limit:
                        break
                out.sort(key=lambda x: (x["artist"].lower(), x["title"].lower()))
                return self._json({"count": len(out), "songs": out})
            if len(parts) == 3 and parts[0] == "media":
                p = self._media_path(parts[1], parts[2])
                if p and p.exists():
                    ctype = "audio/mpeg" if parts[2] == "mp3" else "application/octet-stream"
                    return self._serve_file(p, ctype)
                return self._json({"error": "not found"}, 404)
            return self._json({"error": "not found"}, 404)

        def do_POST(self):
            u = urlparse(self.path)
            body = self._body()
            if u.path == "/api/singers":
                name = (body.get("name") or "").strip()[:40]
                if not name:
                    return self._json({"error": "name required"}, 400)

                def fn():
                    if name not in state.singers:
                        state.singers.append(name)
                state.mutate(songs, fn)
                return self._json({"ok": True, "singers": state.singers})
            if u.path == "/api/queue":
                sid = body.get("song_id")
                singer = (body.get("singer") or "").strip()[:40]
                if sid not in songs or not singer:
                    return self._json({"error": "song_id and singer required"}, 400)

                def fn():
                    if singer not in state.singers:
                        state.singers.append(singer)
                    state.queue.append({"id": state.next_entry_id,
                                        "singer": singer, "song_id": sid})
                    state.next_entry_id += 1
                state.mutate(songs, fn)
                return self._json({"ok": True})
            if u.path == "/api/screen/ended":
                flow.song_ended()
                return self._json({"ok": True})
            if u.path.startswith("/api/kj/"):
                cmd = u.path.rsplit("/", 1)[1]
                if cmd in ("play", "pause"):
                    flow.transport_cmd(cmd)
                elif cmd == "skip":
                    flow.skip()
                elif cmd == "start_now":
                    flow.start_now()
                else:
                    return self._json({"error": "unknown command"}, 400)
                return self._json({"ok": True})
            return self._json({"error": "not found"}, 404)

        def do_DELETE(self):
            m = re.fullmatch(r"/api/queue/(\d+)", self.path)
            if not m:
                return self._json({"error": "not found"}, 404)
            eid = int(m.group(1))

            def fn():
                state.queue = [e for e in state.queue if e["id"] != eid]
            state.mutate(songs, fn)
            return self._json({"ok": True})

    return Handler


# --------------------------------------------------------------------------

def main() -> None:
    cfg_path = Path(sys.argv[sys.argv.index("--config") + 1]) if "--config" in sys.argv \
        else ROOT / "config.json"
    cfg = load_config(cfg_path)
    print(f"[{APP}] scanning {cfg['music_root']} ...")
    songs = scan_library(cfg["music_root"])
    print(f"[{APP}] {len(songs)} songs indexed")
    state = State(ROOT / "state.json")
    flow = Flow(state, songs, cfg)
    threading.Thread(target=flow.tick_forever, daemon=True).start()
    server = ThreadingHTTPServer((cfg["host"], cfg["port"]), make_handler(cfg, state, songs, flow))
    server.daemon_threads = True
    print(f"[{APP}] singers: {lan_url(cfg)}  |  KJ: {lan_url(cfg)}kj  |  screen: {lan_url(cfg)}screen")
    server.serve_forever()


if __name__ == "__main__":
    main()
