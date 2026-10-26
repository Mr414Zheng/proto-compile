import AppKit
import Combine
import UniformTypeIdentifiers

@MainActor
final class AppModel: ObservableObject {
    enum InputKind: String, CaseIterable { case html, proto
        var label: String { self == .html ? "HTML 文档" : "Proto 文件 / 目录" }
    }
    enum TargetRuntime: String, CaseIterable {
        case current = "3.25.9"
        case compatible = "3.6.1"
        func label(for language: OutputLanguage) -> String {
            self == .current ? "\(language.runtimeName) 3.25.9（默认）" : "兼容 \(language.runtimeName) 3.6.1"
        }
        func dependency(for language: OutputLanguage) -> String {
            language == .java ? "com.google.protobuf:protobuf-java:\(rawValue)" : "pod 'Protobuf', '\(rawValue)'"
        }
        var expectedCompilerVersion: String { self == .current ? "libprotoc 25.9" : "libprotoc 3.6.1" }
    }
    enum OutputLanguage: String, CaseIterable {
        case java, objc
        var label: String { self == .java ? "Java" : "Objective-C" }
        var runtimeName: String { self == .java ? "protobuf-java" : "Protobuf Objective-C" }
    }
    @Published var targetRuntime: TargetRuntime = .current
    @Published private(set) var outputLanguage: OutputLanguage = .java
    @Published var inputKind: InputKind = .html
    @Published var inputPaths: [String] = []
    @Published var includePaths: [String] = []
    @Published var outputParent: String?
    @Published var excludeOPT0081 = true
    @Published var isInspecting = false
    @Published var isConverting = false
    @Published var isCancelling = false
    @Published var stage = "等待导入"
    @Published var stageDetail = "选择源文件，检查通过后即可转换。"
    @Published var inspection: EngineMessage?
    @Published var result: EngineMessage?
    @Published var failure: EngineFailure?
    @Published var compilerVersion = "正在读取…"
    @Published var showAbout = false
    private var activeRun: EngineRun?
    private var versionRun: EngineRun?
    private var versionID: String?
    private var activeID: String?
    private var liveRuns: [String: EngineRun] = [:]
    private var shutdownCompletion: (() -> Void)?
    private let engineExecutable: URL?
    private let temporaryParent: URL
    private var sessionRoot: URL?
    private var preparationByRun: [String: URL] = [:]
    private var preparedDirectory: URL?
    private var awaitingProcessCleanup: [String: URL] = [:]

    init(engineExecutable: URL? = nil, temporaryParent: URL? = nil) {
        self.engineExecutable = engineExecutable
        self.temporaryParent = temporaryParent ?? FileManager.default.temporaryDirectory
    }

    private func makeRun() -> EngineRun {
        if let engineExecutable { return EngineRun(executable: engineExecutable) }
        return EngineRun()
    }

    private func makeVersionRun() -> EngineRun {
        // 测试注入仍使用协议引擎，正常查询直接运行所选编译器，避免启动解释器。
        if engineExecutable != nil { return makeRun() }
        let executable = Bundle.main.bundleURL.appendingPathComponent("Contents/Helpers/ConversionEngine.app/Contents/Frameworks/toolchains/\(targetRuntime.rawValue)/bin/protoc")
        return EngineRun(executable: executable, expectedCompilerVersion: targetRuntime.expectedCompilerVersion)
    }

    private func preparationOutput() throws -> URL {
        if sessionRoot == nil {
            let root = temporaryParent.appendingPathComponent("ProtoCompile-Session-" + UUID().uuidString, isDirectory: true)
            try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
            sessionRoot = root
        }
        return sessionRoot!.appendingPathComponent(UUID().uuidString, isDirectory: true)
    }

    private func removePreparation(_ directory: URL?) {
        guard let directory else { return }
        try? FileManager.default.removeItem(at: directory)
    }

    private func processExited(id: String) {
        if let directory = awaitingProcessCleanup.removeValue(forKey: id) { removePreparation(directory) }
        finishShutdownIfNeeded()
    }

    var version: String { Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "—" }
    var build: String { Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String ?? "—" }
    var canConvert: Bool { inspection != nil && outputParent != nil && !isInspecting && !isConverting && !inputPaths.isEmpty }
    var busy: Bool { isConverting }

