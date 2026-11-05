//
//  BarkTests.swift
//  BarkTests
//
//  Created by huangfeng on 2021/6/24.
//  Copyright © 2021 Fin. All rights reserved.
//

import XCTest
@testable import Bark
import RealmSwift
import RxSwift
import SwiftyJSON
import Moya
import UserNotifications

class BarkTests: XCTestCase {
    private func store() throws -> PendingMessageStore {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        addTeardownBlock { try? FileManager.default.removeItem(at: directory) }
        return PendingMessageStore(directory: directory)
    }

    private func realm() throws -> Realm {
        try Realm(configuration: Realm.Configuration(inMemoryIdentifier: UUID().uuidString, objectTypes: [Message.self]))
    }

    func testHistoryRetainsFilesOnWriteFailureAndRetriesAfterReopen() throws {
        let store = try store()
        let file = try store.save(["id": "history", "body": "retained", "createDate": 100.0])
        let database = try realm()
        struct WriteFailure: Error {}
        XCTAssertThrowsError(try store.importMessages(into: database, write: { _ in throw WriteFailure() }))
        XCTAssertTrue(FileManager.default.fileExists(atPath: file.path))
        XCTAssertTrue(database.objects(Message.self).isEmpty)
        _ = try store.importMessages(into: database)
        XCTAssertFalse(FileManager.default.fileExists(atPath: file.path))
        let reopened = try Realm(configuration: database.configuration)
        XCTAssertEqual(reopened.object(ofType: Message.self, forPrimaryKey: "history")?.body, "retained")
        _ = try store.importMessages(into: reopened)
        XCTAssertEqual(reopened.objects(Message.self).count, 1)
    }

    func testHistoryUpdatesAndConcurrentHandoffDoNotLoseNewerDelivery() throws {
        let store = try store()
        let database = try realm()
        let first = try store.save(["id": "../../same-id", "body": "old", "createDate": 100.0])
        var second: URL?
        _ = try store.importMessages(into: database, write: { changes in
            second = try store.save(["id": "../../same-id", "body": "new", "createDate": 200.0])
            try database.write(changes)
        })
        XCTAssertNotEqual(first, second)
        XCTAssertTrue(FileManager.default.fileExists(atPath: try XCTUnwrap(second).path))
        _ = try store.importMessages(into: database)
        // Late retry of an older payload must not overwrite a newer version.
        try store.save(["id": "../../same-id", "body": "late old", "createDate": 150.0])
        _ = try store.importMessages(into: database)
        XCTAssertEqual(database.objects(Message.self).count, 1)
        XCTAssertEqual(database.objects(Message.self).first?.body, "new")
    }

    func testHistoryExpiryMalformedFilesAndRenewedMessage() throws {
        let store = try store()
        let database = try realm()
        try store.save(["id": "expired", "createDate": 100.0, "expireDate": 101.0])
        try store.save(["id": "renewed", "createDate": 100.0, "expireDate": 300.0])
        _ = try store.importMessages(into: database, now: Date(timeIntervalSince1970: 200))
        try store.save(["id": "renewed", "body": "renewed", "createDate": 400.0, "expireDate": 1000.0])
        let malformed = store.directory.appendingPathComponent("malformed.plist")
        try Data("not a plist".utf8).write(to: malformed)
        let result = try store.importMessages(into: database, now: Date(timeIntervalSince1970: 500))
        XCTAssertEqual(database.objects(Message.self).count, 1)
        XCTAssertEqual(database.objects(Message.self).first?.body, "renewed")
        XCTAssertTrue(result.expiredIDs.isEmpty)
        XCTAssertTrue(FileManager.default.fileExists(atPath: malformed.path))
        let expired = try store.importMessages(into: database, now: Date(timeIntervalSince1970: 1001))
        XCTAssertEqual(expired.expiredIDs, ["renewed"])
        XCTAssertTrue(database.objects(Message.self).isEmpty)
    }

    func testHistoryPersistsAcrossDiskRealmReopen() throws {
        let store = try store()
        try FileManager.default.createDirectory(at: store.directory, withIntermediateDirectories: true)
        let configuration = Realm.Configuration(fileURL: store.directory.appendingPathComponent("history.realm"), schemaVersion: 19, objectTypes: [Message.self])
        try store.save(["id": "disk", "body": "saved", "createDate": 100.0])
        try autoreleasepool {
            let database = try Realm(configuration: configuration)
            _ = try store.importMessages(into: database)
        }
        let reopened = try Realm(configuration: configuration)
        XCTAssertEqual(reopened.object(ofType: Message.self, forPrimaryKey: "disk")?.body, "saved")
    }

