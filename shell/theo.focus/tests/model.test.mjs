// Unit tests for Model.js (the plugin's pure logic):
//   node --test shell/theo.focus/tests/
import { test } from "node:test"
import assert from "node:assert/strict"
import { readFileSync } from "node:fs"
import { dirname, join } from "node:path"
import { fileURLToPath } from "node:url"

const here = dirname(fileURLToPath(import.meta.url))
const NAMES = [
  "WARN_SECONDS", "GLYPH_LOCK", "GLYPH_UNLOCK", "GLYPH_ALERT", "GLYPH_PENALTY", "safeId", "safeAddress",
  "escapeMarkup", "oneLine", "parseStatus", "normalizeStatus", "activeUnlocks", "activePenalties",
  "monitorProblem", "shortLeft", "longLeft", "barView", "newMemo", "diff", "warnNotice", "relockNotice",
  "penaltyEndNotice", "penaltyLines", "compileRegexes", "windowsToClose", "closeDispatch", "terminalArgv",
  "notifyArgv", "TERMINAL_SCRIPT", "TERMINAL_SCRIPT_WAIT", "focusArgs", "healthNotice", "cardScreen", "shownFromText", "shownToText"
]
const M = (() => {
  const src = readFileSync(join(here, "..", "Model.js"), "utf8").replace(/^\.pragma library\s*$/m, "")
  return new Function(src + "\nreturn {" + NAMES.map((n) => `${n}: ${n}`).join(",") + "}")()
})()

const NOW = 1793900000

function raw(over = {}) {
  return Object.assign({
    ok: true, version: "0.1.0", mode: "light", now: NOW, started: NOW - 10000, day_start: NOW - 40000,
    groups: [
      { id: "youtube", label: "YouTube", enabled: true, unlockable: true, locked: true, unlocked_until: null,
        used_today_min: 15, daily_minutes: 40, max_minutes: 20, cooldown_until: null,
        webapp_classes: ["^chrome-(www\\.)?youtube\\.com__"], hosts_count: 0, url_block: ["youtube.com"], url_allow: [] },
      { id: "reddit", label: "Reddit", enabled: true, unlockable: true, locked: true, unlocked_until: null,
        used_today_min: 0, daily_minutes: 40, max_minutes: 20, cooldown_until: null,
        webapp_classes: ["(?i)^chrome-(www\\.)?reddit\\.com__"] },
      { id: "porn", label: "Porn", enabled: true, unlockable: false, locked: true, unlocked_until: null, webapp_classes: [] }
    ],
    unlocks: [], lists: {}, judge_calls_last_hour: 0, penalties: [],
    watch: { mode: "observe", title_interval_seconds: 60, last_seen: NOW - 20,
             last: { t: NOW - 20, verdict: "working", app: "foot", title: "x", source: "title" }, flags_today: 2 }
  }, over)
}
const ok = (o) => M.parseStatus(0, JSON.stringify(o)).status

function unlocked(o, id, until) {
  const g = o.groups.find((x) => x.id === id)
  g.locked = false
  g.unlocked_until = until
  o.unlocks.push({ group: id, label: g.label, since: until - 900, until, minutes: 15, reason: "r" })
  return o
}

const PEN = { id: "p1a2b3c4", since: NOW - 60, until: NOW + 1740, domain: "news.ycombinator.com", app: "Hacker News",
              title: "Hacker News - Vivaldi", description: "news aggregator", source: "title", reason: "streak" }

test("parseStatus: ok, unreachable, errors", () => {
  assert.equal(M.parseStatus(0, JSON.stringify(raw())).kind, "ok")
  assert.equal(M.parseStatus(3, '{"ok":false,"error":"daemon_unreachable"}\n').kind, "unreachable")
  assert.equal(M.parseStatus(3, "").kind, "unreachable")
  assert.equal(M.parseStatus(1, '{"ok":false,"error":"daemon_unreachable"}').kind, "unreachable")
  const e = M.parseStatus(1, '{"ok":false,"error":"forbidden","message":"not allowed"}')
  assert.deepEqual([e.kind, e.message], ["error", "not allowed"])
  assert.equal(M.parseStatus(0, "garbage").kind, "error")
  assert.equal(M.parseStatus(-2, "").kind, "error")
  assert.match(M.parseStatus(-1, "").message, /timed out/)
  assert.equal(M.parseStatus(0, '{"ok":true}').kind, "error")
})

