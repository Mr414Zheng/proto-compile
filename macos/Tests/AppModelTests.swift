import AppKit
import Foundation
import Darwin

@main
struct AppModelTests {
    static func mockEngine() throws {
        let request = try JSONSerialization.jsonObject(with: Data((readLine() ?? "").utf8)) as! [String: Any]
        let action = request["action"] as! String
        let language = request["output_language"] as? String ?? "java"
        let paths = request["input_paths"] as? [String] ?? []
        let slow = paths.first?.contains("slow") == true
        if slow { signal(SIGTERM, SIG_IGN) }
        if let path = request["preparation_output"] as? String {
            precondition(!FileManager.default.fileExists(atPath: path))
            try FileManager.default.createDirectory(atPath: path, withIntermediateDirectories: false)
            try Data(language.utf8).write(to: URL(fileURLWithPath: path).appendingPathComponent("artifact"))
        }
        let log = ProcessInfo.processInfo.environment["PROTO_COMPILE_MODEL_TEST_LOG"]!
        let line = try JSONSerialization.data(withJSONObject: request) + Data([10])
        let descriptor = open(log, O_WRONLY | O_APPEND | O_CREAT, 0o600)
        line.withUnsafeBytes { _ = write(descriptor, $0.baseAddress, $0.count) }
        close(descriptor)
        if slow { Thread.sleep(forTimeInterval: 0.5) }
        else { Thread.sleep(forTimeInterval: 0.04) }
        var response: [String: Any] = ["protocol_version": 1, "task_id": request["task_id"]!, "type": "result",
                                       "proto_count": 1, "java_count": language == "java" ? 1 : 0,
                                       "output_language": language, "objc_count": language == "objc" ? 2 : 0,
                                       "generated_count": language == "java" ? 1 : 2,
                                       "compiler_version": "mock-" + (request["target_runtime"] as! String),
                                       "runtime_version": request["target_runtime"]!, "warnings": [], "changes": [], "excluded": [], "source_files": paths]
        if paths.first?.contains("fail") == true {
            response["type"] = "error"
            response["code"] = "INVALID_INPUT"
            response["message_zh"] = "测试预检失败"
        } else if action == "inspect" {
            response["preparation_path"] = request["preparation_output"]
        } else if action == "convert" {
            let preparation = request["preparation_path"] as! String
            precondition(FileManager.default.fileExists(atPath: preparation + "/artifact"))
            let preparedLanguage = try String(contentsOfFile: preparation + "/artifact", encoding: .utf8)
            precondition(preparedLanguage == language, "conversion must reuse preparation for its language")
            let output = (request["output_parent"] as! String) + "/result-" + UUID().uuidString
            try FileManager.default.createDirectory(atPath: output, withIntermediateDirectories: true)
            response["output_path"] = output
        }
        FileHandle.standardOutput.write(try JSONSerialization.data(withJSONObject: response) + Data([10]))
    }

