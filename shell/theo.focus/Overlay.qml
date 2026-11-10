import QtQuick
import Quickshell
import Quickshell.Wayland
import Quickshell.Hyprland
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "Model.js" as Model

// Overlay entry point (kind "overlay"): the penalty screen. Dims every
// monitor on the overlay layer and shows PenaltyCard on the one that had
// focus, with exclusive keyboard focus there. Esc = Back to work.
// Never a trap: if that monitor goes away the card moves to the first screen
// left, a click on the dimmed area (once armed) is Back to work, and the
// service closes it after 60 s whatever happens (a local backstop at 65 s
// hides the windows even if the service is gone).
//
// The open state lives in the service (Service.qml): `opened` is bound to
// service.overlayOpen and the plugin contract below only forwards to it.
//   service._showOverlay -> shell.summon -> open()   (already open: no-op)
//   service._hideOverlay -> shell.hide   -> close()  (service is closing: no-op)
//   shell summon/hide from outside       -> service.openFromShell / closeFromShell
Item {
  id: root

  property var shell: null
  property var manifest: null
  property var service: null

  readonly property string pluginId: (manifest && manifest.id) ? manifest.id : "theo.focus"
  readonly property var svc: service || (shell && typeof shell.serviceFor === "function" ? shell.serviceFor(pluginId) : null)
  readonly property bool opened: !!(svc && svc.overlayOpen)

  property string activeScreen: ""
  // The screen the card is on: activeScreen while it exists, else the first
  // screen. Re-evaluated whenever the screen list changes (undocking).
  readonly property string cardScreen: {
    var screens = Quickshell.screens, names = []
    for (var i = 0; i < screens.length; i++) names.push(screens[i].name)
    return Model.cardScreen(names, root.activeScreen)
  }
  readonly property bool armed: !!(svc && svc.overlayArmed)
  property bool expired: false
  readonly property bool shown: root.opened && !root.expired

  Timer {
    interval: 65000
    running: root.opened && !root.expired
    onTriggered: {
      console.warn("theo.focus: overlay backstop: hiding the penalty overlay")
      root.expired = true
      root.close()
      if (root.shell && typeof root.shell.hide === "function") root.shell.hide(root.pluginId)
    }
  }

  function pickScreen() {
    var m = Hyprland.focusedMonitor
    root.activeScreen = m ? m.name : (Quickshell.screens.length ? Quickshell.screens[0].name : "")
  }

  // ------------------------------------------------------- plugin contract
  function open(payloadJson) {
    pickScreen()
    if (!root.svc) return
    if (root.svc.overlayOpen) return
    if (!root.svc.openFromShell() && root.shell && typeof root.shell.hide === "function")
      Qt.callLater(function() { root.shell.hide(root.pluginId) })
  }

  function close() {
    if (root.svc) root.svc.closeFromShell()
  }

  function toggle() {
    if (root.opened) root.close()
    else root.open("{}")
  }

  onOpenedChanged: if (opened) { if (!activeScreen) pickScreen() } else { activeScreen = ""; expired = false }

  Variants {
    model: Quickshell.screens

    PanelWindow {
      id: win
      required property var modelData
      readonly property bool isActive: modelData.name === root.cardScreen
      screen: modelData
      visible: root.shown
      color: "transparent"
      anchors { top: true; bottom: true; left: true; right: true }
      exclusionMode: ExclusionMode.Ignore
      WlrLayershell.namespace: "theo-focus"
      WlrLayershell.layer: WlrLayer.Overlay
      WlrLayershell.keyboardFocus: win.isActive ? WlrKeyboardFocus.Exclusive : WlrKeyboardFocus.None

      Rectangle {
        anchors.fill: parent
        color: Util.alpha(Commons.Color.background, 0.85)
      }

      // A click on the dimmed area, on any screen, is Back to work once armed.
      MouseArea {
        anchors.fill: parent
        acceptedButtons: Qt.AllButtons
        onClicked: if (root.armed && root.svc) root.svc.dismissCurrent()
      }

      Loader {
        id: cardLoader
        active: win.isActive && root.shown
        anchors.centerIn: parent
        sourceComponent: PenaltyCard {
          service: root.svc
          availableWidth: win.width
        }
        onLoaded: item.forceActiveFocus()
      }
    }
  }
}
