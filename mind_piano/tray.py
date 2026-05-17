import subprocess
from pathlib import Path

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

_ICON_PATH = Path(__file__).parent / "resources" / "mind-piano.svg"


class TrayIcon:
    def __init__(self, app, on_quit, on_open_config):
        self._app = app
        self._on_quit = on_quit
        self._on_open_config = on_open_config

        self._tray = QSystemTrayIcon(QIcon(str(_ICON_PATH)), app)
        self._tray.setToolTip("Mind Piano")

        menu = QMenu()
        self._status_action = menu.addAction("Starting...")
        self._status_action.setEnabled(False)
        menu.addSeparator()
        open_config_action = menu.addAction("Open Config")
        open_config_action.triggered.connect(self._on_open_config)
        quit_action = menu.addAction("Quit")
        quit_action.triggered.connect(self._on_quit)

        self._tray.setContextMenu(menu)
        self._tray.show()

    def set_status(self, text: str):
        self._status_action.setText(text)


def open_config_file(config_path):
    """Open the config file in the default editor."""
    try:
        subprocess.Popen(["xdg-open", str(config_path)])
    except FileNotFoundError:
        print(f"Config file: {config_path}")
