#!/usr/bin/env python3
"""
Nextcloud API wrapper for Hermes Agent (rewrite/fork of adnw-vinc/hermes-nextcloud).

Unified access to Nextcloud via WebDAV (files), the Notes API, CalDAV (events,
tasks) and CardDAV (contacts). Standard library only, no curl.

Main differences from the original:
  * HTTP via urllib: credentials never appear on a command line, real HTTP
    status codes are checked (no more "success" on 401/404/500), and upload
    content can never be interpreted as "@file" by curl.
  * Edits of events, tasks and contacts modify the ORIGINAL item in place and
    write it back to its own URL with If-Match (etag). Nothing else in the item
    (recurrence, alarms, attendees, addresses, photos, ...) is thrown away.
  * Append commands for files and notes, conditional writes everywhere.

Every command prints one JSON object: {"status": "success", "data": ...} or
{"status": "error", "message": ...} (exit code 1 on error).
"""

import argparse
import base64
import json
import os
import re
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from xml.sax.saxutils import escape as xml_escape

try:  # Python 3.9+
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover - Python 3.8
    ZoneInfo = None
    ZoneInfoNotFoundError = Exception

DEFAULT_ENV_FILE = os.path.expanduser("~/.hermes/nextcloud.env")
ENV_KEYS = (
    "NEXTCLOUD_URL",
    "NEXTCLOUD_USER",
    "NEXTCLOUD_TOKEN",
    "NEXTCLOUD_USER_ID",
    "NEXTCLOUD_TIMEZONE",
    "NEXTCLOUD_ALLOW_HTTP",
)
TIMEOUT = 30

D = "{DAV:}"
CAL = "{urn:ietf:params:xml:ns:caldav}"
CARD = "{urn:ietf:params:xml:ns:carddav}"


class NCError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_env(path=DEFAULT_ENV_FILE):
    """Read the env file; real environment variables take precedence."""
    env = {}
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    for k in ENV_KEYS:
        if os.environ.get(k):
            env[k] = os.environ[k]
    if not env.get("NEXTCLOUD_TIMEZONE"):
        env["NEXTCLOUD_TIMEZONE"] = "UTC"
    if not env.get("NEXTCLOUD_USER_ID"):
        # The login name is usually also the user id. setup.py stores the real
        # id when they differ (e.g. when logging in with an e-mail address).
        env["NEXTCLOUD_USER_ID"] = env.get("NEXTCLOUD_USER", "")
    return env


def require_credentials(env):
    missing = [k for k in ("NEXTCLOUD_URL", "NEXTCLOUD_USER", "NEXTCLOUD_TOKEN") if not env.get(k)]
    if missing:
        raise NCError(f"Missing credentials: {', '.join(missing)}. Run scripts/setup.py first.")


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow redirects: they could carry the Authorization header elsewhere."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)

_STATUS_HINTS = {
    401: "Authentication failed (401): check user name and app password.",
    403: "Forbidden (403): no permission for this item.",
    404: "Not found (404).",
    405: "Method not allowed here (405).",
    409: "Conflict (409): parent folder probably does not exist.",
    412: "Precondition failed (412): the item was changed by someone else since it was read, "
         "or it already exists. Read it again and retry.",
    423: "Locked (423): the item is locked by another client.",
    507: "Insufficient storage (507): quota exceeded.",
}


class Client:
    def __init__(self, env):
        require_credentials(env)
        self.base = env["NEXTCLOUD_URL"].rstrip("/")
        parsed = urllib.parse.urlsplit(self.base)
        if parsed.scheme not in ("https", "http") or not parsed.netloc:
            raise NCError(f"Invalid NEXTCLOUD_URL: {self.base!r}")
        if parsed.scheme == "http" and env.get("NEXTCLOUD_ALLOW_HTTP") != "1":
            raise NCError("Refusing to send credentials over plain http://. Use https:// "
                          "(or set NEXTCLOUD_ALLOW_HTTP=1 if you really mean it).")
        self.origin = f"{parsed.scheme}://{parsed.netloc}"
        self.user = env["NEXTCLOUD_USER"]
        self.user_id = env.get("NEXTCLOUD_USER_ID") or self.user
        self.tz = env.get("NEXTCLOUD_TIMEZONE") or "UTC"
        raw = f"{env['NEXTCLOUD_USER']}:{env['NEXTCLOUD_TOKEN']}".encode("utf-8")
        self._auth = "Basic " + base64.b64encode(raw).decode("ascii")

    # -- URL helpers --------------------------------------------------------

    def url(self, path_or_href):
        """Absolute URL. Server hrefs (already encoded, absolute path) use the origin."""
        if path_or_href.startswith("http://") or path_or_href.startswith("https://"):
            return path_or_href
        return self.origin + path_or_href

    def _uid_q(self):
        return urllib.parse.quote(self.user_id, safe="@")

    def dav_path(self, path=""):
        prefix = urllib.parse.urlsplit(self.base).path.rstrip("/")
        path = urllib.parse.quote(path.lstrip("/"), safe="/")
        return f"{prefix}/remote.php/dav/files/{self._uid_q()}/{path}"

    def caldav_home(self):
        prefix = urllib.parse.urlsplit(self.base).path.rstrip("/")
        return f"{prefix}/remote.php/dav/calendars/{self._uid_q()}/"

    def carddav_home(self):
        prefix = urllib.parse.urlsplit(self.base).path.rstrip("/")
        return f"{prefix}/remote.php/dav/addressbooks/users/{self._uid_q()}/"

    def dav_root(self):
        prefix = urllib.parse.urlsplit(self.base).path.rstrip("/")
        return f"{prefix}/remote.php/dav/"

    def notes_path(self, sub=""):
        prefix = urllib.parse.urlsplit(self.base).path.rstrip("/")
        return f"{prefix}/index.php/apps/notes/api/v1/{sub.lstrip('/')}"

    def rel_file_path(self, href):
        """Server href -> path relative to the user's files root."""
        dec = urllib.parse.unquote(href)
        marker = f"/remote.php/dav/files/{self.user_id}"
        i = dec.find(marker)
        rel = dec[i + len(marker):] if i >= 0 else dec
        return "/" + rel.lstrip("/")

    # -- request ------------------------------------------------------------

    def request(self, method, path, body=None, headers=None, ok=(200, 201, 204, 207)):
        h = {
            "Authorization": self._auth,
            "OCS-APIRequest": "true",
            "User-Agent": "hermes-nextcloud",
        }
        if headers:
            h.update(headers)
        data = body.encode("utf-8") if isinstance(body, str) else body
        req = urllib.request.Request(self.url(path), data=data, method=method, headers=h)
        try:
            with _OPENER.open(req, timeout=TIMEOUT) as r:
                status, rheaders, content = r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            status, rheaders, content = e.code, e.headers, (e.read() or b"")
        except urllib.error.URLError as e:
            raise NCError(f"Connection error: {e.reason}")
        except (socket.timeout, TimeoutError):
            raise NCError(f"Request timed out after {TIMEOUT} seconds")
        if 300 <= status < 400:
            loc = rheaders.get("Location", "") if rheaders else ""
            raise NCError(f"Unexpected redirect ({status}) to {loc!r}. Check NEXTCLOUD_URL.", status)
        if status not in ok:
            msg = _STATUS_HINTS.get(status, f"HTTP {status}")
            detail = re.sub(r"<[^>]+>", " ", content.decode("utf-8", "replace"))
            detail = re.sub(r"\s+", " ", detail).strip()[:200]
            if detail:
                msg += f" Server said: {detail}"
            raise NCError(msg, status)
        return status, rheaders, content


