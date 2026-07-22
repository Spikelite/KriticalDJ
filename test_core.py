"""KriticalDJ core tests -- stdlib only, run with:  python test_core.py"""
import json
import tempfile
import time
import zipfile
from pathlib import Path

from kriticaldj import (Flow, SingerRegistry, State, Stats, VersionStore,
                        locked_count, move_entry, move_singer, parse_title,
                        pick_next, random_song, scan_library,
                        validate_config_changes)

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


def test_singer_registry_reattach():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "singers.json"
        reg = SingerRegistry(p)
        name1, id1 = reg.resolve("Ann")
        name2, id2 = reg.resolve("ann")   # case-insensitive: same singer
        assert (name2, id2) == ("Ann", id1)
        _, id3 = reg.resolve("Bob")
        assert id3 != id1
        reg2 = SingerRegistry(p)          # survives restart
        assert reg2.resolve("ANN")[1] == id1


def test_stats_events_and_skip_vs_complete():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        reg = SingerRegistry(td / "singers.json")
        stats = Stats(td / "stats.jsonl", reg)
        songs = {"x": {"artist": "A", "title": "T", "search": "a t"}}
        st = State(td / "state.json")
        flow = Flow(st, songs, {"intermission_seconds": 1}, stats)
        st.mutate(songs, lambda: (st.singers.append("Ann"),
                                  st.queue.append(E(1, "Ann"))))
        stats.log("queued", "Ann", flow._info("x"))
        flow._begin_next()                # -> started
        flow.skip()                       # -> skipped
        st.mutate(songs, lambda: st.queue.append(E(2, "Ann")))
        flow._begin_next()                # -> started
        flow.song_ended()                 # -> completed
        rows = [json.loads(l) for l in (td / "stats.jsonl").read_text().splitlines()]
        assert [r["event"] for r in rows] == \
            ["queued", "started", "skipped", "started", "completed"]
        assert all(r["singer_id"] == rows[0]["singer_id"] for r in rows)
        assert rows[2]["title"] == "T" and rows[2]["artist"] == "A"


def test_restart_current_bumps_transport_only():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        songs = {"x": {"artist": "A", "title": "T", "search": "a t"}}
        st = State(td / "state.json")
        flow = Flow(st, songs, {"intermission_seconds": 1})
        st.mutate(songs, lambda: (st.singers.append("Ann"),
                                  st.queue.append(E(1, "Ann"))))
        flow._begin_next()
        now_id, seq = st.now["id"], st.transport["seq"]
        flow.restart_current()
        # same song stays on stage; screen gets a one-shot 'restart' via new seq
        assert st.now["id"] == now_id and st.phase == "playing"
        assert st.transport == {"cmd": "restart", "seq": seq + 1}
        # no-op when nothing is playing
        flow.song_ended()
        seq2 = st.transport["seq"]
        flow.restart_current()
        assert st.transport["seq"] == seq2


def test_skip_to_singer_next_keeps_singer_and_rotation():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        reg = SingerRegistry(td / "singers.json")
        stats = Stats(td / "stats.jsonl", reg)
        songs = {"x": {"artist": "A", "title": "T", "search": "a t"}}
        st = State(td / "state.json")
        flow = Flow(st, songs, {"intermission_seconds": 1}, stats)
        # Ann has two songs, Bob one; cursor starts at Ann
        st.mutate(songs, lambda: (st.singers.extend(["Ann", "Bob"]),
                                  st.queue.extend([E(1, "Ann"), E(2, "Bob"),
                                                   E(3, "Ann")])))
        flow._begin_next()                 # Ann's #1 on stage, cursor now at Bob
        assert st.now["id"] == 1 and st.cursor == 1
        flow.skip_to_singer_next()         # broken track -> Ann's next (#3) now
        assert st.now["singer"] == "Ann" and st.now["id"] == 3
        assert st.phase == "playing"
        assert st.cursor == 1              # rotation untouched: Bob is still next
        assert st.transport["cmd"] == "play"
        # Ann's #3 finishes normally; rotation resumes at Bob
        flow.song_ended()
        flow._begin_next()
        assert st.now["singer"] == "Bob" and st.now["id"] == 2
        # stats: started(1), skipped(1), started(3), completed(3), started(2)
        rows = [json.loads(l) for l in (td / "stats.jsonl").read_text().splitlines()]
        assert [r["event"] for r in rows] == \
            ["started", "skipped", "started", "completed", "started"]


