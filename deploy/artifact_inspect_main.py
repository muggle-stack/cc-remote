"""Start the local artifact MCP server from the owning Wrapper release."""

from pathlib import Path
import sys


def main() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from cc_remote.wrapper.artifact_inspection import main as serve

    serve()


if __name__ == "__main__":
    main()
