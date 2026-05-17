#!/usr/bin/env python

import os
import signal
import subprocess
import sys
import threading
import time

os.environ.setdefault("LOGLEVEL", "DEBUG")

import click
import fluidsynth
import fusion
import mido
from fusion import get_logger
from fusion.libs.action import action
from sf2utils.sf2parse import Sf2File

from mind_piano.config import Config
from mind_piano.looper import Looper

log = get_logger(__name__)


@action("sync_view_state")
def sync_view_state(vs, mp):
    """Main-thread action: reads looper/MindPiano state → view state."""
    looper = mp.looper
    vs.bpm = looper.bpm
    vs.metronomeOn = looper._metronome_on
    vs.playing = looper.playing
    vs.recording = any(s.recording for s in looper.strips)
    vs.currentStrip = looper.current_strip
    vs.masterGain = mp.master_gain
    vs.loopDuration = looper._loop_duration_secs
    vs.loopEpoch = looper._loop_epoch_ms

    for i, strip in enumerate(looper.strips):
        if i >= vs.stripCount:
            break
        ss = vs.strip(i)
        ss.muted = strip.muted
        ss.recording = strip.recording
        ss.has_content = strip.has_content
        ss.instrument = mp.strip_instruments[i]


class MindPiano:
    """Synth setup + MIDI routing. Delegates looping to Looper."""

    def __init__(self, config: Config):
        self.config = config
        self.view_state = None  # set after construction
        strip_count = max(len(config.strips), 8)

        # ── Master strip state ────────────────────────────────────
        self._modifier_held = False
        self._master_strip = config.master_strip  # index or None
        self._master_fader_cc: int | None = None
        self._master_button_key: tuple | None = None

        if self._master_strip is not None and self._master_strip < len(config.strips):
            ms = config.strips[self._master_strip]
            fader = ms.get("fader", {})
            if fader.get("type", "cc") == "cc":
                self._master_fader_cc = fader.get("number")
            btn = ms.get("button", {})
            self._master_button_key = (btn.get("type", "cc"), btn.get("number"))

        # ── Binding lookup tables ────────────────────────────────
        self._strip_button_to_index: dict[tuple, int] = {}
        for i, strip in enumerate(config.strips):
            if i == self._master_strip:
                continue  # master strip button handled separately
            btn = strip.get("button", {})
            key = (btn.get("type", "cc"), btn.get("number"))
            self._strip_button_to_index[key] = i

        transport = config.transport
        self._binding_record = _binding_key(transport.get("record"))
        self._binding_play = _binding_key(transport.get("play"))
        self._binding_stop = _binding_key(transport.get("stop"))
        self._binding_modifier = _binding_key(transport.get("modifier"))

        # ── FluidSynth ──────────────────────────────────────────
        self.fs = fluidsynth.Synth()
        self.fs.setting("synth.gain", config.synth_gain)
        self.fs.start(driver=config.audio_driver,
                      midi_driver=config.midi_driver)

        for ch in range(strip_count):
            self.fs.cc(ch, 7, 127)  # max volume per channel

        soundfont = config.soundfont_file
        if not soundfont or not os.path.isfile(soundfont):
            log.error("Soundfont not found: %s", soundfont)
            log.error("Edit config at: %s", config.config_file)
            sys.exit(1)

        self.sfid = self.fs.sfload(soundfont)
        self.presets = _load_presets(soundfont)
        log.info("Loaded %d presets", len(self.presets))

        # Default instrument (preset 0) on all strip channels
        if self.presets:
            p = self.presets[0]
            for ch in range(strip_count):
                self.fs.program_select(ch, self.sfid, p["bank"], p["preset"])

        # ── Looper ───────────────────────────────────────────────
        self.looper = Looper(self.fs, self.sfid, self.presets, strip_count)

        # Per-strip instrument names (written by MIDI thread, read by UI timer)
        default_name = self.presets[0].get("name", "Piano") if self.presets else "Piano"
        self.strip_instruments: list[str] = [default_name] * strip_count
        self.master_gain: float = config.synth_gain

    # ── MIDI event routing ───────────────────────────────────────

    def _schedule_view_sync(self):
        """Schedule a view state sync on the main thread (thread-safe)."""
        if self.view_state is not None:
            fusion.call_delayed(sync_view_state, 0,
                                args=[self.view_state, self])

    def midi_event_retrieval_loop(self):
        port = _open_midi_port_for_app(self.config)
        if port is None:
            return

        log.info("Listening for MIDI events…")
        with port:
            for msg in port:
                log.debug("MIDI: %s", msg)

                if msg.type in ("note_on", "note_off"):
                    self._handle_note(msg)
                elif msg.type in ("program_change", "control_change"):
                    self._handle_binding_or_program(msg)
                else:
                    log.debug("No handler for: %s", msg)
                    continue

                self._schedule_view_sync()

    def _handle_note(self, msg):
        """Play note or select instrument if modifier is held."""
        if self._modifier_held and msg.type == "note_on" and msg.velocity > 0:
            self._select_preset(msg.note)
            return
        if self._modifier_held:
            return  # swallow note_off while modifier held

        ch = self.looper.current_strip
        if msg.type == "note_on":
            self.fs.noteon(ch, msg.note, msg.velocity)
        else:
            self.fs.noteoff(ch, msg.note)
        self.looper.record_note(msg)

    def _handle_binding_or_program(self, msg):
        key = _msg_binding_key(msg)

        # ── Modifier button (transport): track held state ────────
        if key == self._binding_modifier:
            if msg.type == "control_change":
                self._modifier_held = msg.value > 0
                log.debug("Modifier %s", "held" if self._modifier_held else "released")
            return

        # ── Master strip button: toggle metronome ────────────────
        if key == self._master_button_key:
            if msg.type == "control_change" and msg.value == 127:
                return  # act on release
            self.looper.toggle_metronome()
            return

        # ── Master strip fader: master gain ──────────────────────
        if (msg.type == "control_change"
                and self._master_fader_cc is not None
                and msg.control == self._master_fader_cc):
            gain = (msg.value / 127.0) * 5.0  # 0.0 – 5.0 range
            self.fs.setting("synth.gain", gain)
            self.master_gain = gain
            log.debug("Master gain → %.2f", gain)
            return

        # ── Modifier held + mod wheel (CC 1): set tempo ─────────
        if (self._modifier_held
                and msg.type == "control_change"
                and msg.control == 1):
            bpm = 30 + (msg.value / 127.0) * 270  # 30–300 BPM
            self.looper.set_bpm(bpm)
            return

        # ── Transport ────────────────────────────────────────────
        if key == self._binding_record:
            if msg.type == "control_change" and msg.value == 127:
                return  # act on release
            self.looper.toggle_record()

        elif key == self._binding_play:
            if msg.type == "control_change" and msg.value == 127:
                return
            self.looper.play()

        elif key == self._binding_stop:
            if msg.type == "control_change" and msg.value == 127:
                return
            self.looper.stop()

        elif key in self._strip_button_to_index:
            if msg.type == "control_change" and msg.value == 127:
                return
            idx = self._strip_button_to_index[key]
            if self._modifier_held:
                self.looper.current_strip = idx
                log.info("Selected strip %d", idx)
            else:
                self.looper.toggle_mute(idx)

        elif msg.type == "program_change":
            self._handle_program_change(msg)
        else:
            log.debug("Unbound: %s", msg)

    def _handle_program_change(self, msg):
        ch = self.looper.current_strip
        i = min(msg.program, len(self.presets) - 1)
        p = self.presets[i]
        self.fs.program_select(ch, self.sfid, p["bank"], p["preset"])
        log.info("Strip %d → preset %d (bank=%d)", ch, p["preset"], p["bank"])

    def _select_preset(self, note: int):
        """Select instrument for the current strip and play a preview."""
        if not self.presets:
            return
        i = min(note, len(self.presets) - 1)
        ch = self.looper.current_strip
        p = self.presets[i]
        self.fs.program_select(ch, self.sfid, p["bank"], p["preset"])
        log.info("Strip %d → instrument %d (note=%d, bank=%d)",
                 ch, p["preset"], note, p["bank"])
        self.strip_instruments[ch] = p.get("name", f"Preset {i}")
        self._preview_instrument(ch)

    def _preview_instrument(self, channel: int):
        """Play a quick 3-octave triplet so the user hears the instrument."""
        notes = [48, 60, 72]  # C3, C4, C5 — one octave apart
        vel = 80
        delay = 0.08
        hold = 0.15

        def _play():
            for n in notes:
                self.fs.noteon(channel, n, vel)
                time.sleep(delay)
            time.sleep(hold)
            for n in notes:
                self.fs.noteoff(channel, n)

        threading.Thread(target=_play, daemon=True).start()

    def shutdown(self):
        self.looper.stop()
        self.looper._stop_metronome()
        self.fs.delete()


