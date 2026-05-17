#!/usr/bin/env python

import argparse
import os
import shutil
import subprocess
from pathlib import Path

LOCAL = os.path.expanduser("~/.local")
LOCAL_APPS = os.path.join(LOCAL, "share", "applications")
SOURCE_PATH = Path(__file__).parent

for path in [LOCAL_APPS]:
    os.makedirs(path, exist_ok=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--uninstall", action="store_true")
    args = parser.parse_args()

    desktop_src = SOURCE_PATH / "mind-piano.desktop"
    desktop_dest = os.path.join(LOCAL_APPS, "mind-piano.desktop")

    if args.uninstall:
        if os.path.exists(desktop_dest):
            print("Removing", desktop_dest)
            os.remove(desktop_dest)
        else:
            print("Not installed:", desktop_dest)
    else:
        if os.path.lexists(desktop_dest):
            os.remove(desktop_dest)
        print("Copying %s to %s" % (desktop_src, desktop_dest))
        shutil.copy(desktop_src, desktop_dest)

    subprocess.run(
        ["update-desktop-database", os.path.join(LOCAL, "share", "applications")]
    )


if __name__ == "__main__":
    main()
