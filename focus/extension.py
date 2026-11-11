"""Packs the browser extension (extension/) into a signed CRX for forced install.

`python3 -m focus.extension build SRC` runs once per install, as root.  The
signing key is generated on this machine and kept, so the extension id stays
the same across reinstalls.  focusd only reads the result, through `installed()`:
without a build it renders the browser policy with no extension in it.
"""

import hashlib
import io
import json
import os
import re
import struct
import subprocess
import sys
import zipfile

from . import paths

FILES = ("manifest.json", "schema.json", "background.js", "filters.mjs", "blocked.html", "blocked.js")
ID_FILE, KEY_FILE, CRX_FILE, UPDATE_FILE = "id", "key.pem", "focus-guard.crx", "update.xml"
MAX_FILE = 1 << 20


def directory() -> str:
    return paths.p("/var/lib/focus/extension")


def file_url(path: str) -> str:
    return "file://" + path


def installed() -> tuple[str, str] | None:
    """(extension id, update URL) of the build on this machine, or None."""
    try:
        ext = paths.read_trusted(os.path.join(directory(), ID_FILE), 64).decode("ascii").strip()
        if not (len(ext) == 32 and all("a" <= c <= "p" for c in ext)):
            return None
        if not all(os.path.isfile(os.path.join(directory(), f)) for f in (UPDATE_FILE, CRX_FILE)):
            return None
    except (OSError, UnicodeDecodeError, paths.UntrustedFile):
        return None
    return ext, file_url(os.path.join(directory(), UPDATE_FILE))


def extension_id(public_der: bytes) -> str:
    """Chromium's id: the first 16 bytes of SHA-256, each hex digit moved to a-p."""
    digest = hashlib.sha256(public_der).hexdigest()[:32]
    return "".join(chr(ord("a") + int(c, 16)) for c in digest)


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        low = n & 0x7F
        n >>= 7
        out.append(low | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _field(number: int, payload: bytes) -> bytes:
    """A length-delimited protobuf field."""
    return _varint(number << 3 | 2) + _varint(len(payload)) + payload


def _zip(src: str, update_url: str) -> tuple[bytes, str]:
    """The zipped extension with its update URL stamped in, and its version."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name in FILES:
            # Runs as root on files in a user-writable repo.
            data = paths.read_trusted(os.path.join(src, name), MAX_FILE, require_root=False)
            if name == "manifest.json":
                manifest = json.loads(data)
                manifest["update_url"] = update_url
                version = manifest["version"]
                if not (isinstance(version, str) and re.fullmatch(r"\d+(\.\d+){0,3}", version)):
                    raise ValueError(f"bad extension version: {version!r}")
                data = json.dumps(manifest, indent=2).encode()
            z.writestr(zipfile.ZipInfo(name, (2020, 1, 1, 0, 0, 0)), data, zipfile.ZIP_DEFLATED)
    return buf.getvalue(), version


def _openssl(*args: str, data: bytes | None = None) -> bytes:
    return subprocess.run(["openssl", *args], input=data, check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout


def crx(archive: bytes, public_der: bytes, key: str) -> bytes:
    """A CRX3 file: magic, version, length-prefixed signed header, then the zip."""
    crx_id = hashlib.sha256(public_der).digest()[:16]
    signed_data = _field(1, crx_id)
    message = b"CRX3 SignedData\x00" + struct.pack("<I", len(signed_data)) + signed_data + archive
    signature = _openssl("dgst", "-sha256", "-sign", key, data=message)
    proof = _field(1, public_der) + _field(2, signature)
    header = _field(2, proof) + _field(10000, signed_data)
    return b"Cr24" + struct.pack("<II", 3, len(header)) + header + archive


def build(src: str) -> str:
    out = directory()
    paths.ensure_dir(out, 0o755)
    key = os.path.join(out, KEY_FILE)
    if not os.path.exists(key):
        pem = _openssl("genrsa", "2048")
        paths.atomic_write(key, pem, 0o600)
    public_der = _openssl("rsa", "-in", key, "-pubout", "-outform", "DER")
    ext = extension_id(public_der)
    crx_path, update_path = os.path.join(out, CRX_FILE), os.path.join(out, UPDATE_FILE)
    archive, version = _zip(src, file_url(update_path))
    paths.atomic_write(crx_path, crx(archive, public_der, key), 0o644)
    paths.atomic_write(update_path, (
        "<?xml version='1.0' encoding='UTF-8'?>\n"
        "<gupdate xmlns='http://www.google.com/update2/response' protocol='2.0'>\n"
        f"  <app appid='{ext}'>\n"
        f"    <updatecheck codebase='{file_url(crx_path)}' version='{version}' />\n"
        "  </app>\n</gupdate>\n").encode(), 0o644)
    paths.atomic_write(os.path.join(out, ID_FILE), (ext + "\n").encode(), 0o644)
    return ext


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "build":
        sys.exit("usage: python3 -m focus.extension build SRC")
    print(build(sys.argv[2]))
