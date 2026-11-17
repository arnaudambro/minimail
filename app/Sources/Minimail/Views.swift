import SwiftUI

@main
struct MinimailApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var delegate
    @StateObject private var store = Store()

    var body: some Scene {
        Window("Minimail", id: "main") {
            ContentView()
                .environmentObject(store)
                .frame(minWidth: 860, minHeight: 500)
                .task { await store.start() }
        }
        .commands {
            CommandGroup(replacing: .newItem) {
                Button("New Message") { store.newMessage() }.keyboardShortcut("n")
            }
            CommandMenu("Message") {
                Button("Reply") { store.reply() }.keyboardShortcut("r").disabled(store.current == nil)
                Divider()
                Button("Mark as Read") { Task { await store.setRead(true) } }.keyboardShortcut("u", modifiers: [.command, .shift])
                Button("Mark as Unread") { Task { await store.setRead(false) } }.keyboardShortcut("u", modifiers: [.command, .option])
                Button("Mark All as Read") { Task { await store.markAllRead() } }.keyboardShortcut("k", modifiers: [.command, .shift])
                Button(store.activeFolder == .spam ? "Not Spam" : "Move to Spam") { Task { await store.toggleSpam() } }
                    .keyboardShortcut("j", modifiers: [.command, .shift])
                Divider()
                Button("Refresh") { Task { await store.reload() } }.keyboardShortcut("n", modifiers: [.command, .shift])
            }
        }
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
}

struct ContentView: View {
    @EnvironmentObject var store: Store

    var body: some View {
        NavigationSplitView {
            Sidebar().navigationSplitViewColumnWidth(min: 190, ideal: 220)
        } content: {
            MessageList().navigationSplitViewColumnWidth(min: 300, ideal: 380)
        } detail: {
            DetailView()
        }
        .sheet(item: $store.compose) { draft in
            ComposeView(draft: draft).environmentObject(store)
        }
    }
}

// MARK: - Sidebar

struct Sidebar: View {
    @EnvironmentObject var store: Store

    var body: some View {
        List(selection: $store.folder) {
            Section("Mailboxes") {
                ForEach(Folder.allCases) { f in
                    Label(f.title, systemImage: f.icon)
                        .badge(f == .inbox ? inboxBadge : 0)
                        .tag(f)
                }
            }
            Section("Accounts") {
                accountRow(nil, title: "All Accounts", count: store.inboxUnread.values.reduce(0, +))
                ForEach(store.accounts) { a in
                    accountRow(a.name, title: a.email, count: store.inboxUnread[a.name] ?? 0)
                }
            }
        }
        .listStyle(.sidebar)
    }

    private var inboxBadge: Int {
        store.account.map { store.inboxUnread[$0] ?? 0 } ?? store.inboxUnread.values.reduce(0, +)
    }

    private func accountRow(_ name: String?, title: String, count: Int) -> some View {
        let active = store.account == name
        return HStack {
            Image(systemName: active ? "largecircle.fill.circle" : "circle")
                .foregroundStyle(active ? Color.accentColor : .secondary)
                .font(.caption)
            Text(title).lineLimit(1).truncationMode(.middle)
            Spacer()
            if store.errors[name ?? ""] != nil {
                Image(systemName: "exclamationmark.triangle").foregroundStyle(.orange).help(store.errors[name ?? ""] ?? "")
            } else if count > 0 {
                Text("\(count)").foregroundStyle(.secondary).font(.callout)
            }
        }
        .contentShape(Rectangle())
        .onTapGesture { store.account = name }
    }
}

// MARK: - Message list

struct MessageList: View {
    @EnvironmentObject var store: Store

