import SwiftUI

/// 标题与箭头共用一个按钮，避免只有小箭头能够响应点击。
struct ClickableDisclosureStyle: DisclosureGroupStyle {
    func makeBody(configuration: Configuration) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            Button {
                configuration.isExpanded.toggle()
            } label: {
                HStack(spacing: 5) {
                    Image(systemName: configuration.isExpanded ? "chevron.down" : "chevron.right")
                        .font(.system(size: 10, weight: .semibold))
                        .foregroundColor(.secondary)
                        .frame(width: 12)
                        .accessibilityHidden(true)
                    configuration.label
                }
                .frame(maxWidth: .infinity, minHeight: 22, alignment: .leading)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityValue(configuration.isExpanded ? "已展开" : "已收起")
            if configuration.isExpanded {
                configuration.content.padding(.leading, 17)
            }
        }
    }
}

@MainActor
final class ViewOptions: ObservableObject {
    @Published var advanced = false
    @Published var details = false
    @Published var errorDetails = false
    @Published var changes = false
    @Published var warnings = false
}

struct ContentView: View {
    @ObservedObject var model: AppModel
    @ObservedObject private var options = ViewOptions()

    var body: some View {
        VStack(spacing: 0) {
            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    header
                    inputSection
                    targetSection
                    outputSection
                    DisclosureGroup("高级设置", isExpanded: $options.advanced) {
                        VStack(alignment: .leading, spacing: 8) {
                            Toggle("排除 OPT0081 协议", isOn: $model.excludeOPT0081)
                                .onChange(of: model.excludeOPT0081) { _ in model.parametersChanged() }
                            Text("默认排除 OPT0081；取消勾选后将尝试处理该协议。")
                                .font(.callout).foregroundColor(.secondary)
                        }.padding(.top, 8).frame(maxWidth: .infinity, alignment: .leading)
                    }.disabled(model.busy)
                    Divider()
                    statusSection
                    if let failure = model.failure { errorSection(failure) }
                    if let result = model.result { successSection(result) }
                }
                .padding(24)
            }
            Divider()
            HStack {
                Text("版本 \(model.version) (\(model.build))")
                Text("·")
                Text("protoc \(model.compilerVersion)")
                    .lineLimit(1).truncationMode(.middle)
                Spacer()
                Button("关于") { model.showAbout = true }.buttonStyle(.link)
            }
            .font(.caption).foregroundColor(.secondary).padding(.horizontal, 24).padding(.vertical, 12)
        }
        .frame(minWidth: 720, minHeight: 700)
        .disclosureGroupStyle(ClickableDisclosureStyle())
        .background(Color(nsColor: .windowBackgroundColor))
        .sheet(isPresented: $model.showAbout) { aboutView }
    }

    private var header: some View {
        HStack(alignment: .center, spacing: 14) {
            Image(systemName: "curlybraces.square.fill")
                .font(.system(size: 36)).foregroundColor(.accentColor).accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 4) {
                Text("Proto Compile").font(.title2.weight(.semibold))
                Text("从协议文档或 Proto 文件生成 Java 源码")
                    .foregroundColor(.secondary)
            }
            Spacer()
            Text("本地转换").font(.caption.weight(.medium))
                .padding(.horizontal, 10).padding(.vertical, 5)
                .background(Color(nsColor: .controlBackgroundColor), in: Capsule())
        }
    }

    private var inputSection: some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 14) {
                Picker("导入类型", selection: Binding(get: { model.inputKind }, set: { model.setKind($0) })) {
                    ForEach(AppModel.InputKind.allCases, id: \.self) { kind in Text(kind.label).tag(kind) }
                }.pickerStyle(.segmented).labelsHidden().accessibilityLabel("导入类型")
                HStack(alignment: .top, spacing: 12) {
                    Image(systemName: model.inputKind == .html ? "doc.text" : "doc.on.doc")
                        .font(.title2).foregroundColor(.secondary).accessibilityHidden(true)
                    VStack(alignment: .leading, spacing: 5) {
                        Text(model.inputPaths.isEmpty ? (model.inputKind == .html ? "选择 HTML 协议文档" : "选择 Proto 文件或目录") : "已选择 \(model.inputPaths.count) 个输入")
                            .font(.headline)
                        if model.inputPaths.isEmpty {
                            Text(model.inputKind == .html ? "支持 .html、.htm 单文件" : "支持多个 .proto 文件，或包含协议的目录")
                                .font(.callout).foregroundColor(.secondary)
                        } else {
                            ForEach(model.inputPaths, id: \.self) { path in pathText(path) }
                        }
                    }.frame(maxWidth: .infinity, alignment: .leading)
                    Button(model.inputPaths.isEmpty ? "选择…" : "更改…", action: model.chooseInput)
                        .keyboardShortcut("o", modifiers: .command)
                }
                if model.inputKind == .proto {
                    Divider()
                    HStack {
                        Text("依赖目录").font(.subheadline.weight(.medium))
                        Text("可选").font(.caption).foregroundColor(.secondary)
                        Spacer()
                        Button("添加…", action: model.addInclude)
                    }
                    if model.includePaths.isEmpty {
                        Text("若协议引用其他目录中的 Proto，请添加对应的导入根目录。")
                            .font(.callout).foregroundColor(.secondary)
                    } else {
                        ForEach(model.includePaths, id: \.self) { path in
                            HStack(spacing: 12) {
                                pathText(path)
                                Spacer(minLength: 0)
                                Button { model.removeInclude(path) } label: { Image(systemName: "minus.circle") }
                                    .buttonStyle(.borderless).help("移除此依赖目录")
                                    .accessibilityLabel("移除依赖目录 \(path)")
                            }
                        }
                    }
                }
            }.padding(10)
        } label: { Label("1. 导入源文件", systemImage: "square.and.arrow.down").font(.headline) }
        .disabled(model.busy)
    }

    private var targetSection: some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 10) {
                Picker("生成目标", selection: Binding(get: { model.targetRuntime }, set: { model.setTargetRuntime($0) })) {
                    ForEach(AppModel.TargetRuntime.allCases, id: \.self) { target in
                        Text(target.label).tag(target)
                    }
                }.pickerStyle(.menu)
                if model.targetRuntime == .compatible {
                    Text("适用于暂时无法升级 protobuf-java 的旧项目。")
                        .font(.callout).foregroundColor(.secondary)
                }
                HStack(spacing: 12) {
                    Text("项目依赖").font(.callout)
                    Text(model.targetRuntime.dependency)
                        .font(.system(.callout, design: .monospaced)).textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                    Button("复制", action: model.copyDependency).help("复制 Maven / Gradle 依赖坐标")
                }
            }.padding(10).frame(maxWidth: .infinity, alignment: .leading)
        } label: { Label("2. 选择生成目标", systemImage: "shippingbox").font(.headline) }
        .disabled(model.busy)
    }

    private var outputSection: some View {
        GroupBox {
            HStack(alignment: .center, spacing: 12) {
                Image(systemName: "folder").font(.title2).foregroundColor(.secondary).accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 5) {
                    if let path = model.outputParent { pathText(path) }
                    else { Text("尚未选择导出位置").foregroundColor(.secondary) }
                    Text("将在此目录下创建独立结果文件夹。")
                        .font(.callout).foregroundColor(.secondary)
                }.frame(maxWidth: .infinity, alignment: .leading)
                Button("选择目录…", action: model.chooseOutput)
            }.padding(10)
        } label: { Label("3. 选择导出父目录", systemImage: "folder").font(.headline) }
        .disabled(model.busy)
    }

    private var statusSection: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(alignment: .center, spacing: 12) {
                if model.isInspecting || model.isConverting {
                    ProgressView().controlSize(.small).frame(width: 22)
                } else {
                    Image(systemName: model.failure != nil ? "exclamationmark.triangle" : (model.inspection != nil ? "checkmark.circle" : "info.circle"))
                        .font(.title3).foregroundColor(model.failure != nil ? .orange : .secondary)
                        .accessibilityHidden(true)
                }
                VStack(alignment: .leading, spacing: 4) {
                    Text(model.stage).font(.headline)
                    Text(model.stageDetail).font(.callout).foregroundColor(.secondary).fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 16)
                if model.isConverting {
                    Button(model.isCancelling ? "正在取消…" : "取消", action: model.cancel)
                        .disabled(model.isCancelling).keyboardShortcut(.cancelAction)
                } else {
                    Button("开始转换", action: model.convert)
                        .buttonStyle(.borderedProminent).controlSize(.large)
                        .disabled(!model.canConvert).keyboardShortcut(.defaultAction)
                }
            }
            if let inspection = model.inspection {
                HStack(spacing: 18) {
                    Label("\(inspection.proto_count ?? 0) 个 Proto", systemImage: "doc.text")
                    Label("\(inspection.excluded?.count ?? 0) 项排除", systemImage: "line.3.horizontal.decrease.circle")
                    Label("\(inspection.warnings?.count ?? 0) 条警告", systemImage: "exclamationmark.triangle")
                }.font(.callout).foregroundColor(.secondary)
                if !(inspection.warnings ?? []).isEmpty || !(inspection.excluded ?? []).isEmpty || !(inspection.source_files ?? []).isEmpty {
                    DisclosureGroup("检查详情", isExpanded: $options.details) {
                        if options.details {
                            VStack(alignment: .leading, spacing: 8) {
                                detailList("警告", values: inspection.warnings ?? [])
                                detailList("已排除", values: inspection.excluded ?? [])
                                detailList("源文件", values: inspection.source_files ?? [])
                            }.padding(.top, 8).frame(maxWidth: .infinity, alignment: .leading)
                        }
                    }.font(.callout)
                }
            }
        }
    }

    private func errorSection(_ failure: EngineFailure) -> some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 10) {
                Label(failure.title, systemImage: "exclamationmark.triangle.fill")
                    .font(.headline).foregroundColor(.primary)
                Text(failure.suggestion).foregroundColor(.secondary).fixedSize(horizontal: false, vertical: true)
                HStack {
                    DisclosureGroup("错误详情", isExpanded: $options.errorDetails) {
                        if options.errorDetails {
                            Text(failure.details).font(.system(.caption, design: .monospaced))
                                .textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading).padding(.top, 8)
                        }
                    }
                    Button("复制详情", action: model.copyError)
                }
            }.padding(10).frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private func successSection(_ result: EngineMessage) -> some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 10) {
                HStack {
                    Label("结果已导出", systemImage: "checkmark.circle.fill").font(.headline)
                    Spacer()
                    Button("在 Finder 中打开", action: model.revealResult)
                }
                if let path = result.output_path { pathText(path) }
                Text("\(result.proto_count ?? 0) 个 Proto  ·  \(result.java_count ?? 0) 个 Java")
                    .font(.callout).foregroundColor(.secondary)
                if let runtime = result.runtime_version {
                    Text("目标运行库：protobuf-java \(runtime)").font(.callout).foregroundColor(.secondary)
                }
                if let warnings = result.warnings, !warnings.isEmpty {
                    Label("\(warnings.count) 条警告", systemImage: "exclamationmark.triangle")
                        .font(.callout.weight(.medium))
                    Text(warnings[0]).font(.callout).foregroundColor(.secondary).lineLimit(2)
                    DisclosureGroup("查看全部警告", isExpanded: $options.warnings) {
                        if options.warnings { recordText(warnings, copyLabel: "复制全部警告") }
                    }
                }
                if let changes = result.changes, !changes.isEmpty {
                    DisclosureGroup("处理记录（\(changes.count) 条）", isExpanded: $options.changes) {
                        if options.changes { recordText(changes, copyLabel: "复制处理记录") }
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

    @ViewBuilder private func detailList(_ title: String, values: [String]) -> some View {
        if !values.isEmpty {
            Text(title).font(.callout.weight(.medium))
            Text(values.joined(separator: "\n\n")).font(.callout).foregroundColor(.secondary)
                .textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
        }
    }

    private func recordText(_ values: [String], copyLabel: String) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Button(copyLabel) {
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(values.joined(separator: "\n"), forType: .string)
            }
            Text(values.joined(separator: "\n\n"))
                .font(.callout).foregroundColor(.secondary).textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
        }.padding(.top, 8).frame(maxWidth: .infinity, alignment: .leading)
    }

    private var aboutView: some View {
        VStack(spacing: 14) {
            Image(systemName: "curlybraces.square.fill").font(.system(size: 56)).foregroundColor(.accentColor).accessibilityHidden(true)
            Text("Proto Compile").font(.title2.weight(.semibold))
            Text("版本 \(model.version) (\(model.build))")
            Text("当前目标：protobuf-java \(model.targetRuntime.rawValue)")
            Text("内置 protoc：\(model.compilerVersion)").textSelection(.enabled)
            Text("将 HTML 协议文档与 Proto 文件转换为 Java 源码。\n所有转换均在本机完成。")
                .multilineTextAlignment(.center).foregroundColor(.secondary)
            Button("完成") { model.showAbout = false }.keyboardShortcut(.defaultAction)
        }.padding(32).frame(width: 400)
    }
}
