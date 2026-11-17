import SwiftUI

// MARK: - Settings window (⌘,)

struct SettingsView: View {
    var body: some View {
        TabView {
            AccountsSettings().tabItem { Label("Accounts", systemImage: "at") }
            NotificationSettings().tabItem { Label("Notifications", systemImage: "bell") }
            PrivacySettings().tabItem { Label("Privacy", systemImage: "hand.raised") }
        }
        .frame(width: 560, height: 400)
    }
}

struct AccountsSettings: View {
    @EnvironmentObject var store: Store
    @State private var selected: String?
    @State private var confirmRemove = false
    @State private var passwordFor: AccountInfo?
    @State private var showAdd = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            List(store.accounts, selection: $selected) { a in
                HStack {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(a.email)
                        Text(store.providers.first { $0.key == a.provider }?.label ?? a.provider)
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    Spacer()
                    if let err = store.errors[a.name] {
                        Label("Login failed", systemImage: "exclamationmark.triangle.fill")
                            .font(.caption).foregroundStyle(.orange).help(err)
                    }
                }
                .tag(a.name)
                .padding(.vertical, 2)
            }
            .listStyle(.bordered(alternatesRowBackgrounds: true))
            .overlay {
                if store.accounts.isEmpty {
                    Text("No accounts yet.").foregroundStyle(.secondary)
                }
            }
            HStack(spacing: 8) {
                Button { showAdd = true } label: { Image(systemName: "plus") }
                    .help("Add an account")
                Button { confirmRemove = true } label: { Image(systemName: "minus") }
                    .help("Remove the selected account")
                    .disabled(selected == nil)
                Spacer()
                Button("Change Password…") { passwordFor = store.accounts.first { $0.name == selected } }
                    .disabled(selected == nil)
            }
        }
        .padding()
        .confirmationDialog("Remove this account from Minimail?", isPresented: $confirmRemove) {
            Button("Remove", role: .destructive) {
                if let s = selected { Task { await store.removeAccount(s) } }
                selected = nil
            }
        } message: {
            Text("Your emails stay on the server. Only Minimail forgets the account and its password.")
        }
        .sheet(isPresented: $showAdd) {
            AddAccountView().environmentObject(store)
        }
        .sheet(item: $passwordFor) { a in
            ChangePasswordView(account: a).environmentObject(store)
        }
    }
}

struct NotificationSettings: View {
    @EnvironmentObject var store: Store

    var body: some View {
        Form {
            Section {
                Toggle("Notify me when new mail arrives", isOn: Binding(
                    get: { store.notif?.installed ?? false },
                    set: { on in Task { await store.setNotifications(on) } }
                ))
                .disabled(store.notif == nil || store.busy)
                Text("Click a notification to mark the email as read. Notifications keep working when Minimail is closed, and start automatically when you log in.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            if let n = store.notif {
                Section {
                    LabeledContent("Status", value: n.running ? "Running" : (n.installed ? "Starting…" : "Off"))
                    if !n.notifier {
                        HStack {
                            Label("A helper (terminal-notifier) is missing.", systemImage: "exclamationmark.triangle")
                                .foregroundStyle(.orange)
                            Spacer()
                            Button("Install") { Task { await store.installNotifier() } }
                                .disabled(store.busy)
                        }
                    }
                }
            }
        }
        .formStyle(.grouped)
        .task { await store.loadNotif() }
    }
}

struct PrivacySettings: View {
    @AppStorage("loadRemoteContent") private var loadRemote = false

