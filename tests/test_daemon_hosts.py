"""focusd as a process: /etc/hosts preservation, refusals, policy files,
restore-on-tamper and list caches."""

import getpass
import hashlib
import json
import os
import signal
import stat
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "fakes"))
import daemon_harness as H  # noqa: E402

BEGIN = "# BEGIN focus — managed by focusd, manual edits are reverted\n".encode()
END = b"# END focus\n"
LOCKED = BEGIN + (b"0.0.0.0 reddit.com\n:: reddit.com\n0.0.0.0 www.reddit.com\n"
                  b":: www.reddit.com\n0.0.0.0 old.reddit.com\n:: old.reddit.com\n"
                  b"0.0.0.0 poki.com\n:: poki.com\n") + END


class HostsTests(unittest.TestCase):
    def daemon(self, **kw):
        d = H.Daemon(**kw)
        self.addCleanup(d.cleanup)
        return d

    def test_block_appended_rest_byte_for_byte(self):
        original = b"127.0.0.1 localhost\r\n#   odd   spacing\t\n\n::1 localhost"
        d = self.daemon(hosts=original).start()
        self.assertEqual(d.hosts(), original + b"\n" + LOCKED)
        self.assertEqual(stat.S_IMODE(os.stat(d.path("/etc/hosts")).st_mode), 0o644)
        d.stop()
        # Restart: the block is replaced in place, never duplicated, and the
        # first write after start (none needed here) is no restore.
        d.start()
        self.assertEqual(d.hosts(), original + b"\n" + LOCKED)
        self.assertEqual(d.events("restore"), [])

    def test_stale_block_from_previous_run_is_not_a_restore(self):
        stale = H.HOSTS + BEGIN + b"0.0.0.0 old.example\n" + END + b"192.168.1.1 router\n"
        d = self.daemon(hosts=stale).start()
        self.assertEqual(d.hosts(), H.HOSTS + b"192.168.1.1 router\n" + LOCKED)
        self.assertEqual(d.events("restore"), [])

    def test_refuses_two_begins(self):
        bad = H.HOSTS + BEGIN + END + BEGIN + END
        d = self.daemon(hosts=bad).start()
        self.assertEqual(d.hosts(), bad)
        self.assertIn("more than one", d.wait_event("hosts_error")[0]["error"])
        # Policy files are still written.
        self.assertIn("reddit.com", d.policy()["URLBlocklist"])

    def test_refuses_begin_without_end(self):
        bad = H.HOSTS + BEGIN + b"0.0.0.0 x.com\n"
        d = self.daemon(hosts=bad).start()
        self.assertEqual(d.hosts(), bad)
        self.assertIn("without", d.wait_event("hosts_error")[0]["error"])
        d.send(signal.SIGHUP)                 # still refused, error not repeated
        d.wait_event("config_reload")
        self.assertEqual(len(d.events("hosts_error")), 1)

    def test_restore_on_tamper(self):
        d = self.daemon().start()
        tampered = d.hosts().replace(b"0.0.0.0 reddit.com\n", b"")
        with open(d.path("/etc/hosts"), "wb") as f:
            f.write(tampered)
        with open(d.path("/etc/chromium/policies/managed/focus.json"), "w") as f:
            f.write("{}")
        d.send(signal.SIGHUP)
        restores = d.wait_for(lambda: len(d.events("restore")) >= 2 and d.events("restore"))
        files = {e["file"] for e in restores}
        self.assertEqual(files, {d.path("/etc/hosts"),
                                 d.path("/etc/chromium/policies/managed/focus.json")})
        self.assertEqual(d.hosts(), H.HOSTS + LOCKED)
        self.assertIn("reddit.com", d.policy()["URLBlocklist"])

    def test_edit_outside_block_is_kept_and_not_a_restore(self):
        d = self.daemon(reconcile=1).start()
        with open(d.path("/etc/hosts"), "wb") as f:
            f.write(b"10.9.9.9 added\n" + H.HOSTS + LOCKED)
        time.sleep(1.6)
        self.assertEqual(d.hosts(), b"10.9.9.9 added\n" + H.HOSTS + LOCKED)
        self.assertEqual(d.events("restore"), [])

    def test_policy_files(self):
        d = self.daemon().start()
        expected = {"URLBlocklist": ["youtube.com", "youtu.be", "reddit.com"],
                    "URLAllowlist": ["music.youtube.com", "youtube.com/embed"],
                    "DnsOverHttpsMode": "off", "ForceGoogleSafeSearch": True}
        self.assertEqual(d.policy("chromium"), expected)
        self.assertEqual(d.policy("vivaldi"), expected)     # dir created
        for sub in ("/etc/vivaldi", "/etc/vivaldi/policies", "/etc/vivaldi/policies/managed"):
            self.assertEqual(stat.S_IMODE(os.stat(d.path(sub)).st_mode), 0o755)
        self.assertEqual(os.listdir(d.path("/etc/chromium/policies/managed")), ["focus.json"])

    def test_nothing_locked_gives_empty_block(self):
        d = self.daemon()
        d.write_config('user = "%s"\n[groups.a]\nenabled = false\nhosts = ["a.com"]\n'
                       % getpass.getuser())
        d.start()
        self.assertEqual(d.hosts(), H.HOSTS + BEGIN + END)
        self.assertEqual(d.policy()["URLBlocklist"], [])

    def test_list_cache_used_and_fetch_error_logged(self):
        d = self.daemon()
        os.makedirs(d.path("/var/lib/focus/lists"))
        txt = b"a.porn.example\nb.porn.example\nmaps.google.com\nreddit.com\n"
        with open(d.path("/var/lib/focus/lists/porn.txt"), "wb") as f:
            f.write(txt)
        with open(d.path("/var/lib/focus/lists/porn.meta.json"), "w") as f:
            json.dump({"url": "https://127.0.0.1:9/list.txt", "updated": 1.0, "entries": 4,
                       "sha256": hashlib.sha256(txt).hexdigest()}, f)
        d.start()
        hosts = d.hosts()
        self.assertIn(b"0.0.0.0 a.porn.example\n0.0.0.0 b.porn.example\n", hosts)
        self.assertNotIn(b":: a.porn.example", hosts)        # list entries: 0.0.0.0 only
        self.assertNotIn(b"google", hosts)                    # never_block
        self.assertEqual(hosts.count(b"0.0.0.0 reddit.com\n"), 1)   # dedup
        d.wait_event("list_error", name="porn")              # stale → refetch fails, cache kept
        st = d.request({"op": "status"})
        self.assertEqual(st["lists"]["porn"], {"entries": 4, "updated": 1.0})
        porn = next(g for g in st["groups"] if g["id"] == "porn")
        self.assertEqual(porn["hosts_count"], 3)

    def test_no_cache_contributes_nothing(self):
        d = self.daemon().start()
        st = d.request({"op": "status"})
        self.assertEqual(st["lists"]["porn"], {"entries": 0, "updated": None})
        self.assertNotIn(b"porn", d.hosts())


if __name__ == "__main__":
    unittest.main()
