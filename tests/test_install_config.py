"""Sanity tests for the config and judge policy (builder D).

Checks etc/config.example.toml + etc/judge.example.md (tracked, always present)
and, when they exist, the local git-ignored etc/config.toml + etc/judge.md against
the contract (§3) and real window titles and classes, so a careless edit to a
pattern shows up here first. If you removed groups from your local copy on
purpose, the checks that name those groups will tell you so.
"""

import os
import re
import sys
import tomllib
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLE_CONFIG = os.path.join(REPO, "etc", "config.example.toml")
EXAMPLE_JUDGE = os.path.join(REPO, "etc", "judge.example.md")
LOCAL_CONFIG = os.path.join(REPO, "etc", "config.toml")
LOCAL_JUDGE = os.path.join(REPO, "etc", "judge.md")
HOST_RE = re.compile(r"^(?:[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def load(path):
    with open(path, "rb") as f:
        return tomllib.load(f)


def under(name, domain):
    return name == domain or name.endswith("." + domain)


class ConfigChecks:
    """Mixed into one TestCase per config file (bottom of this module)."""
    CONFIG = None
    JUDGE = None
    IS_EXAMPLE = False

    @classmethod
    def setUpClass(cls):
        cls.c = load(cls.CONFIG)
        cls.groups = cls.c["groups"]
        cls.watch = cls.c["watch"]
        cls.never = cls.c["safety"]["never_block"]

    def rx(self, patterns):
        return [re.compile(p) for p in patterns]

    def any_search(self, patterns, text):
        return any(p.search(text) for p in self.rx(patterns))

    # ------------------------------------------------------------ structure
    def test_top_level(self):
        self.assertRegex(self.c["user"], r"^[a-z_][a-z0-9_-]{0,31}$")
        self.assertEqual(self.c["mode"], "light")
        self.assertIn(self.watch["mode"], ("off", "observe", "nudge", "enforce"))
        if self.IS_EXAMPLE:
            self.assertEqual(self.watch["mode"], "observe", "the monitor starts in observe mode")
        self.assertEqual(self.c["judge"]["policy_file"], "/etc/focus/judge.md")
        self.assertTrue(self.c["browser"]["disable_doh"])

    def test_groups_valid(self):
        self.assertEqual(list(self.groups)[:4], ["youtube", "reddit", "x", "instagram"])
        for gid, g in self.groups.items():
            self.assertRegex(gid, r"^[a-z0-9_-]{1,32}$")
            for key in ("label", "enabled", "unlockable", "hosts", "lists", "url_block", "url_allow",
                        "webapp_classes", "title_patterns"):
                self.assertIn(key, g, f"{gid}.{key}")
            for h in g["hosts"]:
                self.assertRegex(h, HOST_RE, f"{gid}: {h}")
                self.assertFalse(any(under(h, d) for d in self.never), f"{gid}: {h} is under never_block")
            self.assertEqual(len(g["hosts"]), len(set(g["hosts"])), f"{gid}: duplicate hosts")
            for u in g["url_block"] + g["url_allow"]:
                self.assertNotRegex(u, r"\s|://", f"{gid}: {u}")
            for name in g["lists"]:
                self.assertIn(name, self.c["lists"])
            self.rx(g["webapp_classes"] + g["title_patterns"])  # compile

    def test_linkedin_short_unlocks(self):
        g = self.groups["linkedin"]
        self.assertTrue(g["enabled"] and g["unlockable"])
        self.assertEqual(g["max_minutes"], 5)
        self.assertIn("linkedin.com", g["url_block"])
        self.assertIn("www.linkedin.com", g["hosts"])

    def test_policy_only_groups(self):
        self.assertEqual(self.groups["youtube"]["hosts"], [])
        self.assertEqual(self.groups["instagram"]["hosts"], [])
        self.assertIn("music.youtube.com", self.groups["youtube"]["url_allow"])
        self.assertIn("instagram.com/direct", self.groups["instagram"]["url_allow"])
        self.assertEqual(self.groups["instagram"]["webapp_classes"], [], "never close the DM web app")

    def test_never_block(self):
        for h in self.never:
            self.assertRegex(h, HOST_RE)
        for must in ("anthropic.com", "claude.ai", "google.com", "github.com", "tailscale.com",
                     "ts.net", "archlinux.org", "microsoftonline.com", "live.com"):
            self.assertIn(must, self.never)
        if self.IS_EXAMPLE:
            self.assertIn("nextcloud.example.com", self.never, "placeholder for your own file-sync server")
        blocked = {u.split("/")[0] for g in self.groups.values() for u in g["url_block"]}
        for b in blocked:
            self.assertFalse(any(under(b, d) for d in self.never), f"url_block {b} is under never_block")

    def test_porn_list(self):
        lst = self.c["lists"]["porn"]
        self.assertTrue(lst["url"].startswith("https://"))
        self.assertLess(lst["min_entries"], 76800)
        self.assertGreater(lst["max_entries"], 76800)
        self.assertEqual(self.groups["porn"]["lists"], ["porn"])
        self.assertFalse(self.groups["porn"]["unlockable"])

    # ------------------------------------------------------------ patterns vs real titles/classes
    # (title, expected) for browser / web-app windows. Expected: a group id, "d:<site>" for a
    # distraction pattern (the match text focus-watch reports as the app), or None.
    TITLES = [
        # YouTube; YouTube Music must never match
        ("What I Found In My Safe - YouTube - Vivaldi", "youtube"),
        ("(3) What I Found In My Safe - YouTube - Vivaldi", "youtube"),
        ("What I Found In My Safe - YouTube", "youtube"),
        ("YouTube - Vivaldi", "youtube"),
        ("(2) YouTube", "youtube"),
        ("Song - YouTube Music - Vivaldi", None),
        ("YouTube Music", None),
        ("YouTube Music - Vivaldi", None),
        ("Song • Artist - YouTube Music", None),
        ("(1) YouTube Music - Chromium", None),
        ("Liked music - YouTube Music", None),
        ("youtube-dl/README.md at master · ytdl-org/youtube-dl · GitHub - Vivaldi", None),
        ("www.youtube.com - Vivaldi", None),
        # Reddit
        ("Why is my code slow? : r/learnpython - Vivaldi", "reddit"),
        ("r/AskReddit - Vivaldi", "reddit"),
        ("r/bioinformatics on Reddit: Seurat v5 integration question - Vivaldi", "reddit"),
        ("Reddit - Dive into anything - Vivaldi", "reddit"),
        ("(4) Reddit - Dive into anything", "reddit"),
        ("best laptop for students - Reddit - Vivaldi", "reddit"),
        ("reddit seurat integration - Google Search - Vivaldi", None),
        ('Feed | LinkedIn - Vivaldi', 'linkedin'),
        ('(3) Feed | LinkedIn - Vivaldi', 'linkedin'),
        ('Tok informací | LinkedIn - Vivaldi', 'linkedin'),
        ('Zveřejnit | LinkedIn', 'linkedin'),
        ('Jane Doe | LinkedIn - Vivaldi', 'linkedin'),
        ('LinkedIn - Vivaldi', 'linkedin'),
        ('LinkedIn: Log In or Sign Up - Vivaldi', 'linkedin'),
        ('LinkedIn - Wikipedia - Vivaldi', None),
        ('linkedin post tips - Google Search - Vivaldi', None),
        ('linkedin/school-of-sre · GitHub - Vivaldi', None),
        ("Reddit - Wikipedia - Vivaldi", None),
        # X
        ("Home / X - Vivaldi", "x"),
        ("(2) Home / X", "x"),
        ('Name on X: "hello" / X - Vivaldi', "x"),
        ("Xorg - ArchWiki - Vivaldi", None),
        # Instagram (feed only; a bare "Instagram" is also the DM inbox)
        ("Jane (@jane.doe) • Instagram photos and videos - Vivaldi", "instagram"),
        ('Jane on Instagram: "summer" - Vivaldi', "instagram"),
        ("Instagram", None),
        ("Inbox • Direct - Vivaldi", None),
        # games
        ("Play Slither.io on CrazyGames - Vivaldi", "games"),
        ("Poki - Free Online Games - Play Now! - Vivaldi", "games"),
        ("lichess.org • Free Online Chess - Vivaldi", "games"),
        ("Play Chess Online for Free - Chess.com - Vivaldi", "games"),
        ("GeoGuessr - Let's explore the world! - Vivaldi", "games"),
        ("agar.io", "games"),
        ("Krunker - Vivaldi", "games"),
        ("Wordle — The New York Times - Vivaldi", "games"),
        ("Daily Crossword Puzzle - The New York Times - Vivaldi", "games"),
        ("NYT Games - Vivaldi", "games"),
        ("user/agar.io-clone · GitHub – Vivaldi", None),
        ("How brain connections form – The New York Times", None),
        ("Chess engine design.pdf - Vivaldi", None),
        # porn
        ("Free Porn Videos & Sex Movies - Porn, XXX, Porn Tube | Pornhub - Vivaldi", "porn"),
        ("XVIDEOS.COM - Vivaldi", "porn"),
        ("Chaturbate - Free Adult Webcams - Vivaldi", "porn"),
        ("Pornhub fined over age checks - BBC News - Vivaldi", "d:BBC News"),
        # distractions no group blocks
        ("Hacker News - Vivaldi", "d:Hacker News"),
        ("Show HN: A thing | Hacker News - Vivaldi", "d:Hacker News"),
        ("(1) Facebook – Vivaldi", "d:Facebook"),
        ("(2) Twitch – Vivaldi", "d:Twitch"),
        ("streamer - Twitch", "d:Twitch"),
        ("Facebook - Wikipedia - Vivaldi", None),
        ("Twitch - Google Search - Vivaldi", None),
        ("TikTok - Make Your Day - Vivaldi", "d:TikTok"),
        ("Stranger Things | Netflix - Vivaldi", "d:Netflix"),
        ("Netflix Prize data paper.pdf - Vivaldi", None),
        ("Prime Video: The Boys - Vivaldi", "d:Prime Video"),
        ("Home | Disney+ - Vivaldi", "d:Disney+"),
        ("Senate votes on bill | CNN Politics - Vivaldi", "d:CNN Politics"),
        ("Story - BBC News - Vivaldi", "d:BBC News"),
        ("Article | World news | The Guardian - Vivaldi", "d:The Guardian"),
        ("Vláda schválila rozpočet - iDNES.cz - Vivaldi", "d:iDNES.cz"),
        ("Článek | iROZHLAS - spolehlivé zprávy - Vivaldi", "d:iROZHLAS"),
        # neutral
        ("docx comparer - Hledat Googlem - Vivaldi", None),
        ("[2401.12345] Attention Is All You Need - arXiv - Vivaldi", None),
    ]

    def title_hit(self, title):
        for gid, g in self.groups.items():
            if g["enabled"] and self.any_search(g["title_patterns"], title):
                return gid
        for p in self.rx(self.watch["distraction_title_patterns"]):
            m = p.search(title)
            if m:
                return "d:" + m.group(0).strip(" \t-–—|:·")  # same stripping as focus-watch
        return None

    def test_title_table(self):
        self.assertGreaterEqual(len(self.TITLES), 40)
        for title, want in self.TITLES:
            with self.subTest(title=title):
                self.assertEqual(self.title_hit(title), want)

    def test_webapp_classes(self):
        cases = [("youtube", "chrome-youtube.com__-Default", True), ("youtube", "chrome-www.youtube.com__-Default", True),
                 ("youtube", "chrome-music.youtube.com__-Default", False), ("x", "chrome-x.com__-Default", True),
                 ("reddit", "chrome-www.reddit.com__-Default", True)]
        for gid, cls, want in cases:
            self.assertEqual(self.any_search(self.groups[gid]["webapp_classes"], cls), want, f"{gid}: {cls}")

    def classify(self, cls, title):
        """§12 title step with the review amendments, every enabled group locked:
        class exemptions first; group/distraction title patterns only for browsers and web apps."""
        w = self.watch
        if self.any_search(w["never_touch_classes"] + w["work_classes"], cls):
            return "working"
        browserish = self.any_search(w["browser_classes"], cls) or re.match(r"^chrome-.*__", cls)
        for g in self.groups.values():
            if not g["enabled"]:
                continue
            if self.any_search(g["webapp_classes"], cls):
                return "procrastinating"
            if browserish and self.any_search(g["title_patterns"], title):
                return "procrastinating"
        if browserish and self.any_search(w["distraction_title_patterns"], title):
            return "procrastinating"
        if self.any_search(w["work_title_patterns"], title):
            return "working"
        return "undecided"

    def test_title_step_on_real_windows(self):
        cases = [
            ("vivaldi-stable", "What I Found In My Safe - YouTube - Vivaldi", "procrastinating"),
            ("vivaldi-stable", "docx comparer - Hledat Googlem - Vivaldi", "undecided"),
            ("vivaldi-stable", "umap_plot_combined_cd4_cd8.png (7036×7274) - Vivaldi", "undecided"),
            ("com.microsoft.VSCode", "report.md - thesis (Workspace) - Visual Studio Code", "working"),
            ("com.microsoft.VSCode", "Hacker News - Vivaldi.md - Visual Studio Code", "working"),
            ("org.omarchy.claude", "◐ Procrastination blocking system stage", "working"),
            ("org.omarchy.terminal", "Focus", "working"),
            ("org.gnome.Nautilus", "Home", "working"),
            ("chrome-web.whatsapp.com__-Default", "web.whatsapp.com", "undecided"),
            ("chrome-x.com__-Default", "Home / X", "procrastinating"),
            ("chrome-youtube.com__-Default", "What I Found In My Safe - YouTube", "procrastinating"),
            ("vivaldi-stable", "[2401.12345] Attention Is All You Need - arXiv - Vivaldi", "working"),
            ("vivaldi-stable", "2401.12345v2.pdf - Vivaldi", "working"),
            ("vivaldi-stable", "thesis - Overleaf, Online LaTeX Editor - Vivaldi", "working"),
            ("vivaldi-stable", "Show HN: A thing | Hacker News - Vivaldi", "procrastinating"),
            ("foot", "Hacker News", "working"),
            ("mpv", "Twitch - mpv", "undecided"),  # native app: titles don't count, vision decides
            ("vivaldi-stable", "Stranger Things | Netflix - Vivaldi", "procrastinating"),
            ("vivaldi-stable", "Song - YouTube Music - Vivaldi", "undecided"),
            ("chrome-claude.ai__-Default", "Claude", "working"),
            ("vivaldi-stable", "Seurat integration help - Claude - Vivaldi", "working"),
            ("sioyek", "paper.pdf", "working"),
            ("libreoffice-writer", "thesis.odt - LibreOffice Writer", "working"),
            ("md.obsidian.Obsidian", "Daily note - obsidian - Obsidian v1.9", "working"),
        ]
        for cls, title, want in cases:
            self.assertEqual(self.classify(cls, title), want, f"{cls} | {title}")

    def test_watch_classes(self):
        w = self.watch
        self.assertEqual(w["freeze_classes"], [], "no freezing by default (Hyprland ANR dialog)")
        for c in ("vivaldi-stable", "chromium"):
            self.assertTrue(self.any_search(w["browser_classes"], c), c)
            self.assertFalse(self.any_search(w["never_touch_classes"] + w["work_classes"], c), c)
        self.assertFalse(self.any_search(w["browser_classes"], "chrome-x.com__-Default"))
        for c in ("foot", "org.omarchy.terminal", "org.omarchy.claude", "com.microsoft.VSCode", "obsidian",
                  "md.obsidian.Obsidian", "Alacritty", "com.mitchellh.ghostty"):
            self.assertTrue(self.any_search(w["never_touch_classes"], c), c)
        self.assertTrue(re.match(r"^http://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$", w["ollama_url"]))

    # ------------------------------------------------------------ the real loaders
    def test_focusd_loader_reports_no_errors(self):
        sys.path.insert(0, REPO)
        try:
            from focus import config as fconfig  # A's module
        except ImportError as e:
            self.skipTest(f"focus.config not importable yet: {e}")
        finally:
            sys.path.remove(REPO)
        cfg, errors, fatal = fconfig.load(self.CONFIG)
        self.assertFalse(fatal)
        self.assertEqual(errors, [])

    def test_judge_policy(self):
        if not os.path.isfile(self.JUDGE):
            self.skipTest(f"{self.JUDGE} does not exist")
        with open(self.JUDGE, encoding="utf-8") as f:
            text = f.read()
        self.assertGreater(len(text), 1000)
        self.assertLess(len(text), 20000)
        for word in ("grant", "deny", "ask", "Appeals", "<theo>"):
            self.assertIn(word, text)


class ExampleConfigTests(ConfigChecks, unittest.TestCase):
    CONFIG = EXAMPLE_CONFIG
    JUDGE = EXAMPLE_JUDGE
    IS_EXAMPLE = True


@unittest.skipUnless(os.path.isfile(LOCAL_CONFIG), "no local etc/config.toml")
class LocalConfigTests(ConfigChecks, unittest.TestCase):
    CONFIG = LOCAL_CONFIG
    JUDGE = LOCAL_JUDGE


if __name__ == "__main__":
    unittest.main()
