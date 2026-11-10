import QtQuick
import Quickshell
import Quickshell.Io
import "Model.js" as Model

// Headless singleton for theo.focus. Polls `focus status --json` every 10 s
// (and right after its own actions), and from what changed between polls:
//   - notifies 2 min before an unlock expires, on re-lock, and when a penalty ends
//   - closes Hyprland windows matching a re-locked group's webapp_classes
//   - queues new penalties for the full-screen overlay (Overlay.qml)
// Opens the floating terminal for `focus unlock` / `focus appeal <id>` and runs
// `focus lock all`. Contract: docs/ARCHITECTURE.md §10 in the focus repo.
//
// Dev overrides (environment of the Quickshell process; unset in production):
//   FOCUS_BIN              focus CLI           (default /usr/local/bin/focus)
//   FOCUS_HYPRCTL          hyprctl             (default hyprctl)
//   FOCUS_PLUGIN_NOTIFY    omarchy-notification-send compatible notifier
//   FOCUS_PLUGIN_TERMINAL  program run as <it> <focus> <args...> instead of the floating terminal
//   FOCUS_PLUGIN_POLL_MS   poll interval       (default 10000, min 1000)
//   FOCUS_PLUGIN_STATE     shown-penalty file  (default $XDG_RUNTIME_DIR/theo-focus-shown.json)
Item {
  id: root

  property var shell: null
  property var manifest: null
  property string omarchyPath: Quickshell.env("OMARCHY_PATH") || "/usr/share/omarchy"

  readonly property string pluginId: (manifest && manifest.id) ? manifest.id : "theo.focus"
  property string focusBin: Quickshell.env("FOCUS_BIN") || "/usr/local/bin/focus"
  property string hyprctl: Quickshell.env("FOCUS_HYPRCTL") || "hyprctl"
  property string notifyBin: Quickshell.env("FOCUS_PLUGIN_NOTIFY") || (root.omarchyPath + "/bin/omarchy-notification-send")
  property string terminalOverride: Quickshell.env("FOCUS_PLUGIN_TERMINAL") || ""
  readonly property int pollMs: {
    var n = parseInt(Quickshell.env("FOCUS_PLUGIN_POLL_MS") || "", 10)
    return isFinite(n) && n >= 1000 ? n : 10000
  }

  // ------------------------------------------------------------ public state
  // "init" until the first poll, then "ok" | "unreachable" | "error".
  property string phase: "init"
  property string errorMessage: ""
  property var status: null          // last good, normalized status
  property double lastOkAt: 0
  property double clock: Date.now()  // ticks every second for countdowns
  readonly property double nowSec: clock / 1000
  readonly property var view: Model.barView(phase, status, errorMessage, nowSec)
  readonly property var penalties: Model.activePenalties(status, nowSec)
  property bool busy: false          // a lock-all is in flight

  // Overlay: queue of penalty records still to show; [0] is on screen.
  property var overlayQueue: []
  property bool overlayOpen: false
  readonly property var currentPenalty: overlayQueue.length ? overlayQueue[0] : null
  readonly property string currentPenaltyId: currentPenalty ? currentPenalty.id : ""
  // Keys and clicks on the overlay count only after this, so typing that was
  // in flight when it appeared doesn't dismiss it unseen.
  property bool overlayArmed: false
  property int armDelayMs: 700
  // Hard safety limit: the overlay closes by itself after this, whatever happens.
  property int overlayTimeoutMs: 60000
  property string shownPath: Quickshell.env("FOCUS_PLUGIN_STATE")
    || ((Quickshell.env("XDG_RUNTIME_DIR") || "/tmp") + "/theo-focus-shown.json")

  // ---------------------------------------------------------------- private
  property var _memo: Model.newMemo()
  property bool _polling: false
  property bool _pollAgain: false
  property bool _closingOverlay: false
  property int _lostTicks: 0
  property bool _seeded: false
  property string _shownText: ""

  onCurrentPenaltyIdChanged: {
    root.overlayArmed = false
    if (root.currentPenaltyId) armTimer.restart()
    else armTimer.stop()
  }
  onOverlayOpenChanged: {
    if (root.overlayOpen) overlayTimeout.restart()
    else overlayTimeout.stop()
  }

  Timer {
    id: armTimer
    interval: root.armDelayMs
    onTriggered: root.overlayArmed = true
  }

  Timer {
    id: overlayTimeout
    interval: root.overlayTimeoutMs
    onTriggered: {
      console.warn("theo.focus: penalty overlay open for " + Math.round(root.overlayTimeoutMs / 1000) + " s; closing it")
      root.overlayQueue = []
      root._hideOverlay()
    }
  }

  FileView {
    id: shownFile
    path: root.shownPath
    blockLoading: true
    atomicWrites: true
    printErrors: false
  }

  // Seed the overlay memo from the runtime file before the first poll.
  function _seed() {
    if (root._seeded) return
    root._seeded = true
    var text = ""
    try { text = shownFile.text() } catch (e) { text = "" }
    root._memo.shown = Model.shownFromText(text)
    root._shownText = Model.shownToText(root._memo.shown)
  }

  function _saveShown() {
    var text = Model.shownToText(root._memo.shown)
    if (text === root._shownText) return
    root._shownText = text
    try { shownFile.setText(text) } catch (e) { console.warn("theo.focus: could not save shown penalties: " + e) }
  }

  // ------------------------------------------------------------- processes

  Component {
    id: jobComponent

    Process {
      id: job
      property var onDone: null
      property bool finished: false
      property bool started: false
      property int timeoutMs: 20000
      property string label: ""

      stdout: StdioCollector { id: jobOut; waitForEnd: true }
      stderr: StdioCollector { id: jobErr; waitForEnd: true }

      onStarted: job.started = true
      onExited: function(code, status) { Qt.callLater(function() { job.complete(code) }) }
      // A program that cannot be started never emits exited.
      onRunningChanged: if (!running && !job.started) Qt.callLater(function() { if (!job.started) job.complete(-2) })

      function complete(code) {
        if (finished) return
        finished = true
        var res = { code: code, out: jobOut.text || "", err: jobErr.text || "" }
        var cb = onDone
        onDone = null
        Qt.callLater(function() { job.destroy() })
        if (cb) {
          try { cb(res) } catch (e) { console.warn("theo.focus: callback failed: " + e + "\n" + (e.stack || "")) }
        }
      }

      property Timer watchdog: Timer {
        interval: job.timeoutMs
        running: true
        onTriggered: {
          if (job.finished) return
          console.warn("theo.focus: process timed out: " + job.label)
          if (job.running) job.signal(15)
          job.complete(-1)
        }
      }
    }
  }

  function _spawn(command, cb, timeoutMs) {
    var j = jobComponent.createObject(root, {
      command: command, onDone: cb, timeoutMs: timeoutMs || 20000, label: command.slice(0, 3).join(" ")
    })
    if (!j) {
      console.warn("theo.focus: could not create a process for " + command[0])
      if (cb) Qt.callLater(function() { cb({ code: -2, out: "", err: "" }) })
      return
    }
    j.running = true
  }

  function _notify(notice) {
    if (!notice || !notice.headline) return
    Quickshell.execDetached(Model.notifyArgv(root.notifyBin, notice))
  }

  // --------------------------------------------------------------- polling

  function refresh() {
    root._seed()
    if (root._polling) { root._pollAgain = true; return }
    root._polling = true
    _spawn([root.focusBin, "status", "--json"], root._onStatus, 20000)
  }

  function _onStatus(res) {
    root._polling = false
    var r = Model.parseStatus(res.code, res.out)
    var now = Date.now() / 1000
    root.clock = Date.now()
    if (r.kind === "ok") {
      var changes = Model.diff(root.status, r.status, now, root._memo)
      root.status = r.status
      root.lastOkAt = Date.now()
      root.errorMessage = ""
      root.phase = "ok"
      root._apply(changes, now)
      root._saveShown()
    } else {
      if (root.phase !== r.kind || root.errorMessage !== r.message)
        console.warn("theo.focus: " + r.kind + ": " + r.message + (res.err ? " | " + String(res.err).slice(0, 300) : ""))
      root.phase = r.kind
      root.errorMessage = r.message
    }
    if (root._pollAgain) {
      root._pollAgain = false
      Qt.callLater(root.refresh)
    }
  }

  function _apply(changes, now) {
    var i
    for (i = 0; i < changes.warn.length; i++) _notify(Model.warnNotice(changes.warn[i], now))
    if (changes.relocked.length) _closeWebapps(changes.relocked)
    for (i = 0; i < changes.ended.length; i++) _notify(Model.penaltyEndNotice(changes.ended[i]))
    if (changes.health.length) _notify(Model.healthNotice(changes.health))
    _syncOverlay(changes.fresh)
  }

  // Re-lock: close matching web app windows first, then one notification
  // that says how many were closed.
  function _closeWebapps(groups) {
    var withClasses = groups.filter(function(g) { return g.webapp_classes.length > 0 })
    if (!withClasses.length) { _notify(Model.relockNotice(groups, 0)); return }
    _spawn([root.hyprctl, "clients", "-j"], function(res) {
      var found = res.code === 0 ? Model.windowsToClose(res.out, withClasses) : { windows: [], bad: [], error: "hyprctl clients failed (exit " + res.code + ")" }
      if (found.error) console.warn("theo.focus: " + found.error)
      if (found.bad.length) console.warn("theo.focus: skipped webapp_classes JS cannot compile: " + found.bad.join(", "))
      for (var i = 0; i < found.windows.length; i++) {
        var arg = Model.closeDispatch(found.windows[i].address)
        if (arg) Quickshell.execDetached([root.hyprctl, "dispatch", arg])
      }
      root._notify(Model.relockNotice(groups, found.windows.length))
    }, 10000)
  }

  // ---------------------------------------------------------------- overlay

  // Drop queued penalties that are no longer active, add the new ones, and
  // open or close the overlay to match.
  function _syncOverlay(fresh) {
    var active = {}
    var list = root.status ? Model.activePenalties(root.status, Date.now() / 1000) : []
    for (var i = 0; i < list.length; i++) active[list[i].id] = list[i]
    var q = []
    for (var j = 0; j < root.overlayQueue.length; j++) {
      // Keep the queued object itself, so the card on screen doesn't see a change.
      if (active[root.overlayQueue[j].id]) q.push(root.overlayQueue[j])
    }
    for (var k = 0; k < fresh.length; k++) {
      var dup = false
      for (var m = 0; m < q.length; m++) if (q[m].id === fresh[k].id) dup = true
      if (!dup) q.push(fresh[k])
    }
    root.overlayQueue = q
    if (q.length && !root.overlayOpen) root._showOverlay()
    else if (!q.length && root.overlayOpen) root._hideOverlay()
  }

  function _showOverlay() {
    root.overlayOpen = true
    root._lostTicks = 0
    var ok = false
    try {
      if (root.shell && typeof root.shell.isPluginOpen === "function" && root.shell.isPluginOpen(root.pluginId)) return
      ok = !!(root.shell && typeof root.shell.summon === "function" && root.shell.summon(root.pluginId, "{}"))
    } catch (e) {
      console.warn("theo.focus: summon threw: " + e)
    }
    if (!ok) {
      // focus-watch already sent a critical notification at enforcement time.
      console.warn("theo.focus: could not show the penalty overlay (summon failed)")
      root.overlayOpen = false
      root.overlayQueue = []
    }
  }

  function _hideOverlay() {
    if (root._closingOverlay) return
    root._closingOverlay = true
    var wasOpen = root.overlayOpen
    root.overlayOpen = false
    if (wasOpen) {
      try {
        if (root.shell && typeof root.shell.hide === "function") root.shell.hide(root.pluginId)
      } catch (e) { console.warn("theo.focus: hide threw: " + e) }
    }
    root._closingOverlay = false
  }

  // "Back to work" / Esc: next queued penalty, or close.
  function dismissCurrent() {
    var q = root.overlayQueue.slice(1)
    root.overlayQueue = q
    if (!q.length) root._hideOverlay()
  }

  // "Appeal": close the overlay entirely (it would sit above the terminal and
  // keep the keyboard), then `focus appeal <id>`. Other queued penalties stay
  // marked as shown; the bar lists them and middle-click appeals the newest.
  function appealCurrent() {
    var p = root.currentPenalty
    root.overlayQueue = []
    root._hideOverlay()
    if (p) root.appeal(p.id)
  }

  // The shell asked us to close (another overlay took over, `shell hide`).
  function closeFromShell() {
    if (root._closingOverlay || !root.overlayOpen) return
    root.overlayQueue = []
    root.overlayOpen = false
  }

  // The shell summoned the overlay itself (`omarchy-shell shell summon theo.focus`):
  // show the newest active penalty, or nothing.
  function openFromShell() {
    if (root.overlayOpen) return true
    var list = Model.activePenalties(root.status, Date.now() / 1000)
    if (!list.length) return false
    root.overlayQueue = [list[0]]
    root.overlayOpen = true
    root._lostTicks = 0
    return true
  }

  // ---------------------------------------------------------------- actions

  function _terminal(args) {
    Quickshell.execDetached(Model.terminalArgv(root.focusBin, args, root.terminalOverride))
  }

  function openUnlock() { root._terminal(["unlock"]) }

  function appeal(id) {
    if (id !== undefined && id !== null && id !== "") {
      if (!Model.safeId(String(id))) { console.warn("theo.focus: refusing odd penalty id"); return }
      root._terminal(["appeal", String(id)])
    } else {
      root._terminal(["appeal"])
    }
  }

  function appealLatest() {
    var list = Model.activePenalties(root.status, Date.now() / 1000)
    root.appeal(list.length ? list[0].id : "")
  }

  function openWatch() { root._terminal(["watch"]) }

  function lockAll() {
    if (root.busy) return
    root.busy = true
    _spawn([root.focusBin, "lock", "all"], function(res) {
      root.busy = false
      if (res.code !== 0) {
        var r = Model.parseStatus(res.code, res.out)
        root._notify({ glyph: Model.GLYPH_ALERT, urgency: "normal", headline: "Focus: lock all failed",
                       body: Model.escapeMarkup(r.kind === "unreachable" ? "The focus daemon is not reachable." : ("Exit " + res.code + ". " + Model.oneLine(res.err || res.out, 160))) })
      }
      root.refresh()
    }, 30000)
  }

  function statusObject() {
    return {
      phase: root.phase, error: root.errorMessage, bar: root.view.text, tooltip: root.view.tooltip,
      warning: root.view.warning, overlayOpen: root.overlayOpen,
      overlayQueue: root.overlayQueue.map(function(p) { return p.id }),
      shown: Object.keys(root._memo.shown), focusBin: root.focusBin,
      lastOkAt: root.lastOkAt
    }
  }

  // ------------------------------------------------------------------ timers

  Timer {
    interval: root.pollMs
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: root.refresh()
  }

  Timer {
    interval: 1000
    running: true
    repeat: true
    onTriggered: {
      root.clock = Date.now()
      // Summoned but nothing on screen (Overlay.qml failed to load, or the
      // shell dropped it): forget it so the next penalty can open it again.
      if (root.overlayOpen && root.shell && typeof root.shell.isPluginOpen === "function") {
        root._lostTicks = root.shell.isPluginOpen(root.pluginId) ? 0 : root._lostTicks + 1
        if (root._lostTicks >= 3) {
          console.warn("theo.focus: the penalty overlay is not open (failed to load?); dropping it")
          root._lostTicks = 0
          root.overlayQueue = []
          root.overlayOpen = false
        }
      }
    }
  }

  // ------------------------------------------------------------------- IPC
  // omarchy-shell theo.focus unlock|lockAll|appeal|refresh|status|dismiss

  IpcHandler {
    target: "theo.focus"

    function unlock(): void { root.openUnlock() }
    function lockAll(): void { root.lockAll() }
    function appeal(): void { root.appealLatest() }
    function watch(): void { root.openWatch() }
    function refresh(): void { root.refresh() }
    function dismiss(): void { root.dismissCurrent() }
    function status(): string { return JSON.stringify(root.statusObject()) }
  }
}
