import AppKit
import Combine
import SwiftUI

@MainActor private final class PageProbe: ObservableObject {
    @Published var expanded = false
    var shortcuts = [0, 0, 0]
}

private struct ProbePage: View {
    @StateObject private var state = PageProbe()
    let capture: (PageProbe) -> Void

    var body: some View {
        ScrollView {
            VStack {
                TextField("焦点测试", text: .constant("可编辑"))
                DisclosureGroup("展开测试", isExpanded: $state.expanded) { Text("展开内容") }
                Button("打开") { state.shortcuts[0] += 1 }.keyboardShortcut("o", modifiers: .command)
                Button("执行") { state.shortcuts[1] += 1 }.keyboardShortcut(.defaultAction)
                Button("取消") { state.shortcuts[2] += 1 }.keyboardShortcut(.cancelAction)
                ForEach(0..<100) { Text("保留滚动位置 \($0)").frame(height: 24) }
            }
        }.onAppear { capture(state) }
    }
}

@main
struct WorkspaceViewTests {
    @MainActor static func main() {
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)
        let model = AppModel()
        let ble = BleLogModel()
        let state = WorkspaceState(model: model, bleModel: ble)
        var workspaceChanges = 0
        var modelChanges = 0
        var progressStates: [BleProgressState] = []
        let subscriptions: [AnyCancellable] = [
            state.objectWillChange.sink { workspaceChanges += 1 },
            ble.objectWillChange.sink { modelChanges += 1 },
            ble.progress.$state.dropFirst().sink { progressStates.append($0) },
        ]
        let progress = BleProgressState(stage: "解码蓝牙记录", stageDetail: "正在处理", processed: 2, total: 10)
        ble.progress.update(progress)
        ble.progress.update(progress)
        precondition(progressStates == [progress], "one atomic publication per distinct progress event")
        precondition(modelChanges == 0 && workspaceChanges == 0, "progress cannot invalidate inputs/results/workspace")
        model.stage = "检查中"
        model.isInspecting = true
        precondition(workspaceChanges == 0 && !state.switchingDisabled, "inspection progress cannot invalidate or lock workspace")
        model.isConverting = true
        precondition(state.switchingDisabled)
        model.isConverting = false
        precondition(!state.switchingDisabled)
        model.showAbout = true
        precondition(state.showAbout)
        state.setShowAbout(false)
        precondition(!state.showAbout && !model.showAbout)

