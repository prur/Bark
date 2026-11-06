//
//  NotificationService.swift
//  NotificationServiceExtension
//
//  Created by huangfeng on 2018/12/17.
//  Copyright © 2018 Fin. All rights reserved.
//

import UserNotifications

class NotificationService: UNNotificationServiceExtension {
    private var delivery: NotificationDelivery?
    private var processingTask: Task<Void, Never>?
    
    override func didReceive(_ request: UNNotificationRequest, withContentHandler contentHandler: @escaping (UNNotificationContent) -> Void) {
        let delivery = NotificationDelivery(content: request.content, handler: contentHandler)
        self.delivery = delivery
        processingTask = Task {
            guard var bestAttemptContent = (request.content.mutableCopy() as? UNMutableNotificationContent) else {
                delivery.finish()
                return
            }
            
            // 所有的 processor 按顺序从上往下对推送进行处理
            // ciphertext 需要放在最前面，有可能所有的推送数据都在密文里
            // icon 放在最后面，游戏模式下可能会超时，超时后后面的 processor 就没机会运行了。
            let processors: [NotificationContentProcessorItem] = [
                .ciphertext,
                .markdown,
                .level,
                .badge,
                .autoCopy,
                .archive,
                .mute,
                .call,
                .setImage,
                .setIcon
            ]
            
            // 各个 processor 依次对推送进行处理
            for processor in processors.map({ $0.processor }) {
                guard !Task.isCancelled, !delivery.isFinished else { return }
                do {
                    bestAttemptContent = try await processor.process(identifier: request.identifier, content: bestAttemptContent)
                    delivery.update(bestAttemptContent)
                } catch NotificationContentProcessorError.error(let content) {
                    delivery.finish(content)
                    return
                } catch {
                    delivery.finish()
                    return
                }
            }
            
            // 处理完后交付推送
            delivery.finish(bestAttemptContent)
            
            // 发送 Darwin Notification 通知主 APP 有新消息
            CFNotificationCenterPostNotification(
                CFNotificationCenterGetDarwinNotifyCenter(),
                CFNotificationName("com.bark.newmessage" as CFString),
                nil,
                nil,
                true
            )
        }
    }

    override func serviceExtensionTimeWillExpire() {
        super.serviceExtensionTimeWillExpire()
        processingTask?.cancel()
        delivery?.finish()
    }
}
