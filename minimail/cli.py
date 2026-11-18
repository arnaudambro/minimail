"""Command line interface. Every read command supports --json for scripts and AI agents."""

from __future__ import annotations

import argparse
import getpass
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

from . import __version__, core, imap
from .config import (PROVIDERS, NAME_RE, Account, get_account, keychain_delete, keychain_set, load_accounts,
                     save_accounts)


def out_json(data):
    print(json.dumps(data, indent=2, ensure_ascii=False))


def print_errors(errors):
    for name, err in errors.items():
        print(f"! {name}: {err}", file=sys.stderr)


# ---------------------------------------------------------------- accounts

def cmd_account_add(a):
    if not NAME_RE.match(a.name):
        raise SystemExit("Account name must be lowercase letters, digits, '.', '_' or '-'.")
    accts = load_accounts(include_disabled=True)
    if any(x.name == a.name for x in accts):
        raise SystemExit(f"Account '{a.name}' already exists. Change it with `minimail account edit {a.name} ...`, "
                         f"or remove it with `minimail account remove {a.name}`.")
    p = PROVIDERS[a.provider]
    imap_host, imap_port, imap_sec = p["imap"]
    smtp_host, smtp_port, smtp_sec = p["smtp"]
    acct = Account(
        name=a.name, email=a.email, provider=a.provider,
        username=a.username or a.email,
        imap_host=a.imap_host or imap_host, imap_port=a.imap_port or imap_port, imap_security=a.imap_security or imap_sec,
        smtp_host=a.smtp_host or smtp_host, smtp_port=a.smtp_port or smtp_port, smtp_security=a.smtp_security or smtp_sec,
        display_name=a.display_name or "",
        smtp_saves_sent=p.get("smtp_saves_sent", False),
        insecure_tls=p.get("insecure_tls", False),
    )
    if not acct.imap_host or not acct.smtp_host:
        raise SystemExit("custom provider needs --imap-host and --smtp-host")
    if not a.json:
        print(p["help"], file=sys.stderr)
    password = sys.stdin.readline().rstrip("\n") if a.password_stdin else getpass.getpass(f"Password for {acct.email}: ")
    keychain_set(acct.name, _clean(acct, password))
    if a.json:
        # Used by the app: only save the account when the login works.
        ok, err = _check(acct)
        if ok:
            accts.append(acct)
            save_accounts(accts)
        else:
            keychain_delete(acct.name)
            imap.forget_folders(acct.name)
        return out_json({"name": acct.name, "ok": ok, "error": err})
    accts.append(acct)
    save_accounts(accts)
    if a.no_test or _test(acct):
        print(f"Added {acct.name}.")
    else:
        print(f"Saved {acct.name}, but the check failed (see above). Fix the password with "
              f"`minimail account password {acct.name}`, or set a folder with `minimail account folder {acct.name} sent <name>`.")


def _clean(acct: Account, password: str) -> str:
    # Google shows app passwords as "abcd efgh ijkl mnop"; the real password has no spaces.
    return password.replace(" ", "") if acct.provider == "gmail" else password


def _check(acct: Account):
    """Log in and find the folders. Returns (ok, error_message_or_folders)."""
    try:
        with imap.session(acct) as m:
            found = {k: imap.resolve_folder(m, acct, k) for k in imap.FOLDER_KEYS}
        return True, ", ".join(f"{k}={imap.utf7_decode(v)}" for k, v in found.items())
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        if isinstance(getattr(e, "args", [None])[0], bytes):
            msg = e.args[0].decode("utf-8", "replace")
        return False, msg


def _test(acct: Account) -> bool:
    ok, info = _check(acct)
    print(f"✓ {acct.name}: IMAP ok — {info}" if ok else f"✗ {acct.name}: {info}")
    return ok


def cmd_account_test(a):
    accts = core.pick_accounts(a.names)
    ok = all([_test(x) for x in accts])
    sys.exit(0 if ok else 1)