def test_skip_to_singer_next_falls_back_when_alone():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        songs = {"x": {"artist": "A", "title": "T", "search": "a t"}}
        st = State(td / "state.json")
        flow = Flow(st, songs, {"intermission_seconds": 5})
        st.mutate(songs, lambda: (st.singers.append("Ann"),
                                  st.queue.append(E(1, "Ann"))))
        flow._begin_next()                 # Ann's only song on stage
        flow.skip_to_singer_next()         # nothing else queued -> plain skip
        assert st.now is None and st.phase == "intermission"


def test_pin_locks_next_against_late_adds():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        songs = {"x": {"artist": "A", "title": "T", "search": "a t"}}
        st = State(td / "state.json")
        flow = Flow(st, songs, {"intermission_seconds": 1})
        # Ann, Bob, Cal in rotation; only Cal has a song queued
        st.mutate(songs, lambda: (st.singers.extend(["Ann", "Bob", "Cal"]),
                                  st.queue.append(E(1, "Cal"))))
        flow._begin_next()                 # Cal on stage; cursor wraps to Ann
        assert st.now["singer"] == "Cal" and st.pinned is None
        st.mutate(songs, lambda: st.queue.append(E(2, "Bob")))
        assert st.pinned == 2              # Bob announced as next -> locked
        assert st.snapshot(songs)["next"]["singer"] == "Bob"
        # Ann queues; without the pin she'd cut ahead (cursor points at her)
        st.mutate(songs, lambda: st.queue.append(E(3, "Ann")))
        snap = st.snapshot(songs)
        assert snap["next"]["singer"] == "Bob"     # slot held
        assert [e["singer"] for e in snap["upcoming"]] == ["Bob", "Ann"]
        # handoff honors the lock, then locks the new next (Ann)
        flow.song_ended()
        flow._begin_next()
        assert st.now["singer"] == "Bob" and st.pinned == 3
        # pin survives a restart (journaled)
        st2 = State(td / "state.json")
        assert st2.pinned == 3


def test_pin_recomputes_when_pinned_entry_removed():
    with tempfile.TemporaryDirectory() as td:
        songs = {"x": {"artist": "A", "title": "T", "search": "a t"}}
        st = State(Path(td) / "state.json")
        st.mutate(songs, lambda: (st.singers.extend(["Ann", "Bob"]),
                                  st.queue.extend([E(1, "Ann"), E(2, "Bob")])))
        assert st.pinned == 1              # Ann projected first -> locked
        # Ann pulls her song (or KJ deletes it): lock moves to the natural next
        st.mutate(songs, lambda: st.queue.remove(st.queue[0]))
        assert st.pinned == 2
        assert st.snapshot(songs)["next"]["singer"] == "Bob"


def test_pin_untouched_by_skip_to_singer_next():
    with tempfile.TemporaryDirectory() as td:
        songs = {"x": {"artist": "A", "title": "T", "search": "a t"}}
        st = State(Path(td) / "state.json")
        flow = Flow(st, songs, {"intermission_seconds": 1})
        st.mutate(songs, lambda: (st.singers.extend(["Ann", "Bob"]),
                                  st.queue.extend([E(1, "Ann"), E(2, "Bob"),
                                                   E(3, "Ann")])))
        flow._begin_next()                 # Ann's #1 on stage; Bob pinned next
        assert st.pinned == 2
        flow.skip_to_singer_next()         # Ann swaps to her #3; Bob stays next
        assert st.now["id"] == 3 and st.pinned == 2


