import QtQuick
import Quickshell
import qs.Commons
import qs.Ui
import "Model.js" as Model

// Bar item for the focus blocker. A quiet lock when everything is locked,
// "󰿆 YouTube 12m +1" while unlocks run (soonest expiry first), "󰂭 n" for
// active penalties, and the alert glyph in the urgent colour when focusd is
// unreachable or the screen monitor stopped reporting (tooltip says which).
// Left click: floating terminal with `focus unlock`. Right click: lock all now.
// Middle click: appeal the newest penalty, or `focus watch` when there is none.
BarWidget {
  id: root
  moduleName: "theo.focus"

  // The service can come up after the bar (or be recreated by a plugin
  // reload), and serviceFor() is not a notifying binding, so poll until found.
  property var service: null

  function lookupService() {
    var s = bar && bar.shell && typeof bar.shell.serviceFor === "function" ? bar.shell.serviceFor(root.moduleName) : null
    if (s !== root.service) root.service = s
  }

  readonly property var view: service ? service.view : null
  readonly property string displayText: view ? view.text : Model.GLYPH_LOCK
  readonly property var verticalLines: view ? view.lines : [Model.GLYPH_LOCK]
  readonly property string tooltip: view ? view.tooltip : "Focus · starting…"

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  onBarChanged: lookupService()
  Component.onCompleted: lookupService()

  Timer {
    interval: root.service ? 5000 : 1000
    running: true
    repeat: true
    onTriggered: root.lookupService()
  }

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.vertical ? "" : root.displayText
    labelVisible: !root.vertical
    hasVisualContent: root.displayText !== ""
    tooltipText: root.tooltip
    dimmed: !!root.view && root.view.dimmed
    active: !!root.view && root.view.warning
    horizontalMargin: 8.75
    verticalPadding: 8.75
    fixedHeight: root.vertical ? root.verticalLines.length * Style.bar.iconSlot : -1

    onPressed: function(b) {
      if (!root.service) return
      if (b === Qt.RightButton) {
        root.service.lockAll()
      } else if (b === Qt.MiddleButton) {
        if (root.service.penalties.length) root.service.appealLatest()
        else root.service.openWatch()
      } else {
        root.service.openUnlock()
      }
    }

    Column {
      visible: root.vertical
      anchors.fill: parent

      Repeater {
        model: root.verticalLines

        OpticalGlyph {
          required property string modelData
          width: button.width
          height: Style.bar.iconSlot
          text: modelData
          fontFamily: button.fontFamily
          fontSize: modelData.length > 3 ? button.fontSize * 0.85 : button.fontSize
          color: button.active ? button.activeColor : button.foreground
        }
      }
    }
  }
}
