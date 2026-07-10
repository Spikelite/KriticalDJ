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
import uuid
import zipfile
from collections import Counter
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
    "intermission_seconds": 15,  # short grace: up-next is announced on screen
                                 # during the song's last 15s already
    "start_now_countdown_seconds": 3,
    # Rendering leads Bluetooth audio by the sink's latency; this shifts CDG
    # frames relative to audio.currentTime. Calibrate from the KJ console.
    "lyrics_offset_ms": 0,
    "public_url": "",
    # 4-digit gate for the operator surfaces (/kj, /setup). Change it from the
    # setup screen; default is deliberately obvious so first boot isn't locked.
    "kj_pin": "0000",
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


# GUI-editable config fields (POST /api/setup/config): kind drives validation.
_CONFIG_FIELDS = {
    "party_name": "str", "public_url": "str", "music_root": "dir",
    "host": "str", "port": "int", "intermission_seconds": "int",
    "start_now_countdown_seconds": "int", "lyrics_offset_ms": "int",
    "kj_pin": "pin",
}
_CONFIG_LIMITS = {"port": (1, 65535), "intermission_seconds": (3, 600),
                  "start_now_countdown_seconds": (0, 30),
                  "lyrics_offset_ms": (-2000, 2000)}
_RESTART_KEYS = {"host", "port"}  # rebinding the socket can't happen live


def validate_config_changes(cfg: dict, body: dict):
    """Screen a GUI config edit against the whitelist above.

    Returns (changes, errors, restart): sanitized values that actually differ
    from cfg, human-readable rejections, and which accepted keys only take
    effect after a server restart. Does NOT mutate cfg -- the caller applies."""
    changes, errors, restart = {}, [], []
    for key, val in body.items():
        kind = _CONFIG_FIELDS.get(key)
        if kind is None:
            errors.append(f"unknown setting: {key}")
            continue
        if kind == "int":
            try:
                val = int(val)
            except (TypeError, ValueError):
                errors.append(f"{key} must be a number")
                continue
            lo, hi = _CONFIG_LIMITS[key]
            val = max(lo, min(hi, val))
        elif kind == "pin":
            val = str(val).strip()
            if not val:
                continue  # blank field = keep the current PIN
            if not (val.isdigit() and len(val) == 4):
                errors.append("KJ PIN must be exactly 4 digits")
                continue
        else:
            if not isinstance(val, str):
                errors.append(f"{key} must be text")
                continue
            val = val.strip()
            if key == "party_name" and not val:
                errors.append("party_name cannot be empty")
                continue
            if kind == "dir" and not Path(val).is_dir():
                errors.append(f"{key}: not a folder: {val}")
                continue
        if val != cfg.get(key):
            changes[key] = val
            if key in _RESTART_KEYS:
                restart.append(key)
    return changes, errors, restart


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