def cmd_account_list(a):
    accts = load_accounts(include_disabled=True)
    if a.json:
        return out_json([{k: v for k, v in x.__dict__.items()} for x in accts])
    if not accts:
        print("No accounts. Add one with `minimail account add`.")
    for x in accts:
        flag = ("" if x.enabled else "  (disabled)") + ("  (local read)" if x.local_read else "")
        print(f"{x.name:<16} {x.email:<38} {x.provider:<14} {x.imap_host}:{x.imap_port}{flag}")


def cmd_account_remove(a):
    accts = load_accounts(include_disabled=True)
    left = [x for x in accts if x.name != a.name]
    if len(left) == len(accts):
        raise SystemExit(f"Unknown account: {a.name}")
    save_accounts(left)
    keychain_delete(a.name)
    imap.forget_folders(a.name)
    from . import store
    store.forget_account(a.name)
    print(json.dumps({"removed": a.name}) if a.json else f"Removed {a.name}.")


def cmd_account_password(a):
    acct = get_account(a.name)
    password = sys.stdin.readline().rstrip("\n") if a.password_stdin else getpass.getpass(f"New password for {a.name}: ")
    keychain_set(a.name, _clean(acct, password))
    if a.json:
        ok, err = _check(acct)
        return out_json({"name": a.name, "ok": ok, "error": None if ok else err})
    print("Saved in Keychain.")
    _test(acct)


def _edit(name, fn):
    accts = load_accounts(include_disabled=True)
    for x in accts:
        if x.name == name:
            fn(x)
            save_accounts(accts)
            return x
    raise SystemExit(f"Unknown account: {name}")


def cmd_account_enable(a, enabled):
    _edit(a.name, lambda x: setattr(x, "enabled", enabled))
    print(f"{a.name} {'enabled' if enabled else 'disabled'}.")


def cmd_account_local_read(a):
    on = a.state == "on"
    _edit(a.name, lambda x: setattr(x, "local_read", on))
    if a.json:
        return out_json({"name": a.name, "local_read": on})
    print(f"{a.name}: read/unread is now " + ("kept on this Mac only (the server is not changed)." if on
                                              else "synced with the server."))


def cmd_account_edit(a):
    fields = {"username": a.username, "display_name": a.display_name, "email": a.email,
              "imap_host": a.imap_host, "imap_port": a.imap_port, "imap_security": a.imap_security,
              "smtp_host": a.smtp_host, "smtp_port": a.smtp_port, "smtp_security": a.smtp_security}
    changes = {k: v for k, v in fields.items() if v is not None}
    if not changes:
        raise SystemExit("Nothing to change. See `minimail account edit --help`.")
    acct = _edit(a.name, lambda x: [setattr(x, k, v) for k, v in changes.items()])
    imap.forget_folders(a.name)
    if a.json:
        ok, err = _check(acct)
        return out_json({"name": a.name, "ok": ok, "error": None if ok else err})
    print(f"Updated {a.name}: " + ", ".join(f"{k}={v}" for k, v in changes.items()))
    _test(acct)


def cmd_account_folder(a):
    _edit(a.name, lambda x: x.folders.__setitem__(a.key, a.folder))
    imap.forget_folders(a.name)
    print(f"{a.name}: {a.key} -> {a.folder}")


def cmd_account_folders(a):
    acct = get_account(a.name)
    with imap.session(acct) as m:
        for flags, name in imap.list_folders(m):
            print(f"{name:<40} {imap.utf7_decode(name):<40} {flags.decode()}")


def cmd_providers(a):
    if a.json:
        return out_json([{"key": k, "help": p["help"], "imap": p["imap"], "smtp": p["smtp"]} for k, p in PROVIDERS.items()])
    for k, p in PROVIDERS.items():
        print(f"{k:<14} {p['help']}")


# ---------------------------------------------------------------- mail

def fmt_row(m, folder):
    d = datetime.fromtimestamp(m["ts"]).strftime("%Y-%m-%d %H:%M") if m["ts"] else " " * 16
    who = core.short_from(m["to"] if folder == "sent" else m["from"])[:24]
    return f"{'●' if m['unread'] else ' '} {d}  {m['id']:<28} {who:<24}  {m['subject']}"


