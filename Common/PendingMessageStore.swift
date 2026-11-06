import Foundation
import RealmSwift

/// Durable handoff from the notification extension to the app's private Realm.
struct PendingMessageStore {
    let directory: URL

    @discardableResult
    func save(_ message: [String: Any]) throws -> URL {
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        // Each delivery gets its own file. Reusing a hash of the message ID can
        // overwrite a newer delivery while the app is importing the old file.
        let url = directory.appendingPathComponent(UUID().uuidString).appendingPathExtension("plist")
        let data = try PropertyListSerialization.data(fromPropertyList: message, format: .binary, options: 0)
        try data.write(to: url, options: .atomic)
        return url
    }

    struct ImportResult {
        let didChangeMessages: Bool
        let expiredIDs: [String]
        let expiredImageURLs: [String]
    }

    /// Acknowledge files only after the Realm transaction commits. A failed
    /// commit or failed file removal is safe to retry using Message's primary key.
    func importMessages(
        into realm: Realm,
        now: Date = Date(),
        write: ((() -> Void) throws -> Void)? = nil,
        acknowledge: ((URL) throws -> Void)? = nil
    ) throws -> ImportResult {
        let fileManager = FileManager.default
        let files: [URL]
        if fileManager.fileExists(atPath: directory.path) {
            files = try fileManager.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil, options: [.skipsHiddenFiles])
                .filter { $0.pathExtension == "plist" }
        } else {
            files = []
        }

        var acknowledgedFiles: [(url: URL, message: Message)] = []
        var latest: [String: (url: URL, message: Message)] = [:]
        for url in files {
            guard let data = try? Data(contentsOf: url),
                  let dictionary = (try? PropertyListSerialization.propertyList(from: data, options: [], format: nil)) as? [String: Any],
                  let id = dictionary["id"] as? String, !id.isEmpty,
                  let timestamp = dictionary["createDate"] as? TimeInterval, timestamp.isFinite
            else {
                // Preserve malformed files for diagnosis instead of silently
                // deleting data we could not interpret.
                continue
            }
            let message = Message(dict: dictionary)
            acknowledgedFiles.append((url, message))
            if let previous = latest[id], (previous.message.createDate ?? .distantPast) > (message.createDate ?? .distantPast) { continue }
            latest[id] = (url, message)
        }
        // An expired replacement is a tombstone for older queued/stored versions.
        // Choose the newest delivery BEFORE testing expiry, never revive an old body.
        let expiredPending = latest.values.filter { ($0.message.expireDate ?? .distantFuture) <= now }
        let messages = latest.values.map(\.message).filter { ($0.expireDate ?? .distantFuture) > now }
            .sorted { ($0.createDate ?? .distantPast) < ($1.createDate ?? .distantPast) }

        let expired = realm.objects(Message.self).filter("expireDate != nil AND expireDate <= %@", now)
        let expiredIDs = Array(expired.map(\.id))
        var deletedIDs: [String] = []
        var deletedImages: [String] = []
        var didChange = false
        if !messages.isEmpty || !expiredPending.isEmpty || !expiredIDs.isEmpty {
            let commit = write ?? { changes in try realm.write(changes) }
            try commit {
                for message in messages {
                    if let saved = realm.object(ofType: Message.self, forPrimaryKey: message.id),
                       (saved.createDate ?? .distantPast) > (message.createDate ?? .distantPast) {
                        continue
                    }
                    realm.add(message, update: .all)
                    didChange = true
                }
                for pending in expiredPending {
                    if let saved = realm.object(ofType: Message.self, forPrimaryKey: pending.message.id),
                       (saved.createDate ?? .distantPast) <= (pending.message.createDate ?? .distantPast) {
                        deletedIDs.append(saved.id)
                        if let image = saved.image { deletedImages.append(image) }
                        realm.delete(saved)
                        didChange = true
                    }
                }
                // Use the captured IDs: adding a newer version above may have
                // changed its expiry and must not cause it to be deleted.
                for id in expiredIDs {
                    if let message = realm.object(ofType: Message.self, forPrimaryKey: id),
                       let expiry = message.expireDate, expiry <= now {
                        deletedIDs.append(id)
                        if let image = message.image { deletedImages.append(image) }
                        realm.delete(message)
                        didChange = true
                    }
                }
            }
        }
        let remove = acknowledge ?? { try fileManager.removeItem(at: $0) }
        let tombstones = Set(expiredPending.map(\.url))
        var failedIDs = Set<String>()
        for entry in acknowledgedFiles where !tombstones.contains(entry.url) {
            do { try remove(entry.url) }
            catch { if fileManager.fileExists(atPath: entry.url.path) { failedIDs.insert(entry.message.id) } }
        }
        // Keep an expired latest file until every older file is acknowledged.
        // Otherwise a failed old-file removal could resurrect it on the next run.
        for entry in expiredPending where !failedIDs.contains(entry.message.id) { try? remove(entry.url) }
        return ImportResult(didChangeMessages: didChange, expiredIDs: deletedIDs, expiredImageURLs: Array(Set(deletedImages)))
    }
}
