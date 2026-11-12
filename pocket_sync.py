# /// script
# requires-python = ">=3.10"
# dependencies = ["bleak>=0.22"]
# ///
"""Cloud-free downloader for the HeyPocket "Pocket" recorder (PKT01).

Talks to the recorder directly over Bluetooth LE using the ASCII protocol
documented by the pocket-libre project (github.com/shahcolate/pocket-libre,
PROTOCOL.md). Recordings are plain MP3 (16 kHz mono, 32 kbps).

Usage (uv installs bleak automatically):
    uv run pocket_sync.py setkey            # store your 16-char session key
    uv run pocket_sync.py scan              # find the recorder
    uv run pocket_sync.py info              # battery, firmware, storage
    uv run pocket_sync.py list              # recordings on the device
    uv run pocket_sync.py pull              # download new recordings
    uv run pocket_sync.py pull --usb        # same, over a USB-C cable (~27x faster)
    uv run pocket_sync.py usb on|off        # expose storage as a USB drive

The Pocket accepts one BLE connection at a time: close the Pocket phone app
(or turn off the phone's Bluetooth) before running this.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from bleak import BleakClient, BleakScanner

CMD_CHAR = "001120a3-2233-4455-6677-889912345678"    # write APP&..., notify MCU&...
AUDIO_CHAR = "001120a1-2233-4455-6677-889912345678"  # notify: MP3 bytes
NAME_PREFIX = "PKT01"
BYTES_PER_SECOND = 4000

CONFIG_PATH = Path.home() / ".config" / "pocket-sync" / "config.json"
DEFAULT_OUT = Path.home() / "PocketRecordings"
SAFE = re.compile(r"^[A-Za-z0-9_-]+$")


# ── config ──────────────────────────────────────────────────────────

def load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text())
    except FileNotFoundError:
        return {}


def save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f, indent=2)
    tmp.replace(CONFIG_PATH)


def session_key(cfg: dict) -> str:
    key = os.environ.get("POCKET_SESSION_KEY") or cfg.get("session_key")
    if not key:
        sys.exit("No session key. Run: uv run pocket_sync.py setkey")
    return key


# ── device ──────────────────────────────────────────────────────────

@dataclass
class Recording:
    date: str
    name: str
    duration_s: int

    @property
    def rel_path(self) -> Path:
        return Path(self.date) / f"{self.name}.mp3"


async def find_device(cfg: dict, wait: float = 90.0):
    """Find the recorder by saved address or name prefix, scanning up to `wait` s.

    The Pocket sometimes stops advertising for a while (power saving), so keep
    scanning rather than giving up after one short pass.
    """
    addr = cfg.get("address")

    def is_pocket(d, ad):
        return d.address == addr or (d.name or ad.local_name or "").startswith(NAME_PREFIX)

    dev = await BleakScanner.find_device_by_filter(is_pocket, timeout=10.0)
    if dev is None:
        print(f"Waiting up to {wait:.0f} s for the Pocket to appear "
              "(pressing its button may help)...", flush=True)
        dev = await BleakScanner.find_device_by_filter(is_pocket, timeout=wait - 10.0)
    if dev is None:
        sys.exit(
            "Pocket not found. Wake it (press the button), keep it near the Mac, "
            "and make sure the Pocket phone app isn't connected to it."
        )
    if cfg.get("address") != dev.address:
        cfg["address"] = dev.address
        save_config(cfg)
    return dev


class Pocket:
    def __init__(self, device):
        self.device = device
        self.client: BleakClient | None = None
        self.messages: list[str] = []
        self._msg_event = asyncio.Event()
        self.audio = bytearray()
        self._audio_event = asyncio.Event()
        self.disconnected = False

    async def __aenter__(self):
        self.client = BleakClient(
            self.device, timeout=20.0, disconnected_callback=self._on_disconnect
        )
        await self.client.connect()
        await self.client.start_notify(CMD_CHAR, self._on_msg)
        return self

    async def __aexit__(self, *exc):
        if self.client and self.client.is_connected:
            await self.client.disconnect()

    def _on_disconnect(self, _client):
        self.disconnected = True
        self._msg_event.set()
        self._audio_event.set()

    def _on_msg(self, _sender, data: bytearray):
        text = data.decode("ascii", errors="replace").replace("\0", "")
        # One notification can hold several messages back to back.
        for part in re.split(r"(?=MCU&)", text):
            part = part.strip()
            if part:
                self.messages.append(part)
        self._msg_event.set()

    def _on_audio(self, _sender, data: bytearray):
        self.audio.extend(data)
        self._audio_event.set()

    async def send(self, cmd: str) -> int:
        mark = len(self.messages)
        await self.client.write_gatt_char(CMD_CHAR, f"APP&{cmd}".encode(), response=False)
        return mark

    async def collect(self, mark: int, done, timeout: float = 6.0, idle: float = 1.5) -> list[str]:
        """Messages after `mark` until done(msgs) is true, `timeout` passes, or
        nothing new arrives for `idle` seconds (once something has arrived)."""
        deadline = time.monotonic() + timeout
        while True:
            got = self.messages[mark:]
            if done(got) or self.disconnected:
                return got
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return got
            self._msg_event.clear()
            try:
                await asyncio.wait_for(
                    self._msg_event.wait(), min(remaining, idle) if got else remaining
                )
            except asyncio.TimeoutError:
                if got:
                    return got

    async def ask(self, cmd: str, reply: str, timeout: float = 5.0) -> str | None:
        """Send APP&cmd, return the value of the first MCU&reply[&value]."""
        prefix = f"MCU&{reply}"

        def match(msgs):
            for m in msgs:
                if m == prefix:
                    return ""
                if m.startswith(prefix + "&"):
                    return m[len(prefix) + 1:]
            return None

        mark = await self.send(cmd)
        got = await self.collect(mark, lambda ms: match(ms) is not None, timeout, idle=timeout)
        return match(got)

    async def login(self, key: str) -> None:
        result = await self.ask(f"SK&{key}", "SK", timeout=6.0)
        if result == "OK":
            return
        if result == "ERR":
            sys.exit("The Pocket rejected the session key (MCU&SK&ERR).")
        sys.exit("No answer to the session key. Try again (press the Pocket's button first).")

    async def list_dirs(self) -> list[str]:
        mark = await self.send("LIST_DIRS")
        got = await self.collect(mark, lambda ms: any(m.startswith("MCU&DIRS_SUM") for m in ms), 10)
        return [m.split("&", 2)[2] for m in got
                if m.startswith("MCU&DIRS&") and SAFE.match(m.split("&", 2)[2])]

    async def list_date(self, date: str, tries: int = 3) -> list[Recording]:
        for attempt in range(tries):
            if attempt:
                await asyncio.sleep(1.0)
            mark = await self.send(f"LIST&{date}")
            got = await self.collect(mark, lambda ms: any(m.startswith("MCU&LIST&") for m in ms), 15)
            recs, entries, count = [], 0, None
            for m in got:
                parts = m.split("&")
                if m.startswith("MCU&LIST&"):
                    count = int(parts[2]) if parts[2].isdigit() else None
                elif m.startswith("MCU&F&") and len(parts) >= 5:
                    entries += 1
                    if SAFE.match(parts[2]) and SAFE.match(parts[3]):
                        dur = int(parts[4]) if parts[4].isdigit() else 0
                        recs.append(Recording(parts[2], parts[3], dur))
            if count == entries:
                return recs
        raise RuntimeError(f"Incomplete listing for {date} after {tries} tries")

    async def list_all(self) -> list[Recording]:
        recs = []
        for d in await self.list_dirs():
            recs.extend(await self.list_date(d))
            await asyncio.sleep(0.2)
        return sorted(recs, key=lambda r: (r.date, r.name))

    async def download(self, rec: Recording, progress) -> bytes:
        self.audio.clear()
        await self.client.start_notify(AUDIO_CHAR, self._on_audio)
        start_mark = len(self.messages)
        try:
            size_s = await self.ask(f"U&{rec.date}&{rec.name}", "U", timeout=10)
            if not size_s or not size_s.isdigit():
                raise RuntimeError(f"Device did not start the transfer (got {size_s!r})")
            size = int(size_s)
            last_growth = time.monotonic()
            last_len = 0
            while len(self.audio) < size:
                if self.disconnected:
                    raise RuntimeError("Disconnected during transfer")
                self._audio_event.clear()
                try:
                    await asyncio.wait_for(self._audio_event.wait(), 2.0)
                except asyncio.TimeoutError:
                    pass
                if len(self.audio) != last_len:
                    last_len = len(self.audio)
                    last_growth = time.monotonic()
                    progress(last_len, size)
                elif time.monotonic() - last_growth > 20:
                    since = [m for m in self.messages[start_mark:] if m != "MCU&WPING"]
                    raise RuntimeError(
                        f"Transfer stalled at {last_len:,}/{size:,} bytes; "
                        f"device said {since[-6:]}, connected={self.client.is_connected}")
            data = bytes(self.audio[:size])
            if len(data) != size:
                raise RuntimeError(f"Got {len(data):,} of {size:,} bytes")
            # Let MCU&OFF arrive before the next command.
            mark = len(self.messages)
            await self.collect(mark, lambda ms: "MCU&OFF" in ms, timeout=3, idle=3)
            return data
        finally:
            if not self.disconnected:
                try:
                    await self.client.stop_notify(AUDIO_CHAR)
                except Exception:
                    pass


# ── commands ────────────────────────────────────────────────────────

def manifest_path(out: Path) -> Path:
    return out / ".pocket-sync.json"


def load_manifest(out: Path) -> dict:
    try:
        return json.loads(manifest_path(out).read_text())
    except FileNotFoundError:
        return {}


def save_manifest(out: Path, manifest: dict) -> None:
    tmp = manifest_path(out).with_suffix(".tmp")
    tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True))
    tmp.replace(manifest_path(out))


def fmt_dur(s: int) -> str:
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}"


async def cmd_scan(cfg, args):
    print("Scanning for 10 s...")
    found = await BleakScanner.discover(timeout=10.0, return_adv=True)
    hits = [(d, ad) for d, ad in found.values()
            if (d.name or ad.local_name or "").startswith(NAME_PREFIX)]
    if not hits:
        print("No Pocket found. Is the phone app connected to it?")
    for d, ad in hits:
        print(f"{d.name or ad.local_name}  address={d.address}  rssi={ad.rssi}")
    if len(hits) == 1:
        cfg["address"] = hits[0][0].address
        save_config(cfg)
        print("Saved this address to the config.")


def cmd_setkey(cfg, args):
    key = args.key or getpass.getpass("16-character session key: ").strip()
    if not re.fullmatch(r"[A-Za-z0-9]{16}", key):
        sys.exit("Expected 16 letters/digits.")
    cfg["session_key"] = key
    save_config(cfg)
    print(f"Saved to {CONFIG_PATH} (mode 600).")


async def connected(cfg):
    dev = await find_device(cfg)
    pocket = Pocket(dev)
    await pocket.__aenter__()
    try:
        await pocket.login(session_key(cfg))
    except BaseException:
        await pocket.__aexit__(None, None, None)
        raise
    return pocket


async def cmd_info(cfg, args):
    p = await connected(cfg)
    try:
        bat = await p.ask("BAT", "BAT")
        fw = await p.ask("FW", "FW")
        wf = await p.ask("WF", "WF")
        spa = await p.ask("SPACE", "SPA")
        usb = await p.ask("GET&USB", "USB")
        print(f"Device:    {p.device.name}")
        print(f"Battery:   {bat}%")
        print(f"Firmware:  {fw} (WiFi {wf})")
        if spa and "&" in spa:
            free, total = (int(x) for x in spa.split("&")[:2])
            print(f"Storage:   {free / 1024:.1f} GB free of {total / 1024:.1f} GB")
        print(f"USB drive: {'on' if usb == '1' else 'off'}")
    finally:
        await p.__aexit__(None, None, None)


def save_device_index(out: Path, rel_paths: list[str]) -> None:
    """Remember which recordings were on the recorder at the last listing."""
    out.mkdir(parents=True, exist_ok=True)
    tmp = out / ".device.tmp"
    tmp.write_text(json.dumps({"updated": time.strftime("%Y-%m-%dT%H:%M:%S"),
                               "recordings": sorted(rel_paths)}, indent=1))
    tmp.replace(out / ".device.json")


async def cmd_list(cfg, args):
    out = Path(args.out).expanduser()
    manifest = load_manifest(out)
    p = await connected(cfg)
    try:
        recs = await p.list_all()
    finally:
        await p.__aexit__(None, None, None)
    save_device_index(out, [str(r.rel_path) for r in recs])
    total = 0
    for r in recs:
        entry = manifest.get(str(r.rel_path), {})
        mark = "x" if entry.get("deleted_locally") else "✓" if entry else " "
        print(f"{mark} {r.date}  {r.name}  {fmt_dur(r.duration_s)}")
        total += r.duration_s
    print(f"{len(recs)} recordings, {fmt_dur(total)} total "
          f"(~{total * BYTES_PER_SECOND / 1e6:.0f} MB). ✓ = already downloaded")


async def cmd_pull(cfg, args):
    if args.usb:
        return await cmd_pull_usb(cfg, args)
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest(out)
    p = await connected(cfg)
    try:
        recs = await p.list_all()
        save_device_index(out, [str(r.rel_path) for r in recs])
        todo = [r for r in recs
                if not manifest.get(str(r.rel_path), {}).get("deleted_locally")
                and (str(r.rel_path) not in manifest or not (out / r.rel_path).exists())]
        if args.since:
            todo = [r for r in todo if r.date >= args.since]
        if args.limit:
            todo = todo[-args.limit:]
        if not todo:
            print(f"Nothing new ({len(recs)} recordings on the device).")
            return
        est = sum(r.duration_s for r in todo) * BYTES_PER_SECOND
        print(f"Downloading {len(todo)} recording(s), ~{est / 1e6:.1f} MB, into {out}")
        failed = 0
        for i, r in enumerate(todo, 1):
            label = f"[{i}/{len(todo)}] {r.date} {r.name} ({fmt_dur(r.duration_s)})"
            data = None
            for attempt in range(1, args.retries + 1):
                start = time.monotonic()

                def progress(n, size, label=label, start=start):
                    rate = n / max(time.monotonic() - start, 0.1) / 1024
                    print(f"\r{label}  {n * 100 // size:3d}%  {rate:5.1f} KB/s",
                          end="", flush=True)

                try:
                    data = await p.download(r, progress)
                    break
                except RuntimeError as e:
                    print(f"\r{label}  attempt {attempt}/{args.retries} failed: {e}")
                    # Stalls are intermittent; a fresh connection clears them.
                    await p.__aexit__(None, None, None)
                    await asyncio.sleep(3)
                    p = await connected(cfg)
            if data is None:
                failed += 1
                continue
            dest = out / r.rel_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(".part")
            tmp.write_bytes(data)
            tmp.replace(dest)
            manifest[str(r.rel_path)] = {"bytes": len(data), "duration_s": r.duration_s,
                                         "downloaded": time.strftime("%Y-%m-%dT%H:%M:%S")}
            save_manifest(out, manifest)
            print(f"\r{label}  done, {len(data):,} bytes in {time.monotonic() - start:.0f}s      ")
        if failed:
            print(f"{failed} recording(s) failed; run pull again to retry them.")
    finally:
        await p.__aexit__(None, None, None)


async def cmd_delete(cfg, args):
    """Delete recordings from the recorder (not from this Mac). Each argument
    is DATE/NAME, e.g. 2026-11-12/20261112222312."""
    out = Path(args.out).expanduser()
    targets = []
    for ref in args.recordings:
        date, _, name = ref.removesuffix(".mp3").partition("/")
        if not (SAFE.match(date) and SAFE.match(name)):
            sys.exit(f"Bad recording reference: {ref} (expected DATE/NAME)")
        targets.append((date, name))
    p = await connected(cfg)
    failed = 0
    try:
        for date, name in targets:
            await p.ask(f"D&{date}&{name}", "D", timeout=8)
            remaining = await p.list_date(date)
            if any(r.name == name for r in remaining):
                failed += 1
                print(f"{date}/{name}: still on the recorder", flush=True)
            else:
                print(f"{date}/{name}: deleted from the recorder", flush=True)
            await asyncio.sleep(0.3)
        recs = await p.list_all()
    finally:
        await p.__aexit__(None, None, None)
    save_device_index(out, [str(r.rel_path) for r in recs])
    if failed:
        sys.exit(f"{failed} recording(s) could not be deleted.")


async def set_usb(cfg, on: bool) -> str | None:
    """Send APP&USB&1/0 over BLE; returns the device's answer ('1'/'0') or None."""
    p = await connected(cfg)
    try:
        return await p.ask(f"USB&{'1' if on else '0'}", "USB")
    finally:
        await p.__aexit__(None, None, None)


