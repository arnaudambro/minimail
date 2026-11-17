"""Minimal curses interface: Inbox / Sent / Spam, select all, mark read."""

from __future__ import annotations

import curses
import textwrap
from datetime import datetime

from . import core, imap
from .config import load_accounts

HELP = "↑↓ move  space select  a all  r read  u unread  s spam/not spam  enter open  tab folder  c account  f unread-only  g refresh  q quit"


class App:
    def __init__(self, scr):
        self.scr = scr
        self.folder_idx = 0
        self.accounts = [a.name for a in load_accounts()]
        self.acct_idx = -1  # -1 = all
        self.unread_only = False
        self.items: list[dict] = []
        self.cursor = 0
        self.top = 0
        self.selected: set[str] = set()
        self.status = ""

    @property
    def folder(self):
        return imap.FOLDER_KEYS[self.folder_idx]

    @property
    def account(self):
        return None if self.acct_idx < 0 else self.accounts[self.acct_idx]

    # -------------------------------------------------- drawing
    def put(self, y, x, text, attr=0):
        h, w = self.scr.getmaxyx()
        if 0 <= y < h and x < w:
            try:
                self.scr.addnstr(y, x, text, max(0, w - x - 1), attr)
            except curses.error:
                pass

    def flash(self, msg):
        self.status = msg
        self.draw()
        self.scr.refresh()

    def draw(self):
        self.scr.erase()
        h, w = self.scr.getmaxyx()
        x = 1
        self.put(0, x, "Minimail", curses.A_BOLD)
        x += 10
        for i, f in enumerate(imap.FOLDER_KEYS):
            label = f" {f.capitalize()} "
            self.put(0, x, label, curses.A_REVERSE if i == self.folder_idx else 0)
            x += len(label) + 1
        unread = sum(1 for m in self.items if m["unread"])
        info = f"  account: {self.account or 'all'}   {'unread only  ' if self.unread_only else ''}{unread} unread"
        if self.selected:
            info += f"   {len(self.selected)} selected"
        self.put(0, x, info, curses.A_DIM)
        self.put(1, 0, "─" * (w - 1), curses.A_DIM)

        rows = h - 4
        if self.cursor < self.top:
            self.top = self.cursor
        elif self.cursor >= self.top + rows:
            self.top = self.cursor - rows + 1
        show_acct = self.account is None and len(self.accounts) > 1
        for i, m in enumerate(self.items[self.top : self.top + rows]):
            idx = self.top + i
            sel = "■" if m["id"] in self.selected else " "
            dot = "●" if m["unread"] else " "
            d = datetime.fromtimestamp(m["ts"]).strftime("%d/%m %H:%M") if m["ts"] else "           "
            who = core.short_from(m["to"] if self.folder == "sent" else m["from"])[:22]
            acct = f"{m['account'][:12]:<12} " if show_acct else ""
            line = f"{sel} {dot} {d}  {acct}{who:<22}  {m['subject']}"
            attr = curses.A_BOLD if m["unread"] else 0
            if idx == self.cursor:
                attr |= curses.A_REVERSE
            self.put(2 + i, 0, line.ljust(w - 1), attr)
        if not self.items:
            self.put(3, 2, "Nothing here.", curses.A_DIM)
        self.put(h - 2, 0, "─" * (w - 1), curses.A_DIM)
        self.put(h - 1, 1, self.status or HELP, curses.A_DIM)

    # -------------------------------------------------- data
    def load(self):
        self.flash("Loading…")
        accts = [self.account] if self.account else None
        self.items, errors = core.list_messages(self.folder, accts, self.unread_only, limit=50)
        self.items = self.items[:500]
        self.selected &= {m["id"] for m in self.items}
        self.cursor = min(self.cursor, max(0, len(self.items) - 1))
        self.status = ("Errors: " + "; ".join(f"{k}: {v}" for k, v in errors.items())) if errors else ""

    def targets(self) -> list[dict]:
        if self.selected:
            return [m for m in self.items if m["id"] in self.selected]
        return [self.items[self.cursor]] if self.items else []

    def set_seen(self, seen: bool):
        t = [m for m in self.targets() if m["unread"] == seen]
        if not t:
            return
        self.flash("Updating…")
        core.set_seen([m["id"] for m in t], seen)
        for m in t:
            m["unread"] = not seen
        self.selected.clear()
        self.status = f"Marked {len(t)} as {'read' if seen else 'unread'}."

    def spam_toggle(self):
        t = self.targets()
        if not t or self.folder == "sent":
            return
        dest = "inbox" if self.folder == "spam" else "spam"
        self.flash("Moving…")
        core.move([m["id"] for m in t], dest)
        ids = {m["id"] for m in t}
        self.items = [m for m in self.items if m["id"] not in ids]
        self.selected.clear()
        self.cursor = min(self.cursor, max(0, len(self.items) - 1))
        self.status = f"Moved {len(t)} to {dest}."

    def open(self):
        if not self.items:
            return
        m = self.items[self.cursor]
        self.flash("Opening…")
        full = core.get_message(m["id"], mark_read=True)
        m["unread"] = False
        self.status = ""
        Pager(self.scr, full).run()

    # -------------------------------------------------- loop
    def run(self):
        curses.curs_set(0)
        self.load()
        while True:
            self.draw()
            k = self.scr.get_wch()
            self.status = self.status if k in (curses.KEY_RESIZE,) else ""
            try:
                if k in ("q", "\x1b"):
                    return
                elif k in (curses.KEY_DOWN, "j"):
                    self.cursor = min(self.cursor + 1, max(0, len(self.items) - 1))
                elif k in (curses.KEY_UP, "k"):
                    self.cursor = max(self.cursor - 1, 0)
                elif k == curses.KEY_NPAGE:
                    self.cursor = min(self.cursor + 20, max(0, len(self.items) - 1))
                elif k == curses.KEY_PPAGE:
                    self.cursor = max(self.cursor - 20, 0)
                elif k == " " and self.items:
                    mid = self.items[self.cursor]["id"]
                    self.selected ^= {mid}
                    self.cursor = min(self.cursor + 1, len(self.items) - 1)
                elif k == "a":
                    ids = {m["id"] for m in self.items}
                    self.selected = set() if self.selected == ids else ids
                elif k == "r":
                    self.set_seen(True)
                elif k == "u":
                    self.set_seen(False)
                elif k == "s":
                    self.spam_toggle()
                elif k in ("\n", curses.KEY_ENTER):
                    self.open()
                elif k in ("\t", curses.KEY_RIGHT, curses.KEY_LEFT, "1", "2", "3"):
                    if k in ("1", "2", "3"):
                        self.folder_idx = int(k) - 1
                    else:
                        step = -1 if k == curses.KEY_LEFT else 1
                        self.folder_idx = (self.folder_idx + step) % len(imap.FOLDER_KEYS)
                    self.selected.clear()
                    self.cursor = 0
                    self.load()
                elif k == "c" and self.accounts:
                    self.acct_idx = self.acct_idx + 1 if self.acct_idx + 1 < len(self.accounts) else -1
                    self.selected.clear()
                    self.cursor = 0
                    self.load()
                elif k == "f":
                    self.unread_only = not self.unread_only
                    self.cursor = 0
                    self.load()
                elif k == "g":
                    self.load()
            except Exception as e:  # noqa: BLE001
                self.status = f"Error: {e}"


