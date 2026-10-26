import Foundation
import Darwin

struct EngineRequest: Encodable {
    let protocol_version = 1
    let task_id: String
    let action: String
    let input_kind: String
    let input_paths: [String]
    let include_paths: [String]
    let output_parent: String?
    let exclude_opt_0081: Bool
    let target_runtime: String
    var output_language: String = "java"
    var preparation_output: String? = nil
    var preparation_path: String? = nil
}

struct EngineMessage: Decodable {
    let protocol_version: Int
    let task_id: String
    let type: String
    var stage: String?
    var message_zh: String?
    var processed: Int?
    var total: Int?
    var output_path: String?
    var proto_count: Int?
    var java_count: Int?
    var output_language: String?
    var objc_count: Int?
    var generated_count: Int?
    var compiler_version: String?
    var runtime_version: String?
    var preparation_path: String?
    var warnings: [String]?
    var changes: [String]?
    var excluded: [String]?
    var source_files: [String]?
    var code: String?
    var suggestion: String?
    var file: String?
    var line: Int?
    var details: String?

    var resolvedOutputLanguage: String { output_language ?? "java" }
    var generatedFileCount: Int { generated_count ?? (resolvedOutputLanguage == "objc" ? objc_count : java_count) ?? 0 }
}

struct EngineFailure: Error {
    var title: String
    var suggestion: String
    var details: String
}

/// 每次请求独占进程与管道；后台按行读取，终止后再交付最终结果。
final class EngineRun: @unchecked Sendable {
    private let lock = NSLock()
    private var process: Process?
    private var cancelled = false
    private var forced = false
    private var completed = false
    private var delivered = false
    private var completionHandler: ((Result<EngineMessage, EngineFailure>) -> Void)?
    private var errorOutput = Data()
    private let executable: URL
    private let expectedCompilerVersion: String?

    init(executable: URL = Bundle.main.bundleURL.appendingPathComponent("Contents/Helpers/ConversionEngine.app/Contents/MacOS/ConversionEngine"), expectedCompilerVersion: String? = nil) {
        self.executable = executable
        self.expectedCompilerVersion = expectedCompilerVersion
    }

    var didFinishProcess: Bool { lock.lock(); defer { lock.unlock() }; return completed }

