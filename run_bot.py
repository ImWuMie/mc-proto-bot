"""PyCharm 右键运行入口：等价于 ``protobot run``。"""

from pathlib import Path

from protobot.cli_app import main

if __name__ == "__main__":
    # 显式指定配置文件，保证在 PyCharm 里任意工作目录下都能找到 config.yaml
    raise SystemExit(
        main(["run", "--config", str(Path(__file__).with_name("config.yaml"))])
    )
