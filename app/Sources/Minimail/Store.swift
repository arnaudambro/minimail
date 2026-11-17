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
    @Published var accounts: [AccountInfo] = []
    @Published var messages: [Message] = []
    @Published var selection = Set<String>()
    @Published var current: FullMessage?
    @Published var loading = false
    @Published var loadingMessage = false
    @Published var busy = false
    @Published var errors: [String: String] = [:]
    @Published var banner: String?
    @Published var inboxUnread: [String: Int] = [:]
    @Published var compose: Draft?
    @Published var showAddAccount = false
    @Published var providers: [ProviderInfo] = []
    @Published var notif: NotifStatus?

    private var generation = 0
    // Optimistic changes not yet confirmed by the server. A reload must not undo them.
    private var pendingUnread: [String: Bool] = [:]
    private var pendingRemoved = Set<String>()
    private var started = false

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
        while !Task.isCancelled {
            try? await Task.sleep(for: .seconds(90))
            await reload(silent: true)
        }
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
        await perform { _ = try await CLI.run(["daemon", on ? "install" : "uninstall"]) }
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
        if !silent { loading = true }
        var args = ["list", "--json", "-f", activeFolder.rawValue, "-n", "40"]
        if let account { args += ["-a", account] }
        if unreadOnly { args.append("--unread") }
        do {
            let r = try await CLI.json(ListResponse.self, args)
            guard gen == generation else { return }
            messages = applyPending(r.messages)
            errors = r.errors
            selection = selection.intersection(Set(r.messages.map(\.id)))
            if !silent { banner = nil }
        } catch {
            guard gen == generation else { return }
            banner = error.localizedDescription
        }
        loading = false
        await refreshCounts()
    }

    func refreshCounts() async {
        guard let data = try? await CLI.run(["counts", "--json"]),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let accts = obj["accounts"] as? [String: Any] else { return }
        var counts: [String: Int] = [:]
        for (name, v) in accts {
            if let inbox = (v as? [String: Any])?["inbox"] as? [String: Any], let n = inbox["unread"] as? Int {
                counts[name] = n
            }
        }
        inboxUnread = counts
        let total = counts.values.reduce(0, +)
        NSApp.dockTile.badgeLabel = total > 0 ? "\(total)" : nil
    }

    func selectionChanged() {
        if selection.count == 1, let id = selection.first {
            if current?.id != id { Task { await open(id) } }
        } else {
            current = nil
        }
    }

    func open(_ id: String) async {
        loadingMessage = true
        defer { loadingMessage = false }
        let wasUnread = messages.first { $0.id == id }?.unread ?? false
        if wasUnread { setLocal([id], unread: false); pendingUnread[id] = false }
        var args = ["show", id, "--json"]
        if wasUnread { args.append("--mark-read") }
        do {
            let m = try await CLI.json(FullMessage.self, args)
            pendingUnread[id] = nil
            guard selection == [id] else { return }
            current = m
        } catch {
            pendingUnread[id] = nil
            if wasUnread { setLocal([id], unread: true) }
            banner = error.localizedDescription
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
        for i in messages.indices where set.contains(messages[i].id) {
            if messages[i].unread != unread, messages[i].folder == "inbox" {
                inboxUnread[messages[i].account, default: 0] += unread ? 1 : -1
            }
            messages[i].unread = unread
        }
        let total = inboxUnread.values.reduce(0, +)
        NSApp.dockTile.badgeLabel = total > 0 ? "\(total)" : nil
    }

    /// Optimistic: the UI changes at once, the server call runs after, and a failure undoes the change.
    func setRead(_ read: Bool) async {
        let ids = targets.filter { id in messages.first { $0.id == id }?.unread == read }
        guard !ids.isEmpty else { return }
        setLocal(ids, unread: !read)
        for id in ids { pendingUnread[id] = !read }
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
        let savedCounts = inboxUnread
        setLocal(ids, unread: false)
        if activeFolder == .inbox {
            for name in account.map({ [$0] }) ?? Array(inboxUnread.keys) { inboxUnread[name] = 0 }
            updateBadge()
        }
        for id in ids { pendingUnread[id] = false }
        var args = ["mark-read", "--all", "--json", "-f", activeFolder.rawValue]
        if let account { args += ["-a", account] }
        do {
            _ = try await CLI.run(args)
        } catch {
            setLocal(ids, unread: true)
            inboxUnread = savedCounts
            updateBadge()
            banner = error.localizedDescription
        }
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
        let savedCounts = inboxUnread
        for m in removed where m.unread && m.folder == "inbox" { inboxUnread[m.account, default: 1] -= 1 }
        updateBadge()
        messages.removeAll { set.contains($0.id) }
        pendingRemoved.formUnion(set)
        selection = []
        current = nil
        do {
            _ = try await CLI.run([command, "--json"] + ids)
        } catch {
            messages = (messages + removed).sorted { $0.ts > $1.ts }
            inboxUnread = savedCounts
            updateBadge()
            banner = error.localizedDescription
        }
        pendingRemoved.subtract(set)
        await refreshCounts()
    }

    private func updateBadge() {
        let total = inboxUnread.values.reduce(0, +)
        NSApp.dockTile.badgeLabel = total > 0 ? "\(total)" : nil
    }

    func newMessage() {
        compose = Draft(account: account ?? accounts.first?.name ?? "")
    }

    func reply() {
        guard let m = current else { return }
        let subject = m.subject.lowercased().hasPrefix("re:") ? m.subject : "Re: \(m.subject)"
        compose = Draft(account: m.account, to: m.reply_to.isEmpty ? m.from : m.reply_to, subject: subject, replyTo: m.id)
    }

    func send(_ d: Draft) async throws {
        var args = ["send", "--json", "-a", d.account, "--subject", d.subject]
        for addr in d.to.split(separator: ",") {
            let t = addr.trimmingCharacters(in: .whitespaces)
            if !t.isEmpty { args += ["--to", t] }
        }
        if let r = d.replyTo { args += ["--reply", r] }
        _ = try await CLI.run(args, input: d.body)
    }

    private func perform(_ work: @escaping () async throws -> Void) async {
        busy = true
        defer { busy = false }
        do { try await work() } catch { banner = error.localizedDescription }
    }
}
