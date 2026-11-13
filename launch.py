# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Windows launcher for the web app (the Start Menu shortcut that
make_app.ps1 creates runs this with uvw, so there's no console window).

Starts `uv run app.py` in the background if it isn't already running, waits
for it, then opens http://localhost:8765 in the browser. Use the Quit button
in the web page to stop the server. Log: %LOCALAPPDATA%\\Pocket Recordings\\app.log
"""

import os
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from platform_support import CHILD_ENV, IS_WIN, find_uv, spawn_job  # noqa: E402

PORT = os.environ.get("POCKET_APP_PORT", "8765")
URL = f"http://localhost:{PORT}"
LOG = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Pocket Recordings" / "app.log"


def up() -> bool:
    try:
        urllib.request.urlopen(f"{URL}/api/jobs", timeout=1).close()
        return True
    except OSError:
        return False


def alert(msg: str) -> None:
    if IS_WIN:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, msg, "Pocket Recordings", 0x10)
    else:
        print(msg, file=sys.stderr)


def main() -> None:
    if not up():
        LOG.parent.mkdir(parents=True, exist_ok=True)
        log = open(LOG, "a", encoding="utf-8")
        log.write(f"=== {time.strftime('%Y-%m-%d %H:%M:%S')} starting\n")
        log.flush()
        spawn_job([find_uv(), "run", "app.py"], cwd=HERE, stdout=log, stderr=log,
                  stdin=open(os.devnull), env=CHILD_ENV)
        # The first start installs packages, which can take a few minutes.
        deadline = time.monotonic() + 300
        while not up():
            if time.monotonic() > deadline:
                alert(f"The server didn't start. See {LOG}")
                sys.exit(1)
            time.sleep(0.5)
    webbrowser.open(URL)


if __name__ == "__main__":
    main()
