import Foundation
import SQLite3

/// Read-only access to the local cache written by the `minimail` sync (~/.local/state/minimail/mail.db).
/// All reads are local, so lists, counts, search and opening a synced message are instant.
final class MailDB {
    static let path: String = {
        let env = ProcessInfo.processInfo.environment
        let state = env["XDG_STATE_HOME"] ?? FileManager.default.homeDirectoryForCurrentUser.path + "/.local/state"
        return state + "/minimail/mail.db"
    }()

    private var db: OpaquePointer?
    let queue = DispatchQueue(label: "minimail.db", qos: .userInitiated)

    init?() {
        guard FileManager.default.fileExists(atPath: Self.path),
              sqlite3_open_v2(Self.path, &db, SQLITE_OPEN_READWRITE | SQLITE_OPEN_FULLMUTEX, nil) == SQLITE_OK else {
            return nil
        }
        sqlite3_busy_timeout(db, 3000)
    }

    deinit { sqlite3_close(db) }

    /// Run a query and map every row. Arguments: String, Int, Double or nil.
    func rows<T>(_ sql: String, _ args: [Any?] = [], _ map: (Row) -> T) -> [T] {
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else { return [] }
        defer { sqlite3_finalize(stmt) }
        let transient = unsafeBitCast(-1, to: sqlite3_destructor_type.self)
        for (i, a) in args.enumerated() {
            let idx = Int32(i + 1)
            switch a {
            case let v as String: sqlite3_bind_text(stmt, idx, v, -1, transient)
            case let v as Int: sqlite3_bind_int64(stmt, idx, Int64(v))
            case let v as Double: sqlite3_bind_double(stmt, idx, v)
            default: sqlite3_bind_null(stmt, idx)
            }
        }
        var out: [T] = []
        while sqlite3_step(stmt) == SQLITE_ROW { out.append(map(Row(stmt: stmt))) }
        return out
    }

    func dataVersionAsync() async -> Int {
        await withCheckedContinuation { c in queue.async { c.resume(returning: self.dataVersion()) } }
    }

    /// Changes whenever another connection (the sync, the CLI) commits.
    func dataVersion() -> Int {
        rows("PRAGMA data_version") { $0.int(0) }.first ?? 0
    }

    struct Row {
        let stmt: OpaquePointer?
        func string(_ i: Int32) -> String {
            guard let c = sqlite3_column_text(stmt, i) else { return "" }
            return String(cString: c)
        }
        func optString(_ i: Int32) -> String? {
            sqlite3_column_type(stmt, i) == SQLITE_NULL ? nil : string(i)
        }
        func int(_ i: Int32) -> Int { Int(sqlite3_column_int64(stmt, i)) }
        func double(_ i: Int32) -> Double { sqlite3_column_double(stmt, i) }
    }

    // MARK: - Queries

    private static let messageColumns = "m.account, m.folder, m.uid, m.sender, m.recipients, m.subject, m.ts, m.unread, m.flagged, "
        + "m.answered, m.forwarded, COALESCE(m.has_att, 0)"

    private static func message(_ r: Row) -> Message {
        let account = r.string(0), folder = r.string(1), uid = r.int(2)
        return Message(id: "\(account):\(folder):\(uid)", account: account, folder: folder, uid: uid,
                       from: r.string(3), to: r.string(4), subject: r.string(5), date: "",
                       ts: r.double(6), unread: r.int(7) != 0, flagged: r.int(8) != 0,
                       answered: r.int(9) != 0, forwarded: r.int(10) != 0, attachments: r.int(11) != 0)
    }

    static func ftsQuery(_ text: String) -> String {
        text.split(whereSeparator: \.isWhitespace)
            .map { $0.replacingOccurrences(of: "\"", with: "") }
            .filter { !$0.isEmpty }
            .map { "\"\($0)\"*" }
            .joined(separator: " AND ")
    }

    func messages(folder: String?, account: String?, unreadOnly: Bool, search: String, limit: Int = 400) -> [Message] {
        var sql = "SELECT \(Self.messageColumns) FROM messages m"
        var conds: [String] = [], args: [Any?] = []
        let fts = Self.ftsQuery(search)
        if !fts.isEmpty {
            sql += " JOIN messages_fts f ON f.rowid = m.id"
            conds.append("messages_fts MATCH ?"); args.append(fts)
        }
        if let folder { conds.append("m.folder = ?"); args.append(folder) }
        if let account { conds.append("m.account = ?"); args.append(account) }
        if unreadOnly { conds.append("m.unread = 1") }
        if !conds.isEmpty { sql += " WHERE " + conds.joined(separator: " AND ") }
        sql += " ORDER BY m.ts DESC LIMIT ?"
        args.append(limit)
        return rows(sql, args, Self.message)
    }

    func counts() -> [String: [String: Int]] {
        var out: [String: [String: Int]] = [:]
        for (a, f, n) in rows("SELECT account, folder, SUM(unread) FROM messages GROUP BY account, folder", [], {
            ($0.string(0), $0.string(1), $0.int(2))
        }) { out[a, default: [:]][f] = n }
        return out
    }

    func fullMessage(_ m: Message) -> FullMessage? {
        rows("SELECT sender, recipients, cc, subject, reply_to, body, attachments, list_unsub, html FROM messages "
             + "WHERE account=? AND folder=? AND uid=?", [m.account, m.folder, m.uid]) { r -> FullMessage? in
            guard let body = r.optString(5) else { return nil }
            let atts = (try? JSONDecoder().decode([Attachment].self, from: Data(r.string(6).utf8))) ?? []
            let date = m.ts > 0 ? ISO8601DateFormatter().string(from: Date(timeIntervalSince1970: m.ts)) : ""
            return FullMessage(id: m.id, account: m.account, folder: m.folder, from: r.string(0), to: r.string(1),
                               cc: r.string(2), subject: r.string(3), date: date, reply_to: r.string(4),
                               body: body, attachments: atts, unsubscribe: !r.string(7).isEmpty,
                               html: r.optString(8))
        }.first ?? nil
    }

    func contacts(prefix: String, limit: Int = 6) -> [Contact] {
        let p = prefix.trimmingCharacters(in: .whitespaces).lowercased()
        guard !p.isEmpty else { return [] }
        return rows("SELECT email, name FROM contacts WHERE email LIKE ? OR lower(name) LIKE ? OR lower(name) LIKE ? "
                    + "ORDER BY sent DESC, received DESC, last DESC LIMIT ?",
                    [p + "%", p + "%", "% " + p + "%", limit]) { Contact(email: $0.string(0), name: $0.string(1)) }
    }
}

struct Contact: Hashable, Identifiable {
    let email: String
    let name: String
    var id: String { email }
    var formatted: String {
        let n = name.replacingOccurrences(of: ",", with: " ").replacingOccurrences(of: "\"", with: "")
            .trimmingCharacters(in: .whitespaces)
        return n.isEmpty ? email : "\(n) <\(email)>"
    }
}
