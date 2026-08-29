"""Managed-files policy for the dashboard file browser: root resolution, path containment, entry metadata.
"""

import mimetypes
import os
import re
import subprocess
import urllib.request
from dataclasses import dataclass
from fastapi import HTTPException, Request
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


_MANAGED_FILES_ROOT_ENV = "HERMES_DASHBOARD_FILES_ROOT"
_HOSTED_MANAGED_FILES_ROOT = Path("/opt/data")


@dataclass(frozen=True)
class ManagedFilesPolicy:
    default_path: Path
    locked_root: Path | None
    can_change_path: bool


def _fs_path(raw_path: str) -> Path:
    raw = str(raw_path or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="Path is required")
    if "\0" in raw:
        raise HTTPException(status_code=400, detail="Invalid path")
    try:
        if raw.lower().startswith("file:"):
            parsed = urllib.parse.urlparse(raw)
            if parsed.netloc and parsed.netloc not in {"", "localhost"}:
                raise ValueError
            raw = urllib.request.url2pathname(parsed.path)
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        return candidate.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid path")


def _canonical_path(path: Path, *, require_exists: bool = False) -> Path:
    try:
        return path.expanduser().resolve(strict=require_exists)
    except FileNotFoundError:
        if require_exists:
            raise HTTPException(status_code=404, detail="Path not found")
        raise
    except (OSError, RuntimeError):
        raise HTTPException(status_code=400, detail="Invalid path")


def _ensure_managed_root(raw_path: str | Path) -> Path:
    root = Path(raw_path).expanduser()
    try:
        root.mkdir(parents=True, exist_ok=True)
        resolved = root.resolve()
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=500, detail=f"Managed files root is unavailable: {exc}")
    if not resolved.is_dir():
        raise HTTPException(status_code=500, detail="Managed files root is not a directory")
    return resolved


def _path_is_under(root: Path, target: Path) -> bool:
    return target == root or root in target.parents


def _path_text(raw_path: str | None) -> str:
    text = str(raw_path or "").strip()
    if "\x00" in text:
        raise HTTPException(status_code=400, detail="Invalid path")
    return text


def _default_hermes_root_is_opt_data() -> bool:
    raw = os.environ.get("HERMES_HOME", "").strip()
    if not raw:
        return False
    try:
        from hermes_constants import get_default_hermes_root

        root = get_default_hermes_root().expanduser().resolve(strict=False)
    except (OSError, RuntimeError):
        root = Path(raw).expanduser().resolve(strict=False)
    return root == _HOSTED_MANAGED_FILES_ROOT


def _dashboard_updates_config() -> Dict[str, Any]:
    """Return the user-facing Dashboard update settings, best-effort."""
    try:
        from hermes_cli.config import load_config

        updates = (load_config() or {}).get("updates", {})
        return updates if isinstance(updates, dict) else {}
    except Exception:
        return {}


def _dashboard_update_disabled_by_config() -> bool:
    return not bool(_dashboard_updates_config().get("dashboard_update_enabled", True))


def _dashboard_update_disabled_message() -> Optional[str]:
    """Explain why the Dashboard must not offer ``hermes update``."""
    if _dashboard_update_disabled_by_config():
        return (
            "Installing Hermes updates is disabled in this Dashboard because "
            "this installation uses an external update workflow."
        )

    if _default_hermes_root_is_opt_data():
        return "Hermes updates are managed outside this dashboard in containerized environments."

    try:
        from hermes_constants import is_container

        if not is_container():
            return None
    except Exception:
        return None

    from hermes_cli.web_server import PROJECT_ROOT
    from hermes_cli.config import detect_install_method

    try:
        if detect_install_method(PROJECT_ROOT) == "git":
            return None
    except Exception:
        pass
    return "Hermes updates are managed outside this dashboard in containerized environments."


def _dashboard_local_update_managed_externally() -> bool:
    """True when the dashboard should not offer ``hermes update``.

    Containerized dashboards are updated by the outer launcher/image — except a
    ``git`` install (bind-mounted checkout, e.g. the hermes-webui image), where
    the update button is the correct path. pip stays blocked in containers: its
    apply path mutates the running container filesystem.
    """
    return _dashboard_update_disabled_message() is not None


def _dashboard_update_check_target() -> Optional[Tuple[str, str]]:
    """Resolve a configured ``remote/branch`` for a read-only update check."""
    raw = str(_dashboard_updates_config().get("dashboard_update_check_ref", "")).strip()
    if not raw or "/" not in raw:
        return None
    remote, branch = raw.split("/", 1)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", remote):
        return None
    if not branch or branch.startswith("-") or ".." in branch:
        return None

    from hermes_cli.web_server import PROJECT_ROOT

    checked = subprocess.run(
        ["git", "check-ref-format", f"refs/heads/{branch}"],
        capture_output=True,
        timeout=5,
        cwd=str(PROJECT_ROOT),
    )
    return (remote, branch) if checked.returncode == 0 else None


