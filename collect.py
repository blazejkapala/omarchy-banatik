#!/usr/bin/python3 -I
"""Banatik collector: one JSON document about a MikroTik router, read over its
REST API. No root, no subprocesses, no shell: the only thing this program does
is HTTPS GET / POST-print requests to the one router named in the credentials
file, then it prints what it learned.

Boundaries, so the reader does not have to hunt for them:
  * credentials live in $XDG_CONFIG_HOME/banatik/credentials (HOST, USER, PASS,
    FINGERPRINT, optional PORT); the file must be a regular file owned by this
    user, mode 0600, read through a descriptor with O_NOFOLLOW and a byte cap.
    They are never written, never printed, never passed on argv.
  * TLS: HTTPS only, TLS 1.2+, the server certificate must match FINGERPRINT
    (SHA-256 of the DER certificate) before a single byte of the request -
    including the Authorization header - is sent. Nothing else is trusted:
    no system CA store, no redirects (they are never followed), one host, one port.
  * every response is read through a hard byte cap; the document is bounded
    (string lengths, list sizes) before it is printed.
  * the RouterOS user is expected to be read-only (policy read,api,rest-api).
    This program only ever issues GET and the POST ".../print" read verb.
  * state (what was seen last time, for alerts) and traffic history live in a
    private 0700 cache directory; they hold no credentials.
"""
import base64
import hashlib
import http.client
import ipaddress
import json
import os
import re
import socket
import sqlite3
import ssl
import stat
import sys
import tempfile
import time
import urllib.parse

HOME = os.path.expanduser("~")
CONFIG_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.join(HOME, ".config")), "banatik")
CREDENTIALS = os.path.join(CONFIG_DIR, "credentials")
CACHE_DIR = os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.join(HOME, ".cache")), "omarchy-banatik")
STATE_FILE = os.path.join(CACHE_DIR, "state.json")
HISTORY_FILE = os.path.join(CACHE_DIR, "history.jsonl")   # pre-0.2 format, imported once into the database
HISTORY_DB = "history.sqlite"

VERSION = "0.2.1"
MAX_STR = 300
MAX_LIST = 400
CACHE_MAX = 4 * 1024 * 1024
CRED_MAX = 4096
RESP_MAX = 512 * 1024           # per response
LOG_RESP_MAX = 1024 * 1024      # the log is the one big one
CONNECT_TIMEOUT = 3.0
REQUEST_TIMEOUT = 4.0
DEADLINE = 9.0                  # whole run, so the widget never waits longer

HISTORY_STEP = 30               # raw sample interval
RAW_KEEP = 48 * 3600            # raw samples: two days, enough for the 24 h chart
AGG_STEP = 300                  # aggregate buckets: 5 minutes, kept HISTORY_DAYS
HISTORY_DAYS_DEFAULT = 7
HISTORY_DAYS_MAX = 365
HISTORY_MAX_LINES = int(RAW_KEEP / HISTORY_STEP) + 50   # longest history list in the document
RANGE_MAX = HISTORY_DAYS_MAX * 86400
ALERT_KEEP = 24 * 3600
ALERT_MAX = 50
WAN_LOG_KEEP = 7 * 24 * 3600
WAN_LOG_MAX = 200
CLIENT_FORGET = 30 * 24 * 3600  # a MAC not seen for a month is "new" again
CLIENT_NEW_FOR = 3600
LOG_LINES = 40

OPTS = {"log": False, "history": 0, "demo": False, "clients": True, "history_days": HISTORY_DAYS_DEFAULT, "history_dir": ""}
WARNINGS = []
STARTED = time.time()


# --------------------------------------------------------------------------- small helpers

def clip(value, limit=MAX_STR):
    return str(value if value is not None else "")[:limit]


def bound(obj):
    """Final guard before the document is printed: no string longer than
    MAX_STR, no list or object with more than MAX_LIST entries. The traffic
    history is the one list allowed to be long (2880 rows for 24 h); it is
    built here from our own file, bounded by HISTORY_MAX_LINES."""
    if isinstance(obj, dict) and "history" in obj and isinstance(obj.get("history"), list):
        history = [[float(e[0]), {str(k)[:64]: [int(v[0]), int(v[1])] for k, v in list(e[1].items())[:MAX_LIST]}] for e in obj["history"][-HISTORY_MAX_LINES:]
                   if isinstance(e, list) and len(e) == 2 and isinstance(e[1], dict)]
        rest = bound({k: v for k, v in obj.items() if k != "history"})
        rest["history"] = history
        return rest
    if isinstance(obj, str):
        return obj[:MAX_STR]
    if isinstance(obj, list):
        return [bound(x) for x in obj[:MAX_LIST]]
    if isinstance(obj, dict):
        return {str(k)[:MAX_STR]: bound(v) for k, v in list(obj.items())[:MAX_LIST]}
    return obj


def warn(text):
    text = clip(text, 200)
    if text not in WARNINGS:
        WARNINGS.append(text)


def remaining():
    return DEADLINE - (time.time() - STARTED)


def to_int(value, default=0):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def to_bool(value):
    return str(value).strip().lower() in ("true", "yes", "1")


_DUR = re.compile(r"(\d+)(ms|us|w|d|h|m|s)")
_DUR_MUL = {"w": 604800, "d": 86400, "h": 3600, "m": 60, "s": 1, "ms": 0.001, "us": 0.000001}


def duration_s(text):
    """RouterOS durations: 16w5d3h56m14s, 3h40m49s, 15s10ms, 0ms -> seconds."""
    text = str(text or "").strip()
    if not text:
        return None
    total = 0.0
    matched = False
    for num, unit in _DUR.findall(text):
        total += int(num) * _DUR_MUL[unit]
        matched = True
    return total if matched else None


