# Mind Piano

A multi-strip MIDI looper with SoundFont playback, metronome, and a QML visualization — controlled entirely from a MIDI controller. Built with [FluidSynth](https://www.fluidsynth.org/) and the [fusion](https://github.com/v-ko/fusion) state management framework.

## Features

- **Multi-strip looper** — record loops on independent strips, each with its own MIDI channel. The first recording sets the master loop duration; subsequent strips auto-quantize to it.
- **Beat-based timing** — all events are stored in beats, not seconds. Tempo changes mid-recording are handled correctly.
- **Instrument selection** — hold Modifier + press a piano key to select any SoundFont preset for the current strip. A preview triplet plays automatically.
- **Metronome** — toggleable metronome on a dedicated GM drums channel, synced to the current BPM.
- **Tempo control** — hold Modifier + move the mod wheel (CC 1) to set BPM (30–300).
- **Master gain** — a dedicated fader controls FluidSynth's global gain (0–5).
- **QML status window** — shows transport state, BPM, strip status (mute/recording/content), a looping phase bar, and a help panel. Runs as a tray app.
- **Configurable bindings** — all buttons, faders, and transport controls are captured via an interactive wizard. Supports CC and program_change messages.

## Setup

System dependencies:

```bash
# Ubuntu/Debian (either soundfont package works)
sudo apt install fluidsynth libfluidsynth-dev fluid-soundfont-gm  # or freepats

# Arch
sudo pacman -S freepats-general-midi fluidsynth
```

Install [fusion](https://github.com/v-ko/fusion) first (not yet on PyPI):

```bash
pip install /path/to/fusion/py-src
```

Then install mind-piano:

```bash
pip install -e .
python install.py  # adds .desktop entry
```

## Usage

Run the setup wizard to select your MIDI device and soundfont:

```bash
mind-piano config setup
```

Capture strip and transport bindings:

```bash
mind-piano config keys
```

Then launch:

```bash
mind-piano
```

Other config commands:

```bash
mind-piano config open    # open config.json in default editor
mind-piano config delete  # remove the config file
```

A system tray icon lets you toggle the QML window, open the config, or quit.

### Config options

| Key | Description |
|---|---|
| `midi_device_keyword` | Substring to match your MIDI device (e.g. `"Oxygen"`) |
| `soundfont_file` | Path to the `.sf2` soundfont file |
| `extra_sf2_folder` | Extra folder to scan for soundfonts |
| `project_folder` | Directory for recordings |
| `audio_driver` | `pulseaudio`, `pipewire`, `alsa` |
| `midi_driver` | `alsa_seq` |
| `synth_gain` | Master volume (default `2.0`) |
| `master_strip` | Index of the strip used as master (gain fader + metronome button) |

### MIDI controls

All controls are configured via the `mind-piano config keys` wizard. Typical layout:

- **Piano keys** — play on the current strip's channel
- **Modifier (hold)** — enables secondary functions:
  - Modifier + piano key → select instrument for current strip
  - Modifier + mod wheel (CC 1) → set tempo
  - Modifier + strip button → select strip
- **Strip buttons** — toggle mute on each strip
- **Master strip button** — toggle metronome
- **Master strip fader** — master gain
- **Record** — toggle recording on the current strip
- **Play** — start/loop playback
- **Stop** — stop playback and all sound

## Architecture

- **`main.py`** — `MindPiano` class: MIDI routing, synth setup, binding tables, instrument selection. Entry points and CLI.
- **`looper.py`** — `Looper` class: beat-based multi-strip recording/playback engine, metronome, tempo control.
- **`view_state.py`** — `AppViewState` / `StripState`: QObject-based view state with PySide6 Properties and Signals.
- **`views/MainWindow.qml`** — QML UI: transport bar, phase indicator, strip list, help panel.
- **`config.py`** — JSON config at `~/.config/mind-piano/config.json`.

Cross-thread model: MIDI events are received on a daemon thread. Synth calls (low latency) happen there directly. View state updates are dispatched to the Qt main thread via `fusion.call_delayed`.