def cmd_list(a):
    folder = a.folder or (None if a.search else "inbox")
    msgs, errors = core.list_messages(folder, a.account, a.unread, a.limit, a.search, live=a.live)
    if a.json:
        return out_json({"messages": msgs, "errors": errors})
    for m in msgs:
        print(fmt_row(m, m["folder"]))
    if not msgs:
        print("No messages.")
    print_errors(errors)


def cmd_show(a):
    m = core.get_message(a.id, mark_read=a.mark_read, live=a.live)
    if a.json:
        return out_json(m)
    print(f"From:    {m['from']}\nTo:      {m['to']}")
    if m["cc"]:
        print(f"Cc:      {m['cc']}")
    print(f"Date:    {m['date']}\nSubject: {m['subject']}\nId:      {m['id']}")
    if m["attachments"]:
        print("Attach:  " + ", ".join(f"{x['filename']} ({x['size']} B)" for x in m["attachments"]))
    print("─" * 60)
    print(m["body"])


def cmd_attachments(a):
    saved = core.save_attachments(a.id, a.dir)
    if a.json:
        return out_json(saved)
    if not saved:
        print("No attachments.")
    for x in saved:
        print(f"{x['path']}  ({x['size']} B)")


def cmd_mark(a, seen):
    if a.all:
        if not seen:
            raise SystemExit("--all is only supported for mark-read")
        results, errors = core.mark_all_read(a.folder, a.account)
        if a.json:
            return out_json({"marked": results, "errors": errors})
        if not a.quiet:
            for k, v in results.items():
                print(f"{k}: {v} marked read")
            print_errors(errors)
        return
    if not a.ids:
        raise SystemExit("Give message ids, or --all.")
    n = core.set_seen(a.ids, seen)
    if a.json:
        return out_json({"marked": n})
    if not a.quiet:
        print(f"Marked {n} as {'read' if seen else 'unread'}.")


def cmd_move(a):
    n = core.move(a.ids, a.dest)
    print(json.dumps({"moved": n}) if a.json else f"Moved {n} to {a.dest}.")


def cmd_delete(a):
    n = core.delete(a.ids)
    print(json.dumps({"deleted": n}) if a.json else f"Moved {n} to Trash.")


def cmd_undo(a):
    r = core.undo()
    if a.json:
        print(json.dumps(r))
    elif r.get("error"):
        print(r["error"])
    else:
        print(f"Restored {r['restored']} message(s)." + (f" {r['missing']} not found." if r["missing"] else ""))


def cmd_sync(a):
    from . import store
    accts = core.pick_accounts(a.account)
    results, errors = core.parallel(lambda x: len(store.sync_account(x, bodies=a.bodies)), accts)
    if a.json:
        return out_json({"new": results, "errors": errors})
    if not a.quiet:
        for k, v in results.items():
            print(f"{k}: {v} new")
        print_errors(errors)


def cmd_contacts(a):
    from . import store
    rows = store.contacts(store.connect(), a.query or "", a.limit)
    if a.json:
        return out_json(rows)
    for r in rows:
        print(f"{r['email']:<40} {r['name'] or '':<30} sent={r['sent']} received={r['received']}")


def cmd_unsubscribe(a):
    r = core.unsubscribe(a.id)
    if a.json:
        return out_json(r)
    if r["method"] == "one-click":
        print("Unsubscribed." if r["ok"] else f"Failed, open: {r['url']}")
    elif r["method"] == "email":
        print(f"Sent an unsubscribe email to {r['to']}.")
    elif r["method"] == "browser":
        print(f"Open this page to unsubscribe: {r['url']}")
    else:
        print(r["error"])


def cmd_counts(a):
    results, errors = core.unread_counts(a.account, live=a.live)
    if a.json:
        return out_json({"accounts": results, "errors": errors})
    print(f"{'account':<16} {'inbox':>12} {'sent':>8} {'spam':>12}")
    for name, c in results.items():
        def cell(k, unread=True):
            v = c.get(k) or {}
            if "error" in v:
                return "err"
            return f"{v.get('unread', 0)}/{v.get('total', 0)}" if unread else str(v.get("total", 0))
        print(f"{name:<16} {cell('inbox'):>12} {cell('sent', False):>8} {cell('spam'):>12}")
    print("(unread/total)")
    print_errors(errors)


