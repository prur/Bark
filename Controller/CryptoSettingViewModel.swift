//
//  CryptoSettingViewModel.swift
//  Bark
//
//  Created by huangfeng on 2022/11/10.
//  Copyright © 2022 Fin. All rights reserved.
//

import CryptoSwift
import Foundation
import RxCocoa
import RxSwift

class CryptoSettingViewModel: ViewModel, ViewModelType {
    struct Input {
        let algorithmChanged: Driver<String>
        let modeChanged: Driver<String>
        let copyScript: Driver<CryptoSettingFields>
        let done: Driver<CryptoSettingFields>
    }

    struct Output {
        let initial: Driver<(algorithmList: [Algorithm], modeList: [String], paddingList: [String], initialFields: CryptoSettingFields?)>
        let modeListChanged: Driver<[String]>
        let paddingListChanged: Driver<[String]>
        let keyLengthChanged: Driver<Int>
        let showSnackbar: Driver<String>
        let done: Driver<Void>
        let copy: Driver<String>
    }

    struct Dependencies {
        let settingFieldRelay: BehaviorRelay<CryptoSettingFields?>
        let deviceKey: Driver<String>
        let serverAddress: Driver<String>
    }

    private let dependencies: Dependencies

    init(dependencies: Dependencies =
        Dependencies(
            settingFieldRelay: CryptoSettingRelay.shared.fields,
            // Key 好像没有对应的事件流，先“just”，懒得写了
            deviceKey: Driver.just(ServerManager.shared.currentServer.key),
            serverAddress: Driver.just(ServerManager.shared.currentServer.address)
        )
    ) {
        self.dependencies = dependencies
    }

    func transform(input: Input) -> Output {
        let showSnackbar = PublishRelay<String>()

        let modeList = input
            .algorithmChanged
            .compactMap { Algorithm(rawValue: $0) }
            .map { $0.modes }

        let paddingList = input
            .modeChanged
            .map { mode in
                if mode == "GCM" {
                    return ["noPadding"]
                } else {
                    return ["pkcs7"]
                }
            }

        let keyLength =
            Driver.merge([
                Driver.just(dependencies.settingFieldRelay.value)
                    .compactMap { $0 }
                    .compactMap { Algorithm(rawValue: $0.algorithm)?.keyLength },
                input
                    .algorithmChanged
                    .compactMap { Algorithm(rawValue: $0)?.keyLength }
            ])

        // 保存配置
        let done = input.done
            .filter { fields in
                do {
                    try AESCryptoModel.validate(cryptoFields: fields)
                    return true
                } catch {
                    showSnackbar.accept(error.rawString())
                    return false
                }
            }
        done.drive(onNext: { [weak self] fields in
            // 保存设置
            self?.dependencies.settingFieldRelay.accept(self?.preservingIv(fields))
        }).disposed(by: rx.disposeBag)

        let copyScript = input.copyScript
            .filter { [weak self] fields in
                do {
                    try AESCryptoModel.validate(cryptoFields: fields)
                    // 保存配置
                    self?.dependencies.settingFieldRelay.accept(self?.preservingIv(fields))
                    return true
                } catch {
                    showSnackbar.accept(error.rawString())
                    return false
                }
            }
        let copy = Driver.combineLatest(copyScript, dependencies.serverAddress)
            .compactMap { fields, address in try? Self.senderExample(fields: fields, serverAddress: address) }

        return Output(
            initial: Driver.just((
                algorithmList: [Algorithm.aes128, Algorithm.aes192, Algorithm.aes256],
                modeList: ["CBC", "ECB", "GCM"],
                paddingList: ["pkcs7"],
                initialFields: dependencies.settingFieldRelay.value
            )),
            modeListChanged: modeList,
            paddingListChanged: paddingList,
            keyLengthChanged: keyLength,
            showSnackbar: showSnackbar.asDriver(onErrorDriveWith: .empty()),
            done: done.map { _ in () },
            copy: copy
        )
    }

    /// No secrets or fixed nonces are embedded in the copied, reusable example.
    static func senderEndpoint(serverAddress: String) throws -> String {
        guard let url = URL(string: serverAddress), url.host != nil,
              ["https", "http"].contains(url.scheme) else { throw "Invalid server address" }
        return url.appendingPathComponent("push").absoluteString
    }

    static func senderExample(fields: CryptoSettingFields, serverAddress: String) throws -> String {
        let endpoint = String(decoding: try JSONEncoder().encode(senderEndpoint(serverAddress: serverAddress)), as: UTF8.self)
        let keyEncoding = fields.keyEncoding == "base64" ? "base64" : "utf8"
        let ivLength = ["CBC": 16, "GCM": 12][fields.mode] ?? 0
        return """
        // Set ZBARK_DEVICE_KEY and ZBARK_CRYPTO_KEY privately before running.
        // ZBARK_CRYPTO_KEY uses \(keyEncoding) encoding for this configuration.
        const crypto = require('node:crypto');
        const pushUrl = \(endpoint);
        if (!process.env.ZBARK_DEVICE_KEY || !process.env.ZBARK_CRYPTO_KEY) throw new Error('Missing private key configuration');
        const key = Buffer.from(process.env.ZBARK_CRYPTO_KEY, '\(keyEncoding)');
        const iv = \(ivLength) ? crypto.randomBytes(\(ivLength)) : null;
        const json = JSON.stringify({id: crypto.randomUUID(), body: 'Encrypted test', isArchive: '1'});
        const cipher = crypto.createCipheriv('aes-\(fields.algorithm.suffix(3))-\(fields.mode.lowercased())', key, iv);
        cipher.setAutoPadding(\(fields.padding == "pkcs7" ? "true" : "false"));
        const encrypted = Buffer.concat([cipher.update(json, 'utf8'), cipher.final()]);
        const ciphertext = \(fields.mode == "GCM" ? "Buffer.concat([encrypted, cipher.getAuthTag()])" : "encrypted").toString('base64');
        const payload = {device_key: process.env.ZBARK_DEVICE_KEY, ciphertext};
        if (iv) { payload.iv = iv.toString('base64'); payload.iv_encoding = 'base64'; }
        (async () => {
          const response = await fetch(pushUrl, {
            method: 'POST', redirect: 'error', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(payload)
          });
          const result = await response.json();
          console.log({http_status: response.status, code: result.code});
          if (!response.ok || result.code !== 200) process.exitCode = 1;
        })().catch(() => { console.error('Push failed'); process.exitCode = 1; });
        """
    }

    /// IV 不再由用户在设置页输入，这里保留老用户已保存的 iv 作为兜底
    /// 仅保留与当前 mode 长度匹配的 iv，避免切换 mode 后 iv 不适用
    private func preservingIv(_ fields: CryptoSettingFields) -> CryptoSettingFields {
        var fields = fields
        guard let iv = dependencies.settingFieldRelay.value?.iv else {
            return fields
        }
        let expect = ["CBC": 16, "GCM": 12][fields.mode] ?? 0
        let previous = dependencies.settingFieldRelay.value
        let bytes = previous?.ivEncoding == "base64" ? Data(base64Encoded: iv) : iv.data(using: .utf8)
        fields.iv = bytes?.count == expect ? iv : nil
        fields.ivEncoding = fields.iv == nil ? nil : previous?.ivEncoding
        return fields
    }
}
