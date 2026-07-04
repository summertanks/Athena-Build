#!/usr/bin/env python3
"""Athena Linux build-system test suite — aggregator.

The tests live in per-subsystem parts (tests/test_<part>.py); this file
collects every part's TESTS list and runs them all:
    python3 tests/test_module.py
Exits 0 on success, 1 on any failure.  A single part can also be run
directly (python3 tests/test_<part>.py).

Add new tests to the matching part file and register them in that
file's TESTS list — the registration guard (test_policy_pins) enforces
per-file registration and cross-file name uniqueness.

No real Docker, no real sudo, no real network — anything that touches
those is mocked at the boundary.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import test_versioning
import test_cache_snapshot
import test_dependency_resolution
import test_selection_surfaces
import test_chroot_build
import test_source_build
import test_installer
import test_images
import test_federation_coord
import test_mirror_publish
import test_remote_build
import test_repo_audit
import test_session_commands
import test_tui_webapi
import test_config_utils
import test_policy_pins

from _test_helpers import run_tests

TESTS = [
    *test_versioning.TESTS,
    *test_cache_snapshot.TESTS,
    *test_dependency_resolution.TESTS,
    *test_selection_surfaces.TESTS,
    *test_chroot_build.TESTS,
    *test_source_build.TESTS,
    *test_installer.TESTS,
    *test_images.TESTS,
    *test_federation_coord.TESTS,
    *test_mirror_publish.TESTS,
    *test_remote_build.TESTS,
    *test_repo_audit.TESTS,
    *test_session_commands.TESTS,
    *test_tui_webapi.TESTS,
    *test_config_utils.TESTS,
    *test_policy_pins.TESTS,
]


def main() -> int:
    return run_tests(TESTS)


if __name__ == '__main__':
    sys.exit(main())
