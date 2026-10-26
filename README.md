# Proto Compile

将 TiddlyWiki 协议文档或 `.proto` 文件转换为 Java 或 Objective-C Protobuf 源码，提供原生 SwiftUI macOS 应用和 Python 命令行工具。协议解析与代码生成均在本机完成。

| 项目 | 当前状态 |
| --- | --- |
| 应用版本 | 1.3.0，构建号 5 |
| 最低系统要求 | macOS 13.0 |
| 分发架构 | Apple Silicon（arm64）、Intel（x86_64），分别打包 |
| 默认生成目标 | Java；protoc 25.9，运行库版本 3.25.9 |
| 支持语言与运行库 | Java / Objective-C；3.25.9 / 3.6.1 |
| 签名与公证 | 本地 ad-hoc 签名，尚未使用 Developer ID 签名或完成 Apple 公证 |

## 安装与首次打开

根据 Mac 芯片选择 `dist/` 中的安装包：

| Mac 类型 | 安装包 |
| --- | --- |
| Apple Silicon，例如 M 系列芯片 | `Proto-Compile-1.3.0-arm64.zip` |
| Intel | `Proto-Compile-1.3.0-x86_64.zip` |

解压后，将 **Proto Compile.app** 拖入“应用程序”目录。应用内置 Python 3.12.10 和对应架构的两套 protoc；使用匹配架构的包，无需另装 Python、JDK、protoc 或 Rosetta。

安装包属于本地构建产物，`dist/` 不纳入 Git；仅获取源码时，需要自行构建或向维护者获取安装包。

### 提示“Apple 无法验证 Proto Compile”怎么办？

当前包尚未经过 Apple 公证，分享给其他 Mac 后可能被系统安全机制拦截。这个提示表示 Apple 无法完成验证，并不等同于已检测到恶意软件。

确认安装包来自可信来源且未被修改后：

1. 在弹窗中选择“完成”。
2. 打开 **系统设置 → 隐私与安全性**。
3. 找到 Proto Compile 的拦截提示，点击 **“仍要打开”**。
4. 再次确认“打开”；系统会保存这个应用的例外。

