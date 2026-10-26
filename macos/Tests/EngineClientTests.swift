import Foundation

@main
struct EngineClientTests {
    static func main() throws {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent("ProtoCompile-EngineClientTests-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: folder) }
        let request = EngineRequest(task_id: "test", action: "convert", input_kind: "proto", input_paths: [], include_paths: [], output_parent: nil, exclude_opt_0081: true, target_runtime: "3.25.9")
        let encodedRequest = try JSONSerialization.jsonObject(with: JSONEncoder().encode(request)) as! [String: Any]
        precondition(encodedRequest["output_language"] as? String == "java", "old request initializers must default to Java")
        let resultLine = "{\"protocol_version\":1,\"task_id\":\"test\",\"type\":\"result\",\"proto_count\":2,\"java_count\":3,\"output_path\":\"/tmp/result\"}"
        func run(_ name: String, script: String, cancelAfter: Double? = nil, cancelOnEvent: Bool = false, runtime: String = "3.25.9", language: String = "java", expectedCompilerVersion: String? = nil) throws -> Result<EngineMessage, EngineFailure> {
            let file = folder.appendingPathComponent(name)
            try ("#!/bin/sh\nread request\n" + script).write(to: file, atomically: true, encoding: .utf8)
            try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: file.path)
            let client = EngineRun(executable: file, expectedCompilerVersion: expectedCompilerVersion)
            let done = DispatchSemaphore(value: 0)
            let lock = NSLock()
            var response: Result<EngineMessage, EngineFailure>?
            let runtimeRequest = EngineRequest(task_id: request.task_id, action: request.action, input_kind: request.input_kind, input_paths: request.input_paths, include_paths: request.include_paths, output_parent: request.output_parent, exclude_opt_0081: request.exclude_opt_0081, target_runtime: runtime, output_language: language)
            client.start(runtimeRequest, event: { _ in if cancelOnEvent { client.cancel() } }) { value in
                lock.lock(); response = value; lock.unlock(); done.signal()
            }
            if let cancelAfter { DispatchQueue.global().asyncAfter(deadline: .now() + cancelAfter) { client.cancel() } }
            guard done.wait(timeout: .now() + 16) == .success else { fatalError("\(name): timeout") }
            lock.lock(); let received = response!; lock.unlock()
            return received
        }
        let objcLine = "{\"protocol_version\":1,\"task_id\":\"test\",\"type\":\"result\",\"output_language\":\"objc\",\"proto_count\":2,\"java_count\":0,\"objc_count\":4,\"generated_count\":4}"
        let objc = try run("objc", script: "case \"$request\" in *'\"output_language\":\"objc\"'*) ;; *) exit 5 ;; esac\nprintf '%s\\n' '\(objcLine)'\n", language: "objc")
        guard case .success(let objcMessage) = objc, objcMessage.output_language == "objc", objcMessage.java_count == 0, objcMessage.objc_count == 4, objcMessage.generatedFileCount == 4 else {
            fatalError("Objective-C selection and counts must cross the protocol")
        }
        func success(_ value: Result<EngineMessage, EngineFailure>) -> Bool {
            if case .success(let message) = value { return message.type == "result" && message.proto_count == 2 }
            return false
        }
        let partial = try run("partial", script: "printf '%s' '\(resultLine.prefix(40))'\nsleep 0.1\nprintf '%s\\n' '\(resultLine.dropFirst(40))'\n")
        precondition(success(partial), "partial NDJSON must be buffered")
        let wrongID = try run("wrong-id", script: "printf '%s\\n' '{\"protocol_version\":1,\"task_id\":\"stale\",\"type\":\"result\"}'\n")
        if case .success = wrongID { fatalError("wrong task ID must fail") }
        let malformed = try run("malformed", script: "printf 'not json\\n'\n")
        if case .success = malformed { fatalError("malformed stdout must fail") }
        let crash = try run("crash", script: "printf 'diagnostic only\\n' >&2\nexit 9\n")
        if case .success = crash { fatalError("crashed engine must fail") }
        // 模拟发布后忽略 SIGTERM，结果优先于客户端的取消意图。
        let published = try run("published", script: "trap '' TERM\nprintf '%s\\n' '\(resultLine)'\nprintf '%s\\n' '{\"protocol_version\":1,\"task_id\":\"test\",\"type\":\"event\",\"stage\":\"exporting\"}'\nsleep 0.5\nexit 0\n", cancelOnEvent: true)
        precondition(success(published), "published success must win cancellation race")
        let cancelled = try run("cancelled", script: "trap 'exit 0' TERM\nsleep 2\n", cancelAfter: 0.1)
        if case .success = cancelled { fatalError("cancel without result must fail") }
        for runtime in ["3.25.9", "3.6.1"] {
            let tagged = resultLine.dropLast() + ",\"runtime_version\":\"\(runtime)\"}"
            let response = try run("runtime-\(runtime)", script: "case \"$request\" in *'\"target_runtime\":\"\(runtime)\"'*) ;; *) exit 5 ;; esac\nprintf '%s\\n' '\(tagged)'\n", runtime: runtime)
            guard case .success(let message) = response, message.runtime_version == runtime else {
                fatalError("runtime target must reach engine and decode from result: \(runtime)")
            }
        }
        let directVersion = try run("direct-version", script: "printf 'libprotoc 25.9\\n'\n", expectedCompilerVersion: "libprotoc 25.9")
        guard case .success(let version) = directVersion, version.compiler_version == "libprotoc 25.9", version.runtime_version == "3.25.9" else {
            fatalError("direct compiler query must return actual validated version")
        }
        let wrongVersion = try run("wrong-version", script: "printf 'libprotoc 3.6.1\\n'\n", expectedCompilerVersion: "libprotoc 25.9")
        if case .success = wrongVersion { fatalError("mismatched bundled compiler version must fail") }
        let legacy = try JSONDecoder().decode(EngineMessage.self, from: Data(resultLine.utf8))
        precondition(legacy.runtime_version == nil && legacy.output_language == nil && legacy.objc_count == nil && legacy.generated_count == nil && legacy.resolvedOutputLanguage == "java" && legacy.generatedFileCount == 3, "old engine responses remain decodable with Java defaults")
        print("EngineClient tests passed: partial lines, task ID, malformed response, crash, publish/cancel race, cancellation, both runtime targets, direct compiler version, legacy response")
    }
}
