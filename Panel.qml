import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

// banan.banatik — Banatik: your MikroTik router in the bar.
//
// Bar: banana-with-antenna glyph (+ optional label: WAN rate, devices online
// or the router's identity). Hover: one-screen summary. Click: full panel with
// the internet link, router health, interfaces with 24 h charts, Wi-Fi and
// DHCP devices, tunnels, logins, firewall counters and the log. All data
// comes from collect.py over the router's REST API with a read-only user.
Panel {
  id: root
  moduleName: "banan.banatik"
  ipcTarget: "banan.banatik"
  manageIpc: false

  IpcHandler {
    target: "banan.banatik"
    function open(): void { root.open() }
    function close(): void { root.close() }
    function show(): void { root.open() }
    function hide(): void { root.close() }
    function toggle(): void { root.toggle() }
    function refresh(): void { root.refresh() }
    function scrollTo(y: string): void { root.scrollTo(Number(y)) }
  }

  readonly property bool vertical: bar ? bar.vertical : false
  readonly property int barSize: bar ? bar.barSize : Style.bar.sizeHorizontal
  implicitWidth: vertical ? barSize : widgetRow.implicitWidth
  implicitHeight: barSize

  // ------------------------------------------------------------------ theme
  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color urgent: bar ? bar.urgent : Color.urgent
  function mix(a, b, t) {
    return Qt.rgba(a.r + (b.r - a.r) * t, a.g + (b.g - a.g) * t, a.b + (b.b - a.b) * t, 1)
  }
  readonly property color surface: Color.popups.background
  readonly property color barSurface: Color.bar.background
  readonly property color dim: mix(foreground, surface, 0.38)
  readonly property color dimmer: mix(foreground, surface, 0.58)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property color hoverFill: Style.hoverFillFor(foreground, Color.accent)
  readonly property color selectedFill: Style.selectedFillFor(foreground, Color.accent)
  readonly property color accentColor: {
    var a = Color.accent
    var f = foreground
    return (Math.abs(a.r - f.r) + Math.abs(a.g - f.g) + Math.abs(a.b - f.b) < 0.15) ? mix(f, surface, 0.4) : a
  }

  // --------------------------------------------------------------- settings
  function intSetting(name, fallback, min, max) {
    var n = parseInt(String(setting(name, fallback)), 10)
    if (!isFinite(n)) n = fallback
    return Math.max(min, Math.min(max, n))
  }
  function boolSetting(name, fallback) {
    var v = setting(name, fallback)
    if (typeof v === "string") return v === "true" || v === "1" || v === "yes"
    return !!v
  }
  readonly property int refreshIntervalSec: intSetting("refreshIntervalSec", 30, 5, 600)
  readonly property int openRefreshIntervalSec: intSetting("openRefreshIntervalSec", 5, 2, 60)
  readonly property bool showLabel: boolSetting("showLabel", false)
  readonly property string labelStyle: String(setting("labelStyle", "wan"))
  readonly property bool showTooltip: boolSetting("showTooltip", false)
  readonly property string iconStyle: String(setting("iconStyle", "banana"))
  readonly property bool showClients: boolSetting("showClients", true)
  readonly property bool showLog: boolSetting("showLog", true)
  readonly property bool notifyWan: boolSetting("notifyWan", true)
  readonly property bool notifyNewClient: boolSetting("notifyNewClient", true)
  readonly property bool notifyLogin: boolSetting("notifyLogin", true)
  readonly property int historyDays: intSetting("historyDays", 7, 1, 365)
  readonly property string historyDir: String(setting("historyDir", "") || "")

  // ------------------------------------------------------------------- data
  readonly property string scriptPath: Qt.resolvedUrl("collect.py").toString().replace(/^file:\/\//, "")
  property var snap: ({ interfaces: [], clients: [], wifi: { radios: [], clients: [] }, alerts: [], warnings: [], log: [] })
  property bool loaded: false
  property bool refreshing: false
  property string lastError: ""
  property double lastSampleMs: 0
  property var rates: ({})
  property var _prev: null
  property string actionStatus: ""
  property var _seenAlerts: null
  property bool alertFlash: false
  property var history: []
  property int historyStep: 30
  property int chartRange: 3600
  // A new range needs different data from the collector (raw samples up to
  // 24 h, aggregated buckets beyond); fetch it right away.
  onChartRangeChanged: if (opened) refresh()

  readonly property var router: snap && snap.router ? snap.router : null
  readonly property var wan: snap && snap.wan ? snap.wan : null
  readonly property var cloud: snap && snap.cloud ? snap.cloud : {}
  readonly property var wifi: snap && snap.wifi ? snap.wifi : { radios: [], clients: [] }
  readonly property var clients: snap && snap.clients ? snap.clients : []
  readonly property var zerotier: snap && snap.zerotier ? snap.zerotier : null
  readonly property var wireguard: snap && snap.wireguard ? snap.wireguard : null
  readonly property var sessions: snap && snap.sessions ? snap.sessions : []
  readonly property var firewall: snap && snap.firewall ? snap.firewall : null
  readonly property var log: snap && snap.log ? snap.log : []
  readonly property var alerts: snap && snap.alerts ? snap.alerts : []
  readonly property var wanLog: snap && snap.wanLog ? snap.wanLog : []
  readonly property var setup: snap && snap.setup ? snap.setup : null
  readonly property var dns: snap && snap.dns ? snap.dns : []
  readonly property bool wanUp: !!(wan && wan.up)
  readonly property bool healthy: loaded && !setup && !lastError && !!router
  readonly property int onlineCount: {
    var n = 0
    for (var i = 0; i < clients.length; i++) if (clients[i].status === "bound") n++
    return n
  }
  readonly property int wifiClientCount: wifi && wifi.clients ? wifi.clients.length : 0
  readonly property int newClientFor: 3600
  function clientIsNew(c) {
    if (!c || !c.firstSeen) return false
    return (Number(snap.ts || 0) - Number(c.firstSeen)) < newClientFor
  }
  readonly property int newClientCount: {
    var n = 0
    for (var i = 0; i < clients.length; i++) if (clientIsNew(clients[i])) n++
    return n
  }
  readonly property string alertsJson: {
    var out = []
    for (var i = alerts.length - 1; i >= 0 && out.length < 6; i--) out.push(alerts[i])
    return JSON.stringify(out)
  }
  readonly property var latestAlert: alerts.length ? alerts[alerts.length - 1] : null
  readonly property string wanHistoryJson: {
    var out = []
    for (var i = wanLog.length - 1; i >= 0 && out.length < 3; i--) {
      var e = wanLog[i]
      out.push(fmtClock(Number(e.at || 0)) + "  " + (e.iface || "WAN") + " " + (e.state === "up" ? "up" : "DOWN") + (e.ip ? "  " + e.ip : ""))
    }
    return JSON.stringify(out)
  }
  readonly property string warningsJson: JSON.stringify(snap && snap.warnings ? snap.warnings : [])
  readonly property string sessionsJson: JSON.stringify(sessions)
  readonly property string healthJson: JSON.stringify(router && router.health ? router.health : [])
  property bool logExpanded: false
  readonly property string logJson: {
    var list = log || []
    var keep = logExpanded ? list.length : Math.min(12, list.length)
    return JSON.stringify(list.slice(list.length - keep))
  }

  // Scroll offset survives a refresh (the row models are keyed, but a
  // Repeater over JSON still rebuilds when the alert list changes).
  property real _savedScroll: 0
  property bool _restoringScroll: false
  function scrollTo(y) {
    var f = scrollArea.contentItem
    if (!f) return
    f.contentY = Math.max(0, Math.min(Number(y) || 0, Math.max(0, f.contentHeight - f.height)))
  }
  function restoreScroll() {
    if (!_restoringScroll) return
    scrollTo(_savedScroll)
  }
  Timer {
    id: scrollRestoreTimer
    interval: 300
    repeat: false
    onTriggered: { root.restoreScroll(); root._restoringScroll = false }
  }
  Connections {
    target: scrollArea.contentItem
    function onContentHeightChanged() { root.restoreScroll() }
  }

  function applySample(doc) {
    if (opened && scrollArea.contentItem) {
      _savedScroll = scrollArea.contentItem.contentY
      _restoringScroll = _savedScroll > 0
      if (_restoringScroll) scrollRestoreTimer.restart()
    }
    var now = Number(doc.ts) || Date.now() / 1000
    var counters = {}
    var list = doc.interfaces || []
    for (var i = 0; i < list.length; i++) counters[list[i].name] = { rx: Number(list[i].rx) || 0, tx: Number(list[i].tx) || 0 }
    var next = {}
    if (_prev && now > _prev.ts) {
      var dt = now - _prev.ts
      for (var name in counters) {
        var p = _prev.counters[name]
        if (!p) continue
        next[name] = { rx: Math.max(0, (counters[name].rx - p.rx) / dt), tx: Math.max(0, (counters[name].tx - p.tx) / dt) }
      }
    } else {
      for (var n2 in counters) next[n2] = { rx: 0, tx: 0 }
    }
    _prev = { ts: now, counters: counters }
    rates = next
    snap = doc
    loaded = true
    lastSampleMs = Date.now()
    lastError = doc.error ? String(doc.error) : ""
    if (doc.history && doc.history.length !== undefined) {
      history = doc.history
      historyStep = Math.max(30, Number(doc.historyStep) || 30)
    }
    noteAlerts(doc)
    syncRows()
    if (_restoringScroll) Qt.callLater(restoreScroll)
  }

  function alertEnabled(kind) {
    if (kind === "client") return notifyNewClient
    if (kind === "login") return notifyLogin
    return notifyWan   // wan, public, router
  }

  function noteAlerts(doc) {
    var list = doc.alerts || []
    var first = _seenAlerts === null
    var seen = first ? {} : _seenAlerts
    var next = {}
    var now = Date.now() / 1000
    for (var i = 0; i < list.length; i++) {
      var a = list[i]
      var id = String(a.id || "")
      next[id] = true
      if (first || seen[id] || doc.demo) continue
      if (now - Number(a.at || 0) > 600) continue
      if (a.urgent) { alertFlash = true; alertFlashTimer.restart() }
      if (!alertEnabled(String(a.kind || ""))) continue
      Quickshell.execDetached([notifyBin, "-a", "Banatik",
                              "-u", a.urgent ? "critical" : "normal",
                              plain("Banatik: " + a.title),
                              plain(a.body)])
    }
    _seenAlerts = next
  }

  function alertText(a) {
    if (!a) return ""
    var age = fmtAge(Math.max(0, Math.round(Date.now() / 1000 - Number(a.at || 0) + 0 * clockTick)))
    return age + " ago:  " + a.title + (a.body ? " — " + a.body : "")
  }
  function alertUrgent(a) {
    return !!(a && a.urgent && Date.now() / 1000 - Number(a.at || 0) < 3600)
  }

  function handleOutput(text) {
    var raw = String(text || "")
    if (raw.length > maxDocumentChars) {
      lastError = "Collector output too large (" + raw.length + " chars), ignored"
      return
    }
    raw = raw.trim()
    if (raw === "") return
    try {
      applySample(JSON.parse(raw))
    } catch (e) {
      lastError = "Could not parse collector output: " + e
      console.warn("banan.banatik", lastError)
    }
  }

  // Fixed absolute paths only. `-I` keeps PYTHON* variables and the user
  // site directory out of the collector.
  readonly property string pythonBin: "/usr/bin/python3"
  readonly property string clipboardBin: "/usr/bin/wl-copy"
  readonly property string notifyBin: "/usr/bin/notify-send"
  readonly property int maxDocumentChars: 8 * 1024 * 1024

  function plain(value) {
    return String(value === undefined || value === null ? "" : value).replace(/</g, "‹").replace(/>/g, "›")
  }

  function collectorArgs() {
    var args = [pythonBin, "-I", scriptPath]
    if (opened) { args.push("--history"); args.push(String(chartRange)) }
    args.push("--history-days"); args.push(String(historyDays))
    if (historyDir !== "") { args.push("--history-dir"); args.push(historyDir) }
    if (opened && showLog) args.push("--log")
    if (!showClients) args.push("--no-clients")
    if (boolSetting("demo", false)) args.push("--demo")
    return args
  }

  function refresh() {
    if (collector.running) return
    refreshing = true
    collector.command = collectorArgs()
    collector.running = true
    watchdog.restart()
  }

  Process {
    id: collector
    running: false
    command: []
    stdout: StdioCollector { id: collectorOut; waitForEnd: true }
    stderr: StdioCollector { id: collectorErr; waitForEnd: true }
    onExited: function(exitCode) {
      root.refreshing = false
      watchdog.stop()
      var out = String(collectorOut.text || "")
      if (exitCode === 0 && out.trim() !== "") root.handleOutput(out)
      else {
        var err = String(collectorErr.text || "").trim()
        root.lastError = err !== "" ? err.split("\n").slice(-1)[0] : ("Collector exited with code " + exitCode)
      }
    }
  }

  // collect.py has its own 9 s deadline; this only covers the interpreter never coming back.
  Timer {
    id: watchdog
    interval: 14000
    repeat: false
    onTriggered: if (collector.running) collector.running = false
  }

  Timer {
    id: refreshTimer
    interval: (root.opened ? root.openRefreshIntervalSec : root.refreshIntervalSec) * 1000
    repeat: true
    running: true
    triggeredOnStart: true
    onTriggered: root.refresh()
  }

  Timer {
    id: alertFlashTimer
    interval: 120000
    repeat: false
    onTriggered: root.alertFlash = false
  }

  Timer {
    id: actionStatusTimer
    interval: 2200
    repeat: false
    onTriggered: root.actionStatus = ""
  }

  property int clockTick: 0
  Timer { interval: 1000; repeat: true; running: root.opened; onTriggered: root.clockTick += 1 }

  onOpenedChanged: {
    if (opened) { cursorActive = false; alertFlash = false; refresh() }
  }

  // ------------------------------------------------------------- formatting
  function fmtBytes(n) {
    n = Number(n) || 0
    var units = ["B", "kB", "MB", "GB", "TB"]
    var i = 0
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++ }
    return (i === 0 ? Math.round(n) : n.toFixed(n >= 100 ? 0 : 1)) + " " + units[i]
  }
  function fmtRate(bps) { return fmtBytes(bps) + "/s" }
  function fmtBits(bps) {
    bps = Number(bps) || 0
    if (bps >= 1e9) return (bps / 1e9).toFixed(1) + " Gbit/s"
    if (bps >= 1e6) return Math.round(bps / 1e6) + " Mbit/s"
    if (bps >= 1e3) return Math.round(bps / 1e3) + " kbit/s"
    return Math.round(bps) + " bit/s"
  }
  function fmtAge(seconds) {
    seconds = Math.max(0, Math.round(Number(seconds) || 0))
    if (seconds < 60) return seconds + " s"
    if (seconds < 3600) return Math.floor(seconds / 60) + " min"
    if (seconds < 86400) return Math.floor(seconds / 3600) + " h"
    return Math.floor(seconds / 86400) + " d"
  }
  function fmtDuration(seconds) {
    seconds = Math.max(0, Math.round(Number(seconds) || 0))
    if (seconds < 60) return seconds + " s"
    if (seconds < 3600) return Math.floor(seconds / 60) + " min"
    if (seconds < 86400) return Math.floor(seconds / 3600) + " h " + Math.floor((seconds % 3600) / 60) + " min"
    if (seconds < 7 * 86400) return Math.floor(seconds / 86400) + " d " + Math.floor((seconds % 86400) / 3600) + " h"
    return Math.floor(seconds / 604800) + " w " + Math.floor((seconds % 604800) / 86400) + " d"
  }
  function fmtClock(ts) { return Qt.formatTime(new Date(ts * 1000), "HH:mm") }
  function fmtPercent(part, total) {
    part = Number(part) || 0; total = Number(total) || 0
    return total > 0 ? Math.round(part / total * 100) + " %" : "?"
  }
  function signalBars(dbm) {
    dbm = Number(dbm) || -100
    if (dbm >= -55) return "▂▄▆█"
    if (dbm >= -65) return "▂▄▆_"
    if (dbm >= -75) return "▂▄__"
    if (dbm >= -85) return "▂___"
    return "____"
  }
  function signalWord(dbm) {
    dbm = Number(dbm) || -100
    if (dbm >= -55) return "excellent"
    if (dbm >= -65) return "good"
    if (dbm >= -75) return "fair"
    if (dbm >= -85) return "weak"
    return "very weak"
  }
  function plural(n, one, many) {
    n = Number(n) || 0
    return n + " " + (n === 1 ? one : many)
  }
  function rateOf(name) {
    var r = rates[name]
    return r ? r : { rx: 0, tx: 0 }
  }
  function kindGlyph(kind) {
    switch (kind) {
      case "wan": return "󰖟"
      case "ethernet": return "󰈀"
      case "bridge": return "󰛳"
      case "wifi": return "󰖩"
      case "zerotier": return "󱘖"
      case "wireguard": return "󰦝"
      case "tunnel": return "󰒘"
      case "vlan": return "󰡨"
      default: return "󰛳"
    }
  }
  function bandLabel(band) {
    band = String(band || "")
    if (band.indexOf("5ghz") === 0) return "5 GHz"
    if (band.indexOf("2ghz") === 0) return "2.4 GHz"
    if (band.indexOf("6ghz") === 0) return "6 GHz"
    return band
  }
  function freqLabel(freq, band) {
    freq = Number(freq) || 0
    if (freq >= 5925) return "6 GHz · " + freq + " MHz"
    if (freq >= 4900) return "5 GHz · " + freq + " MHz"
    if (freq >= 2400) return "2.4 GHz · " + freq + " MHz"
    return band ? bandLabel(band) : ""
  }

  // ------------------------------------------------------------ text builders
  readonly property string wanLine: {
    if (!loaded) return "…"
    if (setup) return "not configured"
    if (lastError) return "unreachable"
    if (!wan || !wan.present) return "no default route"
    var s = wan.iface || "?"
    if (wan.kind === "pppoe") s += " (PPPoE" + (wan.pppoeOn ? " on " + wan.pppoeOn : "") + ")"
    else if (wan.kind === "dhcp") s += " (DHCP)"
    else if (wan.kind === "static") s += " (static)"
    if (!wan.up) return s + " — DOWN" + (wan.detail ? ": " + wan.detail : "")
    if (wan.ip) s += " · " + wan.ip
    if (wan.gateway && wan.gateway !== wan.iface) s += " → " + wan.gateway
    return s
  }
  readonly property string wanSince: {
    if (!wan || !wan.sinceUp) return ""
    return "up since " + wan.sinceUp + (wan.linkDowns ? " · " + plural(wan.linkDowns, "link drop", "link drops") + " since boot" : "")
  }
  readonly property string publicLine: {
    if (!cloud || !cloud.publicIp) return loaded ? "unknown (IP cloud disabled?)" : "…"
    var s = cloud.publicIp
    if (cloud.ddnsName) s += " · " + cloud.ddnsName
    return s
  }
  readonly property string cloudLine: {
    if (!cloud) return ""
    var parts = []
    if (cloud.bth) parts.push("Back To Home: " + cloud.bth)
    if (cloud.ddns) parts.push("DDNS: " + cloud.ddns)
    if (cloud.warning) parts.push("⚠ " + cloud.warning)
    return parts.join(" · ")
  }
  readonly property string routerMeta: {
    if (!router) return ""
    var parts = []
    if (router.board) parts.push(router.board)
    if (router.version) parts.push("RouterOS " + router.version)
    return parts.join(" · ")
  }
  readonly property string routerDetail: {
    if (!router) return setup ? "credentials needed" : (lastError ? lastError : "")
    var parts = []
    if (router.uptimeS !== null && router.uptimeS !== undefined) parts.push("up " + fmtDuration(router.uptimeS))
    if (router.cpuLoad !== undefined) parts.push("cpu " + router.cpuLoad + " %")
    if (router.memTotal) parts.push("mem " + fmtPercent(router.memTotal - router.memFree, router.memTotal))
    return parts.join(" · ")
  }
  readonly property string firmwareLine: {
    if (!router) return ""
    if (router.firmwarePending) return router.firmware + " → " + router.firmwareUpgrade + " available (applies on the next reboot)"
    return router.firmware ? router.firmware + " (current)" : "unknown"
  }
  readonly property string versionLine: {
    if (!router) return ""
    var s = router.version || "?"
    if (router.channel) s += " · channel " + router.channel
    if (router.latestVersion && router.latestVersion !== String(router.version).split(" ")[0]) s += " · " + router.latestVersion + " available"
    return s
  }
  readonly property string firewallLine: {
    if (!firewall) return ""
    var s = plural(firewall.filter, "filter rule", "filter rules")
    if (firewall.filterDisabled) s += " (" + firewall.filterDisabled + " disabled)"
    s += " · " + plural(firewall.nat, "NAT rule", "NAT rules") + " · " + plural(firewall.connections, "tracked connection", "tracked connections")
    return s
  }
  function sessionText(s) {
    if (!s) return ""
    var t = s.user + " (" + s.group + ") via " + s.via + " from " + (s.address || "?")
    if (s.count > 1) t += " ×" + s.count
    if (s.when) t += " · since " + s.when
    return t
  }
  function clientLabel(c) {
    if (!c) return ""
    return c.host || c.comment || c.mac || "?"
  }
  function clientTooltip(c) {
    if (!c) return ""
    var lines = [clientLabel(c)]
    if (c.ip) lines.push("IP: " + c.ip)
    if (c.mac) lines.push("MAC: " + c.mac)
    if (c.comment && c.comment !== clientLabel(c)) lines.push(c.comment)
    lines.push((c.static ? "static lease" : "dynamic lease") + (c.status ? " · " + c.status : "") + (c.expires ? " · expires in " + fmtDuration(c.expires) : ""))
    if (c.lastSeen !== null && c.lastSeen !== undefined) lines.push("last seen " + fmtAge(c.lastSeen) + " ago")
    var w = wifiClientFor(c.mac)
    if (w) lines.push("Wi-Fi " + w.ssid + " · " + w.signal + " dBm (" + signalWord(w.signal) + ") · " + bandLabel(w.band) + " · ↓ " + fmtBits(w.rxRate) + " ↑ " + fmtBits(w.txRate))
    lines.push("Click to copy the IP")
    return lines.join("\n")
  }
  function wifiClientFor(mac) {
    var list = wifi && wifi.clients ? wifi.clients : []
    for (var i = 0; i < list.length; i++) if (list[i].mac === mac) return list[i]
    return null
  }
  function clientSubtitle(c) {
    if (!c) return ""
    var parts = []
    if (c.ip) parts.push(c.ip)
    var w = wifiClientFor(c.mac)
    if (w) parts.push(w.ssid + " " + signalBars(w.signal) + " " + w.signal + " dBm")
    else if (c.status === "bound") parts.push("wired")
    if (c.status !== "bound") parts.push(c.status)
    if (c.static) parts.push("static")
    if (c.lastSeen !== null && c.lastSeen !== undefined && c.lastSeen > 600) parts.push("seen " + fmtAge(c.lastSeen) + " ago")
    return parts.join(" · ")
  }

  function ifaceRole(f) {
    if (!f) return ""
    if (wan && f.name === wan.iface) return "INTERNET"
    if (f.kind === "bridge") return "LAN"
    return ""
  }
  function ifaceSubtitle(f) {
    if (!f) return ""
    var parts = []
    if (f.kind === "wifi") {
      var radio = radioFor(f.name)
      if (radio) {
        if (radio.ssid) parts.push(radio.ssid)
        var fl = freqLabel(radio.freq, radio.band)
        if (fl) parts.push(fl)
        parts.push(plural(radio.clients, "client", "clients"))
        if (!radio.running) parts.push(radio.disabled ? "disabled" : "not running")
      }
    } else {
      if (f.ips && f.ips.length) parts.push(f.ips.join(", "))
      if (f.kind === "zerotier" && zerotier && zerotier.networks) {
        for (var i = 0; i < zerotier.networks.length; i++) if (zerotier.networks[i].iface === f.name) parts.push(zerotier.networks[i].name || zerotier.networks[i].network)
        if (zerotier.leaves !== undefined) parts.push(plural(zerotier.leaves, "peer", "peers"))
      }
      if (f.kind === "wireguard" && wireguard && wireguard.peers) {
        var n = 0, live = 0
        for (var p = 0; p < wireguard.peers.length; p++) if (wireguard.peers[p].iface === f.name) { n++; if (wireguard.peers[p].lastHandshake !== null && wireguard.peers[p].lastHandshake < 180) live++ }
        parts.push(live + "/" + n + " peers active")
      }
      if (!f.running) parts.push(f.disabled ? "disabled" : "down")
    }
    if (f.comment && f.kind !== "wifi") parts.push(f.comment)
    return parts.join(" · ")
  }
  function radioFor(name) {
    var list = wifi && wifi.radios ? wifi.radios : []
    for (var i = 0; i < list.length; i++) if (list[i].name === name) return list[i]
    return null
  }
  function ifaceTooltip(f) {
    if (!f) return ""
    var r = rateOf(f.name)
    var lines = [f.name + (ifaceRole(f) ? " · " + ifaceRole(f) : "") + " · " + f.kind]
    var sub = ifaceSubtitle(f)
    if (sub) lines.push(sub)
    lines.push("↓ " + fmtRate(r.rx) + "  ↑ " + fmtRate(r.tx))
    lines.push("total ↓ " + fmtBytes(f.rx) + " ↑ " + fmtBytes(f.tx) + " since boot")
    lines.push("Click to expand")
    return lines.join("\n")
  }
  function ifaceDetailLines(f) {
    var lines = []
    if (!f) return lines
    var info = []
    if (f.mac) info.push("MAC " + f.mac)
    if (f.mtu) info.push("MTU " + f.mtu)
    if (f.type) info.push("type " + f.type)
    lines.push({ t: info.join(" · "), k: "info" })
    lines.push({ t: "Counters: ↓ " + fmtBytes(f.rx) + " (" + f.rxPackets + " pkt) ↑ " + fmtBytes(f.tx) + " (" + f.txPackets + " pkt)" + ((f.rxErrors || f.txErrors) ? " · errors " + f.rxErrors + "/" + f.txErrors : "") + ((f.rxDrops || f.txDrops) ? " · drops " + f.rxDrops + "/" + f.txDrops : ""), k: (f.rxErrors || f.txErrors) ? "warn" : "info" })
    var link = []
    if (f.lastUp) link.push("last up " + f.lastUp)
    if (f.lastDown) link.push("last down " + f.lastDown)
    if (f.linkDowns) link.push(plural(f.linkDowns, "link drop", "link drops"))
    if (link.length) lines.push({ t: link.join(" · "), k: "info" })
    if (f.kind === "wan" && wan && wan.iface === f.name) {
      if (wan.kind === "pppoe") lines.push({ t: "PPPoE user " + (wan.pppoeUser || "?") + (wan.acName ? " · concentrator " + wan.acName : "") + (wan.pppoeOn ? " · over " + wan.pppoeOn : ""), k: "info" })
      if (wan.kind === "dhcp") lines.push({ t: "DHCP client: " + (wan.dhcpStatus || "?") + (wan.dhcpExpires ? " · lease expires in " + wan.dhcpExpires : ""), k: "info" })
    }
    if (f.kind === "wifi") {
      var list = wifi && wifi.clients ? wifi.clients : []
      var mine = []
      for (var i = 0; i < list.length; i++) if (list[i].iface === f.name) mine.push(list[i])
      var radio = radioFor(f.name)
      if (radio && radio.masterOf) lines.push({ t: "virtual AP on " + radio.masterOf + (radio.mode ? " · " + radio.mode : ""), k: "info" })
      if (radio && radio.width) lines.push({ t: "channel width " + radio.width + (radio.mode ? " · mode " + radio.mode : ""), k: "info" })
      if (mine.length) {
        lines.push({ t: "Clients on " + (radio && radio.ssid ? radio.ssid : f.name) + ":", k: "head" })
        for (var j = 0; j < mine.length; j++) {
          var c = mine[j]
          var t = signalBars(c.signal) + "  " + (c.host || c.mac) + (c.ip ? "  " + c.ip : "") + "  · " + c.signal + " dBm · " + bandLabel(c.band) + " · ↓ " + fmtRate(c.rxBps) + " ↑ " + fmtRate(c.txBps)
          if (c.uptime !== null && c.uptime !== undefined) t += " · " + fmtDuration(c.uptime)
          lines.push({ t: t, k: "item", dim: c.signal < -80 })
        }
      } else lines.push({ t: "no clients", k: "item", dim: true })
    }
    if (f.kind === "zerotier" && zerotier) {
      for (var z = 0; z < (zerotier.instances || []).length; z++) {
        var inst = zerotier.instances[z]
        lines.push({ t: "ZeroTier " + inst.name + " · " + inst.address + " · " + (inst.online ? "online" : "OFFLINE") + " · port " + inst.port, k: inst.online ? "info" : "warn" })
      }
      for (var nn = 0; nn < (zerotier.networks || []).length; nn++) {
        var net = zerotier.networks[nn]
        if (net.iface !== f.name) continue
        lines.push({ t: "Network " + (net.name || "") + " " + net.network + " · " + net.status + " · " + net.type + (net.allowDefault ? " · default route allowed" : "") + (net.allowManaged ? " · managed routes" : ""), k: net.status === "OK" ? "info" : "warn" })
      }
      var peers = zerotier.peers || []
      var leaves = []
      for (var pp = 0; pp < peers.length; pp++) if (peers[pp].role === "LEAF") leaves.push(peers[pp])
      if (leaves.length) {
        lines.push({ t: "Peers (" + leaves.length + " leaf, " + zerotier.planets + " root servers):", k: "head" })
        for (var q = 0; q < leaves.length; q++) {
          var pe = leaves[q]
          lines.push({ t: pe.address + "  " + (pe.endpoint || "no path") + (pe.latencyMs !== null && pe.latencyMs !== undefined ? " · " + pe.latencyMs + " ms" : "") + (pe.paths > 1 ? " · " + pe.paths + " paths" : ""), k: "item", dim: !pe.endpoint })
        }
      }
    }
    if (f.kind === "wireguard" && wireguard) {
      var wp = wireguard.peers || []
      var any = false
      for (var w = 0; w < wp.length; w++) {
        var peer = wp[w]
        if (peer.iface !== f.name) continue
        if (!any) { lines.push({ t: "Peers:", k: "head" }); any = true }
        var live = peer.lastHandshake !== null && peer.lastHandshake !== undefined && peer.lastHandshake < 180
        var pt = (peer.name || peer.allowed || "peer") + (peer.isBth ? " (Back To Home)" : "") + "  " + (peer.endpoint || "no endpoint")
        pt += peer.lastHandshake !== null && peer.lastHandshake !== undefined ? " · handshake " + fmtAge(peer.lastHandshake) + " ago" : " · never connected"
        pt += " · ↓ " + fmtBytes(peer.rx) + " ↑ " + fmtBytes(peer.tx)
        if (peer.allowed) pt += " · " + peer.allowed
        lines.push({ t: pt, k: "item", dim: !live })
      }
      if (!any) lines.push({ t: "no peers configured", k: "item", dim: true })
    }
    return lines
  }

  // ------------------------------------------------------------ bar surface
  readonly property string barLabel: {
    if (!loaded) return ""
    if (setup) return "setup"
    if (lastError) return "offline"
    if (labelStyle === "clients") return plural(onlineCount, "device", "devices")
    if (labelStyle === "identity") return router && router.identity ? router.identity : ""
    if (!wanUp) return "WAN down"
    var r = wan ? rateOf(wan.iface) : { rx: 0, tx: 0 }
    return "↓" + fmtBytes(r.rx) + " ↑" + fmtBytes(r.tx)
  }
  readonly property string mainGlyph: !loaded ? "󰛳" : (healthy && wanUp ? "󰛳" : "󰲛")
  readonly property color barIconColor: {
    var fg = barForeground
    if (!loaded) return mix(fg, barSurface, 0.4)
    if (setup) return mix(fg, barSurface, 0.3)
    if (lastError || !wanUp || alertFlash) return urgent
    return fg
  }
  readonly property string barTooltip: {
    if (!loaded) return "Banatik: collecting…"
    if (setup) return "Banatik: not configured — open the panel"
    if (lastError) return "Banatik: " + lastError
    var lines = []
    lines.push((router && router.identity ? router.identity : "MikroTik") + (routerMeta ? " · " + routerMeta : ""))
    if (routerDetail) lines.push(routerDetail)
    lines.push("Internet: " + wanLine)
    if (cloud && cloud.publicIp) lines.push("Public IP: " + cloud.publicIp)
    var r = wan ? rateOf(wan.iface) : null
    if (r) lines.push("WAN ↓ " + fmtRate(r.rx) + "  ↑ " + fmtRate(r.tx))
    lines.push(plural(onlineCount, "device online", "devices online") + " · " + plural(wifiClientCount, "Wi-Fi client", "Wi-Fi clients") + (newClientCount ? " · " + newClientCount + " new" : ""))
    if (router && router.firmwarePending) lines.push("⚠ firmware " + router.firmwareUpgrade + " waits for a reboot")
    if (latestAlert && Date.now() / 1000 - Number(latestAlert.at || 0) < 3600) lines.push("⚠ " + alertText(latestAlert))
    return lines.join("\n")
  }

  function handlePress(button) {
    if (button === Qt.RightButton) { refresh(); return }
    toggle()
  }

  Item {
    id: widgetRow
    anchors.fill: parent
    readonly property bool labelShown: root.showLabel && !root.vertical && root.barLabel !== ""
    implicitWidth: button.implicitWidth + (labelShown ? labelButton.implicitWidth : 0)
    implicitHeight: root.barSize

    BarIconButton {
      id: button
      bar: root.bar
      anchors.left: parent.left
      anchors.top: parent.top
      anchors.bottom: parent.bottom
      width: implicitWidth
      text: root.iconStyle === "emoji" ? "🍌" : (root.iconStyle === "banana" ? "" : root.mainGlyph)
      iconComponent: root.iconStyle === "banana" ? banatikIcon : null
      useActiveColor: false
      foreground: root.barIconColor
      tooltipText: root.showTooltip ? root.plain(root.barTooltip) : ""
      onPressed: function(b) { root.handlePress(b) }
    }

    WidgetButton {
      id: labelButton
      bar: root.bar
      anchors.left: button.right
      anchors.top: parent.top
      anchors.bottom: parent.bottom
      width: visible ? implicitWidth : 0
      visible: widgetRow.labelShown
      text: root.barLabel
      fontSize: Style.font.bodySmall
      horizontalMargin: 3
      foreground: root.barIconColor
      useActiveColor: false
      tooltipText: root.showTooltip ? root.plain(root.barTooltip) : ""
      onPressed: function(b) { root.handlePress(b) }
    }
  }

  Component {
    id: banatikIcon
    Item {
      BanatikIcon {
        anchors.centerIn: parent
        iconSize: Style.space(15)
        color: root.barIconColor
      }
    }
  }

  // ------------------------------------------------------------- row model
  property var expanded: ({})
  property bool showDevices: true
  property bool cursorActive: false
  property int cursorIndex: 0

  function isExpanded(key) { return !!expanded[key] }
  function toggleExpanded(key) {
    var next = {}
    for (var k in expanded) next[k] = expanded[k]
    if (next[key]) delete next[key]
    else next[key] = true
    expanded = next
  }
  function setAllExpanded(on) {
    var next = {}
    if (on) for (var i = 0; i < ifaceRows.length; i++) next["if:" + ifaceRows[i].name] = true
    expanded = next
  }

  property var ifaceKeys: []
  property var clientKeys: []
  property var ifaceMap: ({})
  property var clientMap: ({})
  readonly property var ifaceRows: ifaceKeys.map(function(k) { return ifaceMap[k] }).filter(function(x) { return !!x })
  readonly property var clientRows: clientKeys.map(function(k) { return clientMap[k] }).filter(function(x) { return !!x })
  readonly property int deviceHeaderIndex: ifaceKeys.length
  readonly property int rowCount: ifaceKeys.length + 1 + clientKeys.length

  function sameKeys(a, b) {
    if (a.length !== b.length) return false
    for (var i = 0; i < a.length; i++) if (a[i] !== b[i]) return false
    return true
  }
  function ifaceShown(f) {
    if (f.kind === "wifi") return true                      // radios are bridge slaves but carry the interesting traffic
    if (wan && f.name === wan.iface) return true
    if (f.slave) return false                               // ethernet ports inside the bridge: the bridge row has their sum
    if (f.kind === "ethernet" && !f.running) return false
    if (wan && wan.pppoeOn && f.name === wan.pppoeOn) return false   // the physical port under PPPoE duplicates the WAN row
    return true
  }
  function ifaceOrder(f) {
    if (wan && f.name === wan.iface) return 0
    if (f.kind === "bridge") return 1
    if (f.kind === "wifi") return 2
    if (f.kind === "zerotier" || f.kind === "wireguard" || f.kind === "tunnel") return 3
    if (f.kind === "ethernet") return 4
    return 5
  }
  function syncRows() {
    var list = snap && snap.interfaces ? snap.interfaces : []
    var shown = []
    for (var i = 0; i < list.length; i++) if (ifaceShown(list[i])) shown.push(list[i])
    shown.sort(function(a, b) { var d = ifaceOrder(a) - ifaceOrder(b); return d !== 0 ? d : (a.name < b.name ? -1 : (a.name > b.name ? 1 : 0)) })
    var im = {}, ik = []
    for (var s = 0; s < shown.length; s++) { im[shown[s].name] = shown[s]; ik.push(shown[s].name) }
    ifaceMap = im
    if (!sameKeys(ik, ifaceKeys)) ifaceKeys = ik

    var cm = {}, ck = []
    if (showDevices) {
      var cl = (clients || []).slice()
      cl.sort(function(a, b) {
        var ab = a.status === "bound" ? 0 : 1, bb = b.status === "bound" ? 0 : 1
        if (ab !== bb) return ab - bb
        return ipKey(a.ip) < ipKey(b.ip) ? -1 : (ipKey(a.ip) > ipKey(b.ip) ? 1 : 0)
      })
      for (var c = 0; c < cl.length; c++) {
        var key = cl[c].mac || cl[c].ip || ("#" + c)
        if (cm[key]) key += "#" + c
        cm[key] = cl[c]
        ck.push(key)
      }
    }
    clientMap = cm
    if (!sameKeys(ck, clientKeys)) clientKeys = ck
    if (cursorIndex > rowCount - 1) cursorIndex = Math.max(0, rowCount - 1)
  }
  function ipKey(ip) {
    var parts = String(ip || "").split(".")
    if (parts.length !== 4) return "z" + ip
    var out = ""
    for (var i = 0; i < 4; i++) out += ("000" + parts[i]).slice(-3)
    return out
  }
  onShowDevicesChanged: syncRows()

  function rowAt(idx) {
    if (idx < ifaceRows.length) return { section: "iface", item: ifaceRows[idx] }
    idx -= ifaceRows.length
    if (idx === 0) return { section: "deviceHeader", item: null }
    idx -= 1
    if (idx < clientRows.length) return { section: "client", item: clientRows[idx] }
    return { section: "", item: null }
  }
  function setCursor(idx) {
    cursorActive = true
    cursorIndex = Math.max(0, Math.min(rowCount - 1, idx))
  }
  function moveCursor(delta) {
    if (!cursorActive) { cursorActive = true; return }
    setCursor(cursorIndex + delta)
  }
  function activateCursor() {
    var row = rowAt(cursorIndex)
    if (row.section === "iface") toggleExpanded("if:" + row.item.name)
    else if (row.section === "deviceHeader") showDevices = !showDevices
    else if (row.section === "client") copyText(row.item.ip, clientLabel(row.item) + " IP")
  }
  function copyCursor() {
    var row = rowAt(cursorIndex)
    if (row.section === "iface") copyText(row.item.ips && row.item.ips.length ? row.item.ips[0] : row.item.name, row.item.name)
    else if (row.section === "client") copyText(row.item.ip, clientLabel(row.item) + " IP")
  }

  function copyText(text, label) {
    text = String(text || "")
    if (text === "") return
    if (clipboard.running) return
    clipboard.payload = text
    clipboard.stdinEnabled = true
    clipboard.running = true
    actionStatus = "Copied " + (label || "") + ": " + text
    actionStatusTimer.restart()
  }

  // wl-copy gets the text on stdin (argv is world-readable in /proc).
  Process {
    id: clipboard
    property string payload: ""
    command: [root.clipboardBin]
    stdinEnabled: true
    onStarted: {
      write(payload)
      payload = ""
      stdinEnabled = false
    }
  }
  Timer {
    id: clipboardDeadline
    interval: 5000
    repeat: false
    running: clipboard.running
    onTriggered: clipboard.running = false
  }

  function ensureVisible(item) {
    if (!item || !scrollArea.contentItem) return
    var flick = scrollArea.contentItem
    var y = item.mapToItem(panelColumn, 0, 0).y
    if (y < flick.contentY) flick.contentY = Math.max(0, y - Style.space(8))
    else if (y + item.height > flick.contentY + flick.height) flick.contentY = Math.max(0, y + item.height - flick.height + Style.space(8))
  }

  // --------------------------------------------------------------- charts
  function seriesFor(dev, rangeSec, buckets) {
    var now = Date.now() / 1000
    var start = now - rangeSec
    var rx = [], tx = [], cnt = []
    for (var b = 0; b < buckets; b++) { rx.push(0); tx.push(0); cnt.push(0) }
    var prev = null
    var bytesRx = 0, bytesTx = 0, n = 0, firstTs = 0
    var list = history || []
    for (var i = 0; i < list.length; i++) {
      var entry = list[i]
      if (!entry || entry.length < 2) continue
      var t = Number(entry[0])
      var c = entry[1] ? entry[1][dev] : null
      if (!c) { prev = null; continue }
      if (prev) {
        var dt = t - prev.t
        if (dt > 0 && dt < Math.max(900, historyStep * 3)) {
          var drx = c[0] - prev.rx, dtx = c[1] - prev.tx
          if (drx >= 0 && dtx >= 0 && t >= start) {
            var idx = Math.floor((t - start) / rangeSec * buckets)
            if (idx >= buckets) idx = buckets - 1
            if (idx < 0) idx = 0
            rx[idx] += drx / dt; tx[idx] += dtx / dt; cnt[idx] += 1
            bytesRx += drx; bytesTx += dtx; n += 1
            if (!firstTs) firstTs = prev.t
          }
        }
      }
      prev = { t: t, rx: c[0], tx: c[1] }
    }
    var max = 0
    for (var k = 0; k < buckets; k++) {
      if (cnt[k] > 0) { rx[k] /= cnt[k]; tx[k] /= cnt[k]; if (rx[k] > max) max = rx[k]; if (tx[k] > max) max = tx[k] }
      else { rx[k] = null; tx[k] = null }
    }
    return { rx: rx, tx: tx, max: max, start: start, end: now, bytesRx: bytesRx, bytesTx: bytesTx, n: n, firstTs: firstTs }
  }
  readonly property var rangeOptions: {
    var out = [{ sec: 3600, label: "1h" }, { sec: 21600, label: "6h" }, { sec: 86400, label: "24h" }]
    if (historyDays >= 7) out.push({ sec: 7 * 86400, label: "7d" })
    if (historyDays >= 30) out.push({ sec: 30 * 86400, label: "30d" })
    return out
  }
  function rangeLabel(sec) {
    for (var i = 0; i < rangeOptions.length; i++) if (rangeOptions[i].sec === sec) return rangeOptions[i].label
    return Math.round(sec / 3600) + "h"
  }

  readonly property string footerAge: {
    clockTick
    if (!lastSampleMs) return ""
    var s = "refreshed " + fmtAge((Date.now() - lastSampleMs) / 1000) + " ago"
    if (snap.tookMs) s += " · " + snap.tookMs + " ms"
    if (snap.stats && snap.stats.requests) s += " · " + snap.stats.requests + " requests, " + fmtBytes(snap.stats.bytes)
    if (snap.host) s += " · " + snap.host
    return s
  }
  readonly property string setupCommands: "/user group add name=banatik policy=read,api,rest-api,!local,!telnet,!ssh,!ftp,!reboot,!write,!policy,!test,!winbox,!password,!web,!sniff,!sensitive,!romon\n" +
    "/user add name=banatik group=banatik password=\"CHANGE-ME\" address=192.168.88.0/24\n" +
    "/certificate add name=banatik-ca common-name=\"Banatik CA\" key-size=2048 days-valid=3650 key-usage=key-cert-sign,crl-sign\n" +
    "/certificate sign banatik-ca\n" +
    "/certificate add name=banatik-www common-name=\"192.168.88.1\" subject-alt-name=IP:192.168.88.1 key-size=2048 days-valid=3650 key-usage=tls-server\n" +
    "/certificate sign banatik-www ca=banatik-ca\n" +
    "/ip service set www-ssl certificate=banatik-www disabled=no address=192.168.88.0/24 tls-version=only-1.2\n" +
    "/certificate print detail where name=banatik-www"

  // ---------------------------------------------------------------- panel
  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(660))
    contentHeight: panel.fittedContentHeight(panelColumn.implicitHeight)

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onMoveRequested: function(dx, dy) {
        if (dy !== 0) root.moveCursor(dy)
        else if (dx !== 0 && root.cursorActive) {
          var row = root.rowAt(root.cursorIndex)
          if (row.section === "iface") { if ((dx > 0) !== root.isExpanded("if:" + row.item.name)) root.toggleExpanded("if:" + row.item.name) }
        }
      }
      onActivateRequested: if (root.cursorActive) root.activateCursor()
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onTextKey: function(t) {
        if (t === "r") root.refresh()
        else if (t === "c") root.copyCursor()
        else if (t === "d") root.showDevices = !root.showDevices
        else if (t === "g") root.logExpanded = !root.logExpanded
        else if (t === "e") root.setAllExpanded(true)
        else if (t === "w") root.setAllExpanded(false)
        else if (t === "1") root.chartRange = 3600
        else if (t === "2") root.chartRange = 21600
        else if (t === "3") root.chartRange = 86400
        else if (t === "4" && root.historyDays >= 7) root.chartRange = 7 * 86400
        else if (t === "5" && root.historyDays >= 30) root.chartRange = 30 * 86400
      }

      ScrollView {
        id: scrollArea
        anchors.fill: parent
        clip: true
        ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
        ScrollBar.vertical.policy: panelColumn.implicitHeight > height ? ScrollBar.AsNeeded : ScrollBar.AlwaysOff
        Binding {
          target: scrollArea.contentItem
          property: "interactive"
          value: panelColumn.implicitHeight > scrollArea.height
        }

        Column {
          id: panelColumn
          width: scrollArea.availableWidth
          spacing: Style.space(10)

          PanelHero {
            iconComponent: Component {
              Item {
                implicitWidth: Style.space(30)
                implicitHeight: Style.space(30)
                BanatikIcon {
                  anchors.centerIn: parent
                  iconSize: Style.space(28)
                  color: root.foreground
                }
              }
            }
            title: root.router && root.router.identity ? root.router.identity : "Banatik"
            meta: root.routerMeta
            detail: root.routerDetail
            foreground: root.foreground
            fontFamily: root.fontFamily
          }

          // ---------- Setup card ----------
          SetupCard {
            visible: !!root.setup
            width: parent.width
            reason: root.setup ? String(root.setup.reason || "") : ""
            path: root.setup ? String(root.setup.path || "") : ""
          }

          // ---------- Error ----------
          Text {
            visible: root.lastError !== "" && !root.setup
            textFormat: Text.PlainText
            width: parent.width
            text: "⚠ " + root.lastError
            color: root.urgent
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
            wrapMode: Text.WordWrap
          }

          // ---------- Internet ----------
          Column {
            visible: !root.setup
            width: parent.width
            spacing: Style.space(4)

            PanelSeparator { foreground: root.foreground }
            PanelSectionHeader { text: "Internet"; foreground: root.foreground; fontFamily: root.fontFamily }

            InfoLine { label: "WAN"; value: root.wanLine; urgentValue: root.loaded && !root.setup && !root.lastError && !root.wanUp }
            InfoLine { visible: root.wanSince !== ""; label: ""; value: root.wanSince; dimValue: true }
            InfoLine { label: "Public"; value: root.publicLine; dimValue: !(root.cloud && root.cloud.publicIp) }
            InfoLine { label: "DNS"; value: root.dns && root.dns.length ? root.dns.join(", ") : (root.loaded ? "none configured" : "…") }
            InfoLine { visible: root.cloudLine !== ""; label: "Cloud"; value: root.cloudLine; dimValue: true }
            Repeater {
              model: JSON.parse(root.wanHistoryJson)
              delegate: InfoLine {
                required property var modelData
                required property int index
                label: index === 0 ? "History" : ""
                value: modelData
                dimValue: true
              }
            }
            Repeater {
              model: JSON.parse(root.alertsJson)
              delegate: InfoLine {
                required property var modelData
                required property int index
                label: index === 0 ? "Alerts" : ""
                value: root.alertText(modelData)
                urgentValue: root.alertUrgent(modelData)
                dimValue: !root.alertUrgent(modelData) && (Date.now() / 1000 - Number(modelData.at || 0) + 0 * root.clockTick) > 3600
              }
            }
          }

          // ---------- Router ----------
          Column {
            visible: !!root.router
            width: parent.width
            spacing: Style.space(4)

            PanelSeparator { foreground: root.foreground }
            PanelSectionHeader { text: "Router"; foreground: root.foreground; fontFamily: root.fontFamily }

            InfoLine { label: "Model"; value: root.router ? ((root.router.model || root.router.board || "?") + (root.router.serial ? " · S/N " + root.router.serial : "") + (root.router.arch ? " · " + root.router.arch : "")) : "" }
            InfoLine { label: "System"; value: root.versionLine }
            InfoLine { label: "Firmware"; value: root.firmwareLine; urgentValue: false; dimValue: !(root.router && root.router.firmwarePending) }
            Text {
              visible: !!(root.router && root.router.firmwarePending)
              textFormat: Text.PlainText
              width: parent.width
              leftPadding: Style.space(66)
              text: "RouterBOARD firmware is older than RouterOS. It is already staged; a reboot (/system reboot) applies it."
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              wrapMode: Text.WordWrap
            }
            InfoLine { label: "CPU"; value: root.router ? (root.router.cpuLoad + " %" + (root.router.cpuCount ? " · " + root.router.cpuCount + "×" + (root.router.cpuFreq ? root.router.cpuFreq + " MHz " : "") + root.router.cpu : "")) : ""; urgentValue: !!(root.router && root.router.cpuLoad >= 90) }
            InfoLine { label: "Memory"; value: root.router ? (root.fmtBytes(root.router.memTotal - root.router.memFree) + " used of " + root.fmtBytes(root.router.memTotal) + " (" + root.fmtPercent(root.router.memTotal - root.router.memFree, root.router.memTotal) + ")") : ""; urgentValue: !!(root.router && root.router.memTotal && root.router.memFree / root.router.memTotal < 0.08) }
            InfoLine { label: "Storage"; value: root.router ? (root.fmtBytes(root.router.hddFree) + " free of " + root.fmtBytes(root.router.hddTotal) + (root.router.badBlocks ? " · " + root.router.badBlocks + " % bad blocks" : "")) : ""; urgentValue: !!(root.router && root.router.badBlocks) }
            InfoLine { label: "Uptime"; value: root.router && root.router.uptimeS !== null ? root.fmtDuration(root.router.uptimeS) + (root.router.date ? " · router clock " + root.router.date + " " + root.router.time + (root.router.tz ? " " + root.router.tz : "") : "") : "" }
            Repeater {
              model: JSON.parse(root.healthJson)
              delegate: InfoLine {
                required property var modelData
                required property int index
                label: index === 0 ? "Health" : ""
                value: modelData.name + ": " + modelData.value + (modelData.type === "C" ? " °C" : (modelData.type === "V" ? " V" : (modelData.type === "W" ? " W" : "")))
              }
            }
            InfoLine { visible: !!root.firewall; label: "Firewall"; value: root.firewallLine }
            Repeater {
              model: JSON.parse(root.sessionsJson)
              delegate: InfoLine {
                required property var modelData
                required property int index
                label: index === 0 ? "Logins" : ""
                value: root.sessionText(modelData)
                urgentValue: modelData.group === "full" && modelData.via !== "winbox" && modelData.via !== "ssh"
              }
            }
            InfoLine { visible: root.sessions.length === 0 && root.loaded; label: "Logins"; value: "nobody else is logged in"; dimValue: true }
            Repeater {
              model: JSON.parse(root.warningsJson)
              delegate: Text {
                required property var modelData
                textFormat: Text.PlainText
                width: parent.width
                text: "⚠ " + modelData
                color: root.urgent
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                wrapMode: Text.WordWrap
              }
            }
          }

          // ---------- Interfaces ----------
          Column {
            visible: !!root.router
            width: parent.width
            spacing: Style.space(2)

            PanelSeparator { foreground: root.foreground }
            RowLayout {
              width: parent.width
              PanelSectionHeader {
                text: "Interfaces"
                foreground: root.foreground
                fontFamily: root.fontFamily
                topPadding: 0
                Layout.fillWidth: true
              }
              Row {
                spacing: Style.space(4)
                Layout.alignment: Qt.AlignVCenter
                Repeater {
                  model: root.rangeOptions
                  delegate: RangePill {
                    required property var modelData
                    rangeSec: modelData.sec
                    label: modelData.label
                  }
                }
              }
            }

            Repeater {
              model: root.ifaceKeys
              delegate: IfaceRow {
                required property var modelData
                required property int index
                width: parent.width
                iface: root.ifaceMap[modelData] || null
                rowIndex: index
              }
            }
          }

          // ---------- Devices ----------
          Column {
            visible: !!root.router && root.showClients
            width: parent.width
            spacing: Style.space(2)

            PanelSeparator { foreground: root.foreground }
            CursorSurface {
              id: deviceHeader
              width: parent.width
              hasCursor: root.cursorActive && root.cursorIndex === root.deviceHeaderIndex
              foreground: root.foreground
              fill: root.hoverFill
              implicitHeight: deviceHeaderRow.implicitHeight + Style.space(6)
              onHasCursorChanged: if (hasCursor) root.ensureVisible(deviceHeader)
              MouseArea {
                anchors.fill: parent
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                onEntered: root.setCursor(root.deviceHeaderIndex)
                onClicked: root.showDevices = !root.showDevices
              }
              RowLayout {
                id: deviceHeaderRow
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.leftMargin: Style.space(6)
                anchors.rightMargin: Style.space(6)
                anchors.verticalCenter: parent.verticalCenter
                Text {
                  textFormat: Text.PlainText
                  text: root.showDevices ? "󰅀" : "󰅂"
                  color: root.dimmer
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.body
                }
                PanelSectionHeader {
                  text: "Devices" + (root.clients.length ? " (" + root.onlineCount + " online" + (root.clients.length > root.onlineCount ? ", " + (root.clients.length - root.onlineCount) + " known" : "") + (root.newClientCount ? " · " + root.newClientCount + " new" : "") + ")" : "") + (root.wifiClientCount ? " · " + root.plural(root.wifiClientCount, "Wi-Fi client", "Wi-Fi clients") : "")
                  foreground: root.foreground
                  fontFamily: root.fontFamily
                  topPadding: 0
                  Layout.fillWidth: true
                }
              }
            }

            Repeater {
              model: root.clientKeys
              delegate: ClientRow {
                required property var modelData
                required property int index
                width: parent.width
                client: root.clientMap[modelData] || null
                rowIndex: root.deviceHeaderIndex + 1 + index
              }
            }
          }

          // ---------- Log ----------
          Column {
            visible: !!root.router && root.showLog && root.log.length > 0
            width: parent.width
            spacing: Style.space(2)

            PanelSeparator { foreground: root.foreground }
            RowLayout {
              width: parent.width
              PanelSectionHeader {
                text: "Log" + (root.log.length ? " (last " + (root.logExpanded ? root.log.length : Math.min(12, root.log.length)) + " of " + root.log.length + ")" : "")
                foreground: root.foreground
                fontFamily: root.fontFamily
                topPadding: 0
                Layout.fillWidth: true
              }
              Text {
                textFormat: Text.PlainText
                text: root.logExpanded ? "show fewer (g)" : "show all (g)"
                color: root.accentColor
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                Layout.alignment: Qt.AlignVCenter
                MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor; onClicked: root.logExpanded = !root.logExpanded }
              }
            }
            Repeater {
              model: JSON.parse(root.logJson)
              delegate: Row {
                required property var modelData
                width: parent.width
                spacing: Style.space(8)
                Text {
                  textFormat: Text.PlainText
                  text: String(modelData.time || "").slice(-8)
                  width: Style.space(58)
                  color: root.dimmer
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                }
                Text {
                  textFormat: Text.PlainText
                  width: parent.width - Style.space(58) - Style.space(8)
                  text: modelData.message + "   [" + modelData.topics + "]"
                  color: modelData.level === "error" ? root.urgent : (modelData.level === "warning" ? root.foreground : root.dim)
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                  wrapMode: Text.WrapAnywhere
                }
              }
            }
          }

          // ---------- Footer ----------
          Column {
            width: parent.width
            spacing: Style.space(2)

            PanelSeparator { foreground: root.foreground }

            Text {
              textFormat: Text.PlainText
              visible: root.actionStatus !== ""
              width: parent.width
              text: root.actionStatus
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              elide: Text.ElideRight
            }
            Text {
              textFormat: Text.PlainText
              width: parent.width
              text: "j/k move · enter/→ expand · 1-" + root.rangeOptions.length + " chart range · c copy · r refresh · d devices · g full log · e/w expand/collapse all · esc"
              color: root.dimmer
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              wrapMode: Text.WordWrap
            }
            Text {
              textFormat: Text.PlainText
              width: parent.width
              text: (root.refreshing ? "refreshing… · " : "") + root.footerAge
              color: root.dimmer
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              elide: Text.ElideRight
            }
          }
        }
      }
    }
  }

  // ------------------------------------------------------------- components
  component InfoLine: Row {
    property string label: ""
    property string value: ""
    property bool dimValue: false
    property bool urgentValue: false
    width: parent ? parent.width : implicitWidth
    spacing: Style.space(8)

    Text {
      textFormat: Text.PlainText
      text: label
      width: Style.space(58)
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
      font.bold: true
    }
    Text {
      textFormat: Text.PlainText
      width: parent.width - Style.space(58) - Style.space(8)
      text: value
      color: urgentValue ? root.urgent : (dimValue ? root.dim : root.foreground)
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
      wrapMode: Text.WrapAnywhere
    }
  }

  component DetailLines: Column {
    property var lines: []
    width: parent ? parent.width : implicitWidth
    spacing: Style.space(1)

    Repeater {
      model: lines
      delegate: Text {
        required property var modelData
        textFormat: Text.PlainText
        width: parent.width
        leftPadding: modelData.k === "item" ? Style.space(14) : 0
        topPadding: modelData.k === "head" ? Style.space(4) : 0
        text: modelData.t
        color: modelData.k === "warn" ? root.urgent
             : (modelData.k === "head" ? root.foreground : (modelData.dim ? root.dimmer : root.dim))
        font.family: root.fontFamily
        font.pixelSize: modelData.k === "head" ? Style.font.bodySmall : Style.font.caption
        font.bold: modelData.k === "head"
        wrapMode: Text.WrapAnywhere
      }
    }
  }

  // No credentials yet: say what is missing and where it goes. Nothing here
  // runs anything on the router; the RouterOS commands are copied for the
  // user to paste into WinBox or an SSH session.
  component SetupCard: BorderSurface {
    id: setupCard
    property string reason: ""
    property string path: ""
    implicitHeight: setupInner.implicitHeight + Style.space(16)
    radius: Style.cornerRadius
    color: Util.alpha(root.foreground, 0.04)
    borderSpec: Border.controlSpec("normal", root.foreground, root.foreground)

    ColumnLayout {
      id: setupInner
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      anchors.leftMargin: Style.space(10)
      anchors.rightMargin: Style.space(10)
      spacing: Style.space(4)

      Text {
        textFormat: Text.PlainText
        Layout.fillWidth: true
        text: "Connect Banatik to your router"
        color: root.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.body
        font.bold: true
      }
      Text {
        textFormat: Text.PlainText
        Layout.fillWidth: true
        text: setupCard.reason
        color: root.urgent
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        wrapMode: Text.WordWrap
      }
      Text {
        textFormat: Text.PlainText
        Layout.fillWidth: true
        text: "1. On the router create a read-only user and enable REST over HTTPS (right click here copies the RouterOS commands; adjust the LAN address).\n" +
              "2. Create " + setupCard.path + " with mode 0600 containing:\n" +
              "     HOST=192.168.88.1\n     USER=banatik\n     PASS='the password you chose'\n" +
              "3. Run  python3 collect.py --fingerprint  from the plugin directory, compare the SHA-256 with `/certificate print detail` on the router, and add the FINGERPRINT= line it prints.\n" +
              "Banatik never sends the password before the certificate matches. Details: README, section Setup."
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        wrapMode: Text.WordWrap
      }
      Row {
        spacing: Style.space(6)
        PanelActionButton {
          iconText: "󰆏"
          tooltipText: "Copy the RouterOS commands"
          foreground: root.foreground
          hoverColor: root.foreground
          onClicked: root.copyText(root.setupCommands, "RouterOS commands")
        }
        PanelActionButton {
          iconText: "󰈔"
          tooltipText: "Copy the credentials file path"
          foreground: root.foreground
          hoverColor: root.foreground
          onClicked: root.copyText(setupCard.path, "path")
        }
      }
    }
    MouseArea {
      anchors.fill: parent
      acceptedButtons: Qt.RightButton
      z: -1
      onClicked: root.copyText(root.setupCommands, "RouterOS commands")
    }
  }

  component RangePill: BorderSurface {
    id: pill
    property int rangeSec: 3600
    property string label: ""
    readonly property bool current: root.chartRange === rangeSec
    implicitWidth: pillLabel.implicitWidth + Style.space(10)
    implicitHeight: pillLabel.implicitHeight + Style.space(4)
    radius: Style.cornerRadius
    color: current ? root.selectedFill : (pillMouse.containsMouse ? root.hoverFill : "transparent")
    borderSpec: Border.controlSpec(current ? "selected" : "normal", root.foreground, Color.accent)
    Text {
      textFormat: Text.PlainText
      id: pillLabel
      anchors.centerIn: parent
      text: pill.label
      color: pill.current ? root.foreground : root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      font.bold: pill.current
    }
    MouseArea {
      id: pillMouse
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: Qt.PointingHandCursor
      onClicked: root.chartRange = pill.rangeSec
    }
  }

  // Rate-over-time chart on a Canvas: download as a filled area, upload as a
  // line, breaks where no samples exist. `mini` is the in-row sparkline.
  component TrafficChart: Item {
    id: chart
    property var series: null
    property bool mini: false
    implicitHeight: mini ? Style.space(20) : Style.space(90)
    implicitWidth: mini ? Style.space(64) : Style.space(300)

    onSeriesChanged: canvas.requestPaint()
    onWidthChanged: canvas.requestPaint()
    onHeightChanged: canvas.requestPaint()
    Connections {
      target: root
      function onForegroundChanged() { canvas.requestPaint() }
      function onAccentColorChanged() { canvas.requestPaint() }
    }

    function tracePath(ctx, values, x0, w, y0, h, max, closeArea) {
      var n = values.length
      if (n === 0) return
      var step = w / n
      var open = false
      var lastX = 0, startX = 0
      for (var i = 0; i <= n; i++) {
        var v = i < n ? values[i] : null
        var x = x0 + (i + 0.5) * step
        if (v === null || v === undefined) {
          if (open) {
            if (closeArea) { ctx.lineTo(lastX, y0 + h); ctx.lineTo(startX, y0 + h); ctx.closePath() }
            open = false
          }
          continue
        }
        var y = y0 + h - (max > 0 ? (v / max) * h : 0)
        if (!open) {
          if (closeArea) { ctx.moveTo(x, y0 + h); ctx.lineTo(x, y) } else ctx.moveTo(x, y)
          startX = x
          open = true
        } else ctx.lineTo(x, y)
        lastX = x
      }
    }

    Canvas {
      id: canvas
      anchors.fill: parent
      antialiasing: true
      onPaint: {
        var ctx = getContext("2d")
        ctx.reset()
        ctx.clearRect(0, 0, width, height)
        var sr = chart.series
        var fg = root.foreground
        var pad = chart.mini ? 1 : Style.space(2)
        var labelH = chart.mini ? 0 : Style.space(12)
        var x0 = pad, w = width - pad * 2
        var y0 = pad, h = height - pad * 2 - labelH
        ctx.strokeStyle = Qt.rgba(fg.r, fg.g, fg.b, chart.mini ? 0.15 : 0.22)
        ctx.lineWidth = 1
        ctx.beginPath(); ctx.moveTo(x0, y0 + h + 0.5); ctx.lineTo(x0 + w, y0 + h + 0.5); ctx.stroke()
        if (!sr || sr.n === 0) {
          if (!chart.mini) {
            ctx.fillStyle = Qt.rgba(fg.r, fg.g, fg.b, 0.45)
            ctx.font = Style.font.caption + "px " + root.fontFamily
            ctx.textAlign = "center"
            ctx.fillText("collecting history…", x0 + w / 2, y0 + h / 2 + 4)
          }
          return
        }
        var max = Math.max(sr.max, 1)
        var rxc = fg
        ctx.fillStyle = Qt.rgba(rxc.r, rxc.g, rxc.b, chart.mini ? 0.35 : 0.28)
        ctx.beginPath(); chart.tracePath(ctx, sr.rx, x0, w, y0, h, max, true); ctx.fill()
        ctx.strokeStyle = Qt.rgba(rxc.r, rxc.g, rxc.b, 0.95)
        ctx.lineWidth = chart.mini ? 1 : 1.5
        ctx.lineJoin = "round"
        ctx.beginPath(); chart.tracePath(ctx, sr.rx, x0, w, y0, h, max, false); ctx.stroke()
        var txc = root.accentColor
        if (!chart.mini) {
          ctx.fillStyle = Qt.rgba(txc.r, txc.g, txc.b, 0.14)
          ctx.beginPath(); chart.tracePath(ctx, sr.tx, x0, w, y0, h, max, true); ctx.fill()
        }
        ctx.strokeStyle = Qt.rgba(txc.r, txc.g, txc.b, 0.95)
        ctx.lineWidth = chart.mini ? 1 : 1.5
        ctx.beginPath(); chart.tracePath(ctx, sr.tx, x0, w, y0, h, max, false); ctx.stroke()
        if (chart.mini) return
        ctx.font = Style.font.caption + "px " + root.fontFamily
        ctx.fillStyle = Qt.rgba(fg.r, fg.g, fg.b, 0.6)
        ctx.textAlign = "left"
        ctx.fillText("↓ download   ↑ upload   peak " + root.fmtRate(sr.max), x0 + 2, y0 + Style.font.caption)
        var ticks = 4
        for (var i = 0; i <= ticks; i++) {
          var frac = i / ticks
          var tx = x0 + frac * w
          ctx.textAlign = i === 0 ? "left" : (i === ticks ? "right" : "center")
          ctx.fillText(root.fmtClock(sr.start + frac * (sr.end - sr.start)), tx, y0 + h + labelH - 1)
          ctx.beginPath(); ctx.moveTo(Math.round(tx) + 0.5, y0 + h); ctx.lineTo(Math.round(tx) + 0.5, y0 + h + 3); ctx.stroke()
        }
      }
    }
  }

  component IfaceRow: CursorSurface {
    id: ifaceRow
    property var iface: null
    property int rowIndex: 0
    readonly property string key: "if:" + (iface ? iface.name : "")
    readonly property bool expandedRow: root.isExpanded(key)
    readonly property var rate: iface ? root.rateOf(iface.name) : { rx: 0, tx: 0 }
    readonly property bool inactive: iface && !iface.running
    readonly property string role: root.ifaceRole(iface)

    hasCursor: root.cursorActive && root.cursorIndex === rowIndex
    current: role === "INTERNET"
    foreground: root.foreground
    fill: root.hoverFill
    currentFill: root.selectedFill
    implicitHeight: ifaceInner.implicitHeight + Style.space(10)
    onHasCursorChanged: if (hasCursor) root.ensureVisible(ifaceRow)

    MouseArea {
      id: ifaceMouse
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: Qt.PointingHandCursor
      acceptedButtons: Qt.LeftButton | Qt.RightButton
      onEntered: root.setCursor(ifaceRow.rowIndex)
      onClicked: function(mouse) {
        if (!ifaceRow.iface) return
        if (mouse.button === Qt.RightButton) root.copyText(ifaceRow.iface.ips && ifaceRow.iface.ips.length ? ifaceRow.iface.ips[0] : ifaceRow.iface.name, ifaceRow.iface.name)
        else root.toggleExpanded(ifaceRow.key)
      }
      PanelToolTip {
        visible: ifaceMouse.containsMouse && !ifaceRow.expandedRow && ifaceRow.iface !== null
        text: ifaceRow.iface ? root.plain(root.ifaceTooltip(ifaceRow.iface)) : ""
        fontFamily: root.fontFamily
      }
    }

    Column {
      id: ifaceInner
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.leftMargin: Style.space(6)
      anchors.rightMargin: Style.space(6)
      anchors.verticalCenter: parent.verticalCenter
      spacing: Style.space(4)

      RowLayout {
        width: parent.width
        spacing: Style.space(6)

        Text {
          textFormat: Text.PlainText
          text: ifaceRow.iface ? root.kindGlyph(ifaceRow.iface.kind) : ""
          color: ifaceRow.inactive ? root.dimmer : root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.title
          Layout.preferredWidth: Style.space(20)
          horizontalAlignment: Text.AlignHCenter
          Layout.alignment: Qt.AlignVCenter
        }
        Text {
          textFormat: Text.PlainText
          text: ifaceRow.iface ? ifaceRow.iface.name : ""
          color: ifaceRow.inactive ? root.dim : root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          font.bold: true
          Layout.alignment: Qt.AlignVCenter
        }
        BorderSurface {
          visible: ifaceRow.role !== ""
          implicitWidth: pillText.implicitWidth + Style.space(8)
          implicitHeight: pillText.implicitHeight + Style.space(2)
          Layout.alignment: Qt.AlignVCenter
          color: "transparent"
          borderSpec: Border.controlSpec("normal", root.foreground, Color.accent)
          radius: Style.cornerRadius
          Text {
            textFormat: Text.PlainText
            id: pillText
            anchors.centerIn: parent
            text: ifaceRow.role
            color: ifaceRow.role === "INTERNET" && !root.wanUp ? root.urgent : root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            font.bold: true
          }
        }
        Text {
          textFormat: Text.PlainText
          Layout.fillWidth: true
          text: ifaceRow.iface ? root.ifaceSubtitle(ifaceRow.iface) : ""
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          elide: Text.ElideRight
          Layout.alignment: Qt.AlignVCenter
        }
        Text {
          textFormat: Text.PlainText
          visible: !ifaceRow.inactive
          text: "↓ " + root.fmtRate(ifaceRow.rate.rx) + "  ↑ " + root.fmtRate(ifaceRow.rate.tx)
          color: (ifaceRow.rate.rx > 0 || ifaceRow.rate.tx > 0) ? root.foreground : root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          Layout.alignment: Qt.AlignVCenter
        }
        Text {
          textFormat: Text.PlainText
          text: ifaceRow.expandedRow ? "󰅀" : "󰅂"
          color: root.dimmer
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          Layout.alignment: Qt.AlignVCenter
        }
      }

      TrafficChart {
        id: rowChart
        visible: !ifaceRow.inactive
        width: parent.width
        height: ifaceRow.expandedRow ? Style.space(96) : Style.space(28)
        mini: !ifaceRow.expandedRow
        series: ifaceRow.inactive || !ifaceRow.iface ? null : root.seriesFor(ifaceRow.iface.name, root.chartRange, Math.max(20, Math.min(Math.floor(width / 3), Math.floor(root.chartRange / 60))))
        Behavior on height { NumberAnimation { duration: 120; easing.type: Easing.OutCubic } }
      }

      Text {
        textFormat: Text.PlainText
        visible: ifaceRow.expandedRow && !ifaceRow.inactive
        width: parent.width
        text: {
          var sr = rowChart.series
          if (!sr || sr.n === 0) return "History is recorded while the bar runs (every 30 s, 5-minute averages kept " + root.historyDays + " days). No samples for this range yet."
          var t = "Last " + root.rangeLabel(root.chartRange) + ": ↓ " + root.fmtBytes(sr.bytesRx) + " ↑ " + root.fmtBytes(sr.bytesTx) + " · peak " + root.fmtRate(sr.max)
          if (sr.firstTs > sr.start + 120) t += " · data since " + root.fmtClock(sr.firstTs)
          return t
        }
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        wrapMode: Text.WordWrap
      }

      DetailLines {
        visible: ifaceRow.expandedRow
        width: parent.width
        readonly property string detailJson: ifaceRow.expandedRow && ifaceRow.iface ? JSON.stringify(root.ifaceDetailLines(ifaceRow.iface)) : "[]"
        lines: JSON.parse(detailJson)
      }
    }
  }

  component ClientRow: CursorSurface {
    id: clientRow
    property var client: null
    property int rowIndex: 0
    readonly property var wifiInfo: client ? root.wifiClientFor(client.mac) : null
    readonly property bool offline: client && client.status !== "bound"

    hasCursor: root.cursorActive && root.cursorIndex === rowIndex
    foreground: root.foreground
    fill: root.hoverFill
    implicitHeight: clientInner.implicitHeight + Style.space(6)
    onHasCursorChanged: if (hasCursor) root.ensureVisible(clientRow)

    MouseArea {
      id: clientMouse
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: Qt.PointingHandCursor
      acceptedButtons: Qt.LeftButton | Qt.RightButton
      onEntered: root.setCursor(clientRow.rowIndex)
      onClicked: function(mouse) {
        if (!clientRow.client) return
        if (mouse.button === Qt.RightButton) root.copyText(clientRow.client.mac, root.clientLabel(clientRow.client) + " MAC")
        else root.copyText(clientRow.client.ip, root.clientLabel(clientRow.client) + " IP")
      }
      PanelToolTip {
        visible: clientMouse.containsMouse && clientRow.client !== null
        text: clientRow.client ? root.plain(root.clientTooltip(clientRow.client)) : ""
        fontFamily: root.fontFamily
      }
    }

    Row {
      id: clientInner
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.leftMargin: Style.space(12)
      anchors.rightMargin: Style.space(6)
      anchors.verticalCenter: parent.verticalCenter
      spacing: Style.space(8)

      Text {
        textFormat: Text.PlainText
        text: clientRow.wifiInfo ? "󰖩" : (clientRow.offline ? "󰅙" : "󰈀")
        width: Style.space(18)
        horizontalAlignment: Text.AlignHCenter
        color: clientRow.offline ? root.dimmer : root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.body
      }
      Text {
        textFormat: Text.PlainText
        text: root.clientLabel(clientRow.client)
        width: Style.space(170)
        color: clientRow.offline ? root.dim : root.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        font.bold: !clientRow.offline
        elide: Text.ElideRight
      }
      Text {
        textFormat: Text.PlainText
        text: clientRow.client ? root.clientSubtitle(clientRow.client) : ""
        width: parent.width - Style.space(18) - Style.space(170) - Style.space(8) * 3 - (newTag.visible ? newTag.width : 0)
        color: clientRow.offline ? root.dimmer : root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        elide: Text.ElideRight
      }
      Text {
        id: newTag
        textFormat: Text.PlainText
        visible: root.clientIsNew(clientRow.client)
        text: "new"
        color: root.accentColor
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        font.bold: true
      }
    }
  }
}