    var body: some View {
        Form {
            Section {
                Toggle("Always load images in emails", isOn: $loadRemote)
                Text("Images in emails often come from the sender's server. When they load, the sender can see that you opened the email, when, and your IP address. When this is off, click \"Load Images\" on an email to show its images.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Section {
                Text("Emails never run code in Minimail: JavaScript, forms and page changes are blocked. Links open in your browser.")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
        .formStyle(.grouped)
    }
}

// MARK: - Add account

struct AddAccountView: View {
    @EnvironmentObject var store: Store
    @Environment(\.dismiss) private var dismiss

    @State private var email = ""
    @State private var provider = "gmail"
    @State private var providerTouched = false
    @State private var settingProvider = false
    @State private var username = ""
    @State private var password = ""
    @State private var displayName = ""
    @State private var imapHost = ""
    @State private var smtpHost = ""
    @State private var working = false
    @State private var error: String?

    private var help: String { store.providers.first { $0.key == provider }?.help ?? "" }
    private var validEmail: Bool { email.contains("@") && email.split(separator: "@").last?.contains(".") == true }
    private var canAdd: Bool {
        validEmail && !password.isEmpty && !working && (provider != "custom" || (!imapHost.isEmpty && !smtpHost.isEmpty))
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text("Add Account").font(.title2.weight(.semibold)).padding([.horizontal, .top], 20)
            Form {
                TextField("Email", text: $email, prompt: Text("name@example.com"))
                    .textContentType(.emailAddress)
                Picker("Provider", selection: $provider) {
                    ForEach(store.providers) { p in Text(p.label).tag(p.key) }
                }
                .onChange(of: provider) { if !settingProvider { providerTouched = true } }
                if !help.isEmpty {
                    Text(linkified(help)).font(.caption).foregroundStyle(.secondary)
                }
                if provider == "custom" {
                    TextField("IMAP server", text: $imapHost, prompt: Text("imap.example.com"))
                    TextField("SMTP server", text: $smtpHost, prompt: Text("smtp.example.com"))
                }
                TextField("Username", text: $username, prompt: Text(email.isEmpty ? "Same as email" : email))
                SecureField("Password", text: $password)
                TextField("Your name", text: $displayName, prompt: Text("Optional, shown to people you write to"))
            }
            .formStyle(.grouped)
            .scrollDisabled(true)
            .frame(height: provider == "custom" ? 400 : 300)
            if let error {
                Text(error)
                    .font(.callout).foregroundStyle(.red)
                    .textSelection(.enabled)
                    .padding(.horizontal, 20)
                    .fixedSize(horizontal: false, vertical: true)
            }
            HStack {
                if working {
                    ProgressView().controlSize(.small)
                    Text("Connecting…").foregroundStyle(.secondary)
                }
                Spacer()
                Button("Cancel") { dismiss() }.keyboardShortcut(.cancelAction)
                Button("Add Account") { Task { await add() } }
                    .keyboardShortcut(.defaultAction)
                    .disabled(!canAdd)
            }
            .padding(20)
        }
        .frame(width: 520)
        .task(id: email) {
            guard validEmail, !providerTouched else { return }
            try? await Task.sleep(for: .milliseconds(500))
            guard !Task.isCancelled, let p = await store.detectProvider(email.trimmingCharacters(in: .whitespaces)),
                  !providerTouched else { return }
            settingProvider = true
            provider = p
            DispatchQueue.main.async { settingProvider = false }
        }
        .task { if store.providers.isEmpty { await store.loadProviders() } }
    }

    private func shortName() -> String {
        let e = email.lowercased().trimmingCharacters(in: .whitespaces)
        let parts = e.split(separator: "@")
        let local = String(parts.first ?? "mail")
        let domain = String(parts.last ?? "").split(separator: ".").first.map(String.init) ?? ""
        let generic: Set<String> = ["gmail", "googlemail", "icloud", "me", "mac", "yahoo", "outlook", "hotmail", "live", "proton", "protonmail", "pm"]
        var base = generic.contains(domain) || domain.isEmpty ? local : domain
        base = String(base.map { $0.isLetter || $0.isNumber || "._-".contains($0) ? $0 : "-" })
        base = base.trimmingCharacters(in: CharacterSet(charactersIn: "._-"))
        if base.isEmpty || !(base.first!.isLetter || base.first!.isNumber) { base = "mail" }
        var name = base, i = 2
        let taken = Set(store.accounts.map(\.name))
        while taken.contains(name) { name = "\(base)\(i)"; i += 1 }
        return name
    }

    private func add() async {
        working = true
        error = nil
        let err = await store.addAccount(
            name: shortName(), email: email.trimmingCharacters(in: .whitespaces), provider: provider,
            username: username.trimmingCharacters(in: .whitespaces), displayName: displayName,
            password: password, imapHost: imapHost, smtpHost: smtpHost)
        working = false
        if let err { error = err } else { dismiss() }
    }
}

// MARK: - Change password

struct ChangePasswordView: View {
    @EnvironmentObject var store: Store
    @Environment(\.dismiss) private var dismiss
    let account: AccountInfo
    @State private var password = ""
    @State private var working = false
    @State private var error: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("New password for \(account.email)").font(.headline)
            if let help = store.providers.first(where: { $0.key == account.provider })?.help {
                Text(linkified(help)).font(.caption).foregroundStyle(.secondary)
            }
            SecureField("Password", text: $password)
            if let error {
                Text(error).font(.callout).foregroundStyle(.red).textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
            }
            HStack {
                if working { ProgressView().controlSize(.small); Text("Connecting…").foregroundStyle(.secondary) }
                Spacer()
                Button("Cancel") { dismiss() }.keyboardShortcut(.cancelAction)
                Button("Save") {
                    Task {
                        working = true
                        error = await store.changePassword(account.name, password)
                        working = false
                        if error == nil { dismiss() }
                    }
                }
                .keyboardShortcut(.defaultAction)
                .disabled(password.isEmpty || working)
            }
        }
        .padding(20)
        .frame(width: 440)
    }
}