test("normalizeStatus drops bad ids and bounds strings", () => {
  const o = raw({ penalties: [PEN, { ...PEN, id: "p; rm -rf /" }, { ...PEN, id: "p2", title: "x".repeat(5000) }] })
  o.groups.push({ id: "../evil", label: "x" })
  const s = ok(o)
  assert.equal(s.groups.length, 3)
  assert.deepEqual(s.penalties.map((p) => p.id), ["p1a2b3c4", "p2"])
  assert.equal(s.penalties[1].title.length, 300)
  assert.equal(s.watch.title_interval_seconds, 60)
})

test("bar: quiet lock when everything is locked", () => {
  const v = M.barView("ok", ok(raw()), "", NOW)
  assert.equal(v.text, M.GLYPH_LOCK)
  assert.equal(v.dimmed, true)
  assert.equal(v.warning, false)
  assert.match(v.tooltip, /everything locked/)
  assert.match(v.tooltip, /Monitor: observe/)
})

test("bar: soonest unlock first, +n for more, penalties counted", () => {
  const o = unlocked(unlocked(raw(), "reddit", NOW + 1500), "youtube", NOW + 700)
  o.penalties = [PEN]
  const v = M.barView("ok", ok(o), "", NOW)
  assert.equal(v.text, M.GLYPH_UNLOCK + " YouTube 12m +1 " + M.GLYPH_PENALTY + " 1")
  assert.equal(v.dimmed, false)
  assert.match(v.tooltip, /YouTube unlocked · 12 min left/)
  assert.match(v.tooltip, /Penalty: Hacker News · 29 min left \(p1a2b3c4\)/)
  assert.match(v.tooltip, /Middle-click: appeal/)
  assert.equal(M.barView("ok", ok(unlocked(raw(), "youtube", NOW + 45)), "", NOW).text, M.GLYPH_UNLOCK + " YouTube 45s")
})

test("bar: warning when unreachable or erroring", () => {
  const u = M.barView("unreachable", null, "focusd is not reachable", NOW)
  assert.equal(u.warning, true)
  assert.equal(u.text, M.GLYPH_ALERT)
  assert.match(u.tooltip, /daemon is not reachable/)
  const e = M.barView("error", ok(raw()), "the focus command could not be started", NOW)
  assert.equal(e.warning, true)
  assert.match(e.tooltip, /could not be started/)
  assert.equal(M.barView("init", null, "", NOW).dimmed, true)
})

test("monitor warning: stale, null after grace, off", () => {
  const stale = raw(); stale.watch.last_seen = NOW - 181
  assert.match(M.monitorProblem(ok(stale), NOW), /not reporting \(last check 3 min ago\)/)
  const v = M.barView("ok", ok(stale), "", NOW)
  assert.equal(v.warning, true)
  assert.equal(v.text, M.GLYPH_ALERT)
  assert.match(v.tooltip, /screen monitor is not reporting/)
  const fresh = raw(); fresh.watch.last_seen = NOW - 179
  assert.equal(M.monitorProblem(ok(fresh), NOW), "")
  const nul = raw(); nul.watch.last_seen = null; nul.watch.last = null
  assert.match(M.monitorProblem(ok(nul), NOW), /no check since focusd started/)
  nul.started = NOW - 100   // grace: daemon just (re)started
  assert.equal(M.monitorProblem(ok(nul), NOW), "")
  const off = raw(); off.watch.mode = "off"; off.watch.last_seen = null
  assert.equal(M.monitorProblem(ok(off), NOW), "")
  // a warning with an unlock still shows the unlock
  const both = unlocked(raw(), "youtube", NOW + 600); both.watch.last_seen = NOW - 999
  assert.equal(M.barView("ok", ok(both), "", NOW).text, M.GLYPH_ALERT + " YouTube 10m")
})

test("diff: re-lock detected only for unlocked -> locked", () => {
  const memo = M.newMemo()
  const a = ok(unlocked(raw(), "youtube", NOW + 600))
  assert.equal(M.diff(null, a, NOW, memo).relocked.length, 0)       // first poll: nothing
  const b = ok(raw())
  const d = M.diff(a, b, NOW + 10, memo)
  assert.deepEqual(d.relocked.map((g) => g.id), ["youtube"])
  // disabled -> enabled is not a re-lock
  const c = raw(); c.groups[1].enabled = false; c.groups[1].locked = false
  assert.equal(M.diff(ok(c), ok(raw()), NOW, memo).relocked.length, 0)
})