# ── Helpers ──────────────────────────────────────────────────────


def _binding_key(binding) -> tuple | None:
    """Normalise a binding value (dict or legacy int) to a (type, number) tuple."""
    if binding is None:
        return None
    if isinstance(binding, dict):
        return (binding.get("type", "cc"), binding.get("number"))
    return ("cc", binding)


def _msg_binding_key(msg) -> tuple | None:
    if msg.type == "control_change":
        return ("cc", msg.control)
    if msg.type == "program_change":
        return ("program_change", msg.program)
    return None


def _load_presets(sf2_path: str) -> list[dict]:
    presets = []
    with open(sf2_path, "rb") as f:
        sf2 = Sf2File(f)
        for preset in sf2.presets:
            if not hasattr(preset, "bank"):
                continue
            presets.append({
                "bank": preset.bank,
                "preset": preset.preset,
                "name": getattr(preset, "name", f"Preset {preset.preset}"),
            })
    return presets


def _open_midi_port_for_app(config):
    """Open MIDI port for the running app. Returns port or None."""
    input_names = mido.get_input_names()
    if not input_names:
        log.error("No MIDI input devices found.")
        return None

    keyword = config.midi_device_keyword
    choice = None
    if keyword:
        for i, name in enumerate(input_names):
            if keyword in name:
                choice = i
                break

    if choice is None:
        if keyword:
            log.warning('MIDI device matching "%s" not found.', keyword)
        if len(input_names) == 1:
            choice = 0
            log.info("Auto-selecting: %s", input_names[0])
        else:
            log.info("Available MIDI devices:")
            for i, name in enumerate(input_names):
                log.info("  %d: %s", i, name)
            try:
                choice = int(input("Select device number: "))
            except (ValueError, EOFError):
                log.error("Invalid selection.")
                return None

    return mido.open_input(input_names[choice])