def _searchable(s: str) -> str:
    """Lowercase, punctuation-free, token-preserving text for search fields."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", s.lower())).strip()


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
    """Index the library under music_root.

    Preferred source: a curated `index.json` sidecar in the root (emitted by
    song-sorter's Final-final) with entries {path, artist, title, duration} --
    clean display names and instant startup. Fallback: walk the tree for
    mp3+cdg pairs and zips, parsing names from filenames.

    Returns {song_id: {artist, title, search, duration?, mp3|zip, ...}} with
    absolute media paths. Song ids are stable (hash of the relative path)."""
    import hashlib

    root = Path(music_root)
    songs: dict = {}

    def add(rel: str, artist: str, title: str, **media) -> None:
        sid = hashlib.sha1(rel.encode("utf-8")).hexdigest()[:12]
        sa, st = _searchable(artist), _searchable(title)
        # versions[0] mirrors the primary media so the default path is byte-for-
        # byte the old behavior; alternates (multi-version libraries) append to
        # this list and the KJ can promote one (see VersionStore / _media_path).
        v0 = {k: v for k, v in media.items() if k != "label"}
        v0.setdefault("label", media.get("label", "Best"))
        songs[sid] = {
            "artist": artist,
            "title": title,
            "search": (sa + " " + st).strip(),
            "sa": sa,   # field-scoped search
            "st": st,
            "ltr": sa[:1].upper() if sa[:1].isalpha() else "#",  # A-Z browse
            "versions": [v0],
            **media,
        }

    sidecar = root / "index.json"
    if sidecar.exists():
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
            for e in data.get("songs", []):
                p = root / e["path"]
                if not p.is_file():
                    continue
                media = {"zip": str(p)} if p.suffix.lower() == ".zip" else {"mp3": str(p)}
                if e.get("duration"):
                    media["duration"] = int(e["duration"])
                add(e["path"], e.get("artist", "?"), e.get("title", "?"), **media)
                # optional alternate versions of the same song: [{path,label,
                # duration}, ...]. Best copy stays version 0 (the entry above);
                # these become 1..N, selectable by the KJ.
                sid = hashlib.sha1(e["path"].encode("utf-8")).hexdigest()[:12]
                for i, alt in enumerate(e.get("versions", []) or [], start=1):
                    ap = root / alt.get("path", "")
                    if not alt.get("path") or not ap.is_file():
                        continue
                    am = {"zip": str(ap)} if ap.suffix.lower() == ".zip" else {"mp3": str(ap)}
                    if alt.get("duration"):
                        am["duration"] = int(alt["duration"])
                    am["label"] = alt.get("label") or f"Version {i + 1}"
                    songs[sid]["versions"].append(am)
        except (ValueError, OSError, KeyError, TypeError):
            songs = {}
        if songs:
            print(f"[{APP}] curated index.json: {len(songs)} songs")
            return songs

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


class SingerRegistry:
    """Persistent singer identities (singers.json).

    Names are unique case-insensitively and a returning name reattaches to its
    existing id, so statistics accumulate across parties (honor system, like
    everything else). Session resets clear the rotation, NEVER this registry --
    stats rows reference these ids forever."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.by_key: dict = {}
        if path.exists():
            try:
                self.by_key = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                self.by_key = {}

    def resolve(self, name: str) -> tuple:
        """(display_name, singer_id) -- creates the singer on first sight,
        reattaches on any later casing of the same name."""
        key = name.strip().casefold()
        with self.lock:
            rec = self.by_key.get(key)
            now = round(time.time(), 3)
            if rec is None:
                rec = {"name": name.strip(), "id": uuid.uuid4().hex[:10],
                       "first_seen": now, "last_seen": now}
                self.by_key[key] = rec
            else:
                rec["last_seen"] = now
            try:
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(self.by_key, indent=1, ensure_ascii=False),
                               encoding="utf-8")
                os.replace(tmp, self.path)
            except OSError:
                pass
            return rec["name"], rec["id"]


