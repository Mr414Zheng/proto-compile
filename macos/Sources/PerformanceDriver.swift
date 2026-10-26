import AppKit

/// 仅在显式测量参数下驱动真实窗口；常规运行不创建计时器或额外请求。
@MainActor
final class PerformanceDriver {
    private enum Phase: String { case launch, idle, inspectInput = "inspect_input", inspectOutput = "inspect_output", convert, after, shutdown }
    private let model: AppModel
    private let input: String
    private let output: String
    private let runtime: AppModel.TargetRuntime
    private var phase = Phase.launch
    private var phaseStarted = ProcessInfo.processInfo.systemUptime
    private var timer: Timer?
    private var finished = false

    static func markLaunchIfRequested() {
        if CommandLine.arguments.contains("--performance-test") { emitPhase("launch", state: "start") }
    }

    init(model: AppModel, arguments: [String]) throws {
        guard let index = arguments.firstIndex(of: "--performance-test"),
              arguments.count == index + 3 || arguments.count == index + 4 else {
            throw EngineFailure(title: "测量参数不完整", suggestion: "--performance-test <input> <outputParent> [3.25.9|3.6.1]", details: "")
        }
        let rawRuntime = arguments.count == index + 4 ? arguments[index + 3] : "3.25.9"
        guard let selected = AppModel.TargetRuntime(rawValue: rawRuntime) else {
            throw EngineFailure(title: "不支持的测量目标", suggestion: "请选择 3.25.9 或 3.6.1。", details: rawRuntime)
        }
        self.model = model
        self.input = URL(fileURLWithPath: arguments[index + 1]).standardizedFileURL.path
        self.output = URL(fileURLWithPath: arguments[index + 2]).standardizedFileURL.path
        self.runtime = selected
        model.targetRuntime = selected
    }

    func start() {
        phaseStarted = ProcessInfo.processInfo.systemUptime
        let timer = Timer(timeInterval: 0.04, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.tick() }
        }
        self.timer = timer
        RunLoop.main.add(timer, forMode: .common)
    }

    private func tick() {
        guard !finished else { return }
        let elapsed = ProcessInfo.processInfo.systemUptime - phaseStarted
        if elapsed > 60 { fail("阶段超时：\(phase.rawValue)"); return }
        if let failure = model.failure { fail("\(failure.title)\n\(failure.details)"); return }
        switch phase {
        case .launch:
            guard elapsed >= 0.5, model.compilerVersion != "正在读取…" else { return }
            guard model.compilerVersion != "读取失败" else { fail("启动时读取编译器版本失败"); return }
            endPhase()
            begin(.idle)
        case .idle:
            guard elapsed >= 2 else { return }
            endPhase()
            begin(.inspectInput)
            let ext = URL(fileURLWithPath: input).pathExtension.lowercased()
            model.inputKind = ["html", "htm"].contains(ext) ? .html : .proto
            model.inputPaths = [input]
            model.parametersChanged()
        case .inspectInput:
            guard !model.isInspecting else { return }
            guard model.inspection != nil, !model.canConvert, model.outputParent == nil else { fail("导入后的模型预检状态不符"); return }
            endPhase()
            begin(.inspectOutput)
            model.setOutputParent(output)
        case .inspectOutput:
            guard !model.isInspecting else { return }
            guard model.inspection != nil, model.canConvert else { fail("选择导出目录后的模型预检状态不符"); return }
            endPhase()
            begin(.convert)
            model.convert()
        case .convert:
            guard !model.isConverting else { return }
            guard let result = model.result, result.runtime_version == runtime.rawValue,
                  (result.proto_count ?? 0) > 0, (result.java_count ?? 0) > 0,
                  let path = result.output_path, FileManager.default.fileExists(atPath: path) else {
                fail("转换结果或运行库目标无效")
                return
            }
            endPhase()
            begin(.after)
        case .after:
            guard elapsed >= 2 else { return }
            endPhase()
            begin(.shutdown)
            finished = true
            timer?.invalidate()
            timer = nil
            model.shutdown { [self] in
                endPhase()
                Self.emit(["type": "result", "status": "passed", "proto_count": model.result?.proto_count ?? 0,
                           "java_count": model.result?.java_count ?? 0, "output_path": model.result?.output_path ?? "",
                           "runtime_version": model.result?.runtime_version ?? "", "compiler_version": model.compilerVersion,
                           "application_version": model.version, "idle_seconds": 2, "after_seconds": 2])
                exit(0)
            }
        case .shutdown: break
        }
    }

    private func begin(_ next: Phase) {
        phase = next
        phaseStarted = ProcessInfo.processInfo.systemUptime
        Self.emitPhase(next.rawValue, state: "start")
    }

    private func endPhase() { Self.emitPhase(phase.rawValue, state: "end") }

    private func fail(_ details: String) {
        finished = true
        timer?.invalidate()
        timer = nil
        Self.emit(["type": "result", "status": "failed", "phase": phase.rawValue, "details": details])
        model.shutdown { exit(1) }
    }

    private static func emitPhase(_ phase: String, state: String) { emit(["type": "phase", "phase": phase, "state": state]) }

    private static func emit(_ fields: [String: Any]) {
        var object = fields
        object["benchmark_version"] = 1
        object["pid"] = ProcessInfo.processInfo.processIdentifier
        object["monotonic_ns"] = DispatchTime.now().uptimeNanoseconds
        guard let data = try? JSONSerialization.data(withJSONObject: object, options: [.sortedKeys]) else { return }
        FileHandle.standardOutput.write(data + Data([10]))
    }
}
