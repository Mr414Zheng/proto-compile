import AppKit
import Combine
import UniformTypeIdentifiers

@MainActor
final class BleLogModel: ObservableObject {
    enum LogFormat: String, CaseIterable {
        case frames, json
        var label: String { self == .frames ? "完整 BLE 帧" : "格式化 JSON 日志" }
        var hint: String {
            self == .frames
                ? "纯文本，每行一条完整十六进制帧，不含空格、时间或 0x 前缀。空行可忽略。"
                : "支持带时间的 JSON 日志，读取 id、name 和 sentence；sentence 为 payload，不进行帧级 CRC 校验。"
        }
    }

    @Published private(set) var logFormat: LogFormat = .frames
    @Published private(set) var logPath: String?
    @Published private(set) var protocolPath: String?
    @Published private(set) var outputParent: String?
    @Published private(set) var isParsing = false
    @Published private(set) var isCancelling = false
    @Published private(set) var stage = "等待导入"
    @Published private(set) var stageDetail = "请选择日志文件、设备协议 HTML 文档和导出父目录。"
    @Published private(set) var processed: Int?
    @Published private(set) var total: Int?
    @Published private(set) var result: EngineMessage?
    @Published private(set) var failure: EngineFailure?
    private let engineExecutable: URL?
    private var activeID: String?
    @Published private var liveRuns: [String: EngineRun] = [:]
    private var shutdownCompletion: (() -> Void)?
    private var shuttingDown = false

    init(engineExecutable: URL? = nil) { self.engineExecutable = engineExecutable }

    var canParse: Bool { logPath != nil && protocolPath != nil && outputParent != nil && !isParsing && !shuttingDown && liveRuns.isEmpty }
    var busy: Bool { isParsing || !liveRuns.isEmpty || shuttingDown }

    func setFormat(_ format: LogFormat) {
        guard !busy, format != logFormat else { return }
        logFormat = format
        parametersChanged()
    }

    func setLogPath(_ path: String) {
        guard !busy else { return }
        logPath = URL(fileURLWithPath: path).standardizedFileURL.path
        parametersChanged()
    }

    func setProtocolPath(_ path: String) {
        guard !busy else { return }
        protocolPath = URL(fileURLWithPath: path).standardizedFileURL.path
        parametersChanged()
    }

    func setOutputParent(_ path: String) {
        guard !busy else { return }
        outputParent = URL(fileURLWithPath: path).standardizedFileURL.path
        parametersChanged()
    }

    private func parametersChanged() {
        result = nil
        failure = nil
        processed = nil
        total = nil
        stage = canParse ? "准备就绪" : "等待导入"
        stageDetail = canParse ? "将按所选协议逐条解析，并导出 UTF-8 文本。" : "请选择日志文件、设备协议 HTML 文档和导出父目录。"
    }

    func chooseLog() {
        guard !busy else { return }
        let panel = NSOpenPanel()
        panel.title = "选择\(logFormat.label)文件"
        panel.prompt = "导入"
        panel.message = logFormat.hint
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = false
        // 日志扩展名不统一，由解析引擎按所选格式检查实际内容。
        present(panel) { [weak self] url in self?.setLogPath(url.path) }
    }

    func chooseProtocol() {
        guard !busy else { return }
        let panel = NSOpenPanel()
        panel.title = "选择设备协议 HTML 文档"
        panel.prompt = "选择协议"
        panel.message = "请使用与设备协议版本对应的 HTML 文档。"
        panel.allowedContentTypes = [.html]
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = false
        present(panel) { [weak self] url in self?.setProtocolPath(url.path) }
    }

    func chooseOutput() {
        guard !busy else { return }
        let panel = NSOpenPanel()
        panel.title = "选择解析结果导出父目录"
        panel.prompt = "选择"
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.canCreateDirectories = true
        if let outputParent { panel.directoryURL = URL(fileURLWithPath: outputParent) }
        present(panel) { [weak self] url in self?.setOutputParent(url.path) }
    }