def test_validate_config_changes():
    cfg = {"party_name": "Karaoke Night", "intermission_seconds": 15,
           "port": 8080, "host": "0.0.0.0", "public_url": "",
           "lyrics_offset_ms": 0}
    # ints parse from strings (HTML inputs) and clamp to sane ranges
    ch, err, rst = validate_config_changes(cfg, {"intermission_seconds": "45"})
    assert ch == {"intermission_seconds": 45} and not err and not rst
    ch, _, _ = validate_config_changes(cfg, {"intermission_seconds": 99999})
    assert ch["intermission_seconds"] == 600
    ch, _, _ = validate_config_changes(cfg, {"lyrics_offset_ms": -9000})
    assert ch["lyrics_offset_ms"] == -2000
    # values equal to the current config are not "changes"
    ch, err, _ = validate_config_changes(cfg, {"party_name": " Karaoke Night "})
    assert ch == {} and not err
    # rejections: unknown key, non-numeric int, empty party name, bogus folder
    _, err, _ = validate_config_changes(cfg, {"hax": 1})
    assert err and "unknown" in err[0]
    _, err, _ = validate_config_changes(cfg, {"port": "abc"})
    assert err and "port" in err[0]
    _, err, _ = validate_config_changes(cfg, {"party_name": "  "})
    assert err
    _, err, _ = validate_config_changes(cfg, {"music_root": "Z:/no/such/dir"})
    assert err and "music_root" in err[0]
    # a real folder passes; host/port flag a restart
    with tempfile.TemporaryDirectory() as td:
        ch, err, rst = validate_config_changes(
            cfg, {"music_root": td, "port": 9000, "host": "127.0.0.1"})
        assert not err and ch["music_root"] == td
        assert sorted(rst) == ["host", "port"]
    # cfg itself is never touched by validation
    assert cfg["port"] == 8080 and cfg["intermission_seconds"] == 15


def test_snapshot_shows_selected_version():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        st = State(td / "s.json")
        st.versions = VersionStore(td / "versions.json")
        songs = {"x": {"artist": "A", "title": "T", "search": "a t",
                       "versions": [{"label": "Best"}, {"label": "Alt"}]},
                 "y": {"artist": "B", "title": "U", "search": "b u",
                       "versions": [{"label": "Best"}]}}
        st.mutate(songs, lambda: (st.singers.extend(["Ann", "Bob"]),
                                  st.queue.extend([E(1, "Ann"),
                                                   dict(E(2, "Bob"), song_id="y")])))
        snap = st.snapshot(songs)
        ann, bob = snap["upcoming"]
        assert ann["vsel"] == 1                 # default pick displays as v1
        assert "vsel" not in bob                # single-version: no picker, no vsel
        st.versions.set("x", 1)
        assert st.snapshot(songs)["upcoming"][0]["vsel"] == 2  # 1-based
        st.versions.set("x", 7)                 # stale/out-of-range -> default
        assert st.snapshot(songs)["upcoming"][0]["vsel"] == 1
        st.versions = None                      # store not attached (tests, etc.)
        assert "vsel" not in st.snapshot(songs)["upcoming"][0]


def test_validate_config_kj_pin():
    cfg = {"kj_pin": "0000"}
    # a blank PIN field means "keep current" -- not a change, not an error
    ch, err, _ = validate_config_changes(cfg, {"kj_pin": ""})
    assert ch == {} and not err
    # a valid new 4-digit PIN is accepted
    ch, err, _ = validate_config_changes(cfg, {"kj_pin": "4821"})
    assert ch == {"kj_pin": "4821"} and not err
    # wrong length / non-digits are rejected
    for bad in ("123", "12345", "12a4"):
        _, err, _ = validate_config_changes(cfg, {"kj_pin": bad})
        assert err and "PIN" in err[0], bad


def test_scan_versions_from_sidecar():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "best.mp3").write_bytes(b"m"); (root / "best.cdg").write_bytes(b"c")
        (root / "alt.zip").write_bytes(b"z")
        (root / "index.json").write_text(json.dumps({"songs": [
            {"path": "best.mp3", "artist": "Queen", "title": "Bo Rhap",
             "duration": 354, "versions": [
                 {"path": "alt.zip", "label": "SC edit", "duration": 300},
                 {"path": "gone.mp3", "label": "missing"},  # skipped, no file
             ]},
        ]}), encoding="utf-8")
        songs = scan_library(str(root))
        s = list(songs.values())[0]
        # primary keys untouched (back-compat) + a two-entry versions list
        assert s["mp3"].endswith("best.mp3") and s["duration"] == 354
        assert [v["label"] for v in s["versions"]] == ["Best", "SC edit"]
        assert "zip" in s["versions"][1] and s["versions"][1]["duration"] == 300


def test_scan_single_version_backcompat():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        d = root / "q"; d.mkdir()
        (d / "Queen - Bohemian Rhapsody.mp3").write_bytes(b"m")
        (d / "Queen - Bohemian Rhapsody.cdg").write_bytes(b"c")
        s = list(scan_library(str(root)).values())[0]
        # folder-scan libraries get exactly one implicit version
        assert len(s["versions"]) == 1 and s["mp3"].endswith(".mp3")