class VersionStore:
    """Persistent per-song version choice (versions.json): song_id -> index
    into that song's `versions` list. When a library ships alternate copies of
    a song, the KJ can promote one and this remembers it across restarts and
    rescans (song ids are stable path hashes). 0 is the default (best) copy and
    is never stored; session resets never touch this -- it's a library setting,
    not party state."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.by_id: dict = {}
        if path.exists():
            try:
                self.by_id = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                self.by_id = {}

    def get(self, song_id: str) -> int:
        try:
            return int(self.by_id.get(song_id, 0))
        except (TypeError, ValueError):
            return 0

    def set(self, song_id: str, index: int) -> None:
        with self.lock:
            if index <= 0:
                self.by_id.pop(song_id, None)  # 0 = default; keep the file lean
            else:
                self.by_id[song_id] = int(index)
            try:
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(self.by_id, indent=1), encoding="utf-8")
                os.replace(tmp, self.path)
            except OSError:
                pass


class Stats:
    """Append-only party history (stats.jsonl): one JSON line per event
    (queued / started / completed / skipped / removed / session_reset).
    Fire-and-forget -- a stats failure must never interrupt the music."""

    def __init__(self, path: Path, registry: SingerRegistry):
        self.path = path
        self.registry = registry
        self.lock = threading.Lock()

    def log(self, event: str, singer: str = "", song: dict = None) -> None:
        try:
            row = {"ts": round(time.time(), 3),
                   "iso": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "event": event}
            if singer:
                row["singer"], row["singer_id"] = self.registry.resolve(singer)
            if song:
                row["song_id"] = song.get("song_id", "")
                row["artist"] = song.get("artist", "")
                row["title"] = song.get("title", "")
            with self.lock, open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            pass


def move_entry(entries: list, entry_id: int, direction: int) -> bool:
    """KJ reorder: swap a queue entry with its neighbor from the SAME singer
    (rotation order between singers is the singers list's job). Returns True
    if anything moved."""
    idx = next((i for i, e in enumerate(entries) if e["id"] == entry_id), None)
    if idx is None:
        return False
    rng = range(idx - 1, -1, -1) if direction < 0 else range(idx + 1, len(entries))
    for j in rng:
        if entries[j]["singer"] == entries[idx]["singer"]:
            entries[idx], entries[j] = entries[j], entries[idx]
            return True
    return False


def move_singer(singers: list, name: str, direction: int) -> bool:
    """KJ reorder of the rotation itself: move a singer up/down one slot."""
    if name not in singers:
        return False
    i = singers.index(name)
    j = i + (1 if direction > 0 else -1)
    if j < 0 or j >= len(singers):
        return False
    singers[i], singers[j] = singers[j], singers[i]
    return True


class State:
    """All mutable party state, guarded by one lock, journaled to disk."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        self.listeners: list[Queue] = []
        self.extra: dict = {}  # config-derived live values merged into snapshots
        self.singers: list[str] = []
        self.queue: list[dict] = []      # {id, singer, song_id}
        self.cursor = 0                  # rotation position in self.singers
        self.now: dict | None = None     # entry currently on stage
        self.phase = "idle"              # idle|playing|intermission|countdown
        self.deadline = 0.0              # epoch when intermission/countdown ends
        self.transport = {"cmd": "play", "seq": 0}
        self.versions = None             # VersionStore, attached in main();
                                         # snapshots show each song's active pick
        self.next_entry_id = 1
        # The locked "up next" slot: entry id, or None. Once someone is
        # projected next they stay next -- people plan around it (see UAT).
        # Passive queue adds can never displace it; KJ reorders reset it.
        self.pinned: int | None = None
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
                  "deadline", "transport", "next_entry_id", "pinned"):
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
              "deadline", "transport", "next_entry_id", "pinned")}
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
        """Run fn() under the lock, then journal + broadcast. The up-next pin
        is reconciled here so no mutation path can leave it dangling."""
        with self.lock:
            fn()
            self._reconcile_pin()
            self._save()
            self._broadcast(songs)

    def _reconcile_pin(self) -> None:
        """Drop a pin whose entry left the queue; when unpinned, lock in
        whoever is projected next RIGHT NOW (first projection wins -- later
        queue adds must not displace an announced next singer)."""
        if self.pinned is not None and \
                not any(e["id"] == self.pinned for e in self.queue):
            self.pinned = None
        if self.pinned is None:
            up = self.rotation_preview(1)
            if up:
                self.pinned = up[0]["id"]

    # -- views ---------------------------------------------------------------
    def rotation_preview(self, limit: int = 12) -> list:
        """Upcoming (singer, entry) order, simulated without mutating. A
        pinned entry is always first; the simulation continues from the
        rotation slot after its singer, so only the tail stays fluid."""
        entries = list(self.queue)
        cursor = self.cursor
        out = []
        pin = next((e for e in entries if e["id"] == self.pinned), None)
        if pin is not None:
            entries.remove(pin)
            out.append(pin)
            if pin["singer"] in self.singers:
                cursor = (self.singers.index(pin["singer"]) + 1) % len(self.singers)
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
            nv = len(s.get("versions") or [])
            row = {"id": e["id"], "singer": e["singer"], "song_id": e["song_id"],
                   "artist": s.get("artist", "?"), "title": s.get("title", "?"),
                   "duration": s.get("duration"), "nversions": nv}
            if nv > 1 and self.versions is not None:
                idx = self.versions.get(e["song_id"])
                # 1-based for display (v1 = best); out-of-range picks fall back
                # to the default, mirroring _media_path
                row["vsel"] = (idx if 0 <= idx < nv else 0) + 1
            return row
        with self.lock:
            up = self.rotation_preview()
            out = {
                "phase": self.phase,
                "deadline": self.deadline,
                "server_time": time.time(),
                "now": song_view(self.now) if self.now else None,
                "next": song_view(up[0]) if up else None,
                "upcoming": [song_view(e) for e in up],
                "queue": [song_view(e) for e in self.queue],
                "singers": list(self.singers),
                "transport": dict(self.transport),
                "pinned": self.pinned,
            }
            out.update(self.extra)
            return out


