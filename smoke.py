#!/usr/bin/env python3
"""KriticalDJ live smoke test -- stdlib only, run with:  python smoke.py

Stands a REAL server up on a spare port against a throwaway library, drives it
over HTTP the way a phone and the TV do, and asserts on the responses.
test_core.py covers the model underneath; this covers the request-handling
layer above it, which is where the bugs found at parties have actually lived.
Every route the server exposes is exercised, and CI runs this on every pull
request (#34), so that layer is checked before anything merges rather than
only when somebody remembers.

    python smoke.py                 # pick a free port automatically
    python smoke.py --port 8099     # or name one
    python smoke.py --keep          # leave the workspace behind to poke at

TWO TRAPS, designed out here so they stop being re-learned (#35):

1. `music_root` must be a NATIVE path. An MSYS-style "/c/Users/..." string
   indexes ZERO songs rather than failing, and every assertion after that
   passes against an empty library and means nothing. The path below is built
   with pathlib, which is always native form, and the library check fails hard
   on a song count that does not match the fixture.
2. Do NOT plumb song ids through a shell. Reading them with bash `mapfile -t`
   leaves a trailing carriage return on each id, which poisons the JSON bodies
   and produces 400s that read like server bugs. Everything here, ids
   included, stays in Python.

The server writes state.json, singers.json and friends next to kriticaldj.py,
so the app is COPIED into the workspace: a smoke run must never scribble on
the repo checkout or on a real party's state. For the same reason it binds
127.0.0.1 rather than the configured 0.0.0.0, which also avoids a Windows
firewall prompt on every run.

There are TWO libraries. The first is a plain folder walk of pairs and a zip.
The second is described by an index.json sidecar, the way song-sorter emits
one, and carries what a folder walk cannot: alternate versions and per-copy
musical keys. The run switches to it part way through via /api/setup/config,
which tests that route too. They are kept apart on purpose: when a sidecar
exists the scanner skips the folder walk entirely, so one combined library
would quietly stop the pair-and-zip check testing the folder walk at all.
"""
import argparse
import http.client
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
KJ_PIN = "4271"          # deliberately not the 0000 default: proves it is read
INTERMISSION = 1         # seconds; the real default of 15 makes a smoke run crawl
MP3_BYTES = b"ID3" + bytes(range(64))   # 67 bytes, known size for the range checks
# three distinct payloads, one per copy of a song, so a media fetch can prove
# WHICH copy was served rather than merely that something was
VERSION_BYTES = [b"ID3" + bytes([n]) * 40 for n in (1, 2, 3)]


# --------------------------------------------------------------------------
# Workspace

def build_library(root: Path) -> int:
    """Write a tiny fixture library. Returns how many songs it should index.

    Covers both shapes the scanner handles: letter/artist/stem.mp3 + .cdg
    pairs, and a zip holding both members. The contents are nonsense -- nothing
    here plays audio, and the range checks want a known, small size.
    """
    pairs = [("b", "beach boys", "SC8121-03 - Beach Boys - Barbara Ann"),
             ("q", "queen", "SF042-11 - Queen - Bohemian Rhapsody")]
    for letter, artist, stem in pairs:
        d = root / letter / artist
        d.mkdir(parents=True, exist_ok=True)
        (d / (stem + ".mp3")).write_bytes(MP3_BYTES)
        (d / (stem + ".cdg")).write_bytes(b"\x00" * 96)
    z = root / "a" / "abba"
    z.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(z / "SC1234-01 - ABBA - Waterloo.zip", "w") as zf:
        zf.writestr("track.mp3", MP3_BYTES)
        zf.writestr("track.cdg", b"\x00" * 48)
    return len(pairs) + 1


def build_sidecar_library(root: Path) -> int:
    """Write the second library, indexed by an index.json sidecar. Returns how
    many songs it should index.

    One song has three copies: the best (keyed), a duet keyed differently
    (alternates are often transposed), and a zipped radio edit with no key of
    its own. The rest exercise the key-tone rules: a confident machine key
    plays, a doubtful one stays silent, a hand-curated one plays whatever its
    confidence, and a song with no key stays silent.
    """
    def pair(rel, payload):
        mp3 = root / rel
        mp3.parent.mkdir(parents=True, exist_ok=True)
        mp3.write_bytes(payload)
        mp3.with_suffix(".cdg").write_bytes(bytes(96))
        return rel

    radio = "k/kenny rogers/Islands In The Stream (Radio).zip"
    songs = [
        {"path": pair("k/kenny rogers/Islands In The Stream.mp3", VERSION_BYTES[0]),
         "artist": "Kenny Rogers", "title": "Islands In The Stream", "duration": 250,
         "key": "A# major", "key_confidence": 0.85, "key_source": "auto",
         "versions": [
             {"path": pair("k/kenny rogers/Islands In The Stream (Duet).mp3",
                           VERSION_BYTES[1]),
              "label": "Duet", "duration": 252,
              "key": "D major", "key_confidence": 0.9, "key_source": "auto"},
             {"path": radio, "label": "Radio Edit", "duration": 201},
         ]},
        {"path": pair("d/dolly parton/Jolene.mp3", MP3_BYTES),
         "artist": "Dolly Parton", "title": "Jolene", "duration": 162,
         "key": "C# minor", "key_confidence": 0.88, "key_source": "auto"},
        {"path": pair("t/toto/Africa.mp3", MP3_BYTES),
         "artist": "Toto", "title": "Africa", "duration": 295,
         "key": "A major", "key_confidence": 0.2, "key_source": "auto"},
        {"path": pair("b/bill withers/Lean On Me.mp3", MP3_BYTES),
         "artist": "Bill Withers", "title": "Lean On Me", "duration": 255,
         "key": "C major", "key_source": "manual"},
        {"path": pair("a/amy winehouse/Valerie.mp3", MP3_BYTES),
         "artist": "Amy Winehouse", "title": "Valerie", "duration": 233},
    ]
    with zipfile.ZipFile(root / radio, "w") as zf:
        zf.writestr("track.mp3", VERSION_BYTES[2])
        zf.writestr("track.cdg", bytes(48))
    (root / "index.json").write_text(json.dumps({"version": 1, "songs": songs}),
                                     encoding="utf-8")
    return len(songs)


def build_workspace(tmp: Path, port: int):
    """Copy the app beside a fresh config and both libraries.

    Returns (config path, expected song count, workspace facts the later
    checks need).
    """
    shutil.copy2(ROOT / "kriticaldj.py", tmp / "kriticaldj.py")
    shutil.copytree(ROOT / "static", tmp / "static")
    library = tmp / "library"
    library.mkdir()
    expected = build_library(library)
    library_b = tmp / "library-b"
    library_b.mkdir()
    empty = tmp / "library-empty"   # a real folder with nothing to index
    empty.mkdir()
    cfg = {
        "music_root": str(library),   # pathlib -> native form; see trap 1
        "host": "127.0.0.1",
        "port": port,
        "party_name": "Smoke Test Night",
        "intermission_seconds": INTERMISSION,
        "start_now_countdown_seconds": 0,
        "kj_pin": KJ_PIN,
    }
    cfg_path = tmp / "config.json"
    cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    ws = {"tmp": tmp, "port": port, "expected_a": expected,
          "lib_b": library_b, "expected_b": build_sidecar_library(library_b),
          "lib_empty": empty}
    return cfg_path, expected, ws