    func testNotificationDeliveryCompletesOnceAndKeepsSnapshot() {
        let content = UNMutableNotificationContent()
        content.body = "initial"
        var delivered: [String] = []
        let delivery = NotificationDelivery(content: content) { delivered.append($0.body) }
        content.body = "processed"
        delivery.update(content)
        content.body = "still mutating"
        delivery.finish() // timeout
        delivery.finish(content) // late processor / normal completion
        XCTAssertEqual(delivered, ["processed"])
        XCTAssertTrue(delivery.isFinished)

        let immediate = NotificationDelivery(content: content) { delivered.append($0.body) }
        immediate.finish() // timeout before first processor
        immediate.finish()
        XCTAssertEqual(delivered.count, 2)
    }

    func testRegistrationUsesPOSTBodyAndValidatesReturnedKey() throws {
        let target = BarkApi.register(address: "https://example.invalid", key: "private-key", devicetoken: "private-token")
        let endpoint = MoyaProvider<BarkApi>.defaultEndpointMapping(for: target)
        let request = try endpoint.urlRequest()
        XCTAssertEqual(request.httpMethod, "POST")
        XCTAssertNil(request.url?.query)
        let body = String(data: try XCTUnwrap(request.httpBody), encoding: .utf8)
        XCTAssertTrue(body?.contains("private-token") == true)
        XCTAssertEqual(ServerManager.registrationKey(from: JSON(["data": ["key": "valid"]])), "valid")
        for invalid: Any in [NSNull(), 123, "", "   ", ["nested": "value"]] {
            XCTAssertNil(ServerManager.registrationKey(from: JSON(["data": ["key": invalid]])))
        }
    }

    @MainActor
    func testRegistrationPersistsImmediatelyAndCoalescesRetry() {
        let first = Server(address: "https://first.invalid", key: "")
        let second = Server(address: "https://second.invalid", key: "existing")
        var saved: [[String]] = []
        let manager = ServerManager(servers: [first, second]) { saved.append($0.map(\.key)) }
        let fast = PublishSubject<String?>()
        let slow = PublishSubject<String?>()
        var requests = 0
        let register: (Server, String) -> Observable<String?> = { server, _ in
            requests += 1
            return server === first ? fast : slow
        }
        manager.syncAllServers(token: "offline", register: register)
        fast.onNext("issued-key")
        XCTAssertEqual(saved, [["issued-key", "existing"]])
        manager.syncAllServers(token: "offline", register: register)
        XCTAssertEqual(requests, 2)
        fast.onCompleted()
        slow.onError(URLError(.notConnectedToInternet))
        XCTAssertEqual(second.key, "existing")
        manager.syncAllServers(token: "offline", register: { _, _ in .just(nil) })
        XCTAssertEqual(first.key, "issued-key")
    }

    @MainActor
    func testRegistrationDoesNotUndoResetOrRemoval() {
        let server = Server(address: "https://example.invalid", key: "old")
        let manager = ServerManager(servers: [server], persist: { _ in })
        let response = PublishSubject<String?>()
        manager.syncAllServers(token: "offline", register: { _, _ in response })
        server.key = "manually-reset"
        response.onNext("stale-response")
        XCTAssertEqual(server.key, "manually-reset")
        manager.removeServer(server: server)
        response.onNext("late-response")
        XCTAssertEqual(server.key, "manually-reset")
    }

    @MainActor
    func testFailedResetKeepsOldKeyAndSuccessPersistsBeforeRevocation() {
        let server = Server(address: "https://example.invalid", key: "working")
        var saved: [String] = []
        let manager = ServerManager(servers: [server]) { saved.append($0[0].key) }
        var requests: [String?] = []
        let failed = manager.resetServerKey(server: server, requestedKey: nil, token: "offline") { key, _ in
            requests.append(key)
            return .error(URLError(.notConnectedToInternet))
        }.subscribe()
        XCTAssertEqual(requests.count, 1)
        XCTAssertEqual(server.key, "working")
        XCTAssertTrue(saved.isEmpty)
        failed.dispose()
        let success = manager.resetServerKey(server: server, requestedKey: nil, token: "offline") { key, token in
            if token == "deleted" {
                XCTAssertEqual(key, "working")
                XCTAssertEqual(saved, ["replacement"])
                return .error(URLError(.timedOut))
            }
            return .just("replacement")
        }.subscribe()
        XCTAssertEqual(server.key, "replacement")
        success.dispose()
    }
}