test("diff: 2-minute warning fires once, only if seen above 2 min", () => {
  const memo = M.newMemo()
  const until = NOW + 125
  const s = ok(unlocked(raw(), "youtube", until))
  assert.equal(M.diff(null, s, NOW, memo).warn.length, 0)           // 125 s left: armed
  const w = M.diff(s, s, NOW + 6, memo).warn                         // 119 s left
  assert.equal(w.length, 1)
  assert.equal(M.warnNotice(w[0], NOW + 6).headline, "YouTube locks in 2 min")
  assert.equal(M.diff(s, s, NOW + 16, memo).warn.length, 0)         // once only
  // extended: new until -> armed again
  const ext = ok(unlocked(raw(), "youtube", until + 600))
  assert.equal(M.diff(s, ext, NOW + 20, memo).warn.length, 0)
  assert.equal(M.diff(ext, ext, NOW + 610, memo).warn.length, 1)
  // a 90-second grant never warns
  const memo2 = M.newMemo()
  const short = ok(unlocked(raw(), "reddit", NOW + 90))
  assert.equal(M.diff(null, short, NOW, memo2).warn.length, 0)
  assert.equal(M.diff(short, short, NOW + 10, memo2).warn.length, 0)
})

test("diff: new penalties shown once, ended ones reported as over or lifted", () => {
  const memo = M.newMemo()
  const p2 = { ...PEN, id: "p2", since: NOW - 10, until: NOW + 5 }
  const s = ok(raw({ penalties: [PEN, p2] }))
  const d1 = M.diff(null, s, NOW, memo)
  assert.deepEqual(d1.fresh.map((p) => p.id), ["p1a2b3c4", "p2"])   // oldest first
  assert.equal(M.diff(s, s, NOW + 1, memo).fresh.length, 0)
  const later = ok(raw({ penalties: [] }))
  const d2 = M.diff(s, later, NOW + 10, memo)
  const byId = Object.fromEntries(d2.ended.map((p) => [p.id, p.lifted]))
  assert.deepEqual(byId, { p1a2b3c4: true, p2: false })
  assert.equal(M.penaltyEndNotice(d2.ended.find((p) => p.id === "p2")).headline, "Hacker News penalty over")
  assert.equal(M.penaltyEndNotice(d2.ended.find((p) => p.id === "p1a2b3c4")).headline, "Hacker News penalty lifted")
  assert.deepEqual(Object.keys(memo.shown), [])
})

test("windowsToClose: class regexes, (?i), music stays, odd addresses skipped", () => {
  const clients = JSON.stringify([
    { address: "0x55aa01", class: "chrome-www.youtube.com__-Default", title: "Video - YouTube" },
    { address: "0x55aa02", class: "chrome-youtube.com__-Default", title: "YouTube" },
    { address: "0x55aa03", class: "chrome-music.youtube.com__-Default", title: "YouTube Music" },
    { address: "0x55aa04", class: "CHROME-WWW.REDDIT.COM__-Default", title: "reddit" },
    { address: "0x55aa05; rm", class: "chrome-www.youtube.com__-Default", title: "evil" },
    { address: "0x55aa06", class: "foot", title: "foot" },
    "junk"
  ])
  const s = ok(raw())
  const r = M.windowsToClose(clients, s.groups.slice(0, 2))
  assert.deepEqual(r.windows.map((w) => w.address), ["0x55aa01", "0x55aa02", "0x55aa04"])
  assert.equal(M.windowsToClose("nope", s.groups).error !== "", true)
  assert.equal(M.windowsToClose(clients, [s.groups[2]]).windows.length, 0)
  assert.deepEqual(M.compileRegexes(["(unclosed"]).bad, ["(unclosed"])
  assert.equal(M.closeDispatch("0x55aa01"), 'hl.dsp.window.close({ window = "address:0x55aa01" })')
  assert.equal(M.closeDispatch('0x1" }) hl.exec_cmd("x'), "")
})

test("terminal and notify argv: --pause, no interpolation into the script", () => {
  const a = M.terminalArgv("/usr/local/bin/focus", ["appeal", "p1a2b3c4"], "")
  assert.deepEqual(a.slice(0, 8), ["setsid", "uwsm-app", "--", "xdg-terminal-exec", "--app-id=org.omarchy.terminal", "--title=Focus", "-e", "bash"])
  assert.equal(a[9], M.TERMINAL_SCRIPT)
  assert.deepEqual(a.slice(10), ["focus-terminal", "/usr/local/bin/focus", "appeal", "--pause", "p1a2b3c4"])
  assert.ok(!M.TERMINAL_SCRIPT.includes("read"))          // the CLI's --pause waits instead
  assert.deepEqual(M.terminalArgv("/f", ["unlock"], "").slice(10), ["focus-terminal", "/f", "unlock", "--pause"])
  const w = M.terminalArgv("/f", ["watch"], "")
  assert.equal(w[9], M.TERMINAL_SCRIPT_WAIT)                // no --pause there: the script waits
  assert.deepEqual(w.slice(10), ["focus-terminal", "/f", "watch"])
  assert.deepEqual(M.terminalArgv("/f", ["unlock"], "/tmp/term"), ["/tmp/term", "/f", "unlock", "--pause"])
  assert.deepEqual(M.focusArgs(["appeal"]), ["appeal", "--pause"])
  const n = M.notifyArgv("/n", M.relockNotice([{ label: "YouTube" }], 2))
  assert.deepEqual(n, ["/n", "-g", M.GLYPH_LOCK, "-u", "normal", "YouTube is locked again", "Closed 2 windows."])
  assert.equal(M.relockNotice([{ label: "A" }, { label: "B" }], 0).headline, "A, B are locked again")
  assert.equal(M.escapeMarkup("<img src=x> & co"), "&lt;img src=x&gt; &amp; co")
  assert.equal(M.safeId("p1a2b3c4"), true)
  assert.equal(M.safeId("-x; y"), false)
})

