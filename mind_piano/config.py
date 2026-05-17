import json
import os
from pathlib import Path
from typing import Any, Optional

COMMON_SOUNDFONT_PATHS = [
    "/usr/share/sounds/sf2/FluidR3_GM.sf2",
    "/usr/share/soundfonts/freepats-general-midi.sf2",
    "/usr/share/soundfonts/FluidR3_GM.sf2",
    "/usr/share/soundfonts/default.sf2",
]

COMMON_SF2_FOLDERS = [
    "/usr/share/calf/sf2",
    "/usr/share/sounds/sf2",
    "/usr/share/soundfonts",
]


def _find_default_soundfont() -> str:
    """Find the first available soundfont file on the system."""
    for path in COMMON_SOUNDFONT_PATHS:
        if os.path.isfile(path):
            return path
    return ""


def _find_default_sf2_folder() -> str:
    """Find the first available SF2 folder on the system."""
    for path in COMMON_SF2_FOLDERS:
        if os.path.isdir(path):
            return path
    return ""


DEFAULT_CONFIG = {
    "project_folder": str(Path.home() / "mind-piano-projects"),
    "soundfont_file": "",  # Auto-detected on first run
    "extra_sf2_folder": "",  # Optional extra SF2 folder
    "midi_device_keyword": "",  # Substring to match MIDI device name
    "audio_driver": "pulseaudio",
    "midi_driver": "alsa_seq",
    "synth_gain": 2.0,
    "master_strip": None,  # Index into strips[] for the master strip (metronome + gain fader)
    "strips": [],  # [{"button": CC, "fader": CC}, ...]
    "transport": {  # Transport button CC numbers
        "record": None,
        "play": None,
        "stop": None,
        "modifier": None,  # Hold for strip-select / instrument-select
    },
}


class Config:
    def __init__(self, config_path: Optional[str] = None):
        self.config_dir = Path(
            config_path or Path.home() / ".config" / "mind-piano"
        )
        self.config_file = self.config_dir / "config.json"
        self._config = dict(DEFAULT_CONFIG)
        self._init_config()

    def _init_config(self):
        self.config_dir.mkdir(parents=True, exist_ok=True)

        if self.config_file.exists():
            try:
                with open(self.config_file, "r") as f:
                    loaded = json.load(f)
                    self._config.update(loaded)
            except (json.JSONDecodeError, IOError) as e:
                print(f"Error loading config: {e}")
        else:
            # Auto-detect defaults but don't save yet
            self._config["soundfont_file"] = _find_default_soundfont()
            self._config["extra_sf2_folder"] = _find_default_sf2_folder()

    def save(self):
        try:
            with open(self.config_file, "w") as f:
                json.dump(self._config, f, indent=2)
        except IOError as e:
            print(f"Error saving config: {e}")

    def get(self, key: str, default: Any = None) -> Any:
        return self._config.get(key, default)

    def set(self, key: str, value: Any):
        self._config[key] = value
        self.save()

    @property
    def project_folder(self) -> str:
        return self._config["project_folder"]

    @property
    def soundfont_file(self) -> str:
        return self._config["soundfont_file"]

    @property
    def extra_sf2_folder(self) -> str:
        return self._config["extra_sf2_folder"]

    @property
    def midi_device_keyword(self) -> str:
        return self._config["midi_device_keyword"]

    @property
    def audio_driver(self) -> str:
        return self._config["audio_driver"]

    @property
    def midi_driver(self) -> str:
        return self._config["midi_driver"]

    @property
    def synth_gain(self) -> float:
        return self._config["synth_gain"]

    @property
    def master_strip(self) -> int | None:
        return self._config.get("master_strip")

    @property
    def strips(self) -> list:
        return self._config.get("strips", [])

    @property
    def transport(self) -> dict:
        return self._config.get("transport", {})
