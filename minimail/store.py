"""Local SQLite cache: headers, flags, text bodies and contacts.

The watcher keeps it in sync with the servers, so the app and the CLI read
everything locally and instantly. Full-text search uses FTS5.
"""

from __future__ import annotations

import json
import sqlite3
import time
from email import policy
from email.parser import BytesHeaderParser
from email.utils import getaddresses

from . import imap
from .config import STATE_DIR, Account

DB_PATH = STATE_DIR / "mail.db"
SYNC_FOLDERS = ("inbox", "sent", "spam")
MAX_HEADERS = 20000      # per folder, newest first
BODIES_PER_PASS = 40     # text bodies fetched per folder per sync
BODY_WINDOW = 400        # only prefetch bodies of the newest N messages per folder (older ones load on open)
MAX_BODY_SIZE = 3_000_000

HEADER_ITEMS = ("(UID FLAGS INTERNALDATE RFC822.SIZE BODY.PEEK[HEADER.FIELDS "
                "(FROM TO CC SUBJECT DATE MESSAGE-ID REPLY-TO REFERENCES LIST-UNSUBSCRIBE)])")
SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY,
  account TEXT NOT NULL, folder TEXT NOT NULL, uid INTEGER NOT NULL,
  message_id TEXT, sender TEXT, recipients TEXT, cc TEXT, reply_to TEXT, refs TEXT,
  subject TEXT, ts REAL, unread INTEGER NOT NULL DEFAULT 0, flagged INTEGER NOT NULL DEFAULT 0,
  size INTEGER, body TEXT, attachments TEXT, list_unsub TEXT,
  UNIQUE(account, folder, uid));
CREATE INDEX IF NOT EXISTS messages_folder_ts ON messages(folder, ts DESC);
CREATE INDEX IF NOT EXISTS messages_account_folder_ts ON messages(account, folder, ts DESC);
CREATE TABLE IF NOT EXISTS folders(
  account TEXT NOT NULL, folder TEXT NOT NULL, uidvalidity TEXT, synced REAL,
  PRIMARY KEY(account, folder));
CREATE TABLE IF NOT EXISTS contacts(
  email TEXT PRIMARY KEY, name TEXT, received INTEGER NOT NULL DEFAULT 0,
  sent INTEGER NOT NULL DEFAULT 0, last REAL NOT NULL DEFAULT 0);
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
  subject, sender, recipients, body, content='messages', content_rowid='id',
  tokenize='unicode61 remove_diacritics 2');
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
  INSERT INTO messages_fts(rowid, subject, sender, recipients, body)
  VALUES (new.id, new.subject, new.sender, new.recipients, new.body);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, subject, sender, recipients, body)
  VALUES ('delete', old.id, old.subject, old.sender, old.recipients, old.body);
END;
CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE OF subject, sender, recipients, body ON messages BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, subject, sender, recipients, body)
  VALUES ('delete', old.id, old.subject, old.sender, old.recipients, old.body);
  INSERT INTO messages_fts(rowid, subject, sender, recipients, body)
  VALUES (new.id, new.subject, new.sender, new.recipients, new.body);
