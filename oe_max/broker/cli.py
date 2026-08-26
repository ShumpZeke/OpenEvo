"""`oe-max-broker` entrypoint."""
from __future__ import annotations
import argparse, os, sys


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="oe-max-broker",
        description="Run the OE-MAX OpenAI-compatible provider broker.")
    ap.add_argument("--host", default=os.environ.get("OE_MAX_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("OE_MAX_PORT", "8787")))
    ap.add_argument("--verify", action="store_true",
                    help="Run live discovery + smoke tests at startup.")
    ap.add_argument("--reload", action="store_true")
    args = ap.parse_args()

    try:
        import uvicorn
    except ImportError:
        print("uvicorn missing. Run ./bootstrap.sh", file=sys.stderr)
        return 1

    os.environ["OE_MAX_VERIFY_ON_START"] = "1" if args.verify else "0"
    print(f"OE-MAX broker → http://{args.host}:{args.port}/v1")
    print(f"  health   http://{args.host}:{args.port}/health")
    print(f"  status   http://{args.host}:{args.port}/v1/oe-max/status")
    uvicorn.run("oe_max.broker.factory:app_factory", factory=True,
                host=args.host, port=args.port, reload=args.reload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