def test_version_store_persist_and_default():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "versions.json"
        vs = VersionStore(p)
        assert vs.get("abc") == 0            # default when unset
        vs.set("abc", 2)
        assert vs.get("abc") == 2
        vs.set("abc", 0)                     # 0 clears the entry (stays lean)
        assert vs.get("abc") == 0 and "abc" not in vs.by_id
        vs.set("xyz", 3)
        assert VersionStore(p).get("xyz") == 3   # survives restart


def _force_deadline_past(st, songs):
    def fn():
        st.deadline = time.time() - 1
    st.mutate(songs, fn)


def test_intermission_holds_on_pause():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        songs = {"x": {"artist": "A", "title": "T", "search": "a t"}}
        st = State(td / "state.json")
        flow = Flow(st, songs, {"intermission_seconds": 5})
        st.mutate(songs, lambda: (st.singers.extend(["Ann", "Bob"]),
                                  st.queue.extend([E(1, "Ann"), E(2, "Bob")])))
        flow._begin_next()                    # Ann on stage
        flow.transport_cmd("pause")
        flow.song_ended()                     # enters intermission while paused
        flow.tick_once()
        assert st.hold_remaining is not None  # frozen on entry
        assert 4.0 <= st.hold_remaining <= 5.0
        assert st.snapshot(songs)["held"] is True
        # a passed deadline must NOT advance while held
        _force_deadline_past(st, songs)
        flow.tick_once()
        assert st.phase == "intermission" and st.now is None
        # unpause resumes from the frozen remaining, then plays on expiry
        flow.transport_cmd("play")
        flow.tick_once()
        assert st.hold_remaining is None
        assert st.deadline > time.time() + 3  # resumed with ~4-5s left
        _force_deadline_past(st, songs)
        flow.tick_once()
        assert st.phase == "playing" and st.now["singer"] == "Bob"


def test_intermission_autoholds_when_queue_empty():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        songs = {"x": {"artist": "A", "title": "T", "search": "a t"}}
        st = State(td / "state.json")
        flow = Flow(st, songs, {"intermission_seconds": 5})
        st.mutate(songs, lambda: (st.singers.append("Ann"),
                                  st.queue.append(E(1, "Ann"))))
        flow._begin_next()                    # queue is now empty
        flow.song_ended()
        flow.tick_once()
        assert st.hold_remaining is not None  # auto-held: nothing queued
        _force_deadline_past(st, songs)
        flow.tick_once()
        assert st.phase == "intermission"     # parked, not idle
        # a queue add releases the hold automatically...
        st.mutate(songs, lambda: st.queue.append(E(2, "Ann")))
        flow.tick_once()
        assert st.hold_remaining is None and st.deadline > time.time()
        _force_deadline_past(st, songs)
        flow.tick_once()                      # ...and the song plays
        assert st.phase == "playing" and st.now["id"] == 2


def test_manual_pause_wins_over_queue_add():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        songs = {"x": {"artist": "A", "title": "T", "search": "a t"}}
        st = State(td / "state.json")
        flow = Flow(st, songs, {"intermission_seconds": 5})
        st.mutate(songs, lambda: (st.singers.append("Ann"),
                                  st.queue.append(E(1, "Ann"))))
        flow._begin_next()
        flow.transport_cmd("pause")
        flow.song_ended()
        flow.tick_once()
        assert st.hold_remaining is not None
        # queueing must NOT resume a manually paused countdown
        st.mutate(songs, lambda: st.queue.append(E(2, "Ann")))
        flow.tick_once()
        assert st.hold_remaining is not None
        _force_deadline_past(st, songs)
        flow.tick_once()
        assert st.phase == "intermission"     # still parked
        flow.transport_cmd("play")            # only Play releases it
        flow.tick_once()
        assert st.hold_remaining is None


def test_start_now_overrides_hold():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        songs = {"x": {"artist": "A", "title": "T", "search": "a t"}}
        st = State(td / "state.json")
        flow = Flow(st, songs, {"intermission_seconds": 5,
                                "start_now_countdown_seconds": 1})
        st.mutate(songs, lambda: (st.singers.append("Ann"),
                                  st.queue.extend([E(1, "Ann"), E(2, "Ann")])))
        flow._begin_next()
        flow.transport_cmd("pause")
        flow.song_ended()
        flow.tick_once()
        assert st.hold_remaining is not None
        flow.start_now()                      # explicit go beats the hold
        assert st.phase == "countdown" and st.hold_remaining is None
        _force_deadline_past(st, songs)
        flow.tick_once()
        assert st.phase == "playing"


