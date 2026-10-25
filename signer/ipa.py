"""Unpacking and packing IPAs, and the OTA manifests that install them.

Packing is where signing used to spend most of its time: deflating the 30 MB
app binary again for every certificate. Signing changes only the binary's
first pages (load commands) and its tail (the signature), so the unsigned
app's files are deflated once in independent blocks, and each signed copy
reuses every block whose bytes did not change. A raw deflate stream can be
assembled from blocks like that as long as each one starts from a fresh
compressor and all but the last end on a sync flush, which is how they are
made here. Every IPA written is read back and CRC-checked before it is used.
"""
from __future__ import annotations

import os
import plistlib
import stat
import struct
import zipfile
import zlib
from pathlib import Path
from xml.sax.saxutils import escape

BLOCK = 1 << 20          # reuse granularity: costs ~0.2% in size against one stream
MIN_CACHED = 64 * 1024   # smaller files are just deflated
LEVEL = 6                # zip's default, what the IPAs were always packed with


def extract(ipa: Path, dest: Path) -> dict[str, tuple[int, int, int, int, int, int]]:
    """Unpack an IPA keeping file modes (zipfile drops them, and an app whose
    binary is not executable will not launch). Returns each entry's timestamp
    so the signed copies can keep them."""
    dates = {}
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(ipa) as zf:
        for info in zf.infolist():
            name = info.filename
            dates[name.rstrip("/")] = info.date_time
            target = Path(zf.extract(info, dest))
            mode = (info.external_attr >> 16) & 0o7777
            if info.create_system == 3 and stat.S_ISLNK(info.external_attr >> 16):
                link = target.read_text()
                target.unlink()
                os.symlink(link, target)
            elif mode and not info.is_dir():
                os.chmod(target, mode)
    return dates


