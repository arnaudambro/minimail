# Minimail

A minimal IMAP mail client for macOS. Three folders, nothing else: **Inbox, Sent, Spam**.

- A native macOS app (SwiftUI) **and** a terminal UI
- Native macOS notifications for new mail — **click a notification to mark the message as read**
- Terminal UI: select all, mark read, move to spam, read a message
- A scriptable CLI with `--json` output, built so AI agents like [Claude Code](https://claude.com/claude-code) can triage your mail
- All your accounts in one unified list
- Passwords stored in the macOS Keychain, never on disk
- Pure Python standard library, zero dependencies

## Install

```bash
brew install terminal-notifier          # for notifications
uv tool install git+https://github.com/arnaudambro/minimail   # or: pipx install . / uv tool install .
```

### macOS app

```bash
git clone https://github.com/arnaudambro/minimail && cd minimail
app/build.sh                            # builds and installs ~/Applications/Minimail.app (needs Xcode)
```

The app is a thin UI on top of the `minimail` CLI, so install the CLI first.
Shortcuts: ⌘A select all · ⇧⌘U mark read · ⌥⌘U mark unread · ⇧⌘K mark all read · ⇧⌘J spam / not spam · ⌘N new · ⌘R reply · ⇧⌘N refresh.

## Add accounts

```bash
minimail providers                      # presets: gmail, icloud, yahoo, outlook, ovh, ox-numerique, proton-bridge, custom
minimail account add perso me@gmail.com --provider gmail
minimail account add apple me@icloud.com --provider icloud
minimail account add proton me@proton.me --provider proton-bridge --username <bridge username>
minimail account add work me@company.com --provider custom --imap-host imap.company.com --smtp-host smtp.company.com
minimail account test
```

Most providers require an **app password** (Gmail, iCloud, Yahoo). Proton Mail works through
[Proton Mail Bridge](https://proton.me/mail/bridge) — use the credentials the Bridge shows.

Sent and Spam folders are discovered automatically (IMAP SPECIAL-USE, then common names).
Override if needed: `minimail account folder perso spam "Junk"`.

## Use

```bash
minimail                                # terminal UI
minimail counts                         # unread per account
minimail list --unread                  # unified unread inbox
minimail list -f spam
minimail show perso:inbox:4821
minimail mark-read --all                # select all → read, every account
minimail spam perso:inbox:4821
minimail send -a perso --to x@example.com --subject Hi --body "Hello"
minimail send -a perso --reply perso:inbox:4821 --body "Thanks!"
```

### Terminal UI keys

| key | action |
| --- | --- |
| `↑ ↓` / `j k` | move |
| `space` | select / unselect |
| `a` | select all / none |
| `r` / `u` | mark read / unread (selection, or current line) |
| `s` | move to spam (or back to inbox from Spam) |
| `enter` | open (marks read) |
| `tab` / `1 2 3` | Inbox / Sent / Spam |
| `c` | cycle account (all → each) |
| `f` | unread only |
| `g` | refresh |
| `q` | quit |

## Notifications

```bash
minimail daemon install                 # starts at login (launchd), polls every 60 s
minimail daemon status
minimail daemon uninstall
tail -f ~/Library/Logs/minimail.log
```

One notification per new message (or one summary when many arrive at once).
Clicking it marks the message(s) as read. Marking mail read anywhere in Minimail clears its notification.

## Claude Code

```bash
minimail install-skill                  # copies the skill to ~/.claude/skills/minimail
```

Then ask Claude things like *"what's unread in my mail?"* or *"mark all the newsletters as read"*.
The skill tells Claude to ask before sending or bulk-changing anything.

## Files

| what | where |
| --- | --- |
| accounts | `~/.config/minimail/config.json` |
| passwords | macOS Keychain, service `minimail` |
| folder cache, watcher state | `~/.local/state/minimail/` |

## License

MIT