def free_port() -> int:
    """Ask the OS for an unused port. Racy in principle, fine in practice."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --------------------------------------------------------------------------
# HTTP client

class Response:
    def __init__(self, status, headers, body):
        self.status, self.headers, self.body = status, headers, body

    @property
    def json(self):
        try:
            return json.loads(self.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None

    @property
    def text(self):
        return self.body.decode("utf-8", "replace")


class Client:
    """Cookie-aware JSON client. urllib only, so the zero-dependency rule holds."""

    def __init__(self, base):
        self.base, self.cookie = base, None

    def __call__(self, method, path, body=None, headers=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        h = dict(headers or {})
        if data:
            h["Content-Type"] = "application/json"
        if self.cookie:
            h["Cookie"] = self.cookie
        req = urllib.request.Request(self.base + path, data=data, headers=h,
                                     method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                resp = Response(r.status, dict(r.headers), r.read())
        except urllib.error.HTTPError as e:   # 4xx/5xx are answers, not crashes
            resp = Response(e.code, dict(e.headers), e.read())
        cookie = resp.headers.get("Set-Cookie")
        if cookie:
            self.cookie = cookie.split(";")[0]
        return resp

    def get(self, path, **kw):
        return self("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self("POST", path, body if body is not None else {}, **kw)


class EventStream:
    """A live /events subscription, read one event at a time.

    urllib reads the whole body before returning, which never happens on a
    stream, so this speaks http.client directly. Nothing here sleeps or races:
    the server registers the listener BEFORE it writes the first snapshot, so
    a change made after that first read is guaranteed to be delivered. The
    socket timeout only bounds a failure, so a missing push fails in seconds
    instead of hanging the run.
    """

    def __init__(self, port):
        self.conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        self.conn.request("GET", "/events")
        self.resp = self.conn.getresponse()

    def next_event(self):
        """The next data payload, parsed. Keepalive comments are skipped."""
        while True:
            line = self.resp.readline()
            if not line:
                raise Fatal("the event stream closed")
            if line.startswith(b"data: "):
                return json.loads(line[len(b"data: "):].decode("utf-8"))

    def close(self):
        self.conn.close()


# --------------------------------------------------------------------------
# Assertions

class Fatal(Exception):
    """A failure that makes every later assertion meaningless."""


FAILURES = []
PASSED = 0


def check(label, ok, detail=""):
    global PASSED
    if ok:
        PASSED += 1
        print("  ok    " + label)
    else:
        FAILURES.append((label, detail))
        print("  FAIL  " + label + ("  (" + detail + ")" if detail else ""))
    return ok


def wait_for(c, pred, timeout=10.0, what="condition"):
    """Poll /api/state until pred(state) holds.

    The scheduler thread runs twice a second, so anything phase-related needs a
    wait rather than a read.
    """
    deadline = time.time() + timeout
    snap = {}
    while time.time() < deadline:
        snap = c.get("/api/state").json or {}
        if pred(snap):
            return snap
        time.sleep(0.1)
    raise Fatal("timed out after %ss waiting for %s; phase=%r now=%s"
                % (timeout, what, snap.get("phase"), snap.get("now")))


def section(name, fn, *args):
    """Run one group of checks and contain its failure to that group.

    A response that changed shape raises (a KeyError on a field that moved,
    say), and a scheduler wait can time out. Either way that group is done,
    but the groups are independent, so record it and carry on rather than let
    one broken endpoint hide every result after it.
    """
    print("  --    " + name)
    try:
        fn(*args)
    except Exception as exc:
        where = [ln.strip() for ln in traceback.format_exc().splitlines()
                 if ln.strip().startswith("File ")]
        detail = "%s: %s" % (type(exc).__name__, exc)
        FAILURES.append((name + " stopped early", detail))
        print("  FAIL  %s stopped early: %s" % (name, detail))
        if where:
            print("        at " + where[-1])


# --------------------------------------------------------------------------
# The party

def run_checks(c, expected_songs, ws):
    # --- library ----------------------------------------------------------
    songs = c.get("/api/songs?limit=200").json
    # Trap 1's guard: a bad music_root indexes 0 songs, and everything below
    # would then "pass" against an empty library.
    if songs["total"] != expected_songs:
        raise Fatal("indexed %d songs, expected %d -- check music_root is a "
                    "native path" % (songs["total"], expected_songs))
    check("library indexed", True, "%d songs" % songs["total"])
    ids = dict((s["title"], s["song_id"]) for s in songs["songs"])
    check("pair and zip shapes both index",
          set(["Barbara Ann", "Bohemian Rhapsody", "Waterloo"]) <= set(ids),
          str(sorted(ids)))

    cfg = c.get("/api/config").json
    check("config is served from the file we wrote",
          cfg["party_name"] == "Smoke Test Night"
          and cfg["intermission_seconds"] == INTERMISSION,
          "%r / %s" % (cfg["party_name"], cfg["intermission_seconds"]))

    st = c.get("/api/state").json
    check("starts idle", st["phase"] == "idle" and not st["singers"], st["phase"])

    # --- singers and queue ------------------------------------------------
    r = c.post("/api/singers", {"name": "Ann"})
    check("singer joins", r.status == 200 and "Ann" in r.json["singers"], str(r.json))
    r = c.post("/api/singers", {"name": "ann"})
    check("duplicate name refused, case-insensitively",
          r.status == 409 and r.json.get("code") == "name_taken",
          "%s %s" % (r.status, r.json))

    r = c.post("/api/queue", {"song_id": ids["Barbara Ann"], "singer": "Ann"})
    check("queue accepts a song", r.status == 200, "%s %s" % (r.status, r.json))
    r = c.post("/api/queue", {"song_id": ids["Waterloo"], "singer": "Bob"})
    check("queueing enrolls an unknown singer", r.status == 200,
          "%s %s" % (r.status, r.json))
    r = c.post("/api/queue", {"song_id": "nope", "singer": "Ann"})
    check("unknown song_id refused", r.status == 400, str(r.status))

    st = c.get("/api/state").json
    check("both singers in rotation", st["singers"] == ["Ann", "Bob"],
          str(st["singers"]))
    check("next is announced and locked", st["next"]["singer"] == "Ann",
          str(st["next"]))

    # --- flow -------------------------------------------------------------
    st = wait_for(c, lambda s: s["phase"] == "playing", what="the first song")
    check("first song plays, first singer up", st["now"]["singer"] == "Ann",
          str(st["now"]))

    # --- media range (the #17 shape: a bad Range must not send a bad length) --
    sid = ids["Barbara Ann"]
    size = len(MP3_BYTES)
    r = c.get("/media/%s/mp3" % sid, headers={"Range": "bytes=0-3"})
    check("satisfiable range returns 206 with a matching length",
          r.status == 206 and r.headers.get("Content-Length") == "4"
          and len(r.body) == 4,
          "%s len=%s got=%d" % (r.status, r.headers.get("Content-Length"),
                                len(r.body)))
    r = c.get("/media/%s/mp3" % sid, headers={"Range": "bytes=999999-"})
    check("unsatisfiable range returns 416, never a negative length",
          r.status == 416 and r.headers.get("Content-Length") == "0",
          "%s len=%s" % (r.status, r.headers.get("Content-Length")))
    r = c.get("/media/%s/mp3" % sid)
    check("plain media request returns the whole file",
          r.status == 200 and len(r.body) == size,
          "%s got=%d want=%d" % (r.status, len(r.body), size))

    # --- operator gate ----------------------------------------------------
    r = c.post("/api/kj/skip")
    check("KJ command refused without a session", r.status == 401, str(r.status))
    check("KJ page shows the lock", "KJ access" in c.get("/kj").text)
    r = c.post("/api/kj/login", {"pin": "0000"})
    check("wrong PIN refused", r.status == 401, str(r.status))
    check("wrong PIN sets no cookie", c.cookie is None, str(c.cookie))
    r = c.post("/api/kj/login", {"pin": KJ_PIN})
    check("right PIN opens a session", r.status == 200 and c.cookie is not None,
          "%s cookie=%r" % (r.status, c.cookie))
    check("KJ page unlocks", "KJ Console" in c.get("/kj").text)

    # --- transport --------------------------------------------------------
    r = c.post("/api/kj/skip")
    check("skip is accepted once authed", r.status == 200, str(r.status))
    st = wait_for(c, lambda s: s["phase"] == "playing"
                  and s["now"]["singer"] == "Bob",
                  what="the rotation to reach Bob")
    check("rotation advanced to the next singer", True, st["now"]["singer"])

    r = c.post("/api/screen/ended")
    check("screen can end a song", r.status == 200, str(r.status))
    st = wait_for(c, lambda s: s["held"] is True,
                  what="the empty-queue hold")
    check("empty queue parks in intermission rather than idling",
          st["phase"] == "intermission", str(st["phase"]))

    # --- stats and reset --------------------------------------------------
    summary = c.get("/api/stats/summary").json
    check("stats recorded the session", summary["events"] > 0,
          str(summary["events"]))

    r = c.post("/api/kj/reset")
    check("reset is accepted", r.status == 200, str(r.status))
    st = c.get("/api/state").json
    check("reset clears the party",
          st["singers"] == [] and st["queue"] == [] and st["now"] is None,
          str(st))

    # --- surfaces still serve --------------------------------------------
    for path in ("/", "/screen", "/kiosk"):
        r = c.get(path)
        check("%s serves" % path,
              r.status == 200 and "<html" in r.text.lower(), str(r.status))

    # --- the rest of the API (#34) ----------------------------------------
    # Everything above predates #34 and is untouched. The groups below reach
    # every route it does not, each contained by section() so one broken
    # endpoint cannot hide the results of the others.
    section("media and static files", check_media_and_static, c, ids)
    section("setup config and library switch", check_setup, c, ws)
    ids = song_ids(c)          # the sidecar library: new songs, new ids
    if "Islands In The Stream" not in ids:
        raise Fatal("the sidecar library is not active, so every check after "
                    "this would run against the wrong songs")
    section("song versions", check_versions, c, ids)
    section("key tone and singer prefs", check_key_tone, c, ids)
    section("accounts and PINs", check_accounts, c, ids)
    section("saved lists", check_lists, c, ids)
    section("random song", check_random, c, ids)
    section("KJ board edits", check_kj_board, c, ids, ws)
    section("KJ transport", check_kj_transport, c, ids)
    section("live event stream", check_events, c, ws)
    section("logout", check_logout, c)   # last: it ends the operator session


# --------------------------------------------------------------------------
# The rest of the API (#34)

def song_ids(c):
    songs = c.get("/api/songs?limit=200").json["songs"]
    return dict((s["title"], s["song_id"]) for s in songs)


def hold_board(c):
    """Clear the board and put Pause down BEFORE anything is queued.

    A manual pause freezes the intermission, and the scheduler checks for a
    hold before it checks the deadline, so nothing queued after this is ever
    consumed. Every read of the board then sees exactly what was queued, with
    no race against the scheduler thread.
    """
    c.post("/api/kj/reset")
    c.post("/api/kj/pause")


def settle(c):
    """Leave the board empty and the transport running for the next group.
    Reset alone is not enough: it clears the party but leaves Pause down."""
    c.post("/api/kj/reset")
    c.post("/api/kj/play")


def check_media_and_static(c, ids):
    # The first pass only fetched a plain pair. A zipped song is extracted
    # into the media cache on first request, then served like a file.
    wid = ids["Waterloo"]
    r = c.get("/media/%s/mp3" % wid)
    check("a zipped song's audio is extracted and served",
          r.status == 200 and r.body == MP3_BYTES,
          "%s got=%d" % (r.status, len(r.body)))
    r = c.get("/media/%s/cdg" % wid)
    check("and so are its lyrics", r.status == 200 and len(r.body) == 48,
          "%s got=%d" % (r.status, len(r.body)))
    for path, why in (("/media/%s/wav" % wid, "unknown media kind"),
                      ("/media/nope/mp3", "unknown song")):
        r = c.get(path)
        check("media refused: %s" % why, r.status == 404, str(r.status))
    # static/ serves its own files and nothing outside it
    r = c.get("/static/screen.html")
    check("a static file serves",
          r.status == 200 and "<html" in r.text.lower(), str(r.status))
    # Only the raw-backslash form reaches the parent-directory guard. On
    # Windows a backslash is a separator, and with the guard removed that
    # request serves the server's own source (measured). The other forms are
    # refused by routing before the guard, so on their own they passed with
    # or without it. On Linux, the Pi and CI, a backslash is an ordinary
    # filename character: there the guard is defense in depth and this check
    # cannot fail, so it only has teeth on a Windows box.
    backslash = chr(92)   # spelled out so no editor or shell can mangle it
    for path in ("/static/.." + backslash + "kriticaldj.py",
                 "/static/..%2Fkriticaldj.py", "/static/../kriticaldj.py",
                 "/static/.."):
        r = c.get(path)
        check("static cannot climb out: %r" % path,
              r.status == 404 and b"import" not in r.body, str(r.status))


def check_setup(c, ws):
    anon = Client(c.base)
    r = anon.post("/api/setup/config", {"party_name": "Hijacked"})
    check("setup config refused without a session", r.status == 401, str(r.status))
    r = anon.post("/api/kj/rescan")
    check("rescan refused without a session", r.status == 401, str(r.status))

    # a music_root with nothing under it must not strand the party mid-night
    r = c.post("/api/setup/config", {"music_root": str(ws["lib_empty"])})
    j = r.json or {}
    check("an empty music_root is refused with a reason",
          j.get("ok") is False and "music_root" not in j.get("applied", [])
          and any("no songs" in e for e in j.get("errors", [])), str(j))
    total = c.get("/api/songs?limit=1").json["total"]
    check("and the current library is kept", total == ws["expected_a"], str(total))

    # switch to the sidecar library, where versions and keys live
    r = c.post("/api/setup/config", {"music_root": str(ws["lib_b"])})
    j = r.json or {}
    if not (r.status == 200 and j.get("count") == ws["expected_b"]):
        raise Fatal("could not switch to the sidecar library: %s %s"
                    % (r.status, j))
    check("switching music_root rescans into the new library", True,
          "%d songs" % j["count"])
    saved = json.loads((ws["tmp"] / "config.json").read_text(encoding="utf-8"))
    check("and the new music_root is saved to config.json",
          saved.get("music_root") == str(ws["lib_b"]), str(saved.get("music_root")))

    r = c.post("/api/setup/config", {"party_name": "Smoke Test Encore"})
    j = r.json or {}
    check("a live config change applies",
          j.get("ok") is True and j.get("applied") == ["party_name"], str(j))
    check("and shows on /api/config straight away",
          c.get("/api/config").json["party_name"] == "Smoke Test Encore")
    r = c.post("/api/setup/config", {"intermission_seconds": "soon"})
    j = r.json or {}
    check("a malformed value is refused with a reason",
          j.get("ok") is False and bool(j.get("errors")), str(j))

    # a song added to the sidecar after startup appears on rescan
    lib, rel = ws["lib_b"], "t/the killers/Mr. Brightside.mp3"
    (lib / rel).parent.mkdir(parents=True, exist_ok=True)
    (lib / rel).write_bytes(MP3_BYTES)
    (lib / rel).with_suffix(".cdg").write_bytes(bytes(96))
    index = json.loads((lib / "index.json").read_text(encoding="utf-8"))
    index["songs"].append({"path": rel, "artist": "The Killers",
                           "title": "Mr. Brightside"})
    (lib / "index.json").write_text(json.dumps(index), encoding="utf-8")
    r = c.post("/api/kj/rescan")
    check("rescan picks up a song added since startup",
          r.status == 200 and (r.json or {}).get("count") == ws["expected_b"] + 1,
          "%s %s" % (r.status, r.json))
    found = [s["title"] for s in c.get("/api/songs?q=brightside").json["songs"]]
    check("and it is searchable straight away", found == ["Mr. Brightside"],
          str(found))


def check_versions(c, ids):
    isl = ids["Islands In The Stream"]
    j = c.get("/api/song_versions?song_id=%s" % isl).json
    check("a song's versions are listed best first, with labels",
          [v["label"] for v in j["versions"]] == ["Best", "Duet", "Radio Edit"]
          and j["active"] == 0, str(j))
    check("each carries its own duration",
          [v["duration"] for v in j["versions"]] == [250, 252, 201],
          str(j["versions"]))
    r = c.get("/api/song_versions?song_id=nope")
    check("versions of an unknown song: 404", r.status == 404, str(r.status))

    # which copy plays: the KJ's global pick, unless an entry names its own
    def fetch(query=""):
        body = c.get("/media/%s/mp3%s" % (isl, query)).body
        return body, "got %d bytes, marker %r" % (len(body), body[3:4])

    body, got = fetch()
    check("the best copy plays by default", body == VERSION_BYTES[0], got)
    anon = Client(c.base)
    r = anon.post("/api/kj/version", {"song_id": isl, "index": 1})
    check("choosing a version refused without a session", r.status == 401,
          str(r.status))
    r = c.post("/api/kj/version", {"song_id": isl, "index": 1})
    check("the KJ can promote an alternate",
          r.status == 200 and (r.json or {}).get("active") == 1, str(r.json))
    check("the choice is reported back",
          c.get("/api/song_versions?song_id=%s" % isl).json["active"] == 1)
    body, got = fetch()
    check("and it is what plays now", body == VERSION_BYTES[1], got)
    body, got = fetch("?v=0")
    check("an entry's own version still wins over it", body == VERSION_BYTES[0], got)
    body, got = fetch("?v=2")
    check("a zipped alternate is extracted and served", body == VERSION_BYTES[2], got)
    for req, why in (({"song_id": isl, "index": 3}, "index out of range"),
                     ({"song_id": isl, "index": "two"}, "index not a number"),
                     ({"song_id": "nope", "index": 0}, "unknown song")):
        r = c.post("/api/kj/version", req)
        check("version choice refused: %s" % why, r.status == 400, str(r.status))
    c.post("/api/kj/version", {"song_id": isl, "index": 0})   # back to the best


def check_key_tone(c, ids):
    hold_board(c)
    who, isl = "Kira", ids["Islands In The Stream"]
    for sid, ver in ((ids["Jolene"], None), (ids["Africa"], None),
                     (ids["Lean On Me"], None), (ids["Valerie"], None),
                     (isl, 1), (isl, 2)):
        req = {"song_id": sid, "singer": who}
        if ver is not None:
            req["version"] = ver
        c.post("/api/queue", req)
    up = c.get("/api/state").json["upcoming"]
    check("six entries queued, in order", len(up) == 6, str(len(up)))
    jol, afr, lean, val, duet, radio = (up + [{}] * 6)[:6]
    check("a confident machine key plays its tone",
          jol.get("key_tone") is True and jol.get("key") == "C# minor"
          and len(jol.get("tone_hz") or []) == 3, str(jol))
    check("a doubtful machine key stays silent", not afr.get("key_tone"), str(afr))
    check("a hand-curated key plays whatever its confidence",
          lean.get("key_tone") is True and lean.get("key") == "C major", str(lean))
    check("a song with no key stays silent", not val.get("key_tone"), str(val))
    check("an alternate plays its OWN key, not the best copy's",
          duet.get("key") == "D major" and duet.get("vsel") == 2
          and duet.get("ver") == 1, str(duet))
    check("an unkeyed alternate never borrows a sibling's key",
          not radio.get("key_tone") and "key" not in radio, str(radio))

    j = c.get("/api/prefs?singer=%s" % who).json
    check("a singer's key tone defaults to on",
          j.get("key_tone") is True and j.get("key_tone_enabled") is True, str(j))
    r = c.post("/api/prefs", {"singer": who, "key_tone": False})
    check("a singer can opt out",
          r.status == 200 and (r.json or {}).get("key_tone") is False, str(r.json))
    check("the opt-out reads back",
          c.get("/api/prefs?singer=%s" % who).json.get("key_tone") is False)
    first = c.get("/api/state").json["upcoming"][0]
    check("and their songs go silent", not first.get("key_tone"), str(first))
    c.post("/api/prefs", {"singer": who, "key_tone": True})
    first = c.get("/api/state").json["upcoming"][0]
    check("opting back in brings the tone back", first.get("key_tone") is True,
          str(first))
    r = c.post("/api/prefs", {"key_tone": False})
    check("a pref change needs a singer", r.status == 400, str(r.status))
    settle(c)


def check_accounts(c, ids):
    hold_board(c)
    # --- registering ------------------------------------------------------
    r = c.post("/api/accounts", {"name": "Dana"})
    j = r.json or {}
    check("a regular registers with no PIN",
          r.status == 200 and j.get("name") == "Dana" and j.get("pin") is False,
          str(j))
    r = c.post("/api/accounts", {"name": "dana"})
    check("a registered name cannot be registered again",
          r.status == 409 and (r.json or {}).get("code") == "name_taken",
          "%s %s" % (r.status, r.json))
    r = c.post("/api/accounts", {"name": "Pat", "pin": "12"})
    check("a malformed PIN is refused at registration", r.status == 400,
          str(r.status))
    r = c.post("/api/accounts", {"name": "Pat", "pin": "4821"})
    check("a regular registers with a PIN",
          r.status == 200 and (r.json or {}).get("pin") is True, str(r.json))
    r = c.post("/api/accounts", {"name": "   "})
    check("a blank name is refused", r.status == 400, str(r.status))
    listed = dict((a["name"], a["pin"])
                  for a in c.get("/api/accounts").json["accounts"])
    check("accounts are listed with their PIN state",
          listed.get("Dana") is False and listed.get("Pat") is True, str(listed))

    # --- signing in -------------------------------------------------------
    r = c.post("/api/singers/login", {"name": "Dana"})
    check("a regular with no PIN signs straight in", r.status == 200,
          "%s %s" % (r.status, r.json))
    check("and joins the rotation", "Dana" in c.get("/api/state").json["singers"])
    r = c.post("/api/singers/login", {"name": "Pat"})
    check("a PIN account refuses sign-in without it",
          r.status == 401 and (r.json or {}).get("code") == "pin",
          "%s %s" % (r.status, r.json))
    r = c.post("/api/singers/login", {"name": "Pat", "pin": "0000"})
    check("and refuses the wrong PIN", r.status == 401, str(r.status))
    r = c.post("/api/singers/login", {"name": "Pat", "pin": "4821"})
    check("and accepts the right one", r.status == 200, str(r.status))
    r = c.post("/api/singers/login", {"name": "Nobody Registered"})
    check("signing in as an unregistered name: 404", r.status == 404,
          str(r.status))

    # --- #27: a PIN-protected name cannot be taken by the guest route ------
    c.post("/api/accounts", {"name": "Quinn", "pin": "1357"})   # absent tonight
    r = c.post("/api/singers", {"name": "quinn"})
    check("a walk-up cannot take a PIN-protected name (#27)",
          r.status == 409 and (r.json or {}).get("code") == "pin",
          "%s %s" % (r.status, r.json))

    # --- #25 and #26: a walk-up borrowed a registered name -----------------
    c.post("/api/accounts", {"name": "Rory"})                    # absent, no PIN
    c.post("/api/singers", {"name": "Rory"})                     # a walk-up types it
    c.post("/api/queue", {"song_id": ids["Valerie"], "singer": "Rory"})
    lid = c.post("/api/lists", {"singer": "Rory",
                                "name": "Rory Guest Picks"}).json["id"]
    r = c.post("/api/singers/login", {"name": "Rory"})          # the owner arrives
    check("the owner arriving stands the walk-up aside as Rory-G (#25)",
          r.status == 200 and (r.json or {}).get("guest_renamed") == "Rory-G",
          "%s %s" % (r.status, r.json))
    st = c.get("/api/state").json
    check("both are in the rotation",
          "Rory" in st["singers"] and "Rory-G" in st["singers"], str(st["singers"]))
    check("and the walk-up keeps their queued song",
          [e["singer"] for e in st["queue"] if e["song_id"] == ids["Valerie"]]
          == ["Rory-G"], str([(e["singer"], e["title"]) for e in st["queue"]]))
    mine = [l["name"] for l in c.get("/api/lists?singer=Rory").json["lists"]]
    theirs = [l["name"] for l in c.get("/api/lists?singer=Rory-G").json["lists"]]
    check("the walk-up's list goes with them, not to the account (#26)",
          "Rory Guest Picks" in theirs and "Rory Guest Picks" not in mine,
          "account=%s walk-up=%s" % (mine, theirs))
    r = c.post("/api/lists/%s/rename" % lid, {"singer": "Rory", "name": "Taken"})
    check("and the account holder cannot edit it", r.status == 403, str(r.status))

    # --- claiming lists made before registering ---------------------------
    c.post("/api/singers", {"name": "Sky"})                      # a walk-up
    c.post("/api/lists", {"singer": "Sky", "name": "Sky Picks"})
    r = c.post("/api/accounts", {"name": "Sky"})                 # then registers
    check("registering a used name offers its lists instead of taking them",
          r.status == 200 and (r.json or {}).get("claimable") == 1, str(r.json))

    def sky_list():
        rows = c.get("/api/lists?singer=Sky").json["lists"]
        return next((l for l in rows if l["name"] == "Sky Picks"), {})

    check("the list stays a guest list until it is claimed",
          sky_list().get("registered") is False, str(sky_list()))
    r = c.post("/api/accounts/claim", {"name": "Sky"})
    check("claiming binds it to the account",
          r.status == 200 and (r.json or {}).get("claimed") == 1, str(r.json))
    check("and it reads back as an account list",
          sky_list().get("registered") is True, str(sky_list()))
    r = c.post("/api/accounts/claim", {"name": "Nobody Registered"})
    check("claiming for an unregistered name: 404", r.status == 404, str(r.status))

    # --- a singer managing their own PIN -----------------------------------
    r = c.post("/api/singers/pin", {"name": "Dana", "new_pin": "1234"})
    check("a regular can add a first PIN",
          r.status == 200 and (r.json or {}).get("pin") is True, str(r.json))
    r = c.post("/api/singers/pin", {"name": "Dana", "new_pin": "5678"})
    check("changing it needs the current PIN", r.status == 401, str(r.status))
    r = c.post("/api/singers/pin", {"name": "Dana", "pin": "9999", "new_pin": "5678"})
    check("and refuses a wrong current PIN", r.status == 401, str(r.status))
    r = c.post("/api/singers/pin", {"name": "Dana", "pin": "1234", "new_pin": "12"})
    check("a malformed new PIN is refused", r.status == 400, str(r.status))
    r = c.post("/api/singers/pin", {"name": "Dana", "pin": "1234", "new_pin": "5678"})
    check("the right current PIN changes it",
          r.status == 200 and (r.json or {}).get("pin") is True, str(r.json))
    r = c.post("/api/singers/login", {"name": "Dana", "pin": "1234"})
    check("after which the old PIN stops working", r.status == 401, str(r.status))
    r = c.post("/api/singers/pin", {"name": "Dana", "pin": "5678", "new_pin": ""})
    check("removing it returns the account to no-auth",
          r.status == 200 and (r.json or {}).get("pin") is False, str(r.json))
    r = c.post("/api/singers/pin", {"name": "Nobody Registered", "new_pin": "1234"})
    check("a PIN for an unregistered name: 404", r.status == 404, str(r.status))

    # --- the KJ's account page ----------------------------------------------
    anon = Client(c.base)
    r = anon.get("/api/kj/accounts")
    check("the KJ accounts view refuses without a session", r.status == 401,
          str(r.status))
    kj = c.get("/api/kj/accounts").json
    rows = dict((a["name"], a) for a in kj["accounts"])
    check("the KJ sees who is here tonight",
          rows["Dana"]["here"] is True and rows["Quinn"]["here"] is False,
          str(rows))
    check("and who has a PIN",
          rows["Quinn"]["pin"] is True and rows["Dana"]["pin"] is False, str(rows))
    check("and which walk-ups are in the room",
          "Rory-G" in kj["guests"] and "Rory" not in kj["guests"], str(kj["guests"]))
    r = anon.post("/api/kj/account/register", {"name": "Tess"})
    check("KJ account changes refuse without a session", r.status == 401,
          str(r.status))
    r = c.post("/api/kj/account/register", {"name": "Tess"})
    check("the KJ can register a regular",
          r.status == 200 and (r.json or {}).get("name") == "Tess", str(r.json))
    r = c.post("/api/kj/account/register", {"name": "tess"})
    check("but not the same name twice", r.status == 409, str(r.status))
    r = c.post("/api/kj/account/rename", {"name": "Tess", "new_name": "Tessa"})
    check("the KJ can fix a regular's spelling",
          r.status == 200 and (r.json or {}).get("name") == "Tessa", str(r.json))
    names = [a["name"] for a in c.get("/api/accounts").json["accounts"]]
    check("and the account carries the new spelling",
          "Tessa" in names and "Tess" not in names, str(names))
    r = c.post("/api/kj/account/rename", {"name": "Tessa", "new_name": "Dana"})
    check("renaming onto a taken name is refused", r.status == 409, str(r.status))
    r = c.post("/api/kj/account/rename", {"name": "Tessa"})
    check("renaming needs a new name", r.status == 400, str(r.status))
    r = c.post("/api/kj/account/clear_pin", {"name": "Quinn"})
    check("the KJ can clear a forgotten PIN",
          r.status == 200 and (r.json or {}).get("ok") is True, str(r.json))
    r = c.post("/api/singers/login", {"name": "Quinn"})
    check("after which that regular signs in freely", r.status == 200,
          str(r.status))
    r = c.post("/api/kj/account/remove", {"name": "Dana"})
    check("the KJ can drop an account",
          r.status == 200 and (r.json or {}).get("ok") is True, str(r.json))
    kj = c.get("/api/kj/accounts").json
    check("a dropped regular who is here becomes a walk-up again",
          "Dana" not in [a["name"] for a in kj["accounts"]]
          and "Dana" in kj["guests"], str(kj["guests"]))
    r = c.post("/api/kj/account/promote", {"name": "Dana"})
    check("an unknown account action is refused", r.status == 400, str(r.status))
    r = c.post("/api/kj/account/register", {})
    check("an account action needs a name", r.status == 400, str(r.status))
    settle(c)


def check_lists(c, ids):
    hold_board(c)             # loading a list queues songs: keep them still
    owner, isl = "Uma", ids["Islands In The Stream"]
    c.post("/api/singers", {"name": owner})
    r = c.post("/api/lists", {"singer": owner})
    check("a list needs a name", r.status == 400, str(r.status))
    r = c.post("/api/lists", {"singer": owner, "name": "Uma Duets"})
    lid = (r.json or {}).get("id")
    check("a singer creates a list", r.status == 200 and bool(lid), str(r.json))
    r = c.post("/api/lists/%s/add" % lid,
               {"singer": owner, "song_id": isl, "version": 1})
    check("a track is added with its own version",
          (r.json or {}).get("ok") is True, str(r.json))
    for title in ("Jolene", "Valerie"):
        c.post("/api/lists/%s/add" % lid, {"singer": owner, "song_id": ids[title]})
    r = c.post("/api/lists/%s/add" % lid, {"singer": owner, "song_id": "nope"})
    check("an unknown song cannot be added", r.status == 400, str(r.status))

    def tracks():
        rows = c.get("/api/lists?singer=%s" % owner).json["lists"]
        return next((l["tracks"] for l in rows if l["id"] == lid), [])

    t = tracks()
    check("the list reads back in order, version and all",
          [x["title"] for x in t] == ["Islands In The Stream", "Jolene", "Valerie"]
          and t[0].get("version") == 1, str(t))
    check("a multi-version track lists its choices",
          len(t[0].get("versions", [])) == 3, str(t[0]))
    r = c.post("/api/lists/%s/set_version" % lid,
               {"singer": owner, "index": 0, "version": 2})
    check("a track's version can be changed",
          (r.json or {}).get("ok") is True and tracks()[0].get("version") == 2,
          str(tracks()[0]))
    c.post("/api/lists/%s/set_version" % lid, {"singer": owner, "index": 0})
    check("and cleared back to the KJ's pick", "version" not in tracks()[0],
          str(tracks()[0]))
    r = c.post("/api/lists/%s/remove" % lid, {"singer": owner, "index": 2})
    check("a track is removed by position",
          (r.json or {}).get("ok") is True and len(tracks()) == 2, str(tracks()))
    r = c.post("/api/lists/%s/remove" % lid, {"singer": owner, "index": "last"})
    check("a non-numeric position is refused", r.status == 400, str(r.status))
    r = c.post("/api/lists/%s/rename" % lid, {"singer": owner, "name": "Uma Best"})
    check("the owner can rename it", (r.json or {}).get("ok") is True, str(r.json))
    r = c.post("/api/lists/%s/rename" % lid,
               {"singer": "Somebody Else", "name": "Mine Now"})
    check("nobody else can", r.status == 403, str(r.status))
    r = c.post("/api/lists/%s/delete" % lid, {"singer": "Somebody Else"})
    check("or delete it", r.status == 403, str(r.status))

    # any singer may load anyone's list into their OWN queue
    r = c.post("/api/lists/%s/queue" % lid, {"singer": "Vic"})
    check("any singer can load a list into their own queue",
          r.status == 200 and (r.json or {}).get("queued") == 2, str(r.json))
    mine = [e for e in c.get("/api/state").json["queue"] if e["singer"] == "Vic"]
    check("and the tracks queue under that singer", len(mine) == 2, str(mine))
    r = c.post("/api/lists/%s/queue" % lid, {})
    check("loading needs a singer", r.status == 400, str(r.status))
    for path, want, why in (("/api/lists/nope/add", 404, "unknown list"),
                            ("/api/lists/%s" % lid, 404, "malformed path"),
                            ("/api/lists/%s/frobnicate" % lid, 400,
                             "unknown action")):
        r = c.post(path, {"singer": owner})
        check("list call refused: %s" % why, r.status == want, str(r.status))

    # --- the KJ's moderation page ------------------------------------------
    anon = Client(c.base)
    r = anon.get("/api/kj/lists")
    check("the moderation view refuses without a session", r.status == 401,
          str(r.status))

    def kj_row():
        rows = c.get("/api/kj/lists").json["lists"]
        return next((l for l in rows if l["id"] == lid), None)

    row = kj_row() or {}
    check("the KJ sees the list, and its owner is here so it is not orphaned",
          row.get("owner_name") == owner and row.get("orphan") is False, str(row))
    c.post("/api/kj/singer_remove", {"name": owner})
    row = kj_row() or {}
    check("a guest list whose owner went home is flagged orphaned",
          row.get("orphan") is True, str(row))

    r = c.post("/api/kj/list/default", {"list_id": lid})
    check("the KJ can star a list as the random pool",
          (r.json or {}).get("default_random") == lid, str(r.json))
    check("and the songbook is told a pool exists",
          c.get("/api/state").json.get("kj_random") is True)
    r = c.post("/api/queue/random_kj", {"singer": "Wren"})
    check("KJ pick queues a song from the pool",
          r.status == 200 and (r.json or {}).get("song_id") in (isl, ids["Jolene"]),
          "%s %s" % (r.status, r.json))
    r = c.post("/api/queue/random_kj", {})
    check("KJ pick needs a singer", r.status == 400, str(r.status))
    r = c.post("/api/kj/list/default", {"list_id": None})
    check("the pool can be cleared",
          "default_random" in (r.json or {}) and r.json["default_random"] is None,
          str(r.json))
    check("and the songbook is told",
          c.get("/api/state").json.get("kj_random") is False)
    r = c.post("/api/queue/random_kj", {"singer": "Wren"})
    check("KJ pick with no pool is refused", r.status == 400, str(r.status))

    r = c.post("/api/kj/list/%s/rename" % lid, {"name": "House Duets"})
    check("the KJ can rename anyone's list", (r.json or {}).get("ok") is True,
          str(r.json))
    for path, want, why in (("/api/kj/list/%s/rename" % lid, 400, "rename needs a name"),
                            ("/api/kj/list/%s/frobnicate" % lid, 400, "unknown action"),
                            ("/api/kj/list/%s" % lid, 404, "malformed path")):
        r = c.post(path)
        check("KJ list call refused: %s" % why, r.status == want, str(r.status))
    r = c.post("/api/kj/list/%s/delete" % lid)
    check("the KJ can delete anyone's list",
          (r.json or {}).get("ok") is True and kj_row() is None, str(r.json))
    settle(c)


def check_random(c, ids):
    hold_board(c)
    r = c.post("/api/queue/random", {"singer": "Xan"})
    j = r.json or {}
    check("Random song queues a real song",
          r.status == 200 and j.get("song_id") in ids.values()
          and bool(j.get("title")), str(j))
    queued = [(e["singer"], e["song_id"]) for e in c.get("/api/state").json["queue"]]
    check("under that singer", queued == [("Xan", j.get("song_id"))], str(queued))
    r = c.post("/api/queue/random", {})
    check("Random song needs a singer", r.status == 400, str(r.status))
    settle(c)


def check_kj_board(c, ids, ws):
    hold_board(c)
    anon = Client(c.base)
    for who, title in (("Ann", "Jolene"), ("Bob", "Africa"), ("Ann", "Valerie"),
                       ("Cal", "Lean On Me")):
        c.post("/api/queue", {"song_id": ids[title], "singer": who})
    wait_for(c, lambda s: s["held"] is True, what="Pause to hold the board")

    def order():
        return [(e["singer"], e["title"])
                for e in c.get("/api/state").json["upcoming"]]

    def entry(who, title):
        for e in c.get("/api/state").json["queue"]:
            if e["singer"] == who and e["title"] == title:
                return e["id"]
        return None

    check("the board starts in plain rotation order",
          order() == [("Ann", "Jolene"), ("Bob", "Africa"), ("Cal", "Lean On Me"),
                      ("Ann", "Valerie")], str(order()))

    # The up-next hand-off comes FIRST, on a board with no manual order: the
    # KJ's nudges outrank the pin by design, so after a nudge a pinned entry
    # is correctly not "next", and this check would fail against good code.
    bob = entry("Bob", "Africa")
    r = anon.post("/api/kj/pin", {"entry_id": bob})
    check("handing out the up-next slot refused without a session",
          r.status == 401, str(r.status))
    r = c.post("/api/kj/pin", {"entry_id": bob})
    st = c.get("/api/state").json
    check("the KJ can hand the up-next slot to any entry",
          (r.json or {}).get("ok") is True and st["next"]["id"] == bob
          and st["pinned"] == bob, "%s next=%s" % (r.json, st["next"]))
    r = c.post("/api/kj/pin", {"entry_id": 99999})
    check("pinning an unknown entry does nothing", (r.json or {}).get("ok") is False,
          str(r.json))
    r = c.post("/api/kj/pin", {"entry_id": "next"})
    check("pinning a non-numeric entry is refused", r.status == 400, str(r.status))

    r = c.post("/api/kj/entry_move", {"entry_id": entry("Ann", "Valerie"), "dir": -1})
    check("a singer's own songs can be reordered",
          (r.json or {}).get("ok") is True and order()[0] == ("Ann", "Valerie"),
          str(order()))
    r = c.post("/api/kj/entry_move", {"entry_id": "x", "dir": -1})
    check("reordering a non-numeric entry is refused", r.status == 400, str(r.status))
    r = c.post("/api/kj/entry_move", {"entry_id": 99999, "dir": -1})
    check("reordering an unknown entry does nothing",
          (r.json or {}).get("ok") is False, str(r.json))

    r = c.post("/api/kj/queue_move", {"entry_id": entry("Cal", "Lean On Me"), "dir": -1})
    o = order()
    check("the play order can be nudged, so Cal now comes before Bob",
          (r.json or {}).get("ok") is True
          and o.index(("Cal", "Lean On Me")) < o.index(("Bob", "Africa")), str(o))
    r = c.post("/api/kj/queue_move", {"entry_id": entry("Cal", "Lean On Me"),
                                      "dir": "up"})
    check("a non-numeric nudge is refused", r.status == 400, str(r.status))

    r = c.post("/api/kj/singer_move", {"name": "Cal", "dir": -1})
    check("singers can be reordered",
          (r.json or {}).get("ok") is True
          and c.get("/api/state").json["singers"] == ["Ann", "Cal", "Bob"],
          str(c.get("/api/state").json["singers"]))
    r = c.post("/api/kj/singer_move", {"name": "Nobody", "dir": -1})
    check("moving an unknown singer does nothing", (r.json or {}).get("ok") is False,
          str(r.json))
    r = c.post("/api/kj/singer_move", {"name": "Cal", "dir": "up"})
    check("a non-numeric singer move is refused", r.status == 400, str(r.status))

    r = c.post("/api/kj/singer_remove", {"name": "Cal"})
    st = c.get("/api/state").json
    check("the KJ can remove a singer along with their songs",
          r.status == 200 and "Cal" not in st["singers"]
          and not any(e["singer"] == "Cal" for e in st["queue"]), str(st["singers"]))
    r = c("DELETE", "/api/queue/%d" % entry("Ann", "Jolene"))
    check("a queued song can be removed",
          r.status == 200 and entry("Ann", "Jolene") is None, str(r.status))
    r = c("DELETE", "/api/queue/not-a-number")
    check("removing a malformed entry id: 404", r.status == 404, str(r.status))

    # lyrics calibration for Bluetooth latency
    r = anon.post("/api/kj/offset", {"delta": 100})
    check("the lyrics offset refuses without a session", r.status == 401,
          str(r.status))
    r = c.post("/api/kj/offset", {"delta": 150})
    check("lyrics can be nudged later",
          (r.json or {}).get("lyrics_offset_ms") == 150, str(r.json))
    r = c.post("/api/kj/offset", {"delta": 99999})
    check("and are clamped to two seconds",
          (r.json or {}).get("lyrics_offset_ms") == 2000, str(r.json))
    check("the calibration is live on /api/config",
          c.get("/api/config").json["lyrics_offset_ms"] == 2000)
    saved = json.loads((ws["tmp"] / "config.json").read_text(encoding="utf-8"))
    check("and saved, so it survives a restart",
          saved.get("lyrics_offset_ms") == 2000, str(saved.get("lyrics_offset_ms")))
    r = c.post("/api/kj/offset", {"delta": "a bit"})
    check("a non-numeric nudge is refused", r.status == 400, str(r.status))
    c.post("/api/kj/offset", {"delta": -2000})      # back to zero
    settle(c)


def check_kj_transport(c, ids):
    anon = Client(c.base)
    r = anon.post("/api/kj/play")
    check("transport refused without a session", r.status == 401, str(r.status))
    r = c.post("/api/kj/moonwalk")
    check("an unknown KJ command is refused", r.status == 400, str(r.status))

    hold_board(c)
    for who, title in (("Ann", "Jolene"), ("Bob", "Africa"), ("Ann", "Valerie")):
        c.post("/api/queue", {"song_id": ids[title], "singer": who})
    # With songs queued, Pause is the only thing that can hold the board, so
    # a hold here proves the command reached the scheduler.
    st = wait_for(c, lambda s: s["held"] is True, what="Pause to hold the board")
    check("Pause holds the board in intermission",
          st["phase"] == "intermission" and st["transport"]["cmd"] == "pause",
          "%s %s" % (st["phase"], st["transport"]))
    r = c.post("/api/kj/start_now")
    st = wait_for(c, lambda s: s["phase"] == "playing", what="Start now")
    check("Start now overrides the hold",
          r.status == 200 and st["now"]["singer"] == "Ann"
          and st["now"]["title"] == "Jolene", str(st["now"]))
    seq = st["transport"]["seq"]
    c.post("/api/kj/restart")
    st = c.get("/api/state").json
    check("restart replays the same song from the top",
          st["now"]["title"] == "Jolene"
          and st["transport"] == {"cmd": "restart", "seq": seq + 1},
          "%s %s" % (st["now"]["title"], st["transport"]))
    c.post("/api/kj/skip_singer")
    st = c.get("/api/state").json
    check("singer's-next swaps to their next song",
          st["now"]["singer"] == "Ann" and st["now"]["title"] == "Valerie",
          str(st["now"]))
    check("and leaves the rotation alone", st["next"]["singer"] == "Bob",
          str(st["next"]))
    c.post("/api/kj/pause")
    check("Pause reaches the screen",
          c.get("/api/state").json["transport"]["cmd"] == "pause")
    c.post("/api/kj/play")
    check("and so does Play", c.get("/api/state").json["transport"]["cmd"] == "play")
    settle(c)


def check_events(c, ws):
    settle(c)
    stream = EventStream(ws["port"])
    try:
        ctype = stream.resp.getheader("Content-Type")
        check("the event stream opens as text/event-stream",
              stream.resp.status == 200 and ctype == "text/event-stream",
              "%s %s" % (stream.resp.status, ctype))
        first = stream.next_event()
        check("it sends the current state the moment it connects",
              first.get("phase") == "idle" and first.get("singers") == [],
              "%s %s" % (first.get("phase"), first.get("singers")))
        c.post("/api/singers", {"name": "Eve"})
        # Look for the change rather than insisting it is the very next event,
        # in case an unrelated broadcast lands first. Every read is bounded by
        # the socket timeout, so a push that never comes fails, never hangs.
        pushed = None
        for _ in range(5):
            ev = stream.next_event()
            if "Eve" in ev.get("singers", []):
                pushed = ev
                break
        check("a change is pushed to it without polling", pushed is not None)
    finally:
        stream.close()


def check_logout(c):
    r = c.post("/api/kj/logout")
    check("logout is accepted", r.status == 200, str(r.status))
    r = c.post("/api/kj/skip")
    check("the operator session is dead after logout", r.status == 401,
          str(r.status))
    check("and the console shows the lock again", "KJ access" in c.get("/kj").text)


# --------------------------------------------------------------------------
# Runner

def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=0,
                    help="port to bind (default: a free one)")
    ap.add_argument("--keep", action="store_true",
                    help="keep the temp workspace for poking at")
    args = ap.parse_args()

    port = args.port or free_port()
    tmp = Path(tempfile.mkdtemp(prefix="kdj-smoke-"))
    cfg_path, expected, ws = build_workspace(tmp, port)
    base = "http://127.0.0.1:%d" % port
    print("[smoke] workspace %s" % tmp)
    print("[smoke] serving %s" % base)

    proc = subprocess.Popen(
        [sys.executable, str(tmp / "kriticaldj.py"), "--config", str(cfg_path)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        c = Client(base)
        deadline = time.time() + 20
        while True:
            if proc.poll() is not None:
                print(proc.stdout.read())
                print("[smoke] server exited before it answered")
                return 1
            try:
                c.get("/api/state")
                break
            except (urllib.error.URLError, http.client.HTTPException, OSError):
                if time.time() > deadline:
                    print("[smoke] server never came up")
                    return 1
                time.sleep(0.2)
        try:
            run_checks(c, expected, ws)
        except Fatal as exc:
            FAILURES.append(("fatal", str(exc)))
            print("  FATAL " + str(exc))
        except Exception:
            # A response that changed shape raises here (a KeyError on a field
            # that moved, say). Record it rather than letting it escape: an
            # escaping traceback would skip the summary AND the server output
            # below, which is where the explanation usually is.
            print(traceback.format_exc())
            FAILURES.append(("harness crashed",
                             traceback.format_exc().strip().splitlines()[-1]))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        # Drain the server now that it is gone. Handler tracebacks land on its
        # stderr, folded into this pipe, and are usually the real explanation
        # for a failed check, so surface them instead of dropping them.
        server_out = proc.stdout.read() or ""
        if FAILURES and server_out.strip():
            print()
            print("[smoke] server output:")
            for line in server_out.strip().splitlines():
                print("  | " + line)
        if args.keep:
            print("[smoke] workspace kept at %s" % tmp)
        else:
            shutil.rmtree(tmp, ignore_errors=True)

    print()
    if FAILURES:
        print("%d of %d checks FAILED:" % (len(FAILURES), len(FAILURES) + PASSED))
        for label, detail in FAILURES:
            print("  " + label + ("\n    " + detail if detail else ""))
        return 1
    print("%d checks passed" % PASSED)
    return 0


if __name__ == "__main__":
    sys.exit(main())
