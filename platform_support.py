"""The parts of pocket-sync that differ between macOS and Windows.

Imported by pocket_sync.py, transcribe.py and app.py. Standard library only at
import time; numpy and PyAV are imported by the functions that need them.

  decode_audio      afconvert on macOS, PyAV (bundled ffmpeg) elsewhere
  find_drive        /Volumes on macOS, drive letters on Windows
  prepare_drive     macOS remounts read-only; Windows: nothing (never written)
  eject_drive       diskutil on macOS, DeviceIoControl / Explorer on Windows
  KeepAwake         caffeinate on macOS, SetThreadExecutionState on Windows
  spawn_job/kill_job  process group (POSIX) or process tree (Windows)
  trash             ~/.Trash folder on macOS, Recycle Bin (send2trash) on Windows
"""

from __future__ import annotations

import os
import plistlib
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import wave
from pathlib import Path

IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform == "win32"
COMPUTER = "Mac" if IS_MAC else "PC"  # for messages

# Child processes: no console window flashing up when the app runs windowless.
NO_WINDOW = subprocess.CREATE_NO_WINDOW if IS_WIN else 0
# Children print ✓ and accented names; without this they crash on cp1252 pipes.
CHILD_ENV = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1",
             "PYTHONIOENCODING": "utf-8"}

LMS = Path.home() / ".lmstudio" / "bin" / ("lms.exe" if IS_WIN else "lms")


def find_uv() -> str:
    found = shutil.which("uv")
    if found:
        return found
    if IS_WIN:
        local = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
        for p in [Path.home() / ".local/bin/uv.exe", local / "Microsoft/WinGet/Links/uv.exe",
                  *sorted(local.glob("Microsoft/WinGet/Packages/astral-sh.uv_*/uv.exe"))]:
            if p.exists():
                return str(p)
        return "uv"
    return "/opt/homebrew/bin/uv"


# ── audio ───────────────────────────────────────────────────────────

def decode_audio(path: Path, sr: int):
    """Mono int16 samples at `sr` Hz, as a numpy array."""
    import numpy as np
    if IS_MAC:
        with tempfile.TemporaryDirectory() as td:
            wav = Path(td) / "a.wav"
            subprocess.run(["afconvert", "-f", "WAVE", "-d", f"LEI16@{sr}", "-c", "1",
                            str(path), str(wav)], check=True, capture_output=True)
            with wave.open(str(wav)) as w:
                return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    import av
    chunks = []
    with av.open(str(path)) as container:
        stream = container.streams.audio[0]
        resampler = av.AudioResampler(format="s16", layout="mono", rate=sr)
        for frame in container.decode(stream):
            for out in resampler.resample(frame):
                chunks.append(out.to_ndarray().reshape(-1))
        for out in resampler.resample(None):  # flush
            chunks.append(out.to_ndarray().reshape(-1))
    return np.concatenate(chunks).astype(np.int16) if chunks else np.zeros(0, np.int16)


# ── USB drive ──────────────────────────────────────────────────────

def _candidate_roots() -> list[Path]:
    if IS_MAC:
        return list(Path("/Volumes").iterdir())
    if IS_WIN:
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.SetErrorMode(1)  # SEM_FAILCRITICALERRORS: no "insert a disk" dialogs
        mask = k32.GetLogicalDrives()
        system = os.environ.get("SystemDrive", "C:").upper()
        roots = []
        for i in range(26):
            letter = f"{chr(65 + i)}:"
            # 2 = removable, 3 = fixed (some USB storage reports as fixed)
            if mask >> i & 1 and letter != system and k32.GetDriveTypeW(letter + "\\") in (2, 3):
                roots.append(Path(letter + "\\"))
        return roots
    return [p for base in (Path("/media") / os.environ.get("USER", ""), Path("/mnt"))
            if base.is_dir() for p in base.iterdir()]


def find_drive(prefix: str) -> Path | None:
    """A mounted drive whose RECORD/device.txt starts with `prefix`."""
    for root in _candidate_roots():
        try:
            if (root / "RECORD" / "device.txt").read_text(encoding="utf-8", errors="replace") \
                    .startswith(prefix):
                return root
        except OSError:
            continue
    return None


def _disk_info(target: str) -> dict:
    out = subprocess.run(["diskutil", "info", "-plist", target],
                         capture_output=True, check=True).stdout
    return plistlib.loads(out)


def prepare_drive(vol: Path) -> Path:
    """Make sure nothing is written to the recorder's storage while reading it.
    macOS mounts it read-write on its own, so remount it read-only. Windows has
    no simple read-only remount; this program just never writes to it."""
    if not IS_MAC:
        return vol
    info = _disk_info(str(vol))
    if not info.get("WritableVolume", True):
        return vol
    dev = info["DeviceIdentifier"]
    subprocess.run(["diskutil", "unmount", dev], capture_output=True, check=True)
    subprocess.run(["diskutil", "mount", "readOnly", dev], capture_output=True, check=True)
    return Path(_disk_info(dev)["MountPoint"])


