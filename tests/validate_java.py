"""使用已有 JDK 17 和 protobuf-java 验证实际生成结果，不自动下载依赖。"""
import argparse
import subprocess
import tempfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--jdk', required=True, type=Path, help='JDK 17 Home 目录')
    parser.add_argument('--runtime-jar', required=True, type=Path)
    parser.add_argument('--generated', type=Path, default=Path('output/java'))
    parser.add_argument('--no-java-package', action='store_true', help='验证无 Java 包名的生成文件')
    args = parser.parse_args()
    javac = args.jdk.resolve() / 'bin/javac'
    java = args.jdk.resolve() / 'bin/java'
    version = subprocess.run([str(javac), '-version'], check=True, capture_output=True, text=True).stdout.strip()
    if not version.startswith('javac 17.'):
        parser.error(f'验证要求 JDK 17，实际 {version}')
    generated = sorted(args.generated.resolve().rglob('*.java'))
    if not generated:
        parser.error('未找到生成的 Java 文件')
    with tempfile.TemporaryDirectory(prefix='proto-java-validation-') as temp:
        temp = Path(temp)
        classes = temp / 'classes'
        classes.mkdir()
        smoke = Path(__file__).resolve().with_name('JavaSmoke.java')
        if args.no_java_package:
            # 测试逻辑不变，仅让测试类引用默认包中的协议类。
            content = '\n'.join(line for line in smoke.read_text().splitlines()
                                if not line.startswith('import device.protocol.')) + '\n'
            smoke = temp / 'JavaSmoke.java'
            smoke.write_text(content, encoding='utf-8')
        sources = generated + [smoke]
        argfile = temp / 'sources.txt'
        argfile.write_text('\n'.join('"' + str(p).replace('\\', '\\\\').replace('"', '\\"') + '"' for p in sources), encoding='utf-8')
        subprocess.run([str(javac), '-encoding', 'UTF-8', '-cp', str(args.runtime_jar.resolve()), '-d', str(classes), '@' + str(argfile)], check=True)
        subprocess.run([str(java), '-cp', str(classes) + ':' + str(args.runtime_jar.resolve()), 'JavaSmoke'], check=True)
        print(f'PASS: {len(generated)} generated Java files compiled with {version}')


if __name__ == '__main__':
    main()
