# OBS Clip Library

**A free, SteelSeries Moments–style clip library and trimmer that lives inside OBS.**

Press a hotkey to save the last minute of gameplay. A "Clip saved" popup and sound let you know it worked. Then open the **Clips** tab in OBS to watch, trim, adjust the audio, and paste the clip straight into Discord. No account, no upload, no extra app running in the background.

It is one Python script for OBS. It runs entirely on your PC, works offline, and you can edit the code however you like.

<img width="1005" height="515" alt="Screenshot 2026-10-14 180740" src="https://github.com/user-attachments/assets/b334fe5a-7f1e-478f-9835-e284c849210f" />
<img width="1574" height="686" alt="Screenshot 2026-10-14 180707" src="https://github.com/user-attachments/assets/d0c78fae-2731-437b-a13b-13739efe6153" />
<img width="1915" height="1030" alt="Screenshot 2026-10-14 180636" src="https://github.com/user-attachments/assets/2968d1ea-efaf-49ed-8cfd-cd45756152cd" />


---

## Why

SteelSeries Moments, ShadowPlay, and Medal make clipping easy: always-on recording, a hotkey, a library, a quick trim, then share. OBS already has the recording part (the **replay buffer**) and it gives better quality and control, but on its own it just drops files in a folder, so you end up digging through File Explorer and opening a video editor for every clip.

This script adds the missing half to OBS.

| | SteelSeries Moments | OBS Clip Library |
|---|---|---|
| Always-on clipping with a hotkey | Yes | Yes (OBS replay buffer) |
| "Clip saved" notification + sound | Yes | Yes |
| Clip library with thumbnails | Yes | Yes |
| Drag-to-trim timeline | Yes | Yes |
| Separate audio tracks (game / mic / Discord) with mute and volume | Yes | Yes |
| One-click copy to share | Yes | Yes (Ctrl+V into Discord) |
| Recording quality and encoder settings | Limited | Everything OBS offers |
| Account required | Yes | No |
| Open source / editable | No | Yes |

## Features

**Library**
- Clips grouped by day with thumbnails, length, and file size
- Size badge turns red when a clip is over Discord's free upload limit (20 MB)
- Search by name
- "Clip saved" popup and sound the moment you press your hotkey (custom .wav supported)
- Right-click a clip: Open, Rename, Copy for Discord, Show in folder, Delete
- Select several clips with the hover checkbox and delete them together (they go to the Recycle Bin)

**Editor**
- Filmstrip timeline with drag handles to trim
- Exact trimmed file size shown live while you drag
- Play starts wherever the playhead is; optional loop of the trimmed part
- Your trim is remembered per clip, even after restarting OBS
- Drag the bar under the video to make it bigger
- Fullscreen player that fills your whole monitor

**Audio**
- One lane per audio track with its waveform, lined up with the video timeline
- Volume 0–200% and mute per track, with live preview
- Track names come from OBS automatically (for example Desktop Audio, Mic/Aux, Discord); click a name to rename it
- Your audio changes are applied when you copy or save

**Sharing**
- **Copy for Discord**: copies just the trimmed part to the clipboard, so you paste it into Discord with Ctrl+V. Nothing extra is added to your library.
- Over 20 MB? It compresses that copy to fit automatically, using your graphics card when possible. Your original clip stays full quality.
- **Save trim as…**: export the trimmed part anywhere with the normal Windows save window

**Under the hood**
- Runs on `127.0.0.1` only (your own PC). Nothing is uploaded and it works offline.
- The Clips tab updates itself when you update the script, and shows its version number.

## Requirements

