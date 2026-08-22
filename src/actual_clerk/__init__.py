"""Actual Clerk: a budgeting assistant sidecar for Actual Budget."""

import os
from importlib.metadata import PackageNotFoundError, version


def _installed_version() -> str:
    try:
        return version("actual-clerk")
    except PackageNotFoundError:
        return "0.1.0"


def _runtime_version() -> str:
    return os.environ.get("ACTUAL_CLERK_VERSION") or _installed_version()


__version__ = _runtime_version()