def cmd_send(a):
    body = a.body if a.body is not None else sys.stdin.read()
    r = core.send(a.account, a.to or [], a.subject or "", body, a.cc or [], a.bcc or [], a.reply, a.forward,
                  quote=not a.no_quote)
    print(json.dumps(r) if a.json else f"Sent to {', '.join(r['to'])}: {r['subject']}")


# ---------------------------------------------------------------- watcher / tui / skill

def cmd_watch(a):
    from .watch import Watcher
    Watcher(a.interval, emit=a.emit).run()


MX_HINTS = [
    ("google.com", "gmail"), ("googlemail.com", "gmail"),
    ("icloud.com", "icloud"), ("me.com", "icloud"),
    ("yahoodns.net", "yahoo"),
    ("outlook.com", "outlook"),
    ("ovh.net", "ovh"),
    ("protonmail.ch", "proton-bridge"),
    ("numerique.gouv.fr", "ox-numerique"),
]
DOMAIN_HINTS = {"gmail.com": "gmail", "googlemail.com": "gmail", "icloud.com": "icloud", "me.com": "icloud",
                "mac.com": "icloud", "proton.me": "proton-bridge", "protonmail.com": "proton-bridge", "pm.me": "proton-bridge",
                "outlook.com": "outlook", "hotmail.com": "outlook", "live.com": "outlook"}


def detect_provider(email: str) -> tuple[str, list[str]]:
    import subprocess
    domain = email.rsplit("@", 1)[-1].strip().lower()
    if domain.startswith("yahoo."):
        return "yahoo", []
    if domain in DOMAIN_HINTS:
        return DOMAIN_HINTS[domain], []
    try:
        r = subprocess.run(["dig", "+short", "MX", domain], capture_output=True, text=True, timeout=5)
        mx = [line.split()[-1].rstrip(".").lower() for line in r.stdout.splitlines() if line.strip()]
    except Exception:  # noqa: BLE001
        mx = []
    for host in mx:
        for hint, prov in MX_HINTS:
            if host == hint or host.endswith("." + hint):
                return prov, mx
    return "custom", mx


def cmd_detect(a):
    prov, mx = detect_provider(a.email)
    if a.json:
        return out_json({"provider": prov, "mx": mx})
    print(prov)


def cmd_daemon(a):
    from . import watch
    if a.action == "status" and a.json:
        st = watch.agent_status()
        return out_json({"installed": watch.PLIST.exists(), "running": st.startswith("state = running"),
                         "state": st, "notifier": bool(core.notifier())})
    if a.action == "install":
        if not core.notifier():
            print("warning: terminal-notifier not found. Install it: brew install terminal-notifier", file=sys.stderr)
        print(f"Installed {watch.install_agent(a.interval, a.agent)}. Logs: {watch.LOG}")
    elif a.action == "uninstall":
        watch.uninstall_agent()
        print("Uninstalled.")
    else:
        print(watch.agent_status())


def cmd_tui(a):
    from .tui import run
    run()


def cmd_install_skill(a):
    src = Path(__file__).parent / "skill" / "SKILL.md"
    dest = Path(a.dir).expanduser() / "minimail" / "SKILL.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)
    print(f"Installed Claude Code skill at {dest}")


# ---------------------------------------------------------------- parser