def parse_multistatus(content):
    """-> list of (href, {clark_tag: element}) for properties with a 200 status."""
    try:
        root = ET.fromstring(content)
    except ET.ParseError as e:
        raise NCError(f"Could not parse server XML: {e}")
    out = []
    for resp in root.iter(D + "response"):
        href = (resp.findtext(D + "href") or "").strip()
        props = {}
        for ps in resp.findall(D + "propstat"):
            st = ps.findtext(D + "status") or ""
            if " 200" not in st:
                continue
            prop = ps.find(D + "prop")
            if prop is not None:
                for child in prop:
                    props[child.tag] = child
        out.append((href, props))
    return out


def _ptext(props, tag):
    el = props.get(tag)
    return (el.text or "").strip() if el is not None and el.text else ""


def _same_href(a, b):
    return urllib.parse.unquote(a).rstrip("/") == urllib.parse.unquote(b).rstrip("/")


# ---------------------------------------------------------------------------
# iCalendar / vCard text helpers (RFC 5545 / RFC 6350)
# ---------------------------------------------------------------------------

def ical_escape(s):
    return (str(s).replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
            .replace("\r\n", "\\n").replace("\n", "\\n"))


def ical_unescape(s):
    out, i = [], 0
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            n = s[i + 1]
            out.append("\n" if n in "nN" else n)
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def unfold(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"\n[ \t]", "", text).split("\n")


def fold(line):
    """Fold a content line to max 75 octets without splitting UTF-8 characters."""
    if len(line.encode("utf-8")) <= 75:
        return line
    parts, cur, limit = [], "", 75
    for ch in line:
        if len((cur + ch).encode("utf-8")) > limit:
            parts.append(cur)
            cur, limit = ch, 74  # continuation lines start with a space
        else:
            cur += ch
    parts.append(cur)
    return "\r\n ".join(parts)


def serialize(lines):
    return "\r\n".join(fold(l) for l in lines if l != "") + "\r\n"


def split_prop(line):
    """'DTSTART;TZID=Europe/Amsterdam:2026...' -> ('DTSTART', ';TZID=...', '2026...')."""
    in_q = False
    for i, ch in enumerate(line):
        if ch == '"':
            in_q = not in_q
        elif ch == ":" and not in_q:
            left, value = line[:i], line[i + 1:]
            break
    else:
        return line.upper(), "", ""
    semi = left.find(";")
    name = left if semi < 0 else left[:semi]
    params = "" if semi < 0 else left[semi:]
    if "." in name:  # vCard group, e.g. item1.EMAIL
        name = name.split(".", 1)[1]
    return name.upper(), params, value


def find_blocks(lines, comp):
    """Index pairs (begin, end) of every <comp> block (top level within its parent)."""
    blocks, i, comp = [], 0, comp.upper()
    while i < len(lines):
        if lines[i].strip().upper() == f"BEGIN:{comp}":
            depth = 0
            for j in range(i, len(lines)):
                u = lines[j].strip().upper()
                if u.startswith("BEGIN:"):
                    depth += 1
                elif u.startswith("END:"):
                    depth -= 1
                    if depth == 0:
                        blocks.append((i, j))
                        i = j
                        break
        i += 1
    return blocks


def master_block(lines, comp):
    blocks = find_blocks(lines, comp)
    if not blocks:
        return None
    for b in blocks:  # prefer the master over RECURRENCE-ID overrides
        if not any(split_prop(l)[0] == "RECURRENCE-ID" for l in direct_props(lines, b)):
            return b
    return blocks[0]


def direct_props(lines, block):
    """Property lines that belong directly to the block (not to nested VALARM etc.)."""
    b, e = block
    out, depth = [], 0
    for l in lines[b + 1:e]:
        u = l.strip().upper()
        if u.startswith("BEGIN:"):
            depth += 1
        elif u.startswith("END:"):
            depth -= 1
        elif depth == 0 and l.strip():
            out.append(l)
    return out


def props_dict(lines, block):
    d = {}
    for l in direct_props(lines, block):
        name, params, value = split_prop(l)
        d.setdefault(name, []).append((params, value))
    return d


def modify_component(text, comp, remove=(), add=()):
    """Remove properties (by name) from the master <comp> and append new lines.

    Everything else in the item stays byte-for-byte the same (apart from line
    folding), including nested components and unknown properties.
    """
    lines = unfold(text)
    block = master_block(lines, comp)
    if block is None:
        raise NCError(f"No {comp} component found in item")
    b, e = block
    remove = {r.upper() for r in remove}
    new_inner, depth = [], 0
    for l in lines[b + 1:e]:
        u = l.strip().upper()
        if u.startswith("BEGIN:"):
            depth += 1
        elif u.startswith("END:"):
            depth -= 1
        elif depth == 0 and split_prop(l)[0] in remove:
            continue
        new_inner.append(l)
    # Put new properties before any nested components (VALARM), which is tidy.
    insert_at = next((i for i, l in enumerate(new_inner) if l.strip().upper().startswith("BEGIN:")),
                     len(new_inner))
    new_inner[insert_at:insert_at] = list(add)
    return serialize(lines[:b + 1] + new_inner + lines[e:])


# -- dates --------------------------------------------------------------------

def _zone(tzname):
    if tzname.upper() in ("UTC", "Z", "GMT"):
        return timezone.utc
    if ZoneInfo is None:
        return None
    try:
        return ZoneInfo(tzname)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        raise NCError(f"Unknown timezone {tzname!r} (set NEXTCLOUD_TIMEZONE, e.g. Europe/Amsterdam)")


def parse_user_dt(value, tzname):
    """User input -> (params, ical_value, is_date).

    'YYYY-MM-DD' -> all-day date. Times without offset are interpreted in the
    configured timezone and written as UTC ('...Z'), which every client and
    server accepts without needing a VTIMEZONE block.
    """
    v = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        return ";VALUE=DATE", v.replace("-", ""), True
    if re.fullmatch(r"\d{8}", v):
        return ";VALUE=DATE", v, True
    if re.fullmatch(r"\d{8}T\d{6}Z?", v):
        v = f"{v[0:4]}-{v[4:6]}-{v[6:8]}T{v[9:11]}:{v[11:13]}:{v[13:15]}" + ("+00:00" if v.endswith("Z") else "")
    v = v.replace(" ", "T", 1)
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        raise NCError(f"Cannot parse date/time {value!r}. Use YYYY-MM-DD or YYYY-MM-DD HH:MM.")
    if dt.tzinfo is None:
        zone = _zone(tzname)
        if zone is None:  # no zoneinfo available: write a TZID instead
            return f";TZID={tzname}", dt.strftime("%Y%m%dT%H%M%S"), False
        dt = dt.replace(tzinfo=zone)
    return "", dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ"), False


def range_bound(value, tzname, end=False):
    """--from/--to -> UTC 'YYYYMMDDTHHMMSSZ' for a CalDAV time-range."""
    _, v, is_date = parse_user_dt(value, tzname)
    if is_date:
        d = datetime.strptime(v, "%Y%m%d")
        if end:
            d += timedelta(days=1)
        zone = _zone(tzname) or timezone.utc
        return d.replace(tzinfo=zone).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if not v.endswith("Z"):
        raise NCError("Time ranges need zoneinfo support (Python 3.9+) or explicit UTC times.")
    return v


