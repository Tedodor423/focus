"""In-process unit tests for focusd's pure parts: config, render, lists,
state, judge parsing and the DNS-log check."""

import datetime
import json
import os
import shutil
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "fakes"))
import daemon_harness as H  # noqa: E402

from focus import config, judge, lists, paths, render, state  # noqa: E402

GOOD = """
user = "theo"
mode = "light"
[safety]
never_block = ["google.com"]
[lists.porn]
url = "https://example.com/l.txt"
[groups.reddit]
label = "Reddit"
hosts = ["Reddit.com", "www.reddit.com"]
url_block = ["reddit.com"]
lists = ["porn"]
[groups.yt]
hosts = []
url_block = ["youtube.com"]
url_allow = ["music.youtube.com"]
"""


def cfg_from(text):
    import tomllib
    return config.from_dict(tomllib.loads(text))


class ConfigTests(unittest.TestCase):
    def test_good_config(self):
        cfg, errors, fatal = cfg_from(GOOD)
        self.assertEqual(errors, [])
        self.assertFalse(fatal)
        self.assertEqual(list(cfg.groups), ["reddit", "yt"])
        self.assertEqual(cfg.groups["reddit"].hosts, ["reddit.com", "www.reddit.com"])
        self.assertEqual(cfg.groups["yt"].label, "yt")
        self.assertEqual(cfg.cap(cfg.groups["yt"]), 20)
        self.assertEqual(cfg.watch.mode, "observe")

    def test_bad_entries_dropped(self):
        cfg, errors, fatal = cfg_from(GOOD + """
[groups."Bad Id"]
hosts = ["x.com"]
[groups.mixed]
hosts = ["ok.example.com", "1.2.3.4", "localhost", "bad_host!", "single", "mail.google.com", 5]
lists = ["nope"]
webapp_classes = ["^ok$", "(unclosed"]
title_patterns = ["[bad"]
max_minutes = "ten"
[watch]
mode = "aggressive"
penalty_minutes = 0
""")
        self.assertFalse(fatal)
        self.assertNotIn("Bad Id", cfg.groups)
        g = cfg.groups["mixed"]
        self.assertEqual(g.hosts, ["ok.example.com"])
        self.assertEqual(g.lists, [])
        self.assertEqual(g.webapp_classes, ["^ok$"])
        self.assertEqual(g.title_patterns, [])
        self.assertIsNone(g.max_minutes)
        self.assertEqual(cfg.watch.mode, "observe")
        self.assertEqual(cfg.watch.penalty_minutes, 30)
        text = "\n".join(errors)
        for needle in ("Bad Id", "1.2.3.4", "localhost", "never_block", "nope",
                       "(unclosed", "[bad", "max_minutes", "aggressive", "penalty_minutes"):
            self.assertIn(needle, text)

    def test_mode_other_than_light_is_fatal(self):
        cfg, errors, fatal = cfg_from('mode = "strict"\n[groups.a]\nhosts=["a.com"]\n')
        self.assertTrue(fatal)
        self.assertIn("a", cfg.groups)   # still usable for a fail-closed first start

    def test_unparsable_file(self):
        root = H.tmp_root()
        try:
            p = os.path.join(root, "c.toml")
            with open(p, "w") as f:
                f.write("this is [not toml\n")
            cfg, errors, fatal = config.load(p)
            self.assertIsNone(cfg)
            self.assertTrue(fatal)
            cfg, errors, fatal = config.load(os.path.join(root, "missing.toml"))
            self.assertIsNone(cfg)
        finally:
            shutil.rmtree(root)

    def test_penalty_domain_rules(self):
        never = ["google.com", "example.ac.uk", "docs.example.net"]
        cases = {"https://WWW.News.YCombinator.com/item?id=1": ("news.ycombinator.com", None),
                 "x.example": ("x.example", None), "bbc.co.uk": ("bbc.co.uk", None),
                 "ac.uk": (None, "too_broad"), "co.uk": (None, "too_broad"),
                 "com.au": (None, "too_broad"), "github.io": (None, "too_broad"),
                 "www.co.uk": (None, "too_broad"),
                 "example.net": (None, "never_block"),     # parent of docs.example.net
                 "mail.google.com": (None, "never_block"), "google.com": (None, "never_block"),
                 "1.2.3.4": (None, "invalid"), "com": (None, "invalid"), "": (None, "invalid"),
                 None: (None, "invalid"), 5: (None, "invalid")}
        for raw, want in cases.items():
            self.assertEqual(config.penalty_domain(raw, never), want, raw)
        self.assertTrue(config.overlaps_any("ac.uk", {"example.ac.uk"}))
        self.assertFalse(config.overlaps_any("bc.uk", {"example.ac.uk"}))

    def test_hostname_rules(self):
        ok = ["reddit.com", "a.b.example.co.uk", "xn--bcher-kva.example", "ads_1.x.com"]
        bad = ["1.2.3.4", "localhost", "foo.localhost", "localhost.localdomain", "com",
               "-a.com", "a..com", "a.com.", "Ab.com", "a" * 64 + ".com", "a.123"]
        for n in ok:
            self.assertTrue(config.valid_hostname(n), n)
        for n in bad:
            self.assertFalse(config.valid_hostname(n), n)
        self.assertEqual(config.normalize_host(" Reddit.COM. "), "reddit.com")
        self.assertTrue(config.under_any("a.mail.google.com", {"google.com"}))
        self.assertFalse(config.under_any("notgoogle.com", {"google.com"}))
        self.assertEqual(config.url_host("https://www.x.com:443/a?b"), "www.x.com")
        self.assertEqual(config.url_host("youtube.com/embed"), "youtube.com")


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.cfg, _, _ = cfg_from(GOOD)

    def test_host_lines(self):
        entries = {"porn": ["a.example", "maps.google.com", "reddit.com"]}
        lines = render.host_lines(self.cfg, {"reddit", "yt"}, entries,
                                  ["news.ycombinator.com", "www.x.com", "google.com"])
        self.assertEqual(lines, [
            "0.0.0.0 reddit.com", ":: reddit.com",
            "0.0.0.0 www.reddit.com", ":: www.reddit.com",
            "0.0.0.0 a.example",
            "0.0.0.0 news.ycombinator.com", ":: news.ycombinator.com",
            "0.0.0.0 www.news.ycombinator.com", ":: www.news.ycombinator.com",
            "0.0.0.0 www.x.com", ":: www.x.com",
        ])
        self.assertEqual(render.host_lines(self.cfg, set(), entries, []), [])

    def test_policy(self):
        pol = json.loads(render.policy_json(self.cfg, {"reddit", "yt"}, ["x.com"]))
        self.assertEqual(pol, {"URLBlocklist": ["reddit.com", "youtube.com", ".x.com", ".www.x.com"],
                               "URLAllowlist": ["music.youtube.com"],
                               "DnsOverHttpsMode": "off", "ForceGoogleSafeSearch": True})
        self.cfg.browser.disable_doh = False
        self.cfg.browser.force_google_safesearch = False
        pol = json.loads(render.policy_json(self.cfg, set(), []))
        self.assertEqual(pol, {"URLBlocklist": [], "URLAllowlist": []})

    def test_penalty_parent_of_never_block_not_rendered(self):
        self.cfg.never_block = ["google.com", "example.ac.uk"]
        pens = ["ac.uk", "maps.google.com", "fine.example"]
        self.assertEqual(render.host_lines(self.cfg, set(), {}, pens),
                         ["0.0.0.0 fine.example", ":: fine.example",
                          "0.0.0.0 www.fine.example", ":: www.fine.example"])
        pol = json.loads(render.policy_json(self.cfg, set(), pens))
        self.assertEqual(pol["URLBlocklist"], [".fine.example", ".www.fine.example"])

    def test_splice_preserves_bytes(self):
        block = render.render_block(["0.0.0.0 a.com"])
        for original in (H.HOSTS, b"127.0.0.1 localhost\r\n# crlf\r\n",
                         b"127.0.0.1 localhost", b"", b"\n\n  \t\n"):
            out = render.splice_hosts(original, block)
            self.assertTrue(out.endswith(block))
            prefix = out[:-len(block)]
            if original and not original.endswith(b"\n"):
                self.assertEqual(prefix, original + b"\n")
            else:
                self.assertEqual(prefix, original)
            # Re-splicing is stable and replaces (does not duplicate) the block.
            again = render.splice_hosts(out, render.render_block([]))
            self.assertEqual(again, prefix + render.render_block([]))

    def test_splice_moves_block_to_end(self):
        old = render.render_block(["0.0.0.0 old.com"])
        current = b"127.0.0.1 localhost\n" + old + b"10.1.1.1 added-later\n"
        out = render.splice_hosts(current, render.render_block([]))
        self.assertEqual(out, b"127.0.0.1 localhost\n10.1.1.1 added-later\n"
                         + render.render_block([]))

    def test_splice_refusals(self):
        block = render.render_block([])
        with self.assertRaises(render.HostsError):
            render.splice_hosts(b"a\n# BEGIN focus\nb\n", block)
        with self.assertRaises(render.HostsError):
            render.splice_hosts(b"# BEGIN focus\n# END focus\n# BEGIN focus\n# END focus\n", block)
        with self.assertRaises(render.HostsError):   # a block that would not survive
            render.splice_hosts(b"x\n", b"# BEGIN focus\n# BEGIN focus\n# END focus\n")

    def test_empty_block(self):
        self.assertEqual(render.render_block([]),
                         "# BEGIN focus — managed by focusd, manual edits are reverted\n"
                         "# END focus\n".encode())


