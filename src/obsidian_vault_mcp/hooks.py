"""Optional post-write hook dispatcher for vault mutations."""

from __future__ import annotations

import json
import logging
import os
import shlex
import shutil
import subprocess
import threading
from collections.abc import Sequence

from . import config

logger = logging.getLogger(__name__)

_PATH_SEP = ":"


def _prepare_command(command: str) -> list[str] | None:
    """Parse and validate the configured hook command without invoking a shell."""
    try:
        args = shlex.split(command, posix=os.name != "nt")
    except ValueError as exc:
        logger.warning("post-write hook config could not be parsed: %s", exc)
        return None

    if not args:
        return None

    executable = args[0]
    if os.path.isabs(executable):
        if not os.path.exists(executable):
            logger.warning("post-write hook executable does not exist: %s", executable)
            return None
    elif shutil.which(executable) is None:
        logger.warning("post-write hook executable not found on PATH: %s", executable)
        return None

    return args


def _run_cmd(command: str, operation: str, paths: list[str]) -> None:
    """Execute the configured post-write command in a daemon thread."""
    args = _prepare_command(command)
    if args is None:
        return

    env = os.environ.copy()
    env["MCP_OPERATION"] = operation
    env["MCP_PATHS"] = _PATH_SEP.join(paths)
    env["MCP_PATHS_JSON"] = json.dumps(paths, ensure_ascii=False)

    try:
        result = subprocess.run(
            args,
            shell=False,
            env=env,
            cwd=str(config.VAULT_PATH),
            capture_output=True,
            text=True,
            timeout=config.VAULT_MCP_POST_WRITE_TIMEOUT,
        )
        if result.returncode != 0:
            logger.warning(
                "post-write hook exited %d: %s",
                result.returncode,
                (result.stderr or "").strip(),
            )
        else:
            logger.debug("post-write hook ok: %s %s", operation, paths)
    except subprocess.TimeoutExpired:
        logger.warning("post-write hook timed out: %s %s", operation, paths)
    except Exception as exc:
        logger.warning("post-write hook error: %s", exc)


def fire_post_write(operation: str, paths: Sequence[str]) -> None:
    """Dispatch the optional post-write hook fire-and-forget."""
    if not config.VAULT_MCP_POST_WRITE_CMD:
        return

    path_list = [path for path in paths if path]
    if not path_list:
        return

    worker = threading.Thread(
        target=_run_cmd,
        args=(config.VAULT_MCP_POST_WRITE_CMD, operation, path_list),
        daemon=True,
        name="mcp-post-write",
    )
    worker.start()