    @MainActor static func main() throws {
        if URL(fileURLWithPath: CommandLine.arguments[0]).lastPathComponent == "MockPreparationEngine" {
            try mockEngine()
            return
        }
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent("ProtoCompile-ModelTests-" + UUID().uuidString)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: folder) }
        let engine = folder.appendingPathComponent("MockPreparationEngine")
        try FileManager.default.createSymbolicLink(at: engine, withDestinationURL: URL(fileURLWithPath: CommandLine.arguments[0]).standardizedFileURL)
        let log = folder.appendingPathComponent("requests.ndjson")
        setenv("PROTO_COMPILE_MODEL_TEST_LOG", log.path, 1)
        let model = AppModel(engineExecutable: engine, temporaryParent: folder)
        precondition(model.outputLanguage == .java, "Java remains the default")
        precondition(AppModel.TargetRuntime.current.dependency(for: .objc) == "pod 'Protobuf', '3.25.9'")
        precondition(AppModel.TargetRuntime.compatible.dependency(for: .objc) == "pod 'Protobuf', '3.6.1'")
        precondition(AppModel.TargetRuntime.current.dependency(for: .java) == "com.google.protobuf:protobuf-java:3.25.9")
        func requests() -> [[String: Any]] {
            (try? String(contentsOf: log, encoding: .utf8).split(separator: "\n").map { try JSONSerialization.jsonObject(with: Data($0.utf8)) as! [String: Any] }) ?? []
        }
        func wait(_ reason: String, _ condition: () -> Bool) {
            let deadline = Date().addingTimeInterval(6)
            while !condition(), Date() < deadline { RunLoop.main.run(until: Date().addingTimeInterval(0.01)) }
            precondition(condition(), reason)
        }
        model.inputKind = .proto
        model.inputPaths = ["/tmp/normal.proto"]
        model.parametersChanged()
        wait("initial inspect", { model.inspection != nil && !model.isInspecting })
        let initial = model.inspection!
        let prepared = initial.preparation_path!
        let sessionRoot = URL(fileURLWithPath: prepared).deletingLastPathComponent().path
        precondition(FileManager.default.fileExists(atPath: prepared))
        model.setOutputParent(folder.path)
        precondition(model.inspection?.task_id == initial.task_id && model.canConvert && !model.isInspecting)
        precondition(requests().filter { $0["action"] as? String == "inspect" }.count == 1, "output selection must not reinspect")
        model.convert()
        model.setOutputLanguage(.objc)
        precondition(model.outputLanguage == .java, "conversion locks language changes")
        model.setOutputParent("/tmp/ignored-output")
        precondition(model.outputParent == folder.path, "conversion locks output changes")
        wait("convert", { model.result != nil && !model.isConverting })
        let conversion = requests().first { $0["action"] as? String == "convert" }!
        precondition(conversion["preparation_path"] as? String == prepared)
        precondition(FileManager.default.fileExists(atPath: prepared), "conversion does not consume preparation")
        model.setOutputLanguage(.objc)
        precondition(model.inspection == nil && model.result == nil && model.isInspecting, "language change clears results and starts a fresh inspection")
        precondition(!FileManager.default.fileExists(atPath: prepared), "language change clears completed preparation")
        wait("Objective-C inspect", { model.inspection?.output_language == "objc" && !model.isInspecting })
        let objcPreparation = model.inspection!.preparation_path!
        precondition(objcPreparation != prepared)
        model.setOutputLanguage(.objc)
        precondition(model.inspection?.preparation_path == objcPreparation && !model.isInspecting, "selecting the same language retains preparation")
        model.convert()
        model.setOutputLanguage(.java)
        precondition(model.outputLanguage == .objc && model.isConverting, "Objective-C conversion locks language changes")
        wait("Objective-C convert", { model.result?.output_language == "objc" && !model.isConverting })
        let objcConversion = requests().last { $0["action"] as? String == "convert" }!
        precondition(objcConversion["output_language"] as? String == "objc" && objcConversion["preparation_path"] as? String == objcPreparation)
        precondition(model.result?.java_count == 0 && model.result?.objc_count == 2 && model.result?.generatedFileCount == 2)
        precondition(model.stageDetail.contains("2 个 Objective-C"))
        model.setTargetRuntime(.compatible)
        precondition(!FileManager.default.fileExists(atPath: objcPreparation), "target change clears completed snapshot")
        wait("compatible inspect and version", { model.inspection?.runtime_version == "3.6.1" && model.compilerVersion == "mock-3.6.1" })
        model.inputPaths = ["/tmp/fail.proto"]
        model.parametersChanged()
        wait("failed inspect", { model.failure != nil && !model.isInspecting })
        let failedPath = requests().last { ($0["input_paths"] as? [String])?.first == "/tmp/fail.proto" }!["preparation_output"] as! String
        precondition(!FileManager.default.fileExists(atPath: failedPath), "failed inspect must clear its directory")
        model.inputPaths = ["/tmp/slow.proto"]
        model.parametersChanged()
        wait("slow inspect started", { requests().contains { ($0["input_paths"] as? [String])?.first == "/tmp/slow.proto" } })
        let slowPath = requests().last { ($0["input_paths"] as? [String])?.first == "/tmp/slow.proto" }!["preparation_output"] as! String
        model.setOutputLanguage(.java)
        precondition(FileManager.default.fileExists(atPath: slowPath), "active old inspect must retain directory until exit")
        wait("new inspect and cancelled process cleanup", { model.inspection?.output_language == "java" && !model.isInspecting && !FileManager.default.fileExists(atPath: slowPath) })
        model.inputPaths = ["/tmp/slow-shutdown.proto"]
        model.parametersChanged()
        wait("shutdown inspect started", { requests().contains { ($0["input_paths"] as? [String])?.first == "/tmp/slow-shutdown.proto" } })
        let shutdownPath = requests().last { ($0["input_paths"] as? [String])?.first == "/tmp/slow-shutdown.proto" }!["preparation_output"] as! String
        var stopped = false
        model.shutdown { stopped = true }
        precondition(!stopped && FileManager.default.fileExists(atPath: shutdownPath), "shutdown must wait for the active process before cleanup")
        wait("shutdown", { stopped })
        precondition(!FileManager.default.fileExists(atPath: sessionRoot), "shutdown must clear whole session")
        print("AppModel tests passed: output selection, language defaults, language cache invalidation and reuse, conversion language lock, target invalidation, failed inspect cleanup, cancelled run cleanup, shutdown")
    }
}
