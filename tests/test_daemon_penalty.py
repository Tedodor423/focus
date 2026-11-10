"""focusd as a process: penalize (with DNS-log domain validation), penalty
expiry, appeals, observe and the log op."""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "fakes"))
import daemon_harness as H  # noqa: E402

GRANT = {"decision": "grant", "minutes": 10, "message": "Fair enough, that was work."}
DENY = {"decision": "deny", "minutes": 0, "message": "That was procrastination."}
ASK = {"decision": "ask", "minutes": 0, "message": "Which paper?"}


def pen(domain=None, **kw):
    return {"op": "penalize", "app": "Hacker News", "title": "Hacker News - Vivaldi",
            "description": "a news aggregator", "source": "vision", "domain": domain,
            "reason": "procrastinating 3x", **kw}


class Base(unittest.TestCase):
    def daemon(self, *responses, start=True, **kw):
        d = H.Daemon(responses=list(responses) or None, **kw)
        self.addCleanup(d.cleanup)
        return d.start() if start else d


class Penalize(Base):
    def test_not_enforcing(self):
        d = self.daemon(watch_mode="observe")
        r = d.request(pen())
        self.assertEqual((r["ok"], r["error"]), (False, "not_enforcing"))
        self.assertEqual(d.events("penalty"), [])

    def test_domain_kept_and_blocked(self):
        d = self.daemon(start=False)
        H.fake_dnslog(d.root, [(time.time() - 30, "news.ycombinator.com")])
        d.start()
        t0 = time.time()
        r = d.request(pen("https://www.News.YCombinator.com/item?id=1"))
        p = r["penalty"]
        self.assertTrue(r["ok"])
        self.assertNotIn("domain_dropped", r)
        self.assertRegex(p["id"], r"^p[0-9a-f]{8}$")
        self.assertEqual(p["domain"], "news.ycombinator.com")
        self.assertAlmostEqual(p["until"] - p["since"], 30 * 60, delta=0.01)
        self.assertAlmostEqual(p["since"], t0, delta=2)
        for k in ("app", "title", "description", "source", "reason"):
            self.assertEqual(p[k], pen()[k])
        hosts = d.hosts()
        for line in (b"0.0.0.0 news.ycombinator.com\n", b":: news.ycombinator.com\n",
                     b"0.0.0.0 www.news.ycombinator.com\n", b":: www.news.ycombinator.com\n"):
            self.assertIn(line, hosts)
        self.assertEqual(hosts.index(b"news.ycombinator") > hosts.index(b"poki.com"), True)
        self.assertEqual(d.policy()["URLBlocklist"][-2:],
                         [".news.ycombinator.com", ".www.news.ycombinator.com"])
        ev = d.events("penalty")[0]
        self.assertEqual((ev["id"], ev["domain"]), (p["id"], "news.ycombinator.com"))
        self.assertNotIn("domain_dropped", ev)
        st = d.request({"op": "status"})
        self.assertEqual(st["penalties"], [p])
        self.assertEqual(d.state()["penalties"], [p])

    def test_domain_dropped_branches(self):
        d = self.daemon(GRANT, start=False)
        now = time.time()
        H.fake_dnslog(d.root, [(now - 30, "cdn.reddit.com"), (now - 1200, "stale.example"),
                               (now - 30, "fresh.example")])
        d.start()
        d.request({"op": "unlock", "group": "reddit", "reason": "work thread please"})
        cases = [("not a host!", "invalid"), ("1.2.3.4", "invalid"),
                 ("www.google.com", "never_block"), ("mail.github.com", "never_block"),
                 ("cdn.reddit.com", "unlocked_group")]
        for domain, why in cases:
            r = d.request(pen(domain))
            self.assertIsNone(r["penalty"]["domain"], domain)
            self.assertEqual(r["domain_dropped"], why, domain)
            self.assertEqual(d.events("penalty")[-1]["domain_dropped"], why)
            d.request({"op": "appeal", "penalty": r["penalty"]["id"], "reason": "test cleanup"})
        r = d.request(pen("stale.example"))
        self.assertEqual(r["domain_dropped"], "not_in_dnslog")
        d.request({"op": "appeal", "penalty": r["penalty"]["id"], "reason": "test cleanup"})
        r = d.request(pen(None))
        self.assertIsNone(r["penalty"]["domain"])
        self.assertNotIn("domain_dropped", d.events("penalty")[-1])
        self.assertNotIn(b"stale.example", d.hosts())

    def test_parent_suffix_and_exact_dnslog(self):
        """The ac.uk bypass: a parent of a never_block entry, a public suffix,
        or a name only seen as a subdomain in the DNS log is never blocked."""
        d = self.daemon(GRANT, start=False)
        now = time.time()
        H.fake_dnslog(d.root, [(now - 30, n) for n in (
            "www.example.ac.uk", "ac.uk", "co.uk", "user.github.io", "github.io", "example.net",
            "docs.example.net", "news.ycombinator.com", "www.x.example")])
        d.start()
        cases = [("ac.uk", "too_broad"), ("co.uk", "too_broad"), ("github.io", "too_broad"),
                 ("example.net", "never_block"), ("example.ac.uk", "never_block"),
                 ("ycombinator.com", "not_in_dnslog")]
        for domain, why in cases:
            r = d.request(pen(domain))
            self.assertEqual((r["penalty"]["domain"], r.get("domain_dropped")), (None, why), domain)
            d.request({"op": "appeal", "penalty": r["penalty"]["id"], "reason": "test cleanup"})
        r = d.request(pen("x.example"))                 # www.d in the log is enough
        self.assertEqual(r["penalty"]["domain"], "x.example")
        self.assertEqual([u for u in d.policy()["URLBlocklist"] if "example" in u or "uk" in u],
                         [".x.example", ".www.x.example"])
        self.assertNotIn(b"ac.uk", d.hosts())

    def test_state_domains_revalidated(self):
        d = self.daemon(start=False)
        now = time.time()
        raw = ["ac.uk", "WWW.Fine.Example", "maps.google.com", "bad name!", "ok.example", 7]
        d.write_state({"penalties": [
            {"id": f"p0000000{i}", "since": now - 60, "until": now + 600, "domain": dom,
             "app": "a", "title": "t", "description": "", "source": "title", "reason": ""}
            for i, dom in enumerate(raw)]})
        d.start()
        doms = [p["domain"] for p in d.request({"op": "status"})["penalties"]]
        self.assertEqual(doms, [None, "fine.example", None, None, "ok.example", None])
        self.assertEqual([p["domain"] for p in d.state()["penalties"]], doms)
        hosts = d.hosts()
        self.assertIn(b"0.0.0.0 www.fine.example", hosts)
        self.assertIn(b"0.0.0.0 ok.example", hosts)
        for bad in (b"ac.uk", b"google", b"bad name"):
            self.assertNotIn(bad, hosts)
        self.assertEqual(d.policy()["URLBlocklist"][-4:],
                         [".fine.example", ".www.fine.example", ".ok.example", ".www.ok.example"])

    def test_no_dnslog(self):
        d = self.daemon()
        r = d.request(pen("fresh.example"))
        self.assertEqual((r["penalty"]["domain"], r["domain_dropped"]), (None, "no_dnslog"))

    def test_too_many_penalties(self):
        d = self.daemon()
        ids = [d.request(pen())["penalty"]["id"] for _ in range(3)]
        self.assertEqual(len(set(ids)), 3)
        r = d.request(pen())
        self.assertEqual(r["error"], "too_many_penalties")

    def test_bad_requests(self):
        d = self.daemon()
        for req in (pen(source="screen"), pen(domain=5), pen(app=["x"])):
            self.assertEqual(d.request(req)["error"], "bad_request", req)

    def test_expiry_and_persistence(self):
        d = self.daemon(start=False)
        now = time.time()
        p = {"id": "p00000001", "since": now - 60, "until": now + 1.5, "domain": "x.example",
             "app": "X", "title": "t", "description": "", "source": "title", "reason": ""}
        d.write_state({"penalties": [p]})
        d.start()
        self.assertIn(b"0.0.0.0 www.x.example", d.hosts())
        self.assertEqual(d.request({"op": "status"})["penalties"][0]["id"], "p00000001")
        d.wait_event("penalty_end", id="p00000001", by="expire", timeout=4)
        self.assertNotIn(b"x.example", d.hosts())
        self.assertEqual(d.request({"op": "status"})["penalties"], [])
        self.assertNotIn("x.example", d.policy()["URLBlocklist"])

    def test_penalties_survive_restart_and_shutdown(self):
        d = self.daemon(start=False)
        H.fake_dnslog(d.root, [(time.time(), "fresh.example")])
        d.start()
        pid = d.request(pen("fresh.example"))["penalty"]["id"]
        self.assertEqual(d.stop(), 0)
        self.assertIn(b"0.0.0.0 fresh.example", d.hosts())   # fail closed: still blocked
        d.start()
        self.assertEqual(d.request({"op": "status"})["penalties"][0]["id"], pid)