async def cmd_usb(cfg, args):
    got = await set_usb(cfg, args.state == "on")
    print(f"USB mass storage: {'on' if got == '1' else 'off' if got == '0' else 'no answer'}")
    if args.state == "on" and got == "1":
        print("Plug the Pocket into the Mac with a data USB-C cable; "
              "it should mount as a drive with a RECORD folder.")


# ── USB transfer ───────────────────────────────────────────────────

def pocket_volume() -> Path | None:
    """A mounted volume that is a Pocket recorder (RECORD/device.txt says PKT01...)."""
    for vol in Path("/Volumes").iterdir():
        try:
            if (vol / "RECORD" / "device.txt").read_text().startswith(NAME_PREFIX):
                return vol
        except OSError:
            continue
    return None


def disk_info(target: str) -> dict:
    out = subprocess.run(["diskutil", "info", "-plist", target],
                         capture_output=True, check=True).stdout
    return plistlib.loads(out)


def remount_read_only(vol: Path) -> Path:
    """macOS mounts the drive read-write on its own; remount it read-only so
    nothing more is written to the recorder's storage while we read it."""
    info = disk_info(str(vol))
    if not info.get("WritableVolume", True):
        return vol
    dev = info["DeviceIdentifier"]
    subprocess.run(["diskutil", "unmount", dev], capture_output=True, check=True)
    subprocess.run(["diskutil", "mount", "readOnly", dev], capture_output=True, check=True)
    return Path(disk_info(dev)["MountPoint"])