def fmt_duration(seconds):
    seconds = int(seconds or 0)
    if seconds < 60:
        return "%ds" % seconds
    if seconds < 3600:
        return "%dm" % (seconds // 60)
    if seconds < 86400:
        return "%dh %dm" % (seconds // 3600, (seconds % 3600) // 60)
    if seconds < 7 * 86400:
        return "%dd %dh" % (seconds // 86400, (seconds % 86400) // 3600)
    return "%dw %dd" % (seconds // 604800, (seconds % 604800) // 86400)


def parse_ros_time(text):
    """'2026-09-14 13:01:20' in the router's local time -> epoch (this machine's
    zone; close enough for 'N minutes ago', both usually sit in the same zone)."""
    try:
        return time.mktime(time.strptime(str(text).strip(), "%Y-%m-%d %H:%M:%S"))
    except (ValueError, TypeError, OverflowError):
        return None


# --------------------------------------------------------------------------- private files

def ensure_cache_dir():
    try:
        os.mkdir(CACHE_DIR, 0o700)
    except FileExistsError:
        pass
    st = os.lstat(CACHE_DIR)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise OSError("%s is not a directory owned by this user" % CACHE_DIR)
    if st.st_mode & 0o077:
        os.chmod(CACHE_DIR, 0o700)


def read_private_file(path, max_bytes, require_0600=False):
    """Descriptor-bound read: no symlink following, regular file owned by this
    user, at most max_bytes, optionally nobody else may read it."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_size > max_bytes:
            raise OSError("%s is not a private regular file under %d bytes" % (path, max_bytes))
        if require_0600 and st.st_mode & 0o077:
            raise OSError("%s is readable by others (mode %o); chmod 600 it" % (path, st.st_mode & 0o777))
        chunks, total = [], 0
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise OSError("%s grew past %d bytes" % (path, max_bytes))
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def write_private_file(path, data):
    """Exclusive random-named 0600 temporary in the cache directory, fsync,
    atomic rename, directory fsync."""
    ensure_cache_dir()
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", dir=CACHE_DIR)
    try:
        view = memoryview(data)
        while view:
            n = os.write(fd, view)
            view = view[n:]
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(tmp, path)
    except OSError:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    dfd = os.open(CACHE_DIR, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def load_json_file(path, default, max_bytes=CACHE_MAX):
    try:
        value = json.loads(read_private_file(path, max_bytes).decode("utf-8", "replace"))
    except (OSError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


def save_json_file(path, value):
    try:
        write_private_file(path, json.dumps(value, separators=(",", ":")).encode())
    except OSError:
        pass


# --------------------------------------------------------------------------- credentials

HOSTNAME_RE = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")
FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")


class CredentialsError(Exception):
    pass


def load_credentials():
    """KEY=VALUE lines. Values may be wrapped in single or double quotes.
    Returns dict with host, port, user, password, fingerprint - all validated."""
    try:
        raw = read_private_file(CREDENTIALS, CRED_MAX, require_0600=True).decode("utf-8", "replace")
    except FileNotFoundError:
        raise CredentialsError("no credentials file")
    except OSError as e:
        raise CredentialsError(str(e))
    values = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().upper()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    host = values.get("HOST", "").strip().lower()
    if not host:
        raise CredentialsError("HOST is missing in the credentials file")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if not HOSTNAME_RE.match(host):
            raise CredentialsError("HOST is neither an IP address nor a host name")
    port = to_int(values.get("PORT", "443"), 0)
    if not 1 <= port <= 65535:
        raise CredentialsError("PORT must be 1-65535")
    user = values.get("USER", "")
    password = values.get("PASS", "")
    if not user or not password:
        raise CredentialsError("USER or PASS is missing in the credentials file")
    if not re.match(r"^[A-Za-z0-9_.@-]{1,64}$", user):
        raise CredentialsError("USER contains characters RouterOS does not allow")
    if len(password) > 256 or any(ord(c) < 32 or ord(c) == 127 for c in password):
        raise CredentialsError("PASS is too long or contains control characters")
    fingerprint = values.get("FINGERPRINT", "").strip().lower().replace(":", "")
    if not fingerprint:
        raise CredentialsError("FINGERPRINT is missing; run `collect.py --fingerprint` and pin the value")
    if not FINGERPRINT_RE.match(fingerprint):
        raise CredentialsError("FINGERPRINT must be 64 hex characters (SHA-256 of the certificate)")
    return {"host": host, "port": port, "user": user, "password": password, "fingerprint": fingerprint}


# --------------------------------------------------------------------------- REST client

class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """The handshake must present exactly the pinned certificate. Checked in
    connect(), before any request is written to the socket."""

    def __init__(self, host, port, fingerprint, timeout):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE   # replaced by the pin below; the system CA store is not consulted
        http.client.HTTPSConnection.__init__(self, host, port, timeout=timeout, context=ctx)
        self._pin = fingerprint

    def connect(self):
        http.client.HTTPSConnection.connect(self)
        try:
            der = self.sock.getpeercert(binary_form=True)
        except (ssl.SSLError, OSError, AttributeError):
            der = None
        seen = hashlib.sha256(der).hexdigest() if der else ""
        if not der or seen != self._pin:
            self.close()
            raise ssl.SSLError("certificate fingerprint mismatch (seen %s)" % (seen[:16] + "…" if seen else "none"))


class Router:
    def __init__(self, creds):
        self.creds = creds
        self.conn = None
        self.auth = "Basic " + base64.b64encode(("%s:%s" % (creds["user"], creds["password"])).encode()).decode()
        self.requests = 0
        self.bytes = 0
        self.errors = []

    def _connect(self):
        if self.conn is None:
            self.conn = PinnedHTTPSConnection(self.creds["host"], self.creds["port"], self.creds["fingerprint"], CONNECT_TIMEOUT)
            self.conn.connect()
        return self.conn

    def close(self):
        if self.conn is not None:
            try:
                self.conn.close()
            except OSError:
                pass
            self.conn = None

    def _request(self, method, path, body, max_bytes):
        conn = self._connect()
        conn.sock.settimeout(min(REQUEST_TIMEOUT, max(0.5, remaining())))
        headers = {"Authorization": self.auth, "Accept": "application/json", "User-Agent": "omarchy-banatik/" + VERSION, "Connection": "keep-alive"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        raw = resp.read(max_bytes + 1)
        if len(raw) > max_bytes:
            self.close()
            raise OSError("%s: response exceeds %d bytes" % (path, max_bytes))
        self.requests += 1
        self.bytes += len(raw)
        if resp.status in (301, 302, 303, 307, 308):
            self.close()
            raise OSError("%s: redirect refused" % path)
        if resp.status == 401:
            raise PermissionError("router rejected the user name or password (401)")
        if resp.status != 200:
            detail = ""
            try:
                detail = json.loads(raw.decode("utf-8", "replace")).get("detail", "")
            except (ValueError, AttributeError):
                pass
            raise OSError("%s: HTTP %d %s" % (path, resp.status, clip(detail, 120)))
        try:
            return json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            raise OSError("%s: response is not JSON" % path)

    def get(self, path, default=None, max_bytes=RESP_MAX, required=False):
        """GET /rest/<path>. Missing commands (feature not on this RouterOS) and
        errors become `default` plus a warning, unless required."""
        if remaining() < 0.5:
            self.errors.append("%s: skipped, out of time" % path)
            return default
        try:
            return self._request("GET", "/rest/" + path.lstrip("/"), None, max_bytes)
        except PermissionError:
            raise
        except (OSError, ssl.SSLError, socket.timeout, http.client.HTTPException) as e:
            self.close()
            msg = clip(str(e), 160)
            if required:
                raise OSError(msg)
            if "no such command" not in msg and "HTTP 400" not in msg:
                self.errors.append(msg)
            return default

    def print_(self, path, body, default=None, max_bytes=RESP_MAX):
        """POST /rest/<path>/print: the read verb with options (count-only,
        .proplist, .query). Never anything else."""
        if remaining() < 0.5:
            return default
        try:
            return self._request("POST", "/rest/" + path.strip("/") + "/print", body, max_bytes)
        except PermissionError:
            raise
        except (OSError, ssl.SSLError, socket.timeout, http.client.HTTPException) as e:
            self.close()
            self.errors.append(clip(str(e), 160))
            return default


def cmd_fingerprint():
    """`collect.py --fingerprint`: show the certificate the router presents so
    the user can pin it (trust on first use, done once, by hand)."""
    try:
        raw = read_private_file(CREDENTIALS, CRED_MAX, require_0600=True).decode("utf-8", "replace")
    except OSError as e:
        sys.stderr.write("cannot read %s: %s\n" % (CREDENTIALS, e))
        return 2
    host = port = ""
    for line in raw.splitlines():
        k, _, v = line.strip().partition("=")
        v = v.strip().strip("'\"")
        if k.strip().upper() == "HOST":
            host = v.strip().lower()
        elif k.strip().upper() == "PORT":
            port = v
    port = to_int(port or "443", 443)
    if not host:
        sys.stderr.write("HOST is missing in %s\n" % CREDENTIALS)
        return 2
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((host, port), timeout=CONNECT_TIMEOUT) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                der = tls.getpeercert(binary_form=True)
                info = ssl.DER_cert_to_PEM_cert(der)
    except (OSError, ssl.SSLError) as e:
        sys.stderr.write("TLS to %s:%d failed: %s\n" % (host, port, e))
        return 1
    fp = hashlib.sha256(der).hexdigest()
    sys.stdout.write("Certificate presented by %s:%d\n" % (host, port))
    sys.stdout.write("SHA-256 fingerprint: %s\n\n" % fp)
    sys.stdout.write("Compare it with `/certificate print detail` on the router (field `fingerprint`).\n")
    sys.stdout.write("If it matches, add this line to %s:\n\nFINGERPRINT=%s\n" % (CREDENTIALS, fp))
    del info
    return 0


# --------------------------------------------------------------------------- collectors

def iface_kind(row):
    t = str(row.get("type", ""))
    name = str(row.get("name", ""))
    if t == "ether":
        return "ethernet"
    if t in ("wifi", "wlan", "wireless"):
        return "wifi"
    if t == "bridge":
        return "bridge"
    if t.startswith("pppoe") or t.startswith("ppp") or t == "lte":
        return "wan"
    if t == "zerotier":
        return "zerotier"
    if t in ("wg", "wireguard"):
        return "wireguard"
    if t in ("l2tp-out", "sstp-out", "ovpn-out", "pptp-out", "ipip", "gre", "eoip", "6to4", "vxlan", "l2tp-in", "sstp-in", "ovpn-in", "pptp-in"):
        return "tunnel"
    if t == "vlan":
        return "vlan"
    if t == "loopback" or name == "lo":
        return "loopback"
    return "other"


def collect_interfaces(r, addresses):
    rows = r.get("interface", [], required=True)
    if not isinstance(rows, list):
        raise OSError("interface list is not a list")
    out = []
    ip_by_iface = {}
    for a in addresses:
        if to_bool(a.get("disabled")) or to_bool(a.get("invalid")):
            continue
        ip_by_iface.setdefault(str(a.get("actual-interface") or a.get("interface", "")), []).append(str(a.get("address", "")))
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = clip(row.get("name", ""), 64)
        kind = iface_kind(row)
        if kind == "loopback":
            continue
        out.append({
            "name": name,
            "type": clip(row.get("type", ""), 32),
            "kind": kind,
            "running": to_bool(row.get("running")),
            "disabled": to_bool(row.get("disabled")),
            "slave": to_bool(row.get("slave")),
            "comment": clip(row.get("comment", ""), 80),
            "mac": clip(row.get("mac-address", ""), 17),
            "mtu": to_int(row.get("actual-mtu") or row.get("mtu"), 0),
            "rx": to_int(row.get("rx-byte")),
            "tx": to_int(row.get("tx-byte")),
            "rxPackets": to_int(row.get("rx-packet")),
            "txPackets": to_int(row.get("tx-packet")),
            "rxErrors": to_int(row.get("rx-error")),
            "txErrors": to_int(row.get("tx-error")),
            "rxDrops": to_int(row.get("rx-drop")),
            "txDrops": to_int(row.get("tx-drop")) + to_int(row.get("tx-queue-drop")),
            "linkDowns": to_int(row.get("link-downs")),
            "lastUp": clip(row.get("last-link-up-time", ""), 24),
            "lastDown": clip(row.get("last-link-down-time", ""), 24),
            "ips": ip_by_iface.get(name, [])[:8],
        })
    return out


def collect_wan(r, interfaces, addresses):
    routes = r.get("ip/route?dst-address=0.0.0.0/0", [])
    if not isinstance(routes, list):
        routes = []
    active = [x for x in routes if isinstance(x, dict) and to_bool(x.get("active"))]
    by_name = {i["name"]: i for i in interfaces}
    wan = {"present": bool(routes), "up": bool(active), "iface": "", "kind": "", "gateway": "", "ip": "", "sinceUp": None, "detail": ""}
    if not routes:
        wan["detail"] = "no default route configured"
    route = active[0] if active else (routes[0] if routes else None)
    if route:
        gw = str(route.get("gateway", ""))
        immediate = str(route.get("immediate-gw", "")) or gw
        dev = ""
        if gw in by_name:
            dev = gw
        else:
            # "10.0.0.1%ether1" or plain address: find the interface whose network holds it
            if "%" in immediate:
                dev = immediate.split("%", 1)[1]
            else:
                try:
                    g = ipaddress.ip_address(gw)
                    for a in addresses:
                        try:
                            if g in ipaddress.ip_interface(str(a.get("address"))).network:
                                dev = str(a.get("actual-interface") or a.get("interface", ""))
                                break
                        except ValueError:
                            continue
                except ValueError:
                    dev = ""
            wan["gateway"] = clip(gw, 64)
        wan["iface"] = clip(dev, 64)
        i = by_name.get(dev)
        if i:
            wan["ip"] = (i.get("ips") or [""])[0]
            wan["sinceUp"] = i.get("lastUp") or None
            wan["linkDowns"] = i.get("linkDowns", 0)
            if not i.get("running"):
                wan["up"] = False
                wan["detail"] = dev + " is not running"
    # PPPoE / DHCP client details for that interface
    pppoe = r.get("interface/pppoe-client", [])
    if isinstance(pppoe, list):
        for p in pppoe:
            if isinstance(p, dict) and str(p.get("name")) == wan["iface"]:
                wan["kind"] = "pppoe"
                wan["pppoeUser"] = clip(p.get("user", ""), 64)
                wan["pppoeOn"] = clip(p.get("interface", ""), 64)
                wan["acName"] = clip(p.get("ac-name", ""), 64)
                if not to_bool(p.get("running")):
                    wan["up"] = False
                    wan["detail"] = "PPPoE session down"
    dhcp = r.get("ip/dhcp-client", [])
    if isinstance(dhcp, list):
        for d in dhcp:
            if not isinstance(d, dict) or to_bool(d.get("disabled")):
                continue
            if str(d.get("interface")) == wan["iface"] or (wan.get("pppoeOn") and str(d.get("interface")) == wan["pppoeOn"] and not wan["kind"]):
                if wan["kind"] != "pppoe":
                    wan["kind"] = "dhcp"
                    wan["dhcpStatus"] = clip(d.get("status", ""), 32)
                    wan["dhcpExpires"] = clip(d.get("expires-after", ""), 32)
                    if str(d.get("status", "")).startswith("bound"):
                        wan["ip"] = wan["ip"] or clip(d.get("address", ""), 64)
                        wan["gateway"] = wan["gateway"] or clip(d.get("gateway", ""), 64)
                    else:
                        wan["up"] = False
                        wan["detail"] = "DHCP client: " + clip(d.get("status", ""), 32)
    if not wan["kind"] and wan["iface"]:
        wan["kind"] = "static"
    return wan


def collect_wifi(r, leases_by_mac, arp_by_mac):
    radios = r.get("interface/wifi", [])
    table = r.get("interface/wifi/registration-table", [])
    if radios is None or not isinstance(radios, list):
        radios = []
        legacy = r.get("interface/wireless", [])
        if isinstance(legacy, list) and legacy:
            for w in legacy:
                if isinstance(w, dict):
                    radios.append({"name": w.get("name"), "configuration.ssid": w.get("ssid"), "channel.frequency": w.get("frequency"), "channel.width": w.get("channel-width"),
                                   "master": "true" if not w.get("master-interface") else "false", "master-interface": w.get("master-interface", ""),
                                   "running": w.get("running"), "disabled": w.get("disabled"), "configuration.mode": w.get("mode")})
            table = r.get("interface/wireless/registration-table", [])
    if not isinstance(table, list):
        table = []
    radios_out = []
    for w in radios:
        if not isinstance(w, dict):
            continue
        radios_out.append({
            "name": clip(w.get("name", ""), 64),
            "ssid": clip(w.get("configuration.ssid") or w.get("ssid") or "", 64),
            "mode": clip(w.get("configuration.mode") or w.get("mode") or "", 16),
            "freq": to_int(w.get("channel.frequency") or w.get("frequency"), 0),
            "width": clip(w.get("channel.width") or w.get("channel-width") or "", 16),
            "master": to_bool(w.get("master", "true")),
            "masterOf": clip(w.get("master-interface", ""), 64),
            "running": to_bool(w.get("running")),
            "disabled": to_bool(w.get("disabled")),
            "clients": 0,
        })
    by_name = {x["name"]: x for x in radios_out}
    clients = []
    for c in table:
        if not isinstance(c, dict):
            continue
        mac = str(c.get("mac-address", "")).upper()
        lease = leases_by_mac.get(mac) or {}
        b = str(c.get("bytes", "0,0")).split(",")
        client = {
            "mac": clip(mac, 17),
            "iface": clip(c.get("interface", ""), 64),
            "ssid": clip(c.get("ssid", ""), 64),
            "band": clip(c.get("band", ""), 16),
            "signal": to_int(c.get("signal") or str(c.get("signal-strength", "")).split("@")[0], 0),
            "uptime": duration_s(c.get("uptime")),
            "lastActivity": duration_s(c.get("last-activity")),
            "rxRate": to_int(c.get("rx-rate")),
            "txRate": to_int(c.get("tx-rate")),
            "rxBps": to_int(c.get("rx-bits-per-second")) / 8.0,
            "txBps": to_int(c.get("tx-bits-per-second")) / 8.0,
            "rxBytes": to_int(b[0] if b else 0),
            "txBytes": to_int(b[1] if len(b) > 1 else 0),
            "auth": clip(c.get("auth-type", ""), 24),
            "host": clip(lease.get("host-name", ""), 64),
            "ip": clip(lease.get("active-address") or lease.get("address") or arp_by_mac.get(mac, ""), 64),
        }
        clients.append(client)
        if client["iface"] in by_name:
            by_name[client["iface"]]["clients"] += 1
    clients.sort(key=lambda c: (c["ssid"], -c["signal"]))
    # A radio that reports no channel (5 GHz "auto") still has a band: take it from its clients.
    for radio in radios_out:
        if not radio["freq"]:
            for c in clients:
                if c["iface"] == radio["name"] and c["band"]:
                    radio["band"] = c["band"]
                    break
    return {"radios": radios_out, "clients": clients}


def collect_clients(r):
    leases = r.get("ip/dhcp-server/lease", [])
    if not isinstance(leases, list):
        leases = []
    arp = r.get("ip/arp", [])
    if not isinstance(arp, list):
        arp = []
    arp_by_mac = {}
    for a in arp:
        if isinstance(a, dict) and a.get("mac-address") and to_bool(a.get("complete", "true")):
            arp_by_mac[str(a["mac-address"]).upper()] = str(a.get("address", ""))
    out = []
    by_mac = {}
    for l in leases:
        if not isinstance(l, dict):
            continue
        mac = str(l.get("active-mac-address") or l.get("mac-address") or "").upper()
        entry = {
            "ip": clip(l.get("active-address") or l.get("address") or "", 64),
            "mac": clip(mac, 17),
            "host": clip(l.get("host-name", ""), 64),
            "comment": clip(l.get("comment", ""), 80),
            "status": clip(l.get("status", ""), 16),
            "dynamic": to_bool(l.get("dynamic")),
            "static": not to_bool(l.get("dynamic")),
            "server": clip(l.get("server", ""), 32),
            "expires": duration_s(l.get("expires-after")),
            "lastSeen": duration_s(l.get("last-seen")),
            "blocked": to_bool(l.get("blocked")),
            "wifi": False,
        }
        out.append(entry)
        if mac:
            by_mac[mac] = l
    return out, by_mac, arp_by_mac


def collect_zerotier(r):
    instances = r.get("zerotier", [])
    if not isinstance(instances, list) or not instances:
        return None
    ifaces = r.get("zerotier/interface", [])
    peers = r.get("zerotier/peer", [])
    out = {"instances": [], "networks": [], "peers": [], "planets": 0, "leaves": 0}
    for z in instances:
        if isinstance(z, dict):
            ident = str(z.get("identity.public", ""))
            out["instances"].append({"name": clip(z.get("name", ""), 32), "online": to_bool(z.get("online")), "state": clip(z.get("state", ""), 24),
                                     "address": clip(ident.split(":")[0], 16), "port": to_int(z.get("port")), "disabled": to_bool(z.get("disabled"))})
    if isinstance(ifaces, list):
        for n in ifaces:
            if isinstance(n, dict):
                out["networks"].append({"iface": clip(n.get("name", ""), 64), "network": clip(n.get("network", ""), 20), "name": clip(n.get("network-name", ""), 64),
                                        "status": clip(n.get("status", ""), 24), "type": clip(n.get("type", ""), 16), "running": to_bool(n.get("running")),
                                        "instance": clip(n.get("instance", ""), 32), "allowDefault": to_bool(n.get("allow-default")), "allowManaged": to_bool(n.get("allow-managed"))})
    if isinstance(peers, list):
        for p in peers:
            if not isinstance(p, dict):
                continue
            role = str(p.get("role", ""))
            path = str(p.get("path", ""))
            addr = ""
            preferred = ""
            paths = 0
            for chunk in path.split(","):
                if "/" in chunk and re.match(r"^[0-9a-fA-F.:\[\]]+/\d+$", chunk):
                    paths += 1
                    if not addr:
                        addr = chunk
            m = re.search(r"preferred,([0-9a-fA-F.:\[\]]+/\d+)", path)
            if m:
                preferred = m.group(1)
            lat = duration_s(p.get("latency"))
            entry = {"address": clip(p.get("zt-address", ""), 16), "role": clip(role, 8), "latencyMs": int(lat * 1000) if lat is not None else None,
                     "endpoint": clip(preferred or addr, 64), "paths": paths, "bonded": to_bool(p.get("bonded"))}
            if role == "PLANET":
                out["planets"] += 1
            elif role == "LEAF":
                out["leaves"] += 1
            out["peers"].append(entry)
    out["peers"].sort(key=lambda p: (p["role"] != "LEAF", p["latencyMs"] if p["latencyMs"] is not None else 99999))
    return out


def collect_wireguard(r):
    ifaces = r.get("interface/wireguard", [])
    if not isinstance(ifaces, list) or not ifaces:
        return None
    peers = r.get("interface/wireguard/peers", [])
    out = {"interfaces": [], "peers": []}
    for w in ifaces:
        if isinstance(w, dict):
            out["interfaces"].append({"name": clip(w.get("name", ""), 64), "running": to_bool(w.get("running")), "disabled": to_bool(w.get("disabled")),
                                      "port": to_int(w.get("listen-port")), "mtu": to_int(w.get("mtu")), "comment": clip(w.get("comment", ""), 80)})
    if isinstance(peers, list):
        for p in peers:
            if not isinstance(p, dict):
                continue
            hs = duration_s(p.get("last-handshake"))
            out["peers"].append({"iface": clip(p.get("interface", ""), 64), "name": clip(p.get("name") or p.get("comment") or "", 64),
                                 "endpoint": clip((str(p.get("current-endpoint-address", "")) + (":" + str(p.get("current-endpoint-port")) if p.get("current-endpoint-port") else "")), 64),
                                 "allowed": clip(p.get("allowed-address", ""), 120), "lastHandshake": hs, "rx": to_int(p.get("rx")), "tx": to_int(p.get("tx")),
                                 "disabled": to_bool(p.get("disabled")), "responder": to_bool(p.get("responder")), "isBth": "back-to-home" in str(p.get("comment", "")).lower() or str(p.get("name", "")).lower().startswith("bth")})
    return out


def collect_sessions(r, own_user):
    rows = r.get("user/active", [])
    out = []
    if isinstance(rows, list):
        for s in rows:
            if not isinstance(s, dict):
                continue
            if str(s.get("name")) == own_user and str(s.get("via")) in ("api", "rest-api"):
                continue   # that is us
            out.append({"user": clip(s.get("name", ""), 32), "group": clip(s.get("group", ""), 32), "via": clip(s.get("via", ""), 16),
                        "address": clip(s.get("address", ""), 64), "when": clip(s.get("when", ""), 24)})
    # collapse duplicates (WinBox opens several sessions)
    seen = {}
    for s in out:
        key = (s["user"], s["via"], s["address"])
        if key in seen:
            seen[key]["count"] += 1
        else:
            s["count"] = 1
            seen[key] = s
    return list(seen.values())


def collect_log(r):
    """Last LOG_LINES router log lines that are not our own REST logins."""
    rows = r.print_("log", {".proplist": ["time", "topics", "message"], ".query": ["topics=system,info,account", "#!"]}, [], max_bytes=LOG_RESP_MAX)
    if not isinstance(rows, list):
        return []
    out = []
    for e in rows[-LOG_LINES:]:
        if isinstance(e, dict):
            out.append({"time": clip(e.get("time", ""), 24), "topics": clip(e.get("topics", ""), 48), "message": clip(e.get("message", ""), 200),
                        "level": "error" if "error" in str(e.get("topics", "")) or "critical" in str(e.get("topics", "")) else ("warning" if "warning" in str(e.get("topics", "")) else "info")})
    return out


# --------------------------------------------------------------------------- traffic history (SQLite)
#
# Two tables in one private database file:
#   samples(ts, iface, rx, tx)         raw cumulative counters every 30 s, kept 48 h
#   agg(bucket, iface, rx, tx, rxpeak, txpeak, n)   bytes moved per 5-minute bucket, kept historyDays
# The panel asks for one range (--history SECONDS): up to 24 h it gets the raw
# samples, beyond that it gets the buckets re-rolled to 5 or 30 minutes and
# turned back into cumulative counters, so the chart code has one input shape.

def history_dir():
    """The user may point historyDir somewhere else (a synced folder, a bigger
    disk). Accepted only if it is an absolute path to an existing directory
    that is ours, private (0700) and not a symlink; otherwise the cache dir."""
    want = OPTS.get("history_dir") or ""
    if not want:
        ensure_cache_dir()
        return CACHE_DIR
    if not os.path.isabs(want) or len(want) > 512 or any(ord(c) < 32 for c in want):
        warn("historyDir must be an absolute path; using the cache directory")
        ensure_cache_dir()
        return CACHE_DIR
    try:
        st = os.lstat(want)
    except OSError:
        warn("historyDir %s does not exist; using the cache directory" % clip(want, 80))
        ensure_cache_dir()
        return CACHE_DIR
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
        warn("historyDir %s must be a directory you own with mode 0700, no symlink; using the cache directory" % clip(want, 80))
        ensure_cache_dir()
        return CACHE_DIR
    return want


def open_private_dir(path):
    """Descriptor for a directory that is ours, private (0700) and not a symlink.
    The checks run on the descriptor, so they describe the directory that was
    actually opened, not whatever the name points at a moment later."""
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        st = os.fstat(fd)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise OSError("%s must be a directory you own with mode 0700" % path)
    except OSError:
        os.close(fd)
        raise
    return fd


def descriptor_identities():
    """{fd: (device, inode)} of every descriptor this process holds. The listing
    itself briefly holds one descriptor; it is closed again by the time the
    entries are examined, so it drops out instead of shadowing a real one."""
    out = {}
    for name in os.listdir("/proc/self/fd"):
        try:
            st = os.stat("/proc/self/fd/" + name)
        except OSError:
            continue
        out[name] = (st.st_dev, st.st_ino, stat.S_ISREG(st.st_mode))
    return out


def drop_foreign_side_file(dfd, name):
    """SQLite side files (-journal, -wal, -shm) next to the database. Anything
    that is not a plain file of ours is removed without following it; an empty
    journal left behind by the pre-0.2.1 TRUNCATE mode is removed too. A
    non-empty regular journal is a real hot journal and stays for SQLite."""
    try:
        st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
    except FileNotFoundError:
        return
    plain = stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid() and st.st_nlink == 1
    if not plain or (name.endswith("-journal") and st.st_size == 0):
        os.unlink(name, dir_fd=dfd)


def open_history():
    """Open history.sqlite so that every write lands in the file that was
    verified, even if another process of this user swaps the name meanwhile.

    1. The directory is opened with O_NOFOLLOW and checked on its descriptor.
    2. The database file is created/opened relative to that descriptor with
       O_NOFOLLOW and checked on its own descriptor: regular, ours, one link.
    3. sqlite3.connect() opens the file immediately and runs no statement. The
       descriptor it obtained is compared with the verified one by device and
       inode before the first statement. A swap between step 2 and the open
       (symlink, another file renamed into place) shows up as a different
       inode; the connection is closed without a single write.
    4. The rollback journal lives in memory and temp storage too, so SQLite
       never opens a second pathname: no -journal/-wal/-shm file is created,
       inside or outside the directory. The history is expendable, a crash
       mid-commit costs at most a rebuild (see the malformed-database path).
    """
    directory = history_dir()
    dfd = open_private_dir(directory)
    try:
        ffd = os.open(HISTORY_DB, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, 0o600, dir_fd=dfd)
        try:
            st = os.fstat(ffd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_nlink != 1:
                raise OSError("%s is not a private regular file owned by this user" % HISTORY_DB)
            os.fchmod(ffd, 0o600)
            for suffix in ("-journal", "-wal", "-shm"):
                drop_foreign_side_file(dfd, HISTORY_DB + suffix)
            before = descriptor_identities()
            con = sqlite3.connect("file:%s?mode=rw" % urllib.parse.quote(os.path.join(directory, HISTORY_DB)),
                                  uri=True, timeout=2.0, isolation_level=None)
            try:
                opened = [ident for fd, ident in descriptor_identities().items() if before.get(fd) != ident and ident[2]]
                if not opened or any(ident[:2] != (st.st_dev, st.st_ino) for ident in opened):
                    raise OSError("%s was replaced while it was being opened; nothing was written" % HISTORY_DB)
                con.execute("PRAGMA journal_mode=MEMORY")
                con.execute("PRAGMA temp_store=MEMORY")
                con.execute("PRAGMA synchronous=NORMAL")
                con.execute("CREATE TABLE IF NOT EXISTS samples (ts REAL NOT NULL, iface TEXT NOT NULL, rx INTEGER NOT NULL, tx INTEGER NOT NULL, PRIMARY KEY (iface, ts))")
                con.execute("CREATE INDEX IF NOT EXISTS samples_ts ON samples (ts)")
                con.execute("CREATE TABLE IF NOT EXISTS agg (bucket INTEGER NOT NULL, iface TEXT NOT NULL, rx INTEGER NOT NULL, tx INTEGER NOT NULL, "
                            "rxpeak REAL NOT NULL, txpeak REAL NOT NULL, n INTEGER NOT NULL, PRIMARY KEY (iface, bucket))")
                con.execute("CREATE INDEX IF NOT EXISTS agg_bucket ON agg (bucket)")
            except sqlite3.DatabaseError as e:
                # "file is not a database" / "malformed": set the verified file aside
                # (by descriptor-relative rename, never by pathname) and start over next tick.
                con.close()
                if "malformed" in str(e) or "not a database" in str(e):
                    os.rename(HISTORY_DB, HISTORY_DB + ".broken", src_dir_fd=dfd, dst_dir_fd=dfd)
                raise
            except BaseException:
                con.close()
                raise
            return con
        finally:
            os.close(ffd)
    finally:
        os.close(dfd)


def import_jsonl(con, now):
    """One-time import of the pre-0.2 history.jsonl, then the file is removed."""
    try:
        raw = read_private_file(HISTORY_FILE, CACHE_MAX)
    except OSError:
        return
    rows = []
    for line in raw.decode("utf-8", "replace").splitlines():
        try:
            entry = json.loads(line)
            ts = float(entry[0])
            for name, c in entry[1].items():
                rows.append((ts, str(name)[:64], int(c[0]), int(c[1])))
        except (ValueError, TypeError, IndexError, AttributeError):
            continue
    con.execute("BEGIN")
    con.executemany("INSERT OR IGNORE INTO samples (ts, iface, rx, tx) VALUES (?, ?, ?, ?)", rows)
    # Rebuild the aggregates from the imported samples.
    prev = {}
    for ts, name, rx, tx in sorted(rows):
        p = prev.get(name)
        if p and 0 < ts - p[0] < 900 and rx >= p[1] and tx >= p[2]:
            add_agg(con, name, ts, rx - p[1], tx - p[2], ts - p[0])
        prev[name] = (ts, rx, tx)
    con.execute("COMMIT")
    try:
        os.unlink(HISTORY_FILE)
    except OSError:
        pass


def add_agg(con, name, ts, drx, dtx, dt):
    bucket = int(ts // AGG_STEP) * AGG_STEP
    con.execute("INSERT INTO agg (bucket, iface, rx, tx, rxpeak, txpeak, n) VALUES (?, ?, ?, ?, ?, ?, 1) "
                "ON CONFLICT(iface, bucket) DO UPDATE SET rx = rx + excluded.rx, tx = tx + excluded.tx, "
                "rxpeak = MAX(rxpeak, excluded.rxpeak), txpeak = MAX(txpeak, excluded.txpeak), n = n + 1",
                (bucket, name, int(drx), int(dtx), drx / dt, dtx / dt))


def record_history(interfaces, now):
    """Store one sample per HISTORY_STEP, roll it into the 5-minute aggregates,
    prune, and return the history for the requested range (or None)."""
    con = open_history()
    try:
        import_jsonl(con, now)
        # Bridge slaves are folded into the bridge row, except Wi-Fi radios,
        # which get their own row and chart.
        counters = {i["name"]: (i["rx"], i["tx"]) for i in interfaces if i.get("running") and (not i.get("slave") or i.get("kind") == "wifi")}
        last = {}
        for name, ts, rx, tx in con.execute("SELECT s.iface, s.ts, s.rx, s.tx FROM samples s JOIN (SELECT iface, MAX(ts) AS ts FROM samples GROUP BY iface) m ON m.iface = s.iface AND m.ts = s.ts"):
            last[name] = (ts, rx, tx)
        newest = max([v[0] for v in last.values()] or [0.0])
        if now - newest >= HISTORY_STEP - 1:
            con.execute("BEGIN")
            for name, (rx, tx) in counters.items():
                con.execute("INSERT OR REPLACE INTO samples (ts, iface, rx, tx) VALUES (?, ?, ?, ?)", (round(now, 1), name, int(rx), int(tx)))
                p = last.get(name)
                if p and 0 < now - p[0] < 900 and rx >= p[1] and tx >= p[2]:
                    add_agg(con, name, now, rx - p[1], tx - p[2], now - p[0])
            days = max(1, min(HISTORY_DAYS_MAX, int(OPTS["history_days"])))
            con.execute("DELETE FROM samples WHERE ts < ?", (now - RAW_KEEP,))
            con.execute("DELETE FROM agg WHERE bucket < ?", (now - days * 86400,))
            con.execute("COMMIT")
        rng = int(OPTS["history"])
        if rng <= 0:
            return None, HISTORY_STEP
        rng = min(rng, RANGE_MAX)
        if rng <= 86400:
            out = {}
            for ts, name, rx, tx in con.execute("SELECT ts, iface, rx, tx FROM samples WHERE ts >= ? ORDER BY ts", (now - rng,)):
                out.setdefault(ts, {})[name] = [rx, tx]
            return [[ts, row] for ts, row in sorted(out.items())], HISTORY_STEP
        step = AGG_STEP if rng <= 7 * 86400 else 1800
        cum = {}
        out = {}
        for bucket, name, rx, tx in con.execute("SELECT (bucket / ?) * ?, iface, SUM(rx), SUM(tx) FROM agg WHERE bucket >= ? GROUP BY 1, 2 ORDER BY 1", (step, step, now - rng)):
            c = cum.setdefault(name, [0, 0])
            c[0] += rx
            c[1] += tx
            # Counter at the END of the bucket; the chart's delta over one step is this bucket's bytes.
            out.setdefault(float(bucket + step), {})[name] = [c[0], c[1]]
        return [[ts, row] for ts, row in sorted(out.items())], step
    finally:
        con.close()


def update_state(now, wan, public_ip, clients, wifi_clients, sessions, router):
    """Compare with the previous run; differences become alerts (kept 24 h).
    First run records and says nothing."""
    state = load_json_file(STATE_FILE, {})
    changed = False
    new_alerts = []

    def alert(kind, title, body, urgent=False, **extra):
        a = {"id": "%s:%d" % (kind, int(now * 1000)), "kind": kind, "at": round(now, 1), "urgent": bool(urgent), "title": clip(title, 120), "body": clip(body, 300)}
        a.update(extra)
        new_alerts.append(a)

    # --- WAN up/down log
    log = [e for e in state.get("wan", []) if isinstance(e, dict) and now - float(e.get("at", 0) or 0) < WAN_LOG_KEEP][-WAN_LOG_MAX:]
    key = "%s|%s|%s" % (wan.get("iface", ""), "up" if wan.get("up") else "down", wan.get("ip", ""))
    last = log[-1] if log else None
    last_key = "%s|%s|%s" % (last.get("iface", ""), last.get("state", ""), last.get("ip", "")) if last else None
    if last_key != key:
        log.append({"at": round(now, 1), "iface": wan.get("iface", ""), "state": "up" if wan.get("up") else "down", "ip": wan.get("ip", ""), "kind": wan.get("kind", "")})
        changed = True
        if last is not None:
            if not wan.get("up"):
                alert("wan", "Internet link is down", (wan.get("iface") or "WAN") + ": " + (wan.get("detail") or "no active default route"), urgent=True)
            elif last.get("state") == "down":
                alert("wan", "Internet link is back", (wan.get("iface") or "WAN") + (" · " + wan["ip"] if wan.get("ip") else ""))
            elif last.get("ip") != wan.get("ip") and wan.get("ip"):
                alert("wan", "WAN address changed", "%s → %s" % (last.get("ip") or "?", wan["ip"]))
    if log != state.get("wan"):
        state["wan"] = log
        changed = True

    # --- public address (cloud)
    if public_ip:
        prev = state.get("public", "")
        if prev and prev != public_ip:
            alert("public", "Public IP changed", "%s → %s" % (prev, public_ip))
        if prev != public_ip:
            state["public"] = public_ip
            changed = True

    # --- new clients (by MAC)
    known = state.get("clients") if isinstance(state.get("clients"), dict) else None
    first_run = known is None
    known = dict(known or {})
    first_seen = {}
    fresh = []
    seen_now = set()
    for c in clients + wifi_clients:
        mac = c.get("mac", "")
        if not mac or mac in seen_now:
            continue
        seen_now.add(mac)
        old = known.get(mac)
        if isinstance(old, list) and len(old) == 2 and now - float(old[1] or 0) < CLIENT_FORGET:
            known[mac] = [old[0], round(now, 1)]
        else:
            known[mac] = [0 if first_run else round(now, 1), round(now, 1)]
            if not first_run:
                fresh.append(c)
            changed = True
        first_seen[mac] = known[mac][0]
    for mac in list(known.keys()):
        if mac not in seen_now and now - float((known[mac] or [0, 0])[1] or 0) > CLIENT_FORGET:
            del known[mac]
            changed = True
    if len(known) > 2000:
        for mac in sorted(known, key=lambda k: known[k][1])[: len(known) - 2000]:
            del known[mac]
        changed = True
    if known != state.get("clients"):
        state["clients"] = known
        changed = True
    if fresh:
        names = []
        for c in fresh[:5]:
            label = c.get("host") or c.get("mac")
            if c.get("ip"):
                label += " (" + c["ip"] + ")"
            if c.get("ssid"):
                label += " on " + c["ssid"]
            names.append(label)
        alert("client", "New device on the network" if len(fresh) == 1 else "%d new devices on the network" % len(fresh),
              ", ".join(names) + (", …" if len(fresh) > 5 else ""), macs=[c.get("mac") for c in fresh[:20]])

    # --- logins: a session from an address not seen before
    sess_known = state.get("sessions") if isinstance(state.get("sessions"), dict) else {}
    sess_known = dict(sess_known)
    sess_first = sess_known is not None and bool(state.get("sessionsInit"))
    for s in sessions:
        key = "%s@%s/%s" % (s.get("user"), s.get("address"), s.get("via"))
        if key not in sess_known:
            sess_known[key] = round(now, 1)
            changed = True
            if sess_first:
                alert("login", "Router login from a new address", "%s via %s from %s" % (s.get("user"), s.get("via"), s.get("address") or "?"), urgent=str(s.get("group")) == "full")
    if len(sess_known) > 500:
        for k in sorted(sess_known, key=lambda k: sess_known[k])[: len(sess_known) - 500]:
            del sess_known[k]
    if sess_known != state.get("sessions") or not state.get("sessionsInit"):
        state["sessions"] = sess_known
        state["sessionsInit"] = True
        changed = True

    # --- firmware / version changes
    ver_key = "%s|%s" % (router.get("version", ""), router.get("firmware", ""))
    if state.get("version") and state.get("version") != ver_key:
        alert("router", "RouterOS updated", "now %s, firmware %s" % (router.get("version"), router.get("firmware")))
    if state.get("version") != ver_key:
        state["version"] = ver_key
        changed = True

    # --- reboot detection
    up = router.get("uptimeS")
    if isinstance(up, (int, float)) and isinstance(state.get("uptime"), (int, float)):
        if up + 120 < state["uptime"]:
            alert("router", "Router rebooted", "uptime is now " + fmt_duration(up), urgent=True)
    if isinstance(up, (int, float)):
        state["uptime"] = up
        changed = True

    alerts = [a for a in state.get("alerts", []) if isinstance(a, dict) and now - float(a.get("at", 0) or 0) < ALERT_KEEP]
    if new_alerts or len(alerts) != len(state.get("alerts", [])):
        alerts = (alerts + new_alerts)[-ALERT_MAX:]
        state["alerts"] = alerts
        changed = True
    if changed:
        save_json_file(STATE_FILE, state)
    return log[-30:], alerts, first_seen


# --------------------------------------------------------------------------- demo

def demo_output(now):
    interfaces = [
        {"name": "ether1", "type": "ether", "kind": "ethernet", "running": True, "disabled": False, "slave": False, "comment": "ISP", "mac": "02:1A:2B:00:00:01", "mtu": 1500,
         "rx": 562_790_417_378, "tx": 478_542_241_882, "rxPackets": 781_587_074, "txPackets": 759_252_966, "rxErrors": 0, "txErrors": 0, "rxDrops": 0, "txDrops": 0, "linkDowns": 0, "lastUp": "2026-05-20 09:36:28", "lastDown": "", "ips": []},
        {"name": "pppoe-out1", "type": "pppoe-out", "kind": "wan", "running": True, "disabled": False, "slave": False, "comment": "", "mac": "", "mtu": 1492,
         "rx": 540_791_727_430, "tx": 457_867_764_384, "rxPackets": 740_669_612, "txPackets": 756_067_671, "rxErrors": 0, "txErrors": 0, "rxDrops": 0, "txDrops": 0, "linkDowns": 0, "lastUp": "2026-05-20 09:36:29", "lastDown": "", "ips": ["203.0.113.42/32"]},
        {"name": "bridge", "type": "bridge", "kind": "bridge", "running": True, "disabled": False, "slave": False, "comment": "defconf", "mac": "02:1A:2B:00:00:02", "mtu": 1500,
         "rx": 454_384_628_074, "tx": 550_568_626_761, "rxPackets": 737_306_076, "txPackets": 734_572_793, "rxErrors": 0, "txErrors": 0, "rxDrops": 0, "txDrops": 0, "linkDowns": 0, "lastUp": "2026-05-20 09:36:22", "lastDown": "", "ips": ["192.168.88.1/24"]},
        {"name": "ether3", "type": "ether", "kind": "ethernet", "running": True, "disabled": False, "slave": True, "comment": "", "mac": "02:1A:2B:00:00:03", "mtu": 1500,
         "rx": 515_531_133, "tx": 1_784_841_753, "rxPackets": 4_285_820, "txPackets": 14_517_348, "rxErrors": 0, "txErrors": 0, "rxDrops": 0, "txDrops": 0, "linkDowns": 0, "lastUp": "2026-05-20 09:36:24", "lastDown": "", "ips": []},
        {"name": "wifi1", "type": "wifi", "kind": "wifi", "running": True, "disabled": False, "slave": True, "comment": "", "mac": "02:1A:2B:00:00:0C", "mtu": 1500,
         "rx": 2_002_016_880, "tx": 7_097_436_515, "rxPackets": 5_164_936, "txPackets": 7_229_560, "rxErrors": 0, "txErrors": 0, "rxDrops": 0, "txDrops": 0, "linkDowns": 37, "lastUp": "2026-09-14 09:51:25", "lastDown": "2026-09-11 13:37:55", "ips": []},
        {"name": "wifi2", "type": "wifi", "kind": "wifi", "running": True, "disabled": False, "slave": True, "comment": "", "mac": "02:1A:2B:00:00:0D", "mtu": 1500,
         "rx": 421_165_239_754, "tx": 503_621_816_298, "rxPackets": 670_383_033, "txPackets": 675_550_139, "rxErrors": 0, "txErrors": 0, "rxDrops": 0, "txDrops": 0, "linkDowns": 48, "lastUp": "2026-09-14 09:50:29", "lastDown": "2026-09-11 13:42:29", "ips": []},
        {"name": "zerotier1", "type": "zerotier", "kind": "zerotier", "running": True, "disabled": False, "slave": False, "comment": "", "mac": "02:3C:4D:00:00:4F", "mtu": 2800,
         "rx": 429_575_907, "tx": 2_736_014_553, "rxPackets": 3_112_169, "txPackets": 2_318_833, "rxErrors": 0, "txErrors": 0, "rxDrops": 0, "txDrops": 0, "linkDowns": 0, "lastUp": "2026-05-20 09:36:40", "lastDown": "", "ips": ["10.144.63.70/16"]},
        {"name": "wg-bth", "type": "wg", "kind": "wireguard", "running": True, "disabled": False, "slave": False, "comment": "Back To Home", "mac": "", "mtu": 1420,
         "rx": 92_575_907, "tx": 236_014_553, "rxPackets": 312_169, "txPackets": 218_833, "rxErrors": 0, "txErrors": 0, "rxDrops": 0, "txDrops": 0, "linkDowns": 0, "lastUp": "2026-09-01 08:00:00", "lastDown": "", "ips": ["192.168.216.1/24"]},
    ]
    wan = {"present": True, "up": True, "iface": "pppoe-out1", "kind": "pppoe", "gateway": "pppoe-out1", "ip": "203.0.113.42/32", "sinceUp": "2026-05-20 09:36:29", "detail": "", "pppoeUser": "pppoe-user", "pppoeOn": "ether1", "acName": "bras-01.example", "linkDowns": 0}
    clients = [
        {"ip": "192.168.88.194", "mac": "02:11:22:33:44:01", "host": "pihole", "comment": "", "status": "bound", "dynamic": False, "static": True, "server": "defconf", "expires": 1094, "lastSeen": 706, "blocked": False, "wifi": False, "firstSeen": 0},
        {"ip": "192.168.88.193", "mac": "02:11:22:33:44:02", "host": "homeassistant", "comment": "", "status": "bound", "dynamic": False, "static": True, "server": "defconf", "expires": 1000, "lastSeen": 60, "blocked": False, "wifi": False, "firstSeen": 0},
        {"ip": "192.168.88.180", "mac": "02:11:22:33:44:03", "host": "proxmox-vm1", "comment": "proxmox VM", "status": "bound", "dynamic": False, "static": True, "server": "defconf", "expires": 900, "lastSeen": 12, "blocked": False, "wifi": False, "firstSeen": 0},
        {"ip": "192.168.88.151", "mac": "02:11:22:33:44:04", "host": "laptop", "comment": "", "status": "bound", "dynamic": True, "static": False, "server": "defconf", "expires": 1500, "lastSeen": 3, "blocked": False, "wifi": True, "firstSeen": 0},
        {"ip": "192.168.88.168", "mac": "02:11:22:33:44:05", "host": "phone-anna", "comment": "", "status": "bound", "dynamic": True, "static": False, "server": "defconf", "expires": 1700, "lastSeen": 15, "blocked": False, "wifi": True, "firstSeen": 0},
        {"ip": "192.168.88.156", "mac": "02:11:22:33:44:06", "host": "phone-tomek", "comment": "", "status": "bound", "dynamic": True, "static": False, "server": "defconf", "expires": 1600, "lastSeen": 3, "blocked": False, "wifi": True, "firstSeen": 0},
        {"ip": "192.168.88.171", "mac": "02:11:22:33:44:07", "host": "new-tablet", "comment": "", "status": "bound", "dynamic": True, "static": False, "server": "defconf", "expires": 1790, "lastSeen": 1, "blocked": False, "wifi": True, "firstSeen": now - 420},
    ]
    wifi_clients = [
        {"mac": "02:11:22:33:44:04", "iface": "wifi2", "ssid": "Casa-5G", "band": "5ghz-ac", "signal": -40, "uptime": 10378, "lastActivity": 0, "rxRate": 866000000, "txRate": 866000000, "rxBps": 2746, "txBps": 18408, "rxBytes": 857205992, "txBytes": 674992607, "auth": "wpa2-psk", "host": "laptop", "ip": "192.168.88.151"},
        {"mac": "02:11:22:33:44:05", "iface": "wifi2", "ssid": "Casa-5G", "band": "5ghz-ac", "signal": -51, "uptime": 11453, "lastActivity": 15, "rxRate": 866000000, "txRate": 780000000, "rxBps": 120, "txBps": 400, "rxBytes": 12345678, "txBytes": 87654321, "auth": "wpa2-psk", "host": "phone-anna", "ip": "192.168.88.168"},
        {"mac": "02:11:22:33:44:06", "iface": "wifi1", "ssid": "Casa-2G", "band": "2ghz-n", "signal": -71, "uptime": 11397, "lastActivity": 3, "rxRate": 72200000, "txRate": 65000000, "rxBps": 0, "txBps": 0, "rxBytes": 2345678, "txBytes": 7654321, "auth": "wpa2-psk", "host": "phone-tomek", "ip": "192.168.88.156"},
        {"mac": "02:11:22:33:44:07", "iface": "wifi1", "ssid": "Casa-2G", "band": "2ghz-n", "signal": -78, "uptime": 420, "lastActivity": 1, "rxRate": 65000000, "txRate": 58500000, "rxBps": 900, "txBps": 300, "rxBytes": 45678, "txBytes": 54321, "auth": "wpa2-psk", "host": "new-tablet", "ip": "192.168.88.171"},
    ]
    return {
        "ts": now, "tookMs": 63, "demo": True, "version": VERSION,
        "router": {"identity": "casa-router", "board": "hAP ac^3", "model": "RBD53iG-5HacD2HnD", "version": "7.21.3 (stable)", "channel": "stable", "firmware": "6.47.10", "firmwareUpgrade": "7.21.3",
                   "firmwarePending": True, "uptimeS": 10_150_000, "cpu": "ARM", "cpuCount": 4, "cpuFreq": 896, "cpuLoad": 4, "memFree": 130_076_672, "memTotal": 268_435_456,
                   "hddFree": 92_352_512, "hddTotal": 134_217_728, "badBlocks": 0, "serial": "ABCD01234567", "arch": "arm", "time": "13:32:11", "date": "2026-09-14", "tz": "Europe/Warsaw"},
        "wan": wan, "cloud": {"publicIp": "203.0.113.42", "bth": "enabled", "ddns": "auto", "ddnsName": "abcdef012345.sn.mynetname.net"},
        "dns": ["94.140.14.14", "192.168.88.194"],
        "interfaces": interfaces,
        "wifi": {"radios": [
            {"name": "wifi1", "ssid": "Casa-2G", "mode": "ap", "freq": 2412, "width": "20mhz", "master": True, "masterOf": "", "running": True, "disabled": False, "clients": 2},
            {"name": "wifi2", "ssid": "Casa-5G", "mode": "ap", "freq": 5180, "width": "20/40/80mhz", "master": True, "masterOf": "", "running": True, "disabled": False, "clients": 2},
            {"name": "wifi3", "ssid": "Casa-IoT", "mode": "ap", "freq": 0, "width": "", "master": False, "masterOf": "wifi1", "running": False, "disabled": False, "clients": 0},
        ], "clients": wifi_clients},
        "clients": clients, "clientsBound": 7,
        "zerotier": {"instances": [{"name": "zt1", "online": True, "state": "running", "address": "9f3a1c2b7d", "port": 9993, "disabled": False}],
                     "networks": [{"iface": "zerotier1", "network": "6ab565387a1cbe3b", "name": "home-net", "status": "OK", "type": "PRIVATE", "running": True, "instance": "zt1", "allowDefault": False, "allowManaged": True}],
                     "peers": [{"address": "1a2b3c4d5e", "role": "LEAF", "latencyMs": 5, "endpoint": "198.51.100.7/52268", "paths": 2, "bonded": False},
                               {"address": "5e4d3c2b1a", "role": "LEAF", "latencyMs": 9, "endpoint": "203.0.113.9/9164", "paths": 2, "bonded": False},
                               {"address": "778cde7190", "role": "PLANET", "latencyMs": 26, "endpoint": "84.17.53.155/9993", "paths": 1, "bonded": False}],
                     "planets": 4, "leaves": 2},
        "wireguard": {"interfaces": [{"name": "wg-bth", "running": True, "disabled": False, "port": 13231, "mtu": 1420, "comment": "Back To Home"}],
                      "peers": [{"iface": "wg-bth", "name": "phone", "endpoint": "198.51.100.23:41641", "allowed": "192.168.216.2/32", "lastHandshake": 42, "rx": 12_345_678, "tx": 98_765_432, "disabled": False, "responder": True, "isBth": True},
                                {"iface": "wg-bth", "name": "laptop-away", "endpoint": "", "allowed": "192.168.216.3/32", "lastHandshake": 86_400 * 3, "rx": 0, "tx": 0, "disabled": False, "responder": True, "isBth": True}]},
        "sessions": [{"user": "admin", "group": "full", "via": "winbox", "address": "192.168.88.150", "when": "2026-09-14 13:19:48", "count": 2}],
        "firewall": {"filter": 14, "nat": 3, "connections": 434},
        "log": [{"time": "2026-09-14 13:05:43", "topics": "dhcp,info", "message": "defconf assigned 192.168.88.171 for 02:11:22:33:44:07 new-tablet", "level": "info"},
                {"time": "2026-09-14 13:07:51", "topics": "wireless,info", "message": "02:11:22:33:44:07@wifi1(Casa-2G) connected, signal strength -78", "level": "info"},
                {"time": "2026-09-14 12:50:02", "topics": "system,error,critical", "message": "login failure for user admin from 203.0.113.99 via winbox", "level": "error"}],
        "alerts": [{"id": "client:1", "kind": "client", "at": now - 420, "urgent": False, "title": "New device on the network", "body": "new-tablet (192.168.88.171) on Casa-2G", "macs": ["02:11:22:33:44:07"]},
                   {"id": "wan:1", "kind": "wan", "at": now - 7200, "urgent": True, "title": "Internet link is down", "body": "pppoe-out1: PPPoE session down"},
                   {"id": "wan:2", "kind": "wan", "at": now - 7140, "urgent": False, "title": "Internet link is back", "body": "pppoe-out1 · 203.0.113.42/32"}],
        "wanLog": [{"at": now - 86400 * 3, "iface": "pppoe-out1", "state": "up", "ip": "203.0.113.41/32", "kind": "pppoe"}, {"at": now - 7200, "iface": "pppoe-out1", "state": "down", "ip": "", "kind": "pppoe"}, {"at": now - 7140, "iface": "pppoe-out1", "state": "up", "ip": "203.0.113.42/32", "kind": "pppoe"}],
        "warnings": [], "setup": None, "history": None, "historyStep": HISTORY_STEP, "stats": {"requests": 0, "bytes": 0},
    }


# --------------------------------------------------------------------------- main

def setup_document(reason):
    """No usable credentials: the panel shows how to create them. Nothing here
    runs anything; the RouterOS commands are for the user to paste."""
    return {
        "ts": time.time(), "tookMs": int((time.time() - STARTED) * 1000), "version": VERSION,
        "setup": {"reason": clip(reason, 200), "path": CREDENTIALS},
        "router": None, "wan": None, "interfaces": [], "clients": [], "wifi": {"radios": [], "clients": []}, "alerts": [], "warnings": [], "log": [],
    }


def main():
    args = sys.argv[1:]
    if args and args[0] == "--fingerprint":
        sys.exit(cmd_fingerprint())
    i = 0
    while i < len(args):
        a = args[i]
        nxt = args[i + 1] if i + 1 < len(args) else None
        if a == "--log":
            OPTS["log"] = True
        elif a == "--history" and nxt is not None and nxt.isdigit():
            OPTS["history"] = max(0, min(RANGE_MAX, int(nxt)))
            i += 1
        elif a == "--history-days" and nxt is not None and nxt.isdigit():
            OPTS["history_days"] = max(1, min(HISTORY_DAYS_MAX, int(nxt)))
            i += 1
        elif a == "--history-dir" and nxt is not None:
            OPTS["history_dir"] = nxt[:512]
            i += 1
        elif a == "--demo":
            OPTS["demo"] = True
        elif a == "--no-clients":
            OPTS["clients"] = False
        else:
            sys.stderr.write("unknown option %s\n" % clip(a, 40))
            sys.exit(2)
        i += 1
    os.umask(0o077)   # every file this program creates is private
    now = time.time()
    if OPTS["demo"]:
        doc = demo_output(now)
        # Demo counters follow an invented day: a slow wave with bursts, so that
        # rates and charts look alive. Cumulative per interface, per 30 s step.
        import math

        def demo_rate(seed, i, down):
            base = (1_200_000 if down else 350_000) / seed
            wave = 0.25 + 0.75 * (0.5 + 0.5 * math.sin(i / 61.0 + seed))
            burst = 3.0 if (i * 7 + seed * 13) % 97 < 6 else 1.0
            jitter = 0.7 + 0.6 * ((i * 31 + seed * 7) % 11) / 10.0
            return base * wave * burst * jitter

        rng = int(OPTS["history"]) or 3600
        step = 30 if rng <= 86400 else (AGG_STEP if rng <= 7 * 86400 else 1800)
        points = int(rng / step)
        step_now = now / step
        first = int(step_now) - points
        # Accumulate from a day boundary, not from the window start, so the
        # counters only ever grow between two refreshes (rates stay positive).
        per_day = int(86400 / step)
        origin = (int(step_now) // per_day - 1) * per_day
        charted = [f for f in doc["interfaces"] if f["running"] and (not f["slave"] or f["kind"] == "wifi")]
        counters = {f["name"]: [f["rx"] - 2 * 86400 * 1_200_000, f["tx"] - 2 * 86400 * 350_000] for f in charted}
        seeds = {f["name"]: (sum(ord(c) for c in f["name"]) % 5) + 1 for f in charted}
        history = []
        for i in range(min(origin, first), int(step_now) + 1):
            for f in charted:
                n = f["name"]
                counters[n][0] += int(step * demo_rate(seeds[n], int(i * step / 30), True))
                counters[n][1] += int(step * demo_rate(seeds[n], int(i * step / 30), False))
            if first <= i < int(step_now):
                history.append([i * float(step), {n: [c[0], c[1]] for n, c in counters.items()}])
        frac = step_now - int(step_now)
        for f in charted:
            n = f["name"]
            f["rx"] = counters[n][0] + int(frac * step * demo_rate(seeds[n], int(step_now * step / 30), True))
            f["tx"] = counters[n][1] + int(frac * step * demo_rate(seeds[n], int(step_now * step / 30), False))
        if OPTS["history"]:
            doc["history"] = history
            doc["historyStep"] = step
        doc["historyDays"] = OPTS["history_days"]
        json.dump(bound(doc), sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        return

    try:
        creds = load_credentials()
    except CredentialsError as e:
        json.dump(bound(setup_document(str(e))), sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        return

    r = Router(creds)
    try:
        resource = r.get("system/resource", {}, required=True)
    except PermissionError as e:
        json.dump(bound(setup_document(str(e))), sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        return
    except OSError as e:
        msg = str(e)
        if "fingerprint mismatch" in msg:
            json.dump(bound(setup_document("TLS certificate does not match FINGERPRINT: " + msg)), sys.stdout, ensure_ascii=False)
        else:
            json.dump(bound({"ts": now, "tookMs": int((time.time() - STARTED) * 1000), "version": VERSION, "error": clip(msg, 200), "host": creds["host"],
                             "router": None, "wan": None, "interfaces": [], "clients": [], "wifi": {"radios": [], "clients": []}, "alerts": [], "warnings": [], "log": []}), sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        return
    if not isinstance(resource, dict):
        resource = {}

    routerboard = r.get("system/routerboard", {}) or {}
    identity = r.get("system/identity", {}) or {}
    update = r.get("system/package/update", {}) or {}
    clock = r.get("system/clock", {}) or {}
    health = r.get("system/health", []) or []
    uptime_s = duration_s(resource.get("uptime"))
    router = {
        "identity": clip(identity.get("name", ""), 64),
        "board": clip(resource.get("board-name", ""), 64),
        "model": clip(routerboard.get("model", ""), 64),
        "version": clip(resource.get("version", ""), 48),
        "channel": clip(update.get("channel", ""), 24),
        "latestVersion": clip(update.get("latest-version", ""), 24),
        "firmware": clip(routerboard.get("current-firmware", ""), 24),
        "firmwareUpgrade": clip(routerboard.get("upgrade-firmware", ""), 24),
        "firmwarePending": bool(routerboard.get("upgrade-firmware")) and str(routerboard.get("upgrade-firmware")) != str(routerboard.get("current-firmware")),
        "uptimeS": uptime_s,
        "cpu": clip(resource.get("cpu", ""), 24),
        "cpuCount": to_int(resource.get("cpu-count"), 0),
        "cpuFreq": to_int(resource.get("cpu-frequency"), 0),
        "cpuLoad": to_int(resource.get("cpu-load"), 0),
        "memFree": to_int(resource.get("free-memory")),
        "memTotal": to_int(resource.get("total-memory")),
        "hddFree": to_int(resource.get("free-hdd-space")),
        "hddTotal": to_int(resource.get("total-hdd-space")),
        "badBlocks": to_int(str(resource.get("bad-blocks", "0")).rstrip("%"), 0),
        "serial": clip(routerboard.get("serial-number", ""), 32),
        "arch": clip(resource.get("architecture-name", ""), 16),
        "time": clip(clock.get("time", ""), 16),
        "date": clip(clock.get("date", ""), 16),
        "tz": clip(clock.get("time-zone-name", ""), 32),
        "health": [],
    }
    if isinstance(health, list):
        for h in health[:12]:
            if isinstance(h, dict) and h.get("name"):
                router["health"].append({"name": clip(h.get("name"), 32), "value": clip(h.get("value", ""), 16), "type": clip(h.get("type", ""), 8)})

    addresses = r.get("ip/address", []) or []
    if not isinstance(addresses, list):
        addresses = []
    interfaces = collect_interfaces(r, addresses)
    wan = collect_wan(r, interfaces, addresses)
    cloud_raw = r.get("ip/cloud", {}) or {}
    cloud = {"publicIp": clip(cloud_raw.get("public-address", ""), 64), "bth": clip(cloud_raw.get("back-to-home-vpn", ""), 32),
             "ddns": clip(cloud_raw.get("ddns-enabled", ""), 16), "ddnsName": clip(cloud_raw.get("dns-name", ""), 80), "warning": clip(cloud_raw.get("warning", ""), 120)}
    dns_raw = r.get("ip/dns", {}) or {}
    dns = [d.strip() for d in str(dns_raw.get("servers", "")).split(",") if d.strip()]
    dyn = [d.strip() for d in str(dns_raw.get("dynamic-servers", "")).split(",") if d.strip()]
    dns_info = {"servers": dns[:8], "dynamic": dyn[:8], "doh": clip(dns_raw.get("use-doh-server", ""), 120), "remote": to_bool(dns_raw.get("allow-remote-requests"))}

    clients, leases_by_mac, arp_by_mac = ([], {}, {}) if not OPTS["clients"] else collect_clients(r)
    wifi = collect_wifi(r, leases_by_mac, arp_by_mac)
    wifi_macs = {c["mac"] for c in wifi["clients"]}
    for c in clients:
        c["wifi"] = c["mac"] in wifi_macs
    zerotier = collect_zerotier(r)
    wireguard = collect_wireguard(r)
    sessions = collect_sessions(r, creds["user"])
    conn_count = r.print_("ip/firewall/connection", {"count-only": True}, {}) or {}
    filter_rules = r.get("ip/firewall/filter", []) or []
    nat_rules = r.get("ip/firewall/nat", []) or []
    firewall = {"connections": to_int((conn_count or {}).get("ret"), 0) if isinstance(conn_count, dict) else 0,
                "filter": len(filter_rules) if isinstance(filter_rules, list) else 0,
                "nat": len(nat_rules) if isinstance(nat_rules, list) else 0,
                "filterDisabled": sum(1 for f in filter_rules if isinstance(f, dict) and to_bool(f.get("disabled"))) if isinstance(filter_rules, list) else 0}
    log = collect_log(r) if OPTS["log"] else []
    r.close()

    history, history_step = None, HISTORY_STEP
    try:
        history, history_step = record_history(interfaces, now)
    except (OSError, sqlite3.Error) as e:
        warn("history: %s" % clip(str(e), 120))
    wan_log, alerts, first_seen = [], [], {}
    try:
        wan_log, alerts, first_seen = update_state(now, wan, cloud["publicIp"], clients, wifi["clients"], sessions, router)
    except OSError as e:
        warn("state: %s" % e)
    for c in clients:
        c["firstSeen"] = first_seen.get(c["mac"])
    for c in wifi["clients"]:
        c["firstSeen"] = first_seen.get(c["mac"])
    for e in r.errors:
        warn(e)

    output = {
        "ts": now, "tookMs": int((time.time() - STARTED) * 1000), "version": VERSION, "host": creds["host"],
        "router": router, "wan": wan, "cloud": cloud, "dns": dns_info["servers"], "dnsInfo": dns_info,
        "interfaces": interfaces, "wifi": wifi, "clients": clients,
        "clientsBound": sum(1 for c in clients if c["status"] == "bound"),
        "zerotier": zerotier, "wireguard": wireguard, "sessions": sessions, "firewall": firewall,
        "log": log, "alerts": alerts, "wanLog": wan_log, "warnings": WARNINGS, "setup": None,
        "history": history, "historyStep": history_step, "historyDays": OPTS["history_days"], "historyDir": history_dir() if OPTS["history_dir"] else "",
        "stats": {"requests": r.requests, "bytes": r.bytes},
    }
    json.dump(bound(output), sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")


if __name__ == "__main__":
    code = 0
    try:
        main()
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
    except Exception as e:  # never leave the widget without a document
        json.dump({"error": clip("%s: %s" % (type(e).__name__, e)), "ts": time.time(), "router": None, "wan": None, "interfaces": [], "clients": [],
                   "wifi": {"radios": [], "clients": []}, "alerts": [], "warnings": [], "log": []}, sys.stdout)
        sys.stdout.write("\n")
    finally:
        sys.stdout.flush()
        os._exit(code)
