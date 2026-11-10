"""Tests for install-user.sh / uninstall-user.sh (builder D).

Run with a fake HOME and XDG_STATE_HOME; systemctl, hyprctl, omarchy and
omarchy-shell are fakes that log their argv. Nothing touches the real
~/.config, the real shell or the real user manager.
"""

import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REAL_BLOCK = os.path.join(REPO, "shell", "theo.focus", "hypr-bindings.lua")
FALLBACK_BLOCK = textwrap.dedent("""\
    -- BEGIN focus
    -- focus blocker
    o.bind("SUPER + ALT + U", "Focus unlock", "omarchy-shell theo.focus unlock")
    -- END focus
    """)
ORIGINAL = textwrap.dedent("""\
    -- Keep only your personal keybinding overrides here.
    o.bind("SUPER + D", "LifeOS dashboard", "omarchy-shell shell toggle tedodor423.lifeos-dash")

    -- BEGIN recall
    o.bind("SUPER + ALT + R", "Recall", "omarchy-shell theo.recall review")
    -- END recall
    hl.unbind("ALT + SPACE")
    """)

FAKE = """#!/bin/bash
# fake {name}: log argv, optionally fail or print canned output
printf '%s %s\\n' "{name}" "$*" >> "$FAKE_LOG"
case "{name} $*" in
  "omarchy plugin disable"*)
    if [ -e "$HOME/.config/omarchy/plugins/theo.focus" ]; then echo "link-present-at-disable" >> "$FAKE_LOG"; fi ;;
  "omarchy plugin enable"*"--after"*)
    [ -n "${{FAKE_FAIL_AFTER:-}}" ] && exit 1 ;;
  "hyprctl configerrors"*)
    if [ -n "${{FAKE_HYPR_ERRORS:-}}" ]; then printf '["%s"]\\n' "$FAKE_HYPR_ERRORS"; else printf '[\\n\\t""\\n]\\n'; fi ;;
  "systemctl --user restart"*)
    [ -n "${{FAKE_FAIL_RESTART:-}}" ] && exit 1 ;;
esac
exit 0
"""


class UserInstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        t = self.tmp.name
        self.home = os.path.join(t, "home")
        self.state = os.path.join(t, "state")
        self.root = os.path.join(t, "root")
        self.repo = os.path.join(t, "repo")
        self.fakebin = os.path.join(t, "fakebin")
        self.log = os.path.join(t, "calls.log")
        os.makedirs(os.path.join(self.home, ".config", "hypr"))
        os.makedirs(os.path.join(self.home, ".config", "omarchy", "plugins"))
        os.makedirs(os.path.join(self.root, "etc", "systemd", "user"))
        self.write(os.path.join(self.root, "etc", "systemd", "user", "focus-watch.service"), "[Unit]\n")
        self.bindings = os.path.join(self.home, ".config", "hypr", "bindings.lua")
        self.write(self.bindings, ORIGINAL)
        # fake repo
        self.plugin = os.path.join(self.repo, "shell", "theo.focus")
        os.makedirs(self.plugin)
        for f in ("install-user.sh", "uninstall-user.sh"):
            shutil.copy(os.path.join(REPO, f), os.path.join(self.repo, f))
        self.write(os.path.join(self.plugin, "manifest.json"), '{"id": "theo.focus"}\n')
        if os.path.isfile(REAL_BLOCK):
            shutil.copy(REAL_BLOCK, os.path.join(self.plugin, "hypr-bindings.lua"))
        else:
            self.write(os.path.join(self.plugin, "hypr-bindings.lua"), FALLBACK_BLOCK)
        # fakes
        os.makedirs(self.fakebin)
        for name in ("systemctl", "hyprctl", "omarchy", "omarchy-shell"):
            path = os.path.join(self.fakebin, name)
            self.write(path, FAKE.format(name=name))
            os.chmod(path, 0o755)

    @staticmethod
    def write(path, text):
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    @staticmethod
    def read(path):
        with open(path, encoding="utf-8") as f:
            return f.read()

    def block(self):
        lines = self.read(os.path.join(self.plugin, "hypr-bindings.lua")).splitlines()
        b = lines.index("-- BEGIN focus")
        e = lines.index("-- END focus")
        return "\n".join(lines[b:e + 1]) + "\n"

    def run_script(self, name, *args, **extra):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("FOCUS_", "FAKE_", "XDG_STATE"))}
        env.update({
            "HOME": self.home, "XDG_STATE_HOME": self.state, "FOCUS_ROOT": self.root,
            "FAKE_LOG": self.log, "PATH": self.fakebin + os.pathsep + env.get("PATH", "/usr/bin:/bin"),
        })
        for n in ("systemctl", "hyprctl", "omarchy"):
            env["FOCUS_" + n.upper()] = os.path.join(self.fakebin, n)
        env["FOCUS_OMARCHY_SHELL"] = os.path.join(self.fakebin, "omarchy-shell")
        env.update(extra)
        return subprocess.run(["bash", os.path.join(self.repo, name), *args], env=env,
                              capture_output=True, text=True, timeout=60, cwd=self.tmp.name)

    def calls(self):
        try:
            return self.read(self.log).splitlines()
        except FileNotFoundError:
            return []

    def backups(self):
        d = os.path.join(self.state, "focus", "backups")
        return sorted(os.listdir(d)) if os.path.isdir(d) else []

    @property
    def link(self):
        return os.path.join(self.home, ".config", "omarchy", "plugins", "theo.focus")

    # ------------------------------------------------------------ install
    def test_install(self):
        r = self.run_script("install-user.sh")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(os.path.islink(self.link))
        self.assertEqual(os.readlink(self.link), self.plugin)
        self.assertEqual(self.read(self.bindings), ORIGINAL + "\n" + self.block())
        self.assertEqual(len(self.backups()), 1)
        self.assertEqual(self.read(os.path.join(self.state, "focus", "backups", self.backups()[0])), ORIGINAL)
        self.assertEqual(self.calls(), [
            "omarchy-shell shell rescanPlugins",
            "omarchy plugin enable theo.focus --section centerEnd --after theo.recall",
            "hyprctl reload",
            "hyprctl configerrors -j",
            "systemctl --user daemon-reload",
            "systemctl --user enable focus-watch.service",
            "systemctl --user restart focus-watch.service",
        ])

    def test_install_is_idempotent(self):
        self.assertEqual(self.run_script("install-user.sh").returncode, 0)
        first = self.read(self.bindings)
        r = self.run_script("install-user.sh")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.read(self.bindings), first)
        self.assertEqual(len(self.backups()), 1, "no backup when nothing changed")
        self.assertIn("already up to date", r.stdout)
        self.assertIn("symlink already in place", r.stdout)

    def test_replaces_old_block_in_place(self):
        before = "-- top\n"
        after = "-- BEGIN recall\n-- END recall\n"
        self.write(self.bindings, before + "-- BEGIN focus\nold stuff\n-- END focus\n" + after)
        r = self.run_script("install-user.sh")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.read(self.bindings), before + self.block() + after)

    def test_file_without_trailing_newline(self):
        self.write(self.bindings, "-- only line")
        self.assertEqual(self.run_script("install-user.sh").returncode, 0)
        self.assertEqual(self.read(self.bindings), "-- only line\n\n" + self.block())

    def test_malformed_existing_block_left_untouched(self):
        bad = ORIGINAL + "-- BEGIN focus\no.bind('x')\n"
        self.write(self.bindings, bad)
        r = self.run_script("install-user.sh")
        self.assertEqual(r.returncode, 1)
        self.assertIn("malformed", r.stderr)
        self.assertEqual(self.read(self.bindings), bad)
        self.assertNotIn("hyprctl reload", self.calls())

    def test_bad_block_source(self):
        self.write(os.path.join(self.plugin, "hypr-bindings.lua"), "-- BEGIN focus\no.bind()\n")
        r = self.run_script("install-user.sh")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.read(self.bindings), ORIGINAL)

    @unittest.skipUnless(shutil.which("luac"), "luac not installed")
    def test_lua_syntax_error_rejected(self):
        self.write(os.path.join(self.plugin, "hypr-bindings.lua"),
                   '-- BEGIN focus\no.bind("SUPER + ALT + U", "x", \n-- END focus\n')
        r = self.run_script("install-user.sh")
        self.assertEqual(r.returncode, 1)
        self.assertIn("Lua syntax error", r.stderr)
        self.assertEqual(self.read(self.bindings), ORIGINAL)

    def test_config_errors_fail_loudly(self):
        r = self.run_script("install-user.sh", FAKE_HYPR_ERRORS="bindings.lua:40: unknown function")
        self.assertEqual(r.returncode, 1)
        self.assertIn("config errors", r.stderr)
        self.assertIn("unknown function", r.stderr)

    def test_enable_falls_back_without_after(self):
        r = self.run_script("install-user.sh", FAKE_FAIL_AFTER="1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("omarchy plugin enable theo.focus --section centerEnd", self.calls())
        self.assertIn("not after theo.recall", r.stderr)

    def test_conflicting_binding_warns(self):
        self.write(self.bindings, ORIGINAL + 'o.bind("SUPER + ALT + U", "Other", "true")\n')
        r = self.run_script("install-user.sh")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("also bound outside the focus block", r.stderr)

    def test_refuses_non_symlink_plugin_dir(self):
        os.makedirs(self.link)
        self.write(os.path.join(self.link, "keep.txt"), "x")
        r = self.run_script("install-user.sh")
        self.assertEqual(r.returncode, 1)
        self.assertIn("not a symlink", r.stderr)
        self.assertTrue(os.path.isfile(os.path.join(self.link, "keep.txt")))
        self.assertEqual(self.read(self.bindings), ORIGINAL)

    def test_repoints_wrong_symlink(self):
        os.symlink(self.tmp.name, self.link)
        self.assertEqual(self.run_script("install-user.sh").returncode, 0)
        self.assertEqual(os.readlink(self.link), self.plugin)

    def test_without_root_part_skips_monitor(self):
        os.remove(os.path.join(self.root, "etc", "systemd", "user", "focus-watch.service"))
        r = self.run_script("install-user.sh")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("run the root part first", r.stderr)
        self.assertFalse([c for c in self.calls() if c.startswith("systemctl")])

    def test_restart_failure_is_an_error(self):
        r = self.run_script("install-user.sh", FAKE_FAIL_RESTART="1")
        self.assertEqual(r.returncode, 1)
        self.assertIn("could not start", r.stderr)

    def test_no_session_runs_no_commands(self):
        r = self.run_script("install-user.sh", "--no-session")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.calls(), [])
        self.assertTrue(os.path.islink(self.link))
        self.assertIn("-- BEGIN focus", self.read(self.bindings))

    def test_bindings_symlink_is_kept(self):
        real = os.path.join(self.tmp.name, "dotfiles-bindings.lua")
        self.write(real, ORIGINAL)
        os.remove(self.bindings)
        os.symlink(real, self.bindings)
        self.assertEqual(self.run_script("install-user.sh").returncode, 0)
        self.assertTrue(os.path.islink(self.bindings))
        self.assertIn("-- BEGIN focus", self.read(real))

    # ------------------------------------------------------------ uninstall
    def test_uninstall_restores_original(self):
        self.assertEqual(self.run_script("install-user.sh").returncode, 0)
        os.remove(self.log)
        r = self.run_script("uninstall-user.sh")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.read(self.bindings), ORIGINAL)
        self.assertFalse(os.path.lexists(self.link))
        calls = self.calls()
        self.assertEqual(calls[0], "systemctl --user disable --now focus-watch.service")
        i = calls.index("omarchy plugin disable theo.focus")
        self.assertEqual(calls[i + 1], "link-present-at-disable", "disable must run before the symlink goes")
        self.assertIn("hyprctl reload", calls)
        self.assertEqual(calls[-1], "omarchy-shell shell rescanPlugins")
        self.assertTrue(os.path.isdir(os.path.join(self.state, "focus", "backups")))
        # again: nothing left to do, still fine
        r = self.run_script("uninstall-user.sh")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.read(self.bindings), ORIGINAL)

    def test_uninstall_purge_and_non_symlink(self):
        self.assertEqual(self.run_script("install-user.sh").returncode, 0)
        r = self.run_script("uninstall-user.sh", "--purge")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.state, "focus")))
        os.makedirs(self.link)
        r = self.run_script("uninstall-user.sh")
        self.assertEqual(r.returncode, 1)
        self.assertTrue(os.path.isdir(self.link), "a real directory is never removed")


if __name__ == "__main__":
    unittest.main()