def display_dt(params, value, tzname):
    """iCal value -> readable ISO string in the configured timezone."""
    if not value:
        return ""
    try:
        if "VALUE=DATE" in params.upper() or re.fullmatch(r"\d{8}", value):
            return f"{value[0:4]}-{value[4:6]}-{value[6:8]}"
        dt = datetime.strptime(value.rstrip("Z"), "%Y%m%dT%H%M%S")
        if value.endswith("Z"):
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            m = re.search(r"TZID=\"?([^;:\"]+)", params, re.I)
            src = m.group(1) if m else tzname
            try:
                zone = _zone(src)
            except NCError:
                zone = None
            if zone is None:
                return dt.strftime("%Y-%m-%dT%H:%M:%S") + (f" ({src})" if m else "")
            dt = dt.replace(tzinfo=zone)
        zone = _zone(tzname)
        if zone is not None:
            dt = dt.astimezone(zone)
        return dt.strftime("%Y-%m-%dT%H:%M:%S%z")
    except (ValueError, NCError):
        return value


def now_utc():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# ---------------------------------------------------------------------------
# Files (WebDAV)
# ---------------------------------------------------------------------------

PROPFIND_FILES = """<?xml version="1.0" encoding="UTF-8"?>
<d:propfind xmlns:d="DAV:">
  <d:prop>
    <d:resourcetype/><d:getcontenttype/><d:getcontentlength/>
    <d:getlastmodified/><d:getetag/>
  </d:prop>
</d:propfind>"""


def _file_entry(c, href, props):
    rt = props.get(D + "resourcetype")
    is_dir = rt is not None and rt.find(D + "collection") is not None
    size = _ptext(props, D + "getcontentlength")
    return {
        "path": c.rel_file_path(href),
        "type": "dir" if is_dir else "file",
        "size": int(size) if size.isdigit() else 0,
        "content_type": _ptext(props, D + "getcontenttype"),
        "last_modified": _ptext(props, D + "getlastmodified"),
        "etag": _ptext(props, D + "getetag"),
    }


def files_list(c, path="/"):
    url = c.dav_path(path)
    _, _, body = c.request("PROPFIND", url, PROPFIND_FILES,
                           {"Depth": "1", "Content-Type": "application/xml; charset=utf-8"})
    items = [_file_entry(c, h, p) for h, p in parse_multistatus(body) if not _same_href(h, url)]
    items.sort(key=lambda x: (x["type"] != "dir", x["path"].lower()))
    return items


def files_get(c, path):
    _, h, body = c.request("GET", c.dav_path(path), ok=(200,))
    ctype = h.get("Content-Type", "") if h else ""
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return {"path": path, "binary": True, "size": len(body), "content_type": ctype,
                "etag": h.get("ETag", "") if h else "",
                "message": "Binary file: use 'files download --local <file>' instead."}
    return {"path": path, "content": text, "etag": h.get("ETag", "") if h else ""}


def files_put(c, path, data, if_match=None, no_overwrite=False):
    headers = {"Content-Type": "application/octet-stream"}
    if if_match:
        headers["If-Match"] = if_match if if_match.startswith('"') else f'"{if_match}"'
    if no_overwrite:
        headers["If-None-Match"] = "*"
    status, h, _ = c.request("PUT", c.dav_path(path), data, headers, ok=(200, 201, 204))
    return {"path": path, "uploaded": True, "created": status == 201,
            "etag": (h.get("ETag") or h.get("OC-ETag") or "") if h else ""}


def files_append(c, path, text, retries=3):
    """Append a line (or block) to a text file; creates the file if missing."""
    for _ in range(retries):
        try:
            cur = files_get(c, path)
        except NCError as e:
            if e.status != 404:
                raise
            res = files_put(c, path, (text.rstrip("\n") + "\n").encode("utf-8"), no_overwrite=True)
            res["appended"] = True
            return res
        if cur.get("binary"):
            raise NCError("Refusing to append to a binary file")
        content = cur["content"]
        if content and not content.endswith("\n"):
            content += "\n"
        new = content + text.rstrip("\n") + "\n"
        try:
            res = files_put(c, path, new.encode("utf-8"), if_match=cur["etag"] or None)
            res["appended"] = True
            return res
        except NCError as e:
            if e.status != 412:
                raise
    raise NCError("File kept changing while appending; try again")


def files_mkdir(c, path):
    c.request("MKCOL", c.dav_path(path), ok=(201,))
    return {"path": path, "created": True}


def files_delete(c, path):
    c.request("DELETE", c.dav_path(path), ok=(200, 204))
    return {"path": path, "deleted": True}


def files_move(c, src, dst, overwrite=False):
    c.request("MOVE", c.dav_path(src), headers={
        "Destination": c.url(c.dav_path(dst)),
        "Overwrite": "T" if overwrite else "F",
    }, ok=(201, 204))
    return {"src": src, "dst": dst, "moved": True}


def files_search(c, query, max_results=100):
    """Search by file name (case-insensitive substring)."""
    body = f"""<?xml version="1.0" encoding="UTF-8"?>
<d:searchrequest xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">
  <d:basicsearch>
    <d:select><d:prop>
      <d:resourcetype/><d:getcontenttype/><d:getcontentlength/><d:getlastmodified/><d:getetag/>
    </d:prop></d:select>
    <d:from><d:scope>
      <d:href>/files/{xml_escape(c.user_id)}</d:href><d:depth>infinity</d:depth>
    </d:scope></d:from>
    <d:where><d:like><d:prop><d:displayname/></d:prop>
      <d:literal>%{xml_escape(query)}%</d:literal></d:like></d:where>
    <d:limit><d:nresults>{max_results}</d:nresults></d:limit>
  </d:basicsearch>
</d:searchrequest>"""
    try:
        _, _, res = c.request("SEARCH", c.dav_root(), body,
                              {"Content-Type": "text/xml; charset=utf-8"}, ok=(207,))
        found = [_file_entry(c, h, p) for h, p in parse_multistatus(res)]
        q = query.lower()
        return [f for f in found if q in f["path"].rsplit("/", 1)[-1].lower()]
    except NCError as e:
        if e.status not in (400, 405, 415, 501):
            raise
    # Fallback: breadth-first walk with Depth 1 (Depth: infinity is often disabled).
    q, out, queue, seen = query.lower(), [], ["/"], 0
    while queue and seen < 500 and len(out) < max_results:
        d = queue.pop(0)
        seen += 1
        for item in files_list(c, d):
            if query and q in item["path"].rstrip("/").rsplit("/", 1)[-1].lower():
                out.append(item)
            if item["type"] == "dir":
                queue.append(item["path"])
    return out


# ---------------------------------------------------------------------------
# Notes (Nextcloud Notes API v1)
# ---------------------------------------------------------------------------

def _json(body):
    try:
        return json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise NCError(f"Unexpected response: {body[:200]!r}")


def notes_list(c, category=None, with_content=False):
    params = {}
    if category is not None:
        params["category"] = category
    if not with_content:
        params["exclude"] = "content"
    q = ("?" + urllib.parse.urlencode(params)) if params else ""
    _, _, body = c.request("GET", c.notes_path("notes") + q, headers={"Accept": "application/json"}, ok=(200,))
    return _json(body)


def notes_find(c, query):
    q = query.lower()
    return [n for n in notes_list(c) if q in (n.get("title") or "").lower()
            or q in (n.get("category") or "").lower()]


def notes_get(c, note_id):
    _, _, body = c.request("GET", c.notes_path(f"notes/{int(note_id)}"),
                           headers={"Accept": "application/json"}, ok=(200,))
    return _json(body)


