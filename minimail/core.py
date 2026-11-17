"""Mail operations shared by the CLI, the TUI and the watcher."""

from __future__ import annotations

import html
from html.parser import HTMLParser
import imaplib
import re
import shutil
import smtplib
import ssl
import subprocess
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesHeaderParser, BytesParser
from email.utils import formataddr, formatdate, getaddresses, make_msgid, parseaddr, parsedate_to_datetime

import json

from . import imap, store
from .config import Account, get_account, load_accounts

HEADER_ITEMS = "(UID FLAGS INTERNALDATE BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE MESSAGE-ID)])"


# ---------------------------------------------------------------- ids

def make_id(account: str, folder: str, uid: int) -> str:
    return f"{account}:{folder}:{uid}"


def parse_id(mid: str) -> tuple[str, str, int]:
    try:
        account, folder, uid = mid.rsplit(":", 2)
        if folder not in imap.FOLDER_KEYS:
            raise ValueError
        return account, folder, int(uid)
    except ValueError:
        raise SystemExit(f"Bad message id '{mid}'. Expected <account>:<inbox|sent|spam>:<uid>.")


def group_ids(ids) -> dict[tuple[str, str], list[int]]:
    groups: dict[tuple[str, str], list[int]] = defaultdict(list)
    for mid in ids:
        a, f, u = parse_id(mid)
        groups[(a, f)].append(u)
    return groups


# ---------------------------------------------------------------- helpers

def pick_accounts(names=None) -> list[Account]:
    if not names:
        accts = load_accounts()
        if not accts:
            raise SystemExit("No accounts configured. Run `minimail account add --help`.")
        return accts
    return [get_account(n) for n in names]


def parallel(fn, accounts):
    """Run fn(account) for each account in parallel. Returns (results, errors)."""
    results, errors = {}, {}
    with ThreadPoolExecutor(max_workers=max(1, min(16, len(accounts)))) as ex:
        futs = {ex.submit(fn, a): a for a in accounts}
        for fut, a in futs.items():
            try:
                results[a.name] = fut.result()
            except Exception as e:  # noqa: BLE001
                errors[a.name] = f"{type(e).__name__}: {e}"
    return results, errors


def _ts(internaldate: str | None, date_header: str | None) -> float:
    if internaldate:
        try:
            return datetime.strptime(internaldate, "%d-%b-%Y %H:%M:%S %z").timestamp()
        except ValueError:
            pass
    if date_header:
        try:
            return parsedate_to_datetime(date_header).timestamp()
        except Exception:
            pass
    return 0.0


def _summary(acct: Account, folder: str, row: dict) -> dict:
    h = BytesHeaderParser(policy=policy.default).parsebytes(row["body"] or b"")
    ts = _ts(row["internaldate"], h.get("date"))
    flags = [f.lower() for f in row["flags"]]
    return {
        "id": make_id(acct.name, folder, row["uid"]),
        "account": acct.name,
        "folder": folder,
        "uid": row["uid"],
        "from": str(h.get("from", "") or ""),
        "to": str(h.get("to", "") or ""),
        "subject": str(h.get("subject", "") or ""),
        "date": datetime.fromtimestamp(ts, timezone.utc).astimezone().isoformat(timespec="minutes") if ts else "",
        "ts": ts,
        "unread": "\\seen" not in flags,
        "flagged": "\\flagged" in flags,
        "message_id": str(h.get("message-id", "") or ""),
    }


def short_from(addr: str) -> str:
    name, email = parseaddr(addr)
    return name or email or addr


# ---------------------------------------------------------------- read

def ensure_fresh(accts, folders, max_age=90.0) -> dict:
    """Sync accounts whose cache for `folders` is older than max_age seconds. Returns errors."""
    db = store.connect()
    stale = []
    for a in accts:
        for f in folders:
            if time.time() - store.synced_accounts(db, f).get(a.name, 0) > max_age:
                stale.append(a)
                break
    if not stale:
        return {}
    _, errors = parallel(lambda a: store.sync_account(a, folders, bodies=0), stale)
    return errors


