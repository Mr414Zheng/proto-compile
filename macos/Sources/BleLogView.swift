import SwiftUI

@MainActor
final class BleViewOptions: ObservableObject {
    @Published var errorDetails = false
    @Published var warningsExpanded = false
}

struct BleLogView: View {
    @ObservedObject var model: BleLogModel
    let version: String
    let build: String
    @StateObject private var options = BleViewOptions()

    var body: some View {
        VStack(spacing: 0) {
            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    header
                    inputSection
                    selectionSection(title: "2. 选择设备协议", icon: "doc.text", path: model.protocolPath,
                                     empty: "尚未选择设备协议 HTML 文档", hint: "使用对应设备版本的 .html 或 .htm 协议文档。", action: model.chooseProtocol)
                    selectionSection(title: "3. 选择导出父目录", icon: "folder", path: model.outputParent,
                                     empty: "尚未选择导出位置", hint: "在此目录下创建独立结果文件夹，保存 UTF-8 编码的 .txt 文件。", action: model.chooseOutput)
                    Divider()
                    BleStatusView(model: model, progress: model.progress)
                    if let failure = model.failure { errorSection(failure) }
                    if let result = model.result { resultSection(result) }
                }.padding(24)
            }
            Divider()
            HStack {
                Text("版本 \(version) (\(build))")
                Spacer()
                Text("所有解析均在本机完成")
            }.font(.caption).foregroundColor(.secondary).padding(.horizontal, 24).padding(.vertical, 12)
        }
        .frame(minWidth: 720, minHeight: 700)
        .disclosureGroupStyle(ClickableDisclosureStyle())
    }

    private var header: some View {
        HStack(spacing: 14) {
            Image(systemName: "wave.3.right.circle.fill")
                .font(.system(size: 36)).foregroundColor(.accentColor).accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 4) {
                Text("蓝牙日志解析").font(.title2.weight(.semibold))
                Text("按设备协议解码 BLE 帧或 JSON payload 日志")
                    .foregroundColor(.secondary)
            }
            Spacer()
            Text("本地解析").font(.caption.weight(.medium))
                .padding(.horizontal, 10).padding(.vertical, 5)
                .background(Color(nsColor: .controlBackgroundColor), in: Capsule())
        }
    }

    private var inputSection: some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 14) {
                Picker("日志格式", selection: Binding(get: { model.logFormat }, set: { model.setFormat($0) })) {
                    ForEach(BleLogModel.LogFormat.allCases, id: \.self) { format in Text(format.label).tag(format) }
                }.pickerStyle(.segmented).labelsHidden().accessibilityLabel("日志格式")
                HStack(alignment: .top, spacing: 12) {
                    Image(systemName: "doc.plaintext").font(.title2).foregroundColor(.secondary).accessibilityHidden(true)
                    VStack(alignment: .leading, spacing: 5) {
                        if let path = model.logPath { pathText(path) }
                        else { Text("选择\(model.logFormat.label)文件").font(.headline) }
                        Text(model.logFormat.hint).font(.callout).foregroundColor(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }.frame(maxWidth: .infinity, alignment: .leading)
                    Button(model.logPath == nil ? "选择…" : "更改…", action: model.chooseLog)
                        .keyboardShortcut("o", modifiers: .command)
                }
            }.padding(10)
        } label: { Label("1. 导入蓝牙日志", systemImage: "square.and.arrow.down").font(.headline) }
        .disabled(model.busy)
    }

    private func selectionSection(title: String, icon: String, path: String?, empty: String, hint: String, action: @escaping () -> Void) -> some View {
        GroupBox {
            HStack(alignment: .center, spacing: 12) {
                Image(systemName: icon).font(.title2).foregroundColor(.secondary).accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 5) {
                    if let path { pathText(path) }
                    else { Text(empty).foregroundColor(.secondary) }
                    Text(hint).font(.callout).foregroundColor(.secondary).fixedSize(horizontal: false, vertical: true)
                }.frame(maxWidth: .infinity, alignment: .leading)
                Button(path == nil ? "选择…" : "更改…", action: action)
            }.padding(10)
        } label: { Label(title, systemImage: icon).font(.headline) }
        .disabled(model.busy)
    }

    private func errorSection(_ failure: EngineFailure) -> some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 10) {
                Label(failure.title, systemImage: "exclamationmark.triangle.fill").font(.headline)
                Text(failure.suggestion).foregroundColor(.secondary).fixedSize(horizontal: false, vertical: true)
                HStack {
                    DisclosureGroup("错误详情", isExpanded: $options.errorDetails) {
                        if options.errorDetails {
                            Text(failure.details).font(.system(.caption, design: .monospaced)).textSelection(.enabled)
                                .frame(maxWidth: .infinity, alignment: .leading).padding(.top, 8)
                        }
                    }
                    Button("复制详情", action: model.copyError)
                }
            }.padding(10).frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private func resultSection(_ result: EngineMessage) -> some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 10) {
                HStack {
                    Label("解析文本已导出", systemImage: "checkmark.circle.fill").font(.headline)
                    Spacer()
                    Button("打开文本", action: model.openReport).disabled(result.report_path == nil)
                    Button("在 Finder 中打开", action: model.revealResult)
                }
                if let path = result.report_path ?? result.output_path { pathText(path) }
                Text("共 \(result.record_count ?? 0) 条  ·  成功 \(result.decoded_count ?? 0) 条  ·  含警告 \(result.warning_count ?? 0) 条  ·  错误 \(result.error_count ?? 0) 条")
                    .font(.callout).foregroundColor(.secondary).fixedSize(horizontal: false, vertical: true)
                if (result.warning_count ?? 0) > 0 || (result.error_count ?? 0) > 0 {
                    Label("含警告和错误的记录已保留，请在文本中核对逐条诊断。", systemImage: "exclamationmark.triangle")
                        .font(.callout).fixedSize(horizontal: false, vertical: true)
                }
                if let warnings = result.warnings, !warnings.isEmpty {
                    DisclosureGroup("查看解析提示（\(warnings.count) 条）", isExpanded: $options.warningsExpanded) {
                        if options.warningsExpanded {
                            Text(warnings.joined(separator: "\n\n")).font(.callout).foregroundColor(.secondary)
                                .textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading).padding(.top, 8)
                        }
                    }
                }
            }.padding(10).frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private func pathText(_ path: String) -> some View {
        Text(path).font(.system(.callout, design: .monospaced)).foregroundColor(.secondary)
            .textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
            .frame(maxWidth: .infinity, alignment: .leading)
    }
}