def _setup_tray(app, config, qml_window):
    """System tray icon — click or right-click opens window."""
    from PySide6.QtGui import QAction
    from PySide6.QtWidgets import QMenu, QStyle, QSystemTrayIcon

    tray = QSystemTrayIcon(
        app.style().standardIcon(QStyle.StandardPixmap.SP_MediaVolume), app)
    tray.setToolTip("Mind Piano")

    def _toggle_window():
        if qml_window.isVisible():
            qml_window.hide()
        else:
            qml_window.show()
            qml_window.raise_()

    # Left-click toggles the window
    tray.activated.connect(lambda reason: _toggle_window()
                           if reason == QSystemTrayIcon.ActivationReason.Trigger
                           else None)

    menu = QMenu()
    show_action = QAction("Show Window", app)
    show_action.triggered.connect(_toggle_window)
    menu.addAction(show_action)

    open_cfg = QAction("Open Config", app)
    open_cfg.triggered.connect(
        lambda: subprocess.Popen(["xdg-open", str(config.config_file)]))
    menu.addAction(open_cfg)

    quit_action = QAction("Quit", app)
    quit_action.triggered.connect(app.quit)
    menu.addAction(quit_action)

    tray.setContextMenu(menu)
    tray.show()
    return tray


def _run_app(config: Config):
    """Start the synth, tray icon, QML window, and MIDI loop."""
    from pathlib import Path

    from fusion.loop import set_main_loop
    from fusion.platform.qt_widgets.qt_main_loop import QtMainLoop
    from PySide6.QtCore import QUrl
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtWidgets import QApplication

    from mind_piano.view_state import AppViewState

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    set_main_loop(QtMainLoop(app))

    strip_count = max(len(config.strips), 8)
    view_state = AppViewState(strip_count, parent=app)

    mp = MindPiano(config)
    mp.view_state = view_state

    # QML engine
    engine = QQmlApplicationEngine()
    ctx = engine.rootContext()
    ctx.setContextProperty("appState", view_state)

    qml_file = Path(__file__).parent / "views" / "MainWindow.qml"
    engine.load(QUrl.fromLocalFile(str(qml_file)))
    if not engine.rootObjects():
        log.error("Failed to load QML: %s", qml_file)
        sys.exit(1)
    qml_window = engine.rootObjects()[0]

    tray = _setup_tray(app, config, qml_window)

    midi_thread = threading.Thread(
        target=mp.midi_event_retrieval_loop, daemon=True)
    midi_thread.start()

    ret = app.exec()
    mp.shutdown()
    sys.exit(ret)


