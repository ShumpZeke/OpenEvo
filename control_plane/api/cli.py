"""`evolution-server` entrypoint."""

from __future__ import annotations

import argparse
import os
import sys


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="evolution-server",
        description="Run the Evolution control plane (API + Control Center).",
    )
    parser.add_argument("--host", default=os.environ.get("EVOLUTION_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("EVOLUTION_PORT", "8000")))
    parser.add_argument("--workspace", default=os.environ.get("EVOLUTION_WORKSPACE"))
    parser.add_argument("--reload", action="store_true",
                        help="Reload on source changes (development only).")
    args = parser.parse_args()

    if args.workspace:
        os.environ["EVOLUTION_WORKSPACE"] = os.path.abspath(args.workspace)

    try:
        import uvicorn
    except ImportError:
        print("uvicorn is not installed. Run ./bootstrap.sh (or pip install -e .).",
              file=sys.stderr)
        return 1

    print(f"Evolution Control Center → http://{args.host}:{args.port}")
    uvicorn.run(
        "control_plane.api.app:create_app",
        factory=True, host=args.host, port=args.port, reload=args.reload,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