def notes_create(c, title, content, category=None):
    payload = {"title": title, "content": content}
    if category is not None:
        payload["category"] = category
    _, _, body = c.request("POST", c.notes_path("notes"), json.dumps(payload),
                           {"Content-Type": "application/json", "Accept": "application/json"}, ok=(200, 201))
    return _json(body)


def notes_edit(c, note_id, title=None, content=None, category=None, etag=None):
    """Update only the given fields. With --etag the edit fails (412) if the note
    changed after you read it; without it the current etag is used."""
    if etag is None:
        etag = notes_get(c, note_id).get("etag", "")
    payload = {}
    if title is not None:
        payload["title"] = title
    if content is not None:
        payload["content"] = content
    if category is not None:
        payload["category"] = category
    if not payload:
        raise NCError("Nothing to change: give --title, --content and/or --category")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if etag:
        headers["If-Match"] = etag if etag.startswith('"') else f'"{etag}"'
    _, _, body = c.request("PUT", c.notes_path(f"notes/{int(note_id)}"), json.dumps(payload), headers, ok=(200,))
    return _json(body)


def notes_append(c, note_id, text, retries=3):
    for _ in range(retries):
        note = notes_get(c, note_id)
        content = note.get("content") or ""
        if content and not content.endswith("\n"):
            content += "\n"
        try:
            return notes_edit(c, note_id, content=content + text.rstrip("\n") + "\n", etag=note.get("etag"))
        except NCError as e:
            if e.status != 412:
                raise
    raise NCError("Note kept changing while appending; try again")


def notes_delete(c, note_id):
    c.request("DELETE", c.notes_path(f"notes/{int(note_id)}"), ok=(200, 204))
    return {"id": int(note_id), "deleted": True}


# ---------------------------------------------------------------------------
# CalDAV: calendars, events, tasks
# ---------------------------------------------------------------------------

def calendars_list(c, comp=None):
    """comp: None, 'VEVENT' or 'VTODO'."""
    body = """<?xml version="1.0" encoding="UTF-8"?>
<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav" xmlns:cs="http://calendarserver.org/ns/">
  <d:prop><d:displayname/><d:resourcetype/><c:supported-calendar-component-set/><cs:getctag/></d:prop>
</d:propfind>"""
    _, _, res = c.request("PROPFIND", c.caldav_home(), body,
                          {"Depth": "1", "Content-Type": "application/xml; charset=utf-8"})
    out = []
    for href, props in parse_multistatus(res):
        rt = props.get(D + "resourcetype")
        if rt is None or rt.find(CAL + "calendar") is None:
            continue
        comps_el = props.get(CAL + "supported-calendar-component-set")
        comps = [x.get("name") for x in comps_el] if comps_el is not None else ["VEVENT", "VTODO"]
        if comp and comp not in comps:
            continue
        cid = urllib.parse.unquote(href.rstrip("/").rsplit("/", 1)[-1])
        out.append({"id": cid, "name": _ptext(props, D + "displayname") or cid,
                    "components": comps, "href": href})
    return out


def pick_calendar(c, comp, name=None, preferred=()):
    cals = calendars_list(c, comp)
    if not cals:
        raise NCError(f"No calendar supporting {comp} found. Create one in Nextcloud first.")
    if name:
        n = name.lower()
        for cal in cals:
            if cal["id"].lower() == n or cal["name"].lower() == n:
                return cal
        raise NCError(f"Calendar {name!r} not found. Available: {[x['name'] for x in cals]}")
    for p in preferred:
        for cal in cals:
            if cal["id"].lower() == p or cal["name"].lower() == p:
                return cal
    # Skip read-only system calendars where possible
    for cal in cals:
        if cal["id"] not in ("contact_birthdays",) and not cal["id"].startswith("app-generated"):
            return cal
    return cals[0]


def _cal_query(c, cal_href, comp, start=None, end=None, uid=None):
    """-> list of (href, etag, ics). REPORT calendar-query with a GET fallback."""
    tr = f'<c:time-range start="{start}" end="{end}"/>' if (start or end) else ""
    if tr and not start:
        tr = f'<c:time-range end="{end}"/>'
    if tr and not end:
        tr = f'<c:time-range start="{start}"/>'
    uf = (f'<c:prop-filter name="UID"><c:text-match collation="i;octet">{xml_escape(uid)}</c:text-match>'
          f'</c:prop-filter>') if uid else ""
    body = f"""<?xml version="1.0" encoding="UTF-8"?>
<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop><d:getetag/><c:calendar-data/></d:prop>
  <c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="{comp}">{tr}{uf}</c:comp-filter></c:comp-filter></c:filter>
</c:calendar-query>"""
    try:
        _, _, res = c.request("REPORT", cal_href, body,
                              {"Depth": "1", "Content-Type": "application/xml; charset=utf-8"}, ok=(207,))
        out = []
        for href, props in parse_multistatus(res):
            data = props.get(CAL + "calendar-data")
            if data is not None and data.text:
                out.append((href, _ptext(props, D + "getetag"), data.text))
        return out
    except NCError as e:
        if e.status not in (400, 403, 405, 415, 501):
            raise
    # Fallback: PROPFIND + GET each item (slow, no server-side filtering)
    _, _, res = c.request("PROPFIND", cal_href, PROPFIND_FILES,
                          {"Depth": "1", "Content-Type": "application/xml; charset=utf-8"})
    out = []
    for href, _p in parse_multistatus(res):
        if _same_href(href, cal_href) or not href.endswith(".ics"):
            continue
        _, h, data = c.request("GET", href, headers={"Accept": "text/calendar"}, ok=(200,))
        text = data.decode("utf-8", "replace")
        if f"BEGIN:{comp}" in text:
            out.append((href, h.get("ETag", "") if h else "", text))
    return out


def _cal_item(c, href, etag, ics, comp, cal):
    lines = unfold(ics)
    block = master_block(lines, comp)
    if block is None:
        return None
    p = props_dict(lines, block)

    def first(name, unescape=True):
        v = p.get(name)
        if not v:
            return ""
        return ical_unescape(v[0][1]) if unescape else v[0][1]

    def dt(name):
        v = p.get(name)
        return display_dt(v[0][0], v[0][1], c.tz) if v else ""

    item = {"uid": first("UID", False), "summary": first("SUMMARY")}
    if comp == "VEVENT":
        item.update({"start": dt("DTSTART"), "end": dt("DTEND"), "location": first("LOCATION")})
        if "RRULE" in p:
            item["recurring"] = first("RRULE", False)
    else:
        item.update({"due": dt("DUE"), "status": first("STATUS", False) or "NEEDS-ACTION"})
        if "COMPLETED" in p:
            item["completed"] = dt("COMPLETED")
    pri = first("PRIORITY", False)
    if pri.isdigit() and int(pri):
        item["priority"] = int(pri)
    desc = first("DESCRIPTION")
    if desc:
        item["description"] = desc
    if "CATEGORIES" in p:
        item["categories"] = first("CATEGORIES")
    if p.get("RELATED-TO"):
        item["parent"] = first("RELATED-TO", False)
    item.update({"calendar": cal["name"], "href": href, "etag": etag})
    return {k: v for k, v in item.items() if v != ""}