class DeflateCache:
    """Deflated blocks of the unsigned app's files, keyed by path inside the IPA.
    Every block is kept sync-flushed (to sit mid-stream), and each file's last
    block is also kept finished (to end a stream), so a file that signing did
    not touch is copied without compressing anything."""

    def __init__(self) -> None:
        self._raw: dict[str, bytes] = {}
        self._blocks: dict[str, list[bytes]] = {}
        self._final: dict[str, bytes] = {}

    @staticmethod
    def _deflate(chunk: bytes, last: bool) -> bytes:
        comp = zlib.compressobj(LEVEL, zlib.DEFLATED, -15)
        return comp.compress(chunk) + comp.flush(zlib.Z_FINISH if last else zlib.Z_SYNC_FLUSH)

    def prime(self, root: Path) -> None:
        for path in sorted(root.rglob("*")):
            if path.is_file() and not path.is_symlink() and path.stat().st_size >= MIN_CACHED:
                rel = path.relative_to(root).as_posix()
                data = path.read_bytes()
                starts = range(0, len(data), BLOCK)
                self._blocks[rel] = [self._deflate(data[s:s + BLOCK], False) for s in starts]
                self._final[rel] = self._deflate(data[starts[-1]:], True)
                self._raw[rel] = data

    def deflate(self, rel: str, data: bytes) -> bytes:
        raw, blocks = self._raw.get(rel), self._blocks.get(rel, [])
        count = max(1, -(-len(data) // BLOCK))
        out = []
        for i in range(count):
            start = i * BLOCK
            chunk = data[start:start + BLOCK]
            last = i == count - 1
            same = raw is not None and i < len(blocks) and raw[start:start + BLOCK] == chunk
            if same and not last:
                out.append(blocks[i])
            elif same and i == len(blocks) - 1:
                out.append(self._final[rel])
            else:
                out.append(self._deflate(chunk, last))
        return b"".join(out)


def _dos_time(date_time: tuple[int, ...]) -> tuple[int, int]:
    y, mo, d, h, mi, s = date_time[:6]
    y = min(max(y, 1980), 2107)
    return (h << 11) | (mi << 5) | (s // 2), ((y - 1980) << 9) | (mo << 5) | d


def pack(root: Path, out: Path, *, cache: DeflateCache | None = None,
         dates: dict[str, tuple[int, ...]] | None = None) -> None:
    """Zip everything under root (which holds Payload/) into out, the way
    `zip -ry` would: directories as entries, Unix modes, symlinks kept."""
    dates = dates or {}
    default_date = dates.get("Payload", (2020, 1, 1, 0, 0, 0))
    items: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        base = Path(dirpath)
        if base != root:
            items.append(base)
        items.extend(base / f for f in sorted(filenames))

    central = []
    with open(out, "wb") as fh:
        for path in items:
            rel = path.relative_to(root).as_posix()
            st = os.lstat(path)
            if stat.S_ISDIR(st.st_mode):
                name, data, comp, method = rel + "/", b"", b"", 0
            elif stat.S_ISLNK(st.st_mode):
                name, data, method = rel, os.readlink(path).encode(), 0
                comp = data
            else:
                name, data = rel, path.read_bytes()
                if cache is not None and len(data) >= MIN_CACHED:
                    comp = cache.deflate(rel, data)
                else:
                    c = zlib.compressobj(LEVEL, zlib.DEFLATED, -15)
                    comp = c.compress(data) + c.flush()
                method = 8
                if len(comp) >= len(data):
                    comp, method = data, 0
            crc = zlib.crc32(data) & 0xFFFFFFFF
            dos_time, dos_date = _dos_time(dates.get(rel, default_date))
            raw_name = name.encode("utf-8")
            flags = 0 if name.isascii() else 0x800
            version = 20 if method == 8 else 10
            external = (st.st_mode & 0xFFFF) << 16 | (0x10 if stat.S_ISDIR(st.st_mode) else 0)
            offset = fh.tell()
            fh.write(struct.pack("<IHHHHHIIIHH", 0x04034B50, version, flags, method, dos_time,
                                 dos_date, crc, len(comp), len(data), len(raw_name), 0))
            fh.write(raw_name)
            fh.write(comp)
            central.append(struct.pack("<IHHHHHHIIIHHHHHII", 0x02014B50, (3 << 8) | 20, version,
                                       flags, method, dos_time, dos_date, crc, len(comp), len(data),
                                       len(raw_name), 0, 0, 0, 0, external, offset) + raw_name)
        cd_offset = fh.tell()
        for record in central:
            fh.write(record)
        cd_size = fh.tell() - cd_offset
        fh.write(struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, len(central), len(central),
                             cd_size, cd_offset, 0))
    if cd_offset >= 0xFFFFFFFF:
        raise ValueError("IPA too large for a plain zip")


def verify(ipa: Path) -> None:
    """Read every entry back and check its CRC."""
    with zipfile.ZipFile(ipa) as zf:
        bad = zf.testzip()
        if bad is not None:
            raise ValueError(f"{ipa.name}: entry {bad} fails its CRC check")


def app_info(app: Path) -> dict:
    with open(app / "Info.plist", "rb") as fh:
        info = plistlib.load(fh)
    return {
        "bundle_id": str(info.get("CFBundleIdentifier", "")),
        "title": str(info.get("CFBundleDisplayName") or info.get("CFBundleName") or "SideInstaller"),
        "version": str(info.get("CFBundleShortVersionString", "")),
        "executable": str(info.get("CFBundleExecutable", "")),
    }


def manifest(ipa_url: str, bundle_id: str, version: str, title: str) -> str:
    """The OTA manifest an itms-services:// link points at."""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>items</key>
  <array>
    <dict>
      <key>assets</key>
      <array>
        <dict>
          <key>kind</key>
          <string>software-package</string>
          <key>url</key>
          <string>{escape(ipa_url)}</string>
        </dict>
      </array>
      <key>metadata</key>
      <dict>
        <key>bundle-identifier</key>
        <string>{escape(bundle_id)}</string>
        <key>bundle-version</key>
        <string>{escape(version or "1.0")}</string>
        <key>kind</key>
        <string>software</string>
        <key>title</key>
        <string>{escape(title)}</string>
      </dict>
    </dict>
  </array>
</dict>
</plist>
"""
