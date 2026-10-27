# Proto Compile

**面向 macOS 的 Protobuf 协议转换与蓝牙日志解析工具。**

从 TiddlyWiki 协议文档或 `.proto` 文件生成 Java、Objective-C 源码；根据所选 device protocol 文档，将完整 BLE 帧或 JSON payload 日志解析为可读的 TXT。协议和日志内容均在本机处理。

![Version](https://img.shields.io/badge/version-1.4.1-blue)
![macOS](https://img.shields.io/badge/macOS-13.0%2B-black)
![Architecture](https://img.shields.io/badge/architecture-arm64%20%7C%20x86__64-lightgrey)
![Output](https://img.shields.io/badge/output-Java%20%7C%20Objective--C%20%7C%20TXT-orange)

[功能](#功能) · [安装](#安装) · [协议转换](#协议转换) · [蓝牙日志解析](#蓝牙日志解析) · [命令行](#命令行) · [源码构建](#源码构建) · [开发与测试](#开发与测试) · [贡献](#贡献)

## 功能

| 功能 | 支持范围 |
| --- | --- |
| 协议输入 | 受支持的 TiddlyWiki HTML、Proto 文件及目录；桌面应用支持选择多个 Proto 文件 |
| 代码生成 | Java、Objective-C，导出源码及对应 `.proto` 文件 |
| 编译工具链 | 默认 Protobuf 3.25.9，另提供 3.6.1 兼容模式 |
| 输入检查 | 依赖解析、语法检查、生成符号冲突检测、中文错误提示 |
| 蓝牙日志解析 | 完整 BLE 帧或格式化 JSON payload 日志，按所选协议解释字段与消息 |
| 结果导出 | 独立结果目录；成功后发布，支持取消，避免暴露未完成的输出 |
| 桌面体验 | 原生 SwiftUI 界面，两页切换保留滚动及展开状态 |
| 本地运行 | 应用内置 Python 与 protoc；数据解析、协议转换无需上传文件 |

## 系统要求

| 项目 | 要求或默认值 |
| --- | --- |
| 当前版本 | **1.4.1（构建号 7）** |
| 桌面应用 | macOS 13.0 及以上 |
| 架构 | Apple Silicon（arm64）和 Intel（x86_64），分别构建 |
| 源码 CLI | Python 3.9+；自动工具链准备仅支持 macOS |
| 默认导出语言 | Java |
| 默认工具链 | protoc 25.9 → Protobuf 运行库 3.25.9 |
| 兼容工具链 | protoc 3.6.1 → Protobuf 运行库 3.6.1 |

匹配架构的桌面安装包无需另外安装 Python、JDK 或 protoc。生成代码的目标工程仍需接入对应的 Protobuf 运行库。

## 安装

安装包可通过[源码构建](#源码构建)生成；已发布的附件以仓库 [Releases](https://github.com/Mr414Zheng/proto-compile/releases) 页面为准。Git 源码仓库不包含 `dist/` 中的应用包。

| Mac 类型 | 构建产物 |
| --- | --- |
| Apple Silicon | `Proto-Compile-1.4.1-arm64.zip` |
| Intel | `Proto-Compile-1.4.1-x86_64.zip` |

解压对应架构的 ZIP，将 **Proto Compile.app** 放入“应用程序”目录后打开。

> 当前构建采用 ad-hoc 签名，尚未集成 Developer ID 签名和 Apple 公证。macOS 可能提示无法验证开发者；请核对安装包来源，并按系统提供的安全提示处理。

## 协议转换

1. 在顶部选择 **协议转换**。
2. 导入 HTML 文档，或选择 Proto 文件、目录；需要时添加依赖目录。
3. 选择 **Java** 或 **Objective-C**，以及目标运行库版本。
4. 查看自动检查结果，处理错误并核对警告。
5. 选择导出父目录，点击 **开始转换**；完成后可在 Finder 中打开结果。

每次成功转换生成独立目录，`java/` 与 `objc/` 根据所选语言生成其一：

```text
协议转换-日期时间-唯一标识/
├── proto/                   # 对应的协议文件
└── java/                    # Java 源码
```

```text
协议转换-日期时间-唯一标识/
├── proto/
└── objc/                    # Objective-C 头文件与实现文件
    ├── Example.pbobjc.h
    └── Example.pbobjc.m
```

Objective-C 文件计数包含 `.h` 和 `.m`。临时文件、描述符及预检缓存不会混入导出目录。

### 接入生成代码

**Java** 使用完整运行库。Gradle 示例：

```groovy
implementation 'com.google.protobuf:protobuf-java:3.25.9'
```

**Objective-C** 将生成的 `.pbobjc.h`、`.pbobjc.m` 按原有目录结构加入工程，并接入对应运行库。CocoaPods 示例：

```ruby
pod 'Protobuf', '3.25.9'
```

使用兼容模式时，将依赖版本改为 `3.6.1`。导出代码本身不要求安装 JDK 或 CocoaPods；这些依赖由目标工程管理。

普通 Proto 保留已有语言选项。HTML 的 Java 输出默认不声明包名，可通过 CLI 的 `--java-package` 设置根包。HTML 的 Objective-C 输出为未声明 `objc_class_prefix` 的文件添加稳定前缀，并保留已有前缀；公开符号冲突时会停止导出。

## 蓝牙日志解析

1. 在顶部选择 **蓝牙日志解析**。
2. 选择日志格式并导入日志文件。
3. 选择与设备版本对应的 **device protocol HTML** 文档。
4. 选择导出父目录，点击 **开始解析**。
5. 查看正常、提示及错误记录计数，打开导出的 TXT 或结果目录。

### 完整 BLE 帧

使用 UTF-8 纯文本，**每行一条完整帧**。不包含时间、方向、空格、分隔符或 `0x` 前缀；大小写不限，空行可忽略。

```text
44540F0098E1FFFF0D14000000152D0000001D88130000
445402009E1E01005001
```

解析器检查 `DT` 前缀、payload 长度和 CRC。纯帧没有时间和方向；可判断时会输出消息与方向的推断依据，无法可靠区分时保留候选及未知值。

### 格式化 JSON 日志

每行一个 JSON 对象，可在对象前保留以冒号分隔的时间：

```text
{"id":"1","name":"up","sentence":"0801"}
2026-10-27 10:43:05:{"id":"1","name":"up","sentence":"0801"}
```

| 字段 | 含义 |
| --- | --- |
| `id` | 操作码的十进制值，可为字符串或整数 |
| `name` | `up`：设备上报 App；`down`：App 下发设备 |
| `sentence` | 十六进制 Protobuf payload，不含完整帧头 |
| 行首时间 | 可选；保留输入中的时间文本，不推测时区 |

JSON payload 路径不执行帧级 CRC 校验，不能据此判断原始完整帧的 CRC 是否正确。

### 输出约定

每条记录只输出两部分：

```text
原始十六进制字符串：
<输入中的完整帧或 payload>

结构体：
<按所选协议解码的 JSON 结构体>
```

结构体包含 `timestamp`、`opcode`、`direction`、`message` 和 `data`，不包含 `raw_timestamp`。其中：

- 字段名、message 名称及已识别的枚举名称遵循所选协议文档。
- 只输出实际编码的字段，不补造未传输的默认值；不因旧设备缺少新版字段直接判错。
- 保留输入顺序与重复记录；未知字段、坏帧及截断数据保留原始内容和诊断。
- 错误记录可以随其他记录一起导出；文件级错误或取消不会发布未完成的结果。

完整约定见 [蓝牙日志解析规范](蓝牙日志解析规范.md)；该文档保留其现有的审阅稿状态。

### 协议与输入限制

协议映射来自所选文档的 `Content` 中 `OPT_xxxx` 主链接、`$(FILE)` 声明及受支持的消息角色约定。此功能面向具有约定结构的 device protocol 文档，不是任意 HTML 或任意 BLE 协议的通用解码器。

日志单行最多 **8 MiB**，payload 最多 **1 MiB**，消息嵌套最多 **32 层**；完整帧还受其帧头长度字段约束。超限或无法可靠解释的记录会给出中文提示或保留诊断。

## 命令行

CLI 提供协议提取与代码生成。蓝牙日志解析使用桌面页面；目前没有独立的用户 CLI 子命令。

### 获取源码

```bash
git clone https://github.com/Mr414Zheng/proto-compile.git
cd proto-compile

# 准备默认 protoc 工具链（首次需联网下载并校验）
python3 proto_compile.py setup
```

以下示例的输入文件由使用者自行提供，仓库不分发私有协议文档。

```bash
# Proto 目录 → Java
python3 proto_compile.py compile ./protos -o ./output/java-example

# 单文件及依赖目录 → Objective-C
python3 proto_compile.py compile ./protos/main.proto \
  -I ./protos -I ./includes \
  --language objc -o ./output/objc-example

# TiddlyWiki HTML → Java，指定根包
python3 proto_compile.py compile ./private-inputs/protocol.html \
  --java-package com.example.protocol -o ./output/html-example

# 3.6.1 兼容模式
python3 proto_compile.py setup --target-runtime 3.6.1
python3 proto_compile.py compile ./protos \
  --target-runtime 3.6.1 -o ./output/legacy-example

# 仅从 HTML 提取并规范化 Proto
python3 proto_compile.py extract ./private-inputs/protocol.html \
  -o ./output/extracted-proto
```

`compile` 接收一个输入路径；多个文件可放入目录或使用桌面应用导入。**CLI 输出目录必须不存在或为空**，不会覆盖非空目录。省略 `-o` 时，编译输出到 `output/`，提取输出到 `output/proto/`。

### 常用参数

| 参数 | 说明 |
| --- | --- |
| `--language java\|objc` | 导出语言，默认 Java；`compile` 和 `extract` 支持 |
| `--target-runtime 3.25.9\|3.6.1` | 目标运行库；`setup` 和 `compile` 支持 |
| `-o PATH` | 输出目录 |
| `-I PATH` / `--proto-path PATH` | 普通 Proto 的依赖根目录，可重复指定 |
| `--protoc PATH` | 指定与目标运行库版本匹配的编译器 |
| `--java-package NAME` / `--no-java-package` | HTML 的 Java 包名策略 |
| `--repairs FILE` / `--no-repairs` | 替换或禁用 HTML 文档修正规则 |
| `--include-opt-0081` | 尝试包含 HTML 中默认排除的 OPT_0081 |

除另有说明外，以上参数用于 `compile`。完整参数以帮助输出为准：

```bash
python3 proto_compile.py --help
python3 proto_compile.py compile --help
python3 proto_compile.py extract --help
```

## 协议转换的兼容范围

**TiddlyWiki HTML** 支持旧版 `div[title] > pre` 和 JSON tiddler store；按 `$(FILE)` 提取协议，不执行文档脚本。转换会规范化命名空间、导入关系和类型引用，并处理枚举值命名冲突。

- 保留字段编号和二进制编码语义，但规范化可能改变描述符类型全名；使用反射或 Any type URL 的工程应核对接入结果。
- HTML 转换目前不支持 `service`、`extend`、`group`，以及 Java 模式下已有的 `java_*` 选项；遇到时明确报错。
- [内置修正规则](config/device-v16.2-repairs.json) 仅在源文本摘要匹配时生效，不修改原始 HTML。
- 默认排除 OPT_0081 对应的 `his_encrypt_data.proto`，其参考文档缺少被引用的 `HisEncryptData` 定义。

**普通 Proto** 保留源声明和选项，递归解析用户依赖，按 import 相对路径导出；缺失或歧义依赖会报错。`google/protobuf` 标准类型使用配套运行库，不重复生成。

## 源码构建

需要 macOS、Python 3、Xcode 及命令行工具。首次准备会下载固定版本的 Python、编译工具链和构建依赖。

```bash
# 准备两个架构的构建环境及两套运行库工具链
python3 scripts/bootstrap.py

# 构建应用并生成 ZIP
python3 scripts/build_app.py
```

只构建一个架构：

```bash
python3 scripts/bootstrap.py --arch arm64
python3 scripts/build_app.py --arch arm64
# Intel 使用 --arch x86_64
```

产物目录：

```text
dist/
├── arm64/Proto Compile.app
├── x86_64/Proto Compile.app
├── Proto-Compile-1.4.1-arm64.zip
└── Proto-Compile-1.4.1-x86_64.zip
```

构建依赖及缓存放在 `.tools/`，不安装到系统目录。依赖版本见 [build-requirements.txt](scripts/build-requirements.txt)。构建包含 ad-hoc 签名和完整性检查，不包含正式签名、公证或 GitHub Release 发布。

## 开发与测试

### 项目结构

```text
proto-compile/
├── macos/                   # SwiftUI 界面、应用模型和原生测试
├── proto_tool/              # 协议提取、转换、BLE 解码和引擎
├── config/                  # 带摘要约束的协议修正规则
├── scripts/                 # 构建与性能基准工具
├── tests/                   # Python、生成代码及应用包验证
├── proto_compile.py         # 用户 CLI 入口
├── engine_main.py           # 桌面应用引擎入口
└── 蓝牙日志解析规范.md
```

桌面引擎使用 stdin JSON 请求和 stdout NDJSON 响应。协议版本保持为 `1`；转换 CLI 与桌面应用共用服务层。

### 常规验证

```bash
# Python 测试
python3 -m unittest discover -s tests -v

# 原生窗口与长详情测试，需要可用的 macOS 图形会话
python3 macos/Tests/run_workspace_view_tests.py

# 应用包验证；匹配本机架构时执行实际转换链路
python3 tests/verify_bundle.py "dist/arm64/Proto Compile.app" --arch arm64
python3 tests/verify_bundle.py "dist/x86_64/Proto Compile.app" --arch x86_64
```

原生窗口测试仅在临时源码副本中注入详情初始展开状态，不修改生产源码。依赖私有文档的测试在缺少输入时会明确跳过；通用测试不要求公开私有协议。

生成代码的目标工程验证另见 [Java 验证脚本](tests/validate_java.py) 和 [Objective-C 验证脚本](tests/validate_objc.py)。这些验证需要相应的 JDK、运行库或源码，不替代实际 Android / iOS 工程集成测试。

### 性能基准

| 工具 | 用途 |
| --- | --- |
| [benchmark_app.py](scripts/benchmark_app.py) | 协议转换的可见应用与进程树采样 |
| [benchmark_ble.py](scripts/benchmark_ble.py) | 两份源码的 BLE CPU、内存及输出等价对照 |
| [benchmark_ble_app.py](scripts/benchmark_ble_app.py) | 可见应用的切页、连续切换及 BLE 解析采样 |

使用各脚本的 `--help` 查看参数。基准应用需支持对应的测量入口；不要将不含该入口的旧安装包直接当作可驱动基线。正式计时应串行运行，并避免与构建、其他测试争用 CPU。源码服务和打包应用的结果应分别记录。

## 1.4.1 更新

- 页面惰性创建并保留，统一 About 弹窗，隔离隐藏页的焦点、快捷键与辅助功能。
- 简化十六进制校验，降低大单条记录的瞬时内存。
- 使用等价的标准库 CRC 实现，加快完整帧解析。
- 解码进度限制为每 100 ms 至多更新一次；阶段及终态立即发送，取消仍逐条检查。

本机 ARM64、macOS 27.0.1、16 GiB 环境下，相对 1.4.0 的五轮中位数：40 次切页 CPU 时间下降约 **58%–62%**，访问两页后的 RSS 增量低于 **1.2 MiB**；指定大单条样本峰值 RSS 下降约 **83%–92%**。这些是特定场景的测量值，不代表所有日志或整机占用率的统一降幅。

当前版本已通过 128 项 Python 测试及 Swift / 原生窗口专项验证；ARM64 完成实际应用运行和转换链路检查。Intel 目前完成静态包验证，Intel 与最低 macOS 13 的运行兼容性仍需相应真机验收。

## 贡献

欢迎通过 [Issues](https://github.com/Mr414Zheng/proto-compile/issues) 提交问题，或通过 Pull Request 提交改进。

问题报告建议包含应用版本、macOS 版本、CPU 架构、操作步骤、预期与实际结果，以及脱敏后的最小输入。提交代码前请运行相关测试，说明行为变化和验证范围。

私有协议、设备日志、凭据和构建产物不应进入提交。`.gitignore` 已排除协议 HTML / Proto、生成源码、`private-inputs/`、`.tools/`、`output/` 和 `dist/`；提交前仍需检查待提交内容。

## 许可证与第三方组件

本项目目前**未声明项目许可证**，不应据此假定已授予某种开源许可。

第三方组件遵循各自许可。相关文本随应用存放在 `Contents/Resources/Licenses/`，部分构建许可文本位于 [scripts/licenses](scripts/licenses/)。