def list_messages(folder="inbox", accounts=None, unread_only=False, limit=30, search=None, live=False):
    """Latest messages across accounts, newest first. Returns (messages, errors).

    Reads the local cache (synced first if older than 90 s). live=True asks the servers directly."""
    accts = pick_accounts(accounts)
    if not live:
        folders = list(imap.FOLDER_KEYS) if (search and folder is None) else [folder]
        errors = ensure_fresh(accts, folders)
        db = store.connect()
        msgs = store.query(db, folder, [a.name for a in accts], unread_only, limit, search)
        return msgs, errors

    def one(acct: Account):
        with imap.session(acct) as m:
            imap.select(m, acct, folder, readonly=True)
            crit = ["UNSEEN"] if unread_only else ["ALL"]
            if search:
                crit = crit + ["TEXT", imap.quote(search)]
            uids = imap.search_uids(m, *crit)[-limit:]
            if not uids:
                return []
            return [_summary(acct, folder, r) for r in imap.fetch(m, uids, HEADER_ITEMS)]

    results, errors = parallel(one, accts)
    msgs = [x for rows in results.values() for x in rows]
    msgs.sort(key=lambda x: x["ts"], reverse=True)
    return msgs, errors


def unread_counts(accounts=None, live=False):
    accts = pick_accounts(accounts)
    if not live:
        errors = ensure_fresh(accts, list(imap.FOLDER_KEYS))
        c = store.counts(store.connect())
        empty = {"total": 0, "unread": 0}
        return {a.name: {k: c.get(a.name, {}).get(k, empty) for k in imap.FOLDER_KEYS} for a in accts}, errors

    def one(acct: Account):
        out = {}
        with imap.session(acct) as m:
            for key in imap.FOLDER_KEYS:
                try:
                    name = imap.resolve_folder(m, acct, key)
                    typ, data = m.status(imap.quote(name), "(MESSAGES UNSEEN)")
                    mt = re.search(rb"MESSAGES (\d+).*?UNSEEN (\d+)", data[0]) if typ == "OK" else None
                    out[key] = {"total": int(mt.group(1)), "unread": int(mt.group(2))} if mt else None
                except Exception as e:  # noqa: BLE001
                    out[key] = {"error": str(e)}
        return out

    return parallel(one, accts)


class _HTMLText(HTMLParser):
    """HTML to readable text. Links become [text](url) so they stay clickable."""

    def __init__(self):
        super().__init__()
        self.parts, self._skip = [], 0
        self._href, self._link_start = None, 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head"):
            self._skip += 1
        if tag in ("br", "p", "div", "tr", "li", "h1", "h2", "h3", "table"):
            self.parts.append("\n")
        if tag == "a":
            href = (dict(attrs).get("href") or "").strip()
            if href.lower().startswith(("http://", "https://", "mailto:")):
                self._href, self._link_start = href, len(self.parts)

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head") and self._skip:
            self._skip -= 1
        if tag == "a" and self._href:
            text = " ".join("".join(self.parts[self._link_start:]).split())
            if text and not self._skip:
                del self.parts[self._link_start:]
                href = self._href.replace(" ", "%20").replace(")", "%29")
                self.parts.append(text if text == href else f"[{text.replace(']', '')}]({href})")
            self._href = None

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(s: str) -> str:
    p = _HTMLText()
    p.feed(s)
    text = html.unescape("".join(p.parts))
    text = re.sub(r"[ \t\xa0]+", " ", text)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()


def parse_full(raw: bytes):
    """Return (text body, attachments, parsed message) from a raw RFC822 message."""
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    body = ""
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is not None:
        try:
            body = part.get_content()
        except Exception:  # noqa: BLE001
            body = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
        if part.get_content_type() == "text/html":
            body = html_to_text(body)
    attachments = []
    for p in msg.iter_attachments():
        try:
            size = len(p.get_payload(decode=True) or b"")
        except Exception:  # noqa: BLE001
            size = 0
        attachments.append({"filename": p.get_filename(), "type": p.get_content_type(), "size": size})
    return body.strip(), attachments, msg


