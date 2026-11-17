"""Account configuration. Passwords live in the macOS Keychain, never on disk."""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "minimail"
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "minimail"
CONFIG_FILE = CONFIG_DIR / "config.json"
KEYCHAIN_SERVICE = "minimail"

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

PROVIDERS: dict[str, dict] = {
    "gmail": {
        "imap": ("imap.gmail.com", 993, "ssl"),
        "smtp": ("smtp.gmail.com", 465, "ssl"),
        "smtp_saves_sent": True,
        "help": "Gmail / Google Workspace. Needs an app password: myaccount.google.com/apppasswords (2-step verification required).",
    },
    "icloud": {
        "imap": ("imap.mail.me.com", 993, "ssl"),
        "smtp": ("smtp.mail.me.com", 587, "starttls"),
        "smtp_saves_sent": False,
        "help": "iCloud Mail. Needs an app-specific password: account.apple.com > Sign-In and Security.",
    },
    "yahoo": {
        "imap": ("imap.mail.yahoo.com", 993, "ssl"),
        "smtp": ("smtp.mail.yahoo.com", 465, "ssl"),
        "smtp_saves_sent": False,
        "help": "Yahoo Mail. Needs an app password: Account security > Generate app password.",
    },
    "outlook": {
        "imap": ("outlook.office365.com", 993, "ssl"),
        "smtp": ("smtp.office365.com", 587, "starttls"),
        "smtp_saves_sent": True,
        "help": "Outlook / Microsoft 365. Only works where basic auth / app passwords are still allowed.",
    },
    "ovh": {
        "imap": ("ssl0.ovh.net", 993, "ssl"),
        "smtp": ("ssl0.ovh.net", 465, "ssl"),
        "smtp_saves_sent": False,
        "help": "OVH MX Plan / Email Pro. Use the mailbox password.",
    },
    "ox-numerique": {
        "imap": ("imap.ox.numerique.gouv.fr", 993, "ssl"),
        "smtp": ("smtp.ox.numerique.gouv.fr", 465, "ssl"),
        "smtp_saves_sent": False,
        "help": "French public sector OX mail (e.g. beta.gouv.fr). Use the mailbox password.",
    },
    "proton-bridge": {
        "imap": ("127.0.0.1", 1143, "starttls"),
        "smtp": ("127.0.0.1", 1025, "starttls"),
        "smtp_saves_sent": True,
        "insecure_tls": True,
        "help": "Proton Mail through Proton Mail Bridge. Use the username and password shown in the Bridge app.",
    },
    "custom": {
        "imap": (None, 993, "ssl"),
        "smtp": (None, 465, "ssl"),
        "smtp_saves_sent": False,
        "help": "Any IMAP/SMTP server. Pass --imap-host and --smtp-host.",
    },
}


@dataclass
class Account:
    name: str
    email: str
    provider: str
    username: str
    imap_host: str
    imap_port: int
    imap_security: str  # "ssl" | "starttls"
    smtp_host: str
    smtp_port: int
    smtp_security: str  # "ssl" | "starttls"
    display_name: str = ""
    smtp_saves_sent: bool = False
    insecure_tls: bool = False
    enabled: bool = True
    # Optional overrides: {"sent": "Sent Messages", "spam": "Junk"}
    folders: dict = field(default_factory=dict)

    def password(self) -> str:
        return keychain_get(self.name)


def load_accounts(include_disabled: bool = False) -> list[Account]:
    if not CONFIG_FILE.exists():
        return []
    data = json.loads(CONFIG_FILE.read_text())
    accts = [Account(**a) for a in data.get("accounts", [])]
    return accts if include_disabled else [a for a in accts if a.enabled]


def save_accounts(accounts: list[Account]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps({"accounts": [asdict(a) for a in accounts]}, indent=2) + "\n")
    os.chmod(CONFIG_FILE, 0o600)


def get_account(name: str) -> Account:
    for a in load_accounts(include_disabled=True):
        if a.name == name:
            return a
    raise SystemExit(f"Unknown account: {name}. Run `minimail account list`.")


def keychain_set(name: str, password: str) -> None:
    subprocess.run(
        ["security", "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE, "-a", name, "-w", password],
        check=True,
        capture_output=True,
    )


def keychain_get(name: str) -> str:
    r = subprocess.run(
        ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", name, "-w"],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"No password in Keychain for account '{name}'. Run `minimail account password {name}`.")
    return r.stdout.rstrip("\n")


def keychain_delete(name: str) -> None:
    subprocess.run(
        ["security", "delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", name],
        capture_output=True,
    )
