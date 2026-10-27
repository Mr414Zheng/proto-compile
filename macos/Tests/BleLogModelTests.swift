import AppKit
import Foundation
import Darwin

@main
struct BleLogModelTests {
    static func mockEngine() throws {
        let request = try JSONSerialization.jsonObject(with: Data((readLine() ?? "").utf8)) as! [String: Any]
        precondition(request["action"] as? String == "decode_ble")
        precondition(request["input_kind"] as? String == "html")
        precondition(request["include_paths"] as? [String] == [])
        precondition(request["exclude_opt_0081"] as? Bool == false)
        let log = ProcessInfo.processInfo.environment["PROTO_COMPILE_BLE_TEST_LOG"]!
        let encoded = try JSONSerialization.data(withJSONObject: request) + Data([10])
        try encoded.write(to: URL(fileURLWithPath: log))
        let slow = (request["log_path"] as! String).contains("slow")
        if slow { signal(SIGTERM, SIG_IGN) }
        let event: [String: Any] = ["protocol_version": 1, "task_id": request["task_id"]!, "type": "event", "stage": "decoding", "message_zh": "正在逐条解码", "processed": 2]
        FileHandle.standardOutput.write(try JSONSerialization.data(withJSONObject: event) + Data([10]))
        Thread.sleep(forTimeInterval: slow ? 0.4 : 0.08)
        if slow {
            let cancelled: [String: Any] = ["protocol_version": 1, "task_id": request["task_id"]!, "type": "error", "code": "CANCELLED", "message_zh": "解析已取消", "suggestion": "可重新选择日志后重试。"]
            FileHandle.standardOutput.write(try JSONSerialization.data(withJSONObject: cancelled) + Data([10]))
            return
        }
        let output = URL(fileURLWithPath: request["output_parent"] as! String).appendingPathComponent("result-" + UUID().uuidString)
        try FileManager.default.createDirectory(at: output, withIntermediateDirectories: false)
        let report = output.appendingPathComponent("蓝牙日志解析.txt")
        try Data("解析结果".utf8).write(to: report)
        let result: [String: Any] = ["protocol_version": 1, "task_id": request["task_id"]!, "type": "result", "output_path": output.path, "report_path": report.path, "record_count": 3, "decoded_count": 1, "warning_count": 1, "error_count": 1, "warnings": ["第 2 条方向不明确"], "source_files": request["input_paths"]!]
        FileHandle.standardOutput.write(try JSONSerialization.data(withJSONObject: result) + Data([10]))
    }

    @MainActor static func main() throws {
        if URL(fileURLWithPath: CommandLine.arguments[0]).lastPathComponent == "MockBleEngine" { try mockEngine(); return }
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent("ProtoCompile-BleTests-" + UUID().uuidString)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: folder) }
        let engine = folder.appendingPathComponent("MockBleEngine")
        try FileManager.default.createSymbolicLink(at: engine, withDestinationURL: URL(fileURLWithPath: CommandLine.arguments[0]).standardizedFileURL)
        let requestLog = folder.appendingPathComponent("request.ndjson")
        setenv("PROTO_COMPILE_BLE_TEST_LOG", requestLog.path, 1)
        let protocolFile = folder.appendingPathComponent("设备协议.HTML")
        let logFile = folder.appendingPathComponent("设备日志.log")
        let slowFile = folder.appendingPathComponent("slow.log")
        for file in [protocolFile, logFile, slowFile] { try Data("测试输入".utf8).write(to: file) }
        func wait(_ reason: String, _ condition: () -> Bool) {
            let deadline = Date().addingTimeInterval(6)
            while !condition(), Date() < deadline { RunLoop.main.run(until: Date().addingTimeInterval(0.01)) }
            precondition(condition(), reason)
        }
        func lastRequest() throws -> [String: Any] { try JSONSerialization.jsonObject(with: Data(contentsOf: requestLog)) as! [String: Any] }
        let model = BleLogModel(engineExecutable: engine)
        precondition(model.logFormat == .frames && !model.canParse)
        model.parse()
        precondition(model.failure?.title == "尚未选择日志文件" && !model.isParsing)
        model.setLogPath(folder.appendingPathComponent("missing.log").path)
        model.setProtocolPath(protocolFile.path)
        model.setOutputParent(folder.path)
        model.parse()
        precondition(model.failure?.title == "无法读取日志文件" && !model.isParsing)
        model.setLogPath(logFile.path)
        model.setProtocolPath(logFile.path)
        model.parse()
        precondition(model.failure?.title == "协议文档格式不支持")
        model.setProtocolPath(protocolFile.path)
        model.setOutputParent(logFile.path)
        model.parse()
        precondition(model.failure?.title == "无法写入导出目录")
        model.setOutputParent(folder.path)
        precondition(model.canParse && model.failure == nil)
        model.parse()
        model.setFormat(.json)
        model.setLogPath(slowFile.path)
        model.setOutputParent("/tmp/ignored")
        precondition(model.isParsing && !model.canParse && model.logFormat == .frames && model.logPath == logFile.path && model.outputParent == folder.path)
        wait("successful result containing record diagnostics", { model.result != nil && !model.busy })
        precondition(model.failure == nil && model.result?.record_count == 3 && model.result?.error_count == 1)
        precondition(model.stage == "解析完成" && model.stageDetail.contains("错误 1 条"))
        precondition(FileManager.default.fileExists(atPath: model.result!.report_path!))
        let framesRequest = try lastRequest()
        precondition(framesRequest["log_format"] as? String == "frames" && framesRequest["log_path"] as? String == logFile.path)
        precondition(framesRequest["input_paths"] as? [String] == [protocolFile.path])
        model.setFormat(.json)
        precondition(model.result == nil && model.canParse)
        model.parse()
        wait("JSON result", { model.result != nil && !model.busy })
        let jsonRequest = try lastRequest()
        precondition(jsonRequest["log_format"] as? String == "json")
        model.setLogPath(slowFile.path)
        model.parse()
        wait("progress with unknown total", { model.processed == 2 })
        precondition(model.total == nil)
        model.cancel()
        precondition(model.isCancelling && model.isParsing && !model.canParse)
        wait("cancel waits for engine exit", { !model.busy })
        precondition(model.failure?.title == "解析已取消" && model.result == nil)
        model.parse()
        wait("shutdown run started", { model.processed == 2 })
        var stopped = false
        model.shutdown { stopped = true }
        precondition(!stopped, "shutdown must await process exit")
        wait("shutdown completes after engine exits", { stopped })
        precondition(!model.canParse && !model.isParsing)
        print("BleLogModel tests passed: path validation, frames/JSON requests, progress, input lock, partial-record success, cancellation, shutdown")
    }
}
