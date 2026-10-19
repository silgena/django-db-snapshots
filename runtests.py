#!/usr/bin/env python
"""Run the test suite: python runtests.py [test labels]"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tests.settings")

import django  # noqa: E402
from django.conf import settings  # noqa: E402
from django.test.utils import get_runner  # noqa: E402

if __name__ == "__main__":
    django.setup()
    failures = get_runner(settings)(verbosity=1).run_tests(sys.argv[1:] or ["tests"])
    sys.exit(bool(failures))