# --------------------------------------------------------------------------
# Flow control: the server owns the clock

class Flow:
    def __init__(self, state: State, songs: dict, cfg: dict, stats: Stats = None):
        self.state, self.songs, self.cfg = state, songs, cfg
        self.stats = stats

    def _info(self, song_id: str) -> dict:
        s = self.songs.get(song_id, {})
        return {"song_id": song_id, "artist": s.get("artist", "?"),
                "title": s.get("title", "?")}

    def _begin_next(self) -> None:
        """Move the next rotation entry on stage (caller holds no lock)."""
        st = self.state
        began = []

        def fn():
            # honor the locked up-next slot; fall back to the plain rotation
            e = next((x for x in st.queue if x["id"] == st.pinned), None)
            if e is not None and e["singer"] in st.singers:
                st.cursor = (st.singers.index(e["singer"]) + 1) % len(st.singers)
            else:
                e, st.cursor = pick_next(st.singers, st.cursor, st.queue)
            if e is None:
                st.phase, st.now = "idle", None
                return
            st.queue.remove(e)
            st.pinned = None  # consumed; mutate() re-pins the new next
            st.now = e
            st.phase = "playing"
            st.transport = {"cmd": "play", "seq": st.transport["seq"] + 1}
            began.append(e)
        st.mutate(self.songs, fn)
        if began and self.stats:
            self.stats.log("started", began[0]["singer"], self._info(began[0]["song_id"]))

    def song_ended(self, event: str = "completed") -> None:
        st = self.state
        ended = []

        def fn():
            if st.now is not None:
                ended.append(st.now)
            st.now = None
            st.phase = "intermission"
            st.deadline = time.time() + self.cfg["intermission_seconds"]
        st.mutate(self.songs, fn)
        if ended and self.stats:
            self.stats.log(event, ended[0]["singer"], self._info(ended[0]["song_id"]))

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
        self.song_ended("skipped")

    def restart_current(self) -> None:
        """KJ 'start over': re-seek the current song to 0:00 without touching
        the rotation or queue. The screen owns the audio clock, so this rides
        the transport channel as a one-shot 'restart' applied on seq change."""
        st = self.state

        def fn():
            if st.now is not None and st.phase == "playing":
                st.transport = {"cmd": "restart", "seq": st.transport["seq"] + 1}
        st.mutate(self.songs, fn)

    def skip_to_singer_next(self) -> None:
        """Skip the now-playing song but keep the SAME singer on stage,
        promoting their next queued entry immediately (e.g. the current track is
        broken). The rotation cursor is untouched, so the round-robin order is
        unaffected. Falls back to a normal skip if that singer has nothing else
        queued."""
        st = self.state
        skipped, started = [], []

        def fn():
            cur = st.now
            if cur is None:
                return
            skipped.append(cur)
            nxt = next((e for e in st.queue if e["singer"] == cur["singer"]), None)
            if nxt is None:  # nothing else from this singer: behave like a plain skip
                st.now = None
                st.phase = "intermission"
                st.deadline = time.time() + self.cfg["intermission_seconds"]
                return
            st.queue.remove(nxt)
            st.now = nxt
            st.phase = "playing"
            st.transport = {"cmd": "play", "seq": st.transport["seq"] + 1}
            started.append(nxt)
        st.mutate(self.songs, fn)
        if skipped and self.stats:
            self.stats.log("skipped", skipped[0]["singer"], self._info(skipped[0]["song_id"]))
        if started and self.stats:
            self.stats.log("started", started[0]["singer"], self._info(started[0]["song_id"]))

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


