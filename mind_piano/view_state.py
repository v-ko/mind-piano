"""View state for the Mind Piano visualization window.

QObject-based state exposed to QML via context properties.
Updated from @action functions; QML binds declaratively.
"""

from __future__ import annotations

from fusion.platform.qt_widgets import Property
from PySide6.QtCore import QObject, Signal, Slot


class StripState(QObject):
    """Per-strip observable state."""

    index_changed = Signal(int)
    instrument_changed = Signal(str)
    muted_changed = Signal(bool)
    recording_changed = Signal(bool)
    has_content_changed = Signal(bool)
    is_current_changed = Signal(bool)

    def __init__(self, index: int, parent: QObject | None = None):
        super().__init__(parent)
        self._index = index
        self._instrument = "Piano"
        self._muted = False
        self._recording = False
        self._has_content = False
        self._is_current = index == 0

    @Property(int, notify=index_changed)
    def index(self) -> int:
        return self._index

    @Property(str, notify=instrument_changed)
    def instrument(self) -> str:
        return self._instrument

    @instrument.setter
    def instrument(self, value: str) -> None:
        if self._instrument == value:
            return
        self._instrument = value
        self.instrument_changed.emit(value)

    @Property(bool, notify=muted_changed)
    def muted(self) -> bool:
        return self._muted

    @muted.setter
    def muted(self, value: bool) -> None:
        if self._muted == value:
            return
        self._muted = value
        self.muted_changed.emit(value)

    @Property(bool, notify=recording_changed)
    def recording(self) -> bool:
        return self._recording

    @recording.setter
    def recording(self, value: bool) -> None:
        if self._recording == value:
            return
        self._recording = value
        self.recording_changed.emit(value)

    @Property(bool, notify=has_content_changed)
    def has_content(self) -> bool:
        return self._has_content

    @has_content.setter
    def has_content(self, value: bool) -> None:
        if self._has_content == value:
            return
        self._has_content = value
        self.has_content_changed.emit(value)

    @Property(bool, notify=is_current_changed)
    def is_current(self) -> bool:
        return self._is_current

    @is_current.setter
    def is_current(self, value: bool) -> None:
        if self._is_current == value:
            return
        self._is_current = value
        self.is_current_changed.emit(value)


class AppViewState(QObject):
    """Top-level observable state for the entire app."""

    bpm_changed = Signal(float)
    metronome_on_changed = Signal(bool)
    playing_changed = Signal(bool)
    recording_changed = Signal(bool)
    loop_duration_changed = Signal(float)
    loop_epoch_changed = Signal(float)
    current_strip_changed = Signal(int)
    master_gain_changed = Signal(float)
    strip_count_changed = Signal(int)
    soundfont_name_changed = Signal(str)
    preset_count_changed = Signal(int)

    def __init__(self, strip_count: int = 8, parent: QObject | None = None):
        super().__init__(parent)
        self._bpm = 120.0
        self._metronome_on = False
        self._playing = False
        self._recording = False
        self._loop_duration = 0.0   # seconds
        self._loop_epoch = 0.0      # JS Date.now()-compatible ms timestamp
        self._current_strip = 0
        self._master_gain = 2.0
        self._strip_count = strip_count
        self._soundfont_name = ""
        self._preset_count = 0
        self._strips: list[StripState] = [
            StripState(i, parent=self) for i in range(strip_count)
        ]

    def strip(self, index: int) -> StripState:
        return self._strips[index]

    @Slot(int, result=QObject)
    def getStrip(self, index: int) -> QObject:
        """Expose strip state to QML."""
        if 0 <= index < len(self._strips):
            return self._strips[index]
        return None

    @Property(int, constant=True)
    def stripCount(self) -> int:
        return self._strip_count

    @Property(float, notify=bpm_changed)
    def bpm(self) -> float:
        return self._bpm

    @bpm.setter
    def bpm(self, value: float) -> None:
        if abs(self._bpm - value) < 0.1:
            return
        self._bpm = value
        self.bpm_changed.emit(value)

    @Property(bool, notify=metronome_on_changed)
    def metronomeOn(self) -> bool:
        return self._metronome_on

    @metronomeOn.setter
    def metronomeOn(self, value: bool) -> None:
        if self._metronome_on == value:
            return
        self._metronome_on = value
        self.metronome_on_changed.emit(value)

    @Property(bool, notify=playing_changed)
    def playing(self) -> bool:
        return self._playing

    @playing.setter
    def playing(self, value: bool) -> None:
        if self._playing == value:
            return
        self._playing = value
        self.playing_changed.emit(value)

    @Property(bool, notify=recording_changed)
    def recording(self) -> bool:
        return self._recording

    @recording.setter
    def recording(self, value: bool) -> None:
        if self._recording == value:
            return
        self._recording = value
        self.recording_changed.emit(value)

    @Property(float, notify=loop_duration_changed)
    def loopDuration(self) -> float:
        """Loop length in seconds. QML uses this + loopEpoch to derive phase."""
        return self._loop_duration

    @loopDuration.setter
    def loopDuration(self, value: float) -> None:
        if abs(self._loop_duration - value) < 0.001:
            return
        self._loop_duration = value
        self.loop_duration_changed.emit(value)

    @Property(float, notify=loop_epoch_changed)
    def loopEpoch(self) -> float:
        """Wall-clock ms (Date.now()-compatible) when the current loop iteration started."""
        return self._loop_epoch

    @loopEpoch.setter
    def loopEpoch(self, value: float) -> None:
        self._loop_epoch = value
        self.loop_epoch_changed.emit(value)

    @Property(int, notify=current_strip_changed)
    def currentStrip(self) -> int:
        return self._current_strip

    @currentStrip.setter
    def currentStrip(self, value: int) -> None:
        if self._current_strip == value:
            return
        old = self._current_strip
        self._current_strip = value
        # Update is_current flags
        if 0 <= old < len(self._strips):
            self._strips[old].is_current = False
        if 0 <= value < len(self._strips):
            self._strips[value].is_current = True
        self.current_strip_changed.emit(value)

    @Property(float, notify=master_gain_changed)
    def masterGain(self) -> float:
        return self._master_gain

    @masterGain.setter
    def masterGain(self, value: float) -> None:
        if abs(self._master_gain - value) < 0.01:
            return
        self._master_gain = value
        self.master_gain_changed.emit(value)

    @Property(str, notify=soundfont_name_changed)
    def soundfontName(self) -> str:
        return self._soundfont_name

    @soundfontName.setter
    def soundfontName(self, value: str) -> None:
        if self._soundfont_name == value:
            return
        self._soundfont_name = value
        self.soundfont_name_changed.emit(value)

    @Property(int, notify=preset_count_changed)
    def presetCount(self) -> int:
        return self._preset_count

    @presetCount.setter
    def presetCount(self, value: int) -> None:
        if self._preset_count == value:
            return
        self._preset_count = value
        self.preset_count_changed.emit(value)
