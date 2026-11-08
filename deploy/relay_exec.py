"""Start the Relay from this immutable release, independent of module lookup."""
from pathlib import Path
import runpy
import sys


def main() -> None:
    # The service selects this release's interpreter with -I. Only this known
    # source tree is added; ambient PYTHONPATH and cwd cannot choose the module.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    runpy.run_module('cc_remote.relay', run_name='__main__')


if __name__ == '__main__':
    main()
