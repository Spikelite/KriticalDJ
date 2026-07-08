# KriticalDJ — Build Plan

A lean LAN karaoke player. This file is the durable roadmap: development is
deliberately stop-and-start (token budget), so every phase ends at a working,
tested checkpoint and this file records exactly where we are.

**Author:** Spike Graham, with Claude (Anthropic) as co-author.

## Architecture (decided)

- **One Python 3.9+ stdlib-only server** (`kriticaldj.py`). No pip installs on
  the host. Serves three surfaces:
  - `/` — singer UI (BYOD phones/tablets): browse/search library, pick or
    create a singer name, queue songs. Honor system, no auth.
  - `/kj` — KJ console (separate URL space, unlinked from singer UI):
    Play / Pause / Skip / Next, Start-now button, queue management. In-event
    controls only; lifecycle actions live on `/setup` (rescan library, reset
    session, config overview).
  - `/screen` — the TV/projector output, opened fullscreen in a browser on
    the server machine: CDG rendering during songs; between songs an
    intermission board (NOW singing / up NEXT / full rotation queue) plus a
    QR code pointing at the singer UI.
- **State**: `state.json`, atomically rewritten on every mutation — queue,
  singers, rotation cursor, now-playing, phase — so a power failure loses
  nothing. Singer names are per-session data but they live in the journal too
  (surviving a crash mid-party); "new session" = KJ reset (later phase).
- **Flow control lives server-side**: phases `idle -> playing -> intermission
  -> (countdown) -> playing`. The intermission length and the start-now
  countdown are config values. The screen page drives actual audio playback
  and POSTs `/api/screen/ended` when a song finishes; a server thread advances
  phase deadlines and broadcasts.
- **Live updates**: Server-Sent Events at `/events` (stdlib-friendly, one-way
  push is all we need). All three surfaces subscribe.
- **Library**: point `music_root` at any folder tree of karaoke files —
  designed for song-sorter's Final-final output (`letter/artist/stem.ext`),
  but any layout works. Indexes `.mp3`+`.cdg` pairs and `.zip` archives
  containing both. Zips are extracted on demand into `.media-cache/`.
- **Rotation**: classic KJ round-robin. Singers rotate in join order; each
  singer's own queue is FIFO; a singer with nothing queued is skipped but
  stays in rotation. Songs are always tagged with a singer name at queue time.
- **CDG rendering**: vendored open-source MIT decoder (`cdgraphics` npm
  package, browserified into `static/cdgraphics.js`) drawing to a canvas,
  synced to an `<audio>` element. Vendor in Phase 3.
- **QR code**: vendored pure-JS MIT generator rendered client-side on
  `/screen`, encoding the server URL (config `public_url` overrides the
  auto-detected LAN address; LAN has no internet, private hostnames fine).

## Target hardware (decided)

Raspberry Pi 4 running the server. Dual HDMI: an LCD for the KJ (`/kj`) and a
TV for singers (`/screen`, fullscreen Chromium). Audio out over **Bluetooth**
to a speaker that also handles mics and mixing. LAN only, no internet.

Consequences:
- **BT latency (100-300ms) means lyrics would lead the audio.** Config
  `lyrics_offset_ms` shifts CDG rendering relative to `audio.currentTime`;
  the KJ console gets live +/- nudge buttons to calibrate by ear (Phase 3/4).
- Startup scan of a ~54k-file tree on a Pi over USB takes seconds -- fine, but
  the `index.json` sidecar (below) also makes boot near-instant.
- Deployment notes (Phase 5): systemd unit + Chromium kiosk autostart on the
  TV output.

## Library index sidecar (decided)

Filename-derived names carry original-stem noise; song-sorter's cache has the
curated strings. Rather than coupling to the 94MB cache, **song-sorter's
Final-final will also emit a small `index.json` into the output root**
(entries: relative path, artist, title, duration_seconds). KriticalDJ's
scanner uses `music_root/index.json` when present and falls back to the
folder scan otherwise. Small task on each side (song-sorter: extend
`tracks_to_keep`; here: extend `scan_library`).

## Config (`config.json`, created with defaults on first run)

