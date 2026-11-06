//
//  ServerManager.swift
//  Bark
//
//  Created by huangfeng on 2018/3/21.
//  Copyright © 2018年 Fin. All rights reserved.
//

import RxCocoa
import RxSwift
import SwiftUI
import UIKit
import SwiftyJSON

let defaultServer = "https://bark.sanbox.cc"

class Server: Codable {
    let id: String
    let address: String
    var key: String
    var name: String?
    
    var host: String {
        return URL(string: address)?.host ?? ""
    }
    
    var displayName: String {
        if let name = name, !name.isEmpty {
            return name
        }
        return host
    }
    
    var cleanAddress: String {
        return address.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
    }
    
    var addressAndKey: String {
        guard !key.isEmpty else { return cleanAddress }
        return "\(cleanAddress)/\(key)"
    }
    
    init(id: String = UUID().uuidString, address: String, key: String) {
        self.id = id
        self.address = address
        self.key = key
    }
    
    enum CodingKeys: String, CodingKey {
        case id
        case address
        case key
        case name
    }
    
    // 解码
    required init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(String.self, forKey: .id)
        address = try container.decode(String.self, forKey: .address)
        key = try container.decode(String.self, forKey: .key)
        name = try? container.decode(String?.self, forKey: .name)
    }
}

class ServerManager: NSObject {
    static let shared = ServerManager()
    private var persistServers: ([Server]) -> Void = { Settings[.servers] = $0 }

    /// In-memory dependencies for regression tests; never touches saved servers.
    init(servers: [Server], persist: @escaping ([Server]) -> Void) {
        precondition(!servers.isEmpty)
        self.servers = servers
        self.currentServer = servers[0]
        self.persistServers = persist
        super.init()
    }
    override private init() {
        var servers: [Server] = []
        if let savedServers: [Server] = Settings[.servers] {
            servers = savedServers
        }

        if servers.count <= 0 {
            servers = [Server(id: UUID().uuidString, address: defaultServer, key: "")]
        }
        self.servers = servers
        let initialServer = servers[0]
        self.currentServer = initialServer

        super.init()

        // 将老版本数据转换成新版本
        if let key = Settings[.key] {
            let address = Settings[.currentServer] ?? defaultServer
            let server = Server(id: UUID().uuidString, address: address, key: key)

            self.servers = []
            self.addServer(server: server)

            Settings[.currentServerId] = server.id

            // 清空老版本数据
            Settings[.currentServer] = nil
            Settings[.key] = nil
        }

        if let currentServerId = Settings[.currentServerId] {
            self.setCurrentServer(serverId: currentServerId)
        }
    }

    /// 所有的 server
    var servers: [Server] = []
    /// 当前选中的 server ，在教程页显示。
    private(set) var currentServer: Server {
        didSet {
            currentServerUpdateRelay.accept(currentServer)
        }
    }
    let currentServerUpdateRelay = PublishRelay<Server>()
    /// All server-list cells must refresh as soon as a replacement is persisted,
    /// even if the subsequent old-key revocation fails or is still in flight.
    let serverKeyUpdatedRelay = PublishRelay<Void>()

    /// 更改当前选中的 server
    func setCurrentServer(serverId: String) {
        if let server = servers.first(where: { $0.id == serverId }) {
            currentServer = server
        } else {
            currentServer = servers.first!
        }
        Settings[.currentServerId] = serverId
    }

    /// 添加新的 server
    func addServer(server: Server) {
        self.servers.append(server)
        saveServers()
    }

    func updateServerKey(server: Server) {
        let foundServer = self.servers.first { $0.id == server.id }
        foundServer?.key = server.key
        saveServers()
        serverKeyUpdatedRelay.accept(())
        // 主要用于更新首页示例中的 key
        if server.id == currentServer.id {
            currentServerUpdateRelay.accept(currentServer)
        }
    }
    
    /// 移除 server，移除后如果 server 为`空`, `会新增一个默认server`
    func removeServer(server: Server) {
        self.servers.removeAll { $0.id == server.id }
        if self.servers.count <= 0 {
            self.servers.append(
                Server(id: UUID().uuidString, address: defaultServer, key: "")
            )
        }
        if self.currentServer.id == server.id {
            self.setCurrentServer(serverId: self.servers[0].id)
        }
        saveServers()
    }

    /// 保存 servers
    func saveServers() {
        persistServers(self.servers)
    }

    @available(*, unavailable)
    required init?(coder aDecoder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    var dispose: Disposable?
    private var syncingToken: String?
    private var syncingServerIDs: [String] = []
    /// 同步所有 server
    func syncAllServers() {
        guard let token = Client.shared.deviceToken.value, token.count > 0 else {
            return
        }
        syncAllServers(token: token) { server, token in
            BarkApi.provider.request(
                .register(
                    address: server.address,
                    key: server.key,
                    devicetoken: token
                ))
                .filterResponseError()
                .map { result -> String? in
                    switch result {
                    case .success(let json):
                        return Self.registrationKey(from: json)
                    case .failure:
                        return nil
                    }
                }
        }
    }

    static func registrationKey(from json: JSON) -> String? {
        guard let key = json["data", "key"].string, !key.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return nil }
        return key
    }

    func resetServerKey(
        server: Server, requestedKey: String?, token: String,
        register: @escaping (String?, String) -> Observable<String?>
    ) -> Observable<String?> {
        let oldKey = server.key
        return register(requestedKey, token).catchAndReturn(nil).flatMap { [weak self] issuedKey -> Observable<String?> in
            guard let self, let issuedKey, !issuedKey.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
                  self.servers.contains(where: { $0 === server }), server.key == oldKey else { return .just(nil) }
            // Persist a working replacement before invalidating the old link.
            // Closed enrollment and network failures leave the old key usable.
            server.key = issuedKey
            self.updateServerKey(server: server)
            guard !oldKey.isEmpty, oldKey != issuedKey else { return .just(issuedKey) }
            // Keep the replacement, but surface a failed revocation to the UI.
            return register(oldKey, "deleted").map { $0 == oldKey ? Optional(issuedKey) : nil }.catchAndReturn(nil)
        }
    }

    func syncAllServers(token: String, register: (Server, String) -> Observable<String?>) {
        guard !token.isEmpty else { return }
        let serverIDs = servers.map(\.id)
        guard syncingToken != token || syncingServerIDs != serverIDs else { return }
        dispose?.dispose()
        syncingToken = token
        syncingServerIDs = serverIDs
        let apis = servers.map { server -> Observable<(Server, String, String?)> in
            let originalKey = server.key
            return register(server, token)
                .catchAndReturn(nil)
                .map { (server, originalKey, $0) }
        }
        dispose = Observable
            .merge(apis)
            .observe(on: MainScheduler.instance)
            .subscribe(onNext: { [weak self] result in
                let (server, originalKey, newKey) = result
                guard let self, let newKey, !newKey.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
                      self.servers.contains(where: { $0 === server }),
                      server.key == originalKey else { return }
                server.key = newKey
                // Persist each successful response immediately. A slow server,
                // cancellation or app suspension must not lose another key.
                self.saveServers()
                self.serverKeyUpdatedRelay.accept(())
                if server.id == self.currentServer.id {
                    self.currentServerUpdateRelay.accept(server)
                }
            }, onCompleted: { [weak self] in
                self?.syncingToken = nil
            })
    }
    
    func setServerName(server: Server, name: String?) {
        server.name = name
        if server.id == currentServer.id {
            currentServerUpdateRelay.accept(currentServer)
        }
        saveServers()
    }
}