def _open_midi_port(config):
    """Open a MIDI input port using the configured keyword. Returns (port, name)."""
    input_names = mido.get_input_names()
    if not input_names:
        click.echo("No MIDI devices found. Plug one in and try again.")
        sys.exit(1)

    keyword = config.midi_device_keyword
    choice = None
    if keyword:
        for i, name in enumerate(input_names):
            if keyword in name:
                choice = i
                break

    if choice is None:
        click.echo("Available MIDI devices:")
        for i, name in enumerate(input_names):
            click.echo(f"  {i}: {name}")
        if len(input_names) == 1:
            choice = 0
        else:
            choice = click.prompt("Select device number", type=int)

    port = mido.open_input(input_names[choice])
    click.echo(f"Using: {input_names[choice]}\n")
    return port


def _drain_port(port, settle_time=0.4):
    """Discard messages for settle_time seconds to let faders stop sending."""
    deadline = time.time() + settle_time
    while time.time() < deadline:
        msg = port.poll()
        if msg is None:
            time.sleep(0.01)


def _capture_cc(port, prompt_text):
    """Block until a CC or program_change message is received.
    Returns (type, number) — ('cc', control) or ('program_change', program)."""
    click.echo(prompt_text, nl=False)
    while True:
        msg = port.receive()
        if msg.type == 'control_change' and msg.value > 0:
            click.echo(f" CC {msg.control} (value={msg.value})")
            _drain_port(port)
            return ('cc', msg.control)
        elif msg.type == 'program_change':
            click.echo(f" program_change {msg.program}")
            _drain_port(port)
            return ('program_change', msg.program)
        else:
            log.debug(f"Ignored: {msg}")


