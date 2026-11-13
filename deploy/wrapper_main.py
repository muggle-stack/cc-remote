"""Start the Wrapper from the immutable release selected by its service."""
from pathlib import Path
import runpy
import sys


def main() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    runpy.run_module('cc_remote.wrapper', run_name='__main__')


if __name__ == '__main__':
    main()
