import QtQuick
import qs.Commons

// Bar icon: the Bananet banana with a small antenna and two signal arcs on
// its concave side - a banana that is also a router. Drawn natively so it
// follows the theme foreground like every other bar glyph.
Item {
  id: root

  property real iconSize: Style.font.icon
  property color color: Color.foreground

  width: iconSize
  height: iconSize
  implicitWidth: iconSize
  implicitHeight: iconSize

  onColorChanged: canvas.requestPaint()
  onIconSizeChanged: canvas.requestPaint()

  Canvas {
    id: canvas
    anchors.fill: parent
    antialiasing: true
    onPaint: {
      var ctx = getContext("2d")
      var s = width / 16
      ctx.reset()
      ctx.clearRect(0, 0, width, height)
      ctx.fillStyle = root.color
      ctx.strokeStyle = root.color
      ctx.lineJoin = "round"
      ctx.lineCap = "round"

      // Body: the Bananet crescent, nudged down-left to leave room for the
      // antenna in the top-right corner.
      ctx.beginPath()
      ctx.moveTo(3.4 * s, 4.0 * s)
      ctx.bezierCurveTo(0.0 * s, 8.8 * s, 2.6 * s, 14.4 * s, 10.4 * s, 15.4 * s)
      ctx.lineTo(12.8 * s, 15.7 * s)
      ctx.bezierCurveTo(13.8 * s, 15.8 * s, 13.8 * s, 14.4 * s, 12.9 * s, 14.3 * s)
      ctx.bezierCurveTo(7.2 * s, 13.7 * s, 4.6 * s, 9.8 * s, 6.3 * s, 4.3 * s)
      ctx.closePath()
      ctx.fill()

      // Stem.
      ctx.lineWidth = Math.max(1, 1.4 * s)
      ctx.beginPath()
      ctx.moveTo(4.1 * s, 4.2 * s)
      ctx.lineTo(3.7 * s, 2.2 * s)
      ctx.stroke()

      // Wi-Fi fan: a dot in the banana's hollow and three arcs radiating
      // up-right from it, the classic signal symbol tilted to fit the crescent.
      var ox = 9.6 * s, oy = 9.6 * s
      var a0 = -Math.PI * 0.62, a1 = -Math.PI * 0.03   // roughly 11 o'clock to 3 o'clock, clear of the crescent
      ctx.beginPath()
      ctx.arc(ox, oy, 1.05 * s, 0, Math.PI * 2)
      ctx.fill()
      ctx.lineWidth = Math.max(1, 1.25 * s)
      var radii = [2.7, 4.5, 6.3]
      for (var i = 0; i < radii.length; i++) {
        ctx.beginPath()
        ctx.arc(ox, oy, radii[i] * s, a0, a1)
        ctx.stroke()
      }
    }
  }
}