| key | default | meaning |
|---|---|---|
| `music_root` | `""` (required) | folder tree of karaoke files |
| `host` / `port` | `0.0.0.0` / `8080` | bind address |
| `party_name` | `Karaoke Night` | shown on screens |
| `intermission_seconds` | `15` | pause between songs |
| `start_now_countdown_seconds` | `3` | KJ "start now" countdown |
| `public_url` | `""` (auto) | URL encoded in the QR code |

## API sketch

- `GET /api/state` — full state; `GET /events` — SSE push of same
- `GET /api/songs?q=...` — search the index
- `POST /api/singers {"name"}` — join the rotation
- `POST /api/queue {"song_id","singer"}` — queue a song (auto-registers singer)
- `DELETE /api/queue/<entry_id>` — remove an entry
- `POST /api/kj/play|pause|skip|start_now` — transport (KJ)
- `POST /api/kj/restart` — one-shot restart of the current song (via transport)
- `POST /api/kj/skip_singer` — skip now-playing to the SAME singer's next song
- `POST /api/kj/pin {"entry_id"}` — hand the locked up-next slot to any entry
- `GET /api/song_versions?song_id=` — a song's versions + active index
- `POST /api/kj/version {"song_id","index"}` — pick/remember a song's version
- `POST /api/kj/entry_move {"entry_id","dir"}` — reorder within a singer's FIFO
- `POST /api/kj/singer_move {"name","dir"}` / `singer_remove {"name"}` — rotation order
- `POST /api/kj/reset` — clear session; `POST /api/kj/rescan` — reindex library
- `POST /api/setup/config {key: value, ...}` — validated live config edit
- `POST /api/kj/offset {"delta"}` — nudge lyrics_offset_ms (persisted + live)
- `POST /api/screen/ended` — screen reports song finished
- `GET /media/<song_id>/mp3|cdg` — media with HTTP Range support

## Phases

- [x] **Phase 1 — server core** (scaffold, config, scanner, state journal,
      rotation engine, HTTP+SSE API, media serving, unit tests, live smoke
      test). *Done: all endpoints exercised end-to-end with curl.*
- [x] **Phase 2 — singer UI** (`/`): search/browse, name picker, queue +
      my-songs view with live rotation position, SSE-driven banner; sidecar
      `index.json` support in `scan_library` (emitter added in song-sorter).
      *Code complete + API smoke-tested; on-device browser validation deferred
      to the user's next test pass.*
- [x] **Phase 3 — screen** (`/screen`): vendored `cdgraphics` 7.0.0 (ISC)
      and `qrcode-generator` 1.4.4 (MIT) into `static/`; CDG canvas playback
      synced to audio with `lyrics_offset_ms` compensation; audio-unlock gate
      (autoplay policy); intermission board (grab-the-mic NOW / up NEXT /
      rotation list / QR / countdown); idle board; start-now big-number
      countdown; transport (play/pause) applied via SSE seq; POSTs
      /api/screen/ended. *Code complete + endpoints smoke-tested; VISUAL
      validation with real CDG files pending user's test pass.*
- [x] **Phase 4 — KJ console** (`/kj`): transport bar (Play/Pause/Skip/
      Start-now), live lyrics-sync nudge buttons (-50/-10/+10/+50 ms,
      persisted to config + broadcast live to the screen), rotation preview
      with per-entry remove, singer chips with reorder/kick (kick drops their
      queued songs), per-singer queue FIFO reorder, two-step session reset,
      library rescan. *All endpoints smoke-tested end-to-end; browser pass
      pending user.* MVP COMPLETE — phases 1-4 all code-complete.
- [~] **Phase 5 — polish** (OPEN — user review findings land here):
      - [x] singer UI songbook-parity search: All/Artist/Song-title filter
            pills + A–Z artist browse (server: `field`/`letter` params on
            /api/songs with precomputed per-field search text).
      - [x] deployment docs: DEPLOY.md (Pi install, systemd service in
            deploy/kriticaldj.service, dual-screen Chromium kiosk lines,
            Bluetooth pairing + sync calibration, fstab nofail note).
      - [ ] user's browser/hardware test-pass findings (TBD).
      - [ ] GitHub upload (user does this after review).
      - [ ] optional niceties only if wanted: volume duck on pause,
            next-singer chime, config UI.
