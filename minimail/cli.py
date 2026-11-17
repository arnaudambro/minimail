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
        raise SystemExit(f"Account '{a.name}' already exists.")
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
    print(p["help"], file=sys.stderr)
    password = sys.stdin.readline().rstrip("\n") if a.password_stdin else getpass.getpass(f"Password for {acct.email}: ")
    keychain_set(acct.name, password)
    accts.append(acct)
    save_accounts(accts)
    if not a.no_test:
        _test(acct)
    print(f"Added {acct.name}.")


def _test(acct: Account) -> bool:
    try:
        with imap.session(acct) as m:
            found = {k: imap.resolve_folder(m, acct, k) for k in imap.FOLDER_KEYS}
        print(f"✓ {acct.name}: IMAP ok — " + ", ".join(f"{k}={imap.utf7_decode(v)}" for k, v in found.items()))
        return True
    except Exception as e:  # noqa: BLE001
        print(f"✗ {acct.name}: {type(e).__name__}: {e}")
        return False


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
        flag = "" if x.enabled else "  (disabled)"
        print(f"{x.name:<16} {x.email:<38} {x.provider:<14} {x.imap_host}:{x.imap_port}{flag}")


def cmd_account_remove(a):
    accts = load_accounts(include_disabled=True)
    left = [x for x in accts if x.name != a.name]
    if len(left) == len(accts):
        raise SystemExit(f"Unknown account: {a.name}")
    save_accounts(left)
    keychain_delete(a.name)
    imap.forget_folders(a.name)
    print(f"Removed {a.name}.")


def cmd_account_password(a):
    get_account(a.name)
    password = sys.stdin.readline().rstrip("\n") if a.password_stdin else getpass.getpass(f"New password for {a.name}: ")
    keychain_set(a.name, password)
    print("Saved in Keychain.")


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
    for k, p in PROVIDERS.items():
        print(f"{k:<14} {p['help']}")


# ---------------------------------------------------------------- mail

def fmt_row(m, folder):
    d = datetime.fromtimestamp(m["ts"]).strftime("%Y-%m-%d %H:%M") if m["ts"] else " " * 16
    who = core.short_from(m["to"] if folder == "sent" else m["from"])[:24]
    return f"{'●' if m['unread'] else ' '} {d}  {m['id']:<28} {who:<24}  {m['subject']}"


def cmd_list(a):
    msgs, errors = core.list_messages(a.folder, a.account, a.unread, a.limit, a.search)
    if a.json:
        return out_json({"messages": msgs, "errors": errors})
    for m in msgs:
        print(fmt_row(m, a.folder))
    if not msgs:
        print("No messages.")
    print_errors(errors)


def cmd_show(a):
    m = core.get_message(a.id, mark_read=a.mark_read)
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


def cmd_counts(a):
    results, errors = core.unread_counts(a.account)
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
    r = core.send(a.account, a.to or [], a.subject or "", body, a.cc or [], a.bcc or [], a.reply)
    print(json.dumps(r) if a.json else f"Sent to {', '.join(r['to'])}: {r['subject']}")


# ---------------------------------------------------------------- watcher / tui / skill

def cmd_watch(a):
    from .watch import Watcher
    Watcher(a.interval).run()


def cmd_daemon(a):
    from . import watch
    if a.action == "install":
        if not core.notifier():
            print("warning: terminal-notifier not found. Install it: brew install terminal-notifier", file=sys.stderr)
        print(f"Installed {watch.install_agent(a.interval)}. Logs: {watch.LOG}")
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
    s.set_defaults(fn=cmd_account_add)
    s = j(acc.add_parser("list")); s.set_defaults(fn=cmd_account_list)
    s = acc.add_parser("test"); s.add_argument("names", nargs="*"); s.set_defaults(fn=cmd_account_test)
    s = acc.add_parser("remove"); s.add_argument("name"); s.set_defaults(fn=cmd_account_remove)
    s = acc.add_parser("password", help="change the stored password")
    s.add_argument("name"); s.add_argument("--password-stdin", action="store_true"); s.set_defaults(fn=cmd_account_password)
    s = acc.add_parser("enable"); s.add_argument("name"); s.set_defaults(fn=lambda a: cmd_account_enable(a, True))
    s = acc.add_parser("disable"); s.add_argument("name"); s.set_defaults(fn=lambda a: cmd_account_enable(a, False))
    s = acc.add_parser("folder", help="override the server folder used for sent/spam")
    s.add_argument("name"); s.add_argument("key", choices=["sent", "spam"]); s.add_argument("folder")
    s.set_defaults(fn=cmd_account_folder)
    s = acc.add_parser("folders", help="list the server's folders"); s.add_argument("name"); s.set_defaults(fn=cmd_account_folders)

    sub.add_parser("providers", help="list provider presets").set_defaults(fn=cmd_providers)

    s = j(sub.add_parser("list", help="list latest messages, newest first"))
    s.add_argument("--folder", "-f", **folder_kw)
    s.add_argument("--account", "-a", action="append", help="repeatable; default all")
    s.add_argument("--unread", "-u", action="store_true")
    s.add_argument("--limit", "-n", type=int, default=20, help="per account (default 20)")
    s.add_argument("--search", "-s", help="IMAP full-text search")
    s.set_defaults(fn=cmd_list)

    s = j(sub.add_parser("show", help="print one message (does not mark it read)"))
    s.add_argument("id"); s.add_argument("--mark-read", action="store_true"); s.set_defaults(fn=cmd_show)

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

    s = j(sub.add_parser("counts", help="unread/total per account and folder"))
    s.add_argument("--account", "-a", action="append"); s.set_defaults(fn=cmd_counts)

    s = j(sub.add_parser("send", help="send an email (body from --body or stdin)"))
    s.add_argument("--account", "-a", required=True)
    s.add_argument("--to", action="append"); s.add_argument("--cc", action="append"); s.add_argument("--bcc", action="append")
    s.add_argument("--subject")
    s.add_argument("--body")
    s.add_argument("--reply", metavar="ID", help="reply to this message id (fills To/Subject/threading)")
    s.set_defaults(fn=cmd_send)

    s = sub.add_parser("watch", help="poll inboxes and post notifications (foreground)")
    s.add_argument("--interval", type=int, default=60); s.set_defaults(fn=cmd_watch)
    s = sub.add_parser("daemon", help="run the watcher at login via launchd")
    s.add_argument("action", choices=["install", "uninstall", "status"])
    s.add_argument("--interval", type=int, default=60); s.set_defaults(fn=cmd_daemon)

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