def make_handler(cfg: dict, cfg_path: Path, state: State, songs: dict, flow: Flow,
                 registry: SingerRegistry, stats: Stats, versions: VersionStore):
    media_cache = ROOT / ".media-cache"
    static_dir = ROOT / "static"
    # search order fixed once; sorting 50k+ rows per request would sting on a Pi
    ordered = sorted(songs.items(),
                     key=lambda kv: (kv[1]["artist"].lower(), kv[1]["title"].lower()))
    # Basic operator auth: a 4-digit PIN unlocks /kj + /setup and their
    # mutating APIs. Login mints an in-memory session token (dropped on restart
    # -> re-login) delivered as an HttpOnly cookie. Honor-system LAN app, so
    # this just keeps guests off the console, not a hardened auth system.
    sessions: set = set()
    sess_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # quiet
            pass

        def handle(self):
            # Phones and kiosk browsers drop connections constantly: page
            # reloads, aborted media range requests, walking out of WiFi
            # range. Routine, not worth a stack trace on the console.
            try:
                super().handle()
            except (ConnectionError, TimeoutError):
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

        @staticmethod
        def _int_arg(body: dict, key: str, default=None):
            """Integer body field, or None for absent/garbage values so the
            endpoint can 400 instead of tracebacking to a 500."""
            try:
                return int(body.get(key, default))
            except (TypeError, ValueError):
                return None

        # ---- media -------------------------------------------------------
        def _media_path(self, song_id: str, kind: str) -> Path | None:
            s = songs.get(song_id)
            if not s or kind not in ("mp3", "cdg"):
                return None
            # Resolve the active version. Version 0 reads the primary keys off
            # the song dict itself (identical to the pre-multi-version path);
            # an alternate reads from its own media dict with a per-version
            # cache key so extractions never collide.
            vlist = s.get("versions") or []
            idx = versions.get(song_id)
            if 0 < idx < len(vlist):
                v, cache_key = vlist[idx], f"{song_id}.v{idx}"
            else:
                v, cache_key = s, song_id
            if kind in v:
                return Path(v[kind])
            if kind == "cdg" and "mp3" in v:
                # sidecar entries carry only the mp3 path; find the twin lazily
                for suf in (".cdg", ".CDG", ".Cdg"):
                    c = Path(v["mp3"]).with_suffix(suf)
                    if c.exists():
                        v["cdg"] = str(c)
                        return c
                return None
            if "zip" in v:  # extract once into the cache
                if f"zip_{kind}" not in v:  # sidecar zips: discover members lazily
                    with zipfile.ZipFile(v["zip"]) as z:
                        for n in z.namelist():
                            ln = n.lower()
                            if ln.endswith(".mp3"):
                                v.setdefault("zip_mp3", n)
                            elif ln.endswith(".cdg"):
                                v.setdefault("zip_cdg", n)
                if f"zip_{kind}" not in v:
                    return None
                media_cache.mkdir(exist_ok=True)
                out = media_cache / f"{cache_key}.{kind}"
                if not out.exists():
                    with zipfile.ZipFile(v["zip"]) as z:
                        out.write_bytes(z.read(v[f"zip_{kind}"]))
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
        def _page(self, name: str, label: str):
            f = static_dir / name
            if f.exists():
                return self._html(f.read_text(encoding="utf-8"))
            return self._html(_PLACEHOLDER.format(page=label))

        def _authed(self) -> bool:
            for part in (self.headers.get("Cookie") or "").split(";"):
                part = part.strip()
                if part.startswith("kj_auth="):
                    with sess_lock:
                        return part[len("kj_auth="):] in sessions
            return False

        def do_GET(self):
            u = urlparse(self.path)
            parts = [p for p in u.path.split("/") if p]
            if u.path == "/":
                return self._page("singer.html", "Singer")
            if u.path in ("/kj", "/setup"):
                # operator surfaces: show the PIN gate until a valid session
                if not self._authed():
                    return self._page("kjlogin.html", "Locked")
                return self._page("kj.html" if u.path == "/kj" else "setup.html",
                                   "KJ console")
            if u.path == "/screen":
                return self._page("screen.html", "Screen")
            if len(parts) == 2 and parts[0] == "static":
                f = (static_dir / parts[1]).resolve()
                if f.is_file() and f.parent == static_dir.resolve():
                    ctype = {".js": "text/javascript", ".css": "text/css",
                             ".html": "text/html; charset=utf-8",
                             ".png": "image/png", ".svg": "image/svg+xml",
                             }.get(f.suffix, "application/octet-stream")
                    return self._serve_file(f, ctype)
                return self._json({"error": "not found"}, 404)
            if u.path == "/events":
                return self._events()
            if u.path == "/api/state":
                return self._json(state.snapshot(songs))
            if u.path == "/api/config":
                return self._json({"party_name": cfg["party_name"],
                                   "public_url": lan_url(cfg),
                                   "public_url_cfg": cfg["public_url"],
                                   "music_root": cfg["music_root"],
                                   "host": cfg["host"],
                                   "port": cfg["port"],
                                   "intermission_seconds": cfg["intermission_seconds"],
                                   "start_now_countdown_seconds": cfg["start_now_countdown_seconds"],
                                   "lyrics_offset_ms": cfg["lyrics_offset_ms"]})
            if u.path == "/api/stats/summary":
                played, queued, singers_c = Counter(), Counter(), Counter()
                events = resets = 0
                try:
                    with open(stats.path, encoding="utf-8") as f:
                        for line in f:
                            try:
                                row = json.loads(line)
                            except ValueError:
                                continue
                            events += 1
                            ev = row.get("event")
                            label = f"{row.get('artist', '?')} — {row.get('title', '?')}"
                            if ev == "completed":
                                played[label] += 1
                                singers_c[row.get("singer", "?")] += 1
                            elif ev == "queued":
                                queued[label] += 1
                            elif ev == "session_reset":
                                resets += 1
                except OSError:
                    pass
                return self._json({"events": events, "sessions": resets + 1,
                                   "top_played": played.most_common(10),
                                   "top_queued": queued.most_common(10),
                                   "top_singers": singers_c.most_common(10)})
            if u.path == "/api/song_versions":
                sid = parse_qs(u.query).get("song_id", [""])[0]
                s = songs.get(sid)
                if not s:
                    return self._json({"error": "unknown song"}, 404)
                vlist = s.get("versions") or []
                return self._json({
                    "song_id": sid, "artist": s["artist"], "title": s["title"],
                    "active": versions.get(sid),
                    "versions": [{"index": i, "label": v.get("label", f"Version {i + 1}"),
                                  "duration": v.get("duration")}
                                 for i, v in enumerate(vlist)],
                })
            if u.path == "/api/songs":
                qs = parse_qs(u.query)
                toks = _searchable(" ".join(qs.get("q", [""]))).split()
                field = qs.get("field", ["all"])[0]
                letter = qs.get("letter", [""])[0].upper()[:1]
                limit = min(int(qs.get("limit", ["50"])[0]), 200)
                key = {"artist": "sa", "title": "st"}.get(field, "search")
                out, total = [], 0
                for sid, s in ordered:
                    if letter and s["ltr"] != letter:
                        continue
                    if any(t not in s[key] for t in toks):
                        continue
                    total += 1
                    if len(out) < limit:
                        out.append({"song_id": sid, "artist": s["artist"],
                                    "title": s["title"], "duration": s.get("duration")})
                return self._json({"total": total, "songs": out})
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
            if u.path == "/api/kj/login":
                pin = str(body.get("pin", "")).strip()
                if pin and pin == str(cfg.get("kj_pin", "")):
                    tok = uuid.uuid4().hex
                    with sess_lock:
                        sessions.add(tok)
                    body_b = json.dumps({"ok": True}).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body_b)))
                    self.send_header("Set-Cookie",
                                     f"kj_auth={tok}; Path=/; Max-Age=86400; "
                                     "HttpOnly; SameSite=Lax")
                    self.end_headers()
                    self.wfile.write(body_b)
                    return
                return self._json({"error": "wrong PIN"}, 401)
            if u.path == "/api/kj/logout":
                for part in (self.headers.get("Cookie") or "").split(";"):
                    part = part.strip()
                    if part.startswith("kj_auth="):
                        with sess_lock:
                            sessions.discard(part[len("kj_auth="):])
                return self._json({"ok": True})
            # gate the operator mutations (login/logout handled above)
            if (u.path.startswith("/api/kj/") or u.path == "/api/setup/config") \
                    and not self._authed():
                return self._json({"error": "auth required"}, 401)
            if u.path == "/api/singers":
                raw = (body.get("name") or "").strip()[:40]
                if not raw:
                    return self._json({"error": "name required"}, 400)
                name, _sid = registry.resolve(raw)  # canonical casing, stable id

                def fn():
                    if name not in state.singers:
                        state.singers.append(name)
                state.mutate(songs, fn)
                return self._json({"ok": True, "name": name, "singers": state.singers})
            if u.path == "/api/queue":
                sid = body.get("song_id")
                raw = (body.get("singer") or "").strip()[:40]
                if sid not in songs or not raw:
                    return self._json({"error": "song_id and singer required"}, 400)
                singer, _ = registry.resolve(raw)

                def fn():
                    if singer not in state.singers:
                        state.singers.append(singer)
                    state.queue.append({"id": state.next_entry_id,
                                        "singer": singer, "song_id": sid})
                    state.next_entry_id += 1
                state.mutate(songs, fn)
                stats.log("queued", singer, flow._info(sid))
                return self._json({"ok": True})
            if u.path == "/api/screen/ended":
                flow.song_ended()
                return self._json({"ok": True})
            if u.path == "/api/kj/pin":
                eid = self._int_arg(body, "entry_id")
                if eid is None:
                    return self._json({"error": "entry_id must be a number"}, 400)
                ok = []
                def fn():
                    # explicit KJ override: hand the locked up-next slot to
                    # any queued entry (reconcile validates it stays sane)
                    ok.append(any(e["id"] == eid for e in state.queue))
                    if ok[0]:
                        state.pinned = eid
                state.mutate(songs, fn)
                return self._json({"ok": ok[0]})
            if u.path == "/api/kj/entry_move":
                eid = self._int_arg(body, "entry_id")
                direction = self._int_arg(body, "dir")
                if eid is None or direction is None:
                    return self._json({"error": "entry_id and dir must be numbers"}, 400)
                moved = []
                def fn():
                    moved.append(move_entry(state.queue, eid, direction))
                    if moved[0]:  # KJ reorder overrides the up-next lock
                        state.pinned = None
                state.mutate(songs, fn)
                return self._json({"ok": moved[0]})
            if u.path == "/api/kj/singer_move":
                direction = self._int_arg(body, "dir")
                if direction is None:
                    return self._json({"error": "dir must be a number"}, 400)
                moved = []
                def fn():
                    moved.append(move_singer(state.singers,
                                             (body.get("name") or "").strip(),
                                             direction))
                    if moved[0]:  # KJ reorder overrides the up-next lock
                        state.pinned = None
                state.mutate(songs, fn)
                return self._json({"ok": moved[0]})
            if u.path == "/api/kj/singer_remove":
                name = (body.get("name") or "").strip()
                dropped = []
                def fn():
                    if name in state.singers:
                        state.singers.remove(name)
                    dropped.extend(e for e in state.queue if e["singer"] == name)
                    state.queue = [e for e in state.queue if e["singer"] != name]
                    if state.cursor >= len(state.singers):
                        state.cursor = 0
                state.mutate(songs, fn)
                for e in dropped:
                    stats.log("removed", e["singer"], flow._info(e["song_id"]))
                return self._json({"ok": True})
            if u.path == "/api/kj/reset":
                def fn():
                    state.queue = []
                    state.singers = []
                    state.cursor = 0
                    state.now = None
                    state.pinned = None
                    state.phase = "idle"
                    state.deadline = 0.0
                state.mutate(songs, fn)
                stats.log("session_reset")  # party boundary marker for summaries
                return self._json({"ok": True})
            if u.path == "/api/kj/rescan":
                fresh = scan_library(cfg["music_root"])  # fs walk outside the lock
                def fn():
                    songs.clear()
                    songs.update(fresh)
                    ordered[:] = sorted(songs.items(),
                                        key=lambda kv: (kv[1]["artist"].lower(),
                                                        kv[1]["title"].lower()))
                state.mutate(songs, fn)
                return self._json({"ok": True, "count": len(songs)})
            if u.path == "/api/setup/config":
                changes, errors, restart = validate_config_changes(cfg, body)
                count = None
                if "music_root" in changes:
                    # fs walk outside the lock, like /api/kj/rescan; a path
                    # with nothing indexed must not strand the party
                    fresh = scan_library(changes["music_root"])
                    if fresh:
                        count = len(fresh)
                    else:
                        errors.append("no songs found under that music_root"
                                      " -- keeping the current library")
                        changes.pop("music_root")

                def fn():
                    cfg.update(changes)
                    if "lyrics_offset_ms" in changes:
                        state.extra["lyrics_offset_ms"] = cfg["lyrics_offset_ms"]
                    if count is not None:
                        songs.clear()
                        songs.update(fresh)
                        ordered[:] = sorted(songs.items(),
                                            key=lambda kv: (kv[1]["artist"].lower(),
                                                            kv[1]["title"].lower()))
                state.mutate(songs, fn)
                if changes:
                    try:
                        cfg_path.write_text(json.dumps(cfg, indent=2),
                                            encoding="utf-8")
                    except OSError:
                        errors.append("could not write config.json")
                return self._json({"ok": not errors, "applied": sorted(changes),
                                   "errors": errors, "restart_needed": restart,
                                   "count": count})
            if u.path == "/api/kj/offset":
                delta = self._int_arg(body, "delta", 0)
                if delta is None:
                    return self._json({"error": "delta must be a number"}, 400)
                def fn():
                    v = int(cfg.get("lyrics_offset_ms", 0)) + delta
                    cfg["lyrics_offset_ms"] = max(-2000, min(2000, v))
                    state.extra["lyrics_offset_ms"] = cfg["lyrics_offset_ms"]
                state.mutate(songs, fn)
                try:  # calibration should survive a restart
                    cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
                except OSError:
                    pass
                return self._json({"ok": True, "lyrics_offset_ms": cfg["lyrics_offset_ms"]})
            if u.path == "/api/kj/version":
                sid = body.get("song_id")
                s = songs.get(sid)
                if not s:
                    return self._json({"error": "unknown song"}, 400)
                n = len(s.get("versions") or [])
                idx = self._int_arg(body, "index", 0)
                if idx is None:  # garbage must 400, not silently mean "best"
                    return self._json({"error": "index must be a number"}, 400)
                if idx < 0 or idx >= n:
                    return self._json({"error": "version out of range"}, 400)
                versions.set(sid, idx)
                # nudge the surfaces so a version swap shows up live
                state.mutate(songs, lambda: None)
                return self._json({"ok": True, "song_id": sid, "active": idx})
            if u.path.startswith("/api/kj/"):
                cmd = u.path.rsplit("/", 1)[1]
                if cmd in ("play", "pause"):
                    flow.transport_cmd(cmd)
                elif cmd == "skip":
                    flow.skip()
                elif cmd == "restart":
                    flow.restart_current()
                elif cmd == "skip_singer":
                    flow.skip_to_singer_next()
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
            dropped = []

            def fn():
                dropped.extend(e for e in state.queue if e["id"] == eid)
                state.queue = [e for e in state.queue if e["id"] != eid]
            state.mutate(songs, fn)
            for e in dropped:
                stats.log("removed", e["singer"], flow._info(e["song_id"]))
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
    state.extra["lyrics_offset_ms"] = cfg["lyrics_offset_ms"]
    registry = SingerRegistry(ROOT / "singers.json")
    stats = Stats(ROOT / "stats.jsonl", registry)
    versions = VersionStore(ROOT / "versions.json")
    state.versions = versions  # snapshots surface each song's active pick
    flow = Flow(state, songs, cfg, stats)
    threading.Thread(target=flow.tick_forever, daemon=True).start()
    server = ThreadingHTTPServer((cfg["host"], cfg["port"]),
                                 make_handler(cfg, cfg_path, state, songs, flow,
                                              registry, stats, versions))
    server.daemon_threads = True
    print(f"[{APP}] singers: {lan_url(cfg)}  |  KJ: {lan_url(cfg)}kj  |  screen: {lan_url(cfg)}screen")
    server.serve_forever()


if __name__ == "__main__":
    main()
