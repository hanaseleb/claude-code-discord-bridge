"""Regression tests for the dev-worktree import hook installer."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from scripts.install_dev_hook import install_dev_hook


def _write_package(root: Path, name: str, source: str) -> None:
    package = root / name
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(f"SOURCE = {source!r}\n")


def _run_import(site_packages: Path, home: Path, cwd: Path) -> list[str]:
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["HOOK_SITE_PACKAGES"] = str(site_packages)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import os, site;"
                "site.addsitedir(os.environ['HOOK_SITE_PACKAGES']);"
                "import claude_discord, claude_code_core;"
                "print(claude_discord.SOURCE);"
                "print(claude_code_core.SOURCE)"
            ),
        ],
        cwd=cwd,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.splitlines()


def test_hook_redirects_discord_and_core_packages(tmp_path: Path) -> None:
    site_packages = tmp_path / "site-packages"
    base = tmp_path / "base"
    worktree = tmp_path / "worktree"
    home = tmp_path / "home"
    for directory in (site_packages, base, worktree, home):
        directory.mkdir()

    _write_package(base, "claude_discord", "base-discord")
    _write_package(base, "claude_code_core", "base-core")
    _write_package(worktree, "claude_discord", "dev-discord")
    _write_package(worktree, "claude_code_core", "dev-core")
    (home / ".ccdb-dev-worktree").write_text(f"{worktree}\n")
    install_dev_hook(site_packages)

    assert _run_import(site_packages, home, base) == ["dev-discord", "dev-core"]


def test_hook_ignores_partial_worktree_instead_of_mixing_versions(tmp_path: Path) -> None:
    site_packages = tmp_path / "site-packages"
    base = tmp_path / "base"
    worktree = tmp_path / "partial-worktree"
    home = tmp_path / "home"
    for directory in (site_packages, base, worktree, home):
        directory.mkdir()

    _write_package(base, "claude_discord", "base-discord")
    _write_package(base, "claude_code_core", "base-core")
    _write_package(worktree, "claude_discord", "dev-discord")
    (home / ".ccdb-dev-worktree").write_text(f"{worktree}\n")
    install_dev_hook(site_packages)

    assert _run_import(site_packages, home, base) == ["base-discord", "base-core"]