def items_list(c, comp, calendar=None, start=None, end=None):
    cals = [pick_calendar(c, comp, calendar)] if calendar else calendars_list(c, comp)
    out = []
    for cal in cals:
        for href, etag, ics in _cal_query(c, cal["href"], comp, start, end):
            it = _cal_item(c, href, etag, ics, comp, cal)
            if it:
                out.append(it)
    key = "start" if comp == "VEVENT" else "due"
    out.sort(key=lambda x: (x.get(key) or "9999", x.get("summary", "").lower()))
    return out


def find_item(c, comp, uid, calendar=None):
    """-> (cal, href, etag, ics) of the item with this UID."""
    cals = [pick_calendar(c, comp, calendar)] if calendar else calendars_list(c, comp)
    for cal in cals:
        for href, etag, ics in _cal_query(c, cal["href"], comp, uid=uid):
            lines = unfold(ics)
            block = master_block(lines, comp)
            if block and props_dict(lines, block).get("UID", [("", "")])[0][1] == uid:
                return cal, href, etag, ics
    raise NCError(f"{'Event' if comp == 'VEVENT' else 'Task'} with uid {uid!r} not found")


def _put_item(c, href, ics, etag=None, create=False):
    headers = {"Content-Type": "text/calendar; charset=utf-8"}
    if create:
        headers["If-None-Match"] = "*"
    elif etag:
        headers["If-Match"] = etag
    _, h, _ = c.request("PUT", href, ics, headers, ok=(200, 201, 204))
    return h.get("ETag", "") if h else ""


def _new_href(cal, uid):
    return cal["href"].rstrip("/") + "/" + urllib.parse.quote(uid, safe="") + ".ics"


def _wrap_vcalendar(comp_lines):
    return serialize(["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Hermes Agent//hermes-nextcloud//EN",
                      "CALSCALE:GREGORIAN"] + comp_lines + ["END:VCALENDAR"])


def _text_prop(name, value):
    return f"{name}:{ical_escape(value)}"


def _dt_prop(c, name, value):
    params, v, _ = parse_user_dt(value, c.tz)
    return f"{name}{params}:{v}"


def _priority(p):
    if p is None:
        return None
    p = int(p)
    if not 0 <= p <= 9:
        raise NCError("Priority must be 0-9 (1 = highest, 9 = lowest, 0 = none)")
    return p


# -- tasks --------------------------------------------------------------------

TASK_CAL_PREFERENCE = ("tasks", "taken", "todo", "to-do", "takenlijst")


def tasks_list(c, calendar=None, open_only=False):
    items = items_list(c, "VTODO", calendar)
    if open_only:
        items = [t for t in items if t.get("status") not in ("COMPLETED", "CANCELLED")]
    return items


def tasks_create(c, title, calendar=None, due=None, priority=None, description=None, categories=None):
    cal = pick_calendar(c, "VTODO", calendar, TASK_CAL_PREFERENCE)
    uid = str(uuid.uuid4())
    now = now_utc()
    lines = ["BEGIN:VTODO", f"UID:{uid}", f"DTSTAMP:{now}", f"CREATED:{now}", f"LAST-MODIFIED:{now}",
             _text_prop("SUMMARY", title), "STATUS:NEEDS-ACTION"]
    if due:
        lines.append(_dt_prop(c, "DUE", due))
    if _priority(priority):
        lines.append(f"PRIORITY:{_priority(priority)}")
    if description:
        lines.append(_text_prop("DESCRIPTION", description))
    if categories:
        lines.append("CATEGORIES:" + ",".join(ical_escape(x.strip()) for x in categories.split(",") if x.strip()))
    lines.append("END:VTODO")
    href = _new_href(cal, uid)
    etag = _put_item(c, href, _wrap_vcalendar(lines), create=True)
    return {"uid": uid, "summary": title, "calendar": cal["name"], "href": href, "etag": etag, "created": True}


def _edit_item(c, comp, uid, calendar, etag, remove, add):
    cal, href, cur_etag, ics = find_item(c, comp, uid, calendar)
    remove = set(remove) | {"DTSTAMP", "LAST-MODIFIED"}
    add = list(add) + [f"DTSTAMP:{now_utc()}", f"LAST-MODIFIED:{now_utc()}"]
    if comp == "VEVENT":
        lines = unfold(ics)
        seq = props_dict(lines, master_block(lines, comp)).get("SEQUENCE", [("", "0")])[0][1]
        remove.add("SEQUENCE")
        add.append(f"SEQUENCE:{int(seq) + 1 if seq.isdigit() else 1}")
    new_ics = modify_component(ics, comp, remove, add)
    new_etag = _put_item(c, href, new_ics, etag or cur_etag)
    item = _cal_item(c, href, new_etag, new_ics, comp, cal)
    item["updated"] = True
    return item


def tasks_edit(c, uid, calendar=None, title=None, due=None, priority=None, description=None,
               categories=None, etag=None):
    remove, add = set(), []
    if title is not None:
        remove.add("SUMMARY"); add.append(_text_prop("SUMMARY", title))
    if due is not None:
        remove.add("DUE")
        if due.strip():
            add.append(_dt_prop(c, "DUE", due))
    if priority is not None:
        remove.add("PRIORITY")
        if _priority(priority):
            add.append(f"PRIORITY:{_priority(priority)}")
    if description is not None:
        remove.add("DESCRIPTION")
        if description:
            add.append(_text_prop("DESCRIPTION", description))
    if categories is not None:
        remove.add("CATEGORIES")
        if categories.strip():
            add.append("CATEGORIES:" + ",".join(ical_escape(x.strip()) for x in categories.split(",") if x.strip()))
    if not remove:
        raise NCError("Nothing to change")
    return _edit_item(c, "VTODO", uid, calendar, etag, remove, add)


def tasks_complete(c, uid, calendar=None):
    return _edit_item(c, "VTODO", uid, calendar, None,
                      {"STATUS", "COMPLETED", "PERCENT-COMPLETE"},
                      ["STATUS:COMPLETED", f"COMPLETED:{now_utc()}", "PERCENT-COMPLETE:100"])


def tasks_reopen(c, uid, calendar=None):
    return _edit_item(c, "VTODO", uid, calendar, None,
                      {"STATUS", "COMPLETED", "PERCENT-COMPLETE"}, ["STATUS:NEEDS-ACTION"])


def item_delete(c, comp, uid, calendar=None):
    cal, href, etag, _ = find_item(c, comp, uid, calendar)
    headers = {"If-Match": etag} if etag else {}
    c.request("DELETE", href, headers=headers, ok=(200, 204))
    return {"uid": uid, "calendar": cal["name"], "deleted": True}


# -- events -------------------------------------------------------------------

def events_list(c, calendar=None, cal_from=None, cal_to=None):
    start = range_bound(cal_from, c.tz) if cal_from else None
    end = range_bound(cal_to, c.tz, end=True) if cal_to else None
    return items_list(c, "VEVENT", calendar, start, end)


