#!/usr/bin/env python3
"""Publish the ``minimax/`` folder to a branch of the participant's fork.

    python scripts/publish.py --repo https://github.com/callmeapiao/powercontext.git \
        --branch 2026-competition-ecosystem

The script clones the fork, creates the target branch from ``2026-competition``,
copies the deliverable in, commits and pushes.  It never copies runtime state
(``.runtime``) or caches.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IGNORED = shutil.ignore_patterns(".runtime", "__pycache__", "*.pyc", ".pytest_cache", ".venv")


def git(*args: str, cwd: Path | None = None, check: bool = True) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=1800,
    )
    if check and completed.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed:\n{completed.stdout}\n{completed.stderr}")
    return (completed.stdout or "") + (completed.stderr or "")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default="https://github.com/callmeapiao/powercontext.git")
    parser.add_argument("--branch", default="2026-competition-ecosystem")
    parser.add_argument("--base", default="2026-competition")
    parser.add_argument("--source", default=str(ROOT))
    parser.add_argument("--target-dir", default="minimax")
    parser.add_argument("--message", default="feat(minimax): SDD context module for the MiniMax track")
    parser.add_argument("--workdir", default=str(ROOT.parent / "publish_work"))
    args = parser.parse_args()

    source = Path(args.source).resolve()
    workdir = Path(args.workdir).resolve()
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)

    print(f"cloning {args.repo} ...")
    git("clone", "--branch", args.base, "--single-branch", "--depth", "1", args.repo, str(workdir))
    git("checkout", "-b", args.branch, cwd=workdir)

    destination = workdir / args.target_dir
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination, ignore=IGNORED)
    print(f"copied {source} -> {destination}")

    git("add", args.target_dir, cwd=workdir)
    status = git("status", "--porcelain", cwd=workdir)
    if not status.strip():
        print("nothing to commit")
        return 0
    git("commit", "-m", args.message, cwd=workdir)
    git("push", "origin", args.branch, cwd=workdir)

    sha = git("rev-parse", "HEAD", cwd=workdir).strip()
    print(f"\nbranch: {args.branch}")
    print(f"commit: {sha}")
    print(f"files: {len(status.splitlines())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