class ListTests(unittest.TestCase):
    def test_parse_list(self):
        text = """# comment
0.0.0.0 a.example # trailing
127.0.0.1 b.example
c.example
10.0.0.1 evil.example
:: d.example
0.0.0.0 0.0.0.0
0.0.0.0 x.example y.example
bad!name
mail.google.com
A.EXAMPLE
"""
        self.assertEqual(lists.parse_list(text, ["google.com"]),
                         ["a.example", "b.example", "c.example", "x.example", "y.example"])

    def test_fetch_success_and_failure(self):
        root = H.tmp_root()
        old = os.environ.get("FOCUS_ROOT")
        os.environ["FOCUS_ROOT"] = root
        events, updates = [], []
        try:
            cfg, _, _ = cfg_from(GOOD.replace('url = "https://example.com/l.txt"',
                                              'url = "https://example.com/l.txt"\nmin_entries = 2\nmax_entries = 3'))
            body = [b"0.0.0.0 a.example\nb.example\n"]
            L = lists.Lists(lambda t, **f: events.append((t, f)), lambda: updates.append(1),
                            download=lambda url: body[0])
            L.refresh(cfg, time.time())
            H.wait_for(lambda: updates, 5)
            self.assertEqual(L.entries["porn"], ["a.example", "b.example"])
            self.assertEqual(events[-1], ("list_update", {"name": "porn", "entries": 2}))
            with open(root + "/var/lib/focus/lists/porn.txt") as f:
                self.assertEqual(f.read(), "a.example\nb.example\n")
            with open(root + "/var/lib/focus/lists/porn.meta.json") as f:
                meta = json.load(f)
            self.assertEqual((meta["url"], meta["entries"]), ("https://example.com/l.txt", 2))
            # Fresh cache: nothing to fetch. A forced refetch of a too-small list fails
            # and keeps the cache.
            L.refresh(cfg, time.time())
            body[0] = b"only.example\n"
            L2 = lists.Lists(lambda t, **f: events.append((t, f)), lambda: None,
                             download=lambda url: body[0])
            L2.load_cache(cfg)
            self.assertEqual(L2.entries["porn"], ["a.example", "b.example"])
            L2.meta["porn"]["updated"] = 0
            L2.refresh(cfg, time.time())
            H.wait_for(lambda: events[-1][0] == "list_error", 5)
            self.assertIn("outside", events[-1][1]["error"])
            self.assertEqual(L2.entries["porn"], ["a.example", "b.example"])
        finally:
            if old is None:
                os.environ.pop("FOCUS_ROOT", None)
            else:
                os.environ["FOCUS_ROOT"] = old
            shutil.rmtree(root)


