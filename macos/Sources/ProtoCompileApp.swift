import AppKit
import SwiftUI

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate {
    let model = AppModel()
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
        window.contentView = NSHostingView(rootView: ContentView(model: model))
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
        if model.isConverting || model.isInspecting {
            NSApp.terminate(nil)
            return false
        }
        return true
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard !quitting else { return .terminateLater }
        quitting = true
        model.shutdown { DispatchQueue.main.async { NSApp.reply(toApplicationShouldTerminate: true) } }
        return .terminateLater
    }

    func smokeTest() {
        let request = EngineRequest(task_id: "smoke", action: "version", input_kind: "html", input_paths: [], include_paths: [], output_parent: nil, exclude_opt_0081: true, target_runtime: model.targetRuntime.rawValue)
        do {
            let data = try JSONEncoder().encode(request)
            let dictionary = try JSONSerialization.jsonObject(with: data) as? [String: Any]
            guard dictionary?["protocol_version"] as? Int == 1, dictionary?["target_runtime"] as? String == "3.25.9", model.targetRuntime == .current, model.excludeOPT0081, !model.canConvert else {
                throw EngineFailure(title: "模型初始状态不正确", suggestion: "", details: "")
            }
            let response = Data("{\"protocol_version\":1,\"task_id\":\"smoke\",\"type\":\"result\",\"proto_count\":0,\"java_count\":0,\"compiler_version\":\"test\",\"warnings\":[],\"changes\":[],\"excluded\":[],\"source_files\":[]}".utf8)
            let decoded = try JSONDecoder().decode(EngineMessage.self, from: response)
            guard decoded.compiler_version == "test" else { throw EngineFailure(title: "协议解析失败", suggestion: "", details: "") }
            print("SwiftUI model and protocol smoke test passed")
            exit(0)
        } catch { fputs("Smoke test failed: \(error)\n", stderr); exit(1) }
    }

    /// 无面板端到端入口沿用真实模型与 bundle 内引擎，不注入预览结果。
    func integrationTest(html: String, outputParent: String, runtime: String) {
        guard let selected = AppModel.TargetRuntime(rawValue: runtime) else {
            fputs("Unsupported target runtime: \(runtime). Use 3.25.9 or 3.6.1.\n", stderr)
            exit(2)
        }
        model.targetRuntime = selected
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
                    guard model.inspection != nil, model.canConvert else {
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
                          protoCount > 0, javaCount > 0,
                          let output = result.output_path,
                          FileManager.default.fileExists(atPath: output),
                          let compilerVersion = result.compiler_version, !compilerVersion.isEmpty,
                          result.runtime_version == selected.rawValue else {
                        fail("转换结果缺少有效计数、输出目录、编译器版本，或运行库目标不符。")
                        return
                    }
                    let summary: [String: Any] = [
                        "test": "AppModel inspect -> canConvert -> convert",
                        "status": "passed",
                        "inspect_proto_count": model.inspection?.proto_count ?? 0,
                        "proto_count": protoCount,
                        "java_count": javaCount,
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

    private func renderPreview(to path: String) {
        let environment = ProcessInfo.processInfo.environment
        model.preview(environment["PROTO_COMPILE_PREVIEW_STATE"] ?? "empty")
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
        if let index = CommandLine.arguments.firstIndex(of: "--integration-test") {
            guard CommandLine.arguments.count == index + 3 || CommandLine.arguments.count == index + 4 else {
                fputs("Usage: --integration-test <html> <outputParent> [3.25.9|3.6.1]\n", stderr)
                exit(2)
            }
            delegate.integrationTest(html: CommandLine.arguments[index + 1], outputParent: CommandLine.arguments[index + 2], runtime: CommandLine.arguments.count == index + 4 ? CommandLine.arguments[index + 3] : AppModel.TargetRuntime.current.rawValue)
            withExtendedLifetime(delegate) { RunLoop.main.run() }
            return
        }
        let app = NSApplication.shared
        app.setActivationPolicy(.regular)
        app.delegate = delegate
        withExtendedLifetime(delegate) { app.run() }
    }
}