    var body: some View {
        List(store.messages, selection: $store.selection) { m in
            MessageRow(message: m, showAccount: store.account == nil && store.accounts.count > 1, folder: store.activeFolder)
                .tag(m.id)
                .contextMenu {
                    Button(m.unread ? "Mark as Read" : "Mark as Unread") {
                        if !store.selection.contains(m.id) { store.selection = [m.id] }
                        Task { await store.setRead(m.unread) }
                    }
                    if store.activeFolder != .sent {
                        Button(store.activeFolder == .spam ? "Not Spam" : "Move to Spam") {
                            if !store.selection.contains(m.id) { store.selection = [m.id] }
                            Task { await store.toggleSpam() }
                        }
                    }
                }
        }
        .listStyle(.inset)
        .onChange(of: store.selection) { store.selectionChanged() }
        .overlay {
            if store.messages.isEmpty && !store.loading {
                ContentUnavailableView(store.unreadOnly ? "No Unread Mail" : "No Mail", systemImage: store.activeFolder.icon)
            }
        }
        .safeAreaInset(edge: .bottom) { StatusBar() }
        .navigationTitle(store.activeFolder.title)
        .navigationSubtitle(subtitle)
        .toolbar {
            ToolbarItemGroup {
                Button { store.newMessage() } label: { Label("New Message", systemImage: "square.and.pencil") }
                    .help("New message (⌘N)")
                Button { Task { await store.setRead(true) } } label: { Label("Mark as Read", systemImage: "envelope.open") }
                    .help("Mark selection as read (⇧⌘U)")
                    .disabled(store.targets.isEmpty)
                Button { Task { await store.markAllRead() } } label: { Label("Mark All as Read", systemImage: "checkmark.circle") }
                    .help("Mark every message in this mailbox as read (⇧⌘K)")
                if store.activeFolder != .sent {
                    Button { Task { await store.toggleSpam() } } label: {
                        Label(store.activeFolder == .spam ? "Not Spam" : "Spam",
                              systemImage: store.activeFolder == .spam ? "tray.and.arrow.up" : "xmark.bin")
                    }
                    .help(store.activeFolder == .spam ? "Move back to Inbox (⇧⌘J)" : "Move to Spam (⇧⌘J)")
                    .disabled(store.targets.isEmpty)
                }
                Toggle(isOn: $store.unreadOnly) {
                    Label("Unread Only", systemImage: "line.3.horizontal.decrease.circle")
                }
                .help("Show unread only")
                Button { Task { await store.reload() } } label: {
                    if store.loading || store.busy {
                        ProgressView().controlSize(.small)
                    } else {
                        Label("Refresh", systemImage: "arrow.clockwise")
                    }
                }
                .help("Refresh (⇧⌘N)")
            }
        }
    }

    private var subtitle: String {
        let n = store.unreadCount
        let who = store.account.flatMap { a in store.accounts.first { $0.name == a }?.email } ?? "All accounts"
        return n > 0 ? "\(who) — \(n) unread" : who
    }
}

struct MessageRow: View {
    let message: Message
    let showAccount: Bool
    let folder: Folder

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            Circle()
                .fill(message.unread ? Color.accentColor : .clear)
                .frame(width: 8, height: 8)
                .padding(.top, 5)
            VStack(alignment: .leading, spacing: 2) {
                HStack(alignment: .firstTextBaseline) {
                    Text(senderName(folder == .sent ? message.to : message.from))
                        .fontWeight(message.unread ? .semibold : .regular)
                        .lineLimit(1)
                    Spacer(minLength: 6)
                    Text(shortDate(message.ts)).font(.caption).foregroundStyle(.secondary)
                }
                Text(message.subject.isEmpty ? "(no subject)" : message.subject)
                    .font(.callout)
                    .foregroundStyle(message.unread ? .primary : .secondary)
                    .lineLimit(1)
                if showAccount {
                    Text(message.account).font(.caption2).foregroundStyle(.tertiary)
                }
            }
        }
        .padding(.vertical, 3)
    }
}

struct StatusBar: View {
    @EnvironmentObject var store: Store

    var body: some View {
        if store.banner != nil || !store.errors.isEmpty {
            VStack(alignment: .leading, spacing: 2) {
                if let b = store.banner {
                    Text(b).foregroundStyle(.red)
                }
                ForEach(store.errors.sorted(by: { $0.key < $1.key }), id: \.key) { k, v in
                    Text("\(k): \(v)").foregroundStyle(.orange)
                }
            }
            .font(.caption)
            .lineLimit(2)
            .textSelection(.enabled)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(8)
            .background(.bar)
        }
    }
}

// MARK: - Detail

struct DetailView: View {
    @EnvironmentObject var store: Store