class DnslogTests(unittest.TestCase):
    def setUp(self):
        self.root = H.tmp_root()
        self.old = os.environ.get("FOCUS_ROOT")
        os.environ["FOCUS_ROOT"] = self.root

    def tearDown(self):
        if self.old is None:
            os.environ.pop("FOCUS_ROOT", None)
        else:
            os.environ["FOCUS_ROOT"] = self.old
        shutil.rmtree(self.root)

    def test_branches(self):
        now = time.time()
        self.assertEqual(lists.dnslog_check("x.com", now), "no_dnslog")
        H.fake_dnslog(self.root, [(now - 60, "news.ycombinator.com"),
                                  (now - 1800, "old.example"), (now - 30, "www.example.ac.uk"),
                                  (now - 30, "www.w.example")])
        yesterday = datetime.date.fromtimestamp(now - 86400).strftime("%Y-%m-%d")
        H.fake_dnslog(self.root, [(now - 120, "yesterday.example")], day=yesterday)
        self.assertIsNone(lists.dnslog_check("news.ycombinator.com", now))
        self.assertIsNone(lists.dnslog_check("w.example", now))           # www.d counts
        self.assertIsNone(lists.dnslog_check("yesterday.example", now))
        # A name merely under the domain does not count.
        self.assertEqual(lists.dnslog_check("ycombinator.com", now), "not_in_dnslog")
        self.assertEqual(lists.dnslog_check("ac.uk", now), "not_in_dnslog")
        self.assertEqual(lists.dnslog_check("old.example", now), "not_in_dnslog")
        self.assertEqual(lists.dnslog_check("combinator.com", now), "not_in_dnslog")


