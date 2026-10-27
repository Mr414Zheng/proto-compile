import AppKit
import Darwin

/// 仅显式测量参数启用；使用正式窗口与模型，正常运行不创建采样计时器。
@MainActor
final class BlePerformanceDriver {
    private let model: AppModel
    private let ble: BleLogModel
    private let selectMode: (Int) -> Void
    private let scenario: String
    private let count: Int
    private var timer: Timer?
    private var index = 0
    private var finished = false
    private var started = ProcessInfo.processInfo.systemUptime

    init(model: AppModel, ble: BleLogModel, arguments: [String], selectMode: @escaping (Int) -> Void) throws {
        self.model = model; self.ble = ble; self.selectMode = selectMode
        guard let position = arguments.firstIndex(of: "--ble-performance-test") else { throw Self.invalidArguments() }
        let values = Array(arguments.dropFirst(position + 1))
        guard let scenario = values.first else { throw Self.invalidArguments() }
        self.scenario = scenario
        if scenario == "switch", values.count == 3, ["empty", "success", "error"].contains(values[1]),
           let count = Int(values[2]), (2...1000).contains(count), count % 2 == 0 {
            self.count = count
            model.preview(values[1]); ble.preview(values[1])
        } else if scenario == "parse", values.count == 5, let format = BleLogModel.LogFormat(rawValue: values[3]) {
            self.count = 0
            ble.setFormat(format); ble.setProtocolPath(values[1]); ble.setLogPath(values[2]); ble.setOutputParent(values[4])
        } else { throw Self.invalidArguments() }
    }

    func start() {
        started = ProcessInfo.processInfo.systemUptime
        emit("launch")
        DispatchQueue.main.asyncAfter(deadline: .now() + 2) { [self] in
            emit("before_first_visit")
            selectMode(1)
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) { [self] in
                if scenario == "switch" { selectMode(0) }
                emit("idle_start")
                DispatchQueue.main.asyncAfter(deadline: .now() + 2) { [self] in
                    emit("ready")
                    if scenario == "switch" { startSwitching() } else { startParsing() }
                }
            }
        }
    }

    private func startSwitching() {
        emit("switch", index: 0)
        selectMode(1)
        timer = Timer.scheduledTimer(withTimeInterval: 0.25, repeats: true) { [weak self] _ in
            Task { @MainActor in
                guard let self, !self.finished else { return }
                self.index += 1
                self.emit("switch", index: self.index)
                if self.index < self.count { self.selectMode((self.index + 1) % 2) }
                else {
                    self.timer?.invalidate(); self.timer = nil
                    DispatchQueue.main.asyncAfter(deadline: .now() + 2) { [self] in self.complete() }
                }
            }
        }
    }

    private func startParsing() {
        emit("parse_start")
        ble.parse()
        timer = Timer.scheduledTimer(withTimeInterval: 0.02, repeats: true) { [weak self] _ in
            Task { @MainActor in
                guard let self, !self.finished else { return }
                if ProcessInfo.processInfo.systemUptime - self.started > 120 { self.complete(error: "解析测量超时"); return }
                guard !self.ble.busy else { return }
                if let failure = self.ble.failure { self.complete(error: failure.title + "：" + failure.details); return }
                guard let result = self.ble.result, let path = result.report_path,
                      FileManager.default.fileExists(atPath: path), (result.record_count ?? 0) > 0 else {
                    self.complete(error: "解析未产生有效结果"); return
                }
                self.timer?.invalidate(); self.timer = nil
                self.emit("parse_end", extra: ["report_path": path, "record_count": result.record_count ?? 0,
                                               "decoded_count": result.decoded_count ?? 0, "warning_count": result.warning_count ?? 0,
                                               "error_count": result.error_count ?? 0])
                DispatchQueue.main.asyncAfter(deadline: .now() + 2) { [self] in self.complete() }
            }
        }
    }

    private func complete(error: String? = nil) {
        guard !finished else { return }
        finished = true; timer?.invalidate(); timer = nil
        emit("result", extra: ["status": error == nil ? "passed" : "failed", "error": error ?? ""])
        var remaining = 2
        let done = { remaining -= 1; if remaining == 0 { exit(error == nil ? 0 : 1) } }
        model.shutdown(done); ble.shutdown(done)
    }

    private func emit(_ phase: String, index: Int? = nil, extra: [String: Any] = [:]) {
        var usage = rusage(); getrusage(RUSAGE_SELF, &usage)
        var children = rusage(); getrusage(RUSAGE_CHILDREN, &children)
        var info = mach_task_basic_info()
        var size = mach_msg_type_number_t(MemoryLayout<mach_task_basic_info>.size / MemoryLayout<integer_t>.size)
        let status = withUnsafeMutablePointer(to: &info) { pointer in
            pointer.withMemoryRebound(to: integer_t.self, capacity: Int(size)) {
                task_info(mach_task_self_, task_flavor_t(MACH_TASK_BASIC_INFO), $0, &size)
            }
        }
        var row: [String: Any] = ["benchmark_version": 1, "phase": phase, "scenario": scenario,
                                  "pid": ProcessInfo.processInfo.processIdentifier,
                                  "monotonic_ns": DispatchTime.now().uptimeNanoseconds,
                                  "cpu_s": Double(usage.ru_utime.tv_sec + usage.ru_stime.tv_sec) + Double(usage.ru_utime.tv_usec + usage.ru_stime.tv_usec) / 1_000_000,
                                  "max_rss_bytes": usage.ru_maxrss,
                                  "reaped_child_cpu_s": Double(children.ru_utime.tv_sec + children.ru_stime.tv_sec) + Double(children.ru_utime.tv_usec + children.ru_stime.tv_usec) / 1_000_000]
        if status == KERN_SUCCESS { row["rss_bytes"] = info.resident_size }
        if let index { row["index"] = index }
        row.merge(extra) { _, new in new }
        if let data = try? JSONSerialization.data(withJSONObject: row, options: [.sortedKeys]) {
            FileHandle.standardOutput.write(data + Data([10]))
        }
    }

    private static func invalidArguments() -> EngineFailure {
        EngineFailure(title: "性能测量参数无效", suggestion: "--ble-performance-test switch <empty|success|error> <偶数次数> 或 parse <协议> <日志> <frames|json> <输出目录>", details: "")
    }
}
