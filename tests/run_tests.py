"""Tiny test runner (pytest is not installed in the venv).

    venv/Scripts/python tests/run_tests.py [module ...]

Modules that are not present yet (other packages still landing) are skipped.
"""
import importlib
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

MODULES = ["test_tree", "test_traversal", "test_builder", "test_review"]
selected = sys.argv[1:] or MODULES

failed = 0
ran = 0
for module_name in selected:
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name == module_name:
            print("SKIP", module_name, "(not present)")
            continue
        failed += 1
        print("FAIL", module_name, "(import error)")
        traceback.print_exc()
        continue
    for name in sorted(dir(module)):
        if name.startswith("test_"):
            ran += 1
            try:
                getattr(module, name)()
                print("PASS", f"{module_name}.{name}")
            except Exception:
                failed += 1
                print("FAIL", f"{module_name}.{name}")
                traceback.print_exc()
print(f"{ran - failed} passed, {failed} failed")
sys.exit(1 if failed else 0)
