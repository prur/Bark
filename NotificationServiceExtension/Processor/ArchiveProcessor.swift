//
//  ArchiveProcessor.swift
//  NotificationServiceExtension
//
//  Created by huangfeng on 2024/5/29.
//  Copyright © 2024 Fin. All rights reserved.
//

import Foundation
import os

class ArchiveProcessor: NotificationContentProcessor {
    func process(identifier: String, content bestAttemptContent: UNMutableNotificationContent) async throws -> UNMutableNotificationContent {
        let userInfo = bestAttemptContent.userInfo
        
        var isArchive: Bool = ArchiveSettingManager.shared.isArchive
        if let archive = userInfo["isarchive"] as? String {
            isArchive = archive == "1" ? true : false
        }
        
        if isArchive {
            let alert = (userInfo["aps"] as? [String: Any])?["alert"] as? [String: Any]
            let title = alert?["title"] as? String
            let subtitle = alert?["subtitle"] as? String
            let body = alert?["body"] as? String
            let url = userInfo["url"] as? String
            let group = userInfo["group"] as? String
            let image = userInfo["image"] as? String
            let id = userInfo["id"] as? String
            let markdown = userInfo["markdown"] as? String

            let createDate = Date()
            let ttl: TimeInterval?
            if let ttlString = userInfo["ttl"] as? String {
                ttl = TimeInterval(ttlString)
            } else if let ttlNumber = userInfo["ttl"] as? NSNumber {
                ttl = ttlNumber.doubleValue
            } else {
                ttl = nil
            }

            if let ttl, ttl <= 0 {
                return bestAttemptContent
            }

            let expireDate = ttl.map { createDate.addingTimeInterval($0) }

            // 准备消息数据字典
            var messageDict: [String: Any] = [:]
            let messageId = (id != nil && !id!.isEmpty) ? id! : identifier
            messageDict["id"] = messageId
            
            if let title = title {
                messageDict["title"] = title
            }
            if let subtitle = subtitle {
                messageDict["subtitle"] = subtitle
            }
            if let markdown = markdown, !markdown.isEmpty {
                messageDict["body"] = markdown
                messageDict["bodyType"] = "markdown"
            } else if let body = body {
                messageDict["body"] = body
            }
            if let url = url {
                messageDict["url"] = url
            }
            if let image = image {
                messageDict["image"] = image
            }
            if let group = group {
                messageDict["group"] = group
            }
            messageDict["createDate"] = createDate.timeIntervalSince1970
            if let expireDate {
                messageDict["expireDate"] = expireDate.timeIntervalSince1970
            }

            // A failed archive must not prevent notification delivery.
            if let groupUrl = FileManager.default.containerURL(forSecurityApplicationGroupIdentifier: "group.cc.sanbox.bark") {
                do {
                    try PendingMessageStore(directory: groupUrl.appendingPathComponent("pending_messages")).save(messageDict)
                    // Notify immediately after durable handoff, before media or
                    // call processors can consume the extension's time budget.
                    CFNotificationCenterPostNotification(CFNotificationCenterGetDarwinNotifyCenter(), CFNotificationName("com.bark.newmessage" as CFString), nil, nil, true)
                } catch {
                    Logger(subsystem: "cc.sanbox.bark", category: "history").error("Pending history write failed; notification delivery continues")
                }
            }

            let snapshotItem = WidgetHistoryMessage(
                id: messageId,
                group: group,
                title: title,
                subtitle: subtitle,
                body: messageDict["body"] as? String,
                bodyType: messageDict["bodyType"] as? String,
                image: image,
                createDate: createDate
            )
            await WidgetHistorySnapshotStore.shared.prependMessage(snapshotItem)
        }
        return bestAttemptContent
    }
}
