#!/usr/bin/env python3
"""campaign: the command. Its code is the campaign_tool package, imported so Python keeps its
compiled bytecode instead of compiling 10,000 lines on every run."""

import sys

from campaign_tool import main

if __name__ == "__main__":
    sys.exit(main())
