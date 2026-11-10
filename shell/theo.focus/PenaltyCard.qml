import QtQuick
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "Model.js" as Model

// The card in the middle of the dimmed screen: what the monitor caught and
// two buttons, Back to work (default) and Appeal. No window types here, so
// dev/ can host it in a plain window for offscreen renders.
//
// Keys: Esc = Back to work, Enter/Space = the selected button (Back to work
// unless moved), Left/Right/Tab move the selection. Keys and clicks are ignored for the first moments after
// it appears, so typing that was in flight doesn't dismiss it unseen.
BorderSurface {
  id: root

  property var service: null
  property real availableWidth: 1280

  readonly property var svc: root.service
  readonly property var penalty: svc ? svc.currentPenalty : null
  readonly property double nowSec: svc ? svc.nowSec : Date.now() / 1000
  readonly property var lines: Model.penaltyLines(penalty, nowSec)
  readonly property int queued: svc && svc.overlayQueue ? Math.max(0, svc.overlayQueue.length - 1) : 0

  property int selected: 0   // 0 = Back to work, 1 = Appeal
  // Arming (ignore keys/clicks just after it appears) lives in the service.
  readonly property bool armed: !!(svc && svc.overlayArmed)

  // ------------------------------------------------------------- theme
  readonly property color fg: Commons.Color.menu.text
  readonly property color bg: Commons.Color.menu.background
  readonly property color accent: Commons.Color.accent
  readonly property color urgent: Commons.Color.urgent
  readonly property color muted: Util.alpha(fg, 0.6)
  readonly property string fontFamily: Style.font.family
  readonly property int pad: Math.round(Style.spacing.panelPadding * 1.5)

  width: Math.min(Style.space(640), Math.max(Style.space(360), availableWidth - Style.space(48)))
  height: column.implicitHeight + topPadding + bottomPadding + borderTop + borderBottom
  radius: Style.cornerRadius
  color: root.bg
  borderSpec: Border.surfaceSpec("menu", "border", Commons.Color.menu.border, Math.max(1, Style.space(2)))
  padding: root.pad
  focus: true

  // The next queued penalty resets the selection.
  readonly property string penaltyId: penalty ? penalty.id : ""
  onPenaltyIdChanged: root.selected = 0

  // Clicks on the card itself must not reach the dimmed area behind it
  // (a click there dismisses).
  MouseArea {
    anchors.fill: parent
    acceptedButtons: Qt.AllButtons
  }

  function backToWork() { if (root.armed && root.svc) root.svc.dismissCurrent() }
  function appeal() { if (root.armed && root.svc) root.svc.appealCurrent() }
  function activate() { if (root.selected === 1) root.appeal(); else root.backToWork() }

  // Every key is swallowed: the overlay holds the keyboard.
  function handleKey(event) {
    var k = event.key
    if (k === Qt.Key_Escape) root.backToWork()
    else if (k === Qt.Key_Return || k === Qt.Key_Enter || k === Qt.Key_Space) root.activate()
    // Appeal sits left of Back to work.
    else if (k === Qt.Key_Left) root.selected = 1
    else if (k === Qt.Key_Right) root.selected = 0
    else if (k === Qt.Key_Tab || k === Qt.Key_Backtab) root.selected = 1 - root.selected
    return true
  }

  Keys.onPressed: function(event) { event.accepted = root.handleKey(event) }

  Column {
    id: column
    x: root.contentLeftInset
    y: root.contentTopInset
    width: root.width - root.contentLeftInset - root.contentRightInset
    spacing: Style.spacing.panelGap

    Text {
      width: parent.width
      textFormat: Text.PlainText
      text: Model.GLYPH_PENALTY + "  " + root.lines.headline
      color: root.fg
      font.family: root.fontFamily
      font.pixelSize: Style.font.display
      font.bold: true
      wrapMode: Text.Wrap
      maximumLineCount: 3
      elide: Text.ElideRight
    }

    Text {
      visible: text !== ""
      width: parent.width
      textFormat: Text.PlainText
      text: root.lines.title
      color: root.muted
      font.family: root.fontFamily
      font.pixelSize: Style.font.title
      wrapMode: Text.Wrap
      maximumLineCount: 2
      elide: Text.ElideRight
    }

    Text {
      width: parent.width
      textFormat: Text.PlainText
      text: root.lines.blocked
      color: root.urgent
      font.family: root.fontFamily
      font.pixelSize: Style.font.heading
      font.bold: true
    }

    Text {
      visible: text !== ""
      width: parent.width
      textFormat: Text.PlainText
      text: root.lines.detail
      color: root.muted
      font.family: root.fontFamily
      font.pixelSize: Style.font.body
      wrapMode: Text.Wrap
      maximumLineCount: 3
      elide: Text.ElideRight
    }

    Text {
      visible: root.queued > 0
      width: parent.width
      textFormat: Text.PlainText
      text: root.queued === 1 ? "1 more penalty after this one." : root.queued + " more penalties after this one."
      color: root.muted
      font.family: root.fontFamily
      font.pixelSize: Style.font.body
    }

    Item { width: 1; height: Style.space(4) }

    Row {
      spacing: Style.spacing.controlGap
      anchors.right: parent.right

      Button {
        id: appealButton
        text: "Appeal"
        fontSize: Style.font.title
        horizontalPadding: Style.space(16)
        verticalPadding: Style.space(8)
        bordered: true
        hasCursor: root.selected === 1
        onHovered: function(isHovered) { if (isHovered) root.selected = 1 }
        onClicked: root.appeal()
      }

      Button {
        id: backButton
        text: "Back to work"
        fontSize: Style.font.title
        horizontalPadding: Style.space(16)
        verticalPadding: Style.space(8)
        bordered: true
        selected: root.selected === 0
        hasCursor: root.selected === 0
        onHovered: function(isHovered) { if (isHovered) root.selected = 0 }
        onClicked: root.backToWork()
      }
    }

    Text {
      width: parent.width
      horizontalAlignment: Text.AlignRight
      textFormat: Text.PlainText
      text: "Esc: back to work · Appeal asks the judge whether this was work or a needed break"
      color: root.muted
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      wrapMode: Text.Wrap
    }
  }
}