MAX_HTML = 6_000_000
_SCRIPT_RE = re.compile(r"<script\b.*?</script\s*>", re.I | re.S)


def html_of(msg) -> str:
    """The HTML part, with inline (cid:) images embedded as data: URIs and scripts removed. '' if none."""
    if msg is None:
        return ""
    part = msg.get_body(preferencelist=("html",))
    if part is None:
        return ""
    try:
        doc = part.get_content()
    except Exception:  # noqa: BLE001
        doc = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
    if not isinstance(doc, str):
        return ""
    doc = _SCRIPT_RE.sub("", doc)
    if "cid:" in doc:
        import base64
        budget = MAX_HTML - len(doc)
        for p in msg.walk():
            cid = str(p.get("content-id", "") or "").strip().strip("<>")
            if not cid or p.get_content_maintype() != "image" or f"cid:{cid}" not in doc:
                continue
            data = p.get_payload(decode=True) or b""
            uri = f"data:{p.get_content_type()};base64,{base64.b64encode(data).decode()}"
            if len(uri) > budget:
                continue
            budget -= len(uri)
            doc = doc.replace(f"cid:{cid}", uri)
    return doc if len(doc) <= MAX_HTML else ""


def unsubscribe_info(header: str) -> dict:
    """Parse List-Unsubscribe: '<https://...>, <mailto:...>'."""
    urls = re.findall(r"<([^>]+)>", header or "")
    return {"https": next((u for u in urls if u.lower().startswith("https://")), None),
            "mailto": next((u for u in urls if u.lower().startswith("mailto:")), None)}


