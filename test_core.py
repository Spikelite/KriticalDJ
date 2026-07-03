"""KriticalDJ core tests -- stdlib only, run with:  python test_core.py"""
import json
import tempfile
import zipfile
from pathlib import Path

from kriticaldj import (State, move_entry, move_singer, parse_title, pick_next,
                        scan_library)

E = lambda i, s: {"id": i, "singer": s, "song_id": "x"}


def test_rotation_round_robin():
    singers = ["Ann", "Bob", "Cal"]
    q = [E(1, "Bob"), E(2, "Ann"), E(3, "Bob"), E(4, "Cal")]
    order = []
    cursor = 0
    entries = list(q)
    while True:
        e, cursor = pick_next(singers, cursor, entries)
        if e is None:
            break
        entries.remove(e)
        order.append((e["singer"], e["id"]))
    # round-robin from Ann; Bob's own songs stay FIFO
    assert order == [("Ann", 2), ("Bob", 1), ("Cal", 4), ("Bob", 3)], order


def test_rotation_skips_empty_singers():
    singers = ["Ann", "Bob"]
    e, cursor = pick_next(singers, 0, [E(1, "Bob")])
    assert e["singer"] == "Bob" and cursor == 0  # cursor lands after Bob (wraps)


def test_rotation_empty():
    assert pick_next([], 0, []) == (None, 0)
    assert pick_next(["Ann"], 0, []) == (None, 0)


def test_parse_title():
    assert parse_title("SC8121-03 - Beach Boys - Barbara Ann", "Beach Boys") == "Barbara Ann"
    assert parse_title("Beach Boys - Barbara Ann", "Beach Boys") == "Barbara Ann"
    assert parse_title("Barbara Ann - Beach Boys", "Beach Boys") == "Barbara Ann"
    assert parse_title("Barbara Ann", "Beach Boys") == "Barbara Ann"
    assert parse_title("EZH-31 - 04 - Milkshake", "Kelis") == "04 - Milkshake"


def test_scan_pairs_and_zips():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        d = root / "b" / "beach boys"
        d.mkdir(parents=True)
        (d / "SC81 - Beach Boys - Barbara Ann.mp3").write_bytes(b"m" * 10)
        (d / "SC81 - Beach Boys - Barbara Ann.cdg").write_bytes(b"c" * 10)
        (d / "lonely.mp3").write_bytes(b"m")  # no cdg -> skipped
        zp = root / "b" / "beach boys" / "SC82 - Beach Boys - Kokomo.zip"
        with zipfile.ZipFile(zp, "w") as z:
            z.writestr("Kokomo.mp3", b"m" * 10)
            z.writestr("Kokomo.cdg", b"c" * 10)
        # a pair sitting directly in a letter bucket: artist comes from the stem
        (root / "q").mkdir()
        (root / "q" / "Queen - Bohemian Rhapsody.mp3").write_bytes(b"m")
        (root / "q" / "Queen - Bohemian Rhapsody.cdg").write_bytes(b"c")
        songs = scan_library(str(root))
        assert len(songs) == 3, songs
        vals = sorted(songs.values(), key=lambda s: s["title"])
        assert vals[0]["title"] == "Barbara Ann" and vals[0]["artist"] == "Beach Boys"
        assert vals[1]["title"] == "Bohemian Rhapsody" and vals[1]["artist"] == "Queen"
        assert vals[2]["title"] == "Kokomo" and "zip" in vals[2]
        # ids stable across rescans
        assert set(songs) == set(scan_library(str(root)))


def test_move_entry_same_singer_only():
    q = [E(1, "Ann"), E(2, "Bob"), E(3, "Ann"), E(4, "Bob")]
    # moving Ann's #3 up must hop over Bob's #2 and swap with Ann's #1
    assert move_entry(q, 3, -1)
    assert [e["id"] for e in q] == [3, 2, 1, 4]
    # Bob's #4 down: no same-singer sibling below -> no-op
    assert not move_entry(q, 4, 1)
    assert not move_entry(q, 99, -1)  # unknown id


def test_move_singer():
    s = ["Ann", "Bob", "Cal"]
    assert move_singer(s, "Cal", -1) and s == ["Ann", "Cal", "Bob"]
    assert not move_singer(s, "Ann", -1)  # already first
    assert not move_singer(s, "Bob", 1)   # already last
    assert not move_singer(s, "Zoe", 1)   # unknown


def test_scan_prefers_sidecar():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        d = root / "b" / "beach boys"
        d.mkdir(parents=True)
        mp3 = d / "sc81 - beach boys - barbara ann wvocal.mp3"  # noisy stem
        mp3.write_bytes(b"m")
        (d / "sc81 - beach boys - barbara ann wvocal.cdg").write_bytes(b"c")
        (root / "index.json").write_text(json.dumps({"version": 1, "songs": [
            {"path": "b/beach boys/sc81 - beach boys - barbara ann wvocal.mp3",
             "artist": "The Beach Boys", "title": "Barbara Ann", "duration": 132},
            {"path": "b/gone/missing.mp3", "artist": "X", "title": "Y"},  # skipped
        ]}), encoding="utf-8")
        songs = scan_library(str(root))
        assert len(songs) == 1
        s = list(songs.values())[0]
        # curated names win over the noisy filename; duration carried through
        assert s["artist"] == "The Beach Boys" and s["title"] == "Barbara Ann"
        assert s["duration"] == 132 and s["mp3"].endswith(".mp3")
        assert "beach" in s["search"] and "boys" in s["search"]  # token search


def test_state_journal_and_crash_recovery():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "state.json"
        st = State(p)
        st.mutate({}, lambda: (st.singers.append("Ann"),
                               st.queue.append(E(1, "Ann"))))
        # simulate: song on stage, then power failure
        def on_stage():
            st.now = st.queue.pop(0)
            st.phase = "playing"
        st.mutate({}, on_stage)
        st2 = State(p)  # reboot
        assert st2.singers == ["Ann"]
        assert st2.queue and st2.queue[0]["id"] == 1  # song went back in line
        assert st2.now is None and st2.phase == "intermission"


def test_snapshot_shapes():
    with tempfile.TemporaryDirectory() as td:
        st = State(Path(td) / "s.json")
        songs = {"x": {"artist": "A", "title": "T", "search": "at"}}
        st.mutate(songs, lambda: (st.singers.extend(["Ann", "Bob"]),
                                  st.queue.extend([E(1, "Bob"), E(2, "Ann")])))
        snap = st.snapshot(songs)
        assert snap["phase"] == "idle"
        assert snap["next"]["singer"] == "Ann"  # rotation starts at cursor 0
        assert [e["singer"] for e in snap["upcoming"]] == ["Ann", "Bob"]


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"{len(fns)} tests passed")
