"""Thin IMAP helpers on top of imaplib."""

from __future__ import annotations

import base64
import imaplib
import json
import re
import ssl
from contextlib import contextmanager

from .config import STATE_DIR, Account

FOLDER_KEYS = ("inbox", "sent", "spam")

_FALLBACKS = {
    "sent": ["Sent", "Sent Messages", "Sent Items", "Sent Mail", "INBOX.Sent", "[Gmail]/Sent Mail",
             "Envoy&AOk-s", "&AMk-l&AOk-ments envoy&AOk-s", "Messages envoy&AOk-s"],
    "spam": ["Junk", "Spam", "Bulk", "Junk E-mail", "Junk Email", "INBOX.Junk", "INBOX.spam", "INBOX.Spam",
             "[Gmail]/Spam", "Courrier ind&AOk-sirable", "Ind&AOk-sirables", "Pourriel"],
    "trash": ["Trash", "Deleted Messages", "Deleted Items", "Deleted", "Bin", "INBOX.Trash", "[Gmail]/Trash",
              "[Gmail]/Corbeille", "Corbeille", "&AMk-l&AOk-ments supprim&AOk-s", "Messages supprim&AOk-s"],
}
_SPECIAL_USE = {"sent": b"\\sent", "spam": b"\\junk", "trash": b"\\trash"}
_LIST_RE = re.compile(rb'\((?P<flags>[^)]*)\) (?P<delim>"(?:[^"\\]|\\.)*"|NIL) (?P<name>.+)$')
_FOLDER_CACHE = STATE_DIR / "folders.json"


def _ssl_context(acct: Account) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if acct.insecure_tls:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def connect(acct: Account) -> imaplib.IMAP4:
    ctx = _ssl_context(acct)
    if acct.imap_security == "ssl":
        m = imaplib.IMAP4_SSL(acct.imap_host, acct.imap_port, ssl_context=ctx, timeout=30)
    else:
        m = imaplib.IMAP4(acct.imap_host, acct.imap_port, timeout=30)
        m.starttls(ssl_context=ctx)
    m.login(acct.username, acct.password())
    return m


@contextmanager
def session(acct: Account):
    m = connect(acct)
    try:
        yield m
    finally:
        try:
            m.logout()
        except Exception:
            pass


def quote(name: str) -> str:
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def utf7_decode(s: str) -> str:
    """Decode IMAP modified UTF-7 folder names (for display)."""
    out, i = [], 0
    while i < len(s):
        if s[i] == "&":
            j = s.index("-", i)
            if j == i + 1:
                out.append("&")
            else:
                b = s[i + 1 : j].replace(",", "/")
                b += "=" * (-len(b) % 4)
                out.append(base64.b64decode(b).decode("utf-16-be"))
            i = j + 1
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