def events_create(c, summary, start, end=None, calendar=None, location=None, description=None):
    cal = pick_calendar(c, "VEVENT", calendar, ("personal", "persoonlijk"))
    sp, sv, s_is_date = parse_user_dt(start, c.tz)
    if end:
        ep, ev, e_is_date = parse_user_dt(end, c.tz)
        if s_is_date != e_is_date:
            raise NCError("Start and end must both be dates or both be date-times")
    elif s_is_date:  # all-day, one day: DTEND is exclusive
        ep, ev = sp, (datetime.strptime(sv, "%Y%m%d") + timedelta(days=1)).strftime("%Y%m%d")
    else:
        raise NCError("--end is required for timed events")
    if ev <= sv and ep == sp:
        raise NCError("End must be after start")
    uid = str(uuid.uuid4())
    now = now_utc()
    lines = ["BEGIN:VEVENT", f"UID:{uid}", f"DTSTAMP:{now}", f"CREATED:{now}", f"LAST-MODIFIED:{now}",
             "SEQUENCE:0", f"DTSTART{sp}:{sv}", f"DTEND{ep}:{ev}", _text_prop("SUMMARY", summary)]
    if location:
        lines.append(_text_prop("LOCATION", location))
    if description:
        lines.append(_text_prop("DESCRIPTION", description))
    lines.append("END:VEVENT")
    href = _new_href(cal, uid)
    etag = _put_item(c, href, _wrap_vcalendar(lines), create=True)
    return {"uid": uid, "summary": summary, "start": display_dt(sp, sv, c.tz),
            "end": display_dt(ep, ev, c.tz), "calendar": cal["name"], "href": href, "etag": etag,
            "created": True}


def events_edit(c, uid, calendar=None, summary=None, start=None, end=None, location=None,
                description=None, etag=None):
    remove, add = set(), []
    if summary is not None:
        remove.add("SUMMARY"); add.append(_text_prop("SUMMARY", summary))
    if start is not None:
        remove.add("DTSTART"); add.append(_dt_prop(c, "DTSTART", start))
    if end is not None:
        remove |= {"DTEND", "DURATION"}; add.append(_dt_prop(c, "DTEND", end))
    if location is not None:
        remove.add("LOCATION")
        if location:
            add.append(_text_prop("LOCATION", location))
    if description is not None:
        remove.add("DESCRIPTION")
        if description:
            add.append(_text_prop("DESCRIPTION", description))
    if not remove:
        raise NCError("Nothing to change")
    return _edit_item(c, "VEVENT", uid, calendar, etag, remove, add)


# ---------------------------------------------------------------------------
# CardDAV: contacts
# ---------------------------------------------------------------------------

def addressbooks_list(c):
    body = """<?xml version="1.0" encoding="UTF-8"?>
<d:propfind xmlns:d="DAV:"><d:prop><d:displayname/><d:resourcetype/></d:prop></d:propfind>"""
    _, _, res = c.request("PROPFIND", c.carddav_home(), body,
                          {"Depth": "1", "Content-Type": "application/xml; charset=utf-8"})
    out = []
    for href, props in parse_multistatus(res):
        rt = props.get(D + "resourcetype")
        if rt is None or rt.find(CARD + "addressbook") is None:
            continue
        aid = urllib.parse.unquote(href.rstrip("/").rsplit("/", 1)[-1])
        out.append({"id": aid, "name": _ptext(props, D + "displayname") or aid, "href": href})
    return out


def pick_addressbook(c, name=None):
    books = addressbooks_list(c)
    if not books:
        raise NCError("No address books found")
    if name:
        n = name.lower()
        for b in books:
            if b["id"].lower() == n or b["name"].lower() == n:
                return b
        raise NCError(f"Address book {name!r} not found. Available: {[b['name'] for b in books]}")
    for b in books:
        if b["id"] == "contacts":
            return b
    return books[0]


def _card_query(c, ab_href, uid=None):
    """-> list of (href, etag, vcf). REPORT addressbook-query with a GET fallback."""
    if uid:
        flt = (f'<card:prop-filter name="UID"><card:text-match collation="i;octet" match-type="equals">'
               f'{xml_escape(uid)}</card:text-match></card:prop-filter>')
    else:
        flt = '<card:prop-filter name="FN"/>'
    body = f"""<?xml version="1.0" encoding="UTF-8"?>
<card:addressbook-query xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav">
  <d:prop><d:getetag/><card:address-data/></d:prop>
  <card:filter test="anyof">{flt}</card:filter>
</card:addressbook-query>"""
    try:
        _, _, res = c.request("REPORT", ab_href, body,
                              {"Depth": "1", "Content-Type": "application/xml; charset=utf-8"}, ok=(207,))
        out = []
        for href, props in parse_multistatus(res):
            data = props.get(CARD + "address-data")
            if data is not None and data.text:
                out.append((href, _ptext(props, D + "getetag"), data.text))
        return out
    except NCError as e:
        if e.status not in (400, 403, 405, 415, 501):
            raise
    _, _, res = c.request("PROPFIND", ab_href, PROPFIND_FILES,
                          {"Depth": "1", "Content-Type": "application/xml; charset=utf-8"})
    out = []
    for href, _p in parse_multistatus(res):
        if _same_href(href, ab_href) or not href.endswith(".vcf"):
            continue
        _, h, data = c.request("GET", href, headers={"Accept": "text/vcard"}, ok=(200,))
        out.append((href, h.get("ETag", "") if h else "", data.decode("utf-8", "replace")))
    return out


def _contact(href, etag, vcf, book):
    lines = unfold(vcf)
    block = master_block(lines, "VCARD")
    if block is None:
        return None
    p = props_dict(lines, block)

    def first(name):
        v = p.get(name)
        return ical_unescape(v[0][1]) if v else ""

    def many(name):
        return [ical_unescape(v) for _, v in p.get(name, []) if v.strip()]

    org = p.get("ORG")
    org_s = ", ".join(x for x in (ical_unescape(s) for s in re.split(r"(?<!\\);", org[0][1])) if x) if org else ""
    adrs = []
    for _, v in p.get("ADR", []):
        parts = [ical_unescape(s).strip() for s in re.split(r"(?<!\\);", v)]
        s = ", ".join(x for x in parts if x)
        if s:
            adrs.append(s)
    c = {
        "uid": first("UID"), "fullname": first("FN"), "emails": many("EMAIL"), "phones": many("TEL"),
        "organization": org_s, "title": first("TITLE"), "birthday": first("BDAY"),
        "addresses": adrs, "note": first("NOTE"),
        "addressbook": book["name"], "href": href, "etag": etag,
    }
    return {k: v for k, v in c.items() if v not in ("", [])}


def contacts_list(c, addressbook=None):
    books = [pick_addressbook(c, addressbook)] if addressbook else addressbooks_list(c)
    out = []
    for b in books:
        for href, etag, vcf in _card_query(c, b["href"]):
            ct = _contact(href, etag, vcf, b)
            if ct and ct.get("fullname"):
                out.append(ct)
    seen, uniq = set(), []
    for ct in out:
        k = ct.get("uid") or ct["href"]
        if k not in seen:
            seen.add(k)
            uniq.append(ct)
    uniq.sort(key=lambda x: x.get("fullname", "").lower())
    return uniq


def contacts_search(c, query):
    q = query.lower()
    qd = re.sub(r"\D", "", q)
    return [ct for ct in contacts_list(c)
            if q in ct.get("fullname", "").lower() or q in ct.get("organization", "").lower()
            or any(q in e.lower() for e in ct.get("emails", []))
            or (len(qd) >= 3 and any(qd in re.sub(r"\D", "", p) for p in ct.get("phones", [])))]


def find_contact(c, uid, addressbook=None):
    books = [pick_addressbook(c, addressbook)] if addressbook else addressbooks_list(c)
    for b in books:
        for href, etag, vcf in _card_query(c, b["href"], uid=uid):
            lines = unfold(vcf)
            block = master_block(lines, "VCARD")
            if block and props_dict(lines, block).get("UID", [("", "")])[0][1].strip() == uid:
                return b, href, etag, vcf
    raise NCError(f"Contact with uid {uid!r} not found")


