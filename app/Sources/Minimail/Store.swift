import AppKit
import SwiftUI

enum Folder: String, CaseIterable, Identifiable, Hashable {
    case inbox, sent, spam
    var id: String { rawValue }
    var title: String { rawValue.capitalized }
    var icon: String {
        switch self {
        case .inbox: "tray"
        case .sent: "paperplane"
        case .spam: "xmark.bin"
        }
    }
}

struct Draft: Identifiable {
    let id = UUID()
    var account = ""
    var to = ""
    var subject = ""
    var body = ""
    var replyTo: String?
    var forwardOf: String?
}

@MainActor
final class Store: ObservableObject {
    @Published var folder: Folder? = .inbox {
        didSet { if oldValue != folder { selection = []; Task { await reload() } } }
    }
    @Published var account: String? {
        didSet { if oldValue != account { selection = []; Task { await reload() } } }
    }
    @Published var unreadOnly = false {
        didSet { if oldValue != unreadOnly { Task { await reload() } } }
    }
    /// Full-text search over every folder (local, instant).
    @Published var search = "" {
        didSet { if oldValue != search { Task { await reload() } } }
    }
    @Published var syncing = false
    @Published var accounts: [AccountInfo] = []
    @Published var messages: [Message] = []
    @Published var selection = Set<String>()
    @Published var current: FullMessage?
    @Published var loading = false
    @Published var loadingMessage = false
    @Published var busy = false
    @Published var errors: [String: String] = [:]
    @Published var banner: String?
    /// Unread counts per account and folder: unread["gmail"]["inbox"].
    @Published var unread: [String: [String: Int]] = [:]
    /// Bumped on every optimistic change, so a slower server count cannot overwrite it.
    private var countsVersion = 0
    private var inFlight = 0
    @Published var compose: Draft?
    @Published var showAddAccount = false
    @Published var providers: [ProviderInfo] = []
    @Published var notif: NotifStatus?

    private var generation = 0
    // Optimistic changes not yet confirmed by the server. A reload must not undo them.
    private var pendingUnread: [String: Bool] = [:]
    private var pendingRemoved = Set<String>()
    private var started = false
    private var db: MailDB?
    private var lastDataVersion = -1
    private var lastSync = Date.distantPast

    var activeFolder: Folder { folder ?? .inbox }
    var unreadCount: Int { messages.lazy.filter(\.unread).count }
    var targets: [String] { selection.isEmpty ? (current.map { [$0.id] } ?? []) : Array(selection) }

    func start() async {
        guard !started else { return }
        started = true
        await loadAccounts()
        if accounts.isEmpty { showAddAccount = true }
        Task { await loadProviders() }
        Task { await loadNotif() }
        await reload()
        Task { await sync() }
        // Watch the local database: the background sync writes, we re-read (cheap, local).
        var ticks = 0
        while !Task.isCancelled {
            try? await Task.sleep(for: .seconds(1))
            ticks += 1
            if let v = await database()?.dataVersionAsync(), v != lastDataVersion {
                lastDataVersion = v
                await reload(silent: true)
            }
            // Without the background watcher, sync from here every minute.
            if ticks % 60 == 0, notif?.running != true { await sync() }
        }
    }

    private func database() async -> MailDB? {
        if db == nil { db = MailDB() }
        return db
    }