def list_folders(m: imaplib.IMAP4) -> list[tuple[bytes, str]]:
    typ, data = m.list()
    folders = []
    for line in data or []:
        if isinstance(line, tuple):  # literal-encoded name
            folders.append((b"", line[1].decode("utf-8", "replace")))
            continue
        if not line:
            continue
        mt = _LIST_RE.match(line)
        if not mt:
            continue
        name = mt.group("name").decode("utf-8", "replace")
        if name.startswith('"') and name.endswith('"'):
            name = name[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        folders.append((mt.group("flags").lower(), name))
    return folders


def _load_cache() -> dict:
    try:
        return json.loads(_FOLDER_CACHE.read_text())
    except Exception:
        return {}


def resolve_folder(m: imaplib.IMAP4, acct: Account, key: str) -> str:
    """Map 'inbox' | 'sent' | 'spam' to the server's real folder name."""
    if key == "inbox":
        return "INBOX"
    if key in acct.folders:
        return acct.folders[key]
    cache = _load_cache()
    if key in cache.get(acct.name, {}):
        return cache[acct.name][key]
    folders = list_folders(m)
    found = None
    for flags, name in folders:
        if _SPECIAL_USE[key] in flags:
            found = name
            break
    if not found:
        found = _guess(m, folders, key)
    if not found:
        raise RuntimeError(f"[{acct.name}] cannot find the {key} folder; set it with `minimail account folder {acct.name} {key} <name>`")
    cache.setdefault(acct.name, {})[key] = found
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    _FOLDER_CACHE.write_text(json.dumps(cache, indent=2))
    return found


def _guess(m: imaplib.IMAP4, folders, key: str) -> str | None:
    """No SPECIAL-USE flag: match common names, also under a namespace prefix like 'INBOX.'.
    If several folders match, take the one with the most messages."""
    wanted = [c.lower() for c in _FALLBACKS[key]]
    exact = {n.lower(): n for _, n in folders}
    for cand in wanted:
        if cand in exact:
            return exact[cand]
    matches = [n for _, n in folders if re.split(r"[./]", n)[-1].lower() in wanted]
    if len(matches) <= 1:
        return matches[0] if matches else None
    def count(name):
        try:
            typ, data = m.status(quote(name), "(MESSAGES)")
            mt = re.search(rb"MESSAGES (\d+)", data[0]) if typ == "OK" else None
            return int(mt.group(1)) if mt else -1
        except Exception:  # noqa: BLE001
            return -1
    return max(matches, key=lambda n: (count(n), -n.count(".")))


def forget_folders(acct_name: str) -> None:
    cache = _load_cache()
    if cache.pop(acct_name, None) is not None:
        _FOLDER_CACHE.write_text(json.dumps(cache, indent=2))


def select(m: imaplib.IMAP4, acct: Account, key: str, readonly: bool = True) -> str:
    name = resolve_folder(m, acct, key)
    typ, data = m.select(quote(name), readonly=readonly)
    if typ != "OK":
        raise RuntimeError(f"[{acct.name}] cannot open folder {name}: {data}")
    return name


def search_uids(m: imaplib.IMAP4, *criteria: str) -> list[int]:
    typ, data = m.uid("SEARCH", None, *criteria)
    if typ != "OK" or not data or not data[0]:
        return []
    return [int(x) for x in data[0].split()]


def chunks(seq, n=400):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


def uidset(uids) -> str:
    return ",".join(str(u) for u in uids)


_UID_RE = re.compile(rb"UID (\d+)")
_FLAGS_RE = re.compile(rb"FLAGS \(([^)]*)\)")
_DATE_RE = re.compile(rb'INTERNALDATE "([^"]+)"')
_SIZE_RE = re.compile(rb"RFC822\.SIZE (\d+)")


def fetch(m: imaplib.IMAP4, uids, items: str) -> list[dict]:
    """Fetch items for uids. Returns [{uid, flags, internaldate, body}]."""
    out = []
    for part in chunks(uids):
        typ, data = m.uid("FETCH", uidset(part), items)
        if typ != "OK":
            continue
        i = 0
        while i < len(data):
            item = data[i]
            if isinstance(item, tuple):
                meta, body = item[0], item[1]
                # Some servers send trailing attributes after the literal.
                if i + 1 < len(data) and isinstance(data[i + 1], bytes):
                    meta += data[i + 1]
                    i += 1
            elif isinstance(item, bytes) and item.strip() not in (b"", b")"):
                meta, body = item, b""
            else:
                i += 1
                continue
            mu = _UID_RE.search(meta)
            if mu:
                mf = _FLAGS_RE.search(meta)
                md = _DATE_RE.search(meta)
                ms = _SIZE_RE.search(meta)
                out.append({
                    "size": int(ms.group(1)) if ms else None,
                    "uid": int(mu.group(1)),
                    "flags": mf.group(1).decode().split() if mf else [],
                    "internaldate": md.group(1).decode() if md else None,
                    "body": body,
                })
            i += 1
    return out