FAIR = {"fairness_enabled": True, "lock_percent": 33, "bump_limit": 2}


def test_locked_count():
    assert locked_count(6, 33) == 2     # round(1.98)
    assert locked_count(3, 33) == 1     # round(0.99)
    assert locked_count(2, 33) == 1     # round(0.66) -> 1
    assert locked_count(10, 0) == 1     # floor: always protect the up-next slot
    assert locked_count(5, 100) == 5    # everyone locked
    assert locked_count(0, 33) == 0     # nobody


def test_add_singer_fairness_off_appends():
    with tempfile.TemporaryDirectory() as td:
        st = State(Path(td) / "s.json")
        st.singers = ["A", "B", "C"]
        st.add_singer("D", {"fairness_enabled": False})
        assert st.singers == ["A", "B", "C", "D"]


def test_add_singer_newbie_jumps_veterans_below_lock():
    with tempfile.TemporaryDirectory() as td:
        st = State(Path(td) / "s.json")
        st.singers = ["A", "B", "C", "D", "E", "F"]
        st.performed = ["A", "B", "C", "D", "E", "F"]   # all veterans
        st.add_singer("X", FAIR)                         # newcomer (newbie)
        # lock 33% of 6 = 2 -> A,B protected; X lands right below them
        assert st.singers == ["A", "B", "X", "C", "D", "E", "F"]
        assert st.bumps == {"C": 1, "D": 1, "E": 1, "F": 1}  # each jumped vet


def test_add_singer_newbie_never_bumps_newbie():
    with tempfile.TemporaryDirectory() as td:
        st = State(Path(td) / "s.json")
        st.singers = ["N1", "N2", "V"]      # two waiting newbies, one veteran
        st.performed = ["V"]
        st.add_singer("X", FAIR)            # newbie
        # lock=1 (N1). X goes AFTER waiting newbie N2, ahead of veteran V
        assert st.singers == ["N1", "N2", "X", "V"]
        assert st.bumps == {"V": 1}


def test_add_singer_bump_cap_protects_veteran():
    with tempfile.TemporaryDirectory() as td:
        st = State(Path(td) / "s.json")
        st.singers = ["A", "V"]
        st.performed = ["A", "V"]
        st.bumps = {"V": 2}                 # V already at the cap
        st.add_singer("X", FAIR)           # newbie
        assert st.singers == ["A", "V", "X"]   # V protected -> X lands behind it
        assert st.bumps == {"V": 2}            # no further bump


def test_add_singer_veteran_rejoin_appends():
    with tempfile.TemporaryDirectory() as td:
        st = State(Path(td) / "s.json")
        st.singers = ["A", "B"]
        st.performed = ["A", "B", "V"]      # V sang, then was kicked; now rejoining
        st.add_singer("V", FAIR)
        assert st.singers == ["A", "B", "V"]   # no newbie boost for a veteran
        assert st.bumps == {}


def test_performed_and_bump_reset_on_turn():
    with tempfile.TemporaryDirectory() as td:
        songs = {"x": {"artist": "A", "title": "T", "search": "a t"}}
        st = State(Path(td) / "s.json")
        flow = Flow(st, songs, {"intermission_seconds": 1})
        st.mutate(songs, lambda: (st.singers.append("Ann"),
                                  st.queue.append(E(1, "Ann"))))
        st.mutate(songs, lambda: st.bumps.__setitem__("Ann", 1))  # pretend bumped
        flow._begin_next()                  # Ann takes her turn
        assert "Ann" in st.performed        # veteran this session now
        assert "Ann" not in st.bumps        # counter reset on her turn


def test_validate_config_bool_and_fairness():
    cfg = {"fairness_enabled": True, "lock_percent": 33, "bump_limit": 2}
    ch, err, _ = validate_config_changes(cfg, {"fairness_enabled": False})
    assert ch == {"fairness_enabled": False} and not err
    ch, err, _ = validate_config_changes(cfg, {"fairness_enabled": "on"})
    assert ch == {} and not err         # "on" == True == current -> no change
    ch, _, _ = validate_config_changes(cfg, {"lock_percent": "150"})
    assert ch["lock_percent"] == 100    # clamped to range
    ch, _, _ = validate_config_changes(cfg, {"bump_limit": "-5"})
    assert ch["bump_limit"] == 0


