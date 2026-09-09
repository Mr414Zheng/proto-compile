# Proto 转 Java

Python 3.9+ 命令行工具，使用 protoc 3.11.0 生成标准 Protobuf Java 源码。支持 TiddlyWiki HTML、单个 `.proto` 和 proto 目录，Python 部分仅使用标准库。

## 默认使用

在项目目录执行：

```bash
python3 proto_compile.py setup
python3 proto_compile.py compile "device protocol v16.2.html"
```

只生成两个目录：

```text
output/
  java/     57 个无包名的 .java 文件，例如 DeviceConf.java
  proto/    57 个用于生成上述 Java 的 .proto 文件
```

默认排除 `OPT_0081` 对应的 `his_encrypt_data.proto`。Java 不声明 `package`，不含 `device.protocol.*` 类型引用，跨文件使用 `RealtimeData.RtTime` 等类名。不生成 `.options`、报告 JSON、描述符 `.pb`、原始副本或其他输出目录。诊断和修正信息直接显示在终端。

生成结果先在临时目录完成，成功后才发布。输出根目录必须不存在或为空；已有输出请先移走再运行，工具不会覆盖。`-o` 可指定其他输出根目录，但内部始终只有 `java/` 和 `proto/`。

## 其他输入

```bash
# 仅准备可编译的 proto，无需安装 protoc；默认写入 output/proto
python3 proto_compile.py extract "device protocol v16.2.html"

# 编译已有 proto 目录，输出仍为 output/java 和 output/proto
python3 proto_compile.py compile ./protos

# 单个 proto；可重复指定依赖目录
python3 proto_compile.py compile ./protos/main.proto -I ./protos -I ./includes

# 显式选择编译器
python3 proto_compile.py compile ./protos --protoc /path/to/protoc
```

普通 proto 模式保留源文件声明及 Java 选项，输入文件按 import 相对路径复制到 `proto/`。单文件模式只生成并复制指定的输入，外部 import 依赖仍从 `-I` 查找；完整项目通常应使用目录模式。`extract` 输出经过与 HTML 编译相同的过滤、修正和规范化，只有 `.proto` 文件。

`--no-java-package` 和 `--exclude-opt OPT_0081` 保留为兼容参数，这两个行为现在都是 HTML 默认值。若显式传入 `--java-package com.example.device`，则按文件生成独立 Java 子包；该参数与 `--no-java-package` 互斥。

## 工具链

`setup` 下载并校验官方 macOS Intel 安装包，仅写入项目 `.tools/protoc-3.11.0`，不修改系统 PATH。Apple Silicon 需要 Rosetta，本机已验证可运行。旧版官方发布页未提供摘要，安装使用项目固定的下载包 SHA-256 校验值。普通编译不会联网。

编译器选择顺序为 `--protoc`、项目安装版本、PATH。生成源码不需要 JDK；编译和运行源码需要配套的 `protobuf-java:3.11.0`。这批代码使用完整运行库，不是 Lite 代码。

Gradle：

```groovy
implementation 'com.google.protobuf:protobuf-java:3.11.0'
```

Maven：

```xml
<dependency>
  <groupId>com.google.protobuf</groupId>
  <artifactId>protobuf-java</artifactId>
  <version>3.11.0</version>
</dependency>
```

放入 Android 项目时，为相关 Java 文件统一添加原项目的 `package` 声明。默认包中的类不能直接供有包名的业务类引用；如果协议文件分布在不同包，还需要对应 import。

## 文档处理规则

- 读取旧版 TiddlyWiki `div[title] > pre` 和 JSON tiddler store，不执行文档脚本。
- 按 `$(FILE)` 标记提取声明，同名同内容去重，内容不同时报错；拒绝路径穿越及文件名大小写冲突。
- 当前文档有 59 个声明块、58 个唯一 proto，过滤 `OPT_0081` 后为 57 个。文件名包含空格的 `his_encrypt _medic_data.proto` 也会保留。
- proto 中保留独立的 `device_protocol.*` 命名空间，以隔离同名协议；通过 `option java_package = ""` 让 Java 不带包名。字段名、编号、类型、限定符和枚举数值保持不变。
- 类型按作用域及 import 解析；冲突枚举值添加枚举名前缀并更新默认值。缺失或歧义引用明确报错，不猜测。
- 包名变化会改变描述符类型全名；使用反射或 Any type URL 的调用方需要相应调整。

`config/device-v16.2-repairs.json` 保存已确认的文档修正：为 `device_Configure_info.proto` 补充 `yyl_info.proto` import。仅在源内容 SHA-256 匹配时应用，原始 HTML 不变。`his_encrypt_data.proto` 引用的 `HisEncryptData` 未定义，按用户要求排除；其他加密子结构保留，若存在依赖被排除文件的协议则报错。

HTML 编译可用 `--no-repairs` 禁用修正，或 `--repairs rules.json` 替代内置规则。自定义规则需要 `sha256`，以及可选的 `add_imports`、`type_replacements`、`reason`；SHA-256 对提取后原始声明文本的 UTF-8 字节计算。类型修正仅替换字段类型位置，不替换注释和字符串。

HTML 规范化支持 message、enum、oneof、map 和 proto2/proto3 字段；对 service、extend、group、已有 `java_*` 选项等不支持的声明明确报错。普通 proto 编译直接交给 protoc，保留完整语法能力。`-I` 仅用于普通模式。

## 验证与仓库

```bash
python3 -m unittest discover -s tests -v
python3 tests/validate_java.py \
  --jdk /path/to/jdk17/Contents/Home \
  --runtime-jar /path/to/protobuf-java-3.11.0.jar \
  --generated output/java --no-java-package
```

Java 验证编译全部生成源码，并验证跨文件类型、枚举、oneof 切换、预期二进制字节和 proto2 required 约束；不等同于真实设备互通测试。

主分支为 `main`，源码、测试、修正规则及原始 HTML 纳入 Git；`output/`、`.tools/`、缓存和虚拟环境不纳入版本控制。