class Appeals(Base):
    def make(self, *responses, **kw):
        d = self.daemon(*responses, start=False, **kw)
        H.fake_dnslog(d.root, [(time.time(), "fresh.example")])
        d.start()
        return d, d.request(pen("fresh.example"))["penalty"]["id"]

    def test_grant_ends_penalty(self):
        d, pid = self.make(GRANT)
        r = d.request({"op": "appeal", "penalty": pid, "reason": "I was reading a paper on HN."})
        self.assertEqual((r["ok"], r["decision"], r["message"]), (True, "grant", GRANT["message"]))
        self.assertNotIn("granted_minutes", r)
        self.assertNotIn("until", r)
        self.assertEqual(d.request({"op": "status"})["penalties"], [])
        self.assertNotIn(b"fresh.example", d.hosts())
        self.assertEqual(d.events("penalty_end")[-1], {**d.events("penalty_end")[-1],
                                                       "id": pid, "by": "appeal"})
        a = d.events("appeal")[0]
        self.assertEqual((a["penalty"], a["session"], a["reason"]),
                         (pid, r["session"], "I was reading a paper on HN."))
        v = d.events("appeal_verdict")[0]
        self.assertEqual((v["penalty"], v["decision"], v["session"]), (pid, "grant", r["session"]))
        stdin = d.judge_calls()[0]["stdin"]
        self.assertIn("Was this actually work or a necessary short break?", stdin)
        self.assertIn('- app: "Hacker News"', stdin)
        self.assertIn('- window title: "Hacker News - Vivaldi"', stdin)
        self.assertIn('- description: "a news aggregator"', stdin)
        self.assertIn("- detected by: vision check", stdin)
        self.assertIn("- penalty time left: 30 min", stdin)
        self.assertIn("<theo>I was reading a paper on HN.</theo>", stdin)
        r2 = d.request({"op": "appeal", "penalty": pid, "reason": "again please"})
        self.assertEqual(r2["error"], "unknown_penalty")

    def test_deny_cooldown(self):
        d, pid = self.make(DENY)
        r = d.request({"op": "appeal", "penalty": pid, "reason": "it was work, honestly"})
        self.assertEqual(r["decision"], "deny")
        self.assertEqual(len(d.request({"op": "status"})["penalties"]), 1)
        r2 = d.request({"op": "appeal", "penalty": pid, "reason": "it was work, honestly"})
        self.assertEqual((r2["decision"], r2["rule"]), ("refused", "cooldown"))
        self.assertAlmostEqual(r2["retry_after"], d.events("appeal_verdict")[0]["t"] + 300, delta=1)
        self.assertEqual(d.events("refused")[-1], {**d.events("refused")[-1],
                                                   "penalty": pid, "rule": "cooldown"})
        self.assertEqual(len(d.judge_calls()), 1)

    def test_ask_reply_and_rules(self):
        d, pid = self.make(ASK, GRANT)
        r = d.request({"op": "appeal", "penalty": pid, "reason": "x"})
        self.assertEqual(r["error"], "bad_request")                     # reason too short
        r = d.request({"op": "appeal", "penalty": pid, "reason": "research for my thesis"})
        self.assertEqual((r["decision"], r["message"]), ("ask", "Which paper?"))
        r = d.request({"op": "reply", "session": r["session"], "answer": "The attention one."})
        self.assertEqual(r["decision"], "grant")
        self.assertEqual(d.events("followup")[0]["answer"], "The attention one.")
        self.assertEqual(d.request({"op": "appeal", "penalty": "pzzzz", "reason": "abc"})["error"],
                         "unknown_penalty")

    def test_appeal_rate_limit_shared(self):
        d, pid = self.make(DENY, calls=1)
        d.request({"op": "unlock", "group": "reddit", "reason": "uses the only call"})
        r = d.request({"op": "appeal", "penalty": pid, "reason": "was work"})
        self.assertEqual((r["decision"], r["rule"]), ("refused", "rate_limit"))


