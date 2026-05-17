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
        self.armed: bool = False
        self.rec_start_beat: float = 0.0  # where overdub started in the loop
        self._pre_overdub_count: int = 0  # for discard on stop

    @property
    def has_content(self) -> bool:
        return len(self.events) > 0

    def clear(self):
        self.events.clear()
        self.recording = False
        self.armed = False
        self.rec_start_beat = 0.0
        self._pre_overdub_count = 0

    def begin_overdub(self):
        """Mark the start of an overdub pass (for discard)."""
        self._pre_overdub_count = len(self.events)

    def discard_overdub(self):
        """Remove events added since the last begin_overdub."""
        del self.events[self._pre_overdub_count:]
        self.recording = False
        self.armed = False


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
        self._auto_stop_timer: threading.Timer | None = None

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

    def current_loop_beats(self) -> float:
        """Current beat position within the loop (0 .. master_duration)."""
        if not self.playing or self.master_duration is None:
            return 0.0
        elapsed = time.monotonic() - self._loop_wall_start
        elapsed_beats = elapsed * self.bpm / 60.0
        return elapsed_beats % self.master_duration

    def toggle_record(self):
        strip = self.strips[self.current_strip]
        if strip.recording or strip.armed:
            self._stop_recording()  # save
        else:
            self._start_recording()

    def _start_recording(self):
        strip = self.strips[self.current_strip]

        if self.master_duration is None:
            # First recording: arm and wait for first note
            with self._lock:
                strip.events.clear()
                strip.armed = True
                strip._pre_overdub_count = 0
            log.info("Armed strip %d (%.0f BPM)", self.current_strip, self.bpm)
        else:
            # Overdub: start recording immediately at current loop position
            start_beat = self.current_loop_beats()
            with self._lock:
                strip.begin_overdub()
                strip.recording = True
                strip.rec_start_beat = start_beat
                self._rec_beat_acc = start_beat
                self._rec_bpm = self.bpm
                self._rec_wall_start = time.monotonic()
            log.info("Recording (overdub) on strip %d at beat %.1f/%.1f",
                     self.current_strip, start_beat, self.master_duration)

    def _begin_recording(self, strip: Strip):
        """Transition from armed → recording. Called on first note."""
        with self._lock:
            strip.armed = False
            strip.recording = True
            strip._pre_overdub_count = 0
            self._rec_beat_acc = 0.0
            self._rec_bpm = self.bpm
            self._rec_wall_start = time.monotonic()
        log.info("Recording started on strip %d (%.0f BPM)",
                 strip.index, self.bpm)

    def _stop_recording(self):
        """Stop recording and save the events."""
        self._cancel_auto_stop()
        strip = self.strips[self.current_strip]
        with self._lock:
            if strip.armed:
                # Was armed but never got a note — just cancel
                strip.armed = False
                log.info("Cancelled armed state on strip %d",
                         self.current_strip)
                return
            if not strip.recording:
                return
            strip.recording = False
            duration_beats = self._current_rec_beats()

            if self.master_duration is None and strip.has_content:
                self.master_duration = round(duration_beats)
                log.info("Master loop: %.1f beats (%.2fs at %.0f BPM)",
                         self.master_duration,
                         self._beats_to_seconds(self.master_duration),
                         self.bpm)

        # Clip existing strips if new master is shorter
        self._clip_strips_to_master()

        log.info("Stopped recording on strip %d (%d events)",
                 self.current_strip, len(strip.events))

        if not self.playing:
            self.play()

    def discard_recording(self):
        """Discard events from the current recording/overdub pass."""
        self._cancel_auto_stop()
        strip = self.strips[self.current_strip]
        with self._lock:
            if strip.armed:
                strip.armed = False
                log.info("Cancelled armed state on strip %d",
                         self.current_strip)
                return
            if not strip.recording:
                return
            strip.discard_overdub()
            log.info("Discarded recording on strip %d", self.current_strip)

    def _cancel_auto_stop(self):
        if self._auto_stop_timer is not None:
            self._auto_stop_timer.cancel()
            self._auto_stop_timer = None

    def _auto_stop_recording(self, strip_index: int):
        strip = self.strips[strip_index]
        if strip.recording:
            elapsed = self._current_rec_beats()
            log.info("Auto-stop recording on strip %d "
                     "(elapsed %.1f beats, master %.1f)",
                     strip_index, elapsed,
                     self.master_duration or 0)
            prev = self.current_strip
            self.current_strip = strip_index
            self._stop_recording()
            self.current_strip = prev
        else:
            log.debug("Auto-stop ignored on strip %d (not recording)",
                      strip_index)

    def record_note(self, msg):
        """Called from the MIDI thread for note_on / note_off."""
        strip = self.strips[self.current_strip]

        # Armed → start recording on first note_on
        if strip.armed and msg.type == "note_on" and msg.velocity > 0:
            self._begin_recording(strip)

        if not strip.recording:
            return
        beat_time = self._current_rec_beats()
        # Wrap overdub events within the master loop
        if self.master_duration is not None:
            beat_time = beat_time % self.master_duration
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

    # ── clear / reset ────────────────────────────────────────────

    def clear_strip(self, strip_index: int):
        """Clear all events from a strip."""
        if strip_index < 0 or strip_index >= len(self.strips):
            return
        strip = self.strips[strip_index]
        with self._lock:
            strip.clear()
        self._silence_strip(strip_index)
        log.info("Cleared strip %d", strip_index)

        # If no strips have content, reset master
        if not any(s.has_content for s in self.strips):
            self.stop()
            self.master_duration = None
            log.info("All strips empty — master duration reset")

    def reset_master(self):
        """Clear master duration. Next recording will set a new one."""
        self.stop()
        old = self.master_duration
        self.master_duration = None
        log.info("Master duration reset (was %.1f beats)",
                 old if old else 0.0)

    def _clip_strips_to_master(self):
        """Remove events beyond master_duration from all strips."""
        if self.master_duration is None:
            return
        for strip in self.strips:
            with self._lock:
                strip.events = [
                    e for e in strip.events if e.time < self.master_duration
                ]

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
        # Cancel any pending auto-stop and discard active recordings
        self._cancel_auto_stop()
        for strip in self.strips:
            if strip.recording or strip.armed:
                with self._lock:
                    strip.discard_overdub()
                log.info("Discarded recording on strip %d", strip.index)

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
                    if not strip.has_content:
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
                if strip.muted or not strip.has_content:
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