def contacts_get(c, uid, addressbook=None, raw=False):
    b, href, etag, vcf = find_contact(c, uid, addressbook)
    ct = _contact(href, etag, vcf, b)
    if raw:
        ct["vcard"] = vcf
    return ct


def _n_from_name(name):
    parts = name.strip().rsplit(" ", 1)
    if len(parts) == 2:
        return f"N:{ical_escape(parts[1])};{ical_escape(parts[0])};;;"
    return f"N:{ical_escape(name)};;;;"


def _split_list(s):
    return [x.strip() for x in s.split(",") if x.strip()]


def contacts_create(c, name, addressbook=None, email=None, phone=None, organization=None, title=None, note=None):
    b = pick_addressbook(c, addressbook)
    uid = str(uuid.uuid4())
    lines = ["BEGIN:VCARD", "VERSION:3.0", "PRODID:-//Hermes Agent//hermes-nextcloud//EN",
             f"UID:{uid}", f"FN:{ical_escape(name)}", _n_from_name(name)]
    for e in _split_list(email or ""):
        lines.append(f"EMAIL;TYPE=INTERNET:{e}")
    for p in _split_list(phone or ""):
        lines.append(f"TEL;TYPE=VOICE:{p}")
    if organization:
        lines.append(f"ORG:{ical_escape(organization)}")
    if title:
        lines.append(f"TITLE:{ical_escape(title)}")
    if note:
        lines.append(f"NOTE:{ical_escape(note)}")
    lines += [f"REV:{now_utc()}", "END:VCARD"]
    href = b["href"].rstrip("/") + "/" + urllib.parse.quote(uid, safe="") + ".vcf"
    _, h, _ = c.request("PUT", href, serialize(lines),
                        {"Content-Type": "text/vcard; charset=utf-8", "If-None-Match": "*"}, ok=(201, 204))
    return {"uid": uid, "fullname": name, "addressbook": b["name"], "href": href, "created": True}


def contacts_edit(c, uid, addressbook=None, name=None, email=None, phone=None, organization=None,
                  title=None, note=None, etag=None):
    """Only the given fields change. --email / --phone REPLACE all existing
    addresses / numbers (comma-separated list); everything else is kept."""
    b, href, cur_etag, vcf = find_contact(c, uid, addressbook)
    remove, add = {"REV"}, []
    if name is not None:
        remove |= {"FN", "N"}; add += [f"FN:{ical_escape(name)}", _n_from_name(name)]
    if email is not None:
        remove.add("EMAIL"); add += [f"EMAIL;TYPE=INTERNET:{e}" for e in _split_list(email)]
    if phone is not None:
        remove.add("TEL"); add += [f"TEL;TYPE=VOICE:{p}" for p in _split_list(phone)]
    if organization is not None:
        remove.add("ORG")
        if organization:
            add.append(f"ORG:{ical_escape(organization)}")
    if title is not None:
        remove.add("TITLE")
        if title:
            add.append(f"TITLE:{ical_escape(title)}")
    if note is not None:
        remove.add("NOTE")
        if note:
            add.append(f"NOTE:{ical_escape(note)}")
    if remove == {"REV"}:
        raise NCError("Nothing to change")
    add.append(f"REV:{now_utc()}")
    new_vcf = modify_component(vcf, "VCARD", remove, add)
    _, h, _ = c.request("PUT", href, new_vcf, {"Content-Type": "text/vcard; charset=utf-8",
                                               "If-Match": etag or cur_etag}, ok=(200, 201, 204))
    ct = _contact(href, h.get("ETag", "") if h else "", new_vcf, b)
    ct["updated"] = True
    return ct


def contacts_delete(c, uid, addressbook=None):
    b, href, etag, _ = find_contact(c, uid, addressbook)
    c.request("DELETE", href, headers={"If-Match": etag} if etag else {}, ok=(200, 204))
    return {"uid": uid, "addressbook": b["name"], "deleted": True}


def contacts_export(c, uid, local, addressbook=None):
    _, _, _, vcf = find_contact(c, uid, addressbook)
    with open(local, "w", encoding="utf-8", newline="") as f:
        f.write(vcf)
    return {"uid": uid, "local": local, "exported": True}


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------

def check(c):
    c.request("PROPFIND", c.dav_path(""), None, {"Depth": "0"}, ok=(207,))
    return {"authenticated": True, "user": c.user, "user_id": c.user_id, "timezone": c.tz}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def read_content(value=None, file=None):
    """--content TEXT | --content - (stdin) | --content-file PATH. Returns bytes."""
    if file:
        with open(os.path.expanduser(file), "rb") as f:
            return f.read()
    if value == "-":
        return sys.stdin.buffer.read()
    if value is None:
        raise NCError("Give --content TEXT, --content - (read stdin) or --content-file PATH")
    return value.encode("utf-8")


