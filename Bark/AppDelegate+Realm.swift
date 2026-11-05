//
//  AppDelegate+Realm.swift
//  Bark
//
//  Created by huangfeng on 12/18/25.
//  Copyright © 2025 Fin. All rights reserved.
//

import UIKit
import os
import UserNotifications

private let pendingMessageProcessingQueue = DispatchQueue(label: "me.fin.bark.pending-message-processing", qos: .userInitiated)
let kBarkMessagesDidChangeNotification = Notification.Name("com.bark.messagesDidChange")

extension AppDelegate {
    /*
     之前数据库是放在App Groups 共享
     但由于Realm无法解决 0xdead10cc 闪退问题
     因此改为在主APP中存放数据库文件
     Notification Service Extension 使用 plist 文件保存消息，供主APP读取存放到数据库中
     */
    func setupRealm() {
        // 先执行数据库迁移
        migrateRealmDatabase()
        
        // Tell Realm to use this new configuration object for the default Realm
        Realm.Configuration.defaultConfiguration = kRealmDefaultConfiguration
    }

    func migrateRealmDatabase() {
        // 检查是否已经迁移过
        if UserDefaults.standard.bool(forKey: "hasRealmMigrated") {
            return
        }
        
        let fileManager = FileManager.default
        guard let oldGroupUrl = fileManager.containerURL(forSecurityApplicationGroupIdentifier: "group.cc.sanbox.bark"),
              let newDocUrl = fileManager.urls(for: .documentDirectory, in: .userDomainMask).first
        else {
            return
        }
        
        let oldRealmUrl = oldGroupUrl.appendingPathComponent("bark.realm")
        let newRealmUrl = newDocUrl.appendingPathComponent("bark.realm")
        
        // 检查旧文件是否存在
        guard fileManager.fileExists(atPath: oldRealmUrl.path) else {
            // 旧文件不存在，标记为已迁移
            UserDefaults.standard.set(true, forKey: "hasRealmMigrated")
            return
        }
        
        do {
            // 复制主数据库文件
            try fileManager.copyItem(at: oldRealmUrl, to: newRealmUrl)
            
            // 复制相关文件
            let relatedFiles = ["bark.realm.lock", "bark.realm.management", "bark.realm.note"]
            for file in relatedFiles {
                let oldUrl = oldGroupUrl.appendingPathComponent(file)
                let newUrl = newDocUrl.appendingPathComponent(file)
                if fileManager.fileExists(atPath: oldUrl.path) {
                    try? fileManager.copyItem(at: oldUrl, to: newUrl)
                }
            }
            
            // 删除旧文件
            try fileManager.removeItem(at: oldRealmUrl)
            for file in relatedFiles {
                let oldUrl = oldGroupUrl.appendingPathComponent(file)
                try? fileManager.removeItem(at: oldUrl)
            }
            
            // 标记为已迁移
            UserDefaults.standard.set(true, forKey: "hasRealmMigrated")
        } catch {
            print("Realm migration failed: \(error)")
        }
    }
    
    // 处理 Notification Service Extension 保存的待处理消息, 将其存入 Realm 数据库
    func processPendingMessages() async {
        await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
            pendingMessageProcessingQueue.async {
                defer { continuation.resume() }

                guard let groupUrl = FileManager.default.containerURL(forSecurityApplicationGroupIdentifier: "group.cc.sanbox.bark") else {
                    return
                }

                guard let realm = try? Realm() else {
                    return
                }

                let result: PendingMessageStore.ImportResult
                do {
                    result = try PendingMessageStore(directory: groupUrl.appendingPathComponent("pending_messages"))
                        .importMessages(into: realm)
                } catch {
                    // Keep every pending file so the next launch/foreground can
                    // retry a failed transaction without losing history.
                    Logger(subsystem: "cc.sanbox.bark", category: "history").error("History import failed; pending messages retained")
                    return
                }

                if result.didChangeMessages {
                    Task {
                        await WidgetHistorySnapshotStore.shared.refreshFromRealmAsync()
                    }
                    self.notifyMessagesDidChange()
                }

                if !result.expiredIDs.isEmpty {
                    UNUserNotificationCenter.current().removeDeliveredNotifications(withIdentifiers: result.expiredIDs)
                    Task {
                        await MessageDeletionCoordinator.removeImageCaches(result.expiredImageURLs)
                    }
                }

            }
        }
    }
    
    func notifyMessagesDidChange() {
        DispatchQueue.main.async {
            NotificationCenter.default.post(name: kBarkMessagesDidChangeNotification, object: nil)
        }
    }
}