test("penaltyLines", () => {
  const l = M.penaltyLines(PEN, NOW)
  assert.equal(l.headline, "Caught: Hacker News")
  assert.equal(l.title, "Hacker News - Vivaldi")
  assert.equal(l.blocked, "Blocked for 29 min.")
  assert.match(l.detail, /^news\.ycombinator\.com is blocked · Caught by the window title · at \d\d:\d\d$/)
  const v = M.penaltyLines({ ...PEN, source: "vision", domain: null, title: "Hacker News", app: "Hacker News", description: "orange site\nlist" }, NOW)
  assert.equal(v.title, "")
  assert.match(v.detail, /^Screenshot: orange site list/)
})

test("health: warning state, tooltip, notify only on a new non-empty set", () => {
  const h = raw({ health: ["hosts: refusing to write (two BEGIN markers)", "judge: not logged in", 7] })
  const v = M.barView("ok", ok(h), "", NOW)
  assert.equal(v.warning, true)
  assert.equal(v.text, M.GLYPH_ALERT)
  assert.match(v.tooltip, /Focus problem: hosts: refusing to write \(two BEGIN markers\)/)
  assert.match(v.tooltip, /Focus problem: judge: not logged in/)
  assert.equal(ok(raw()).health.length, 0)                       // missing field = empty
  assert.equal(M.barView("ok", ok(raw({ health: [] })), "", NOW).warning, false)
  const memo = M.newMemo()
  const a = ok(h), b = ok(raw({ health: ["judge: not logged in", "hosts: refusing to write (two BEGIN markers)"] }))
  assert.equal(M.diff(null, a, NOW, memo).health.length, 2)      // new set: notify
  assert.equal(M.diff(a, a, NOW + 10, memo).health.length, 0)    // same set: quiet
  assert.equal(M.diff(a, b, NOW + 20, memo).health.length, 0)    // same set, other order: quiet
  const c = ok(raw({ health: ["judge: not logged in"] }))
  assert.deepEqual(M.diff(b, c, NOW + 30, memo).health, ["judge: not logged in"])   // changed set: notify
  const clean = ok(raw())
  assert.equal(M.diff(c, clean, NOW + 40, memo).health.length, 0)   // cleared: no notification
  assert.equal(M.diff(clean, c, NOW + 50, memo).health.length, 1)   // back again: notify
  const n = M.healthNotice(["policy: <x> & y"])
  assert.equal(n.headline, "Focus has a problem")
  assert.equal(n.body, "policy: &lt;x&gt; &amp; y")
})

test("overlay card screen falls back when the focused output disappears", () => {
  assert.equal(M.cardScreen(["eDP-1", "DP-3"], "DP-3"), "DP-3")
  assert.equal(M.cardScreen(["eDP-1"], "DP-3"), "eDP-1")          // undocked
  assert.equal(M.cardScreen(["eDP-1"], ""), "eDP-1")
  assert.equal(M.cardScreen([], "DP-3"), "")
})

test("shown penalty ids round-trip through the runtime file, validated", () => {
  const shown = M.shownFromText('{"ids":["p1a2b3c4","bad id; rm",7,"p2"]}')
  assert.deepEqual(Object.keys(shown).sort(), ["p1a2b3c4", "p2"])
  assert.equal(M.shownToText(shown), '{"ids":["p1a2b3c4","p2"]}\n')
  assert.deepEqual(M.shownFromText(""), {})
  assert.deepEqual(M.shownFromText("garbage"), {})
  // a seeded memo suppresses the overlay for an already shown penalty
  const memo = M.newMemo(); memo.shown = M.shownFromText('{"ids":["p1a2b3c4"]}')
  assert.equal(M.diff(null, ok(raw({ penalties: [PEN] })), NOW, memo).fresh.length, 0)
})
