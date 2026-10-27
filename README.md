# Proto Compile

**在 macOS 上，将 TiddlyWiki 协议文档或 `.proto` 文件转换为 Java / Objective-C Protobuf 源码。**

Proto Compile 提供原生 SwiftUI 桌面应用与 Python 命令行工具，支持协议提取、依赖解析、输入检查和代码生成。协议内容在本机处理，无需上传到服务器。

![Version](https://img.shields.io/badge/version-1.4.0-blue)
![Platform](https://img.shields.io/badge/macOS-13.0%2B-black)
![Architecture](https://img.shields.io/badge/architecture-arm64%20%7C%20x86__64-lightgrey)
![Output](https://img.shields.io/badge/output-Java%20%7C%20Objective--C-orange)

[快速开始](#快速开始) · [命令行](#命令行) · [运行库接入](#运行库接入) · [源码构建](#源码构建) · [开发与验证](#开发与验证)

## 功能

- **两种输入方式**：从受支持的 TiddlyWiki HTML 文档提取协议，或直接编译 `.proto` 文件、多个文件及目录。
- **两种导出语言**：生成 Java 或 Objective-C 源码，并保留对应的 `.proto` 文件。
- **两套编译工具链**：默认运行库版本 `3.25.9`，另提供 `3.6.1` 兼容模式。
- **转换前检查**：解析依赖，报告排除项、警告、语法错误及生成符号冲突。
- **独立结果目录**：桌面应用每次转换创建新目录；转换成功后才发布结果，支持取消任务。
- **自包含 macOS 应用**：分别提供 Apple Silicon 和 Intel 构建，内置 Python 与 protoc。
- **蓝牙日志解析**：选择 device protocol HTML 文档，解析完整 BLE 帧或格式化 JSON payload 日志，按条导出可读 TXT。

## 兼容性

| 项目 | 说明 |
| --- | --- |
| 当前版本 | 1.4.0（构建号 6） |
| 桌面应用 | macOS 13.0 及以上 |
| CPU 架构 | Apple Silicon（arm64）、Intel（x86_64），分别构建 |
| 命令行 | Python 3.9+；自动工具链准备仅支持 macOS |
| 默认语言 | Java |
| 默认模式 | protoc 25.9 → Protobuf 运行库 3.25.9 |
| 兼容模式 | protoc 3.6.1 → Protobuf 运行库 3.6.1 |

Java 输出使用完整运行库，不是 Lite。生成代码应搭配所选模式对应的语言运行库；应用内显示并可复制依赖声明。

## 快速开始

### 桌面应用

仓库不包含编译后的应用。请按[源码构建](#源码构建)生成安装包，或使用维护者提供的对应架构安装包：

| Mac 类型 | 安装包 |
| --- | --- |
| Apple Silicon（M 系列） | `Proto-Compile-1.4.0-arm64.zip` |
| Intel | `Proto-Compile-1.4.0-x86_64.zip` |

解压后，将 **Proto Compile.app** 移入“应用程序”目录。匹配架构的应用无需另装 Python、JDK、protoc 或 Rosetta。

1. 导入 HTML 文档，或选择 Proto 文件 / 目录；需要时添加依赖目录。
2. 选择 **Java** 或 **Objective-C**，再选择目标运行库版本。
3. 查看自动检查结果，处理错误并核对警告。
4. 选择导出父目录，点击 **开始转换**。
5. 完成后点击 **在 Finder 中打开**。

桌面应用创建的结果目录如下，`java/` 与 `objc/` 根据所选语言生成其一：

```text
协议转换-日期时间-唯一标识/
├── proto/                   # 对应的协议文件
└── java/                    # Java 源码
```

```text
协议转换-日期时间-唯一标识/
├── proto/
└── objc/
    ├── Example.pbobjc.h
    └── Example.pbobjc.m
```

Objective-C 文件数量包含头文件和实现文件。预检缓存、描述符及临时文件不会混入最终结果。

### 首次打开

当前构建采用 **ad-hoc 签名，尚未进行 Developer ID 签名及 Apple 公证**。若 macOS 阻止打开，请先确认安装包来源可信，再按照系统设置中的 **隐私与安全性 → 仍要打开** 流程处理。无需关闭系统安全保护。

参见 [Apple：打开未经过公证的 Mac 应用](https://support.apple.com/zh-cn/102445)。

## 蓝牙日志解析

在应用顶部选择“蓝牙日志解析”，导入设备实际使用的 device protocol HTML 文档，再选择日志类型和日志文件。选择导出父目录后点击“开始解析”。每次操作创建独立的结果目录，成功后可打开 TXT 或在 Finder 中查看；取消或文件级错误不会发布半成品。

完整 BLE 帧文件为 UTF-8 纯文本，一行一帧，不带时间、方向、空格或 `0x` 前缀，例如：

```text
44540F0098E1FFFF0D14000000152D0000001D88130000
445402009E1E01005001
```

格式化 JSON 日志为一行一条记录，可包含原始时间：

```text
2026-10-27 10:43:05:{"id":"1","name":"up","sentence":"0801"}
```

`id` 是十进制操作码，`name` 为 `up`（设备上报）或 `down`（App 下发），`sentence` 为十六进制 payload。解析完整帧时检查前缀、长度和 CRC；仅有 payload 时不做帧级 CRC 校验。

每条 TXT 记录只包含“原始十六进制字符串”和“结构体”。结构体具有 `timestamp`、`opcode`、`direction`、`message` 和 `data`，不包含 `raw_timestamp`。协议字段名、嵌套结构、枚举名和实际字段存在性予以保留；字节串显示为十六进制。纯帧没有时间和方向，解析器会在可判断时标注推断依据；无法区分方向或消息时保留候选，不编造结果。

协议文件沿用应用支持的 TiddlyWiki `$(FILE)` 声明格式，并通过 `Content` 中的 `OPT_xxxx` 主链接与 device protocol 消息角色约定建立映射。字段定义来自本次选择的文档；不支持或缺失的根消息映射、未知字段、坏帧和截断数据会在结果中保留并标注。界面展示正常、提示和错误记录的数量，存在坏记录也可导出其诊断；未编码的新版字段不自动视为旧设备错误。日志需为 UTF-8，单行最多 8 MiB，payload 解码及嵌套深度另有安全限制，超限会给出中文提示。

## 命令行

### 获取源码与工具链

```bash
git clone https://github.com/Mr414Zheng/proto-compile.git
cd proto-compile

# 下载并校验默认模式的 protoc（macOS）
python3 proto_compile.py setup
```

以下示例中的输入文件由使用者自行提供，仓库不附带私有协议文档。

### 编译协议

```bash
# Proto 目录 → Java（默认）
python3 proto_compile.py compile ./protos -o ./output/java-example

# 单文件及依赖 → Objective-C
python3 proto_compile.py compile ./protos/main.proto \
  -I ./protos -I ./includes \
  --language objc -o ./output/objc-example

# TiddlyWiki HTML → Java
python3 proto_compile.py compile ./private-inputs/protocol.html \
  -o ./output/html-example

# 兼容模式：先准备 protoc 3.6.1，再生成对应代码
python3 proto_compile.py setup --target-runtime 3.6.1
python3 proto_compile.py compile ./protos \
  --language objc --target-runtime 3.6.1 -o ./output/legacy-example

# 仅提取并规范化 HTML 中的 Proto，不运行 protoc
python3 proto_compile.py extract ./private-inputs/protocol.html \
  -o ./output/extracted-proto
```

`compile` 接收一个输入路径；多个文件可通过目录或桌面应用导入。`-o` 指定结果根目录，其下生成 `proto/` 及 `java/` 或 `objc/`。省略 `-o` 时，编译使用 `output/`，提取使用 `output/proto/`。

**命令行输出目录必须不存在或为空。** 再次执行时请使用新路径，工具不会覆盖非空目录。

### 常用选项

| 参数 | 作用 |
| --- | --- |
| `--language java\|objc` | 选择导出语言，默认 `java`；`compile`、`extract` 均支持 |
| `--target-runtime 3.25.9\|3.6.1` | 选择目标运行库；用于 `setup`、`compile` |
| `-o PATH` | 指定输出目录 |
| `-I PATH` / `--proto-path PATH` | 添加普通 Proto 的导入根目录，可重复指定 |
| `--protoc PATH` | 使用指定编译器，版本必须与目标运行库匹配 |
| `--java-package NAME` | 为 HTML 的 Java 输出指定根包，按协议文件创建子包 |
| `--no-java-package` | 显式使用 HTML 默认的无 Java 包名行为 |
| `--repairs FILE` | 使用自定义 HTML 修正规则替代内置规则 |
| `--no-repairs` | 禁用文档修正，仍执行协议规范化 |
| `--include-opt-0081` | 尝试包含 HTML 中默认排除的 OPT_0081 |

除单独注明外，表中参数用于 `compile`。Java 包名选项仅用于 HTML 的 Java 输出；修正和排除选项仅用于 HTML；`-I` 仅用于普通 Proto。

查看完整帮助：

```bash
python3 proto_compile.py --help
python3 proto_compile.py compile --help
python3 proto_compile.py extract --help
```

## 运行库接入

### Java

Gradle：

```groovy
implementation 'com.google.protobuf:protobuf-java:3.25.9'
```

Maven：

```xml
<dependency>
  <groupId>com.google.protobuf</groupId>
  <artifactId>protobuf-java</artifactId>
  <version>3.25.9</version>
</dependency>
```

兼容模式请将版本改为 `3.6.1`。若生成代码出现编译错误，请核对工程实际解析到的运行库版本、旧 JAR 和 SDK 依赖约束。

普通 Proto 保留原有 `java_*` 选项。HTML 默认生成不带 Java 包名的代码；接入有包名的业务工程时，可通过 CLI 设置根包：

```bash
python3 proto_compile.py compile ./private-inputs/protocol.html \
  --java-package com.example.protocol -o ./output/packaged-example
```

### Objective-C

将生成的 `.pbobjc.h`、`.pbobjc.m` 及其相对目录结构加入目标工程，并接入匹配版本的运行库。CocoaPods 示例：

```ruby
pod 'Protobuf', '3.25.9'
# 兼容模式：pod 'Protobuf', '3.6.1'
```

导出文件不要求安装 CocoaPods；运行库由目标工程管理。

Objective-C 类名不会自动继承 Proto 包命名空间。HTML 模式为未声明 `objc_class_prefix` 的文件添加稳定的 `PCT..._` 前缀，保留已有前缀；普通 Proto 保留用户声明。公开类或枚举符号冲突时会停止导出，并提示调整前缀。

参见 [Protobuf：Objective-C 生成代码说明](https://protobuf.dev/reference/objective-c/objective-c-generated/)。

## 输入规则与限制

### TiddlyWiki HTML

- 支持旧版 `div[title] > pre` 和 JSON tiddler store，按 `$(FILE)` 标记提取协议，不执行文档脚本。
- 规范化命名空间、导入关系和类型引用，处理枚举值命名冲突；同名同内容声明会去重。
- 保留字段编号及二进制编码语义；规范化可能改变描述符类型全名，使用反射或 Any type URL 的工程需核对。
- 当前不支持 HTML 内的 `service`、`extend`、`group`，以及 Java 模式下已有的 `java_*` 选项；遇到这些声明会明确报错。
- 内置文档修正规则仅在源文本 SHA-256 匹配时生效，不修改原始 HTML。规则见 [`config/device-v16.2-repairs.json`](config/device-v16.2-repairs.json)。
- 默认排除 OPT_0081 对应的 `his_encrypt_data.proto`，因为参考文档中缺少其引用的 `HisEncryptData` 定义；可用 `--include-opt-0081` 尝试包含。

此功能面向具有约定结构的协议文档，不是通用 HTML 转换器。

### 普通 Proto

递归解析用户协议依赖，按 import 相对路径复制到 `proto/`，保留源声明和选项。语法由所选版本的 protoc 校验；缺失或歧义依赖会报错。`google/protobuf` 标准类型使用配套运行库实现，不重复生成或导出。

## 源码构建

需要 macOS、Python 3、Xcode 及命令行工具。首次构建需联网下载固定版本的依赖。

```bash
# 准备 Python、构建依赖及两个架构的两套 protoc
python3 scripts/bootstrap.py

# 构建两个架构的应用与 ZIP
python3 scripts/build_app.py
```

仅构建 Apple Silicon：

```bash
python3 scripts/bootstrap.py --arch arm64
python3 scripts/build_app.py --arch arm64
```

Intel 使用 `--arch x86_64`。构建产物位于：

```text
dist/
├── arm64/Proto Compile.app
├── x86_64/Proto Compile.app
├── Proto-Compile-1.4.0-arm64.zip
└── Proto-Compile-1.4.0-x86_64.zip
```

构建工具将 Python、工具链和缓存放在 `.tools/`，不安装到系统目录，也不修改系统 PATH。依赖版本见 [`scripts/build-requirements.txt`](scripts/build-requirements.txt)，下载文件使用固定摘要校验。桌面应用构建需要备齐两个运行库目标。

构建脚本目前只完成 ad-hoc 签名与完整性检查；正式签名、公证流程尚未集成。

## 开发与验证

### 项目结构

```text
proto-compile/
├── macos/                   # SwiftUI 应用、应用模型和 Swift 测试
├── proto_tool/              # 提取、规范化、转换服务和工具链管理
├── config/                  # 带源文本摘要约束的修正规则
├── scripts/                 # 构建、打包和性能测量工具
├── tests/                   # Python 回归及生成代码验证
├── proto_compile.py         # CLI 入口
└── engine_main.py           # 桌面应用转换引擎入口
```

CLI 与桌面引擎共用转换服务。桌面引擎通过 stdin 接收 v1 JSON 请求，通过 stdout 输出 NDJSON 事件。`output_language` 默认为 `java`，`target_runtime` 默认为 `3.25.9`。

### 运行验证

```bash
# 准备两种运行库工具链
python3 proto_compile.py setup
python3 proto_compile.py setup --target-runtime 3.6.1

# Python 回归测试
python3 -m unittest discover -s tests -v

# 应用包检查；匹配本机架构时执行实际转换链路
python3 tests/verify_bundle.py "dist/arm64/Proto Compile.app" --arch arm64

# Java 回归验证：需自行提供私有参考文档的默认无包名生成结果、JDK 和运行库 JAR
python3 tests/validate_java.py \
  --jdk /path/to/jdk17/Contents/Home \
  --runtime-jar /path/to/protobuf-java-3.25.9.jar \
  --generated /path/to/reference-output/java --no-java-package

# Objective-C 验证：自行提供完整官方运行库源码与生成目录
python3 tests/validate_objc.py \
  --runtime-source /path/to/protobuf-25.9/objectivec \
  --target-runtime 3.25.9 --generated ./output/objc-example/objc
```

Java 验证使用 `tests/JavaSmoke.java` 中的参考协议类型，不适用于任意协议输入。Objective-C 验证会编译全部生成文件，并用合成协议检查跨文件类型、枚举、oneof、repeated、精确字节及 required 约束；其运行库使用非 ARC、DEBUG 构建。兼容模式须使用对应的 3.6.1 依赖。

截至 2026-10-26，1.3.0 的验证记录：

- 98 项 Python 回归测试通过；Java 两种模式的文件摘要与前版基线一致。
- Apple Silicon 上两种语言 × 两种运行库的真实预检、转换及应用搬移验证通过。
- Objective-C 两种模式的完整参考文档产物通过编译、链接及序列化验证。
- 两个架构的包结构、依赖、最低系统声明、签名和 ZIP 解压完整性检查通过。

Intel 真机、macOS 13 真机和完整原生交互尚未完成验收；macOS 上的生成代码验证也不替代目标 iOS 工程或真实设备集成测试。

## 参与贡献

欢迎通过 [Issues](https://github.com/Mr414Zheng/proto-compile/issues) 报告问题，或提交 Pull Request。问题报告请包含应用版本、Mac 架构、导出语言、运行库模式、复现步骤与错误信息；协议示例请使用脱敏后的最小样例。

提交代码前运行相关测试，并说明行为变化及验证结果。

### 私有文件

仓库不分发私有协议文档。缺少本地参考 HTML 时，依赖该文档的测试会明确跳过，通用测试仍可运行。

`.gitignore` 排除协议 HTML / Proto、生成的 Java / Objective-C 文件、`private-inputs/`、本地凭据及构建产物；通用测试程序按规则保留。提交前请检查待提交内容，不要强制加入私有协议文件。

## 许可与第三方组件

本仓库目前未声明项目许可证。第三方组件遵循各自许可，相关文本随应用放在 `Contents/Resources/Licenses/`，部分构建许可文本位于 [`scripts/licenses/`](scripts/licenses/)。