        var creations = [0, 0]
        var probes: [Int: PageProbe] = [:]
        let container = RetainedPageContainer { mode in
            creations[mode] += 1
            return AnyView(ProbePage { probes[mode] = $0 })
        }
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 820, height: 800),
                              styleMask: [.titled, .closable, .resizable], backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.contentView = container
        window.makeKeyAndOrderFront(nil)
        app.activate(ignoringOtherApps: true)
        func settle() {
            RunLoop.main.run(until: Date().addingTimeInterval(0.12))
            window.contentView?.layoutSubtreeIfNeeded()
        }
        func descendants(_ view: NSView) -> [NSView] { [view] + view.subviews.flatMap(descendants) }
        func key(_ character: String, modifiers: NSEvent.ModifierFlags = [], code: UInt16) -> NSEvent {
            NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: modifiers, timestamp: 0,
                            windowNumber: window.windowNumber, context: nil, characters: character,
                            charactersIgnoringModifiers: character, isARepeat: false, keyCode: code)!
        }
        container.select(0)
        settle()
        precondition(creations == [1, 0], "second page must remain lazy")
        let firstHost = container.pages[0]!
        let firstProbe = probes[0]!
        firstProbe.expanded = true
        settle()
        let scroll = descendants(firstHost).compactMap { $0 as? NSScrollView }.first!
        scroll.contentView.scroll(to: NSPoint(x: 0, y: 400))
        scroll.reflectScrolledClipView(scroll.contentView)
        let offset = scroll.contentView.bounds.origin
        let commands = [key("o", modifiers: .command, code: 31), key("\r", code: 36), key("\u{1b}", code: 53)]
        for event in commands { _ = window.performKeyEquivalent(with: event) }
        precondition(firstProbe.shortcuts == [1, 1, 1], "active page owns command-O, Return and Escape")
        let field = descendants(firstHost).first(where: { $0 is NSTextField })!
        precondition(window.makeFirstResponder(field), "test must establish focus before leaving page")
        container.select(1)
        settle()
        precondition(creations == [1, 1] && firstHost.window == nil && firstHost.isHidden)
        precondition(firstHost.isAccessibilityHidden(), "hidden page must leave accessibility tree")
        if let responder = window.firstResponder as? NSView {
            precondition(!responder.isDescendant(of: firstHost), "hidden page must relinquish focus")
        }
        for event in commands {
            precondition(!firstHost.performKeyEquivalent(with: event), "detached page cannot dispatch shortcuts")
            _ = window.performKeyEquivalent(with: event)
        }
        precondition(firstProbe.shortcuts == [1, 1, 1] && probes[1]!.shortcuts == [1, 1, 1])
        window.makeFirstResponder(nil)
        window.sendEvent(commands[1])
        window.sendEvent(commands[2])
        settle()
        precondition(firstProbe.shortcuts == [1, 1, 1] && probes[1]!.shortcuts == [1, 2, 2], "native Return/Escape dispatch cannot reach hidden default buttons")
        for _ in 0..<10 { container.select(0); settle(); container.select(1); settle() }
        container.select(0)
        settle()
        precondition(creations == [1, 1] && container.pages[0] === firstHost)
        precondition(probes[0] === firstProbe && firstProbe.expanded, "StateObject and disclosure survive switches")
        precondition(scroll.contentView.bounds.origin == offset, "scroll position survives switches")
        precondition(model.isInspecting && !ble.isParsing, "switches cannot cancel inspection or start parse")
        window.close()

        let workspaceWindow = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 820, height: 800),
                                       styleMask: [.titled, .closable, .resizable], backing: .buffered, defer: false)
        workspaceWindow.isReleasedWhenClosed = false
        workspaceWindow.contentView = NSHostingView(rootView: WorkspaceView(model: model, bleModel: ble, state: state))
        workspaceWindow.makeKeyAndOrderFront(nil)
        for mode in [0, 1, 0] {
            state.mode = mode
            RunLoop.main.run(until: Date().addingTimeInterval(0.2))
            state.setShowAbout(true)
            RunLoop.main.run(until: Date().addingTimeInterval(0.5))
            precondition(workspaceWindow.sheets.count == 1, "workspace owns exactly one About sheet in either mode")
            state.setShowAbout(false)
            RunLoop.main.run(until: Date().addingTimeInterval(0.5))
            precondition(workspaceWindow.sheets.isEmpty, "About dismisses normally")
        }
        workspaceWindow.close()
        if ProcessInfo.processInfo.environment["PROTO_COMPILE_UI_TEST_EXPANDED_FIXTURE"] == "1" { verifyActualPages() }
        withExtendedLifetime(subscriptions) {}
        print("WorkspaceView tests passed: lazy retained hosts, StateObject/disclosure/scroll, active-only command-O/Return/Escape, focus/AX isolation, deduplicated atomic progress, narrow workspace observation, About state")
    }

    @MainActor private static func verifyActualPages() {
        let priorCount = ProcessInfo.processInfo.environment["PROTO_COMPILE_PREVIEW_RECORD_COUNT"]
        setenv("PROTO_COMPILE_PREVIEW_RECORD_COUNT", "2000", 1)
        defer {
            if let priorCount { setenv("PROTO_COMPILE_PREVIEW_RECORD_COUNT", priorCount, 1) }
            else { unsetenv("PROTO_COMPILE_PREVIEW_RECORD_COUNT") }
        }
        let compile = AppModel()
        let ble = BleLogModel()
        compile.preview("success")
        ble.preview("success")
        precondition(compile.result?.changes?.count == 2000)
        let state = WorkspaceState(model: compile, bleModel: ble)
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 820, height: 800),
                              styleMask: [.titled, .closable, .resizable], backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        let root = NSHostingView(rootView: WorkspaceView(model: compile, bleModel: ble, state: state))
        window.contentView = root
        window.makeKeyAndOrderFront(nil)
        defer { window.close() }
        func settle() {
            RunLoop.main.run(until: Date().addingTimeInterval(0.25))
            root.layoutSubtreeIfNeeded()
        }
        func attribute(_ node: NSObject, _ name: String) -> Any? {
            let selector = NSSelectorFromString(name)
            guard node.responds(to: selector) else { return nil }
            return node.perform(selector)?.takeUnretainedValue()
        }
        // SwiftUI 按需创建辅助功能树；原生测试需先启用该模式。
        NSApp.accessibilitySetValue(true, forAttribute: NSAccessibility.Attribute(rawValue: "AXEnhancedUserInterface"))
        func elements() -> [NSObject] {
            var result: [NSObject] = []
            var visited = Set<ObjectIdentifier>()
            func visit(_ node: NSObject) {
                guard visited.insert(ObjectIdentifier(node)).inserted else { return }
                result.append(node)
                for child in attribute(node, "accessibilityChildren") as? [NSObject] ?? [] { visit(child) }
            }
            for view in descendants(root) { visit(view) }
            return result
        }
        func text(_ node: NSObject) -> String {
            ["accessibilityLabel", "accessibilityTitle", "accessibilityValue"]
                .compactMap { attribute(node, $0) as? String }.joined(separator: "\n")
        }
        func hasText(_ phrase: String) -> Bool { elements().contains { text($0).contains(phrase) } }
        func descendants(_ view: NSView) -> [NSView] { [view] + view.subviews.flatMap(descendants) }
        func activeScroll() -> NSScrollView {
            descendants(root).compactMap { $0 as? NSScrollView }.first!
        }
        func setOffset(_ value: CGFloat) -> NSPoint {
            let scroll = activeScroll()
            let maximum = max(0, (scroll.documentView?.bounds.height ?? 0) - scroll.contentView.bounds.height)
            scroll.contentView.scroll(to: NSPoint(x: 0, y: min(value, maximum)))
            scroll.reflectScrolledClipView(scroll.contentView)
            settle()
            return scroll.contentView.bounds.origin
        }
        func snapshot(_ name: String) {
            guard let directory = ProcessInfo.processInfo.environment["PROTO_COMPILE_UI_TEST_SCREENSHOTS"] else { return }
            let destination = URL(fileURLWithPath: directory)
            try! FileManager.default.createDirectory(at: destination, withIntermediateDirectories: true)
            guard let bitmap = root.bitmapImageRepForCachingDisplay(in: root.bounds) else { preconditionFailure("cannot capture UI") }
            root.cacheDisplay(in: root.bounds, to: bitmap)
            try! bitmap.representation(using: .png, properties: [:])!.write(to: destination.appendingPathComponent(name + ".png"))
        }
        settle()
        precondition(hasText("/Users/developer/Protocols/device.proto") && hasText("转换完成"), "compile input path and final status are visible to accessibility")
        precondition(hasText("复制处理记录") && hasText("处理记录 2000："), "actual result renders all 2000 records in the expanded test fixture")
        let compileOffset = setOffset(420)
        snapshot("compile-success-expanded")
        state.mode = 1
        settle()
        precondition(hasText("/Users/developer/Logs/设备蓝牙日志.txt") && hasText("解析完成"), "BLE input path and final status are visible to accessibility")
        precondition(hasText("第 83 条 payload 不完整"))
        let bleOffset = setOffset(180)
        snapshot("ble-success-expanded")
        state.mode = 0
        settle()
        precondition(hasText("复制处理记录") && hasText("处理记录 2000："), "actual compile disclosure survives returning")
        precondition(activeScroll().contentView.bounds.origin == compileOffset)
        state.mode = 1
        settle()
        precondition(hasText("第 83 条 payload 不完整"), "actual BLE disclosure survives returning")
        precondition(activeScroll().contentView.bounds.origin == bleOffset)
        // 截图入口仅填充状态，不启动任何引擎任务，也不触碰剪贴板。
        compile.result = nil
        compile.preview("error")
        ble.preview("error")
        state.mode = 0
        settle()
        precondition(hasText("IMPORT_NOT_FOUND"))
        _ = setOffset(400)
        snapshot("compile-error-expanded")
        state.mode = 1
        settle()
        precondition(hasText("FILE_NOT_FOUND"))
        _ = setOffset(350)
        snapshot("ble-error-expanded")
        state.mode = 0
        settle()
        precondition(hasText("IMPORT_NOT_FOUND"), "compile error details survive returning")
        state.mode = 1
        settle()
        precondition(hasText("FILE_NOT_FOUND"), "BLE error details survive returning")
        precondition(!compile.isInspecting && !compile.isConverting && !ble.isParsing)
        print("Actual page fixture tests passed (initial disclosure state injected into temporary test source; no AX press claim): 2000 compile records, BLE warnings, both input paths/statuses, both error details, disclosure and scroll retention; no clipboard writes or engine tasks")
    }

}