def build_parser():
    p = argparse.ArgumentParser(description="Nextcloud API for Hermes Agent")
    p.add_argument("--env-file", default=DEFAULT_ENV_FILE)
    sub = p.add_subparsers(dest="command")

    sub.add_parser("check", help="Verify credentials and connection")

    # files
    f = sub.add_parser("files", help="Files (WebDAV)").add_subparsers(dest="sub")
    x = f.add_parser("list"); x.add_argument("--path", default="/")
    x = f.add_parser("get", help="Print a text file (with etag)"); x.add_argument("--path", "--remote", required=True)
    x = f.add_parser("download"); x.add_argument("--path", "--remote", required=True); x.add_argument("--local", required=True)
    x = f.add_parser("upload", help="Create/overwrite a file")
    x.add_argument("--path", "--remote", required=True)
    x.add_argument("--content", help="Text, or '-' to read stdin")
    x.add_argument("--content-file", "--local", dest="content_file")
    x.add_argument("--if-match", help="Only overwrite if the etag still matches (from 'files get')")
    x.add_argument("--no-overwrite", action="store_true", help="Fail if the file already exists")
    x = f.add_parser("append", help="Append text as new line(s); creates the file if missing")
    x.add_argument("--path", "--remote", required=True); x.add_argument("--text", required=True)
    x = f.add_parser("mkdir"); x.add_argument("--path", required=True)
    x = f.add_parser("delete"); x.add_argument("--path", required=True)
    x = f.add_parser("move"); x.add_argument("--src", required=True); x.add_argument("--dst", required=True)
    x.add_argument("--overwrite", action="store_true")
    x = f.add_parser("search", help="Find files by name"); x.add_argument("--query", required=True)

    # notes
    n = sub.add_parser("notes", help="Nextcloud Notes app").add_subparsers(dest="sub")
    x = n.add_parser("list"); x.add_argument("--category"); x.add_argument("--with-content", action="store_true")
    x = n.add_parser("find", help="Find notes by title/category"); x.add_argument("--query", required=True)
    x = n.add_parser("get"); x.add_argument("--id", required=True, type=int)
    x = n.add_parser("create"); x.add_argument("--title", required=True)
    x.add_argument("--content", default=""); x.add_argument("--content-file"); x.add_argument("--category")
    x = n.add_parser("edit"); x.add_argument("--id", required=True, type=int); x.add_argument("--title")
    x.add_argument("--content", help="Text, or '-' for stdin"); x.add_argument("--content-file")
    x.add_argument("--category"); x.add_argument("--etag", help="etag from 'notes get' (safe concurrent edit)")
    x = n.add_parser("append"); x.add_argument("--id", required=True, type=int); x.add_argument("--text", required=True)
    x = n.add_parser("delete"); x.add_argument("--id", required=True, type=int)

    # calendars
    k = sub.add_parser("calendars", help="List calendars").add_subparsers(dest="sub")
    x = k.add_parser("list"); x.add_argument("--type", choices=["events", "tasks", "all"], default="all")

    # tasks
    t = sub.add_parser("tasks", help="Tasks (CalDAV VTODO)").add_subparsers(dest="sub")
    x = t.add_parser("list"); x.add_argument("--calendar"); x.add_argument("--open", action="store_true")
    x = t.add_parser("create"); x.add_argument("--title", "--summary", required=True); x.add_argument("--calendar")
    x.add_argument("--due"); x.add_argument("--priority", type=int); x.add_argument("--description")
    x.add_argument("--categories")
    x = t.add_parser("edit"); x.add_argument("--uid", required=True); x.add_argument("--calendar")
    x.add_argument("--title", "--summary"); x.add_argument("--due", help="'' removes the due date")
    x.add_argument("--priority", type=int); x.add_argument("--description"); x.add_argument("--categories")
    x.add_argument("--etag")
    for name in ("complete", "reopen", "delete"):
        x = t.add_parser(name); x.add_argument("--uid", required=True); x.add_argument("--calendar")

    # events
    e = sub.add_parser("calendar", help="Events (CalDAV VEVENT)").add_subparsers(dest="sub")
    x = e.add_parser("list"); x.add_argument("--calendar")
    x.add_argument("--from", dest="cal_from"); x.add_argument("--to", dest="cal_to")
    x = e.add_parser("create"); x.add_argument("--summary", required=True); x.add_argument("--start", required=True)
    x.add_argument("--end"); x.add_argument("--calendar"); x.add_argument("--location"); x.add_argument("--description")
    x = e.add_parser("edit"); x.add_argument("--uid", required=True); x.add_argument("--calendar")
    x.add_argument("--summary"); x.add_argument("--start"); x.add_argument("--end")
    x.add_argument("--location"); x.add_argument("--description"); x.add_argument("--etag")
    x = e.add_parser("delete"); x.add_argument("--uid", required=True); x.add_argument("--calendar")

    # contacts
    ct = sub.add_parser("contacts", help="Contacts (CardDAV)").add_subparsers(dest="sub")
    x = ct.add_parser("list"); x.add_argument("--addressbook")
    x = ct.add_parser("search"); x.add_argument("--query", required=True)
    x = ct.add_parser("get"); x.add_argument("--uid", required=True); x.add_argument("--addressbook")
    x.add_argument("--raw", action="store_true", help="Include the full vCard")
    x = ct.add_parser("create"); x.add_argument("--name", required=True); x.add_argument("--addressbook")
    for a in ("--email", "--phone", "--organization", "--title", "--note"):
        x.add_argument(a)
    x = ct.add_parser("edit"); x.add_argument("--uid", required=True); x.add_argument("--addressbook")
    x.add_argument("--name")
    for a in ("--email", "--phone", "--organization", "--title", "--note", "--etag"):
        x.add_argument(a)
    x = ct.add_parser("delete"); x.add_argument("--uid", required=True); x.add_argument("--addressbook")
    x = ct.add_parser("export"); x.add_argument("--uid", required=True); x.add_argument("--local", required=True)
    x.add_argument("--addressbook")

    ab = sub.add_parser("addressbooks", help="List address books").add_subparsers(dest="sub")
    ab.add_parser("list")
    return p


def dispatch(c, a):
    cmd, s = a.command, getattr(a, "sub", None)
    if cmd == "check":
        return check(c)
    if cmd == "files":
        if s == "list": return files_list(c, a.path)
        if s == "get": return files_get(c, a.path)
        if s == "download":
            _, _, body = c.request("GET", c.dav_path(a.path), ok=(200,))
            with open(os.path.expanduser(a.local), "wb") as fh:
                fh.write(body)
            return {"path": a.path, "local": a.local, "size": len(body), "downloaded": True}
        if s == "upload":
            return files_put(c, a.path, read_content(a.content, a.content_file), a.if_match, a.no_overwrite)
        if s == "append": return files_append(c, a.path, a.text)
        if s == "mkdir": return files_mkdir(c, a.path)
        if s == "delete": return files_delete(c, a.path)
        if s == "move": return files_move(c, a.src, a.dst, a.overwrite)
        if s == "search": return files_search(c, a.query)
    if cmd == "notes":
        if s == "list": return notes_list(c, a.category, a.with_content)
        if s == "find": return notes_find(c, a.query)
        if s == "get": return notes_get(c, a.id)
        if s == "create":
            content = read_content(a.content, a.content_file).decode("utf-8")
            return notes_create(c, a.title, content, a.category)
        if s == "edit":
            content = None
            if a.content is not None or a.content_file:
                content = read_content(a.content, a.content_file).decode("utf-8")
            return notes_edit(c, a.id, a.title, content, a.category, a.etag)
        if s == "append": return notes_append(c, a.id, a.text)
        if s == "delete": return notes_delete(c, a.id)
    if cmd == "calendars" and s == "list":
        return calendars_list(c, {"events": "VEVENT", "tasks": "VTODO"}.get(a.type))
    if cmd == "tasks":
        if s == "list": return tasks_list(c, a.calendar, a.open)
        if s == "create": return tasks_create(c, a.title, a.calendar, a.due, a.priority, a.description, a.categories)
        if s == "edit":
            return tasks_edit(c, a.uid, a.calendar, a.title, a.due, a.priority, a.description, a.categories, a.etag)
        if s == "complete": return tasks_complete(c, a.uid, a.calendar)
        if s == "reopen": return tasks_reopen(c, a.uid, a.calendar)
        if s == "delete": return item_delete(c, "VTODO", a.uid, a.calendar)
    if cmd == "calendar":
        if s == "list": return events_list(c, a.calendar, a.cal_from, a.cal_to)
        if s == "create":
            return events_create(c, a.summary, a.start, a.end, a.calendar, a.location, a.description)
        if s == "edit":
            return events_edit(c, a.uid, a.calendar, a.summary, a.start, a.end, a.location, a.description, a.etag)
        if s == "delete": return item_delete(c, "VEVENT", a.uid, a.calendar)
    if cmd == "contacts":
        if s == "list": return contacts_list(c, a.addressbook)
        if s == "search": return contacts_search(c, a.query)
        if s == "get": return contacts_get(c, a.uid, a.addressbook, a.raw)
        if s == "create":
            return contacts_create(c, a.name, a.addressbook, a.email, a.phone, a.organization, a.title, a.note)
        if s == "edit":
            return contacts_edit(c, a.uid, a.addressbook, a.name, a.email, a.phone, a.organization,
                                 a.title, a.note, a.etag)
        if s == "delete": return contacts_delete(c, a.uid, a.addressbook)
        if s == "export": return contacts_export(c, a.uid, a.local, a.addressbook)
    if cmd == "addressbooks" and s == "list":
        return addressbooks_list(c)
    raise NCError(f"Unknown or incomplete command. Run with --help.")


def main(argv=None):
    parser = build_parser()
    a = parser.parse_args(argv)
    if not a.command:
        parser.print_help()
        return 0
    try:
        c = Client(load_env(a.env_file))
        result = {"status": "success", "data": dispatch(c, a)}
        code = 0
    except NCError as e:
        result, code = {"status": "error", "message": str(e)}, 1
    except OSError as e:
        result, code = {"status": "error", "message": f"Local file error: {e}"}, 1
    print(json.dumps(result, ensure_ascii=False, indent=None))
    return code


if __name__ == "__main__":
    sys.exit(main())