def build_parser():
    p = argparse.ArgumentParser(prog="minimail", description="Minimal IMAP mail client: Inbox, Sent, Spam.")
    p.add_argument("--version", action="version", version=f"minimail {__version__}")
    sub = p.add_subparsers(dest="cmd")

    def j(sp):
        sp.add_argument("--json", action="store_true", help="machine-readable output")
        return sp

    folder_kw = dict(choices=imap.FOLDER_KEYS, default="inbox")

    # account
    acc = sub.add_parser("account", help="manage accounts").add_subparsers(dest="acmd", required=True)
    s = acc.add_parser("add", help="add an account (password goes to the macOS Keychain)")
    s.add_argument("name", help="short name, e.g. perso")
    s.add_argument("email")
    s.add_argument("--provider", choices=PROVIDERS.keys(), required=True)
    s.add_argument("--username", help="login (defaults to the email)")
    s.add_argument("--display-name")
    s.add_argument("--imap-host"); s.add_argument("--imap-port", type=int)
    s.add_argument("--imap-security", choices=["ssl", "starttls"])
    s.add_argument("--smtp-host"); s.add_argument("--smtp-port", type=int)
    s.add_argument("--smtp-security", choices=["ssl", "starttls"])
    s.add_argument("--password-stdin", action="store_true")
    s.add_argument("--no-test", action="store_true")
    s.add_argument("--json", action="store_true", help="test the login first; save only if it works")
    s.set_defaults(fn=cmd_account_add)
    s = j(acc.add_parser("list")); s.set_defaults(fn=cmd_account_list)
    s = acc.add_parser("test"); s.add_argument("names", nargs="*"); s.set_defaults(fn=cmd_account_test)
    s = j(acc.add_parser("remove")); s.add_argument("name"); s.set_defaults(fn=cmd_account_remove)
    s = acc.add_parser("password", help="change the stored password")
    s.add_argument("name"); s.add_argument("--password-stdin", action="store_true"); s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_account_password)
    s = j(acc.add_parser("edit", help="change account settings (username, servers, ...)"))
    s.add_argument("name")
    s.add_argument("--username"); s.add_argument("--display-name"); s.add_argument("--email")
    s.add_argument("--imap-host"); s.add_argument("--imap-port", type=int)
    s.add_argument("--imap-security", choices=["ssl", "starttls"])
    s.add_argument("--smtp-host"); s.add_argument("--smtp-port", type=int)
    s.add_argument("--smtp-security", choices=["ssl", "starttls"])
    s.set_defaults(fn=cmd_account_edit)
    s = acc.add_parser("enable"); s.add_argument("name"); s.set_defaults(fn=lambda a: cmd_account_enable(a, True))
    s = acc.add_parser("disable"); s.add_argument("name"); s.set_defaults(fn=lambda a: cmd_account_enable(a, False))
    s = j(acc.add_parser("local-read", help="shared mailbox: mark read/unread on this Mac only, never on the server"))
    s.add_argument("name"); s.add_argument("state", choices=["on", "off"])
    s.set_defaults(fn=cmd_account_local_read)
    s = acc.add_parser("folder", help="override the server folder used for sent/spam")
    s.add_argument("name"); s.add_argument("key", choices=["sent", "spam"]); s.add_argument("folder")
    s.set_defaults(fn=cmd_account_folder)
    s = acc.add_parser("folders", help="list the server's folders"); s.add_argument("name"); s.set_defaults(fn=cmd_account_folders)

    j(sub.add_parser("providers", help="list provider presets")).set_defaults(fn=cmd_providers)
    s = j(sub.add_parser("detect", help="guess the provider of an email address (MX lookup)"))
    s.add_argument("email"); s.set_defaults(fn=cmd_detect)

    s = j(sub.add_parser("list", help="list latest messages, newest first"))
    s.add_argument("--folder", "-f", choices=imap.FOLDER_KEYS, help="default inbox; with --search, all folders")
    s.add_argument("--account", "-a", action="append", help="repeatable; default all")
    s.add_argument("--unread", "-u", action="store_true")
    s.add_argument("--limit", "-n", type=int, default=20, help="per account (default 20)")
    s.add_argument("--search", "-s", help="full-text search (subject, people, body)")
    s.add_argument("--live", action="store_true", help="ask the servers instead of the local cache")
    s.set_defaults(fn=cmd_list)

    s = j(sub.add_parser("show", help="print one message (does not mark it read)"))
    s.add_argument("id"); s.add_argument("--mark-read", action="store_true"); s.add_argument("--live", action="store_true")
    s.set_defaults(fn=cmd_show)

    s = j(sub.add_parser("attachments", help="save a message's attachments to a folder"))
    s.add_argument("id"); s.add_argument("--dir", "-d", default="~/Downloads", help="destination folder (default: ~/Downloads)")
    s.set_defaults(fn=cmd_attachments)

    for name, seen in (("mark-read", True), ("mark-unread", False)):
        s = j(sub.add_parser(name))
        s.add_argument("ids", nargs="*")
        s.add_argument("--all", action="store_true", help="every unread message in --folder")
        s.add_argument("--folder", "-f", **folder_kw)
        s.add_argument("--account", "-a", action="append")
        s.add_argument("--quiet", "-q", action="store_true")
        s.set_defaults(fn=lambda a, seen=seen: cmd_mark(a, seen))

    s = j(sub.add_parser("spam", help="move messages to spam")); s.add_argument("ids", nargs="+")
    s.set_defaults(fn=cmd_move, dest="spam")
    s = j(sub.add_parser("not-spam", help="move messages back to inbox")); s.add_argument("ids", nargs="+")
    s.set_defaults(fn=cmd_move, dest="inbox")

    s = j(sub.add_parser("delete", help="move messages to Trash")); s.add_argument("ids", nargs="+")
    s.set_defaults(fn=cmd_delete)
    s = j(sub.add_parser("undo", help="undo the last delete / spam / not-spam"))
    s.set_defaults(fn=cmd_undo)

    s = j(sub.add_parser("unsubscribe", help="unsubscribe from a mailing list (List-Unsubscribe)"))
    s.add_argument("id"); s.set_defaults(fn=cmd_unsubscribe)

    s = j(sub.add_parser("counts", help="unread/total per account and folder"))
    s.add_argument("--account", "-a", action="append"); s.add_argument("--live", action="store_true")
    s.set_defaults(fn=cmd_counts)

    s = j(sub.add_parser("sync", help="update the local cache from the servers"))
    s.add_argument("--account", "-a", action="append")
    s.add_argument("--bodies", type=int, default=60, help="text bodies to fetch per folder (default 60)")
    s.add_argument("--quiet", "-q", action="store_true")
    s.set_defaults(fn=cmd_sync)

    s = j(sub.add_parser("contacts", help="known addresses, most used first (from your mail)"))
    s.add_argument("query", nargs="?"); s.add_argument("--limit", "-n", type=int, default=10)
    s.set_defaults(fn=cmd_contacts)

    s = j(sub.add_parser("send", help="send an email (body from --body or stdin)"))
    s.add_argument("--account", "-a", required=True)
    s.add_argument("--to", action="append"); s.add_argument("--cc", action="append"); s.add_argument("--bcc", action="append")
    s.add_argument("--subject")
    s.add_argument("--body")
    s.add_argument("--reply", metavar="ID", help="reply to this message id (fills To/Subject/threading)")
    s.add_argument("--no-quote", action="store_true", help="with --reply: do not append the quoted original (the body already has it)")
    s.add_argument("--forward", metavar="ID", help="forward this message id (body = your note on top; keeps HTML and attachments)")
    s.set_defaults(fn=cmd_send)

    s = sub.add_parser("watch", help="poll inboxes and post notifications (foreground)")
    s.add_argument("--interval", type=int, default=30)
    s.add_argument("--emit", action="store_true", help="print notifications as JSON lines instead of posting them")
    s.set_defaults(fn=cmd_watch)
    s = j(sub.add_parser("daemon", help="run the watcher at login via launchd"))
    s.add_argument("action", choices=["install", "uninstall", "status"])
    s.add_argument("--interval", type=int, default=30)
    s.add_argument("--agent", help="run this notifier app binary instead (it starts `minimail watch --emit` itself)")
    s.set_defaults(fn=cmd_daemon)

    sub.add_parser("tui", help="interactive terminal UI (default)").set_defaults(fn=cmd_tui)
    s = sub.add_parser("install-skill", help="install the Claude Code skill")
    s.add_argument("--dir", default="~/.claude/skills"); s.set_defaults(fn=cmd_install_skill)
    return p


def main(argv=None):
    p = build_parser()
    a = p.parse_args(argv)
    if not getattr(a, "fn", None):
        return cmd_tui(a)
    try:
        a.fn(a)
    except KeyboardInterrupt:
        sys.exit(130)
    except RuntimeError as e:
        raise SystemExit(f"error: {e}")
