#!/usr/bin/env python3
"""Compatibility wrapper pointing to 1st.py."""

import importlib
import sys

# Import 1st.py dynamic module
_app = importlib.import_module("1st")

# Mirror all attributes from 1st into current module so mock.patch('rec2_app.xxx') works
for _attr, _val in _app.__dict__.items():
    if not _attr.startswith("__"):
        globals()[_attr] = _val

if __name__ == "__main__":
    sys.exit(main())
