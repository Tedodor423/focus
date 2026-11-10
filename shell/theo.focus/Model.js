.pragma library

// Pure helpers for the theo.focus plugin (Service.qml, BarWidget.qml,
// PenaltyCard.qml): parsing `focus status --json`, the bar text and tooltip,
// what changed between two polls (re-locks, 2-minute warnings, new and ended
// penalties), Hyprland window matching and the floating terminal argv.
// No QML types here, so tests/model.test.mjs runs it under node.
//
// Everything that comes out of the daemon is treated as untrusted display
// text: strings are bounded, ids validated, and nothing is ever put into a
// shell command line (argv lists only).

var WARN_SECONDS = 120          // notify this long before an unlock expires
var LIFTED_SLACK_SECONDS = 15   // a penalty gone earlier than this before its end was lifted
var MAX_STR = 300
var MAX_ITEMS = 64

var GLYPH_LOCK = String.fromCodePoint(0xf033e)     // nf-md-lock
var GLYPH_UNLOCK = String.fromCodePoint(0xf0fc6)   // nf-md-lock_open_variant
var GLYPH_ALERT = String.fromCodePoint(0xf0026)    // nf-md-alert
var GLYPH_PENALTY = String.fromCodePoint(0xf00ad)  // nf-md-block_helper (the spec's "⛔n", as a bar-font glyph)
var GLYPH_TIMER = String.fromCodePoint(0xf051b)    // nf-md-timer_outline

// ----------------------------------------------------------------- values

function isObj(v) { return v !== null && typeof v === "object" && !Array.isArray(v) }
function num(v) { return typeof v === "number" && isFinite(v) ? v : null }
function int0(v) { var n = num(v); return n === null ? 0 : Math.round(n) }
function str(v, max) {
  if (typeof v !== "string") return ""
  var m = max || MAX_STR
  return v.length > m ? v.slice(0, m) : v
}
function strOrNull(v, max) { return typeof v === "string" && v !== "" ? str(v, max) : null }
function bool(v) { return v === true }
function arr(v, max) { return Array.isArray(v) ? v.slice(0, max || MAX_ITEMS) : [] }

// Penalty ids are "p" + 8 hex chars; group ids ^[a-z0-9_-]{1,32}$. Anything
// that ends up in an argv is checked against this anyway.
function safeId(v) { return typeof v === "string" && /^[A-Za-z0-9_-]{1,64}$/.test(v) }

// Hyprland window addresses as `hyprctl clients -j` prints them.
function safeAddress(v) { return typeof v === "string" && /^0x[0-9a-fA-F]{1,16}$/.test(v) }

