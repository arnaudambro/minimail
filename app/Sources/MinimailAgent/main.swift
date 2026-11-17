import AppKit
import UserNotifications

// Minimail notification agent. launchd starts it at login. It runs `minimail watch --emit`,
// which keeps the local cache in sync and prints one JSON line per notification.
// Clicking a notification opens the email in Minimail; "Mark as Read" marks it read in place.

struct Payload: Decodable {
    let ids: [String]
    let title: String
    let subtitle: String
    let body: String
    let account: String
}

func minimailCLI() -> String? {
    let home = FileManager.default.homeDirectoryForCurrentUser.path
    let env = ProcessInfo.processInfo.environment
    return [env["MINIMAIL_BIN"], "\(home)/.local/bin/minimail", "/opt/homebrew/bin/minimail", "/usr/local/bin/minimail"]
        .compactMap { $0 }.first { FileManager.default.isExecutableFile(atPath: $0) }
}

func cliEnvironment() -> [String: String] {
    var env = ProcessInfo.processInfo.environment
    let home = FileManager.default.homeDirectoryForCurrentUser.path
    env["PATH"] = "\(home)/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:" + (env["PATH"] ?? "")
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    return env
}

final class Agent: NSObject, NSApplicationDelegate, UNUserNotificationCenterDelegate {
    private var watcher: Process?
    private var buffer = Data()

    func applicationDidFinishLaunching(_ notification: Notification) {
        let center = UNUserNotificationCenter.current()
        center.delegate = self
        let read = UNNotificationAction(identifier: "read", title: "Mark as Read", options: [])
        let open = UNNotificationAction(identifier: "open", title: "Open", options: [.foreground])
        center.setNotificationCategories([
            UNNotificationCategory(identifier: "mail", actions: [read, open], intentIdentifiers: [], options: [])
        ])
        center.requestAuthorization(options: [.alert, .sound, .badge]) { granted, error in
            if !granted { NSLog("Minimail agent: notifications not allowed \(String(describing: error))") }
        }
        startWatcher()
    }

    private func startWatcher() {
        guard let cli = minimailCLI() else {
            NSLog("Minimail agent: minimail CLI not found, retrying in 60 s")
            DispatchQueue.main.asyncAfter(deadline: .now() + 60) { self.startWatcher() }
            return
        }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: cli)
        p.arguments = ["watch", "--emit"]
        p.environment = cliEnvironment()
        let out = Pipe()
        p.standardOutput = out
        p.standardError = FileHandle.standardError
        out.fileHandleForReading.readabilityHandler = { [weak self] h in
            let data = h.availableData
            DispatchQueue.main.async { self?.consume(data) }
        }
        p.terminationHandler = { [weak self] _ in
            DispatchQueue.main.asyncAfter(deadline: .now() + 5) { self?.startWatcher() }
        }
        do { try p.run(); watcher = p } catch {
            NSLog("Minimail agent: cannot start watcher: \(error)")
            DispatchQueue.main.asyncAfter(deadline: .now() + 30) { self.startWatcher() }
        }
    }

    private func consume(_ data: Data) {
        buffer.append(data)
        while let nl = buffer.firstIndex(of: 0x0A) {
            let line = buffer[buffer.startIndex..<nl]
            buffer.removeSubrange(buffer.startIndex...nl)
            if let p = try? JSONDecoder().decode(Payload.self, from: line) { post(p) }
        }
    }

    private func post(_ p: Payload) {
        let c = UNMutableNotificationContent()
        c.title = p.title
        c.subtitle = p.subtitle
        c.body = p.body.isEmpty ? p.account : p.body
        c.sound = .default
        c.categoryIdentifier = "mail"
        c.threadIdentifier = p.account
        c.userInfo = ["ids": p.ids]
        let id = p.ids.count == 1 ? p.ids[0] : "batch-\(UUID().uuidString)"
        UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: id, content: c, trigger: nil))
    }

    // Show banners even though the agent is technically "active".
    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                withCompletionHandler done: @escaping (UNNotificationPresentationOptions) -> Void) {
        done([.banner, .sound, .list])
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
                                withCompletionHandler done: @escaping () -> Void) {
        let ids = response.notification.request.content.userInfo["ids"] as? [String] ?? []
        switch response.actionIdentifier {
        case "read":
            markRead(ids)
        case UNNotificationDismissActionIdentifier:
            break
        default: // click on the notification, or "Open"
            if let first = ids.first { openInApp(first) }
        }
        done()
    }

    private func markRead(_ ids: [String]) {
        guard !ids.isEmpty, let cli = minimailCLI() else { return }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: cli)
        p.arguments = ["mark-read", "--quiet"] + ids
        p.environment = cliEnvironment()
        try? p.run()
    }

    private func openInApp(_ id: String) {
        var comps = URLComponents()
        comps.scheme = "minimail"
        comps.host = "open"
        comps.queryItems = [URLQueryItem(name: "id", value: id)]
        guard let url = comps.url else { return }
        // The agent lives inside Minimail.app/Contents/Library/LoginItems/.
        let app = Bundle.main.bundleURL.deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent()
        let cfg = NSWorkspace.OpenConfiguration()
        cfg.activates = true
        if app.pathExtension == "app" {
            NSWorkspace.shared.open([url], withApplicationAt: app, configuration: cfg)
        } else {
            NSWorkspace.shared.open(url)
        }
    }
}

let app = NSApplication.shared
let agent = Agent()
app.delegate = agent
app.setActivationPolicy(.accessory)
app.run()