END;
"""

_initialized = False


def connect() -> sqlite3.Connection:
    global _initialized
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=30000")
    if not _initialized:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(SCHEMA)
        _migrate(db)
        _initialized = True
    return db


def _migrate(db) -> None:
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version >= SCHEMA_VERSION:
        return
    cols = {r[1] for r in db.execute("PRAGMA table_info(messages)")}
    if "list_unsub" not in cols:
        db.execute("ALTER TABLE messages ADD COLUMN list_unsub TEXT")
    if version < 2:
        # v2 keeps links in bodies: parse them again (prefetch refills them progressively).
        db.execute("UPDATE messages SET body=NULL, attachments=NULL")
    db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    db.commit()


# ---------------------------------------------------------------- reads

def row_to_summary(r: sqlite3.Row) -> dict:
    from .core import make_id
    from datetime import datetime, timezone
    ts = r["ts"] or 0
    return {
        "id": make_id(r["account"], r["folder"], r["uid"]),
        "account": r["account"], "folder": r["folder"], "uid": r["uid"],
        "from": r["sender"] or "", "to": r["recipients"] or "", "subject": r["subject"] or "",
        "date": datetime.fromtimestamp(ts, timezone.utc).astimezone().isoformat(timespec="minutes") if ts else "",
        "ts": ts, "unread": bool(r["unread"]), "flagged": bool(r["flagged"]),
        "message_id": r["message_id"] or "",
    }


def synced_accounts(db, folder: str) -> dict[str, float]:
    return {r["account"]: r["synced"] or 0 for r in db.execute("SELECT account, synced FROM folders WHERE folder=?", (folder,))}


def fts_query(text: str) -> str:
    """Turn user text into a safe FTS5 query: every word must match (prefix match)."""
    words = [w.replace('"', "") for w in text.split() if w.replace('"', "")]
    return " AND ".join(f'"{w}"*' for w in words)


def query(db, folder: str | None, accounts: list[str] | None, unread_only=False, limit=50,
          search: str | None = None, per_account=True) -> list[dict]:
    where, args = [], []
    if folder:
        where.append("m.folder=?"); args.append(folder)
    if accounts:
        where.append(f"m.account IN ({','.join('?' * len(accounts))})"); args += accounts
    if unread_only:
        where.append("m.unread=1")
    sql = "SELECT m.* FROM messages m"
    if search and fts_query(search):
        sql += " JOIN messages_fts f ON f.rowid = m.id"
        where.append("messages_fts MATCH ?"); args.append(fts_query(search))
    if where:
        sql += " WHERE " + " AND ".join(where)
    if per_account and not search:
        # `limit` newest messages per account, like the server version did
        sql = (f"SELECT * FROM (SELECT x.*, ROW_NUMBER() OVER (PARTITION BY account ORDER BY ts DESC) AS rn "
               f"FROM ({sql}) x) WHERE rn <= ? ORDER BY ts DESC")
        args.append(limit)
    else:
        sql += " ORDER BY m.ts DESC LIMIT ?"
        args.append(limit)
    return [row_to_summary(r) for r in db.execute(sql, args)]


def get(db, account: str, folder: str, uid: int):
    return db.execute("SELECT * FROM messages WHERE account=? AND folder=? AND uid=?", (account, folder, uid)).fetchone()


def counts(db) -> dict:
    out: dict = {}
    for r in db.execute("SELECT account, folder, COUNT(*) AS total, SUM(unread) AS unread FROM messages GROUP BY account, folder"):
        out.setdefault(r["account"], {})[r["folder"]] = {"total": r["total"], "unread": r["unread"] or 0}
    return out


def contacts(db, prefix: str = "", limit: int = 10) -> list[dict]:
    p = prefix.strip().lower()
    rows = db.execute(
        "SELECT email, name, received, sent, last FROM contacts "
        "WHERE (? = '' OR email LIKE ? OR lower(name) LIKE ? OR lower(name) LIKE ?) "
        "ORDER BY sent DESC, received DESC, last DESC LIMIT ?",
        (p, p + "%", p + "%", "% " + p + "%", limit))
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- writes (used by actions)

def set_unread(db, account: str, folder: str, uids, unread: bool) -> None:
    db.executemany("UPDATE messages SET unread=? WHERE account=? AND folder=? AND uid=?",
                   [(int(unread), account, folder, u) for u in uids])
    db.commit()


def set_all_read(db, account: str, folder: str) -> None:
    db.execute("UPDATE messages SET unread=0 WHERE account=? AND folder=? AND unread=1", (account, folder))
    db.commit()


def remove(db, account: str, folder: str, uids) -> None:
    db.executemany("DELETE FROM messages WHERE account=? AND folder=? AND uid=?", [(account, folder, u) for u in uids])
    db.commit()


def store_body(db, account: str, folder: str, uid: int, body: str, attachments: list, cc="", reply_to="", refs="",
               list_unsub="") -> None:
    db.execute("UPDATE messages SET body=?, attachments=?, cc=COALESCE(NULLIF(?, ''), cc), "
               "reply_to=COALESCE(NULLIF(?, ''), reply_to), refs=COALESCE(NULLIF(?, ''), refs), "
               "list_unsub=COALESCE(NULLIF(?, ''), list_unsub) WHERE account=? AND folder=? AND uid=?",
               (body, json.dumps(attachments), cc, reply_to, refs, list_unsub, account, folder, uid))
    db.commit()


def forget_account(name: str) -> None:
    db = connect()
    db.execute("DELETE FROM messages WHERE account=?", (name,))
    db.execute("DELETE FROM folders WHERE account=?", (name,))
    db.commit()


# ---------------------------------------------------------------- sync

def _header_row(acct: Account, key: str, r: dict) -> tuple:
    from .core import _ts
    h = BytesHeaderParser(policy=policy.default).parsebytes(r["body"] or b"")
    flags = [f.lower() for f in r["flags"]]
    get = lambda k: str(h.get(k, "") or "")  # noqa: E731
    return (acct.name, key, r["uid"], get("message-id"), get("from"), get("to"), get("cc"), get("reply-to"),
            get("references"), get("subject"), _ts(r["internaldate"], h.get("date")),
            int("\\seen" not in flags), int("\\flagged" in flags), r.get("size"), get("list-unsubscribe"))


def _update_contacts(db, rows: list[tuple]) -> None:
    for row in rows:
        folder, sender, to, cc, ts = row[1], row[4], row[5], row[6], row[10] or 0
        if folder == "sent":
            people, col = getaddresses([to, cc]), "sent"
        else:
            people, col = getaddresses([sender]), "received"
        for name, email in people:
            email = email.strip().lower()
            if "@" not in email or email.startswith(("noreply", "no-reply", "donotreply", "do-not-reply")):
                continue
            db.execute(f"INSERT INTO contacts(email, name, {col}, last) VALUES (?, ?, 1, ?) "
                       f"ON CONFLICT(email) DO UPDATE SET {col}={col}+1, last=MAX(last, excluded.last), "
                       f"name=CASE WHEN excluded.name != '' THEN excluded.name ELSE name END",
                       (email, name.strip(), ts))


def sync_folder(db, m, acct: Account, key: str, bodies: int = BODIES_PER_PASS) -> list[dict]:
    """Bring one folder of the cache up to date. Returns new messages (not on the first sync)."""
    from .core import parse_full

    imap.select(m, acct, key, readonly=True)
    typ, data = m.response("UIDVALIDITY")
    uidvalidity = (data[-1].decode() if data and isinstance(data[-1], bytes) else str(data[-1])) if data and data[-1] else "0"
    row = db.execute("SELECT uidvalidity FROM folders WHERE account=? AND folder=?", (acct.name, key)).fetchone()
    first = row is None
    if row and row["uidvalidity"] != uidvalidity:
        db.execute("DELETE FROM messages WHERE account=? AND folder=?", (acct.name, key))
        first = True

    server = set(imap.search_uids(m, "ALL"))
    cached = {r["uid"]: (r["unread"], r["flagged"]) for r in
              db.execute("SELECT uid, unread, flagged FROM messages WHERE account=? AND folder=?", (acct.name, key))}

    gone = [u for u in cached if u not in server]
    if gone:
        remove(db, acct.name, key, gone)

    new = sorted(server - cached.keys(), reverse=True)[:MAX_HEADERS]
    inserted: list[dict] = []
    for part in imap.chunks(new, 500):
        rows = [_header_row(acct, key, r) for r in imap.fetch(m, part, HEADER_ITEMS)]
        db.executemany(
            "INSERT OR IGNORE INTO messages(account, folder, uid, message_id, sender, recipients, cc, reply_to, refs, "
            "subject, ts, unread, flagged, size, list_unsub) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        _update_contacts(db, rows)
        db.commit()
        if not first:
            inserted += [{"uid": r[2], "unread": bool(r[11])} for r in rows]

    # Flags: two cheap searches instead of fetching every message's flags.
    if cached:
        unseen = set(imap.search_uids(m, "UNSEEN"))
        flagged = set(imap.search_uids(m, "FLAGGED"))
        changes = [(int(u in unseen), int(u in flagged), acct.name, key, u)
                   for u, (ur, fl) in cached.items() if u in server and (ur != int(u in unseen) or fl != int(u in flagged))]
        if changes:
            db.executemany("UPDATE messages SET unread=?, flagged=? WHERE account=? AND folder=? AND uid=?", changes)

    # Text bodies, newest first, so opening and searching recent mail is instant.
    if bodies:
        missing = [r["uid"] for r in db.execute(
            "SELECT uid FROM (SELECT uid, body, size FROM messages WHERE account=? AND folder=? ORDER BY ts DESC LIMIT ?) "
            "WHERE body IS NULL AND COALESCE(size, 0) < ? LIMIT ?",
            (acct.name, key, BODY_WINDOW, MAX_BODY_SIZE, bodies))]
        for part in imap.chunks(missing, 20):
            for r in imap.fetch(m, part, "(UID BODY.PEEK[])"):
                try:
                    body, atts, msg = parse_full(r["body"])
                except Exception:  # noqa: BLE001
                    body, atts, msg = "", [], None
                unsub = str(msg.get("list-unsubscribe", "") or "") if msg is not None else ""
                db.execute("UPDATE messages SET body=?, attachments=?, list_unsub=COALESCE(NULLIF(?, ''), list_unsub) "
                           "WHERE account=? AND folder=? AND uid=?",
                           (body, json.dumps(atts), unsub, acct.name, key, r["uid"]))
            db.commit()

    db.execute("INSERT INTO folders(account, folder, uidvalidity, synced) VALUES (?,?,?,?) "
               "ON CONFLICT(account, folder) DO UPDATE SET uidvalidity=excluded.uidvalidity, synced=excluded.synced",
               (acct.name, key, uidvalidity, time.time()))
    db.commit()
    if not inserted:
        return []
    uids = [x["uid"] for x in inserted]
    return [row_to_summary(r) | {"body": r["body"] or ""} for r in db.execute(
        f"SELECT * FROM messages WHERE account=? AND folder=? AND uid IN ({','.join('?' * len(uids))})",
        [acct.name, key, *uids])]


def sync_account(acct: Account, folders=SYNC_FOLDERS, m=None, bodies: int = BODIES_PER_PASS) -> list[dict]:
    db = connect()
    own = m is None
    if own:
        m = imap.connect(acct)
    try:
        new = []
        for key in folders:
            new += sync_folder(db, m, acct, key, bodies=bodies)
        return new
    finally:
        db.close()
        if own:
            try:
                m.logout()
            except Exception:  # noqa: BLE001
                pass