- Windows 10 or 11
- [OBS Studio](https://obsproject.com/) 30 or newer (tested on 32.2)
- Python **3.12** (64-bit). OBS needs this exact version for Python scripts.
- ffmpeg (thumbnails, trimming, compression)
- Chrome or Edge (only for the fullscreen player; Edge comes with Windows)

## Setup (about 10 minutes, once)

### 1. Install Python 3.12 and ffmpeg

Right-click the Start button, open **Terminal**, and run:

```
winget install Python.Python.3.12
winget install Gyan.FFmpeg
```

Click **Yes** if Windows asks for permission. Do this before opening OBS (or restart OBS afterward).

No winget? Get Python from [python.org/downloads/windows](https://www.python.org/downloads/windows/) (the newest **3.12.x**, "Windows installer (64-bit)", Install Now) and ffmpeg from [gyan.dev/ffmpeg/builds](https://www.gyan.dev/ffmpeg/builds/) (`ffmpeg-release-essentials.zip`, extracted to `C:\ffmpeg`).

### 2. Set up the OBS replay buffer

The replay buffer is OBS's always-on "save the last X seconds" feature. This video walks through it:

**▶ [How to set up the OBS replay buffer](YOUTUBE_LINK)**

The short version:
1. Add a **Display Capture** source so everything on screen is recorded (games and desktop).
2. **Settings → Output**: enable the **Replay Buffer** and set **Maximum Replay Time** (for example 60 seconds).
3. Set **Recording Format** to **Hybrid MP4** (or MP4). MKV clips may not play in the editor.
4. **Settings → Hotkeys**: give **Save Replay** a key (for example Alt+Insert). Put it on *Save Replay*, not Start/Stop Replay Buffer.
5. Click **Start Replay Buffer**.

### 3. (Optional) Record game, mic and Discord on separate tracks

Skip this and you'll get a single volume slider per clip. Do it and you can mute or turn down your mic, game audio, and Discord separately.

1. In the **Audio Mixer**, click the gear icon → **Advanced Audio Properties**. Tick only these tracks:

   | Source | Tracks |
   |---|---|
   | Desktop Audio | 1 and 2 |
   | Mic/Aux | 1 and 3 |
   | Discord (or any other source) | 1 and 4 |

2. **Settings → Output**: set **Output Mode** to **Advanced**. On the **Recording** tab, tick Audio Track **1, 2, 3 and 4**. Advanced mode has its own copy of the settings, so set the recording path, format (Hybrid MP4), encoder, and bitrate there too, and enable the replay buffer on the **Replay Buffer** tab.
3. Stop and start the replay buffer.

Track 1 holds everything mixed together, so clips still sound normal everywhere else. Tracks 2 and up are what the Clip Library's sliders control.

### 4. Add the script to OBS

1. Download `clip_library.py` and `clip_saved.wav` from this repo and put them in a folder that will stay put, for example `Documents\OBS Scripts`. OBS loads the script from there every time.
2. In OBS: **Tools → Scripts → Python Settings**, then **Browse** to
   `C:\Users\<your name>\AppData\Local\Programs\Python\Python312`
   (can't see AppData? paste `%LOCALAPPDATA%\Programs\Python` into the address bar).
3. **Scripts** tab → **+** → pick `clip_library.py`. Leave the settings blank; it finds your recording folder and ffmpeg on its own.
4. **Docks → Custom Browser Docks**: name it `Clips`, URL `http://127.0.0.1:8765`, then **Apply**.

Press your Save Replay hotkey. You should hear the chime and see the clip appear in the Clips tab.

### 5. (Optional) Start OBS ready to clip

Right-click your OBS shortcut → **Properties** and set **Target** to:

```
"C:\Program Files\obs-studio\bin\64bit\obs64.exe" --startreplaybuffer --minimize-to-tray --disable-updater --disable-shutdown-check
```

(each option starts with two normal hyphens). To launch with Windows, press **Win + R**, type `shell:startup`, and copy the shortcut into that folder.

## Using it

1. **Clip**: press your Save Replay hotkey.
2. **Open**: click the clip in the Clips tab.
3. **Trim**: drag the blue handles. Click the timeline to move the playhead.
4. **Audio**: mute or adjust each track on the left of its lane.
5. **Share**: **Copy for Discord**, then Ctrl+V in Discord. Or **Save trim as…** to keep a copy.

Tips: right-click clips in the library for more options, drag the bar under the video to enlarge it, and double-click a volume slider to reset it to 100%.

## Make it yours

Everything is in `clip_library.py`, and it's meant to be edited:

- **Settings at the top**: `DISCORD_LIMIT_MB` (change to 50 or 500 if you have Nitro), `FIT_TARGET_BYTES`, `STRIP_FRAMES`, and more.
- **The interface**: the dock's HTML, CSS, and JavaScript are in the `PAGE` string near the bottom of the file. Colors are CSS variables at the top of that block.
- **The behavior**: the Python half handles OBS events, the local web server, and the ffmpeg commands for trimming, compressing, waveforms, and thumbnails.

After editing, click **⟳** (reload) in **Tools → Scripts**. The Clips tab notices the new version and refreshes itself. If you change things, bump `VERSION` at the top so the tab knows to reload.

## Troubleshooting

| Problem | Fix |
|---|---|
| Clips tab is blank or can't connect | **Tools → Scripts → Script Log** shows the error. Usually Python isn't set in Python Settings, or it isn't 3.12. |
| Yellow "ffmpeg wasn't found" bar | Install ffmpeg, then fully restart OBS. |
| Hotkey starts the buffer but no clip is saved | The key is on Start/Stop Replay Buffer. Move it to **Save Replay**. |
| "Can't play this clip here" | Recording format is MKV. Switch to Hybrid MP4 or MP4. |
| Only one volume slider | The clip has one audio track. See step 3 (separate tracks). Older clips stay single-track. |
| Display Capture is black on a laptop | Windows Settings → System → Display → Graphics → set OBS to **Power saving**. |
| "Couldn't open port" in the Script Log | Something else uses port 8765. Change **Port** in the script settings and the dock URL to match. |
| Tab doesn't show the new version after updating | Fully close OBS (including the tray icon) and reopen it. |

## How it works

OBS docks can show any web page, so the script runs a tiny private web server on `127.0.0.1:8765`, and the Clips dock is a page served from it. When you trim, copy, or delete, the page asks the script, and the script does the work with ffmpeg and Windows. The server only listens on your own PC, only serves files from your clips folder, and rejects actions that don't come from the Clips page.

Small helper files (thumbnails, waveforms, saved trims and audio settings) live in `%LOCALAPPDATA%\obs-clip-library`. They're safe to delete at any time.

## Credits

Built with help from Claude (Anthropic). Not affiliated with OBS Project or SteelSeries.

## License

[MIT](LICENSE): use it, change it, share it.
