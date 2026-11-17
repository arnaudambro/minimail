import SwiftUI

@main
struct MinimailApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var delegate
    @StateObject private var store = Store()

    var body: some Scene {
        Window("Minimail", id: "main") {
            ContentView()
                .environmentObject(store)
                .onOpenURL { store.openURL($0) }
                .frame(minWidth: 860, minHeight: 500)
                .task { await store.start() }
        }
        Settings {
            SettingsView().environmentObject(store)
        }
        .commands {
            CommandGroup(replacing: .newItem) {
                Button("New Message") { store.newMessage() }.keyboardShortcut("n")
                Button("Add Account…") { store.showAddAccount = true }
            }
            CommandGroup(replacing: .pasteboard) {
                Button("Cut") { NSApp.sendAction(#selector(NSText.cut(_:)), to: nil, from: nil) }.keyboardShortcut("x")
                Button("Copy") { NSApp.sendAction(#selector(NSText.copy(_:)), to: nil, from: nil) }.keyboardShortcut("c")
                Button("Paste") { NSApp.sendAction(#selector(NSText.paste(_:)), to: nil, from: nil) }.keyboardShortcut("v")
                Button("Select All") {
                    // In a text field (search, compose): select its text. Elsewhere: select every message.
                    let w = NSApp.keyWindow
                    if w?.firstResponder is NSText || w == nil || w?.sheetParent != nil
                        || w?.identifier?.rawValue.localizedCaseInsensitiveContains("settings") == true {
                        NSApp.sendAction(#selector(NSText.selectAll(_:)), to: nil, from: nil)
                    } else {
                        store.selectAll()
                    }
                }.keyboardShortcut("a")
            }
            CommandMenu("Message") {
                Button("Reply") { store.reply() }.keyboardShortcut("r").disabled(store.current == nil)
                Divider()
                Button("Mark as Read") { Task { await store.setRead(true) } }.keyboardShortcut("u", modifiers: [.command, .shift])
                Button("Mark as Unread") { Task { await store.setRead(false) } }.keyboardShortcut("u", modifiers: [.command, .option])
                Button("Mark All as Read") { Task { await store.markAllRead() } }.keyboardShortcut("k", modifiers: [.command, .shift])
                Button(store.activeFolder == .spam ? "Not Spam" : "Move to Spam") { Task { await store.toggleSpam() } }
                    .keyboardShortcut("j", modifiers: [.command, .shift])
                Button("Delete") { Task { await store.delete() } }
                    .keyboardShortcut(.delete, modifiers: .command)
                Divider()
                Button("Get New Mail") { Task { await store.sync() } }.keyboardShortcut("n", modifiers: [.command, .shift])
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
        .sheet(isPresented: $store.showAddAccount) {
            AddAccountView().environmentObject(store)
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
                        .badge(f == .sent ? 0 : store.unreadCount(f, account: store.account))
                        .tag(f)
                }
            }
            Section("Accounts") {
                accountRow(nil, title: "All Accounts", count: store.unreadCount(.inbox))
                ForEach(store.accounts) { a in
                    accountRow(a.name, title: a.email, count: store.unreadCount(.inbox, account: a.name))
                }
            }
        }
        .listStyle(.sidebar)
        .safeAreaInset(edge: .bottom) { SidebarFooter() }
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

struct SidebarFooter: View {
    @EnvironmentObject var store: Store

    var body: some View {
        HStack(spacing: 14) {
            Button { store.showAddAccount = true } label: { Image(systemName: "plus") }
                .help("Add an account")
            Button {
                Task { await store.setNotifications(!(store.notif?.installed ?? false)) }
            } label: {
                Image(systemName: store.notif?.installed == true ? "bell.fill" : "bell.slash")
                    .foregroundStyle(store.notif?.installed == true ? Color.accentColor : .secondary)
            }
            .help(store.notif?.installed == true
                  ? "Notifications are on. Click to turn them off."
                  : "Notifications are off. Click to get a notification for new mail.")
            .disabled(store.notif == nil || store.busy)
            Spacer()
            SettingsLink { Image(systemName: "gearshape") }
                .help("Settings: accounts and notifications")
        }
        .buttonStyle(.borderless)
        .font(.system(size: 14))
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
    }
}

// MARK: - Message list

struct MessageList: View {
    @EnvironmentObject var store: Store

    var body: some View {
        List(store.messages, selection: $store.selection) { m in
            MessageRow(message: m, showAccount: store.account == nil && store.accounts.count > 1,
                       showFolder: !store.search.isEmpty, folder: Folder(rawValue: m.folder) ?? store.activeFolder)
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
                    Divider()
                    Button("Delete", role: .destructive) {
                        if !store.selection.contains(m.id) { store.selection = [m.id] }
                        Task { await store.delete() }
                    }
                }
        }
        .listStyle(.inset)
        .onDeleteCommand { Task { await store.delete() } }
        .onChange(of: store.selection) { store.selectionChanged() }
        .overlay {
            if store.accounts.isEmpty && !store.loading {
                ContentUnavailableView {
                    Label("Welcome to Minimail", systemImage: "envelope")
                } description: {
                    Text("Add your first email account to start.")
                } actions: {
                    Button("Add Account") { store.showAddAccount = true }.buttonStyle(.borderedProminent)
                }
            } else if store.messages.isEmpty && !store.search.isEmpty {
                ContentUnavailableView.search(text: store.search)
            } else if store.messages.isEmpty && !store.loading {
                ContentUnavailableView(store.unreadOnly ? "No Unread Mail" : "No Mail", systemImage: store.activeFolder.icon)
            }
        }
        .safeAreaInset(edge: .bottom) { StatusBar() }
        .searchable(text: $store.search, placement: .toolbar, prompt: "Search all mail")
        .navigationTitle(store.search.isEmpty ? store.activeFolder.title : "Search")
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
                Button { Task { await store.delete() } } label: { Label("Delete", systemImage: "trash") }
                    .help("Move to Trash (⌫)")
                    .disabled(store.targets.isEmpty)
                Toggle(isOn: $store.unreadOnly) {
                    Label("Unread Only", systemImage: "line.3.horizontal.decrease.circle")
                }
                .help("Show unread only")
                Button { Task { await store.sync() } } label: {
                    if store.syncing {
                        ProgressView().controlSize(.small)
                    } else {
                        Label("Get New Mail", systemImage: "arrow.clockwise")
                    }
                }
                .help("Get new mail (⇧⌘N). Minimail also checks by itself.")
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
    var showFolder = false
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
                if showAccount || showFolder {
                    Text([showAccount ? message.account : nil, showFolder ? folder.title : nil]
                        .compactMap { $0 }.joined(separator: " · "))
                        .font(.caption2).foregroundStyle(.tertiary)
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
                        Button("Delete", role: .destructive) { Task { await store.delete() } }
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

struct UnsubscribeButton: View {
    @EnvironmentObject var store: Store
    let id: String
    @State private var working = false

    var body: some View {
        Button {
            Task { working = true; await store.unsubscribe(id); working = false }
        } label: {
            if working { ProgressView().controlSize(.mini) } else { Text("Unsubscribe") }
        }
        .controlSize(.small)
        .disabled(working)
        .help("Unsubscribe from this mailing list")
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
                    if message.unsubscribe == true {
                        UnsubscribeButton(id: message.id)
                    }
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
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyy-MM-dd'T'HH:mmXXXXX"
        guard let d = ISO8601DateFormatter.flexible.date(from: message.date) ?? ISO8601DateFormatter().date(from: message.date)
                ?? f.date(from: message.date) else { return message.date }
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
                AddressField(text: $draft.to)
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


// MARK: - Address autocomplete

/// "To" field that suggests addresses found in your mail (people you write to come first).
struct AddressField: View {
    @EnvironmentObject var store: Store
    @Binding var text: String
    @State private var suggestions: [Contact] = []
    @State private var highlighted = 0
    @FocusState private var focused: Bool

    private var token: String {
        String(text.split(separator: ",", omittingEmptySubsequences: false).last ?? "").trimmingCharacters(in: .whitespaces)
    }

    var body: some View {
        TextField("To", text: $text, prompt: Text("name@example.com, …"))
            .focused($focused)
            .onChange(of: text) { refresh() }
            .onKeyPress(.downArrow) { move(1) }
            .onKeyPress(.upArrow) { move(-1) }
            .onKeyPress(.tab) { pick() }
            .onKeyPress(.return) { pick() }
            .onKeyPress(.escape) {
                guard !suggestions.isEmpty else { return .ignored }
                suggestions = []
                return .handled
            }
        if focused {
            ForEach(Array(suggestions.enumerated()), id: \.element) { i, c in
                HStack {
                    Text(c.name.isEmpty ? c.email : c.name).lineLimit(1)
                    if !c.name.isEmpty { Text(c.email).foregroundStyle(.secondary).lineLimit(1) }
                    Spacer()
                }
                .padding(.vertical, 2)
                .padding(.horizontal, 6)
                .background(i == highlighted ? Color.accentColor.opacity(0.2) : .clear, in: RoundedRectangle(cornerRadius: 5))
                .contentShape(Rectangle())
                .onTapGesture { choose(c) }
            }
        }
    }

    private func refresh() {
        let t = token
        suggestions = t.count >= 1 && !t.contains("<") ? store.contacts(t).filter { !text.contains($0.email) } : []
        highlighted = 0
    }

    private func move(_ d: Int) -> KeyPress.Result {
        guard !suggestions.isEmpty else { return .ignored }
        highlighted = (highlighted + d + suggestions.count) % suggestions.count
        return .handled
    }

    private func pick() -> KeyPress.Result {
        guard suggestions.indices.contains(highlighted) else { return .ignored }
        choose(suggestions[highlighted])
        return .handled
    }

    private func choose(_ c: Contact) {
        var parts = text.split(separator: ",", omittingEmptySubsequences: false).map { $0.trimmingCharacters(in: .whitespaces) }
        if parts.isEmpty { parts = [""] }
        parts[parts.count - 1] = c.formatted
        text = parts.filter { !$0.isEmpty }.joined(separator: ", ") + ", "
        suggestions = []
    }
}
