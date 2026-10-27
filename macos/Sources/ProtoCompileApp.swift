import AppKit
import SwiftUI

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate {
    let model = AppModel()
    let bleModel = BleLogModel()
    private var window: NSWindow?
    private var quitting = false
    private var performanceDriver: PerformanceDriver?

    func applicationDidFinishLaunching(_ notification: Notification) {
        installMenu()
        let args = CommandLine.arguments
        if args.contains("--smoke-test") {
            smokeTest()
            return
        }
        if let previewIndex = args.firstIndex(of: "--render-preview"), args.count > previewIndex + 1 {
            renderPreview(to: args[previewIndex + 1])
            return
        }
        if args.contains("--performance-test") {
            do { performanceDriver = try PerformanceDriver(model: model, arguments: args) }
            catch { fputs("Performance setup failed: \(error)\n", stderr); exit(2) }
        }
        let window = makeWindow()
        self.window = window
        if performanceDriver != nil { window.setContentSize(NSSize(width: 820, height: 800)); window.center() }
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        model.loadVersion()
        performanceDriver?.start()
    }

    private func makeWindow() -> NSWindow {
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 820, height: 800), styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "Proto Compile"
        window.minSize = NSSize(width: 760, height: 740)
        let previewBLE = CommandLine.arguments.contains("--render-preview") && ProcessInfo.processInfo.environment["PROTO_COMPILE_PREVIEW_WORKFLOW"] == "ble"
        window.contentView = NSHostingView(rootView: WorkspaceView(model: model, bleModel: bleModel, initialMode: previewBLE ? 1 : 0))
        window.center()
        window.delegate = self
        window.isReleasedWhenClosed = false
        window.setFrameAutosaveName("ProtoCompileMainWindow")
        return window
    }

    private func installMenu() {
        let menu = NSMenu()
        let item = NSMenuItem()
        menu.addItem(item)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "关于 Proto Compile", action: #selector(showAbout), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "隐藏 Proto Compile", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        let hideOthers = appMenu.addItem(withTitle: "隐藏其他", action: #selector(NSApplication.hideOtherApplications(_:)), keyEquivalent: "h")
        hideOthers.keyEquivalentModifierMask = [.command, .option]
        appMenu.addItem(withTitle: "全部显示", action: #selector(NSApplication.unhideAllApplications(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "退出 Proto Compile", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        item.submenu = appMenu
        let edit = NSMenuItem()
        menu.addItem(edit)
        edit.submenu = NSMenu(title: "编辑")
        edit.submenu?.addItem(withTitle: "拷贝", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.submenu?.addItem(withTitle: "全选", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        NSApp.mainMenu = menu
    }

    @objc private func showAbout() { model.showAbout = true }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        if model.isConverting || model.isInspecting || bleModel.busy {
            NSApp.terminate(nil)
            return false
        }
        return true
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard !quitting else { return .terminateLater }
        quitting = true
        var remaining = 2
        let finished = {
            remaining -= 1
            if remaining == 0 { DispatchQueue.main.async { NSApp.reply(toApplicationShouldTerminate: true) } }
        }
        model.shutdown(finished)
        bleModel.shutdown(finished)
        return .terminateLater
    }

    func smokeTest() {
        let request = EngineRequest(task_id: "smoke", action: "version", input_kind: "html", input_paths: [], include_paths: [], output_parent: nil, exclude_opt_0081: true, target_runtime: model.targetRuntime.rawValue)
        do {
            let data = try JSONEncoder().encode(request)
            let dictionary = try JSONSerialization.jsonObject(with: data) as? [String: Any]
            guard dictionary?["protocol_version"] as? Int == 1, dictionary?["target_runtime"] as? String == "3.25.9", dictionary?["output_language"] as? String == "java", model.outputLanguage == .java, model.targetRuntime == .current, model.excludeOPT0081, !model.canConvert else {
                throw EngineFailure(title: "模型初始状态不正确", suggestion: "", details: "")
            }
            let response = Data("{\"protocol_version\":1,\"task_id\":\"smoke\",\"type\":\"result\",\"proto_count\":0,\"java_count\":0,\"compiler_version\":\"test\",\"warnings\":[],\"changes\":[],\"excluded\":[],\"source_files\":[]}".utf8)
            let decoded = try JSONDecoder().decode(EngineMessage.self, from: response)
            guard decoded.compiler_version == "test", decoded.resolvedOutputLanguage == "java", decoded.generatedFileCount == 0 else { throw EngineFailure(title: "协议解析失败", suggestion: "", details: "") }
            print("SwiftUI model and protocol smoke test passed")
            exit(0)
        } catch { fputs("Smoke test failed: \(error)\n", stderr); exit(1) }
    }

    /// 无面板端到端入口沿用真实模型与 bundle 内引擎，不注入预览结果。
    func integrationTest(html: String, outputParent: String, runtime: String, language: String = "java") {
        guard let selected = AppModel.TargetRuntime(rawValue: runtime) else {
            fputs("Unsupported target runtime: \(runtime). Use 3.25.9 or 3.6.1.\n", stderr)
            exit(2)
        }
        guard let selectedLanguage = AppModel.OutputLanguage(rawValue: language) else {
            fputs("Unsupported output language: \(language). Use java or objc.\n", stderr)
            exit(2)
        }
        model.targetRuntime = selected
        model.setOutputLanguage(selectedLanguage)
        let started = Date()
        var converting = false
        var finishing = false
        model.inputKind = .html
        model.inputPaths = [URL(fileURLWithPath: html).standardizedFileURL.path]
        model.outputParent = URL(fileURLWithPath: outputParent).standardizedFileURL.path
        model.parametersChanged()
        model.loadVersion()
        Timer.scheduledTimer(withTimeInterval: 0.05, repeats: true) { [self] timer in
            Task { @MainActor in
                guard !finishing else { return }
                @MainActor func fail(_ message: String) {
                    finishing = true
                    timer.invalidate()
                    fputs("Integration test failed: \(message)\n", stderr)
                    model.shutdown { exit(1) }
                }
                if Date().timeIntervalSince(started) >= 30 {
                    fail("30 秒内未完成预检和转换；正在终止引擎。")
                    return
                }
                if let failure = model.failure {
                    fail("\(failure.title)\n\(failure.suggestion)\n\(failure.details)")
                    return
                }
                if !converting, !model.isInspecting {
                    guard model.inspection?.output_language == selectedLanguage.rawValue, model.canConvert else {
                        fail("inspect 未产生有效结果，或 canConvert 未变为 true。")
                        return
                    }
                    converting = true
                    model.convert()
                    guard model.isConverting, !model.canConvert else {
                        fail("convert 未启动，或转换期间仍允许重入。")
                        return
                    }
                }
                if converting, !model.isConverting, let result = model.result {
                    guard let protoCount = result.proto_count, let javaCount = result.java_count,
                          let objcCount = result.objc_count, let generatedCount = result.generated_count,
                          protoCount > 0, generatedCount > 0,
                          result.output_language == selectedLanguage.rawValue,
                          selectedLanguage == .java ? (javaCount == generatedCount && objcCount == 0) : (objcCount == generatedCount && javaCount == 0),
                          let output = result.output_path,
                          FileManager.default.fileExists(atPath: output),
                          let compilerVersion = result.compiler_version, !compilerVersion.isEmpty,
                          result.runtime_version == selected.rawValue else {
                        fail("转换结果缺少有效计数、输出目录、编译器版本，或运行库目标不符。")
                        return
                    }
                    let generatedRoot = URL(fileURLWithPath: output).appendingPathComponent(selectedLanguage.rawValue)
                    let files = (FileManager.default.enumerator(at: generatedRoot, includingPropertiesForKeys: nil)?.allObjects as? [URL] ?? [])
                    let extensions = selectedLanguage == .java ? ["java"] : ["h", "m"]
                    guard files.filter({ extensions.contains($0.pathExtension) }).count == generatedCount,
                          selectedLanguage != .objc || files.filter({ $0.pathExtension == "h" }).count == files.filter({ $0.pathExtension == "m" }).count else {
                        fail("产物文件数量或 Objective-C 头文件 / 实现文件配对数量不符。")
                        return
                    }
                    let summary: [String: Any] = [
                        "test": "AppModel inspect -> canConvert -> convert",
                        "status": "passed",
                        "inspect_proto_count": model.inspection?.proto_count ?? 0,
                        "proto_count": protoCount,
                        "java_count": javaCount,
                        "objc_count": objcCount,
                        "generated_count": generatedCount,
                        "output_language": result.output_language ?? "",
                        "compiler_version": compilerVersion,
                        "runtime_version": result.runtime_version ?? "",
                        "application_version": model.version,
                        "application_build": model.build,
                        "output_path": output
                    ]
                    do {
                        let json = try JSONSerialization.data(withJSONObject: summary, options: [.sortedKeys])
                        print(String(decoding: json, as: UTF8.self))
                        finishing = true
                        timer.invalidate()
                        model.shutdown { exit(0) }
                    } catch { fail(error.localizedDescription) }
                }
            }
        }
    }

    func bleIntegrationTest(html: String, log: String, format: String, outputParent: String) {
        guard let selectedFormat = BleLogModel.LogFormat(rawValue: format) else {
            fputs("日志格式无效，请使用 frames 或 json。\n", stderr)
            exit(2)
        }
        bleModel.setFormat(selectedFormat)
        bleModel.setLogPath(log)
        bleModel.setProtocolPath(html)
        bleModel.setOutputParent(outputParent)
        bleModel.parse()
        let started = Date()
        var finishing = false
        Timer.scheduledTimer(withTimeInterval: 0.05, repeats: true) { [self] timer in
            Task { @MainActor in
                guard !finishing else { return }
                @MainActor func stop(_ code: Int32, _ message: String) {
                    finishing = true
                    timer.invalidate()
                    if code == 0 { print(message) }
                    else { fputs("蓝牙日志集成测试失败：\(message)\n", stderr) }
                    bleModel.shutdown { exit(code) }
                }
                if Date().timeIntervalSince(started) > 120 {
                    stop(1, "120 秒内未完成解析，正在停止引擎。")
                } else if let failure = bleModel.failure {
                    stop(1, "\(failure.title)\n\(failure.suggestion)\n\(failure.details)")
                } else if !bleModel.isParsing, let result = bleModel.result {
                    guard let path = result.report_path, let output = result.output_path,
                          let count = result.record_count, let decoded = result.decoded_count,
                          let warnings = result.warning_count, let errors = result.error_count,
                          count == decoded + warnings + errors,
                          FileManager.default.fileExists(atPath: path) else {
                        stop(1, "结果文件缺失或记录计数不一致。")
                        return
                    }
                    do {
                        let summary: [String: Any] = ["output_path": output, "report_path": path, "record_count": count,
                                                      "decoded_count": decoded, "warning_count": warnings, "error_count": errors]
                        let json = try JSONSerialization.data(withJSONObject: summary, options: [.sortedKeys])
                        stop(0, String(decoding: json, as: UTF8.self))
                    } catch { stop(1, "无法生成结果摘要：\(error.localizedDescription)") }
                }
            }
        }
    }

    private func renderPreview(to path: String) {
        let environment = ProcessInfo.processInfo.environment
        model.preview(environment["PROTO_COMPILE_PREVIEW_STATE"] ?? "empty")
        if environment["PROTO_COMPILE_PREVIEW_WORKFLOW"] == "ble" {
            bleModel.preview(environment["PROTO_COMPILE_PREVIEW_STATE"] ?? "idle")
        }
        let appearance = environment["PROTO_COMPILE_PREVIEW_THEME"] == "dark" ? NSAppearance.Name.darkAqua : .aqua
        NSApp.appearance = NSAppearance(named: appearance)
        let window = makeWindow()
        self.window = window
        window.setContentSize(NSSize(width: 820, height: 860))
        window.orderFront(nil)
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.8) {
            guard let view = window.contentView,
                  let bitmap = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { exit(1) }
            view.cacheDisplay(in: view.bounds, to: bitmap)
            guard let data = bitmap.representation(using: .png, properties: [:]) else { exit(1) }
            do { try data.write(to: URL(fileURLWithPath: path)); print("Rendered \(path)"); exit(0) }
            catch { fputs("Preview failed: \(error)\n", stderr); exit(1) }
        }
    }
}

