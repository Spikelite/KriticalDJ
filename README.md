# KriticalDJ

A lean, offline, LAN-only karaoke player. One Python file, zero dependencies.
Point it at a folder of karaoke tracks (`.mp3`+`.cdg` pairs or `.zip`s) and it
runs your whole night: guests browse and queue songs from their own phones,
a fair singer rotation decides who's up, and the TV shows lyrics during songs
and the rotation board (with a join-in QR code) between them.

Built to pair with [song-sorter](https://github.com/Spikelite/song-sorter)'s
cleaned library output, but any folder tree of CDG karaoke files works.

## Quick start

```text
python kriticaldj.py            # first run writes config.json
# edit config.json: set music_root
python kriticaldj.py
```

Then open, in a browser:

| URL | Who | What |
|---|---|---|
| `/` | Singers (BYOD phones/tablets) | Search the library, pick your name, queue songs |
| `/kj` | The KJ (host machine) | Play / Pause / Skip, Start-now, manage the queue |
| `/screen` | The TV/projector (fullscreen browser) | Lyrics during songs; NOW / NEXT / rotation + QR between songs |

No accounts, no auth — it's a karaoke party, the honor system runs the door.

## How the rotation works

Classic KJ rules: singers rotate in the order they joined; your own songs play
in the order you queued them; if it's your turn but you have nothing queued,
you're skipped (you keep your spot). Songs are always queued under a singer's
name.

## Crash safety

Queue, singers, rotation position, and playback phase are journaled to
`state.json` on every change. After a power failure the party resumes at the
intermission board with the interrupted song back at the front of the line.
Singer names last for the session (KJ can reset for the next party).

## Config

`config.json` (created on first run):

| key | default | meaning |
|---|---|---|
| `music_root` | *(required)* | folder tree of karaoke files |
| `host` / `port` | `0.0.0.0` / `8080` | bind address |
| `party_name` | `Karaoke Night` | shown on all surfaces |
| `intermission_seconds` | `30` | pause between songs |
| `start_now_countdown_seconds` | `3` | countdown after the KJ hits Start now |
| `public_url` | *(auto-detected)* | URL encoded in the on-screen QR code |

## Status

Under active development — see [PLAN.md](PLAN.md) for the phase roadmap.
Phase 1 (server core: library index, rotation engine, crash-safe state,
HTTP + SSE API, media serving) is complete and tested. UI phases are next.

Run the tests: `python test_core.py`

## Credits

By **Spike Graham**, with **Claude** (Anthropic) as co-author.

## License

MIT — see [LICENSE](LICENSE).
