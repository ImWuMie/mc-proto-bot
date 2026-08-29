"""Entry point for the frozen portable build.

PyInstaller cannot run ``protobot/cli_app.py`` directly: as a top-level script
its relative imports ("attempted relative import with no known parent
package") would fail. This tiny script imports the package properly, so the
exe behaves exactly like the ``protobot`` console command.
"""

from protobot.cli_app import main

if __name__ == "__main__":
    raise SystemExit(main())