    func loadVersion() {
        versionRun?.cancel()
        let id = UUID().uuidString
        versionID = id
        compilerVersion = "正在读取…"
        let run = makeVersionRun()
        versionRun = run
        liveRuns[id] = run
        let request = makeRequest(action: "version", id: id)
        run.start(request, event: { _ in }) { [weak self] response in
            Task { @MainActor in
                guard let self else { return }
                self.liveRuns.removeValue(forKey: id)
                guard self.versionID == id else { self.finishShutdownIfNeeded(); return }
                switch response {
                case .success(let message): self.compilerVersion = message.type == "result" ? (message.compiler_version ?? "读取失败") : "读取失败"
                case .failure: self.compilerVersion = "读取失败"
                }
                self.versionRun = nil
                self.versionID = nil
                self.finishShutdownIfNeeded()
            }
        }
    }

    func setTargetRuntime(_ target: TargetRuntime) {
        guard !busy, targetRuntime != target else { return }
        targetRuntime = target
        parametersChanged()
        loadVersion()
    }

    func copyDependency() {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(targetRuntime.dependency(for: outputLanguage), forType: .string)
    }

    func setOutputLanguage(_ language: OutputLanguage) {
        guard !busy, outputLanguage != language else { return }
        outputLanguage = language
        parametersChanged()
    }

    func setKind(_ kind: InputKind) {
        guard !busy, inputKind != kind else { return }
        inputKind = kind
        inputPaths = []
        parametersChanged()
    }

    func chooseInput() {
        guard !busy else { return }
        let panel = NSOpenPanel()
        panel.title = inputKind == .html ? "选择 HTML 协议文档" : "选择 Proto 文件或目录"
        panel.prompt = "导入"
        panel.canChooseFiles = true
        panel.canChooseDirectories = inputKind == .proto
        panel.allowsMultipleSelection = inputKind == .proto
        panel.allowedContentTypes = inputKind == .html ? [UTType.html] : [UTType(filenameExtension: "proto") ?? .data]
        present(panel) { [weak self] urls in
            guard let self else { return }
            self.inputPaths = urls.map(\.path)
            self.parametersChanged()
        }
    }

    func addInclude() {
        guard !busy else { return }
        let panel = NSOpenPanel()
        panel.title = "添加 Proto 依赖目录"
        panel.prompt = "添加"
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = true
        present(panel) { [weak self] urls in
            guard let self else { return }
            for url in urls where !self.includePaths.contains(url.path) { self.includePaths.append(url.path) }
            self.parametersChanged()
        }
    }

    func removeInclude(_ path: String) {
        guard !busy else { return }
        includePaths.removeAll { $0 == path }
        parametersChanged()
    }

    func chooseOutput() {
        guard !busy else { return }
        let panel = NSOpenPanel()
        panel.title = "选择导出父目录"
        panel.prompt = "选择"
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.canCreateDirectories = true
        if let outputParent { panel.directoryURL = URL(fileURLWithPath: outputParent) }
        present(panel) { [weak self] urls in
            guard let self, let url = urls.first else { return }
            self.setOutputParent(url.path)
        }
    }

    func setOutputParent(_ path: String) {
        guard !busy else { return }
        outputParent = path
        result = nil
        if inspection != nil {
            stage = "输入检查通过"
            stageDetail = "准备就绪，可以开始转换。"
        }
    }

    private func present(_ panel: NSOpenPanel, selection: @escaping ([URL]) -> Void) {
        if let window = NSApp.mainWindow {
            panel.beginSheetModal(for: window) { response in
                if response == .OK { selection(panel.urls) }
            }
        } else if panel.runModal() == .OK { selection(panel.urls) }
    }

    func parametersChanged() {
        guard !busy else { return }
        activeRun?.cancel()
        activeRun = nil
        activeID = nil
        removePreparation(preparedDirectory)
        preparedDirectory = nil
        inspection = nil
        result = nil
        failure = nil
        isInspecting = false
        guard !inputPaths.isEmpty else {
            stage = "等待导入"
            stageDetail = "选择源文件，检查通过后即可转换。"
            return
        }
        start(action: "inspect")
    }

    func convert() {
        guard canConvert else { return }
        result = nil
        failure = nil
        start(action: "convert")
    }