private struct BleStatusView: View {
    @ObservedObject var model: BleLogModel
    @ObservedObject var progress: BleProgressModel

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 12) {
                if model.busy { ProgressView().controlSize(.small).frame(width: 22) }
                else {
                    Image(systemName: model.failure != nil ? "exclamationmark.triangle" : (model.result != nil ? "checkmark.circle" : "info.circle"))
                        .font(.title3).foregroundColor(model.failure != nil ? .orange : .secondary).accessibilityHidden(true)
                }
                VStack(alignment: .leading, spacing: 4) {
                    Text(progress.state.stage).font(.headline)
                    Text(progress.state.stageDetail).font(.callout).foregroundColor(.secondary).fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 16)
                if model.isParsing {
                    Button(model.isCancelling ? "正在取消…" : "取消", action: model.cancel)
                        .disabled(model.isCancelling).keyboardShortcut(.cancelAction)
                } else {
                    Button("开始解析", action: model.parse)
                        .buttonStyle(.borderedProminent).controlSize(.large)
                        .disabled(!model.canParse).keyboardShortcut(.defaultAction)
                }
            }
            if model.isParsing, let processed = progress.state.processed {
                if let total = progress.state.total, total > 0 {
                    ProgressView(value: Double(min(processed, total)), total: Double(total))
                    Text("已处理 \(processed) / \(total) 条记录").font(.callout).foregroundColor(.secondary)
                } else { Text("已处理 \(processed) 条记录").font(.callout).foregroundColor(.secondary) }
            }
        }
    }

}