- [x] **Phase 6 — statistics system** (DONE 2026-07-03): records
      what gets picked and what actually gets played, tied to singer identity.
      - **Event log**: append-only `stats.jsonl`, one JSON line per event
        (`ts`, `event`, `singer_id`, `song_id`, artist/title snapshot).
        Events: `queued`, `started`, `completed`, `skipped`, `removed`.
        Append-only = crash-safe and trivially analyzable later; writes are
        fire-and-forget so stats can never block the party flow.
      - **Singer identity**: today singers are bare per-session names. Add a
        persistent `singers.json` registry (name -> {id, first_seen,
        last_seen}); names are enforced unique, and a returning name
        reattaches to its existing id (honor system, same as everything
        else). Session reset clears the *rotation*, never the registry.
        This keeps the door open for a more robust ID system later without
        rewriting history — stats rows reference the id, not the name.
      - **Future queries this enables**: most-picked / most-played songs,
        skip rate per song, singer histories ("your favorites"), per-party
        summaries, "play it again" shortcuts in the singer UI.
      - *Implemented:* SingerRegistry (singers.json, case-insensitive unique
        names, ids reattach across parties, resets never touch it) + Stats
        (stats.jsonl append-only, events queued/started/completed/skipped/
        removed/session_reset, fire-and-forget) wired through Flow and all
        queue/kick/reset endpoints; singer names canonicalize through the
        registry everywhere; GET /api/stats/summary aggregates (events,
        sessions, top played/queued/singers) and /setup displays it.