    private func makeRequest(action: String, id: String) -> EngineRequest {
        var request = EngineRequest(task_id: id, action: action, input_kind: inputKind.rawValue,
                      input_paths: inputPaths, include_paths: includePaths,
                      output_parent: outputParent, exclude_opt_0081: excludeOPT0081, target_runtime: targetRuntime.rawValue, output_language: outputLanguage.rawValue)
        if action == "inspect" { request.preparation_output = preparationByRun[id]?.path }
        if action == "convert" { request.preparation_path = inspection?.preparation_path }
        return request
    }

    private func start(action: String) {
        let id = UUID().uuidString
        if action == "inspect" {
            do { preparationByRun[id] = try preparationOutput() }
            catch {
                failure = EngineFailure(title: "无法创建预检临时目录", suggestion: "请检查磁盘空间和临时目录权限后重试。", details: error.localizedDescription)
                stage = "预检失败"
                stageDetail = failure!.suggestion
                return
            }
        }
        activeID = id
        let run = makeRun()
        activeRun = run
        liveRuns[id] = run
        isInspecting = action == "inspect"
        isConverting = action == "convert"
        isCancelling = false
        stage = action == "inspect" ? "正在检查输入" : "正在转换"
        stageDetail = "正在启动内置转换引擎…"
        run.start(makeRequest(action: action, id: id), processExit: { [weak self] in
            Task { @MainActor in self?.processExited(id: id) }
        }, event: { [weak self] message in
            Task { @MainActor in
                guard let self, self.activeID == id, !self.isCancelling else { return }
                let names = ["reading": "读取文件", "parsing": "解析协议", "validating": "校验 Proto", "generating": "生成 \(self.outputLanguage.label)", "exporting": "导出结果"]
                self.stage = names[message.stage ?? ""] ?? "正在处理"
                self.stageDetail = message.message_zh ?? ""
                if let processed = message.processed, let total = message.total, total > 0 {
                    self.stageDetail += "（\(processed) / \(total)）"
                }
            }
        }) { [weak self] response in
            Task { @MainActor in
                guard let self else { return }
                self.liveRuns.removeValue(forKey: id)
                let preparation = self.preparationByRun.removeValue(forKey: id)
                var retainPreparation = false
                defer {
                    if !retainPreparation, let preparation {
                        if run.didFinishProcess { self.removePreparation(preparation) }
                        else { self.awaitingProcessCleanup[id] = preparation }
                    }
                    self.finishShutdownIfNeeded()
                }
                guard self.activeID == id else { return }
                self.activeRun = nil
                self.activeID = nil
                self.isInspecting = false
                self.isConverting = false
                self.isCancelling = false
                switch response {
                case .success(let message):
                    if message.type == "error" {
                        self.failure = self.error(from: message)
                        self.stage = message.code == "CANCELLED" ? "转换已取消" : "检查或转换失败"
                        self.stageDetail = message.message_zh ?? "请查看下方错误详情。"
                    } else {
                        if let version = message.compiler_version { self.compilerVersion = version }
                        if action == "inspect" {
                            if let path = message.preparation_path {
                                guard let preparation, URL(fileURLWithPath: path).resolvingSymlinksInPath() == preparation.resolvingSymlinksInPath() else {
                                    self.failure = EngineFailure(title: "预检临时目录不一致", suggestion: "请重新导入输入文件后重试。", details: "引擎返回了非本次请求的预检路径。")
                                    self.stage = "预检失败"
                                    return
                                }
                                self.preparedDirectory = preparation
                                retainPreparation = true
                            }
                            self.inspection = message
                            self.stage = "输入检查通过"
                            self.stageDetail = self.outputParent == nil ? "请选择导出父目录。" : "准备就绪，可以开始转换。"
                        } else {
                            self.result = message
                            self.stage = "转换完成"
                            let language = OutputLanguage(rawValue: message.resolvedOutputLanguage) ?? self.outputLanguage
                            self.stageDetail = "已导出 \(message.proto_count ?? 0) 个 Proto、\(message.generatedFileCount) 个 \(language.label) 文件。"
                        }
                    }
                case .failure(let failure):
                    self.failure = failure
                    self.stage = failure.title
                    self.stageDetail = failure.suggestion
                }
            }
        }
    }