def _run_keys_wizard(config):
    """Interactive wizard to capture strip and transport bindings."""
    port = _open_midi_port(config)

    try:
        # Strips
        count = click.prompt("How many strips (button+fader pairs)?", type=int, default=len(config.strips) or 8)
        strips = []
        for i in range(count):
            click.echo(f"\n--- Strip {i + 1} ---")
            btn_type, btn_num = _capture_cc(port, f"  Press the button:  ")
            fader_type, fader_num = _capture_cc(port, f"  Move the fader:    ")
            strips.append({
                "button": {"type": btn_type, "number": btn_num},
                "fader": {"type": fader_type, "number": fader_num},
            })

        # Transport
        click.echo("\n--- Transport ---")
        rec_type, rec_num = _capture_cc(port, "  Press Record:   ")
        play_type, play_num = _capture_cc(port, "  Press Play:     ")
        stop_type, stop_num = _capture_cc(port, "  Press Stop:     ")
        mod_type, mod_num = _capture_cc(port, "  Press Modifier:  ")

        config.set("strips", strips)
        config.set("transport", {
            "record": {"type": rec_type, "number": rec_num},
            "play": {"type": play_type, "number": play_num},
            "stop": {"type": stop_type, "number": stop_num},
            "modifier": {"type": mod_type, "number": mod_num},
        })

        # Master strip
        click.echo("\n--- Master Strip ---")
        mod = click.prompt(
            "Which strip is the master? (1-%d, or 0 for none)" % count,
            type=int, default=0)
        if 1 <= mod <= count:
            config.set("master_strip", mod - 1)
            click.echo(f"Strip {mod} set as master (metronome + gain fader).")
        else:
            config.set("master_strip", None)
            click.echo("No master strip.")

        click.echo(f"\nBindings saved to {config.config_file}")
    finally:
        port.close()


def _run_device_wizard(config):
    """Interactive wizard for device selection."""
    input_names = mido.get_input_names()
    if not input_names:
        click.echo("No MIDI devices detected (plug one in first).")
        sys.exit(1)

    click.echo("Available MIDI devices:")
    for i, name in enumerate(input_names):
        click.echo(f"  {i}: {name}")

    if len(input_names) == 1:
        choice = 0
        click.echo(f"Auto-selecting the only device: {input_names[0]}")
    else:
        choice = click.prompt("Select device number", type=int)

    if choice < 0 or choice >= len(input_names):
        click.echo("Invalid selection.")
        sys.exit(1)

    # Extract a unique keyword from the device name (first word before ':')
    selected = input_names[choice]
    keyword = selected.split(":")[0].strip()
    click.echo(f"Using keyword: {keyword!r}")
    config.set("midi_device_keyword", keyword)


def _run_full_setup():
    """Full setup wizard: device keyword + key bindings."""
    config = Config()
    _run_device_wizard(config)
    click.echo()
    _run_keys_wizard(config)
    click.echo(f"\nSetup complete. Config saved to {config.config_file}")


@click.group(invoke_without_command=True)
@click.pass_context
def cli(ctx):
    """Mind Piano — MIDI keyboard instrument with recording/looping."""
    if ctx.invoked_subcommand is None:
        config = Config()
        if not config.config_file.exists():
            click.echo(
                "WARNING: No config file found. "
                "Run 'mind-piano config setup' to choose a keyboard and create one."
            )
            sys.exit(1)
        _run_app(config)


@cli.group()
def config():
    """Manage the configuration."""
    pass


@config.command("setup")
def config_setup():
    """Full setup wizard: device + key bindings."""
    _run_full_setup()


@config.command("keys")
def config_keys():
    """Capture strip and transport key bindings."""
    cfg = Config()
    if not cfg.config_file.exists():
        click.echo("No config file. Run 'mind-piano config setup' first.")
        sys.exit(1)
    _run_keys_wizard(cfg)


@config.command("open")
def config_open():
    """Open the config file in the default editor."""
    import subprocess
    cfg = Config()
    if not cfg.config_file.exists():
        click.echo("Config file does not exist yet. Run 'mind-piano config setup' first.")
        sys.exit(1)
    subprocess.Popen(["xdg-open", str(cfg.config_file)])


@config.command("delete")
def config_delete():
    """Delete the config file."""
    cfg = Config()
    if cfg.config_file.exists():
        cfg.config_file.unlink()
        click.echo(f"Deleted {cfg.config_file}")
    else:
        click.echo("No config file to delete.")


def main():
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    cli()


if __name__ == '__main__':
    main()