- [~] **Phase 7 — UAT-feedback pass** (started 2026-07-04, chunked for the
      token budget; each chunk ends compilable + tested):
      - [x] **C1 singer UI**: name picker is a dropdown (placeholder default so
            nobody queues as someone else, remembered in localStorage, inline
            "add a new name" form); custom search clear-✕ (iOS Safari never
            shows the native one; suppressed native + own button on both).
      - [x] **C2 KJ transport**: `restart` (start current song over, one-shot
            via transport seq; screen seeks 0) + `skip_singer` (skip the
            now-playing song but promote the SAME singer's next queued entry;
            cursor untouched so rotation is unaffected; falls back to plain
            skip when they have nothing else).
      - [x] **C3 locked up-next**: `state.pinned` (journaled entry id) — first
            projection locks the next slot; passive queue adds can never
            displace it (rotation_preview fronts the pin, `_begin_next`
            consumes + re-pins). KJ overrides: reorders clear the pin,
            `/api/kj/pin` hands the slot to any entry ("Next" button per
            rotation row, 🔒 on the locked one). Reconciled centrally in
            `State.mutate` so no path leaves it dangling.
      - [x] **C4 screen timing**: up-next call-out banner fades in over the
            song's last 15 s (screen owns the audio clock, so it's computed
            client-side from `duration - currentTime`; content set in apply(),
            visibility toggled by the 200 ms ticker; hidden when nobody is
            next); `intermission_seconds` default 30 -> 15 (existing
            config.json values override -- update deployed configs by hand
            until C5's GUI); corner elapsed/total timer in the play header.
            Live-verified in a driven browser incl. a real mid-song
            /api/kj/restart seek; pixel layout -> user's hardware pass.
      - [x] **C5 config GUI**: /setup's config table is an editable form ->
            `POST /api/setup/config` (whitelist + type/range validation in
            module-level `validate_config_changes`, unit-tested). Applies
            live + persists to config.json; a changed music_root rescans
            automatically and is refused if the new tree has no songs;
            host/port are saved but reported `restart_needed`. GET
            /api/config now also returns music_root/host/port/raw
            public_url.
      - [x] **C6 Bluetooth**: DEPLOY.md gained a ranked "if the audio
            stutters or drops out" checklist. Top suspects for the UAT
            dropouts: Pi 4 WiFi/BT single shared antenna (fix: Ethernet +
            disable-wifi overlay, or 5 GHz-only) and USB 3 ports/enclosures
            radiating across 2.4 GHz (fix: library drive on the black USB 2
            ports). Then: placement above the crowd, pin A2DP + disable
            headset roles (WirePlumber conf), `vcgencmd get_throttled`
            power check, Class 1 dongle escalation, and the wired-aux
            endgame (USB DAC -> mixer aux, lyrics offset back to ~0).
            Doc-only chunk; no app code.
      - [~] **C7 library data cleanup** (lives in song-sorter, not KDJ —
            KDJ reads index.json verbatim). Scanned the synced 164k-track
            cache. Verdicts: (a) **Coulton album** — 32 tracks mislabeled
            `Bz's Homemade`, songs already correct → alias added; (b) **Rocky
            Horror** — `The Rocky Horror Show` ×7 unified to `...Picture
            Show` ×31 → alias added. Both apply via the *Unify artists*
            menu on prod (39 tracks) then re-export. (c) `[SF Karaoke]`/
            `[DMG Karaoke]` brackets — 0 in current cache, already gone; a
            stale index.json on the Pi just needs a re-export. (d)
            `Artist-Song-Artist` — 0 in source data; likely a KDJ display
            artifact, awaiting a concrete example. (e) **catalog-tag leaks**
            — FIXED in song-sorter main.py: `_parse_artist_song` now handles
            compact `CATALOG-TRACK-ARTIST-SONG` stems (no spaces, e.g.
            `DIS61201-13-MARY POPPINS-...`) via `_COMPACT_CATALOG_RE`, and
            `refresh_names` falls back to it for Unknown-artist tracks so a
            repeat *Refresh* fixes the 107/113 already cached (6 malformed
            stragglers left: MW-808/THK/LEG w/ catalog-internal dashes or no
            song). Verified against all 113 real stems + regressions.
      - [~] **C8 multi-version songs (big)** — KriticalDJ consumer side DONE
            + tested + browser-verified: index.json entries may carry an
            optional `versions: [{path,label,duration}]` alongside the best
            copy; scan builds a per-song `versions` list (v0 = best, mirrors
            the old primary keys so single-version libraries are byte-for-byte
            unchanged). `VersionStore` (versions.json) remembers the KJ's
            per-song choice across restarts/rescans (stable id hashes; 0 =
            default, never stored; resets never touch it). `_media_path`
            serves the active version with a per-version extraction cache
            key. `GET /api/song_versions`, `POST /api/kj/version`; snapshot
            song_view carries `nversions`; KJ console shows a `⧉ vN` picker
            on rotation rows with >1 version. **song-sorter emit side DONE**:
            `tracks_to_keep` ranks copies best-first (`_ranked_tracks`),
            exports the top N (configurable `version_limit`, prompted +
            remembered; 1 = old single-version behavior), and lists the
            alternates under each index entry, labelled by the source
            brand-folder. Round-trip verified: emit -> KriticalDJ
            `scan_library` reads every version and media resolves. The feature
            is now live end to end.

- [x] **Basic KJ auth** (post-UAT ask): a 4-digit `kj_pin` (config default
      `0000`, editable from /setup, write-only -- never returned by
      /api/config) gates the operator surfaces. `/kj` + `/setup` serve
      `static/kjlogin.html` until `POST /api/kj/login` mints an in-memory
      session token (HttpOnly cookie, dropped on restart); all `/api/kj/*`
      (except login/logout) and `/api/setup/config` require it. Singer/screen
      surfaces + read-only GETs stay public. `POST /api/kj/logout` + a Lock
      link on both operator pages. Validated: 23 unit tests + full curl auth
      flow + browser lock/unlock/relock.

## Notes for future sessions

- Tests: `python test_core.py` (stdlib, no pytest needed).
- Smoke test: `python kriticaldj.py --config <cfg>` then curl the API.
- The Final-final output tree's artist folders are lowercase (clean names);
  the scanner title-cases them for display. Song titles parse from the file
  stem (`CATALOG - Artist - Title` variants handled heuristically).
- Screen restart/refresh mid-song restarts the current track from 0:00 (the
  screen owns the playback clock). Acceptable v1 behavior.
- Chromium kiosk should launch with `--autoplay-policy=no-user-gesture-required`
  (else the one-tap audio gate handles it).
- song-sorter's songbook stays a static file; KriticalDJ's `/` replaces it at
  parties. (Optionally the songbook generator could later gain a mode that
  links to KriticalDJ, but the built-in UI makes that unnecessary.)