    private func error(from message: EngineMessage) -> EngineFailure {
        var context = "错误代码：\(message.code ?? "UNKNOWN")"
        if let file = message.file { context += "\n文件：\(file)" }
        if let line = message.line { context += "\n行号：\(line)" }
        if let details = message.details { context += "\n\(details)" }
        return EngineFailure(title: message.message_zh ?? "转换失败", suggestion: message.suggestion ?? "请检查输入文件后重试。", details: context)
    }

    func cancel() {
        guard isConverting, !isCancelling else { return }
        isCancelling = true
        stage = "正在取消"
        stageDetail = "正在等待转换引擎停止并清理临时文件…"
        activeRun?.cancel()
    }

    func shutdown(_ completion: @escaping () -> Void) {
        versionRun?.cancel()
        shutdownCompletion = completion
        if liveRuns.isEmpty, versionRun == nil { finishShutdownIfNeeded(); return }
        isCancelling = true
        stage = "正在停止转换引擎"
        stageDetail = "进程结束后将关闭应用。"
        for run in liveRuns.values { run.cancel() }
    }

    private func finishShutdownIfNeeded() {
        guard liveRuns.isEmpty, versionRun == nil, awaitingProcessCleanup.isEmpty, shutdownCompletion != nil else { return }
        removePreparation(sessionRoot)
        sessionRoot = nil
        preparedDirectory = nil
        let completion = shutdownCompletion
        shutdownCompletion = nil
        completion?()
    }

    func revealResult() {
        guard let path = result?.output_path else { return }
        NSWorkspace.shared.open(URL(fileURLWithPath: path, isDirectory: true))
    }

    func copyError() {
        guard let failure else { return }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString("\(failure.title)\n\(failure.suggestion)\n\(failure.details)", forType: .string)
    }

    func preview(_ state: String) {
        if let runtime = ProcessInfo.processInfo.environment["PROTO_COMPILE_PREVIEW_RUNTIME"],
           let selected = TargetRuntime(rawValue: runtime) { targetRuntime = selected }
        if let language = ProcessInfo.processInfo.environment["PROTO_COMPILE_PREVIEW_LANGUAGE"],
           let selected = OutputLanguage(rawValue: language) { outputLanguage = selected }
        compilerVersion = "预览模式"
        if state == "empty" { return }
        inputKind = .proto
        inputPaths = ["/Users/developer/Protocols/device.proto"]
        includePaths = ["/Users/developer/Protocols/common"]
        outputParent = "/Users/developer/Exports"
        inspection = EngineMessage(protocol_version: 1, task_id: "preview", type: "result", proto_count: 12, java_count: 0, output_language: outputLanguage.rawValue, objc_count: 0, generated_count: 0, runtime_version: targetRuntime.rawValue, warnings: [], excluded: ["OPT0081.proto"], source_files: inputPaths)
        stage = "输入检查通过"
        stageDetail = "准备就绪，可以开始转换。"
        if state == "success" {
            let generatedCount = outputLanguage == .java ? 36 : 24
            result = EngineMessage(protocol_version: 1, task_id: "preview", type: "result", output_path: "/Users/developer/Exports/ProtoCompile-20261026", proto_count: 12, java_count: outputLanguage == .java ? generatedCount : 0, output_language: outputLanguage.rawValue, objc_count: outputLanguage == .objc ? generatedCount : 0, generated_count: generatedCount, runtime_version: targetRuntime.rawValue, warnings: [], changes: (0..<max(1, min(2000, Int(ProcessInfo.processInfo.environment["PROTO_COMPILE_PREVIEW_RECORD_COUNT"] ?? "1") ?? 1))).map { "处理记录 \($0 + 1)：已生成 \(outputLanguage.label) 源文件" }, excluded: ["OPT0081.proto"], source_files: inputPaths)
            stage = "转换完成"
            stageDetail = "已导出 12 个 Proto、\(generatedCount) 个 \(outputLanguage.label) 文件。"
        }
        if state == "error" {
            inspection = nil
            failure = EngineFailure(title: "无法解析 Proto 依赖", suggestion: "请添加包含 common/types.proto 的依赖目录后重试。", details: "错误代码：IMPORT_NOT_FOUND\n文件：device.proto\n行号：18")
            stage = "检查失败"
            stageDetail = "请查看下方错误信息。"
        }
    }
}