def eject_drive(vol: Path) -> None:
    if IS_MAC:
        whole = _disk_info(str(vol))["ParentWholeDisk"]
        subprocess.run(["diskutil", "eject", whole], capture_output=True, check=True)
        return
    if IS_WIN:
        letter = str(vol)[:2]
        try:
            _win_eject(letter)
        except OSError:
            # Explorer's "Eject" verb, as a fallback (no error reporting).
            ps = (f"(New-Object -ComObject Shell.Application).Namespace(17)"
                  f".ParseName('{letter}').InvokeVerb('Eject')")
            subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, timeout=30, creationflags=NO_WINDOW)
        if Path(letter + "\\").exists():
            raise OSError(f"{letter} is still mounted")
        return
    subprocess.run(["umount", str(vol)], capture_output=True, check=True)


def _win_eject(letter: str) -> None:
    """Flush, lock, dismount and eject a volume (the classic DeviceIoControl
    sequence). Raises OSError if Windows refuses, e.g. a file is still open."""
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    k32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID,
                                    wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                                    ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    GENERIC_READ, GENERIC_WRITE = 0x80000000, 0x40000000
    FILE_SHARE_RW, OPEN_EXISTING = 0x3, 3
    FSCTL_LOCK_VOLUME, FSCTL_DISMOUNT_VOLUME = 0x90018, 0x90020
    IOCTL_STORAGE_MEDIA_REMOVAL, IOCTL_STORAGE_EJECT_MEDIA = 0x2D4804, 0x2D4808
    h = k32.CreateFileW(f"\\\\.\\{letter}", GENERIC_READ | GENERIC_WRITE, FILE_SHARE_RW,
                        None, OPEN_EXISTING, 0, None)
    if h in (None, wintypes.HANDLE(-1).value):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        n = wintypes.DWORD()

        def ioctl(code, inbuf=None, size=0):
            if not k32.DeviceIoControl(h, code, inbuf, size, None, 0, ctypes.byref(n), None):
                raise ctypes.WinError(ctypes.get_last_error())

        for _ in range(10):  # the lock fails while something has a file open
            try:
                ioctl(FSCTL_LOCK_VOLUME)
                break
            except OSError:
                import time
                time.sleep(0.5)
        else:
            raise OSError("the drive is in use")
        ioctl(FSCTL_DISMOUNT_VOLUME)
        prevent = ctypes.c_bool(False)
        ioctl(IOCTL_STORAGE_MEDIA_REMOVAL, ctypes.byref(prevent), 1)
        ioctl(IOCTL_STORAGE_EJECT_MEDIA)
    finally:
        k32.CloseHandle(h)


# ── keep awake ─────────────────────────────────────────────────────

class KeepAwake:
    """Keep the computer from idle-sleeping while `set(True)`."""

    def __init__(self):
        self.busy = False
        self._proc: subprocess.Popen | None = None
        self._changed = threading.Event()
        self._thread: threading.Thread | None = None

    def set(self, busy: bool) -> None:
        if IS_MAC:
            if busy and (self._proc is None or self._proc.poll() is not None):
                self._proc = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
            elif not busy and self._proc and self._proc.poll() is None:
                self._proc.terminate()
                self._proc = None
            return
        if not IS_WIN or busy == self.busy:
            return
        # SetThreadExecutionState is per thread, so one thread owns it.
        self.busy = busy
        if self._thread is None:
            self._thread = threading.Thread(target=self._win_loop, daemon=True)
            self._thread.start()
        self._changed.set()

    def _win_loop(self) -> None:
        import ctypes
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        while True:
            self._changed.wait()
            self._changed.clear()
            flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if self.busy else 0)
            ctypes.windll.kernel32.SetThreadExecutionState(ctypes.c_uint(flags))

    def stop(self) -> None:
        self.set(False)


# ── child processes ────────────────────────────────────────────────

def spawn_job(cmd: list[str], **kw) -> subprocess.Popen:
    """Start a job in its own process group, so it can be stopped with its children."""
    if IS_WIN:
        kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | NO_WINDOW
    else:
        kw["start_new_session"] = True
    return subprocess.Popen(cmd, **kw)


def kill_job(p: subprocess.Popen) -> None:
    """Stop a job started by spawn_job, and everything it started."""
    if p.poll() is not None:
        return
    if IS_WIN:
        # `uv run` starts python as a child; kill the whole tree.
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)],
                       capture_output=True, creationflags=NO_WINDOW)
        return
    try:
        os.killpg(p.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass


# ── trash ──────────────────────────────────────────────────────────

def trash(files: list[Path], label: str) -> None:
    """Move files to the Trash (macOS: together in a folder named `label`) or
    the Recycle Bin."""
    if IS_MAC:
        dest = Path.home() / ".Trash" / label
        dest.mkdir(parents=True, exist_ok=True)
        for f in files:
            shutil.move(str(f), dest / f.name)
        return
    from send2trash import send2trash
    for f in files:
        send2trash(str(f))
