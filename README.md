# Proto 转 Java

Python 3.9+ 命令行工具，使用官方 `protoc` 生成标准 Protobuf Java 源码。支持 TiddlyWiki HTML 协议文档、单个 `.proto` 和 proto 目录；Python 部分仅使用标准库。

## 仓库管理

主分支为 `main`。源码、测试、修正规则和原始协议 HTML 纳入 Git；`output/` 生成产物、`.tools/` 下载工具链、Python 缓存及本地虚拟环境不纳入版本控制。克隆后执行下方 `setup` 和编译命令即可重新生成 Java 文件。

## 快速使用

在项目目录执行：

```bash
# macOS Apple Silicon / Intel：下载并校验固定版本 protoc 33.5
python3 proto_compile.py setup

# 单独提取原始声明，不需要 protoc
python3 proto_compile.py extract "device protocol v16.2.html" -o output/proto

# 从 HTML 提取、规范化并生成 Java
python3 proto_compile.py compile "device protocol v16.2.html" -o output/device --exclude-opt OPT_0081

# 编译已有 proto 目录（递归查找 *.proto）
python3 proto_compile.py compile ./protos -o output/custom

# 编译单文件；可重复指定依赖搜索目录
python3 proto_compile.py compile ./protos/main.proto -I ./protos -I ./includes -o output/single
```

输入或输出路径含空格时加引号。输出目录必须不存在或为空；再次运行请使用新目录，或自行移走上一次产物。工具不覆盖已有文件。

`setup` 仅写入项目 `.tools/protoc-33.5`，不修改系统 PATH。重复执行会检查已有版本。下载使用官方发布的 SHA-256 校验值；校验失败不会安装。普通编译不会自动联网。

编译器选择顺序为 `--protoc`、项目 `.tools`、PATH。其他系统可自行安装 protoc 并指定：

```bash
python3 proto_compile.py compile ./protos -o output/custom --protoc /path/to/protoc
```

生成源码不需要 Java 环境；编译、运行这些源码需要 JDK 和 Protobuf Java 运行库。单文件模式只为指定输入生成 Java，import 依赖需要另外生成；完整项目通常应使用目录模式。

## HTML 输出与命名

```text
output/device/
  original/       原始提取的 .proto 和 .options
  normalized/     加入命名空间、修正类型引用的 .proto 副本
  java/           生成的 Java 源码
  schema.pb       包含 import 的 FileDescriptorSet
  report.json     文件来源、源内容校验值、命名映射、修正记录和编译警告
```

本工具读取旧版 TiddlyWiki 的 `div[title] > pre` 数据和 JSON tiddler store，不执行 HTML 内的脚本。只提取 `// $(FILE)文件名` 声明块；同名同内容的文件去重，内容不同时报错。文件名保留大小写和空格，拒绝路径穿越与文件名大小写冲突。

当前文档包含 **59 个 proto 声明块、58 个唯一 proto 文件**：`phone_book.proto` 在草稿和正文中重复；`his_encrypt _medic_data.proto` 文件名中确实有空格，也会保留。计划阶段的 57 个计数漏掉了后者。

HTML 模式为每个文件设置独立的 proto package（`device_protocol.<文件名规范化>`）和 Java 子包（`device.protocol.<文件名规范化>`）。文件名转小写、路径分隔符与特殊字符转下划线；冲突时报错。使用以下参数更改 Java 根包：

```bash
python3 proto_compile.py compile "device protocol v16.2.html" -o output/custom-package --java-package com.example.device --exclude-opt OPT_0081
```

消息保留在 protoc 默认外层类内，例如 `device.protocol.realtime_data.RealtimeData.RtTime`。外层类与消息同名时，protoc 自动添加 `OuterClass`，例如 `device.protocol.weather.WeatherOuterClass.Weather`。

规范化保留字段名、编号、类型、required/optional/repeated 和枚举数值；仅为冲突的枚举值添加枚举名前缀，同步更新默认值。类型引用按本地作用域、直接 import 和 public import 解析，歧义或缺失引用会报错。包名变化会改变描述符类型全名，使用反射、类型注册或 Any type URL 的调用方需要同步修改。`.options` 是原协议附带的 nanopb 配置，仅保留在 original，不参与 Java 编译。

