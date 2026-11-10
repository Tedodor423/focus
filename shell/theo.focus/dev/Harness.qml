import QtQuick
import Quickshell
import qs.Commons
import qs.Commons as Commons
import "../Model.js" as Model
import ".." as F

// Offscreen walk through the real Service.qml, BarWidget.qml and
// PenaltyCard.qml against dev/fakes.py (canned `focus status --json`,
// canned `hyprctl clients -j`, logged dispatch/notifications/terminals).
// Hosted by dev/harness.sh in a throwaway Quickshell config; prints
// PASS/FAIL lines and saves PNGs to $FOCUS_DEV_DIR/shots.
Item {
  id: h
  width: 960
  height: 700

  readonly property string dir: Quickshell.env("FOCUS_DEV_DIR") || "/tmp/theo-focus-dev"
  property double base: 0
  property int failures: 0
  property int summons: 0
  property int hides: 0
  property bool shellOpen: false
  property int stepIndex: -1

  function readFile(path) {
    try {
      var xhr = new XMLHttpRequest()
      xhr.open("GET", "file://" + path, false)
      xhr.send()
      return xhr.status === 200 || xhr.status === 0 ? xhr.responseText : ""
    } catch (e) { return "" }
  }

  function logOf(role) {
    var out = []
    var lines = readFile(h.dir + "/" + role + ".log").split("\n")
    for (var i = 0; i < lines.length; i++) if (lines[i]) out.push(JSON.parse(lines[i]))
    return out
  }

  function has(list, pred) { for (var i = 0; i < list.length; i++) if (pred(list[i])) return true; return false }
  function last(list) { return list.length ? list[list.length - 1] : null }
  function eq(a, b) { return JSON.stringify(a) === JSON.stringify(b) }

  function check(name, ok, detail) {
    if (!ok) h.failures++
    console.log((ok ? "PASS " : "FAIL ") + name + (detail !== undefined ? "  " + detail : ""))
  }

  function shot(name) {
    h.grabToImage(function(result) { result.saveToFile(h.dir + "/shots/" + name + ".png") })
  }

  function key(k) { return cardLoader.item ? cardLoader.item.handleKey({ key: k, accepted: false }) : false }

  // ------------------------------------------------------------ fakes

  QtObject {
    id: fakeShell
    function summon(id, payload) { h.summons++; h.shellOpen = true; return true }
    function hide(id) { h.hides++; h.shellOpen = false; return true }
    function isPluginOpen(id) { return h.shellOpen }
    function serviceFor(id) { return svc }
  }

  property var _svc2: null
  property int _summons2: 0
  QtObject {
    id: fakeShell2
    function summon(id, payload) { h._summons2++; return true }
    function hide(id) { return true }
    function isPluginOpen(id) { return false }
    function serviceFor(id) { return h._svc2 }
  }

  QtObject {
    id: fakeBar
    property var shell: fakeShell
    property color foreground: Commons.Color.bar.text
    property color barForeground: Commons.Color.bar.text
    property color background: Commons.Color.bar.background
    property color urgent: Commons.Color.bar.active
    property string fontFamily: Style.font.family
    property bool vertical: false
    property int barSize: Style.bar.sizeHorizontal
    property bool foregroundAnimationEnabled: false
    function showTooltip(t, s) {}
    function hideTooltip(t) {}
    function registerClickTarget(t) {}
    function unregisterClickTarget(t) {}
    function moduleWidgets(id) { return [] }
  }

  F.Service {
    id: svc
    shell: fakeShell
  }
  // PenaltyCard has its own `svc` property; bind through this name instead.
  readonly property var theService: svc

  // ------------------------------------------------------------ scene

  Rectangle {
    anchors.fill: parent
    gradient: Gradient {
      GradientStop { position: 0; color: "#3b4261" }
      GradientStop { position: 1; color: "#1a1b26" }
    }
    Text { x: 40; y: 80; text: "news.ycombinator.com — Hacker News"; color: "#c0caf5"; font.family: "monospace"; font.pixelSize: 18 }
  }

  Rectangle {
    id: barStrip
    width: parent.width
    height: Style.bar.sizeHorizontal
    color: Commons.Color.bar.background
    Text {
      anchors.left: parent.left; anchors.leftMargin: 12; anchors.verticalCenter: parent.verticalCenter
      text: "bar ›"; color: Commons.Color.bar.text; font.family: Style.font.family; font.pixelSize: Style.font.body
    }
    F.BarWidget {
      id: widget
      bar: fakeBar
      anchors.centerIn: parent
      height: parent.height
    }
    Text {
      anchors.right: parent.right; anchors.rightMargin: 12; anchors.verticalCenter: parent.verticalCenter
      width: parent.width / 2 - widget.width; elide: Text.ElideRight; horizontalAlignment: Text.AlignRight
      textFormat: Text.PlainText
      text: "tooltip: " + svc.view.tooltip.split("\n")[0]
      color: Commons.Color.bar.text; font.family: Style.font.family; font.pixelSize: Style.font.caption
    }
  }

  // What Overlay.qml shows on the focused monitor.
  Item {
    anchors.fill: parent
    anchors.topMargin: barStrip.height
    visible: svc.overlayOpen
    Rectangle { anchors.fill: parent; color: Util.alpha(Commons.Color.background, 0.85) }
    Loader {
      id: cardLoader
      active: svc.overlayOpen
      anchors.centerIn: parent
      sourceComponent: F.PenaltyCard { service: h.theService; availableWidth: h.width }
    }
  }

  // ------------------------------------------------------------ steps
  // { name, scenario?, refresh (default true), at? (seconds after base to wait for),
  //   before?(), wait ms (default 1500), check(), shot? }

  readonly property var steps: [
    { name: "locked", scenario: "locked", shot: "bar-locked", check: function() {
      check("first poll ok", svc.phase === "ok", svc.phase + " " + svc.errorMessage)
      check("quiet lock glyph", svc.view.text === Model.GLYPH_LOCK && svc.view.dimmed && !svc.view.warning, svc.view.text)
      check("no overlay, nothing closed", h.summons === 0 && logOf("hyprctl").length === 0)
    } },
    { name: "unlocked", scenario: "unlocked", shot: "bar-unlocked", check: function() {
      var t = svc.view.text
      check("unlock text, soonest first, +1", t.indexOf(Model.GLYPH_UNLOCK + " YouTube ") === 0 && /m \+1$/.test(t), t)
      check("tooltip lists both", /YouTube unlocked/.test(svc.view.tooltip) && /Reddit unlocked/.test(svc.view.tooltip))
      check("no warning yet", logOf("notify").length === 0)
    } },
    { name: "two-minute warning", at: 22, check: function() {
      var n = logOf("notify")
      check("warns 2 min before expiry", has(n, function(a) { return a.indexOf("YouTube locks in 2 min") !== -1 }), JSON.stringify(n))
    } },
    { name: "warning only once", check: function() {
      check("no second warning", logOf("notify").length === 1)
    } },
    { name: "relocked", scenario: "relocked", wait: 2500, check: function() {
      var hy = logOf("hyprctl")
      var d = hy.filter(function(a) { return a[0] === "dispatch" }).map(function(a) { return a[1] })
      check("asked hyprctl for clients", has(hy, function(a) { return eq(a, ["clients", "-j"]) }))
      d.sort()
      check("closed exactly the YouTube web apps", eq(d, [
        'hl.dsp.window.close({ window = "address:0x55aa01" })',
        'hl.dsp.window.close({ window = "address:0x55aa02" })']), JSON.stringify(d))
      var n = last(logOf("notify"))
      check("re-lock notification", n && n.indexOf("YouTube is locked again") !== -1 && n.indexOf("Closed 2 windows.") !== -1, JSON.stringify(n))
    } },
    { name: "penalty", scenario: "penalty", wait: 400, shot: "overlay-penalty", check: function() {
      check("overlay summoned", h.summons === 1 && svc.overlayOpen && eq(svc.overlayQueue.map(function(p) { return p.id }), ["p1a2b3c4"]))
      check("bar counts the penalty", svc.view.text.indexOf(Model.GLYPH_PENALTY + " 1") !== -1, svc.view.text)
      key(Qt.Key_Escape)
      check("keys ignored right after it appears", svc.overlayOpen)
    } },
    { name: "esc = back to work", refresh: false, wait: 900, before: function() {}, check: function() {
      key(Qt.Key_Escape)
      check("Esc closes", !svc.overlayOpen && h.hides === 1 && svc.overlayQueue.length === 0)
    } },
    { name: "shown once per session", check: function() {
      check("no second overlay for the same penalty", h.summons === 1 && !svc.overlayOpen)
    } },
    { name: "shown ids saved", refresh: false, wait: 100, check: function() {
      check("runtime file lists the shown penalty", readFile(h.dir + "/shown.json").indexOf("p1a2b3c4") !== -1, readFile(h.dir + "/shown.json"))
    } },
    { name: "a restarted service doesn't re-show it", refresh: false, wait: 1500, before: function() {
      var c = Qt.createComponent(Qt.resolvedUrl("../Service.qml"))
      h._svc2 = c.createObject(h, { shell: fakeShell2 })
      if (!h._svc2) console.log("svc2: " + c.errorString())
    }, check: function() {
      check("second instance polled", !!h._svc2 && h._svc2.phase === "ok", h._svc2 ? h._svc2.phase : "none")
      check("no overlay after restart for a seen penalty", h._summons2 === 0 && h._svc2 && !h._svc2.overlayOpen)
      if (h._svc2) h._svc2.destroy()
    } },
    { name: "two new penalties", scenario: "penalty2", shot: "overlay-queue", check: function() {
      check("queued oldest first", h.summons === 2 && eq(svc.overlayQueue.map(function(p) { return p.id }), ["p2b3c4d5", "p3c4d5e6"]),
            JSON.stringify(svc.overlayQueue.map(function(p) { return p.id })))
    } },
    { name: "a poll keeps the card armed; Enter shows the next", wait: 300, shot: "overlay-next", check: function() {
      key(Qt.Key_Return)
      check("next penalty on screen", svc.overlayOpen && svc.currentPenalty && svc.currentPenalty.id === "p3c4d5e6")
    } },
    { name: "(let the next card arm)", refresh: false, wait: 900, check: function() {} },
    { name: "appeal", refresh: false, before: function() { key(Qt.Key_Left); key(Qt.Key_Return) }, check: function() {
      var t = last(logOf("terminal"))
      check("appeal opens the terminal with the id", eq(t, [svc.focusBin, "appeal", "--pause", "p3c4d5e6"]), JSON.stringify(t))
      check("appeal closes the overlay", !svc.overlayOpen && h.hides === 2)
    } },
    { name: "penalties ended", scenario: "ended", check: function() {
      var n = logOf("notify").map(function(a) { return a.join(" ") })
      check("penalty end notifications", has(n, function(s) { return s.indexOf("Hacker News penalty lifted") !== -1 })
            && has(n, function(s) { return s.indexOf("Twitch penalty lifted") !== -1 }), JSON.stringify(n.slice(-3)))
      check("no penalty count left", svc.view.text.indexOf(Model.GLYPH_PENALTY) === -1, svc.view.text)
    } },
    { name: "60 s safety timeout (shortened)", scenario: "penalty4", wait: 2500, before: function() { svc.overlayTimeoutMs = 1200 }, check: function() {
      check("overlay was summoned", h.summons === 3, h.summons)
      check("closed itself after the timeout", !svc.overlayOpen && svc.overlayQueue.length === 0 && !h.shellOpen)
      svc.overlayTimeoutMs = 60000
    } },
    { name: "monitor stale", scenario: "stale-monitor", shot: "bar-monitor-stale", check: function() {
      check("warning state", svc.view.warning && svc.view.text === Model.GLYPH_ALERT, svc.view.text)
      check("tooltip says the monitor", /screen monitor is not reporting/.test(svc.view.tooltip))
    } },
    { name: "monitor off", scenario: "monitor-off", check: function() {
      check("mode off: no warning", !svc.view.warning && svc.view.dimmed)
    } },
    { name: "health problems", scenario: "health", shot: "bar-health", check: function() {
      check("health warning state", svc.view.warning && svc.view.text === Model.GLYPH_ALERT, svc.view.text)
      check("tooltip lists the problems", /Focus problem: hosts: refusing/.test(svc.view.tooltip) && /Focus problem: judge: not logged in/.test(svc.view.tooltip))
      var n = last(logOf("notify"))
      check("one health notification", n && n.indexOf("Focus has 2 problems") !== -1, JSON.stringify(n))
      h._notes = logOf("notify").length
    } },
    { name: "health unchanged", check: function() {
      check("no repeat notification", logOf("notify").length === h._notes)
    } },
    { name: "unreachable", scenario: "unreachable", shot: "bar-unreachable", check: function() {
      check("unreachable phase", svc.phase === "unreachable" && svc.view.warning, svc.phase)
      check("tooltip says the daemon", /daemon is not reachable/.test(svc.view.tooltip))
    } },
    { name: "forbidden", scenario: "forbidden", check: function() {
      check("error phase with message", svc.phase === "error" && svc.errorMessage === "uid 1000 not allowed", svc.errorMessage)
    } },
    { name: "missing cli", before: function() { h._bin = svc.focusBin; svc.focusBin = "/nonexistent/focus" }, check: function() {
      check("missing CLI is a warning", svc.phase === "error" && /could not be started/.test(svc.errorMessage), svc.errorMessage)
      svc.focusBin = h._bin
    } },
    { name: "back to ok", scenario: "locked", check: function() {
      check("recovers", svc.phase === "ok" && !svc.view.warning)
    } },
    { name: "lock all", refresh: false, wait: 2500, before: function() { svc.lockAll() }, check: function() {
      var f = logOf("focus")
      var i = -1
      for (var k = 0; k < f.length; k++) if (eq(f[k], ["lock", "all"])) i = k
      check("ran focus lock all, then polled", i !== -1 && i < f.length - 1 && eq(f[i + 1], ["status", "--json"]), JSON.stringify(f.slice(-3)))
    } },
    { name: "click actions", refresh: false, wait: 500, before: function() { svc.openUnlock(); svc.appealLatest(); svc.openWatch() }, check: function() {
      // execDetached runs them in parallel, so the log order varies.
      var t = logOf("terminal").slice(-3).map(function(a) { return JSON.stringify(a) }).sort()
      var want = [[svc.focusBin, "appeal", "--pause"], [svc.focusBin, "unlock", "--pause"], [svc.focusBin, "watch"]].map(function(a) { return JSON.stringify(a) })
      check("unlock / appeal / watch terminals", eq(t, want), JSON.stringify(t))
    } },
    { name: "overlay compiles", refresh: false, wait: 100, check: function() {
      var c = Qt.createComponent(Qt.resolvedUrl("../Overlay.qml"), Component.PreferSynchronous)
      // Offscreen there is no layer-shell backend; anything else is a real error.
      var err = c.status === Component.Error ? c.errorString().trim() : ""
      var only = err.split("\n").filter(function(l) { return l.trim() !== "" && l.indexOf("No PanelWindow backend loaded") === -1 })
      check("Overlay.qml compiles (bar the missing offscreen PanelWindow backend)", c.status !== Component.Null && only.length === 0, err)
      var b = Qt.createComponent(Qt.resolvedUrl("../BarWidget.qml"))
      check("BarWidget.qml compiles", b.status === Component.Ready, b.errorString())
    } }
  ]

  property string _bin: ""
  property int _notes: 0

  function setScenario(name, then) {
    Quickshell.execDetached([h.dir + "/bin/focus", "--dev-set", name])
    later(400, then)
  }

  function later(ms, fn) {
    var t = Qt.createQmlObject('import QtQuick; Timer { repeat: false }', h)
    t.interval = Math.max(1, ms)
    t.triggered.connect(function() { t.destroy(); fn() })
    t.start()
  }

  function runStep(i) {
    if (i >= h.steps.length) {
      console.log("DONE failures=" + h.failures)
      later(800, function() { Qt.exit(h.failures ? 1 : 0) })
      return
    }
    var s = h.steps[i]
    h.stepIndex = i
    console.log("-- " + s.name)
    var go = function() {
      if (s.before) s.before()
      if (s.refresh !== false) svc.refresh()
      later(s.wait || 1500, function() {
        try { s.check() } catch (e) { h.check(s.name + " threw", false, e + "") }
        if (s.shot) h.shot(s.shot)
        later(s.shot ? 300 : 10, function() { h.runStep(i + 1) })
      })
    }
    var start = function() {
      if (s.at) {
        var waitMs = (h.base + s.at) * 1000 - Date.now()
        later(waitMs, go)
      } else go()
    }
    if (s.scenario) setScenario(s.scenario, start)
    else start()
  }

  Component.onCompleted: {
    h.base = parseFloat(readFile(h.dir + "/base"))
    later(1500, function() { h.runStep(0) })
  }
}
