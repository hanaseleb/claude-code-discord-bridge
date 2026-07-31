#!/usr/bin/env python3
"""Install the ccdb dev-worktree import hook into the active Python environment."""

from __future__ import annotations

import sysconfig
from pathlib import Path

HOOK_SOURCE = '''"""Redirect ccdb packages to the worktree named by ~/.ccdb-dev-worktree."""
import importlib.util
import os
import sys

_TARGETS = ("claude_discord", "claude_code_core")


def _install():
    dev_file = os.path.expanduser("~/.ccdb-dev-worktree")
    if not os.path.isfile(dev_file):
        return
    with open(dev_file, encoding="utf-8") as handle:
        worktree = handle.read().strip()
    if not all(os.path.isdir(os.path.join(worktree, target)) for target in _TARGETS):
        return

    class _Finder:
        def find_spec(self, fullname, path, target=None):
            root = fullname.split(".", 1)[0]
            if root not in _TARGETS:
                return None
            package_path = os.path.join(worktree, *fullname.split("."))
            if os.path.isdir(package_path):
                init = os.path.join(package_path, "__init__.py")
                if os.path.isfile(init):
                    return importlib.util.spec_from_file_location(
                        fullname,
                        init,
                        submodule_search_locations=[package_path],
                    )
            module_path = package_path + ".py"
            if os.path.isfile(module_path):
                return importlib.util.spec_from_file_location(fullname, module_path)
            return None

    sys.meta_path.insert(0, _Finder())


_install()
'''


def install_dev_hook(site_packages: Path) -> None:
    """Write or refresh the hook and its ``.pth`` activator."""
    site_packages.mkdir(parents=True, exist_ok=True)
    (site_packages / "_ccdb_dev_hook.py").write_text(HOOK_SOURCE, encoding="utf-8")
    (site_packages / "_ccdb_dev_hook.pth").write_text(
        "import _ccdb_dev_hook\n",
        encoding="utf-8",
    )


def main() -> None:
    install_dev_hook(Path(sysconfig.get_paths()["purelib"]))


if __name__ == "__main__":
    main()
