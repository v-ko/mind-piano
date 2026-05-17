"""Looper engine — recording, playback, and strip management.

Pure logic module. Receives a fluidsynth.Synth reference but owns no
MIDI I/O. The caller (MindPiano) feeds MIDI events in and the Looper
drives the synth.

Time model
----------
All event timestamps and master_duration are stored in **beats**
(1 beat = 1 quarter note).  BPM is purely a playback-speed knob:

    seconds = beats × 60 / bpm
    beats   = seconds × bpm / 60

This lets the user change tempo freely — slow down to record a
difficult passage, speed back up for playback — without re-encoding
anything.  A running beat accumulator handles BPM changes mid-recording.
"""

import threading
import time
from dataclasses import dataclass

from fusion import get_logger

log = get_logger(__name__)

DEFAULT_BPM = 120.0
MIN_BPM = 30.0
MAX_BPM = 300.0

# Metronome: short blip on a dedicated channel
METRONOME_NOTE = 76        # high woodblock (GM percussion)
METRONOME_VELOCITY = 100
METRONOME_DURATION = 0.04  # seconds – short click


@dataclass
class MidiEvent:
    """A recorded MIDI event with a beat-based timestamp."""
    time: float          # in beats, relative to loop start
    msg_type: str
    channel: int
    note: int = 0
    velocity: int = 0

    @classmethod
    def from_mido(cls, msg, time_beats: float, channel: int) -> "MidiEvent":
        return cls(
            time=time_beats,
            msg_type=msg.type,
            channel=channel,
            note=getattr(msg, "note", 0),
            velocity=getattr(msg, "velocity", 0),
        )


class Strip:
    """One layer/track in the looper."""

    def __init__(self, index: int):
        self.index = index
        self.events: list[MidiEvent] = []
        self.muted: bool = False
        self.recording: bool = False

    @property
    def has_content(self) -> bool:
        return len(self.events) > 0

    def clear(self):
        self.events.clear()
        self.recording = False