    private func present(_ panel: NSOpenPanel, selection: @escaping (URL) -> Void) {
        if let window = NSApp.mainWindow {
            panel.beginSheetModal(for: window) { response in
                if response == .OK, let url = panel.url { selection(url) }
            }
        } else if panel.runModal() == .OK, let url = panel.url { selection(url) }
    }

    private func validatePaths() -> EngineFailure? {
        let manager = FileManager.default
        for (path, label) in [(logPath, "日志文件"), (protocolPath, "协议文档")] {
            guard let path else { return EngineFailure(title: "尚未选择\(label)", suggestion: "请选择\(label)后重试。", details: "缺少\(label)路径。") }
            var directory: ObjCBool = false
            guard manager.fileExists(atPath: path, isDirectory: &directory), !directory.boolValue, manager.isReadableFile(atPath: path) else {
                return EngineFailure(title: "无法读取\(label)", suggestion: "请重新选择可读取的\(label)，并检查文件访问权限。", details: "文件路径：\(path)")
            }
        }
        guard let protocolPath, ["html", "htm"].contains(URL(fileURLWithPath: protocolPath).pathExtension.lowercased()) else {
            return EngineFailure(title: "协议文档格式不支持", suggestion: "请选择 .html 或 .htm 格式的设备协议文档。", details: "协议文档路径：\(protocolPath ?? "未选择")")
        }
        guard let outputParent else { return EngineFailure(title: "尚未选择导出父目录", suggestion: "请选择用于保存解析结果的父目录。", details: "缺少导出父目录路径。") }
        var directory: ObjCBool = false
        guard manager.fileExists(atPath: outputParent, isDirectory: &directory), directory.boolValue, manager.isWritableFile(atPath: outputParent) else {
            return EngineFailure(title: "无法写入导出目录", suggestion: "请重新选择可写入的父目录，并检查目录访问权限。", details: "目录路径：\(outputParent)")
        }
        return nil
    }

    func parse() {
        guard !busy else { return }
        result = nil
        failure = validatePaths()
        if let failure { stage = failure.title; stageDetail = failure.suggestion; return }
        let id = UUID().uuidString
        let run = engineExecutable.map { EngineRun(executable: $0) } ?? EngineRun()
        activeID = id
        liveRuns[id] = run
        isParsing = true
        isCancelling = false
        processed = nil
        total = nil
        stage = "正在解析日志"
        stageDetail = "正在启动本地解析引擎…"
        var request = EngineRequest(task_id: id, action: "decode_ble", input_kind: "html", input_paths: [protocolPath!], include_paths: [], output_parent: outputParent, exclude_opt_0081: false, target_runtime: AppModel.TargetRuntime.current.rawValue)
        request.log_path = logPath
        request.log_format = logFormat.rawValue
        run.start(request, processExit: { [weak self] in
            Task { @MainActor in
                guard let self else { return }
                // 取消超时会先交付失败；仍保留进程引用，直到收到真实退出信号。
                if self.activeID != id { self.liveRuns.removeValue(forKey: id) }
                self.finishShutdownIfNeeded()
            }
        }, event: { [weak self] message in
            Task { @MainActor in
                guard let self, self.activeID == id, !self.isCancelling else { return }
                let stages = ["reading": "读取日志", "parsing": "解析设备协议", "decoding": "解码蓝牙记录", "exporting": "导出解析结果"]
                self.stage = stages[message.stage ?? ""] ?? "正在解析日志"
                self.stageDetail = message.message_zh ?? "正在处理，请稍候。"
                self.processed = message.processed
                self.total = message.total
            }
        }) { [weak self] response in
            Task { @MainActor in
                guard let self else { return }
                if run.didFinishProcess { self.liveRuns.removeValue(forKey: id) }
                guard self.activeID == id else { self.finishShutdownIfNeeded(); return }
                self.activeID = nil
                self.isParsing = false
                self.isCancelling = false
                switch response {
                case .success(let message) where message.type == "result":
                    self.result = message
                    self.stage = "解析完成"
                    self.stageDetail = "共 \(message.record_count ?? 0) 条：成功 \(message.decoded_count ?? 0) 条，含警告 \(message.warning_count ?? 0) 条，错误 \(message.error_count ?? 0) 条。"
                case .success(let message):
                    var details = "错误代码：\(message.code ?? "UNKNOWN")"
                    if let file = message.file { details += "\n文件：\(file)" }
                    if let line = message.line { details += "\n行号：\(line)" }
                    if let context = message.details { details += "\n\(context)" }
                    self.failure = EngineFailure(title: message.message_zh ?? "蓝牙日志解析失败", suggestion: message.suggestion ?? "请检查日志格式、设备协议和导出目录后重试。", details: details)
                    self.stage = message.code == "CANCELLED" ? "解析已取消" : "解析失败"
                    self.stageDetail = self.failure!.suggestion
                case .failure(let failure):
                    self.failure = EngineFailure(title: failure.title.replacingOccurrences(of: "转换", with: "解析"), suggestion: failure.suggestion.replacingOccurrences(of: "转换", with: "解析"), details: failure.details)
                    self.stage = self.failure!.title
                    self.stageDetail = self.failure!.suggestion
                }
                self.finishShutdownIfNeeded()
            }
        }
    }