class StateTests(unittest.TestCase):
    def test_day_start(self):
        t = datetime.datetime(2026, 11, 10, 3, 59).timestamp()
        self.assertEqual(state.day_start(t, (4, 0)), datetime.datetime(2026, 11, 9, 4, 0).timestamp())
        t = datetime.datetime(2026, 11, 10, 4, 0).timestamp()
        self.assertEqual(state.day_start(t, (4, 0)), t)
        self.assertEqual(state.next_day_start(t, (4, 0)),
                         datetime.datetime(2026, 11, 11, 4, 0).timestamp())

    def test_state_roundtrip_and_garbage(self):
        root = H.tmp_root()
        try:
            p = os.path.join(root, "state.json")
            st = state.State()
            st.unlocks["yt"] = {"since": 1.0, "until": 2.0, "minutes": 3, "reason": "r"}
            st.penalties.append({"id": "p1", "since": 1.0, "until": 2.0})
            st.save(p)
            st2, err = state.State.load(p)
            self.assertIsNone(err)
            self.assertEqual(st2.to_json(), st.to_json())
            with open(p, "w") as f:
                f.write('{"unlocks": [1], "penalties": {"a": 1}, "cooldowns": {"a": "x"}}')
            st3, err = state.State.load(p)
            self.assertEqual(st3.to_json(), state.State().to_json())
            with open(p, "w") as f:
                f.write("{torn")
            _, err = state.State.load(p)
            self.assertIn("unreadable", err)
        finally:
            shutil.rmtree(root)