    func start(_ request: EngineRequest, processExit: @escaping () -> Void = {}, event: @escaping (EngineMessage) -> Void,
               completion: @escaping (Result<EngineMessage, EngineFailure>) -> Void) {
        lock.lock(); completionHandler = completion; lock.unlock()
        DispatchQueue.global(qos: .userInitiated).async { [self] in
            let worker = Process()
            let input = Pipe(), output = Pipe(), errors = Pipe()
            worker.executableURL = executable
            if expectedCompilerVersion != nil { worker.arguments = ["--version"] }
            worker.standardInput = input
            worker.standardOutput = output
            worker.standardError = errors
            errors.fileHandleForReading.readabilityHandler = { [weak self] handle in
                let data = handle.availableData
                guard let self, !data.isEmpty else { return }
                self.lock.lock()
                if self.errorOutput.count < 131072 { self.errorOutput.append(data.prefix(131072 - self.errorOutput.count)) }
                self.lock.unlock()
            }
            var terminal: EngineMessage?
            var protocolError: String?
            do {
                let encoded = try JSONEncoder().encode(request) + Data([10])
                // run 与取消共用锁，避免取消发生在进程尚未启动的间隙。
                lock.lock()
                if cancelled {
                    lock.unlock()
                    throw EngineFailure(title: "操作已取消", suggestion: "可重新选择输入后重试。", details: "请求尚未启动。")
                }
                do { try worker.run(); process = worker; lock.unlock() }
                catch { lock.unlock(); throw error }
                if expectedCompilerVersion == nil { try input.fileHandleForWriting.write(contentsOf: encoded) }
                try input.fileHandleForWriting.close()
                var buffer = Data()
                func consume(_ line: Data) {
                    guard !line.isEmpty else { return }
                    if let expectedCompilerVersion {
                        let actual = String(decoding: line, as: UTF8.self)
                        guard terminal == nil, actual == expectedCompilerVersion else {
                            protocolError = "内置编译器版本不匹配：期望 \(expectedCompilerVersion)，实际输出 \(actual)。"
                            return
                        }
                        terminal = EngineMessage(protocol_version: 1, task_id: request.task_id, type: "result", proto_count: 0, java_count: 0, output_language: request.output_language, objc_count: 0, generated_count: 0, compiler_version: actual, runtime_version: request.target_runtime)
                        return
                    }
                    do {
                        let message = try JSONDecoder().decode(EngineMessage.self, from: line)
                        guard message.protocol_version == 1, message.task_id == request.task_id else {
                            protocolError = "引擎响应的协议版本或任务 ID 不匹配。"
                            return
                        }
                        if message.type == "event" { event(message) }
                        else if message.type == "result" || message.type == "error" { terminal = message }
                        else { protocolError = "未知响应类型：\(message.type)" }
                    } catch { protocolError = "无法解析引擎响应：\(error.localizedDescription)" }
                }
                while true {
                    let chunk = output.fileHandleForReading.availableData
                    if chunk.isEmpty { break }
                    buffer.append(chunk)
                    while let newline = buffer.firstIndex(of: 10) {
                        consume(Data(buffer[..<newline]))
                        buffer.removeSubrange(...newline)
                    }
                    if buffer.count > 8_388_608 { protocolError = "引擎响应超过单行大小限制。"; worker.terminate(); break }
                }
                if !buffer.isEmpty { consume(buffer) }
                worker.waitUntilExit()
                errors.fileHandleForReading.readabilityHandler = nil
                lock.lock()
                completed = true
                let wasCancelled = cancelled, wasForced = forced
                let diagnostic = String(decoding: errorOutput, as: UTF8.self)
                lock.unlock()
                processExit()
                // 发布临界区可能先完成再收到取消；有效成功结果代表已实际发布。
                if let terminal, terminal.type == "result", worker.terminationStatus == 0, protocolError == nil {
                    deliver(.success(terminal))
                } else if wasCancelled {
                    let cleanupConfirmed = terminal?.code == "CANCELLED" && !wasForced
                    deliver(.failure(EngineFailure(title: "转换已取消", suggestion: cleanupConfirmed ? "引擎已结束，可重新转换。" : "引擎已停止；请检查导出目录是否有残留临时文件后重试。", details: terminal?.details ?? (wasForced ? "引擎未在时限内退出，已强制停止；无法确认清理是否完成。" : "已发送取消请求，进程已退出；未收到清理完成确认。"))))
                } else if let protocolError {
                    deliver(.failure(EngineFailure(title: "无法读取转换结果", suggestion: "请重新打开应用后重试；若问题持续，请复制详情。", details: protocolError)))
                } else if let terminal {
                    if terminal.type == "result", worker.terminationStatus != 0 {
                        deliver(.failure(EngineFailure(title: "转换引擎异常退出", suggestion: "请重试，并检查导出目录。", details: "退出码：\(worker.terminationStatus)\n\(diagnostic)")))
                    } else { deliver(.success(terminal)) }
                } else {
                    deliver(.failure(EngineFailure(title: "转换引擎意外停止", suggestion: "请检查输入文件后重试；若问题持续，请复制详情。", details: "退出码：\(worker.terminationStatus)\n\(diagnostic)")))
                }
            } catch {
                errors.fileHandleForReading.readabilityHandler = nil
                try? input.fileHandleForWriting.close()
                if worker.isRunning { worker.terminate(); worker.waitUntilExit() }
                lock.lock(); completed = true; lock.unlock()
                processExit()
                deliver(.failure(error as? EngineFailure ?? EngineFailure(title: "无法启动转换引擎", suggestion: "请确认应用包完整，并将应用复制到本机后重试。", details: error.localizedDescription)))
            }
        }
    }

    private func deliver(_ result: Result<EngineMessage, EngineFailure>) {
        lock.lock()
        guard !delivered else { lock.unlock(); return }
        delivered = true
        let handler = completionHandler
        completionHandler = nil
        lock.unlock()
        handler?(result)
    }

    func cancel() {
        lock.lock()
        guard !completed else { lock.unlock(); return }
        cancelled = true
        if let process, process.isRunning { process.terminate() }
        lock.unlock()
        DispatchQueue.global().asyncAfter(deadline: .now() + 10) { [self] in
            lock.lock()
            if !completed, let process, process.isRunning {
                forced = true
                kill(process.processIdentifier, SIGKILL)
            }
            lock.unlock()
        }
        // 子进程若继承 stdout 并阻止 EOF，UI 仍需得到明确的取消失败说明。
        DispatchQueue.global().asyncAfter(deadline: .now() + 12) { [self] in
            lock.lock()
            let stillWaiting = !completed
            lock.unlock()
            if stillWaiting {
                deliver(.failure(EngineFailure(title: "取消等待超时", suggestion: "已尝试强制停止引擎；无法确认临时文件已清理，请检查导出目录。", details: "取消后 12 秒仍未收到进程结束信号或管道 EOF。")))
            }
        }
    }
}