def _check_configured_dashboard_git_ref(remote: str, branch: str) -> Optional[int]:
    """Fetch and count official commits missing from HEAD without applying them."""
    from hermes_cli.web_server import PROJECT_ROOT

    target_ref = f"refs/remotes/{remote}/{branch}"
    try:
        remote_check = subprocess.run(
            ["git", "remote", "get-url", remote],
            capture_output=True,
            timeout=5,
            cwd=str(PROJECT_ROOT),
        )
        if remote_check.returncode != 0:
            return None
        fetched = subprocess.run(
            ["git", "fetch", "--quiet", remote, f"+refs/heads/{branch}:{target_ref}"],
            capture_output=True,
            timeout=15,
            cwd=str(PROJECT_ROOT),
        )
        if fetched.returncode != 0:
            return None
        counted = subprocess.run(
            ["git", "rev-list", "--count", f"HEAD..{target_ref}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            cwd=str(PROJECT_ROOT),
        )
        return int(counted.stdout.strip()) if counted.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _managed_files_policy(request: Request, *, create_root: bool = True) -> ManagedFilesPolicy:
    raw_forced_root = os.environ.get(_MANAGED_FILES_ROOT_ENV, "").strip()
    if raw_forced_root:
        root = _ensure_managed_root(raw_forced_root) if create_root else _canonical_path(Path(raw_forced_root))
        return ManagedFilesPolicy(default_path=root, locked_root=root, can_change_path=False)

    # Remote/OAuth access does not imply a hosted container (a gated macOS launchd
    # install still browses its home). Lock to /opt/data only when the Hermes
    # root actually IS /opt/data or HERMES_DASHBOARD_FILES_ROOT is set.
    if _default_hermes_root_is_opt_data():
        root = _ensure_managed_root(_HOSTED_MANAGED_FILES_ROOT) if create_root else _HOSTED_MANAGED_FILES_ROOT
        return ManagedFilesPolicy(default_path=root, locked_root=root, can_change_path=False)

    home = _canonical_path(Path.home())
    return ManagedFilesPolicy(default_path=home, locked_root=None, can_change_path=True)


def _resolve_managed_path(
    raw_path: str | None, request: Request, *, for_write: bool = False
) -> tuple[ManagedFilesPolicy, Path, str]:
    policy = _managed_files_policy(request)
    text = _path_text(raw_path)
    root = policy.locked_root

    if root is not None and (not text or text in {".", "/"}):
        candidate = root
    elif not text:
        candidate = policy.default_path
    else:
        candidate = Path(text).expanduser()
        if root is not None and not candidate.is_absolute():
            if any(part == ".." for part in candidate.parts):
                raise HTTPException(status_code=400, detail="Path cannot contain '..'")
            candidate = root / candidate
        elif not candidate.is_absolute():
            raise HTTPException(status_code=400, detail="Path must be absolute")

    if ".." in candidate.parts:
        raise HTTPException(status_code=400, detail="Path cannot contain '..'")

    if for_write and not candidate.exists():
        parent = _canonical_path(candidate.parent)
        resolved = parent / candidate.name
    else:
        resolved = _canonical_path(candidate, require_exists=not for_write)

    if root is not None and not _path_is_under(root, resolved):
        raise HTTPException(status_code=403, detail="Path outside managed files root")

    return policy, resolved, str(resolved)


def _managed_response_meta(policy: ManagedFilesPolicy) -> Dict[str, Any]:
    locked_root = str(policy.locked_root) if policy.locked_root is not None else None
    return {"root": locked_root, "locked_root": locked_root, "can_change_path": policy.can_change_path}


def _managed_file_entry(policy: ManagedFilesPolicy, target: Path) -> Dict[str, Any]:
    try:
        resolved = target.resolve()
    except (OSError, RuntimeError):
        raise HTTPException(status_code=400, detail="Invalid path")
    if policy.locked_root is not None and not _path_is_under(policy.locked_root, resolved):
        raise HTTPException(status_code=403, detail="Path outside managed files root")

    try:
        st = resolved.stat()
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Could not stat path: {exc}")

    is_dir = resolved.is_dir()
    mime_type = None if is_dir else (mimetypes.guess_type(resolved.name)[0] or "application/octet-stream")
    return {
        "name": target.name or resolved.name or str(resolved),
        "path": str(resolved),
        "is_directory": is_dir,
        "size": None if is_dir else st.st_size,
        "mtime": st.st_mtime,
        "mime_type": mime_type,
    }