class JudgeParseTests(unittest.TestCase):
    def ok(self, so):
        return json.dumps({"is_error": False, "structured_output": so}).encode()

    def test_parse(self):
        v = judge.parse_output(self.ok({"decision": "grant", "minutes": 99, "message": " hi "}))
        self.assertEqual(v, {"decision": "grant", "minutes": 60, "message": "hi"})
        bad = [b"", b"not json", b"[1]", json.dumps({"is_error": True}).encode(),
               json.dumps({"structured_output": {"decision": "grant"}}).encode(),
               self.ok({"decision": "maybe", "minutes": 1, "message": ""}),
               self.ok({"decision": "grant", "minutes": "5", "message": ""}),
               self.ok({"decision": "grant", "minutes": 5}),
               json.dumps({"is_error": False}).encode()]
        for out in bad:
            with self.assertRaises(judge.JudgeError, msg=out):
                judge.parse_output(out)

    def test_command_as_root_uses_popen_credentials(self):
        import getpass
        import pwd
        cfg, _, _ = cfg_from(GOOD + '[judge]\ncommand = ["/opt/x/bin/claude"]\n')
        cfg.user = getpass.getuser()
        pw = pwd.getpwnam(cfg.user)
        argv, kw = judge.build_command(cfg, "SYS", privileged=True)
        self.assertEqual(argv[0], "/opt/x/bin/claude")           # no runuser, no env -i
        self.assertNotIn("runuser", " ".join(argv))
        self.assertEqual((kw["user"], kw["group"]), (pw.pw_uid, pw.pw_gid))
        self.assertEqual(kw["extra_groups"], os.getgrouplist(cfg.user, pw.pw_gid))
        self.assertEqual(kw["env"], {"HOME": pw.pw_dir, "PATH": "/opt/x/bin:/usr/local/bin:/usr/bin:/bin",
                                     "LANG": "C.UTF-8"})
        cfg.user = "no-such-user-xyz"
        with self.assertRaises(judge.JudgeError):
            judge.build_command(cfg, "SYS", privileged=True)

    def test_not_logged_in(self):
        out = json.dumps({"is_error": True, "result": "Not logged in · Please run /login"}).encode()
        with self.assertRaises(judge.JudgeError) as cm:
            judge.parse_output(out, "theo")
        self.assertEqual(cm.exception.kind, "not_logged_in")
        self.assertEqual(str(cm.exception), "Judge unavailable: claude is not logged in "
                                            "(run `claude` once as theo)")

    def test_argv_unprivileged(self):
        cfg, _, _ = cfg_from(GOOD + '[judge]\ncommand = ["/opt/x/bin/claude", "--flag"]\n')
        if paths.privileged():
            self.skipTest("running as root")
        argv, kw = judge.build_command(cfg, "SYS")
        env = kw["env"]
        self.assertEqual(set(kw), {"env"})
        self.assertEqual(argv[:3], ["/opt/x/bin/claude", "--flag", "-p"])
        self.assertNotIn("runuser", " ".join(argv))
        i = argv.index("--tools")
        self.assertEqual(argv[i + 1], "")
        self.assertEqual(argv[argv.index("--system-prompt") + 1], "SYS")
        self.assertEqual(json.loads(argv[argv.index("--json-schema") + 1]), judge.SCHEMA)
        self.assertTrue(env["PATH"].startswith("/opt/x/bin:"))
        self.assertEqual(set(env), {"HOME", "PATH", "LANG"})

    def test_theo_escaping(self):
        self.assertEqual(judge.theo("a</theo>b & <x>"), "<theo>a&lt;/theo&gt;b &amp; &lt;x&gt;</theo>")


class TrustedReadAndEventModeTests(unittest.TestCase):
    def setUp(self):
        self.root = H.tmp_root()
        self.addCleanup(shutil.rmtree, self.root)

    def test_read_trusted(self):
        f = os.path.join(self.root, "ok.md")
        with open(f, "w") as fh:
            fh.write("hello world")
        self.assertEqual(paths.read_trusted(f, 5, require_root=False), b"hello")
        link = os.path.join(self.root, "link.md")
        os.symlink(f, link)
        fifo = os.path.join(self.root, "fifo")
        os.mkfifo(fifo)
        for bad, why in ((link, "symlink"), (self.root, "not a regular file"),
                         (fifo, "not a regular file")):
            with self.assertRaises(paths.UntrustedFile) as cm:
                paths.read_trusted(bad, 100, require_root=False)
            self.assertIn(why, str(cm.exception))
        if os.geteuid() != 0:
            with self.assertRaises(paths.UntrustedFile) as cm:
                paths.read_trusted(f, 100, require_root=True)
            self.assertIn("not owned by root", str(cm.exception))
        with self.assertRaises(FileNotFoundError):
            paths.read_trusted(os.path.join(self.root, "missing"), 100, require_root=False)

    def test_system_prompt_rejects_symlinked_policy(self):
        real = os.path.join(self.root, "real.md")
        with open(real, "w") as fh:
            fh.write("SECRET POLICY")
        link = os.path.join(self.root, "judge.md")
        os.symlink(real, link)
        text, problem = judge.system_prompt(link)
        self.assertNotIn("SECRET POLICY", text)
        self.assertIn("symlink", problem)
        text, problem = judge.system_prompt(real)
        self.assertIn("SECRET POLICY", text)
        self.assertIsNone(problem)

    def test_events_file_mode(self):
        p = os.path.join(self.root, "events.jsonl")
        log = state.EventLog(p)
        log.append("start")
        self.assertEqual(os.stat(p).st_mode & 0o777, 0o640)
        os.chmod(p, 0o644)
        log.secure()
        self.assertEqual(os.stat(p).st_mode & 0o777, 0o640)
        os.chmod(p, 0o666)
        log.append("stop")                      # fixed on every append too
        self.assertEqual(os.stat(p).st_mode & 0o777, 0o640)


if __name__ == "__main__":
    unittest.main()
