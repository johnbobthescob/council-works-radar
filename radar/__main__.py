"""Allow `python -m radar` to run the CLI."""

import sys

from radar.cli import main

if __name__ == "__main__":
    sys.exit(main())
