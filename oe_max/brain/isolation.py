"""
Workspace isolation — candidates never evaluate in the user's active tree.

Uses:
  - git worktree when available (fast, shares object store)
  - temp clone fallback
  - ephemeral patch workspace for pure patch validation

A candidate must not silently alter the user's real project.
Promotion is an explicit operation.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Generator, Optional


def _run(cmd: list, cwd: Optional[Path] = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True)


def is_git_repo(path: Path) -> bool:
    return (path / ".git").exists() or _run(["git", "rev-parse", "--git-dir"], cwd=path).returncode == 0


@contextmanager
def isolated_worktree(
    repo_root: Path, *, prefix: str = "openevo-candidate-"
) -> Generator[Path, None, None]:
    """
    Create an isolated worktree for candidate evaluation.

    - Tries `git worktree add` (fast)
    - Falls back to temp dir + copy if git worktree unavailable
    - Always cleans up, even on failure
    - Caller must apply the candidate patch inside the worktree; the worktree
      is discarded after the context exits unless explicitly promoted.
    """
    repo_root = Path(repo_root).resolve()
    # Prefer git worktree
    worktree_dir: Optional[Path] = None
    added_via_git = False
    tmp_parent = Path(tempfile.gettempdir())

    try:
        if is_git_repo(repo_root):
            # Create a temp dir for the worktree
            worktree_dir = Path(tempfile.mkdtemp(prefix=prefix, dir=str(tmp_parent)))
            # Remove the empty dir so git worktree can create it
            worktree_dir.rmdir()
            # Create worktree on detached HEAD at current SHA (no branch pollution)
            base_sha = _run(["git", "rev-parse", "HEAD"], cwd=repo_root).stdout.strip() or "HEAD"
            res = _run(["git", "worktree", "add", "--detach", str(worktree_dir), base_sha], cwd=repo_root)
            if res.returncode == 0 and worktree_dir.exists():
                added_via_git = True
            else:
                # Fallback
                worktree_dir = None

        if worktree_dir is None or not worktree_dir.exists():
            # Fallback: temp copy (shallow where possible)
            worktree_dir = Path(tempfile.mkdtemp(prefix=prefix))
            # Copy non-gitignored files minimally; for correctness we copy everything except .git
            for item in repo_root.iterdir():
                if item.name == ".git":
                    continue
                dest = worktree_dir / item.name
                try:
                    if item.is_dir():
                        shutil.copytree(item, dest, symlinks=True)
                    else:
                        shutil.copy2(item, dest)
                except Exception:
                    pass
            # Ensure it's a valid working dir
            (worktree_dir / ".openevo-isolated").write_text("isolated workspace\n", encoding="utf-8")

        yield worktree_dir
    finally:
        if worktree_dir and worktree_dir.exists():
            try:
                if added_via_git:
                    _run(["git", "worktree", "remove", "--force", str(worktree_dir)], cwd=repo_root)
                    # git worktree remove may leave the dir; ensure removal
                shutil.rmtree(worktree_dir, ignore_errors=True)
            except Exception:
                pass


def apply_patch(worktree: Path, patch: str) -> bool:
    """
    Apply a unified diff patch inside the worktree.
    Returns True if patch applied cleanly (exit 0).
    Uses `git apply` when available, else `patch`.
    """
    worktree = Path(worktree)
    # Try git apply first
    try:
        proc = subprocess.run(
            ["git", "apply", "--whitespace=nowarn", "-"],
            input=patch,
            text=True,
            cwd=str(worktree),
            capture_output=True,
        )
        if proc.returncode == 0:
            return True
    except Exception:
        pass
    # Fallback to patch
    try:
        proc = subprocess.run(
            ["patch", "-p1", "--forward"],
            input=patch,
            text=True,
            cwd=str(worktree),
            capture_output=True,
        )
        return proc.returncode == 0
    except FileNotFoundError:
        # No patch tool — write patch to file and try to apply naively (for tests)
        return False


def promote_worktree_to_repo(worktree: Path, repo_root: Path) -> None:
    """
    Explicit promotion — copy worktree contents back to the real repo.
    This is the ONLY way a candidate alters the user's tree.
    Never called automatically during evaluation.
    """
    worktree = Path(worktree).resolve()
    repo_root = Path(repo_root).resolve()
    if worktree == repo_root:
        raise ValueError("worktree and repo_root are the same — refusing to promote onto itself")
    for item in worktree.iterdir():
        if item.name in (".git", ".openevo-isolated"):
            continue
        dest = repo_root / item.name
        try:
            if dest.exists():
                if dest.is_dir():
                    shutil.rmtree(dest)
                else:
                    dest.unlink()
            if item.is_dir():
                shutil.copytree(item, dest, symlinks=True)
            else:
                shutil.copy2(item, dest)
        except Exception as e:
            raise RuntimeError(f"promotion failed for {item.name}: {e}") from e