class ObserveAndLog(Base):
    def obs(self, d, verdict="working", app="foot", title="vim", **kw):
        return d.request({"op": "observe", "verdict": verdict, "source": "title", "app": app,
                          "class": "foot", "title": title, "description": "",
                          "domain": None, "confidence": 1.0, "streak": 0, **kw})

    def test_observe_dedup_and_flags(self):
        d = self.daemon()
        st = d.request({"op": "status"})["watch"]
        self.assertEqual(st, {"mode": "enforce", "title_interval_seconds": 60,
                              "last_seen": None, "last": None, "flags_today": 0})
        self.assertEqual(self.obs(d), {"ok": True})
        self.obs(d)                                   # identical: not logged
        self.obs(d, title="other file")               # changed title: logged
        for _ in range(2):                            # procrastinating: always logged
            self.obs(d, "procrastinating", "YouTube", "Cats - YouTube", streak=2)
        self.obs(d, "procrastinating", "YouTube", "x" * 400)
        evs = d.events("observe")
        self.assertEqual([e["verdict"] for e in evs],
                         ["working", "working", "procrastinating", "procrastinating",
                          "procrastinating"])
        self.assertEqual(len(evs[-1]["title"]), 300)
        self.assertEqual((evs[2]["class"], evs[2]["streak"], evs[2]["confidence"]),
                         ("foot", 2, 1.0))
        w = d.request({"op": "status"})["watch"]
        self.assertEqual(w["flags_today"], 3)
        self.assertEqual((w["last"]["verdict"], w["last"]["app"], w["last"]["source"]),
                         ("procrastinating", "YouTube", "title"))
        self.assertAlmostEqual(w["last_seen"], time.time(), delta=3)
        self.assertEqual(w["last"]["t"], w["last_seen"])
        self.assertEqual(self.obs(d, "bored")["error"], "bad_request")
        self.assertEqual(self.obs(d, source="psychic")["error"], "bad_request")

    def test_flags_today_counts_previous_runs(self):
        d = self.daemon(start=False)
        now = time.time()
        d.write_events([{"t": now - 3 * 86400, "type": "observe", "verdict": "procrastinating"},
                        {"t": now - 100, "type": "observe", "verdict": "procrastinating"},
                        {"t": now - 90, "type": "observe", "verdict": "working"},
                        {"t": now - 50, "type": "stop"}])
        d.start()
        self.assertEqual(d.request({"op": "status"})["watch"]["flags_today"], 1)

    def test_log(self):
        d = self.daemon()
        self.obs(d)
        self.obs(d, title="b")
        d.request({"op": "lock", "group": "all"})
        r = d.request({"op": "log"})
        types = [e["type"] for e in r["events"]]
        self.assertEqual(types[0], "start")
        self.assertIn("observe", types)
        r = d.request({"op": "log", "types": ["observe"], "limit": 1})
        self.assertEqual([e["title"] for e in r["events"]], ["b"])
        r = d.request({"op": "log", "types": ["observe"]})
        self.assertEqual([e["title"] for e in r["events"]], ["vim", "b"])   # oldest first
        since = d.events("observe")[1]["t"]
        r = d.request({"op": "log", "since": since, "types": ["observe"]})
        self.assertEqual([e["title"] for e in r["events"]], ["b"])
        self.assertEqual(len(d.request({"op": "log", "limit": 9999})["events"]),
                         len(d.events()))
        for bad in ({"limit": "5"}, {"since": "x"}, {"types": "observe"}):
            self.assertEqual(d.request({"op": "log", **bad})["error"], "bad_request")


if __name__ == "__main__":
    unittest.main()
