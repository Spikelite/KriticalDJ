# Deploying KriticalDJ on a Raspberry Pi

Target: Raspberry Pi 4, Raspberry Pi OS (Bookworm), dual HDMI — TV on HDMI-0
for `/screen`, an LCD on HDMI-1 for `/kj` — Bluetooth speaker for audio,
LAN with no internet. Nothing needs pip; Python 3 ships with the OS.

## 1. Install

```bash
git clone <repo> /home/pi/KriticalDJ    # or copy the folder over
cd /home/pi/KriticalDJ
python3 kriticaldj.py                   # writes config.json, then exits
nano config.json                        # set music_root, party_name, etc.
python3 kriticaldj.py                   # sanity check, then Ctrl-C
```

Point `music_root` at the mounted library (e.g. `/mnt/karaoke-usb/output`).
If the drive is in `/etc/fstab`, keep `nofail` on it so boot never hangs —
the server starts anyway and a **Rescan** from `/setup` picks the library up
once mounted.

## 2. Run as a service

```bash
sudo cp deploy/kriticaldj.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now kriticaldj
journalctl -u kriticaldj -f             # watch the log
```

The service restarts on failure; state (`state.json`) survives restarts and
power cuts by design.

## 3. Screens

Autostart two Chromium windows (Wayland/labwc: `~/.config/labwc/autostart`;
X11/LXDE: `~/.config/lxsession/LXDE-pi/autostart`):

```bash
# TV (kiosk; the autoplay flag removes the one-tap audio unlock)
chromium-browser --kiosk --autoplay-policy=no-user-gesture-required \
  --user-data-dir=/home/pi/.kdj-screen http://localhost:8080/screen &

# KJ LCD (second display; adjust the position for your layout)
chromium-browser --user-data-dir=/home/pi/.kdj-kj --window-position=1920,0 \
  --start-fullscreen http://localhost:8080/kj &
```

Separate `--user-data-dir`s let two Chromium instances run side by side.
Turn off screen blanking: `raspi-config` > Display > Screen Blanking > No.

## 4. Bluetooth audio

Pair the speaker once via `bluetoothctl` (`pair`/`trust`/`connect`) or the
desktop applet and set it as the default output. Then calibrate the lyrics:
play any song and use the **Lyrics sync** nudge buttons on `/kj` until the
highlight matches what you hear (Bluetooth typically wants +100 to +250 ms).
The value persists in `config.json`.

## 5. Phones

Guests join over the LAN via the QR code on the intermission screen. If you
run a private hostname (e.g. `karaoke.lan`), set it as `public_url` in
`config.json` so the QR encodes that instead of the raw IP.

## Files the server writes

| file | what | safe to delete? |
|---|---|---|
| `state.json` | live party state (crash recovery) | yes — clears the current party |
| `singers.json` | persistent singer-ID registry | keep — stats reference these ids |
| `stats.jsonl` | append-only event history | keep — it's your party history |
| `.media-cache/` | zip extractions | yes — rebuilt on demand |
