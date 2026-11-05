#!/usr/bin/env python3
"""CodeMonkey: the app's server and page.

Original clean-room implementation (no frameworks, stdlib only).

Run:
    python3 webui.py [--project DIR] [--port 8080] [--config config.yaml]

Then open the printed URL in a browser. The desktop app (desktop/) starts it the same way.
"""

import argparse
import os
import sys

if sys.version_info < (3, 10):
    sys.exit("CodeMonkey needs Python 3.10 or newer; this is %d.%d." % sys.version_info[:2])

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from webui.app import WebApp  # noqa: E402
from webui.server import serve  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Web UI for the agent framework.")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument(
        "--project",
        default=None,
        help="Default project directory for new sessions.",
    )
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument(
        "--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1)."
    )
    args = parser.parse_args()

    app = WebApp(args.config, args.project)
    print(f"Default project: {app.default_project}")
    print(f"Providers: {', '.join(app.config_snapshot()['providers'])}")
    serve(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