    var body: some View {
        Group {
            if store.selection.count > 1 {
                VStack(spacing: 14) {
                    Image(systemName: "envelope.badge").font(.system(size: 40)).foregroundStyle(.secondary)
                    Text("\(store.selection.count) messages selected").font(.title3)
                    HStack {
                        Button("Mark as Read") { Task { await store.setRead(true) } }
                        Button("Mark as Unread") { Task { await store.setRead(false) } }
                        if store.activeFolder != .sent {
                            Button(store.activeFolder == .spam ? "Not Spam" : "Move to Spam") { Task { await store.toggleSpam() } }
                        }
                    }
                }
            } else if let m = store.current {
                MessageView(message: m)
            } else if store.loadingMessage {
                ProgressView()
            } else {
                Text("No Message Selected").foregroundStyle(.secondary)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .toolbar {
            ToolbarItem {
                Button { store.reply() } label: { Label("Reply", systemImage: "arrowshape.turn.up.left") }
                    .help("Reply (⌘R)")
                    .disabled(store.current == nil)
            }
        }
    }
}

struct MessageView: View {
    let message: FullMessage

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 10) {
                Text(message.subject.isEmpty ? "(no subject)" : message.subject)
                    .font(.title2.weight(.semibold))
                HStack(alignment: .firstTextBaseline) {
                    Text(senderName(message.from)).font(.headline)
                    Text(senderEmail(message.from)).foregroundStyle(.secondary)
                    Spacer()
                    Text(formattedDate).font(.caption).foregroundStyle(.secondary)
                }
                VStack(alignment: .leading, spacing: 2) {
                    Text("To: \(message.to)")
                    if !message.cc.isEmpty { Text("Cc: \(message.cc)") }
                    Text("Account: \(message.account)")
                }
                .font(.caption)
                .foregroundStyle(.secondary)
                if !message.attachments.isEmpty {
                    Label(message.attachments.map { $0.filename ?? $0.type }.joined(separator: ", "), systemImage: "paperclip")
                        .font(.caption)
                }
                Divider()
                Text(linkified(message.body))
                    .font(.body)
                    .lineSpacing(3)
            }
            .textSelection(.enabled)
            .padding(24)
            .frame(maxWidth: 760, alignment: .leading)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .id(message.id)
    }

    private var formattedDate: String {
        guard let d = ISO8601DateFormatter.flexible.date(from: message.date) else { return message.date }
        return d.formatted(date: .abbreviated, time: .shortened)
    }
}

extension ISO8601DateFormatter {
    static let flexible: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withFullDate, .withTime, .withColonSeparatorInTime, .withDashSeparatorInDate, .withTimeZone]
        return f
    }()
}

// MARK: - Compose

struct ComposeView: View {
    @EnvironmentObject var store: Store
    @Environment(\.dismiss) private var dismiss
    @State var draft: Draft
    @State private var sending = false
    @State private var error: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Form {
                Picker("From", selection: $draft.account) {
                    ForEach(store.accounts) { a in Text(a.email).tag(a.name) }
                }
                TextField("To", text: $draft.to, prompt: Text("name@example.com, …"))
                TextField("Subject", text: $draft.subject)
            }
            .padding()
            TextEditor(text: $draft.body)
                .font(.body)
                .scrollContentBackground(.hidden)
                .padding(.horizontal, 12)
                .frame(minHeight: 260)
            if draft.replyTo != nil {
                Text("The original message is quoted below your text.")
                    .font(.caption).foregroundStyle(.secondary).padding(.horizontal)
            }
            if let error {
                Text(error).font(.caption).foregroundStyle(.red).padding(.horizontal).textSelection(.enabled)
            }
            HStack {
                Spacer()
                Button("Cancel") { dismiss() }.keyboardShortcut(.cancelAction)
                Button {
                    Task { await send() }
                } label: {
                    if sending { ProgressView().controlSize(.small) } else { Text("Send") }
                }
                .keyboardShortcut(.return, modifiers: .command)
                .disabled(sending || draft.to.trimmingCharacters(in: .whitespaces).isEmpty || draft.account.isEmpty)
            }
            .padding()
        }
        .frame(width: 620, height: 520)
    }

    private func send() async {
        sending = true
        defer { sending = false }
        do {
            try await store.send(draft)
            dismiss()
        } catch {
            self.error = error.localizedDescription
        }
    }
}
