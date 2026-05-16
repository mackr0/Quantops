"""pytest config for edgar_form4 — adds the package's parent
directory to sys.path so `import edgar_form4` works in tests."""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