HTML 规范化适用于文档中的 message、enum、oneof、map、proto2/proto3 字段声明。不支持 service、extend、group、已有 `java_*` 选项等声明时会明确报错；完整 Protobuf 语言请使用普通 proto 编译模式。普通模式不修改原始协议及 Java 选项，语法支持取决于所选 protoc。`-I` 仅用于普通模式。

## 文档问题与可审查修正

`config/device-v16.2-repairs.json` 保存针对当前文档的修正规则。规则只在提取内容 SHA-256 完全匹配时应用，原始 HTML 和 original 文件保持不变，所有应用的修正记录到 `report.json`。

- `device_Configure_info.proto` 引用的 `yyl_report_type`、`yyl_body_report` 仅在 `yyl_info.proto` 定义；生成副本补充此 import。
- `his_encrypt_data.proto` 引用了不存在的 `HisEncryptData`。按用户要求，使用 `--exclude-opt OPT_0081` 排除该文件，生成其余 57 个协议的 Java。其他 `OPT_NONE` 加密数据结构保留；不推测缺失类型。

`--exclude-opt` 目前仅支持已确认的 `OPT_0081 → his_encrypt_data.proto` 映射，限 HTML 模式。排除发生在类型解析和编译之前，记录到报告的 `excluded` 字段；`original` 保留全部 58 个原始 proto，`normalized` 和 `java` 中不包含被排除文件。若其他文件依赖被排除协议，将明确报错，不递归删除依赖方。不传该参数时仍尝试编译全部协议。

可用 `--no-repairs` 禁用内置修正，也可使用 `--repairs rules.json` 替代内置规则。自定义规则中的文件必须存在且源内容校验匹配，否则失败；提取报告中的 `sha256` 可用于编写规则。

```json
{
  "example.proto": {
    "sha256": "提取后文件的完整 SHA-256",
    "add_imports": ["dependency.proto"],
    "type_replacements": {"OldType": "CorrectType"},
    "reason": "经协议维护者确认的修正原因"
  }
}
```

类型修正只替换字段类型的词法位置，不替换注释、字符串和字段名。未知或有歧义的类型不能通过自动推测处理。

## 在 Java 项目使用

将生成的 `java/` 目录作为 Java 源码目录，或复制其内容到 `src/main/java`。固定 protoc 33.5 对应本项目验证使用的 `protobuf-java:4.33.5`。

Maven：

```xml
<dependency>
  <groupId>com.google.protobuf</groupId>
  <artifactId>protobuf-java</artifactId>
  <version>4.33.5</version>
</dependency>
```

Gradle：

```groovy
implementation 'com.google.protobuf:protobuf-java:4.33.5'
```

```java
import device.protocol.realtime_data.RealtimeData;

var time = RealtimeData.RtTime.newBuilder().setSeconds(123).build();
byte[] payload = time.toByteArray();
var decoded = RealtimeData.RtTime.parseFrom(payload);
```

自定义 protoc 版本时，请使用对应的 Java 运行库。参考 [Java 代码生成说明](https://protobuf.dev/reference/java/java-generated/) 和 [跨版本兼容说明](https://protobuf.dev/support/cross-version-runtime-guarantee/)。

## 验证

```bash
python3 -m unittest discover -s tests -v

# JDK Home 必须是 JDK 17；验证脚本不会下载依赖
python3 tests/validate_java.py \
  --jdk /path/to/jdk17/Contents/Home \
  --runtime-jar /path/to/protobuf-java-4.33.5.jar \
  --generated output/device/java
```

Python 测试覆盖文档提取、重复/非法文件名、命名空间、作用域、枚举默认值、导入路径及失败时取消发布。未安装项目 protoc 时会明确跳过真实编译测试。

Java 验证脚本编译全部输入源码，并针对当前文档生成的默认包验证跨文件类型、枚举、oneof 切换、固定预期二进制字节以及 proto2 required 约束。此验证不等同于真实设备互通测试。
