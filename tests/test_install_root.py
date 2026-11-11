"""Tests for install.sh / uninstall.sh (builder D), run unprivileged into a FOCUS_ROOT temp prefix.

A fake repo (copies of the real scripts, launchers, config and units plus a stub
`focus` package) is used, so these tests don't depend on the other builders' code.
Its etc/config.toml and etc/judge.md come from the real repo's local copies, or
the tracked examples on a fresh clone.
No systemctl/resolvectl runs under a prefix; the scripts print "would run: ...".
"""

import os
import shutil
import stat
import subprocess
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOSTS = b"127.0.0.1 localhost\n::1 localhost\n# a comment\n10.0.0.5 nas.home  nas\n"
BLOCK = ("# BEGIN focus — managed by focusd, manual edits are reverted\n"
         "0.0.0.0 reddit.com\n:: reddit.com\n# END focus\n").encode()
STUB = "def main(argv=None):\n    return 0\n"


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def local_or_example(local, example):
    """Path in the real repo of a local etc/ file, or its example on a fresh clone."""
    path = os.path.join(REPO, local)
    return path if os.path.isfile(path) else os.path.join(REPO, example)


class RootInstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = os.path.join(self.tmp.name, "repo")
        self.root = os.path.join(self.tmp.name, "root")
        self.make_repo()
        self.make_root()

    # ------------------------------------------------------------ fixtures
    def make_repo(self):
        r = self.repo
        for d in ("bin", "dnslog", "etc", "systemd", "focus"):
            os.makedirs(os.path.join(r, d))
        for f in ("install.sh", "uninstall.sh", "bin/focus", "bin/focusd", "bin/focus-watch",
                  "dnslog/focus-dnslog", "etc/config.example.toml", "etc/judge.example.md",
                  "systemd/focusd.service", "systemd/focus-dnslog.service", "systemd/focus-watch.service"):
            shutil.copy(os.path.join(REPO, f), os.path.join(r, f))
        shutil.copy(local_or_example("etc/config.toml", "etc/config.example.toml"), os.path.join(r, "etc/config.toml"))
        shutil.copy(local_or_example("etc/judge.md", "etc/judge.example.md"), os.path.join(r, "etc/judge.md"))
        for m in ("__init__", "cli", "daemon", "watch", "render"):
            self.write(os.path.join(r, "focus", m + ".py"), STUB)
        for m in ("paths.py", "extension.py"):
            shutil.copy(os.path.join(REPO, "focus", m), os.path.join(r, "focus", m))
        shutil.copytree(os.path.join(REPO, "extension"), os.path.join(r, "extension"))

    def make_root(self):
        os.makedirs(os.path.join(self.root, "etc", "chromium", "policies", "managed"))
        os.makedirs(os.path.join(self.root, "etc", "vivaldi", "policies", "managed"))
        with open(self.p("etc/hosts"), "wb") as f:
            f.write(HOSTS)
        self.write(self.p("etc/chromium/policies/managed/color.json"), '{"BrowserThemeColor": "#123456"}\n')
        self.write(self.p("etc/chromium/policies/managed/omarchy.json"), '{"x": 1}\n')

    @staticmethod
    def write(path, text):
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def p(self, rel):
        return os.path.join(self.root, rel)

    DEFAULT = object()

    def run_script(self, name, *args, root=DEFAULT, extra_env=None):
        env = {k: v for k, v in os.environ.items() if k not in ("FOCUS_ROOT", "DESTDIR")}
        if root is not None:
            env["FOCUS_ROOT"] = self.root if root is self.DEFAULT else root
        env["FOCUS_OLLAMA_URL"] = "http://127.0.0.1:9"  # nothing listens: the warning path
        env.update(extra_env or {})
        return subprocess.run(["bash", os.path.join(self.repo, name), *args], env=env,
                              capture_output=True, text=True, timeout=60, cwd=self.tmp.name)

    def install(self, *args):
        r = self.run_script("install.sh", *args)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r

    # ------------------------------------------------------------ install
    def test_install_layout_and_modes(self):
        r = self.install()
        expect = {
            "usr/local/lib/focus/focus/__init__.py": 0o644,
            "usr/local/lib/focus/focus/daemon.py": 0o644,
            "usr/local/lib/focus/focus-dnslog": 0o755,
            "usr/local/bin/focus": 0o755,
            "usr/local/bin/focus-watch": 0o755,
            "usr/local/sbin/focusd": 0o755,
            "etc/focus/config.toml": 0o644,
            "etc/focus/judge.md": 0o644,
            "etc/systemd/system/focusd.service": 0o644,
            "etc/systemd/system/focus-dnslog.service": 0o644,
            "etc/systemd/user/focus-watch.service": 0o644,
            "var/lib/focus/hosts.orig": 0o644,
        }
        for rel, m in expect.items():
            self.assertTrue(os.path.isfile(self.p(rel)), rel)
            self.assertEqual(mode(self.p(rel)), m, rel)
        for rel, m in {"var/lib/focus": 0o755, "var/lib/focus/lists": 0o755, "var/lib/focus/judge": 0o755,
                       "var/log/focus": 0o755, "var/log/focus/dns": 0o750}.items():
            self.assertTrue(os.path.isdir(self.p(rel)), rel)
            self.assertEqual(mode(self.p(rel)), m, rel)
        with open(self.p("var/lib/focus/hosts.orig"), "rb") as f:
            self.assertEqual(f.read(), HOSTS)
        with open(self.p("etc/hosts"), "rb") as f:
            self.assertEqual(f.read(), HOSTS, "install.sh must never edit /etc/hosts")
        self.assertEqual(sorted(os.listdir(self.p("etc/chromium/policies/managed"))), ["color.json", "omarchy.json"])
        self.assertEqual(os.listdir(self.p("etc/vivaldi/policies/managed")), [])
        self.assertIn("would run: systemctl enable focus-dnslog.service focusd.service", r.stdout)
        self.assertIn("would run: systemctl --global enable focus-watch.service", r.stdout)
        self.assertIn("Ollama", r.stderr)  # unreachable → warning only, still exit 0
        with open(self.p("usr/local/bin/focus")) as f:
            self.assertEqual(f.readline().strip(), "#!/usr/bin/python3 -I")

    def test_destdir_alias(self):
        r = self.run_script("install.sh", root=None, extra_env={"DESTDIR": self.root})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.isfile(self.p("usr/local/sbin/focusd")))

    def test_no_start(self):
        r = self.install("--no-start")
        self.assertNotIn("systemctl enable", r.stdout)
        self.assertNotIn("--global enable", r.stdout)
        self.assertIn("would run: systemctl daemon-reload", r.stdout)

    def test_idempotent_backups_and_stale_modules(self):
        self.install()
        # /etc/hosts changes after the first install (focusd's block): hosts.orig stays the original.
        with open(self.p("etc/hosts"), "ab") as f:
            f.write(BLOCK)
        self.install()
        with open(self.p("var/lib/focus/hosts.orig"), "rb") as f:
            self.assertEqual(f.read(), HOSTS)
        self.assertFalse(os.path.exists(self.p("var/lib/focus/backups")), "unchanged config: no backup")
        # Config change → previous version backed up; removed module → removed from the install.
        with open(os.path.join(self.repo, "etc/config.toml"), "a") as f:
            f.write("\n# changed\n")
        os.remove(os.path.join(self.repo, "focus", "render.py"))
        os.makedirs(self.p("usr/local/lib/focus/focus/__pycache__"))
        self.install()
        backups = os.listdir(self.p("var/lib/focus/backups"))
        self.assertEqual(len(backups), 1)
        self.assertTrue(backups[0].startswith("config.toml."))
        self.assertFalse(os.path.exists(self.p("usr/local/lib/focus/focus/render.py")))
        self.assertFalse(os.path.exists(self.p("usr/local/lib/focus/focus/__pycache__")))
        with open(self.p("etc/focus/config.toml")) as f:
            self.assertIn("# changed", f.read())

    def test_refuses_bad_prefixes(self):
        for bad, msg in (("relative/dir", "absolute"), ("/", "not a test prefix"),
                         (os.path.join(self.tmp.name, "nope"), "does not exist")):
            r = self.run_script("install.sh", root=bad)
            self.assertNotEqual(r.returncode, 0, bad)
            self.assertIn(msg, r.stderr, bad)

    @unittest.skipIf(os.geteuid() == 0, "only meaningful unprivileged")
    def test_refuses_without_root_or_prefix(self):
        env = {k: v for k, v in os.environ.items() if k not in ("FOCUS_ROOT", "DESTDIR")}
        r = subprocess.run(["bash", os.path.join(self.repo, "install.sh")], env=env,
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 1)
        self.assertIn("must run as root", r.stderr)
        r = subprocess.run(["bash", os.path.join(self.repo, "uninstall.sh")], env=env,
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 1)
        self.assertIn("must run as root", r.stderr)

    def test_missing_repo_file_installs_nothing(self):
        os.remove(os.path.join(self.repo, "focus", "watch.py"))
        r = self.run_script("install.sh")
        self.assertEqual(r.returncode, 1)
        self.assertIn("focus/watch.py", r.stderr)
        self.assertFalse(os.path.exists(self.p("usr")))

    def test_syntax_error_installs_nothing(self):
        self.write(os.path.join(self.repo, "focus", "daemon.py"), "def main(:\n")
        r = self.run_script("install.sh")
        self.assertEqual(r.returncode, 1)
        self.assertIn("syntax", r.stderr.lower())
        self.assertFalse(os.path.exists(self.p("usr")))

    def test_bad_config_installs_nothing(self):
        self.write(os.path.join(self.repo, "etc", "config.toml"), "user = [unclosed\n")
        r = self.run_script("install.sh")
        self.assertEqual(r.returncode, 1)
        self.assertFalse(os.path.exists(self.p("usr")))

    # ------------------------------------------------------------ local config vs example
    def read(self, path):
        with open(path, "rb") as f:
            return f.read()

    def test_local_config_wins_over_example(self):
        with open(os.path.join(self.repo, "etc/config.toml"), "a") as f:
            f.write("\n# local marker\n")
        with open(os.path.join(self.repo, "etc/judge.md"), "a") as f:
            f.write("\nlocal marker\n")
        r = self.install()
        self.assertNotIn("using the example", r.stderr)
        self.assertEqual(self.read(self.p("etc/focus/config.toml")), self.read(os.path.join(self.repo, "etc/config.toml")))
        self.assertEqual(self.read(self.p("etc/focus/judge.md")), self.read(os.path.join(self.repo, "etc/judge.md")))

    def test_falls_back_to_example_with_warning(self):
        os.remove(os.path.join(self.repo, "etc/config.toml"))
        os.remove(os.path.join(self.repo, "etc/judge.md"))
        r = self.install()
        self.assertIn("using the example config; copy etc/config.example.toml to etc/config.toml and edit it", r.stderr)
        self.assertIn("using the example judge policy", r.stderr)
        self.assertEqual(self.read(self.p("etc/focus/config.toml")),
                         self.read(os.path.join(self.repo, "etc/config.example.toml")))
        self.assertEqual(self.read(self.p("etc/focus/judge.md")),
                         self.read(os.path.join(self.repo, "etc/judge.example.md")))

    def test_no_config_at_all_installs_nothing(self):
        os.remove(os.path.join(self.repo, "etc/config.toml"))
        os.remove(os.path.join(self.repo, "etc/config.example.toml"))
        r = self.run_script("install.sh")
        self.assertEqual(r.returncode, 1)
        self.assertIn("etc/config.toml", r.stderr)
        self.assertFalse(os.path.exists(self.p("usr")))

    # ------------------------------------------------------------ uninstall
    def simulate_daemon(self):
        with open(self.p("etc/hosts"), "ab") as f:
            f.write(BLOCK)
        for d in ("etc/chromium/policies/managed", "etc/vivaldi/policies/managed"):
            self.write(self.p(d + "/focus.json"), '{"URLBlocklist": ["reddit.com"]}\n')

    def test_uninstall_removes_only_ours(self):
        self.install()
        self.simulate_daemon()
        r = self.run_script("uninstall.sh")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with open(self.p("etc/hosts"), "rb") as f:
            self.assertEqual(f.read(), HOSTS, "everything outside the block must survive byte for byte")
        self.assertEqual(sorted(os.listdir(self.p("etc/chromium/policies/managed"))), ["color.json", "omarchy.json"])
        self.assertEqual(os.listdir(self.p("etc/vivaldi/policies/managed")), [])
        for rel in ("usr/local/lib/focus", "usr/local/bin/focus", "usr/local/bin/focus-watch",
                    "usr/local/sbin/focusd", "etc/focus", "etc/systemd/system/focusd.service",
                    "etc/systemd/system/focus-dnslog.service", "etc/systemd/user/focus-watch.service"):
            self.assertFalse(os.path.exists(self.p(rel)), rel)
        self.assertTrue(os.path.isfile(self.p("var/lib/focus/hosts.orig")))
        self.assertTrue(os.path.isdir(self.p("var/log/focus/dns")))
        self.assertTrue(any(n.startswith("config.toml.uninstalled.") for n in os.listdir(self.p("var/lib/focus/backups"))))
        self.assertIn("would run: systemctl disable --now focusd.service focus-dnslog.service", r.stdout)
        self.assertIn("would run: systemctl --global disable focus-watch.service", r.stdout)
        # Running it again is harmless.
        r = self.run_script("uninstall.sh")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_uninstall_block_in_middle_keeps_rest(self):
        self.install()
        with open(self.p("etc/hosts"), "wb") as f:
            f.write(HOSTS + BLOCK + b"192.168.1.9 printer\n")
        r = self.run_script("uninstall.sh")
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(self.p("etc/hosts"), "rb") as f:
            self.assertEqual(f.read(), HOSTS + b"192.168.1.9 printer\n")
        self.assertEqual(mode(self.p("etc/hosts")), 0o644)

    def test_uninstall_malformed_block_left_untouched(self):
        self.install()
        broken = HOSTS + BLOCK.split(b"# END")[0]
        with open(self.p("etc/hosts"), "wb") as f:
            f.write(broken)
        r = self.run_script("uninstall.sh")
        self.assertEqual(r.returncode, 1)
        self.assertIn("malformed", r.stderr)
        self.assertIn("'# BEGIN focus' on line(s) [5]", r.stderr)
        with open(self.p("etc/hosts"), "rb") as f:
            self.assertEqual(f.read(), broken)
        self.assertFalse(os.path.exists(self.p("usr/local/lib/focus")), "the rest of the uninstall still runs")

    def test_uninstall_two_begins_left_untouched(self):
        self.install()
        broken = HOSTS + BLOCK + BLOCK
        with open(self.p("etc/hosts"), "wb") as f:
            f.write(broken)
        r = self.run_script("uninstall.sh")
        self.assertEqual(r.returncode, 1)
        self.assertIn("more than one", r.stderr)
        self.assertIn("line(s) [5, 9]", r.stderr)
        with open(self.p("etc/hosts"), "rb") as f:
            self.assertEqual(f.read(), broken)

    def test_uninstall_tolerates_what_focusd_tolerates(self):
        # focusd's rules: the block ends at the FIRST "# END focus..." after BEGIN;
        # stray END lines outside the block are ordinary lines and are kept.
        self.install()
        stray_before = b"# END focus (left over)\n"
        block = BLOCK.replace(b"# END focus\n", b"  # END focus  \r\n")
        tail = b"# END focus\n192.168.1.9 printer\n"
        with open(self.p("etc/hosts"), "wb") as f:
            f.write(HOSTS + stray_before + block + tail)
        r = self.run_script("uninstall.sh")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with open(self.p("etc/hosts"), "rb") as f:
            self.assertEqual(f.read(), HOSTS + stray_before + tail)

    def test_install_builds_the_browser_extension(self):
        self.install()
        d = self.p("var/lib/focus/extension")
        self.assertEqual(mode(os.path.join(d, "key.pem")), 0o600)
        for name in ("focus-guard.crx", "update.xml", "id"):
            self.assertEqual(mode(os.path.join(d, name)), 0o644, name)
        with open(os.path.join(d, "id")) as f:
            first = f.read()
        self.install()
        with open(os.path.join(d, "id")) as f:
            self.assertEqual(f.read(), first, "a reinstall keeps the extension id")

    def test_a_failed_extension_build_only_warns(self):
        shutil.rmtree(os.path.join(self.repo, "extension"))
        out = self.install()
        self.assertFalse(os.path.exists(self.p("var/lib/focus/extension/id")))
        self.assertIsNotNone(out)

    def test_uninstall_uses_focusd_split_hosts_when_available(self):
        real_pkg = os.path.join(REPO, "focus")
        if not os.path.isfile(os.path.join(real_pkg, "render.py")):
            self.skipTest("focus/render.py (builder A) not present")
        for fn in os.listdir(real_pkg):
            if fn.endswith(".py"):
                shutil.copy(os.path.join(real_pkg, fn), os.path.join(self.repo, "focus", fn))
        self.install()
        with open(self.p("etc/hosts"), "ab") as f:
            f.write(BLOCK)
        r = self.run_script("uninstall.sh")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("focusd's split_hosts", r.stdout)
        with open(self.p("etc/hosts"), "rb") as f:
            self.assertEqual(f.read(), HOSTS)

    def test_uninstall_reads_policy_dirs_from_config(self):
        self.install()
        custom = self.p("opt/browser/policies")
        os.makedirs(custom)
        for name in ("focus.json", "other.json"):
            self.write(os.path.join(custom, name), "{}\n")
        cfg = self.p("etc/focus/config.toml")
        with open(cfg, encoding="utf-8") as f:
            text = f.read()
        text = text.replace('policy_dirs = ["/etc/chromium/policies/managed", "/etc/vivaldi/policies/managed"]',
                            'policy_dirs = ["/opt/browser/policies/"]')
        self.assertIn("/opt/browser/policies/", text)
        with open(cfg, "w", encoding="utf-8") as f:
            f.write(text)
        self.simulate_daemon()
        r = self.run_script("uninstall.sh")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(os.listdir(custom), ["other.json"])
        # the defaults are still cleaned (an older config may have used them); Omarchy's files stay
        self.assertEqual(sorted(os.listdir(self.p("etc/chromium/policies/managed"))), ["color.json", "omarchy.json"])
        self.assertEqual(os.listdir(self.p("etc/vivaldi/policies/managed")), [])

    def test_uninstall_purge(self):
        self.install()
        r = self.run_script("uninstall.sh", "--purge")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(self.p("var/lib/focus")))
        self.assertFalse(os.path.exists(self.p("var/log/focus")))
        self.assertTrue(os.path.exists(self.p("etc/hosts")))


if __name__ == "__main__":
    unittest.main()