@main
struct ProtoCompileApplication {
    @MainActor static func main() {
        PerformanceDriver.markLaunchIfRequested()
        let delegate = AppDelegate()
        if CommandLine.arguments.contains("--smoke-test") { delegate.smokeTest(); return }
        if let index = CommandLine.arguments.firstIndex(of: "--ble-integration-test") {
            guard CommandLine.arguments.count == index + 5 else {
                fputs("用法：--ble-integration-test <协议 HTML> <日志文件> <frames|json> <导出父目录>\n", stderr)
                exit(2)
            }
            delegate.bleIntegrationTest(html: CommandLine.arguments[index + 1], log: CommandLine.arguments[index + 2], format: CommandLine.arguments[index + 3], outputParent: CommandLine.arguments[index + 4])
            withExtendedLifetime(delegate) { RunLoop.main.run() }
            return
        }
        if let index = CommandLine.arguments.firstIndex(of: "--integration-test") {
            guard (index + 3...index + 5).contains(CommandLine.arguments.count) else {
                fputs("Usage: --integration-test <html> <outputParent> [3.25.9|3.6.1] [java|objc]\n", stderr)
                exit(2)
            }
            let options = Array(CommandLine.arguments.dropFirst(index + 3))
            let languageOnly = options.count == 1 && AppModel.OutputLanguage(rawValue: options[0]) != nil
            let runtime = !options.isEmpty && !languageOnly ? options[0] : AppModel.TargetRuntime.current.rawValue
            let language = languageOnly ? options[0] : (options.count == 2 ? options[1] : "java")
            delegate.integrationTest(html: CommandLine.arguments[index + 1], outputParent: CommandLine.arguments[index + 2], runtime: runtime, language: language)
            withExtendedLifetime(delegate) { RunLoop.main.run() }
            return
        }
        let app = NSApplication.shared
        app.setActivationPolicy(.regular)
        app.delegate = delegate
        withExtendedLifetime(delegate) { app.run() }
    }
}