class Pager:
    def __init__(self, scr, msg):
        self.scr, self.msg, self.top = scr, msg, 0

    def lines(self, w):
        m = self.msg
        head = [f"From:    {m['from']}", f"To:      {m['to']}"]
        if m["cc"]:
            head.append(f"Cc:      {m['cc']}")
        head += [f"Date:    {m['date']}", f"Subject: {m['subject']}", f"Account: {m['account']}"]
        if m["attachments"]:
            head.append("Attach:  " + ", ".join(a["filename"] or a["type"] for a in m["attachments"]))
        out = head + ["─" * (w - 2)]
        for para in m["body"].splitlines():
            out += textwrap.wrap(para, w - 2) or [""]
        return out

    def run(self):
        while True:
            h, w = self.scr.getmaxyx()
            lines = self.lines(w)
            self.scr.erase()
            for i, line in enumerate(lines[self.top : self.top + h - 1]):
                try:
                    self.scr.addnstr(i, 1, line, w - 2, curses.A_BOLD if self.top + i < 6 and line[:1] != "─" else 0)
                except curses.error:
                    pass
            try:
                self.scr.addnstr(h - 1, 1, "↑↓/space scroll  q back", w - 2, curses.A_DIM)
            except curses.error:
                pass
            k = self.scr.get_wch()
            if k in ("q", "\x1b", curses.KEY_LEFT, curses.KEY_BACKSPACE, "\x7f"):
                return
            if k in (curses.KEY_DOWN, "j"):
                self.top = min(self.top + 1, max(0, len(lines) - h + 1))
            elif k in (curses.KEY_UP, "k"):
                self.top = max(0, self.top - 1)
            elif k in (" ", curses.KEY_NPAGE):
                self.top = min(self.top + h - 2, max(0, len(lines) - h + 1))
            elif k == curses.KEY_PPAGE:
                self.top = max(0, self.top - h + 2)


def run():
    curses.wrapper(lambda scr: App(scr).run())
