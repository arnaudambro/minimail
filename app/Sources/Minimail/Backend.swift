import Foundation

// The app is a thin UI over the `minimail` CLI (`--json` everywhere), so the CLI stays the single source of truth.

struct Message: Codable, Identifiable, Hashable {
    let id: String
    let account: String
    let folder: String
    let uid: Int
    let from: String
    let to: String
    let subject: String
    let date: String
    let ts: Double
    var unread: Bool
    let flagged: Bool
}

struct ListResponse: Codable {
    let messages: [Message]
    let errors: [String: String]
}

struct Attachment: Codable, Hashable {
    let filename: String?
    let type: String
    let size: Int
}

struct FullMessage: Codable, Identifiable {
    let id: String
    let account: String
    let folder: String
    let from: String
    let to: String
    let cc: String
    let subject: String
    let date: String
    let reply_to: String
    let body: String
    let attachments: [Attachment]
    var unsubscribe: Bool? = nil
    /// nil: not parsed yet, "": plain-text message.
    var html: String? = nil
}

struct UnsubscribeResult: Codable {
    let method: String?
    let ok: Bool
    let url: String?
    let to: String?
    let error: String?
}

struct AccountInfo: Codable, Identifiable, Hashable {
    let name: String
    let email: String
    let provider: String
    let enabled: Bool
    var id: String { name }
}

struct ProviderInfo: Codable, Identifiable, Hashable {
    let key: String
    let help: String
    var id: String { key }
    var label: String {
        switch key {
        case "gmail": "Gmail / Google Workspace"
        case "icloud": "iCloud"
        case "yahoo": "Yahoo"
        case "outlook": "Outlook / Microsoft 365"
        case "ovh": "OVH"
        case "ox-numerique": "OX (numerique.gouv.fr)"
        case "proton-bridge": "Proton Mail (via Bridge)"
        case "custom": "Other IMAP server"
        default: key
        }
    }
}

struct LoginResult: Codable {
    let name: String
    let ok: Bool
    let error: String?
}

struct DetectResult: Codable {
    let provider: String
}

struct NotifStatus: Codable {
    let installed: Bool
    let running: Bool
    let notifier: Bool
}

struct CLIError: LocalizedError {
    let message: String
    var errorDescription: String? { message }
}

enum CLI {
    static let path: String? = {
        let env = ProcessInfo.processInfo.environment
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        let candidates = [env["MINIMAIL_BIN"], "\(home)/.local/bin/minimail", "/opt/homebrew/bin/minimail", "/usr/local/bin/minimail"]
        return candidates.compactMap { $0 }.first { FileManager.default.isExecutableFile(atPath: $0) }
    }()

    static func run(_ args: [String], input: String? = nil) async throws -> Data {
        guard let path else {
            throw CLIError(message: "The minimail command was not found. Install it with: uv tool install minimail")
        }
        return try await withCheckedThrowingContinuation { cont in
            DispatchQueue.global(qos: .userInitiated).async {
                let p = Process()
                p.executableURL = URL(fileURLWithPath: path)
                p.arguments = args
                var env = ProcessInfo.processInfo.environment
                let home = FileManager.default.homeDirectoryForCurrentUser.path
                env["PATH"] = "\(home)/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:" + (env["PATH"] ?? "")
                env["PYTHONIOENCODING"] = "utf-8"
                p.environment = env
                let out = Pipe(), err = Pipe(), inp = Pipe()
                p.standardOutput = out
                p.standardError = err
                p.standardInput = inp
                do { try p.run() } catch { cont.resume(throwing: error); return }
                if let input { inp.fileHandleForWriting.write(Data(input.utf8)) }
                try? inp.fileHandleForWriting.close()
                var errData = Data()
                let errReader = DispatchQueue(label: "stderr")
                errReader.async { errData = err.fileHandleForReading.readDataToEndOfFile() }
                let data = out.fileHandleForReading.readDataToEndOfFile()
                p.waitUntilExit()
                errReader.sync {}
                if p.terminationStatus != 0 {
                    let msg = String(decoding: errData.isEmpty ? data : errData, as: UTF8.self)
                        .trimmingCharacters(in: .whitespacesAndNewlines)
                    cont.resume(throwing: CLIError(message: msg.isEmpty ? "minimail failed (\(p.terminationStatus))" : msg))
                } else {
                    cont.resume(returning: data)
                }
            }
        }
    }

    static func json<T: Decodable>(_ type: T.Type, _ args: [String], input: String? = nil) async throws -> T {
        try JSONDecoder().decode(T.self, from: try await run(args, input: input))
    }
}

// MARK: - Formatting helpers

func senderName(_ s: String) -> String {
    if let r = s.range(of: "<") {
        let name = s[..<r.lowerBound].trimmingCharacters(in: .whitespaces).trimmingCharacters(in: CharacterSet(charactersIn: "\""))
        if !name.isEmpty { return name }
        return s[r.upperBound...].replacingOccurrences(of: ">", with: "")
    }
    return s
}

func senderEmail(_ s: String) -> String {
    guard let a = s.range(of: "<"), let b = s.range(of: ">", range: a.upperBound..<s.endIndex) else { return "" }
    return String(s[a.upperBound..<b.lowerBound])
}

func shortDate(_ ts: Double) -> String {
    guard ts > 0 else { return "" }
    let d = Date(timeIntervalSince1970: ts)
    let cal = Calendar.current
    if cal.isDateInToday(d) { return d.formatted(date: .omitted, time: .shortened) }
    if cal.isDateInYesterday(d) { return "Yesterday" }
    if cal.isDate(d, equalTo: .now, toGranularity: .year) { return d.formatted(.dateTime.day().month(.abbreviated)) }
    return d.formatted(date: .numeric, time: .omitted)
}

private let markdownLink = try! NSRegularExpression(pattern: #"\[([^\]\n]{1,300})\]\(((?:https?|mailto):[^)\s]+)\)"#)

/// Body text to clickable text: [label](url) becomes "label" linked to url, bare URLs become links.
func linkified(_ text: String) -> AttributedString {
    let capped = text.count > 200_000 ? String(text.prefix(200_000)) + "\n…" : text
    let ns = capped as NSString
    var result = AttributedString()
    var cursor = 0
    for m in markdownLink.matches(in: capped, range: NSRange(location: 0, length: ns.length)) {
        if m.range.location > cursor {
            result += plainLinkified(ns.substring(with: NSRange(location: cursor, length: m.range.location - cursor)))
        }
        var label = AttributedString(ns.substring(with: m.range(at: 1)))
        label.link = URL(string: ns.substring(with: m.range(at: 2)))
        result += label
        cursor = m.range.location + m.range.length
    }
    if cursor < ns.length { result += plainLinkified(ns.substring(from: cursor)) }
    return result
}

private func plainLinkified(_ capped: String) -> AttributedString {
    var attr = AttributedString(capped)
    guard let detector = try? NSDataDetector(types: NSTextCheckingResult.CheckingType.link.rawValue) else { return attr }
    let ns = capped as NSString
    for match in detector.matches(in: capped, range: NSRange(location: 0, length: ns.length)) {
        guard let url = match.url, let range = Range(match.range, in: capped),
              let lower = AttributedString.Index(range.lowerBound, within: attr),
              let upper = AttributedString.Index(range.upperBound, within: attr) else { continue }
        attr[lower..<upper].link = url
    }
    return attr
}