// Notification bodies are rendered as (a safe subset of) markup by the
// Omarchy notification daemon; escape so "<b>" in a window title stays text.
function escapeMarkup(s) {
  return String(s === undefined || s === null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
}

// One line, bounded: window titles may contain newlines or be huge.
function oneLine(s, max) {
  var t = String(s === undefined || s === null ? "" : s).replace(/[\r\n\t]+/g, " ").trim()
  var m = max || 120
  return t.length > m ? t.slice(0, m - 1) + "…" : t
}

// --------------------------------------------------------------- status

function normalizeGroup(g) {
  if (!isObj(g) || !safeId(g.id)) return null
  var classes = []
  var raw = arr(g.webapp_classes, 32)
  for (var i = 0; i < raw.length; i++) if (typeof raw[i] === "string" && raw[i] !== "") classes.push(str(raw[i]))
  return {
    id: g.id,
    label: str(g.label, 80) || g.id,
    enabled: bool(g.enabled),
    unlockable: bool(g.unlockable),
    locked: bool(g.locked),
    unlocked_until: num(g.unlocked_until),
    used_today_min: int0(g.used_today_min),
    daily_minutes: int0(g.daily_minutes),
    max_minutes: int0(g.max_minutes),
    cooldown_until: num(g.cooldown_until),
    webapp_classes: classes
  }
}

function normalizeUnlock(u) {
  if (!isObj(u) || !safeId(u.group)) return null
  var until = num(u.until)
  if (until === null) return null
  return {
    group: u.group,
    label: str(u.label, 80) || u.group,
    since: num(u.since),
    until: until,
    minutes: int0(u.minutes)
  }
}

function normalizePenalty(p) {
  if (!isObj(p) || !safeId(p.id)) return null
  var until = num(p.until)
  if (until === null) return null
  return {
    id: p.id,
    since: num(p.since),
    until: until,
    domain: strOrNull(p.domain, 253),
    app: str(p.app),
    title: str(p.title),
    description: str(p.description),
    source: str(p.source, 16),
    reason: str(p.reason)
  }
}

function normalizeWatch(w) {
  if (!isObj(w)) return null
  var mode = str(w.mode, 16)
  if (["off", "observe", "nudge", "enforce"].indexOf(mode) === -1) mode = "observe"
  var last = isObj(w.last) ? {
    t: num(w.last.t), verdict: str(w.last.verdict, 32), app: str(w.last.app),
    title: str(w.last.title), source: str(w.last.source, 16)
  } : null
  var iv = num(w.title_interval_seconds)
  return {
    mode: mode,
    title_interval_seconds: iv !== null && iv > 0 ? iv : 60,
    last_seen: num(w.last_seen),
    last: last,
    flags_today: int0(w.flags_today)
  }
}

// status.health: persistent daemon problems (§7); missing = none.
function normalizeHealth(h) {
  var out = []
  var raw = arr(h, 32)
  for (var i = 0; i < raw.length; i++) {
    var t = oneLine(str(raw[i]), 200)
    if (t && out.indexOf(t) === -1) out.push(t)
  }
  return out
}

function normalizeStatus(o) {
  var groups = [], unlocks = [], penalties = []
  var i, x
  var rg = arr(o.groups)
  for (i = 0; i < rg.length; i++) { x = normalizeGroup(rg[i]); if (x) groups.push(x) }
  var ru = arr(o.unlocks)
  for (i = 0; i < ru.length; i++) { x = normalizeUnlock(ru[i]); if (x) unlocks.push(x) }
  var rp = arr(o.penalties)
  for (i = 0; i < rp.length; i++) { x = normalizePenalty(rp[i]); if (x) penalties.push(x) }
  return {
    version: str(o.version, 32),
    mode: str(o.mode, 16),
    now: num(o.now),
    started: num(o.started),
    groups: groups,
    unlocks: unlocks,
    penalties: penalties,
    judge_calls_last_hour: int0(o.judge_calls_last_hour),
    health: normalizeHealth(o.health),
    watch: normalizeWatch(o.watch)
  }
}

// Exit code + stdout of `focus status --json` -> one of
//   { kind: "ok", status }            the daemon answered
//   { kind: "unreachable", message }  exit 3 / daemon_unreachable (tamper warning)
//   { kind: "error", message }        anything else (CLI missing, timeout, junk)
// code -1 = our watchdog killed it, -2 = it never started.
function parseStatus(code, text) {
  var obj = null
  try { obj = JSON.parse(String(text || "").trim()) } catch (e) { obj = null }
  if (code === 3 || (isObj(obj) && obj.ok === false && obj.error === "daemon_unreachable"))
    return { kind: "unreachable", message: "focusd is not reachable" }
  if (code === -2) return { kind: "error", message: "the focus command could not be started" }
  if (code === -1) return { kind: "error", message: "focus status timed out" }
  if (!isObj(obj)) return { kind: "error", message: "unreadable output from focus status (exit " + code + ")" }
  if (obj.ok !== true)
    return { kind: "error", message: oneLine(str(obj.message) || str(obj.error) || ("focus status failed (exit " + code + ")"), 160) }
  if (!Array.isArray(obj.groups)) return { kind: "error", message: "focus status returned no groups" }
  return { kind: "ok", status: normalizeStatus(obj) }
}

// ------------------------------------------------------------- derived

// Active unlocks, soonest expiry first. `unlocks` is authoritative; groups
// that say they are unlocked but are missing from it are added from the
// group record so the bar never hides an open group.
function activeUnlocks(status, nowSec) {
  if (!status) return []
  var out = [], seen = {}
  var i
  for (i = 0; i < status.unlocks.length; i++) {
    var u = status.unlocks[i]
    if (u.until > nowSec && !seen[u.group]) { out.push(u); seen[u.group] = true }
  }
  for (i = 0; i < status.groups.length; i++) {
    var g = status.groups[i]
    if (g.enabled && !g.locked && g.unlocked_until !== null && g.unlocked_until > nowSec && !seen[g.id]) {
      out.push({ group: g.id, label: g.label, since: null, until: g.unlocked_until, minutes: 0 })
      seen[g.id] = true
    }
  }
  out.sort(function(a, b) { return a.until - b.until })
  return out
}

function activePenalties(status, nowSec) {
  if (!status) return []
  var out = []
  for (var i = 0; i < status.penalties.length; i++)
    if (status.penalties[i].until > nowSec) out.push(status.penalties[i])
  // Newest first: that is the one `focus appeal` without an id picks.
  out.sort(function(a, b) { return (b.since || 0) - (a.since || 0) })
  return out
}

// "" when fine, else why the monitor counts as not reporting. A daemon that
// started less than 3 intervals ago gets a grace period (last_seen is null
// until focus-watch's first check after a daemon restart).
function monitorProblem(status, nowSec) {
  if (!status || !status.watch) return ""
  var w = status.watch
  if (w.mode === "off") return ""
  var limit = 3 * w.title_interval_seconds
  if (w.last_seen === null) {
    if (status.started !== null && nowSec - status.started < limit) return ""
    return "the screen monitor is not reporting (no check since focusd started)"
  }
  var age = nowSec - w.last_seen
  if (age > limit) return "the screen monitor is not reporting (last check " + formatAgo(age) + " ago)"
  return ""
}

// ------------------------------------------------------------ formatting

function pad2(n) { return (n < 10 ? "0" : "") + n }

function clockTime(epochSec) {
  var d = new Date(epochSec * 1000)
  return pad2(d.getHours()) + ":" + pad2(d.getMinutes())
}

// Bar countdown: "12m" (rounded up, so it never shows 0m while open), "45s".
function shortLeft(seconds) {
  var s = Math.max(0, Math.ceil(seconds))
  if (s < 60) return s + "s"
  return Math.ceil(s / 60) + "m"
}

// "12 min", "1 min", "40 s"
function longLeft(seconds) {
  var s = Math.max(0, Math.ceil(seconds))
  if (s < 60) return s + " s"
  return Math.ceil(s / 60) + " min"
}

function formatAgo(seconds) {
  var s = Math.max(0, Math.round(seconds))
  if (s < 90) return s + " s"
  if (s < 5400) return Math.round(s / 60) + " min"
  return Math.round(s / 3600) + " h"
}

// ------------------------------------------------------------- bar view

// phase: "init" | "ok" | "unreachable" | "error"
// Returns { text, lines (vertical bar), tooltip, warning, dimmed }.
function barView(phase, status, errorMessage, nowSec) {
  var hints = "Click: unlock · Right-click: lock all now"
  if (phase === "init") {
    return { text: GLYPH_LOCK, lines: [GLYPH_LOCK], tooltip: "Focus · checking…", warning: false, dimmed: true }
  }
  if (phase !== "ok" || !status) {
    var why = phase === "unreachable"
      ? "Focus: the daemon is not reachable (focusd stopped?). Blocking may be off."
      : "Focus: " + (errorMessage || "status unavailable")
    return { text: GLYPH_ALERT, lines: [GLYPH_ALERT], tooltip: why + "\n" + hints, warning: true, dimmed: false }
  }

  var unlocks = activeUnlocks(status, nowSec)
  var pens = activePenalties(status, nowSec)
  var problem = monitorProblem(status, nowSec)
  var health = status.health || []
  var warn = problem !== "" || health.length > 0

  var parts = [warn ? GLYPH_ALERT : (unlocks.length ? GLYPH_UNLOCK : GLYPH_LOCK)]
  var lines = [parts[0]]
  if (unlocks.length) {
    var first = unlocks[0]
    var left = shortLeft(first.until - nowSec)
    parts.push(oneLine(first.label, 24) + " " + left + (unlocks.length > 1 ? " +" + (unlocks.length - 1) : ""))
    lines.push(left)
  }
  if (pens.length) {
    // A space: the glyph is wider than its cell and would cover the digit.
    parts.push(GLYPH_PENALTY + " " + pens.length)
    lines.push(GLYPH_PENALTY + " " + pens.length)
  }

  var tip = []
  for (var h = 0; h < health.length; h++) tip.push("Focus problem: " + health[h])
  if (problem) tip.push("Focus: " + problem + ".")
  tip.push(unlocks.length ? "Focus · " + unlocks.length + " unlocked" : "Focus · everything locked")
  for (var i = 0; i < unlocks.length; i++) {
    var u = unlocks[i]
    tip.push(oneLine(u.label, 40) + " unlocked · " + longLeft(u.until - nowSec) + " left (until " + clockTime(u.until) + ")")
  }
  for (var j = 0; j < pens.length; j++) {
    var p = pens[j]
    tip.push("Penalty: " + oneLine(p.app || p.title || "?", 60) + " · " + longLeft(p.until - nowSec) + " left (" + p.id + ")")
  }
  var w = status.watch
  if (w) {
    var mon = "Monitor: " + w.mode
    if (w.mode !== "off") {
      mon += w.last_seen !== null ? " · last check " + formatAgo(nowSec - w.last_seen) + " ago" : " · no check yet"
      mon += " · " + w.flags_today + " flagged today"
    }
    tip.push(mon)
  }
  tip.push(hints + (pens.length ? " · Middle-click: appeal" : " · Middle-click: monitor log"))

  return {
    text: parts.join(" "),
    lines: lines,
    tooltip: tip.join("\n"),
    warning: warn,
    dimmed: !warn && !unlocks.length && !pens.length
  }
}

// ---------------------------------------------------------- transitions

function newMemo() { return { armed: {}, warned: {}, shown: {}, healthKey: "" } }

function groupMap(status) {
  var m = {}
  if (status) for (var i = 0; i < status.groups.length; i++) m[status.groups[i].id] = status.groups[i]
  return m
}

// What happened between the last good status (`prev`, null on the first
// poll) and `next`. Mutates `memo` (warning arming, overlay ids shown this
// session). Returns:
//   relocked: groups that went unlocked -> locked (close their web apps)
//   warn:     unlocks that just crossed WARN_SECONDS before expiry
//   ended:    penalties that disappeared ({ ...penalty, lifted })
//   fresh:    active penalties not shown in the overlay yet
function diff(prev, next, nowSec, memo) {
  var out = { relocked: [], warn: [], ended: [], fresh: [], health: [] }
  var i
  if (prev) {
    var before = groupMap(prev)
    for (i = 0; i < next.groups.length; i++) {
      var g = next.groups[i], pg = before[g.id]
      if (pg && pg.enabled && !pg.locked && g.locked) out.relocked.push(g)
    }
    var nowIds = {}
    for (i = 0; i < next.penalties.length; i++) nowIds[next.penalties[i].id] = true
    for (i = 0; i < prev.penalties.length; i++) {
      var pp = prev.penalties[i]
      if (!nowIds[pp.id]) {
        var ended = {}
        for (var k in pp) ended[k] = pp[k]
        ended.lifted = pp.until - nowSec > LIFTED_SLACK_SECONDS
        out.ended.push(ended)
      }
    }
  }

  var keys = {}
  var unlocks = activeUnlocks(next, nowSec)
  for (i = 0; i < unlocks.length; i++) {
    var u = unlocks[i]
    var key = u.group + "@" + Math.round(u.until)
    keys[key] = true
    var rem = u.until - nowSec
    if (rem > WARN_SECONDS) memo.armed[key] = true
    else if (rem > 0 && memo.armed[key] && !memo.warned[key]) {
      memo.warned[key] = true
      out.warn.push(u)
    }
  }
  for (var a in memo.armed) if (!keys[a]) delete memo.armed[a]
  for (var w in memo.warned) if (!keys[w]) delete memo.warned[w]

  var pens = activePenalties(next, nowSec)
  var listed = {}
  for (i = 0; i < next.penalties.length; i++) listed[next.penalties[i].id] = true
  // Oldest first, so a queue shows them in the order they happened.
  for (i = pens.length - 1; i >= 0; i--) {
    if (!memo.shown[pens[i].id]) {
      memo.shown[pens[i].id] = true
      out.fresh.push(pens[i])
    }
  }
  for (var s in memo.shown) if (!listed[s]) delete memo.shown[s]

  // Health: notify only when the set of problems changes to a new non-empty
  // set (an empty set in between makes the same problem new again).
  var hk = (next.health || []).slice().sort().join("\n")
  if (hk !== memo.healthKey) {
    memo.healthKey = hk
    if (hk) out.health = next.health.slice()
  }
  return out
}

// Overlay ids already shown, kept in a small file under $XDG_RUNTIME_DIR so a
// shell restart doesn't show them again (cleared on reboot).
function shownFromText(text) {
  var o = null
  try { o = JSON.parse(String(text || "")) } catch (e) { o = null }
  var ids = isObj(o) ? arr(o.ids, MAX_ITEMS) : []
  var out = {}
  for (var i = 0; i < ids.length; i++) if (safeId(ids[i])) out[ids[i]] = true
  return out
}

function shownToText(shown) {
  return JSON.stringify({ ids: Object.keys(shown).filter(safeId).sort().slice(0, MAX_ITEMS) }) + "\n"
}

// ------------------------------------------------------------ notifications

// [headline, body] pairs. Headlines are shown as plain text by the Omarchy
// notification daemon; bodies are markup, hence escapeMarkup.
function warnNotice(u, nowSec) {
  return { glyph: GLYPH_TIMER, urgency: "normal",
           headline: oneLine(u.label, 40) + " locks in " + longLeft(u.until - nowSec),
           body: escapeMarkup("Wrap up. Its web app windows close when it locks.") }
}

function relockNotice(groups, closedCount) {
  var names = []
  for (var i = 0; i < groups.length; i++) names.push(oneLine(groups[i].label, 40))
  var body = closedCount > 0 ? "Closed " + closedCount + (closedCount === 1 ? " window." : " windows.") : ""
  return { glyph: GLYPH_LOCK, urgency: "normal",
           headline: names.join(", ") + (groups.length === 1 ? " is" : " are") + " locked again",
           body: escapeMarkup(body) }
}

function healthNotice(list) {
  return { glyph: GLYPH_ALERT, urgency: "normal",
           headline: list.length === 1 ? "Focus has a problem" : "Focus has " + list.length + " problems",
           body: escapeMarkup(list.join("\n")) }
}

function penaltyEndNotice(p) {
  return { glyph: GLYPH_LOCK, urgency: "normal",
           headline: oneLine(p.app || p.title || "Focus", 60) + " penalty " + (p.lifted ? "lifted" : "over"),
           body: "" }
}

// ------------------------------------------------------------ penalty card

function penaltyLines(p, nowSec) {
  if (!p) return { headline: "", title: "", blocked: "", detail: "" }
  var app = oneLine(p.app || "something distracting", 80)
  var title = oneLine(p.title, 160)
  var mins = Math.max(1, Math.ceil((p.until - nowSec) / 60))
  var detail = []
  if (p.domain) detail.push(oneLine(p.domain, 80) + " is blocked")
  if (p.source === "vision" && p.description) detail.push("Screenshot: " + oneLine(p.description, 160))
  else if (p.source === "title") detail.push("Caught by the window title")
  if (p.since !== null) detail.push("at " + clockTime(p.since))
  return {
    headline: "Caught: " + app,
    title: title && title !== app ? title : "",
    blocked: "Blocked for " + mins + " min.",
    detail: detail.join(" · ")
  }
}

// The screen the overlay card goes on: the focused one while it still
// exists, else the first screen left ("" only with no screens at all).
function cardScreen(names, active) {
  if (active && names.indexOf(active) !== -1) return active
  return names.length ? names[0] : ""
}

// ------------------------------------------------------------ hyprland

// Python-style regexes from config.toml -> RegExp. A leading "(?i)" becomes
// the i flag; anything V4 cannot compile is skipped (reported in `bad`).
function compileRegexes(patterns) {
  var out = [], bad = []
  for (var i = 0; i < patterns.length; i++) {
    var src = String(patterns[i]), flags = ""
    if (src.indexOf("(?i)") === 0) { src = src.slice(4); flags = "i" }
    var re = null
    try { re = new RegExp(src, flags + "u") } catch (e) {
      try { re = new RegExp(src, flags) } catch (e2) { re = null }
    }
    if (re) out.push(re)
    else bad.push(String(patterns[i]))
  }
  return { regexes: out, bad: bad }
}

// `hyprctl clients -j` output + the re-locked groups -> windows to close.
function windowsToClose(clientsJson, groups) {
  var clients
  try { clients = JSON.parse(String(clientsJson || "")) } catch (e) { return { windows: [], bad: [], error: "unreadable hyprctl clients output" } }
  if (!Array.isArray(clients)) return { windows: [], bad: [], error: "hyprctl clients did not return a list" }
  var patterns = []
  for (var i = 0; i < groups.length; i++) patterns = patterns.concat(groups[i].webapp_classes || [])
  var compiled = compileRegexes(patterns)
  var out = []
  if (!compiled.regexes.length) return { windows: out, bad: compiled.bad, error: "" }
  for (var j = 0; j < clients.length; j++) {
    var c = clients[j]
    if (!isObj(c) || !safeAddress(c.address)) continue
    var cls = typeof c["class"] === "string" ? c["class"] : ""
    if (!cls) continue
    for (var k = 0; k < compiled.regexes.length; k++) {
      if (compiled.regexes[k].test(cls)) {
        out.push({ address: c.address, cls: cls, title: str(c.title) })
        break
      }
    }
  }
  return { windows: out, bad: compiled.bad, error: "" }
}

// The Lua dispatch Omarchy uses (omarchy-hyprland-window-close-all).
function closeDispatch(address) {
  if (!safeAddress(address)) return ""
  return 'hl.dsp.window.close({ window = "address:' + address + '" })'
}

// ------------------------------------------------------------ terminal

// The floating terminal: Omarchy's org.omarchy.terminal app-id gets the
// floating-window rule. The command runs from "$@" (argv, never pasted into
// the script) and the bar is told to refresh when it ends. `focus unlock` and
// `focus appeal` get --pause (the CLI waits for Enter so the judge's verdict
// stays readable); for anything else the script itself waits for a key
// (not after Ctrl-C).
var TERMINAL_SCRIPT = '"$@"; omarchy-shell -q theo.focus refresh >/dev/null 2>&1 &'
var TERMINAL_SCRIPT_WAIT = '"$@"; code=$?; omarchy-shell -q theo.focus refresh >/dev/null 2>&1 & '
  + 'if [ "$code" -ne 130 ]; then printf \'\\n\\033[2mPress any key to close\\033[0m\'; read -rsn1; fi'

// ["unlock"] -> ["unlock", "--pause"]; ["appeal", id] -> ["appeal", "--pause", id]
function focusArgs(args) {
  var a = args.slice()
  if (a[0] === "unlock" || a[0] === "appeal") a.splice(1, 0, "--pause")
  return a
}

function terminalArgv(focusBin, args, override) {
  var a = focusArgs(args)
  var cmd = [String(focusBin)].concat(a)
  if (override) return [String(override)].concat(cmd)
  var script = a.indexOf("--pause") !== -1 ? TERMINAL_SCRIPT : TERMINAL_SCRIPT_WAIT
  return ["setsid", "uwsm-app", "--", "xdg-terminal-exec", "--app-id=org.omarchy.terminal", "--title=Focus",
          "-e", "bash", "-c", script, "focus-terminal"].concat(cmd)
}

function notifyArgv(notifyBin, notice) {
  var argv = [String(notifyBin)]
  if (notice.glyph) argv.push("-g", notice.glyph)
  argv.push("-u", notice.urgency || "normal", notice.headline)
  if (notice.body) argv.push(notice.body)
  return argv
}
