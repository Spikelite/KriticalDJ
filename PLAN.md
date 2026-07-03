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
    Play / Pause / Skip / Next, Start-now button, queue management.
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

## Config (`config.json`, created with defaults on first run)

| key | default | meaning |
|---|---|---|
| `music_root` | `""` (required) | folder tree of karaoke files |
| `host` / `port` | `0.0.0.0` / `8080` | bind address |
| `party_name` | `Karaoke Night` | shown on screens |
| `intermission_seconds` | `30` | pause between songs |
| `start_now_countdown_seconds` | `3` | KJ "start now" countdown |
| `public_url` | `""` (auto) | URL encoded in the QR code |

## API sketch

- `GET /api/state` — full state; `GET /events` — SSE push of same
- `GET /api/songs?q=...` — search the index
- `POST /api/singers {"name"}` — join the rotation
- `POST /api/queue {"song_id","singer"}` — queue a song (auto-registers singer)
- `DELETE /api/queue/<entry_id>` — remove an entry
- `POST /api/kj/play|pause|skip|start_now` — transport (KJ)
- `POST /api/screen/ended` — screen reports song finished
- `GET /media/<song_id>/mp3|cdg` — media with HTTP Range support

## Phases

- [x] **Phase 1 — server core** (scaffold, config, scanner, state journal,
      rotation engine, HTTP+SSE API, media serving, unit tests, live smoke
      test). *Done: all endpoints exercised end-to-end with curl.*
- [ ] **Phase 2 — singer UI** (`/`): search/browse (songbook-style UX), singer
      name picker (tap an existing name or add yours), queue + my-songs view,
      live rotation position ("you're 3rd").
- [ ] **Phase 3 — screen** (`/screen`): vendor cdgraphics + QR libs; CDG
      canvas playback synced to audio; intermission board (NOW / NEXT / queue
      + QR + countdown); idle board when queue empty.
- [ ] **Phase 4 — KJ console** (`/kj`): transport buttons, start-now, queue
      reorder/remove, singer management, session reset, rescan library.
- [ ] **Phase 5 — polish**: pitch-free niceties only if wanted (volume duck on
      pause, next-singer audio chime, config UI). GitHub upload.

## Notes for future sessions

- Tests: `python test_core.py` (stdlib, no pytest needed).
- Smoke test: `python kriticaldj.py --config <cfg>` then curl the API.
- The Final-final output tree's artist folders are lowercase (clean names);
  the scanner title-cases them for display. Song titles parse from the file
  stem (`CATALOG - Artist - Title` variants handled heuristically).
- song-sorter's songbook stays a static file; KriticalDJ's `/` replaces it at
  parties. (Optionally the songbook generator could later gain a mode that
  links to KriticalDJ, but the built-in UI makes that unnecessary.)
