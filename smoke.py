#!/usr/bin/env python3
"""KriticalDJ live smoke test -- stdlib only, run with:  python smoke.py

Stands a REAL server up on a spare port against a throwaway library, drives it
over HTTP the way a phone and the TV do, and asserts on the responses.
test_core.py covers the model underneath; this covers the request-handling
layer above it, which is where the bugs found at parties have actually lived.

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
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
KJ_PIN = "4271"          # deliberately not the 0000 default: proves it is read
INTERMISSION = 1         # seconds; the real default of 15 makes a smoke run crawl
MP3_BYTES = b"ID3" + bytes(range(64))   # 67 bytes, known size for the range checks


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


def build_workspace(tmp: Path, port: int):
    """Copy the app beside a fresh config and library.

    Returns (config path, expected song count).
    """
    shutil.copy2(ROOT / "kriticaldj.py", tmp / "kriticaldj.py")
    shutil.copytree(ROOT / "static", tmp / "static")
    library = tmp / "library"
    library.mkdir()
    expected = build_library(library)
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
    return cfg_path, expected


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


# --------------------------------------------------------------------------
# The party

def run_checks(c, expected_songs):
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
    cfg_path, expected = build_workspace(tmp, port)
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
            run_checks(c, expected)
        except Fatal as exc:
            FAILURES.append(("fatal", str(exc)))
            print("  FATAL " + str(exc))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
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
