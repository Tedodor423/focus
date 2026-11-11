"""The browser extension: CRX packing, its install record and the policy it adds (§14)."""

import io
import json
import os
import shutil
import struct
import subprocess
import tempfile
import types
import unittest
import zipfile
from unittest import mock

from focus import extension, paths, render

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO, "extension")


def read_varint(data, i):
    shift = value = 0
    while True:
        b = data[i]
        i += 1
        value |= (b & 0x7F) << shift
        shift += 7
        if not b & 0x80:
            return value, i


def fields(data):
    out, i = {}, 0
    while i < len(data):
        tag, i = read_varint(data, i)
        assert tag & 7 == 2
        size, i = read_varint(data, i)
        out[tag >> 3] = data[i:i + size]
        i += size
    return out


def cfg():
    group = lambda gid, block, allow: types.SimpleNamespace(id=gid, url_block=block, url_allow=allow)
    return types.SimpleNamespace(
        groups={"a": group("a", ["a.example"], ["music.a.example"]), "b": group("b", ["b.example"], [])},
        never_block=[],
        browser=types.SimpleNamespace(disable_doh=False, force_google_safesearch=False),
    )


class ExtensionTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="focus-ext-")
        self.addCleanup(shutil.rmtree, self.root, True)
        patcher = mock.patch.dict(os.environ, {"FOCUS_ROOT": self.root})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.dir = os.path.join(self.root, "var/lib/focus/extension")

    def test_not_built_means_no_extension_in_the_policy(self):
        self.assertIsNone(extension.installed())
        policy = json.loads(render.policy_json(cfg(), {"a"}, []))
        self.assertEqual(sorted(policy), ["URLAllowlist", "URLBlocklist"])

    def test_crx_is_a_valid_signed_package(self):
        ext = extension.build(SRC)
        with open(os.path.join(self.dir, "focus-guard.crx"), "rb") as f:
            blob = f.read()
        self.assertEqual(blob[:4], b"Cr24")
        version, size = struct.unpack("<II", blob[4:12])
        self.assertEqual(version, 3)
        header, archive = blob[12:12 + size], blob[12 + size:]
        outer = fields(header)
        proof = fields(outer[2])
        signed = outer[10000]
        self.assertEqual(extension.extension_id(proof[1]), ext)
        self.assertEqual(fields(signed)[1], bytes.fromhex(__import__("hashlib").sha256(proof[1]).hexdigest()[:32]))

        pub = os.path.join(self.root, "pub.der")
        sig = os.path.join(self.root, "sig")
        with open(pub, "wb") as f:
            f.write(proof[1])
        with open(sig, "wb") as f:
            f.write(proof[2])
        message = b"CRX3 SignedData\x00" + struct.pack("<I", len(signed)) + signed + archive
        done = subprocess.run(["openssl", "dgst", "-sha256", "-keyform", "DER", "-verify", pub, "-signature", sig],
                              input=message, capture_output=True)
        self.assertEqual(done.returncode, 0, done.stderr)

        z = zipfile.ZipFile(io.BytesIO(archive))
        self.assertCountEqual(z.namelist(), extension.FILES)
        manifest = json.loads(z.read("manifest.json"))
        self.assertEqual(manifest["update_url"], "file://" + os.path.join(self.dir, "update.xml"))
        self.assertEqual(manifest["incognito"], "split")

    def test_id_is_stable_and_the_key_is_private(self):
        first = extension.build(SRC)
        self.assertEqual(extension.build(SRC), first)
        self.assertEqual(os.stat(os.path.join(self.dir, "key.pem")).st_mode & 0o777, 0o600)
        for name in ("focus-guard.crx", "update.xml", "id"):
            self.assertEqual(os.stat(os.path.join(self.dir, name)).st_mode & 0o777, 0o644)
        with open(os.path.join(self.dir, "update.xml")) as f:
            xml = f.read()
        self.assertIn(f"appid='{first}'", xml)
        self.assertIn("focus-guard.crx", xml)

    def test_a_damaged_build_is_ignored(self):
        extension.build(SRC)
        for bad in (b"", b"not-an-id\n", b"z" * 32 + b"\n", b"\xff\xfe"):
            with open(os.path.join(self.dir, "id"), "wb") as f:
                f.write(bad)
            self.assertIsNone(extension.installed(), bad)
        for name in ("update.xml", "focus-guard.crx"):
            ext = extension.build(SRC)
            os.unlink(os.path.join(self.dir, name))
            self.assertIsNone(extension.installed(), ext)

    def copy_source(self):
        src = os.path.join(self.root, "src")
        shutil.copytree(SRC, src)
        return src

    def test_a_bad_version_is_refused(self):
        src = self.copy_source()
        path = os.path.join(src, "manifest.json")
        with open(path) as f:
            manifest = json.load(f)
        for bad in ("1' codebase='x", "", 1, None):
            manifest["version"] = bad
            with open(path, "w") as f:
                json.dump(manifest, f)
            with self.assertRaises(ValueError, msg=repr(bad)):
                extension.build(src)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "update.xml")))

    def test_a_symlink_in_the_source_is_refused(self):
        src = self.copy_source()
        path = os.path.join(src, "blocked.js")
        os.unlink(path)
        os.symlink("/etc/hostname", path)
        with self.assertRaises(paths.UntrustedFile):
            extension.build(src)

    def test_policy_carries_the_final_lists(self):
        ext = extension.build(SRC)
        policy = json.loads(render.policy_json(cfg(), {"a"}, ["pen.example"]))
        self.assertEqual(policy["ExtensionInstallForcelist"],
                         [f"{ext};file://{os.path.join(self.dir, 'update.xml')}"])
        self.assertEqual(policy["MandatoryExtensionsForIncognitoNavigation"], [ext])
        lists = policy["3rdparty"]["extensions"][ext]
        self.assertEqual(lists["block"], policy["URLBlocklist"])
        self.assertEqual(lists["allow"], policy["URLAllowlist"])
        self.assertIn("a.example", lists["block"])
        self.assertIn(".pen.example", lists["block"])
        self.assertNotIn("b.example", lists["block"])

    def test_unlocking_empties_the_lists(self):
        ext = extension.build(SRC)
        lists = json.loads(render.policy_json(cfg(), set(), []))["3rdparty"]["extensions"][ext]
        self.assertEqual(lists, {"block": [], "allow": []})


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class FilterTest(unittest.TestCase):
    def test_filters(self):
        done = subprocess.run(["node", "--test", os.path.join(SRC, "tests/filters.test.mjs")],
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)


if __name__ == "__main__":
    unittest.main()
