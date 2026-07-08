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

### If the audio stutters or drops out

Work down this list — it's ordered by how often each one is the culprit on a
Pi 4. The first two matter most: the Pi 4's WiFi and Bluetooth **share a
single antenna**, and its blue USB 3 ports **radiate broadband noise across
the whole 2.4 GHz band** — both are famous for exactly this symptom.

1. **Get the Pi off 2.4 GHz WiFi.** Best: plug it into the router by
   Ethernet and turn the radio off entirely —
   ```bash
   sudo rfkill block wifi                 # try it for tonight
   echo "dtoverlay=disable-wifi" | sudo tee -a /boot/firmware/config.txt
   #                                      # make it permanent (reboot)
   ```
   If the Pi must stay on WiFi, put it on a 5 GHz SSID so Bluetooth has the
   2.4 GHz antenna time to itself.
2. **Move the library drive to the black USB 2 ports.** USB 3 traffic (blue
   ports, USB 3 enclosures and cables) jams 2.4 GHz radios sitting inches
   away. USB 2 is plenty fast for serving mp3s; the startup scan barely
   slows. If the drive must stay on USB 3, use a short shielded extension to
   get the enclosure away from the board.
3. **Placement.** Line of sight from Pi to speaker, both above head height
   if you can — a room full of people soaks up 2.4 GHz — and keep it within
   a few meters.
4. **Pin the high-quality profile and stop radio chatter.** If the system
   ever flips the speaker to the headset profile (mono, 16 kHz — sounds like
   a phone call), pin A2DP and disable the headset roles:
   ```bash
   pactl list cards short                 # find bluez_card.XX_XX_...
   pactl set-card-profile bluez_card.XX_XX_XX_XX_XX_XX a2dp-sink
   sudo tee /etc/wireplumber/wireplumber.conf.d/50-bluez.conf >/dev/null <<'EOF'
   monitor.bluez.properties = {
     bluez5.roles = [ a2dp_sink a2dp_source ]
   }
   EOF
   ```
   (Reboot after the WirePlumber change. Pre-Bookworm PulseAudio spells the
   profile `a2dp_sink`.) Also stop discovery once everything is paired:
   `bluetoothctl discoverable off`, `scan off`, `pairable off`.
5. **Check the power supply.** `vcgencmd get_throttled` — anything but
   `0x0` means under-voltage, which glitches USB and radio alike. Use the
   official 5 V / 3 A supply.
6. **Escalation: a Class 1 USB Bluetooth adapter** (100 mW, long-range) on a
   short USB 2 extension away from the board, and disable the onboard radio
   with `dtoverlay=disable-bt` so the two controllers don't fight.
7. **Endgame: go wired.** If the speaker/mixer has an aux input, a ~$10 USB
   audio adapter (cleaner than the Pi's own headphone jack) into it removes
   every radio problem *and* the latency — set **Lyrics sync** back to ~0
   on `/kj` and forget this section exists.

One more distinction worth making: dropouts (sound cutting out) are the radio
issues above, but *distortion* on loud passages is gain staging — pull the
speaker's input trim down a notch rather than running everything at 100%.

## 5. Phones

Guests join over the LAN via the QR code on the intermission screen. If you
run a private hostname (e.g. `karaoke.lan`), set it as `public_url` in
`config.json` so the QR encodes that instead of the raw IP.

## Files the server writes

| file | what | safe to delete? |
|---|---|---|
| `state.json` | live party state (crash recovery) | yes — clears the current party |
| `singers.json` | persistent singer-ID registry | keep — stats reference these ids |
| `versions.json` | KJ's per-song version picks | keep — or resets a song to its best copy |
| `stats.jsonl` | append-only event history | keep — it's your party history |
| `.media-cache/` | zip extractions | yes — rebuilt on demand |