无需关闭系统安全保护。受公司管理的 Mac 可能需要 IT 管理员协助。操作依据见 [Apple 官方打开说明](https://support.apple.com/zh-cn/102445)。

## 使用桌面应用

1. **导入源文件**：选择一个 HTML 文档，或选择一个、多个 `.proto` 文件及协议目录。Proto 模式可添加依赖目录。
2. **选择导出语言与运行库**：Java（默认）或 Objective-C，再按目标工程的 Protobuf 版本选择默认模式或兼容模式。切换语言会重新预检，转换期间不能切换。
3. **等待检查完成**：查看协议数量、排除项、警告和错误详情；输入或生成目标变化后会重新检查。
4. **选择导出父目录**：点击“开始转换”，需要时可取消。
5. **查看结果**：完成后点击“在 Finder 中打开”。

每次成功转换都会在导出父目录下创建独立结果文件夹，不覆盖历史结果：

```text
协议转换-日期时间-唯一标识/
├── java/     # 生成的 Java 源码
└── proto/    # 对应的协议文件
```

上图为 Java 输出。选择 Objective-C 时，结果目录仅包含 `objc/` 和 `proto/`，`objc/` 内为配对的 `.pbobjc.h`、`.pbobjc.m` 文件，不同时导出 Java。预检缓存、描述符及编译临时文件不会混入最终结果；转换成功后才发布结果目录。

“高级设置”“检查详情”“错误详情”“查看全部警告”和“处理记录”均支持点击箭头、标题文字或标题行空白处展开、收起。详情可查看、选择或复制；主窗口和“关于”显示应用及编译器版本。

### 选择正确的运行库

| 模式 | 实际编译器 | 工程依赖 | 适用情况 |
| --- | --- | --- | --- |
| 默认 | protoc 25.9 | `com.google.protobuf:protobuf-java:3.25.9` | 使用对应运行库的工程 |
| 兼容 | protoc 3.6.1 | `com.google.protobuf:protobuf-java:3.6.1` | 暂时无法升级运行库的旧工程 |

上表为 Java 依赖；两种 Java 模式都生成完整运行库代码，不是 Lite 代码。Objective-C 使用对应版本的 `Protobuf` Objective-C 运行库。切换版本会实际更换编译器并重新预检。

Gradle 示例：

```groovy
implementation 'com.google.protobuf:protobuf-java:3.25.9'
```

Maven 示例：

```xml
<dependency>
  <groupId>com.google.protobuf</groupId>
  <artifactId>protobuf-java</artifactId>
  <version>3.25.9</version>
</dependency>
```

兼容模式将版本替换为 `3.6.1`。如果生成代码出现 `UnusedPrivateParameter`、`@Override` 等编译错误，先检查工程实际解析到的运行库版本、旧 JAR 和其他 SDK 的依赖约束，再用匹配模式重新生成；不要手工删除生成代码中的方法或注解。

### Objective-C 输出与接入

选择 Objective-C 后，同一份 v16.2 参考文档生成 **57 个 Proto、57 个 `.pbobjc.h` 和 57 个 `.pbobjc.m`**，界面中的“114 个 Objective-C 文件”包含头文件和实现文件。

```text
协议转换-日期时间-唯一标识/
├── objc/
│   ├── Example.pbobjc.h
│   └── Example.pbobjc.m
└── proto/
```

将全部生成的头文件、实现文件及其相对目录结构加入目标工程，并接入匹配版本的 Objective-C Protobuf 运行库。CocoaPods 示例：

```ruby
pod 'Protobuf', '3.25.9'
# 选择兼容模式时使用：pod 'Protobuf', '3.6.1'
```

应用可复制对应依赖声明，但不替你安装目标工程依赖。无需为了导出文件安装 CocoaPods；应用自带代码生成器。

Objective-C 的类名不会自动继承 Proto 包命名空间。HTML 模式为未声明 `objc_class_prefix` 的文件生成稳定、按文件区分的 `PCT..._` 前缀，已有前缀会保留；普通 Proto 不改写用户声明。若生成的公开类或枚举符号冲突，会停止导出并提示调整前缀。前缀不改变字段的 Protobuf 编号和二进制编码。

参考 [官方 Objective-C 生成代码说明](https://protobuf.dev/reference/objective-c/objective-c-generated/)。

### Java 包名

- **HTML 模式**：默认生成不带 Java 包名的源码，跨文件使用外部类名引用。本地 v16.2 参考文档默认生成 57 个 Proto 和 57 个 Java 文件。
- **普通 Proto 模式**：保留源文件声明及 `java_*` 选项，可在协议中设置 `option java_package`。
- 默认包中的类不能直接供有包名的业务类引用。接入 Android 或其他 Java 工程时需先规划包名；HTML 命令行模式支持 `--java-package`，会按协议文件生成独立 Java 子包。

## 命令行使用

需要 Python 3.9 或更高版本。以下命令均在项目根目录执行；`setup` 自动准备工具链的流程仅支持 macOS。示例中的 `device protocol v16.2.html` 是自行提供的本地私有输入，不随 Git 仓库分发。

```bash
# 准备默认目标的原生 protoc
python3 proto_compile.py setup

# 编译自行提供的本地私有协议文档
python3 proto_compile.py compile "device protocol v16.2.html" -o ./output/current

# 为 protobuf-java 3.6.1 准备工具链并生成
python3 proto_compile.py setup --target-runtime 3.6.1
python3 proto_compile.py compile "device protocol v16.2.html" \
  --target-runtime 3.6.1 -o ./output/legacy

# 导出 Objective-C（默认使用 3.25.9，可同时指定 --target-runtime 3.6.1）
python3 proto_compile.py compile "device protocol v16.2.html" \
  --language objc -o ./output/objective-c

# 编译 Proto 目录，或单文件及其依赖
python3 proto_compile.py compile ./protos -o ./output/protos
python3 proto_compile.py compile ./protos/main.proto \
  -I ./protos -I ./includes -o ./output/single

# 仅提取并规范化 HTML 内的协议，不运行 protoc
python3 proto_compile.py extract "device protocol v16.2.html" -o ./output/extracted-proto

# 为 HTML 生成的 Java 指定根包（按文件创建子包）
python3 proto_compile.py compile "device protocol v16.2.html" \
  --java-package com.example.protocol -o ./output/packaged
```

`compile` 接收一个输入路径；多文件可通过目录或桌面应用导入。省略 `-o` 时，Java 编译结果默认写入 `output/java/`、`output/proto/`；Objective-C 写入 `output/objc/`、`output/proto/`。提取结果默认写入 `output/proto/`，`extract --language objc` 可提取带 Objective-C 前缀的规范化协议。

**输出目录必须不存在或为空。** 重复执行示例前请指定新的输出路径，或自行移走已有结果；工具不会覆盖非空目录。

常用编译选项：

| 参数 | 作用 |
| --- | --- |
| `-o PATH` | 指定输出根目录 |
| `--language java` / `--language objc` | 选择输出语言，默认 Java；compile、extract 均支持 |
| `-I PATH` / `--proto-path PATH` | 添加普通 Proto 的导入根目录，可重复使用 |
| `--target-runtime VERSION` | 选择 `3.25.9` 或 `3.6.1` |
| `--protoc PATH` | 显式指定编译器，版本仍须与目标匹配 |
| `--java-package NAME` | 指定 HTML 生成 Java 的根包 |
| `--no-java-package` | 显式选择 HTML 默认的无 Java 包名行为 |
| `--include-opt-0081` | 尝试包含 HTML 中默认排除的 OPT_0081 |
| `--repairs FILE` | 使用自定义 HTML 修正规则替代内置规则 |
| `--no-repairs` | 禁用已知文档问题修正，仍执行规范化 |

`--java-package` 与 `--no-java-package` 互斥，`--repairs` 与 `--no-repairs` 互斥。Java 包名选项仅用于 HTML 的 Java 输出；修正和排除选项仅用于 HTML；`-I` 仅用于普通 Proto。旧参数 `--exclude-opt OPT_0081` 仍兼容，但 HTML 默认已排除该协议。

运行 `python3 proto_compile.py compile --help` 可查看完整参数。命令行依次查找显式 `--protoc`、项目原生工具链和 PATH；打包应用始终使用内置编译器。

## 输入与处理规则

### TiddlyWiki HTML

- 支持旧版 `div[title] > pre` 和 JSON tiddler store；按 `$(FILE)` 标记提取协议，不执行 HTML 脚本。
- 同名同内容声明去重；内容不同、路径穿越或文件名大小写冲突时明确报错。
- 为协议建立独立的 `device_protocol.*` 命名空间，解析作用域与 import，处理枚举值命名冲突并更新引用。
- 保持字段名、编号、类型、限定符及枚举数值；描述符类型全名可能因包名规范化发生变化，反射和 Any type URL 的使用方需相应核对。
- 支持 message、enum、oneof、map 和 proto2/proto3 字段；遇到 service、extend、group、Java 模式下已有 `java_*` 选项等当前不支持的 HTML 声明时明确报错。

当前 v16.2 示例默认排除 `OPT_0081` 对应的 `his_encrypt_data.proto`，其引用的 `HisEncryptData` 未定义；其他加密子结构仍保留。取消排除会尝试编译并报告该问题。若其他协议依赖被排除文件，也会报告错误。

`config/device-v16.2-repairs.json` 记录已确认的文档修正，包括为 `device_Configure_info.proto` 补充 `yyl_info.proto` import。规则只在提取后的源文本 SHA-256 匹配时生效，不修改原始 HTML；不匹配时跳过该修正并提示。

自定义规则以文件名为键，要求 `sha256`，可包含 `add_imports`、`type_replacements` 和 `reason`。摘要针对提取后原始声明文本的 UTF-8 字节；类型替换只作用于字段类型位置。

对未来文档的兼容取决于其存储结构、标记与协议语法是否仍在支持范围内，不依赖文件名或固定协议数量，也不承诺解析任意 HTML。

### 普通 Proto

递归收集用户协议依赖，按 import 相对路径复制到结果的 `proto/`，并保留原声明和选项。实际语法由所选版本的 protoc 校验；缺失或歧义依赖会报错。`google/protobuf` 标准类型仅用于解析，Java 和 Objective-C 均使用配套运行库中的标准类型实现，不重复导出。

## 从源码构建 macOS 应用

需要 macOS、Python 3、Xcode 及其命令行工具，并允许首次下载构建依赖。

```bash
# 准备构建 Python、固定依赖及两个架构的两套 protoc
python3 scripts/bootstrap.py

# 构建两个架构的应用和 ZIP
python3 scripts/build_app.py

# 或只准备、构建 Apple Silicon 版本
python3 scripts/bootstrap.py --arch arm64
python3 scripts/build_app.py --arch arm64
```

构建依赖固定在 `scripts/build-requirements.txt`，下载文件使用固定摘要校验。protoc 25.9 使用官方架构包，protoc 3.6.1 从源码编译；Python、工具链和构建缓存保存在项目 `.tools/`，不安装到系统目录，也不修改系统 PATH。

`bootstrap.py --target-runtime 3.25.9` 或 `--target-runtime 3.6.1` 可单独准备工具链；构建桌面应用前必须备齐两种目标。`build_app.py --output PATH` 可改变产物位置。

默认构建产物：

```text
dist/
├── arm64/Proto Compile.app
├── x86_64/Proto Compile.app
├── Proto-Compile-1.3.0-arm64.zip
└── Proto-Compile-1.3.0-x86_64.zip
```

应用内采用嵌套 `ConversionEngine.app`，包含 Python 与编译器；第三方许可随包放在 `Contents/Resources/Licenses/`。

### 正式签名与公证

当前构建脚本只执行 **ad-hoc 签名和完整性校验**，尚未接入正式签名、公证流程。`codesign --verify` 通过不代表 Gatekeeper 已批准，也不代表 Apple 公证通过。

正式对外分发需要可用的 Apple Developer Program 团队、Developer ID Application 证书及对应私钥，并完成以下流程：

1. 对主应用、嵌套引擎和内置二进制进行正式签名，按要求启用 Hardened Runtime。
2. 验证签名后的运行行为。
3. 提交 Apple 公证，检查结果及日志。
4. 通过后附加公证票据，重新生成分发 ZIP 并验证。

这不要求上架 Mac App Store。具体要求参见 [Apple Developer ID 分发说明](https://developer.apple.com/developer-id/)。

## 开发与验证

主要目录：

| 路径 | 用途 |
| --- | --- |
| `macos/Sources/` | SwiftUI 界面、应用模型和进程客户端 |
| `macos/Info.plist` | 应用版本、构建号及最低系统声明 |
| `proto_tool/` | 提取、规范化、修正、转换服务及工具链管理 |
| `proto_compile.py` | 命令行入口 |
| `engine_main.py` | 桌面转换引擎入口 |
| `config/` | 带源文本摘要约束的修正规则 |
| `scripts/` | 构建、打包和性能测量脚本 |
| `tests/`、`macos/Tests/` | Python、Java、应用包和 Swift 验证工具 |

CLI 与桌面引擎共用 `proto_tool/service.py`。桌面引擎通过 stdin 接收一个 v1 JSON 请求，支持 `inspect`、`convert`、`version`；stdout 输出带 `task_id` 的 NDJSON 事件、结果或中文错误。`target_runtime` 省略时默认为 `3.25.9`；新增 `output_language` 为 `java` 或 `objc`，省略仍为 Java。结果包含实际语言、编译器和运行库版本，以及 `generated_count`、`java_count`、`objc_count` 文件计数。预检缓存包含语言身份，禁止跨语言复用。

常用验证命令：

```bash
# Python 回归（先准备工具链）
python3 -m unittest discover -s tests -v

# 校验实际生成的 Java；JDK 和运行库 JAR 由验证者提供
python3 tests/validate_java.py \
  --jdk /path/to/jdk17/Contents/Home \
  --runtime-jar /path/to/protobuf-java-3.25.9.jar \
  --generated ./output/current/java --no-java-package

# 应用包检查，以及本机架构上的真实引擎和 Swift 转换链路
python3 tests/verify_bundle.py "dist/arm64/Proto Compile.app" \
  --arch arm64 --html "device protocol v16.2.html"
python3 tests/verify_bundle.py "dist/x86_64/Proto Compile.app" --arch x86_64

# Swift 进程客户端验证
xcrun swiftc -parse-as-library -swift-version 5 \
  -module-cache-path /tmp/proto-swift-cache \
  macos/Sources/EngineClient.swift macos/Tests/EngineClientTests.swift \
  -o /tmp/proto-client-tests
/tmp/proto-client-tests
```

兼容模式的 Java 验证应改用 `protobuf-java-3.6.1.jar` 和对应输出目录。Java 验证覆盖源码编译、跨文件类型、枚举、oneof、预期二进制字节及 proto2 required 约束，不等同于真实设备互通测试。

Objective-C 验证使用 macOS Clang、Foundation 和完整官方运行库源码，不自动下载依赖（cpp-only 源码包不够）：

```bash
python3 tests/validate_objc.py \
  --runtime-source /path/to/protobuf-25.9/objectivec \
  --target-runtime 3.25.9 --generated ./output/objective-c/objc
```

脚本共同导入全部生成头文件并编译链接所有实现，另以合成协议验证跨文件同名类型、枚举、oneof、repeated、精确字节和 required 校验。兼容模式须提供对应 3.6.1 官方运行库源码；验证使用非 ARC、DEBUG 构建，以启用运行库的 required 错误检查。这是 macOS 上的源码验证，不能替代目标 iOS 工程集成验收。

### 已有验证记录与边界

截至 2026-10-26：

- 1.3.0 转换引擎的 98 项 Python 回归测试通过；Java 两种运行库下的文件摘要和诊断与 1.2.1 基线一致。Objective-C 两种运行库下的完整文档产物均通过真实编译链接、跨文件往返和二进制校验。
- 1.3.0 Apple Silicon 应用通过中文空格路径搬移、无效外部 Python 配置及不可用 PATH 下的运行检查；两种语言 × 两种运行库均完成真实 Swift 预检与转换。完整文档每次生成 57 个 Proto，以及 57 个 Java 文件或 57 对 Objective-C 头文件与实现文件。
- 1.3.0 两个架构均完成构建、包内架构、动态库依赖、最低系统声明及深层签名检查；两份 ZIP 解压后的文件内容、权限、符号链接和严格签名检查通过。最终应用生成的文件摘要与已通过源码验证的产物全部一致。
- 折叠标题的文字、箭头、行空白处点击和禁用状态已验证；浅色、深色界面已渲染检查。
- Intel 真机、macOS 13 真机、完整原生面板与键盘操作仍需验收。当前运行验证主机为 Apple Silicon / macOS 27.0.1；隔离环境验证不等同于全新系统验收。

### 性能优化记录

已实现流式提取、解析对象及时释放、选择输出目录不重复预检、会话内校验后复用成果、详情按需创建，以及大任务分批生成。缓存会检查输入、依赖、编译器和产物摘要；分批保留全局语义校验、输出重名检查、取消与原子发布。

下表是 **1.1.0 与 1.2.0 阶段版**的探索性对比，并非当前版本的重新测量。真实可见窗口依次执行启动、闲置、导入预检、选择输出、转换、结果停留和退出；统计主界面及全部子进程。每场景仅一轮，使用同一 12 秒观察窗和 2ms 内存采样。

| 场景 | CPU 时间降幅 | 峰值物理 footprint 降幅 |
| --- | --- | --- |
| 默认模式 · v16.2 HTML | 51.7% | 33.9% |
| 默认模式 · 57 份 Proto 目录 | 27.2% | 16.8% |
| 兼容模式 · v16.2 HTML | 42.6% | 29.5% |
| 兼容模式 · 57 份 Proto 目录 | 33.9% | 20.3% |

四组输出文件摘要一致。CPU 统计 user+system 时间，内存为并发进程物理 footprint 的采样峰值，与 RSS 不同；结果仅适用于所测机器、输入和流程，尚非多轮稳定性结论。全过程 CPU、内存均降低 50% 的目标未达成，性能专项已暂停。

复现工具为 `scripts/benchmark_app.py` 与 `macos/Tests/build_performance_fixtures.py`；比较时须保留优化前的源码和应用副本，并使用相同驱动、输入及窗口条件。

## Git 与私有文件

源码、构建脚本、通用测试及摘要约束的修正规则纳入 Git。协议 HTML/HTM、Proto、生成的 Java 与 `.pbobjc.h` / `.pbobjc.m`、协议选项文件、`private-inputs/`、构建产物及本地签名私钥由 `.gitignore` 排除；`tests/JavaSmoke.java` 是允许跟踪的验证程序。

`device protocol v16.2.html` 仅保留在本地，不随仓库分发。没有该文件时，依赖它的回归用例会明确跳过，通用测试仍可运行；需要完整文档验收时，请通过获授权的渠道获取并放到项目根目录。

添加新输入可先运行 `git check-ignore -v "文件路径"` 核对规则，提交前使用 `git diff --cached --name-only` 检查待提交文件。不要使用 `git add -f` 强行加入私有协议或凭据。

`output/`、`dist/`、`.tools/`、Python 缓存和虚拟环境均不纳入 Git。生成产物应从源码重新构建或单独分发。
