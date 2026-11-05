import Foundation
import UserNotifications

/// Shares a best-attempt snapshot with the expiry callback and consumes the
/// completion handler exactly once, including early failures and timeout races.
final class NotificationDelivery {
    private let lock = NSLock()
    private var content: UNNotificationContent
    private var handler: ((UNNotificationContent) -> Void)?

    init(content: UNNotificationContent, handler: @escaping (UNNotificationContent) -> Void) {
        self.content = content.copy() as! UNNotificationContent
        self.handler = handler
    }

    var isFinished: Bool {
        lock.lock()
        defer { lock.unlock() }
        return handler == nil
    }

    func update(_ content: UNNotificationContent) {
        lock.lock()
        defer { lock.unlock() }
        guard handler != nil else { return }
        self.content = content.copy() as! UNNotificationContent
    }

    func finish(_ content: UNNotificationContent? = nil) {
        lock.lock()
        guard let handler else { lock.unlock(); return }
        let result = content ?? self.content
        self.handler = nil
        lock.unlock()
        handler(result)
    }
}
