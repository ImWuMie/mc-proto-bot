"""Right-click-run entry point for PyCharm; same as ``protobot run``."""

from pathlib import Path

from protobot.cli_app import main

if __name__ == "__main__":
    # Name the config file explicitly so PyCharm finds config.yaml whatever
    # working directory it starts in
    raise SystemExit(
        main(["run", "--config", str(Path(__file__).with_name("config.yaml"))])
    )