class Looper:
    """Multi-strip looper with beat-based timing and metronome."""

    def __init__(self, synth, sfid: int, presets: list, strip_count: int):
        self.synth = synth
        self.sfid = sfid
        self.presets = presets

        self.strips = [Strip(i) for i in range(strip_count)]
        self.current_strip: int = 0
        self.master_duration: float | None = None  # in beats
        self.playing: bool = False

        # ── tempo ────────────────────────────────────────────────
        self.bpm: float = DEFAULT_BPM

        # Beat accumulator for recording (handles mid-recording BPM changes)
        self._rec_beat_acc: float = 0.0
        self._rec_bpm: float = DEFAULT_BPM
        self._rec_wall_start: float = 0.0  # wall-clock of last BPM change

        self._playback_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

        # Playback phase tracking — wall-clock ms epoch for each loop start
        self._loop_wall_start: float = 0.0      # monotonic
        self._loop_epoch_ms: float = 0.0         # time.time()*1000, for UI
        self._loop_duration_secs: float = 0.0    # current loop length in secs

        # ── metronome ────────────────────────────────────────────
        self._metronome_on: bool = False
        self._metronome_channel: int = strip_count  # dedicated channel after strips
        self._metronome_thread: threading.Thread | None = None
        self._metronome_stop = threading.Event()

        # Set up the metronome channel to use GM drums (bank 128, preset 0)
        # Channel 9 is GM drums by convention, but we use our own channel
        # and just select a percussion preset
        synth.program_select(self._metronome_channel, sfid, 128, 0)

    # ── tempo ────────────────────────────────────────────────────

    def set_bpm(self, bpm: float):
        bpm = max(MIN_BPM, min(MAX_BPM, bpm))
        old = self.bpm

        # Update beat accumulator if recording
        if self._is_recording():
            now = time.monotonic()
            self._rec_beat_acc += (now - self._rec_wall_start) * (self._rec_bpm / 60.0)
            self._rec_wall_start = now
            self._rec_bpm = bpm

        self.bpm = bpm
        if abs(old - bpm) > 1:
            log.info("BPM → %.0f", bpm)

    def _beats_to_seconds(self, beats: float) -> float:
        return beats * 60.0 / self.bpm

    def _seconds_to_beats(self, seconds: float) -> float:
        return seconds * self.bpm / 60.0

    def _is_recording(self) -> bool:
        return any(s.recording for s in self.strips)

    def _current_rec_beats(self) -> float:
        """Current beat position since recording started."""
        now = time.monotonic()
        return self._rec_beat_acc + (now - self._rec_wall_start) * (self._rec_bpm / 60.0)

    # ── recording ────────────────────────────────────────────────

    def toggle_record(self):
        strip = self.strips[self.current_strip]
        if strip.recording:
            self._stop_recording()
        else:
            self._start_recording()

    def _start_recording(self):
        strip = self.strips[self.current_strip]
        with self._lock:
            strip.clear()
            strip.recording = True
            self._rec_beat_acc = 0.0
            self._rec_bpm = self.bpm
            self._rec_wall_start = time.monotonic()
        log.info("Recording on strip %d (%.0f BPM)", self.current_strip, self.bpm)

        # If a master duration exists, schedule auto-stop
        if self.master_duration is not None:
            duration_secs = self._beats_to_seconds(self.master_duration)
            threading.Timer(
                duration_secs, self._auto_stop_recording,
                args=[self.current_strip],
            ).start()

    def _stop_recording(self):
        strip = self.strips[self.current_strip]
        with self._lock:
            if not strip.recording:
                return
            strip.recording = False
            duration_beats = self._current_rec_beats()

            if self.master_duration is None and strip.has_content:
                self.master_duration = duration_beats
                log.info("Master loop: %.1f beats (%.2fs at %.0f BPM)",
                         duration_beats,
                         self._beats_to_seconds(duration_beats),
                         self.bpm)

        log.info("Stopped recording on strip %d (%d events)",
                 self.current_strip, len(strip.events))

        if not self.playing:
            self.play()

    def _auto_stop_recording(self, strip_index: int):
        strip = self.strips[strip_index]
        if strip.recording:
            log.info("Auto-stop recording on strip %d", strip_index)
            prev = self.current_strip
            self.current_strip = strip_index
            self._stop_recording()
            self.current_strip = prev

    def record_note(self, msg):
        """Called from the MIDI thread for note_on / note_off."""
        strip = self.strips[self.current_strip]
        if not strip.recording:
            return
        beat_time = self._current_rec_beats()
        event = MidiEvent.from_mido(msg, beat_time, self.current_strip)
        with self._lock:
            strip.events.append(event)

    # ── mute / strip selection ───────────────────────────────────

    def toggle_mute(self, strip_index: int):
        if strip_index < 0 or strip_index >= len(self.strips):
            return
        strip = self.strips[strip_index]
        strip.muted = not strip.muted
        log.info("Strip %d %s", strip_index,
                 "muted" if strip.muted else "unmuted")
        if strip.muted:
            self._silence_strip(strip_index)

    def select_next_empty_strip(self):
        for i, s in enumerate(self.strips):
            if not s.has_content and not s.recording:
                self.current_strip = i
                log.info("Selected strip %d", i)
                return
        log.info("No empty strips available")

    # ── metronome ────────────────────────────────────────────────

    def toggle_metronome(self):
        if self._metronome_on:
            self._stop_metronome()
        else:
            self._start_metronome()

    def _start_metronome(self):
        self._metronome_on = True
        self._metronome_stop.clear()
        self._metronome_thread = threading.Thread(
            target=self._metronome_loop, daemon=True)
        self._metronome_thread.start()
        log.info("Metronome ON (%.0f BPM)", self.bpm)

    def _stop_metronome(self):
        self._metronome_on = False
        self._metronome_stop.set()
        if self._metronome_thread:
            self._metronome_thread.join(timeout=1.0)
            self._metronome_thread = None
        log.info("Metronome OFF")

    def _metronome_loop(self):
        while not self._metronome_stop.is_set():
            self.synth.noteon(self._metronome_channel,
                              METRONOME_NOTE, METRONOME_VELOCITY)
            self._metronome_stop.wait(METRONOME_DURATION)
            self.synth.noteoff(self._metronome_channel, METRONOME_NOTE)

            interval = 60.0 / self.bpm  # re-read BPM each tick
            remaining = interval - METRONOME_DURATION
            if remaining > 0:
                self._metronome_stop.wait(remaining)

    # ── playback ─────────────────────────────────────────────────

    def play(self):
        if self.master_duration is None:
            log.info("Nothing recorded yet")
            return
        if self.playing:
            return

        self.playing = True
        self._stop_event.clear()
        self._playback_thread = threading.Thread(
            target=self._playback_loop, daemon=True)
        self._playback_thread.start()
        log.info("Playback started (%.1f beats, %.0f BPM)",
                 self.master_duration, self.bpm)

    def stop(self):
        if not self.playing:
            return
        self.playing = False
        self._stop_event.set()
        if self._playback_thread:
            self._playback_thread.join(timeout=1.0)
            self._playback_thread = None
        self._silence_all()
        log.info("Playback stopped")

    def _playback_loop(self):
        """Clock loop — converts beat-based events to wall-clock using BPM."""
        while not self._stop_event.is_set():
            loop_start = time.monotonic()
            self._loop_wall_start = loop_start
            self._loop_epoch_ms = time.time() * 1000.0
            self._loop_duration_secs = self.master_duration * 60.0 / self.bpm
            loop_bpm = self.bpm  # snapshot BPM for this iteration

            with self._lock:
                schedule = []
                for strip in self.strips:
                    if strip.muted or not strip.has_content:
                        continue
                    for ev in strip.events:
                        schedule.append(ev)

            schedule.sort(key=lambda e: e.time)

            if not schedule:
                wait = self._beats_to_seconds(self.master_duration)
                self._stop_event.wait(wait)
                continue

            for ev in schedule:
                if self._stop_event.is_set():
                    return

                # Convert beat time to wall-clock offset using current BPM
                offset_secs = ev.time * 60.0 / self.bpm
                target = loop_start + offset_secs
                wait = target - time.monotonic()
                if wait > 0:
                    self._stop_event.wait(wait)
                    if self._stop_event.is_set():
                        return

                strip = self.strips[ev.channel]
                if strip.muted:
                    continue
                self._fire_event(ev)

            # Wait for loop remainder
            loop_duration_secs = self.master_duration * 60.0 / self.bpm
            remaining = (loop_start + loop_duration_secs) - time.monotonic()
            if remaining > 0:
                self._stop_event.wait(remaining)

    def _fire_event(self, ev: MidiEvent):
        if ev.msg_type == "note_on":
            self.synth.noteon(ev.channel, ev.note, ev.velocity)
        elif ev.msg_type == "note_off":
            self.synth.noteoff(ev.channel, ev.note)

    # ── silence helpers ──────────────────────────────────────────

    def _silence_strip(self, strip_index: int):
        for note in range(128):
            self.synth.noteoff(strip_index, note)

    def _silence_all(self):
        for i in range(len(self.strips)):
            self._silence_strip(i)
