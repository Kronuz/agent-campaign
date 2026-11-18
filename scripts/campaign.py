#!/usr/bin/env python3
"""campaign: the command. Its code is campaign_tool.py, imported so Python keeps its compiled
bytecode instead of compiling 8,000 lines on every run."""

import sys

from campaign_tool import main

if __name__ == "__main__":
    sys.exit(main())
