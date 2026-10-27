import AppKit
import Combine
import SwiftUI

@MainActor
final class WorkspaceState: ObservableObject {
    @Published var mode: Int
    @Published private(set) var switchingDisabled: Bool
    @Published private(set) var showAbout: Bool
    private let model: AppModel
    private var subscriptions = Set<AnyCancellable>()

    init(model: AppModel, bleModel: BleLogModel, initialMode: Int = 0) {
        self.model = model
        mode = initialMode
        switchingDisabled = model.isConverting || bleModel.busy
        showAbout = model.showAbout
        model.$isConverting.combineLatest(bleModel.busyPublisher)
            .map { $0 || $1 }.removeDuplicates()
            .sink { [weak self] in
                guard let self, self.switchingDisabled != $0 else { return }
                self.switchingDisabled = $0
            }.store(in: &subscriptions)
        model.$showAbout.removeDuplicates().sink { [weak self] in
            guard let self, self.showAbout != $0 else { return }
            self.showAbout = $0
        }.store(in: &subscriptions)
    }

    func setShowAbout(_ value: Bool) { model.showAbout = value }
}

struct WorkspaceView: View {
    let model: AppModel
    let bleModel: BleLogModel
    @StateObject private var state: WorkspaceState

    init(model: AppModel, bleModel: BleLogModel, initialMode: Int = 0) {
        self.model = model
        self.bleModel = bleModel
        _state = StateObject(wrappedValue: WorkspaceState(model: model, bleModel: bleModel, initialMode: initialMode))
    }

    init(model: AppModel, bleModel: BleLogModel, state: WorkspaceState) {
        self.model = model
        self.bleModel = bleModel
        _state = StateObject(wrappedValue: state)
    }

    var body: some View {
        VStack(spacing: 0) {
            Picker("工作模式", selection: $state.mode) {
                Text("协议转换").tag(0)
                Text("蓝牙日志解析").tag(1)
            }
            .pickerStyle(.segmented).labelsHidden().accessibilityLabel("工作模式")
            .padding(.horizontal, 24).padding(.top, 16).padding(.bottom, 8)
            .disabled(state.switchingDisabled)
            WorkspacePages(mode: state.mode, model: model, bleModel: bleModel)
                .frame(minWidth: 720, minHeight: 700)
        }
        .background(Color(nsColor: .windowBackgroundColor))
        .sheet(isPresented: Binding(get: { state.showAbout }, set: state.setShowAbout)) {
            AboutView(model: model)
        }
    }
}

private struct WorkspacePages: NSViewRepresentable {
    let mode: Int
    let model: AppModel
    let bleModel: BleLogModel

    func makeNSView(context: Context) -> RetainedPageContainer {
        let container = RetainedPageContainer { mode in
            if mode == 0 { return AnyView(ContentView(model: model)) }
            return AnyView(BleLogView(model: bleModel, version: model.version, build: model.build))
        }
        container.select(mode)
        return container
    }

    func updateNSView(_ view: RetainedPageContainer, context: Context) { view.select(mode) }
}

/// 保留宿主与 SwiftUI 状态；离开页面时移出原生视图树，隔离快捷键、焦点及辅助功能。
@MainActor
final class RetainedPageContainer: NSView {
    private let makePage: (Int) -> AnyView
    private(set) var pages: [Int: WorkspacePageHost] = [:]
    private(set) var selectedMode: Int?

    init(makePage: @escaping (Int) -> AnyView) {
        self.makePage = makePage
        super.init(frame: .zero)
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    func select(_ mode: Int) {
        guard selectedMode != mode else { return }
        if let selectedMode, let previous = pages[selectedMode] {
            // 文本编辑器也可能是窗口的 field editor，先结束编辑再移除宿主。
            if let responder = window?.firstResponder as? NSView,
               responder.isDescendant(of: previous) || (responder as? NSTextView)?.isFieldEditor == true {
                window?.makeFirstResponder(nil)
            }
            previous.setAccessibilityHidden(true)
            previous.isHidden = true
            previous.removeFromSuperview()
        }
        let page: WorkspacePageHost
        if let existing = pages[mode] { page = existing }
        else {
            page = WorkspacePageHost(rootView: makePage(mode))
            page.autoresizingMask = [.width, .height]
            pages[mode] = page
        }
        selectedMode = mode
        page.frame = bounds
        addSubview(page)
        page.isHidden = false
        page.setAccessibilityHidden(false)
    }
}

@MainActor
final class WorkspacePageHost: NSHostingView<AnyView> {
    override func performKeyEquivalent(with event: NSEvent) -> Bool {
        guard window != nil, !isHiddenOrHasHiddenAncestor else { return false }
        return super.performKeyEquivalent(with: event)
    }
}