    func cancel() {
        guard isParsing, !isCancelling, let id = activeID else { return }
        isCancelling = true
        stage = "正在取消解析"
        stageDetail = "正在等待解析引擎停止并清理临时文件…"
        liveRuns[id]?.cancel()
    }

    func shutdown(_ completion: @escaping () -> Void) {
        shuttingDown = true
        shutdownCompletion = completion
        for run in liveRuns.values { run.cancel() }
        finishShutdownIfNeeded()
    }

    private func finishShutdownIfNeeded() {
        guard liveRuns.isEmpty, let completion = shutdownCompletion else { return }
        shutdownCompletion = nil
        completion()
    }

    func revealResult() {
        guard let path = result?.output_path else { return }
        if !NSWorkspace.shared.open(URL(fileURLWithPath: path, isDirectory: true)) {
            failure = EngineFailure(title: "无法打开结果目录", suggestion: "请在 Finder 中按下方路径查找解析结果。", details: "目录路径：\(path)")
        }
    }

    func openReport() {
        guard let path = result?.report_path else { return }
        if !NSWorkspace.shared.open(URL(fileURLWithPath: path)) {
            failure = EngineFailure(title: "无法打开解析文本", suggestion: "请在 Finder 中打开结果目录，并使用文本编辑器查看 .txt 文件。", details: "文件路径：\(path)")
        }
    }

    func copyError() {
        guard let failure else { return }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString("\(failure.title)\n\(failure.suggestion)\n\(failure.details)", forType: .string)
    }

    /// 截图入口只填充展示状态，不启动解析进程。
    func preview(_ state: String) {
        if state == "idle" || state == "empty" { return }
        logFormat = .json
        logPath = "/Users/developer/Logs/设备蓝牙日志.txt"
        protocolPath = "/Users/developer/Protocols/device protocol v16.2.html"
        outputParent = "/Users/developer/Exports"
        parametersChanged()
        if state == "success" {
            result = EngineMessage(protocol_version: 1, task_id: "preview", type: "result",
                                   output_path: "/Users/developer/Exports/BLE-20261027",
                                   report_path: "/Users/developer/Exports/BLE-20261027/蓝牙日志解析.txt",
                                   record_count: 128, decoded_count: 124, warning_count: 3, error_count: 1,
                                   warnings: ["第 17 条含未知字段，已保留原始编码供核对。", "第 83 条 payload 不完整，已保留原始内容和错误位置。"])
            stage = "解析完成"
            stageDetail = "共 128 条：成功 124 条，含警告 3 条，错误 1 条。"
        } else if state == "error" {
            failure = EngineFailure(title: "无法读取设备协议文档", suggestion: "请重新选择可读取的 HTML 协议文档，并检查文件访问权限。", details: "错误代码：FILE_NOT_FOUND\n文件：\(protocolPath!)")
            stage = "解析失败"
            stageDetail = failure!.suggestion
        } else if state == "busy" {
            isParsing = true
            processed = 76
            total = 128
            stage = "解码蓝牙记录"
            stageDetail = "正在按设备协议逐条解码，并保留异常记录。"
        }
    }
}
