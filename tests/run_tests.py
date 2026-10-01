"""Tiny test runner (pytest is not installed in the venv).

    venv/Scripts/python tests/run_tests.py
"""
import importlib
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

failed = 0
for module_name in ["test_fast"]:
    module = importlib.import_module(module_name)
    for name in sorted(dir(module)):
        if name.startswith("test_"):
            try:
                getattr(module, name)()
                print("PASS", name)
            except Exception:
                failed += 1
                print("FAIL", name)
                traceback.print_exc()
sys.exit(1 if failed else 0)