def test_manual_order_nudge_and_stickiness():
    with tempfile.TemporaryDirectory() as td:
        songs = {"x": {"artist": "A", "title": "T", "search": "a t"}}
        st = State(Path(td) / "state.json")
        st.mutate(songs, lambda: (st.singers.extend(["Ann", "Bob", "Cal"]),
                                  st.queue.extend([E(1, "Ann"), E(2, "Bob"), E(3, "Cal")])))
        assert [e["id"] for e in st.rotation_preview()] == [1, 2, 3]  # round-robin
        # nudge Cal's entry (3) up one -> swaps past Bob (2), sticks
        st.mutate(songs, lambda: st.move_in_order(3, -1))
        assert st.manual_order == [1, 3, 2]
        assert [e["id"] for e in st.rotation_preview()] == [1, 3, 2]
        # sticky: a late add lands in the fluid tail, never above the bump
        st.mutate(songs, lambda: (st.singers.append("Dee"), st.queue.append(E(4, "Dee"))))
        assert [e["id"] for e in st.rotation_preview()] == [1, 3, 2, 4]
        # the base rotation is untouched: singer order + per-singer FIFO intact
        assert st.singers == ["Ann", "Bob", "Cal", "Dee"]
        assert [e["id"] for e in st.queue] == [1, 2, 3, 4]


def test_manual_order_bounds_and_unknown():
    with tempfile.TemporaryDirectory() as td:
        songs = {"x": {"artist": "A", "title": "T", "search": "a t"}}
        st = State(Path(td) / "state.json")
        st.mutate(songs, lambda: (st.singers.extend(["Ann", "Bob"]),
                                  st.queue.extend([E(1, "Ann"), E(2, "Bob")])))
        assert st.move_in_order(1, -1) is False   # already first
        assert st.move_in_order(2, 1) is False    # already last
        assert st.move_in_order(99, -1) is False  # unknown entry
        assert st.manual_order == []              # nothing frozen on a no-op


def test_manual_order_reconciles_and_consumes():
    with tempfile.TemporaryDirectory() as td:
        songs = {"x": {"artist": "A", "title": "T", "search": "a t"}}
        st = State(Path(td) / "state.json")
        flow = Flow(st, songs, {"intermission_seconds": 1})
        st.mutate(songs, lambda: (st.singers.extend(["Ann", "Bob", "Cal"]),
                                  st.queue.extend([E(1, "Ann"), E(2, "Bob"), E(3, "Cal")])))
        st.mutate(songs, lambda: st.move_in_order(3, -1))
        assert st.manual_order == [1, 3, 2]
        # a removed entry drops out of the manual order
        st.mutate(songs, lambda: st.queue.remove(next(e for e in st.queue if e["id"] == 2)))
        assert st.manual_order == [1, 3]
        # playing the front entry consumes it out of the manual order too
        flow._begin_next()
        assert st.now["id"] == 1 and st.manual_order == [3]


def test_manual_order_survives_restart():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "state.json"
        songs = {"x": {"artist": "A", "title": "T", "search": "a t"}}
        st = State(p)
        st.mutate(songs, lambda: (st.singers.extend(["Ann", "Bob", "Cal"]),
                                  st.queue.extend([E(1, "Ann"), E(2, "Bob"), E(3, "Cal")])))
        st.mutate(songs, lambda: st.move_in_order(3, -1))
        assert State(p).manual_order == [1, 3, 2]  # journaled


def test_random_song_excludes_and_falls_back():
    songs = {"a": {}, "b": {}, "c": {}}
    # one candidate left -> deterministic
    assert random_song(songs, {"a", "b"}) == "c"
    # excluding everything falls back to the whole library (never dead-ends)
    assert random_song(songs, {"a", "b", "c"}) in songs
    # empty library -> None
    assert random_song({}, set()) is None
    # a normal pick is always a real, non-excluded id
    for _ in range(20):
        assert random_song(songs, {"a"}) in ("b", "c")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"{len(fns)} tests passed")