def unsubscribe(mid: str) -> dict:
    """One-click unsubscribe (RFC 8058) when offered, else send the mailto, else return the web page."""
    import urllib.parse
    import urllib.request
    account, folder, uid = parse_id(mid)
    acct = get_account(account)
    with imap.session(acct) as m:
        imap.select(m, acct, folder, readonly=True)
        rows = imap.fetch(m, [uid], "(UID BODY.PEEK[HEADER.FIELDS (LIST-UNSUBSCRIBE LIST-UNSUBSCRIBE-POST)])")
    if not rows:
        raise SystemExit(f"Message not found: {mid}")
    h = BytesHeaderParser(policy=policy.default).parsebytes(rows[0]["body"] or b"")
    info = unsubscribe_info(str(h.get("list-unsubscribe", "") or ""))
    one_click = "one-click" in str(h.get("list-unsubscribe-post", "") or "").lower()
    if info["https"] and one_click:
        req = urllib.request.Request(info["https"], data=b"List-Unsubscribe=One-Click", method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded",
                                              "User-Agent": "Minimail"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return {"method": "one-click", "ok": r.status < 400, "url": info["https"]}
        except Exception as e:  # noqa: BLE001
            return {"method": "browser", "ok": False, "url": info["https"], "error": str(e)}
    if info["mailto"]:
        parsed = urllib.parse.urlparse(info["mailto"])
        q = urllib.parse.parse_qs(parsed.query)
        to = urllib.parse.unquote(parsed.path)
        send(account, [to], (q.get("subject") or ["unsubscribe"])[0], (q.get("body") or ["unsubscribe"])[0])
        return {"method": "email", "ok": True, "to": to}
    if info["https"]:
        return {"method": "browser", "ok": True, "url": info["https"]}
    return {"method": None, "ok": False, "error": "This email has no unsubscribe link."}


def get_message(mid: str, mark_read=False, live=False) -> dict:
    account, folder, uid = parse_id(mid)
    acct = get_account(account)
    if not live:
        db = store.connect()
        r = store.get(db, account, folder, uid)
        if r is not None and r["body"] is not None and r["html"] is not None:
            info = store.row_to_summary(r)
            info.update({"cc": r["cc"] or "", "reply_to": r["reply_to"] or "", "references": r["refs"] or "",
                         "body": r["body"], "attachments": json.loads(r["attachments"] or "[]"),
                         "unsubscribe": bool(r["list_unsub"])})
            if mark_read and info["unread"]:
                set_seen([mid], True)
                info["unread"] = False
            return info
    with imap.session(acct) as m:
        imap.select(m, acct, folder, readonly=not mark_read)
        rows = imap.fetch(m, [uid], "(UID FLAGS INTERNALDATE BODY.PEEK[])")
        if not rows:
            raise SystemExit(f"Message not found: {mid}")
        row = rows[0]
        if mark_read:
            m.uid("STORE", str(uid), "+FLAGS.SILENT", "(\\Seen)")
    body, attachments, msg = parse_full(row["body"])
    info = _summary(acct, folder, {"uid": uid, "flags": row["flags"], "internaldate": row["internaldate"], "body": row["body"]})
    if mark_read:
        info["unread"] = False
    info.update({
        "cc": str(msg.get("cc", "") or ""),
        "reply_to": str(msg.get("reply-to", "") or ""),
        "references": str(msg.get("references", "") or ""),
        "body": body,
        "attachments": attachments,
        "unsubscribe": bool(msg.get("list-unsubscribe")),
    })
    try:
        db = store.connect()
        store.store_body(db, account, folder, uid, body, attachments, info["cc"], info["reply_to"], info["references"],
                         str(msg.get("list-unsubscribe", "") or ""), html_of(msg))
        if mark_read:
            store.set_unread(db, account, folder, [uid], False)
    except Exception:  # noqa: BLE001
        pass
    if mark_read:
        clear_notifications([mid])
    return info


# ---------------------------------------------------------------- write

def set_seen(ids, seen=True) -> int:
    n = 0
    for (account, folder), uids in group_ids(ids).items():
        acct = get_account(account)
        with imap.session(acct) as m:
            imap.select(m, acct, folder, readonly=False)
            for part in imap.chunks(uids):
                m.uid("STORE", imap.uidset(part), "+FLAGS.SILENT" if seen else "-FLAGS.SILENT", "(\\Seen)")
                n += len(part)
        store.set_unread(store.connect(), account, folder, uids, not seen)
    if seen:
        clear_notifications(ids)
    return n


def mark_all_read(folder="inbox", accounts=None):
    accts = pick_accounts(accounts)

    def one(acct: Account):
        with imap.session(acct) as m:
            imap.select(m, acct, folder, readonly=False)
            uids = imap.search_uids(m, "UNSEEN")
            for part in imap.chunks(uids):
                m.uid("STORE", imap.uidset(part), "+FLAGS.SILENT", "(\\Seen)")
        store.set_all_read(store.connect(), acct.name, folder)
        return len(uids)

    results, errors = parallel(one, accts)
    if folder == "inbox":
        clear_notifications(None)
    return results, errors


def move(ids, dest: str) -> int:
    """Move messages to inbox/spam (or sent). Uses MOVE when available."""
    n = 0
    for (account, folder), uids in group_ids(ids).items():
        if folder == dest:
            continue
        acct = get_account(account)
        with imap.session(acct) as m:
            target = imap.resolve_folder(m, acct, dest)
            imap.select(m, acct, folder, readonly=False)
            caps = {c.upper() for c in (m.capabilities or ())}
            for part in imap.chunks(uids):
                s = imap.uidset(part)
                if "MOVE" in caps:
                    typ, data = m.uid("MOVE", s, imap.quote(target))
                else:
                    typ, data = m.uid("COPY", s, imap.quote(target))
                    if typ == "OK":
                        m.uid("STORE", s, "+FLAGS.SILENT", "(\\Deleted)")
                        if "UIDPLUS" in caps:
                            m.uid("EXPUNGE", s)
                        else:
                            m.expunge()
                if typ != "OK":
                    raise RuntimeError(f"[{account}] move failed: {data}")
                n += len(part)
        store.remove(store.connect(), account, folder, uids)
    clear_notifications(ids)
    return n


def delete(ids) -> int:
    """Move messages to the account's Trash. Without a Trash folder, delete them for good."""
    n = 0
    for (account, folder), uids in group_ids(ids).items():
        acct = get_account(account)
        with imap.session(acct) as m:
            try:
                trash = imap.resolve_folder(m, acct, "trash")
            except RuntimeError:
                trash = None
        if trash:
            n += move([make_id(account, folder, u) for u in uids], "trash")
            continue
        with imap.session(acct) as m:
            imap.select(m, acct, folder, readonly=False)
            caps = {c.upper() for c in (m.capabilities or ())}
            for part in imap.chunks(uids):
                s = imap.uidset(part)
                m.uid("STORE", s, "+FLAGS.SILENT", "(\\Deleted)")
                if "UIDPLUS" in caps:
                    m.uid("EXPUNGE", s)
                else:
                    m.expunge()
                n += len(part)
        store.remove(store.connect(), account, folder, uids)
    clear_notifications(ids)
    return n


def send(account: str, to, subject: str, body: str, cc=(), bcc=(), reply_to_id: str | None = None) -> dict:
    acct = get_account(account)
    msg = EmailMessage()
    msg["From"] = formataddr((acct.display_name, acct.email)) if acct.display_name else acct.email
    to = list(to)
    if reply_to_id:
        orig = get_message(reply_to_id)
        if not to:
            to = [orig["reply_to"] or orig["from"]]
        if not subject:
            s = orig["subject"]
            subject = s if s.lower().startswith("re:") else f"Re: {s}"
        if orig["message_id"]:
            msg["In-Reply-To"] = orig["message_id"]
            msg["References"] = (orig["references"] + " " + orig["message_id"]).strip()
        quoted = "\n".join("> " + line for line in orig["body"].splitlines())
        body = f"{body}\n\nOn {orig['date']}, {orig['from']} wrote:\n{quoted}\n"
    if not to:
        raise SystemExit("No recipient.")
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=acct.email.split("@")[-1])
    msg["Date"] = formatdate(localtime=True)
    msg.set_content(body)

    ctx = ssl.create_default_context()
    if acct.insecure_tls:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    if acct.smtp_security == "ssl":
        s = smtplib.SMTP_SSL(acct.smtp_host, acct.smtp_port, context=ctx, timeout=30)
    else:
        s = smtplib.SMTP(acct.smtp_host, acct.smtp_port, timeout=30)
        s.starttls(context=ctx)
    with s:
        s.login(acct.username, acct.password())
        rcpts = [e for _, e in getaddresses(to + list(cc) + list(bcc))]
        s.send_message(msg, to_addrs=rcpts)

    if not acct.smtp_saves_sent:
        with imap.session(acct) as m:
            sent = imap.resolve_folder(m, acct, "sent")
            m.append(imap.quote(sent), "(\\Seen)", imaplib.Time2Internaldate(time.time()), msg.as_bytes())
    if reply_to_id:
        try:
            a, f, u = parse_id(reply_to_id)
            with imap.session(get_account(a)) as m:
                imap.select(m, get_account(a), f, readonly=False)
                m.uid("STORE", str(u), "+FLAGS.SILENT", "(\\Answered \\Seen)")
        except Exception:  # noqa: BLE001
            pass
    return {"account": account, "to": to, "cc": list(cc), "subject": subject, "message_id": msg["Message-ID"]}


# ---------------------------------------------------------------- notifications

def notifier() -> str | None:
    return shutil.which("terminal-notifier") or next(
        (p for p in ("/opt/homebrew/bin/terminal-notifier", "/usr/local/bin/terminal-notifier") if shutil.which(p)), None
    )


def notification_group(mid: str) -> str:
    return "minimail-" + mid


def clear_notifications(ids) -> None:
    """Remove delivered notifications (ids=None removes all of ours)."""
    tn = notifier()
    if not tn:
        return
    groups = ["ALL"] if ids is None else [notification_group(i) for i in ids]
    # Fire and forget: `terminal-notifier -remove` can hang on recent macOS.
    for g in groups[:50]:
        try:
            p = subprocess.Popen([tn, "-remove", g], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
            try:
                p.wait(timeout=2)
            except subprocess.TimeoutExpired:
                p.kill()
        except OSError:
            pass
