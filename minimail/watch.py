"""Background watcher: polls every inbox and posts macOS notifications.

Clicking a notification marks that message as read (or every message of a
grouped notification).
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import core, imap
from .config import STATE_DIR, Account, load_accounts

STATE_FILE = STATE_DIR / "watch.json"
MAX_SINGLE = 4  # above this many new messages per account, send one summary notification
LABEL = "dev.minimail.watch"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
LOG = Path.home() / "Library" / "Logs" / "minimail.log"


def log(msg: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def self_command() -> list[str]:
    exe = shutil.which("minimail")
    if exe:
        return [exe]
    return [sys.executable, "-m", "minimail"]


def notify(title: str, subtitle: str, message: str, group: str, on_click: list[str]) -> None:
    tn = core.notifier()
    if not tn:
        log("terminal-notifier not found: brew install terminal-notifier")
        return
    subprocess.run(
        [tn, "-title", title, "-subtitle", subtitle, "-message", message or " ", "-group", group,
         "-sound", "default", "-execute", shlex.join(on_click)],
        capture_output=True,
    )


class Watcher:
    def __init__(self, interval: int = 60):
        self.interval = interval
        self.conns: dict[str, object] = {}
        try:
            self.state = json.loads(STATE_FILE.read_text())
        except Exception:
            self.state = {}

    def save(self):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(self.state, indent=2))

    def conn(self, acct: Account):
        m = self.conns.get(acct.name)
        if m is None:
            m = imap.connect(acct)
            self.conns[acct.name] = m
        return m

    def drop(self, acct: Account):
        m = self.conns.pop(acct.name, None)
        if m is not None:
            try:
                m.logout()
            except Exception:
                pass

    def check(self, acct: Account) -> list[dict]:
        try:
            return self._check(acct)
        except Exception as e:  # noqa: BLE001
            log(f"[{acct.name}] {type(e).__name__}: {e}")
            self.drop(acct)
            return []

    def _check(self, acct: Account) -> list[dict]:
        m = self.conn(acct)
        imap.select(m, acct, "inbox", readonly=True)  # re-select refreshes the mailbox
        uidvalidity = (m.response("UIDVALIDITY")[1] or [b"0"])[-1]
        uidvalidity = uidvalidity.decode() if isinstance(uidvalidity, bytes) else str(uidvalidity)
        st = self.state.get(acct.name)
        if not st or st.get("uidvalidity") != uidvalidity:
            uids = imap.search_uids(m, "ALL")
            self.state[acct.name] = {"uidvalidity": uidvalidity, "last_uid": max(uids) if uids else 0}
            return []
        last = st["last_uid"]
        new = [u for u in imap.search_uids(m, "UID", f"{last + 1}:*") if u > last]
        if not new:
            return []
        st["last_uid"] = max(new)
        unseen = set(imap.search_uids(m, "UNSEEN"))
        new = [u for u in new if u in unseen]
        if not new:
            return []
        rows = imap.fetch(m, new, core.HEADER_ITEMS)
        return [core._summary(acct, "inbox", r) for r in rows]

    def announce(self, acct: Account, msgs: list[dict]) -> None:
        cmd = self_command()
        if len(msgs) > MAX_SINGLE:
            ids = [x["id"] for x in msgs]
            senders = ", ".join(dict.fromkeys(core.short_from(x["from"]) for x in msgs))
            notify(f"{len(msgs)} new emails", acct.email, senders[:200], f"minimail-batch-{acct.name}",
                   cmd + ["mark-read", "--quiet", *ids])
            return
        for x in msgs:
            notify(core.short_from(x["from"]), x["subject"] or "(no subject)", f"{acct.email} · click to mark as read",
                   core.notification_group(x["id"]), cmd + ["mark-read", "--quiet", x["id"]])

    def run(self):
        log(f"watching {len(load_accounts())} accounts every {self.interval}s")
        while True:
            accts = load_accounts()  # reload so new accounts are picked up
            with ThreadPoolExecutor(max_workers=max(1, min(16, len(accts)))) as ex:
                results = list(zip(accts, ex.map(self.check, accts)))
            for acct, msgs in results:
                if msgs:
                    log(f"[{acct.name}] {len(msgs)} new")
                    self.announce(acct, msgs)
            self.save()
            time.sleep(self.interval)


# ---------------------------------------------------------------- launchd

def install_agent(interval: int) -> str:
    cmd = self_command() + ["watch", "--interval", str(interval)]
    args = "\n".join(f"    <string>{a}</string>" for a in cmd)
    path = os.environ.get("PATH", "") + ":/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    PLIST.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key>
  <array>
{args}
  </array>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>{path}</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>{LOG}</string>
  <key>StandardErrorPath</key><string>{LOG}</string>
</dict>
</plist>
""")
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"], capture_output=True)
    r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(PLIST)], capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"launchctl bootstrap failed: {r.stderr.strip()}")
    return str(PLIST)


def uninstall_agent() -> None:
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
    PLIST.unlink(missing_ok=True)


def agent_status() -> str:
    r = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"], capture_output=True, text=True)
    if r.returncode != 0:
        return "not running"
    for line in r.stdout.splitlines():
        if line.strip().startswith("state ="):
            return line.strip()
    return "loaded"
