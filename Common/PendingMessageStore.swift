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
        write: ((() -> Void) throws -> Void)? = nil
    ) throws -> ImportResult {
        let fileManager = FileManager.default
        let files: [URL]
        if fileManager.fileExists(atPath: directory.path) {
            files = try fileManager.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil, options: [.skipsHiddenFiles])
                .filter { $0.pathExtension == "plist" }
        } else {
            files = []
        }

        var acknowledgedFiles: [URL] = []
        var messages: [Message] = []
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
            acknowledgedFiles.append(url)
            if let expiry = message.expireDate, expiry <= now { continue }
            messages.append(message)
        }
        messages.sort { ($0.createDate ?? .distantPast) < ($1.createDate ?? .distantPast) }

        let expired = realm.objects(Message.self).filter("expireDate != nil AND expireDate <= %@", now)
        let expiredIDs = Array(expired.map(\.id))
        var deletedIDs: [String] = []
        var deletedImages: [String] = []
        var didChange = false
        if !messages.isEmpty || !expiredIDs.isEmpty {
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
        for url in acknowledgedFiles { try? fileManager.removeItem(at: url) }
        return ImportResult(didChangeMessages: didChange, expiredIDs: deletedIDs, expiredImageURLs: Array(Set(deletedImages)))
    }
}