    /// Ask the servers for news. The results land in the database, which the loop above picks up.
    func sync() async {
        guard !syncing, !accounts.isEmpty else { return }
        syncing = true
        defer { syncing = false; lastSync = .now }
        do {
            let data = try await CLI.run(["sync", "--json", "--bodies", "40"])
            if let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                errors = (obj["errors"] as? [String: String]) ?? [:]
            }
        } catch {
            banner = error.localizedDescription
        }
        await reload(silent: true)
    }

    func contacts(_ prefix: String) -> [Contact] {
        db?.contacts(prefix: prefix) ?? []
    }

    func loadAccounts() async {
        do {
            accounts = try await CLI.json([AccountInfo].self, ["account", "list", "--json"]).filter(\.enabled)
        } catch {
            banner = error.localizedDescription
        }
    }

    func loadProviders() async {
        if let p = try? await CLI.json([ProviderInfo].self, ["providers", "--json"]) { providers = p }
    }

    func detectProvider(_ email: String) async -> String? {
        try? await CLI.json(DetectResult.self, ["detect", email, "--json"]).provider
    }

    /// Returns nil on success, or an error message.
    func addAccount(name: String, email: String, provider: String, username: String, displayName: String,
                    password: String, imapHost: String, smtpHost: String) async -> String? {
        var args = ["account", "add", name, email, "--provider", provider, "--password-stdin", "--json"]
        if !username.isEmpty { args += ["--username", username] }
        if !displayName.isEmpty { args += ["--display-name", displayName] }
        if provider == "custom" { args += ["--imap-host", imapHost, "--smtp-host", smtpHost] }
        do {
            let r = try await CLI.json(LoginResult.self, args, input: password + "\n")
            guard r.ok else { return friendly(r.error, provider: provider) }
            await loadAccounts()
            await reload()
            return nil
        } catch {
            return error.localizedDescription
        }
    }

    func changePassword(_ name: String, _ password: String) async -> String? {
        do {
            let r = try await CLI.json(LoginResult.self, ["account", "password", name, "--password-stdin", "--json"], input: password + "\n")
            if r.ok { await reload() }
            return r.ok ? nil : friendly(r.error, provider: accounts.first { $0.name == name }?.provider ?? "")
        } catch {
            return error.localizedDescription
        }
    }

    func removeAccount(_ name: String) async {
        await perform { _ = try await CLI.run(["account", "remove", name, "--json"]) }
        if account == name { account = nil }
        await loadAccounts()
        await reload()
    }

    private func friendly(_ error: String?, provider: String) -> String {
        let e = error ?? "Login failed."
        let auth = e.localizedCaseInsensitiveContains("authenticat") || e.localizedCaseInsensitiveContains("credentials")
            || e.localizedCaseInsensitiveContains("login")
        guard auth else { return e }
        switch provider {
        case "gmail", "icloud", "yahoo":
            return "The server refused the password. This provider needs an app password, not your normal password.\n(\(e))"
        case "proton-bridge":
            return "The Bridge refused the password. Use the password shown in the Proton Mail Bridge app.\n(\(e))"
        default:
            return "The server refused the username or password.\n(\(e))"
        }
    }

    func loadNotif() async {
        notif = try? await CLI.json(NotifStatus.self, ["daemon", "status", "--json"])
    }

    func setNotifications(_ on: Bool) async {
        // Prefer the native agent bundled in this app (Minimail icon, action buttons).
        let agent = Bundle.main.bundleURL.appendingPathComponent("Contents/Library/LoginItems/MinimailAgent.app/Contents/MacOS/MinimailAgent").path
        var args = ["daemon", on ? "install" : "uninstall"]
        if on, FileManager.default.isExecutableFile(atPath: agent) { args += ["--agent", agent] }
        await perform { _ = try await CLI.run(args) }
        try? await Task.sleep(for: .milliseconds(500))
        await loadNotif()
    }

    func installNotifier() async {
        let brew = ["/opt/homebrew/bin/brew", "/usr/local/bin/brew"].first { FileManager.default.isExecutableFile(atPath: $0) }
        guard let brew else {
            banner = "Homebrew is not installed. Install terminal-notifier manually."
            return
        }
        busy = true
        defer { busy = false }
        await withCheckedContinuation { (c: CheckedContinuation<Void, Never>) in
            DispatchQueue.global().async {
                let p = Process()
                p.executableURL = URL(fileURLWithPath: brew)
                p.arguments = ["install", "terminal-notifier"]
                try? p.run()
                p.waitUntilExit()
                c.resume()
            }
        }
        await loadNotif()
    }

    func reload(silent: Bool = false) async {
        generation += 1
        let gen = generation
        guard let db = await database() else {
            // No cache yet: the first sync creates it.
            loading = !accounts.isEmpty
            return
        }
        let folder = search.isEmpty ? activeFolder.rawValue : nil
        let (account, unreadOnly, search) = (self.account, self.unreadOnly, self.search)
        let (list, counts) = await withCheckedContinuation { c in
            db.queue.async {
                c.resume(returning: (db.messages(folder: folder, account: account, unreadOnly: unreadOnly, search: search),
                                     db.counts()))
            }
        }
        guard gen == generation else { return }
        messages = applyPending(list)
        selection = selection.intersection(Set(messages.map(\.id)))
        loading = false
        if !silent { banner = nil }
        // Keep optimistic counts while a server call is still running.
        if inFlight == 0 {
            unread = counts
            updateBadge()
        }
    }

    func refreshCounts() async {
        await reload(silent: true)
    }

    func unreadCount(_ folder: Folder, account: String? = nil) -> Int {
        if let account { return unread[account]?[folder.rawValue] ?? 0 }
        return unread.values.reduce(0) { $0 + ($1[folder.rawValue] ?? 0) }
    }

    private func bump(_ account: String, _ folder: String, _ delta: Int) {
        unread[account, default: [:]][folder] = max(0, (unread[account]?[folder] ?? 0) + delta)
    }

    func selectAll() {
        selection = Set(messages.map(\.id))
    }

    func selectionChanged() {
        if selection.count == 1, let id = selection.first {
            if current?.id != id { Task { await open(id) } }
        } else {
            current = nil
        }
    }

    func open(_ id: String) async {
        guard let msg = messages.first(where: { $0.id == id }) else { return }
        let wasUnread = msg.unread
        if wasUnread { setLocal([id], unread: false); pendingUnread[id] = false }
        // 1. Instant: the body is usually already in the local cache.
        if let db, let cached = await withCheckedContinuation({ c in db.queue.async { c.resume(returning: db.fullMessage(msg)) } }) {
            if selection == [id] { current = cached }
            if cached.html == nil {
                // Synced before HTML was stored: fetch it once in the background, then show it.
                Task {
                    _ = try? await CLI.run(["show", "--json", id])
                    if selection == [id], let full = await withCheckedContinuation({ c in
                        db.queue.async { c.resume(returning: db.fullMessage(msg)) } }) {
                        current = full
                    }
                }
            }
            if wasUnread {
                inFlight += 1
                defer { inFlight -= 1 }
                do { _ = try await CLI.run(["mark-read", "--json", id]) } catch {
                    setLocal([id], unread: true)
                    banner = error.localizedDescription
                }
                pendingUnread[id] = nil
            }
            return
        }
        // 2. Not cached yet: fetch it from the server (this also caches it).
        loadingMessage = true
        defer { loadingMessage = false }
        inFlight += 1
        defer { inFlight -= 1 }
        var args = ["show", id, "--json"]
        if wasUnread { args.append("--mark-read") }
        do {
            var m = try await CLI.json(FullMessage.self, args)
            pendingUnread[id] = nil
            guard selection == [id] else { return }
            if let db, let full = await withCheckedContinuation({ c in
                db.queue.async { c.resume(returning: db.fullMessage(msg)) } }) {
                m = full  // the cache now also has the HTML version
            }
            current = m
        } catch {
            pendingUnread[id] = nil
            if wasUnread { setLocal([id], unread: true) }
            banner = error.localizedDescription
        }
    }

    /// minimail://open?id=<account:folder:uid> (from a notification)
    func openURL(_ url: URL) {
        guard url.scheme == "minimail",
              let id = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems?.first(where: { $0.name == "id" })?.value
        else { return }
        let parts = id.split(separator: ":")
        guard parts.count == 3, let f = Folder(rawValue: String(parts[1])) else { return }
        search = ""
        account = nil
        folder = f
        Task {
            await reload()
            selection = [id]
            selectionChanged()
        }
    }

    private func applyPending(_ list: [Message]) -> [Message] {
        list.compactMap { m in
            if pendingRemoved.contains(m.id) { return nil }
            var m = m
            if let u = pendingUnread[m.id] { m.unread = u }
            return m
        }
    }

    private func setLocal(_ ids: [String], unread: Bool) {
        let set = Set(ids)
        countsVersion += 1
        for i in messages.indices where set.contains(messages[i].id) {
            if messages[i].unread != unread {
                bump(messages[i].account, messages[i].folder, unread ? 1 : -1)
            }
            messages[i].unread = unread
        }
        updateBadge()
    }

    /// Optimistic: the UI changes at once, the server call runs after, and a failure undoes the change.
    func setRead(_ read: Bool) async {
        let ids = targets.filter { id in messages.first { $0.id == id }?.unread == read }
        guard !ids.isEmpty else { return }
        setLocal(ids, unread: !read)
        for id in ids { pendingUnread[id] = !read }
        inFlight += 1
        defer { inFlight -= 1 }
        do {
            _ = try await CLI.run([read ? "mark-read" : "mark-unread", "--json"] + ids)
        } catch {
            setLocal(ids, unread: read)
            banner = error.localizedDescription
        }
        for id in ids { pendingUnread[id] = nil }
    }

    func markAllRead() async {
        let ids = messages.filter(\.unread).map(\.id)
        let savedCounts = unread
        setLocal(ids, unread: false)
        for name in account.map({ [$0] }) ?? Array(unread.keys) { unread[name, default: [:]][activeFolder.rawValue] = 0 }
        updateBadge()
        for id in ids { pendingUnread[id] = false }
        inFlight += 1
        var args = ["mark-read", "--all", "--json", "-f", activeFolder.rawValue]
        if let account { args += ["-a", account] }
        do {
            _ = try await CLI.run(args)
        } catch {
            setLocal(ids, unread: true)
            unread = savedCounts
            updateBadge()
            banner = error.localizedDescription
        }
        inFlight -= 1
        for id in ids { pendingUnread[id] = nil }
        await refreshCounts()
    }

    func toggleSpam() async {
        guard activeFolder != .sent else { return }
        await removeOptimistically(activeFolder == .spam ? "not-spam" : "spam")
    }

    func delete() async {
        await removeOptimistically("delete")
    }

    /// Remove the targets from the list at once, then run the CLI command; put them back if it fails.
    private func removeOptimistically(_ command: String) async {
        let ids = targets
        guard !ids.isEmpty else { return }
        let set = Set(ids)
        let removed = messages.filter { set.contains($0.id) }
        let savedCounts = unread
        countsVersion += 1
        for m in removed where m.unread {
            bump(m.account, m.folder, -1)
            if command == "spam" { bump(m.account, "spam", 1) }
            if command == "not-spam" { bump(m.account, "inbox", 1) }
        }
        updateBadge()
        inFlight += 1
        messages.removeAll { set.contains($0.id) }
        pendingRemoved.formUnion(set)
        selection = []
        current = nil
        do {
            _ = try await CLI.run([command, "--json"] + ids)
        } catch {
            messages = (messages + removed).sorted { $0.ts > $1.ts }
            unread = savedCounts
            updateBadge()
            banner = error.localizedDescription
        }
        inFlight -= 1
        pendingRemoved.subtract(set)
        await refreshCounts()
    }

    private func updateBadge() {
        let total = unreadCount(.inbox)
        NSApp.dockTile.badgeLabel = total > 0 ? "\(total)" : nil
    }

    func unsubscribe(_ id: String) async {
        do {
            let r = try await CLI.json(UnsubscribeResult.self, ["unsubscribe", id, "--json"])
            switch r.method {
            case "one-click" where r.ok: banner = "Unsubscribed."
            case "email": banner = "Sent an unsubscribe email to \(r.to ?? "the sender")."
            default:
                if let u = r.url.flatMap(URL.init(string:)) {
                    NSWorkspace.shared.open(u)
                    banner = "Opened the unsubscribe page in your browser."
                } else {
                    banner = r.error ?? "No unsubscribe link in this email."
                }
            }
        } catch {
            banner = error.localizedDescription
        }
    }

    func newMessage() {
        compose = Draft(account: account ?? accounts.first?.name ?? "")
    }

    func reply() {
        guard let m = current else { return }
        let subject = m.subject.lowercased().hasPrefix("re:") ? m.subject : "Re: \(m.subject)"
        compose = Draft(account: m.account, to: m.reply_to.isEmpty ? m.from : m.reply_to, subject: subject, replyTo: m.id)
    }

    func forward() {
        guard let m = current else { return }
        let s = m.subject
        let subject = ["fwd:", "fw:", "tr:"].contains(where: { s.lowercased().hasPrefix($0) }) ? s : "Fwd: \(s)"
        compose = Draft(account: m.account, subject: subject, forwardOf: m.id)
    }

    func send(_ d: Draft) async throws {
        var args = ["send", "--json", "-a", d.account, "--subject", d.subject]
        for addr in d.to.split(separator: ",") {
            let t = addr.trimmingCharacters(in: .whitespaces)
            if !t.isEmpty { args += ["--to", t] }
        }
        if let r = d.replyTo { args += ["--reply", r] }
        if let f = d.forwardOf { args += ["--forward", f] }
        _ = try await CLI.run(args, input: d.body)
    }

    private func perform(_ work: @escaping () async throws -> Void) async {
        busy = true
        defer { busy = false }
        do { try await work() } catch { banner = error.localizedDescription }
    }
}