def eject(vol: Path) -> None:
    whole = disk_info(str(vol))["ParentWholeDisk"]
    subprocess.run(["diskutil", "eject", whole], capture_output=True, check=True)


async def wait_for(check, timeout: float, interval: float = 1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        await asyncio.sleep(interval)
    return None


async def cmd_pull_usb(cfg, args):
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest(out)

    vol = pocket_volume()
    turned_on = False
    if vol is None:
        print("Switching the Pocket to USB drive mode over Bluetooth...")
        if await set_usb(cfg, True) != "1":
            sys.exit("The Pocket did not confirm USB mode.")
        turned_on = True
        print("Plug the Pocket into this Mac with a data USB-C cable (waiting up to 5 min)...")
        vol = await wait_for(pocket_volume, 300)
        if vol is None:
            await set_usb(cfg, False)
            sys.exit("No Pocket drive appeared; USB mode switched back off.")

    try:
        vol = remount_read_only(vol)
        rec_dir = vol / "RECORD"
        found = []
        for f in sorted(rec_dir.glob("*/*.mp3")):
            date, name = f.parent.name, f.stem
            if SAFE.match(date) and SAFE.match(name) and not name.startswith("."):
                found.append((f, Path(date) / f.name))
        save_device_index(out, [str(rel) for _, rel in found])
        if args.since:
            found = [(f, rel) for f, rel in found if rel.parts[0] >= args.since]
        todo = [(f, rel) for f, rel in found
                if not manifest.get(str(rel), {}).get("deleted_locally")
                and (not (out / rel).exists() or (out / rel).stat().st_size != f.stat().st_size)]
        if args.limit:
            todo = todo[-args.limit:]
        if not todo:
            print(f"Nothing new ({len(found)} recordings on the device).")
        else:
            total = sum(f.stat().st_size for f, _ in todo)
            print(f"Copying {len(todo)} recording(s), {total / 1e6:.1f} MB, into {out}")
            t0 = time.monotonic()
            for f, rel in todo:
                dest = out / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                tmp = dest.with_suffix(".part")
                shutil.copyfile(f, tmp)
                size = f.stat().st_size
                if tmp.stat().st_size != size:
                    tmp.unlink()
                    print(f"  {rel}: size mismatch, skipped")
                    continue
                tmp.replace(dest)
                manifest[str(rel)] = {"bytes": size, "duration_s": size // BYTES_PER_SECOND,
                                      "downloaded": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                      "via": "usb"}
                save_manifest(out, manifest)
                print(f"  {rel}  {size:,} bytes")
            dt = time.monotonic() - t0
            print(f"Done in {dt:.1f}s ({total / 1e6 / max(dt, 0.01):.1f} MB/s).")
    finally:
        try:
            eject(vol)
            print("Ejected the Pocket drive.")
        except Exception as e:
            print(f"Could not eject the drive ({e}); eject it in Finder before unplugging.")

    # The recorder can't use its storage while USB mode is on and the cable
    # is in, so always switch it back off. Switching off can make the drive
    # reappear briefly; eject it again if so.
    print("Switching USB drive mode off over Bluetooth...")
    got = None
    try:
        got = await set_usb(cfg, False)
    except SystemExit as e:
        print(e)
    if got != "0":
        print("Could not confirm USB mode is off; run `uv run pocket_sync.py usb off`.")
    again = await wait_for(pocket_volume, 8)
    if again:
        try:
            eject(again)
        except Exception:
            pass
    print("Unplug the cable now. The Pocket can't see its recordings while it's plugged in.")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scan", help="find the recorder and save its address")
    k = sub.add_parser("setkey", help="store the 16-character session key")
    k.add_argument("key", nargs="?")
    sub.add_parser("info", help="battery, firmware, storage")
    for name in ("list", "pull"):
        s = sub.add_parser(name)
        s.add_argument("--out", default=str(DEFAULT_OUT), help=f"default {DEFAULT_OUT}")
        if name == "pull":
            s.add_argument("--since", help="only dates >= YYYY-MM-DD")
            s.add_argument("--limit", type=int, help="only the newest N new recordings")
            s.add_argument("--retries", type=int, default=4, help="attempts per recording")
            s.add_argument("--usb", action="store_true",
                           help="copy over a USB-C cable instead (much faster)")
    d = sub.add_parser("delete", help="delete recordings from the recorder (DATE/NAME ...)")
    d.add_argument("recordings", nargs="+")
    d.add_argument("--out", default=str(DEFAULT_OUT), help=f"default {DEFAULT_OUT}")
    u = sub.add_parser("usb", help="turn USB mass-storage mode on/off")
    u.add_argument("state", choices=["on", "off"])
    args = ap.parse_args()
    cfg = load_config()

    if args.cmd == "setkey":
        return cmd_setkey(cfg, args)
    fn = {"scan": cmd_scan, "info": cmd_info, "list": cmd_list,
          "pull": cmd_pull, "usb": cmd_usb, "delete": cmd_delete}[args.cmd]
    try:
        asyncio.run(fn(cfg, args))
    except KeyboardInterrupt:
        print("\nInterrupted.")


if __name__ == "__main__":
    main()
