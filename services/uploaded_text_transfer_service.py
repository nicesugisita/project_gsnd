"""
Uploaded text remote transfer service.

Moves uploaded extracted text files to a remote dataset directory.
"""

import logging
import os
import subprocess
import shutil
from typing import Dict, Optional

from core.config import Config

logger = logging.getLogger(__name__)


def _build_ssh_prefix(timeout: int) -> list[str]:
    base_command = [
        "ssh",
        "-p",
        str(int(Config.UPLOADED_TEXT_REMOTE_PORT)),
        "-o",
        f"ConnectTimeout={timeout}",
    ]

    ssh_key = (Config.UPLOADED_TEXT_REMOTE_SSH_KEY_PATH or "").strip()
    password = (Config.UPLOADED_TEXT_REMOTE_PASSWORD or "").strip()

    if password:
        if shutil.which("sshpass") is None:
            raise RuntimeError("sshpass not installed for password auth")
        return [
            "sshpass",
            "-p",
            password,
            *base_command,
            "-o",
            "PreferredAuthentications=password",
            "-o",
            "PubkeyAuthentication=no",
        ]

    base_command.extend(["-o", "BatchMode=yes"])
    if ssh_key:
        base_command.extend(["-i", ssh_key])
    return base_command


def load_uploaded_text_content(path: str) -> Dict[str, Optional[str]]:
    if not path:
        return {"ok": False, "content": None, "message": "empty path"}

    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as file:
                return {"ok": True, "content": file.read(), "message": None}
        except Exception as exc:
            return {"ok": False, "content": None, "message": str(exc)}

    timeout = max(1, int(Config.UPLOADED_TEXT_REMOTE_TIMEOUT))
    remote_user = (Config.UPLOADED_TEXT_REMOTE_USER or "").strip()
    remote_host = (Config.UPLOADED_TEXT_REMOTE_HOST or "").strip()

    if not remote_host:
        return {"ok": False, "content": None, "message": "remote host is empty"}

    target_host = f"{remote_user + '@' if remote_user else ''}{remote_host}"

    try:
        command = [*_build_ssh_prefix(timeout), target_host, "cat", path]
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout + 5,
        )
    except Exception as exc:
        return {"ok": False, "content": None, "message": str(exc)}

    if result.returncode != 0:
        error_message = (result.stderr or result.stdout or "ssh cat failed").strip()
        return {"ok": False, "content": None, "message": error_message}

    return {"ok": True, "content": result.stdout or "", "message": None}


def transfer_uploaded_text_to_remote(local_path: str) -> Dict[str, Optional[str]]:
    """Transfer a local text file to configured remote server using scp."""
    if not Config.UPLOADED_TEXT_REMOTE_ENABLED:
        return {
            "ok": False,
            "remote_path": None,
            "message": "remote transfer disabled",
        }

    if not local_path or not os.path.exists(local_path):
        return {
            "ok": False,
            "remote_path": None,
            "message": f"local file not found: {local_path}",
        }

    filename = os.path.basename(local_path)
    remote_dir = (Config.UPLOADED_TEXT_REMOTE_DIR or "").rstrip("/")
    remote_path = f"{remote_dir}/{filename}"

    remote_user = (Config.UPLOADED_TEXT_REMOTE_USER or "").strip()
    remote_host = (Config.UPLOADED_TEXT_REMOTE_HOST or "").strip()
    remote_port = int(Config.UPLOADED_TEXT_REMOTE_PORT)
    timeout = max(1, int(Config.UPLOADED_TEXT_REMOTE_TIMEOUT))

    if not remote_host or not remote_dir:
        return {
            "ok": False,
            "remote_path": None,
            "message": "remote host or directory is empty",
        }

    destination = f"{remote_user + '@' if remote_user else ''}{remote_host}:{remote_dir}/"

    try:
        ssh_prefix = _build_ssh_prefix(timeout)
    except Exception as exc:
        return {
            "ok": False,
            "remote_path": None,
            "message": str(exc),
        }

    command = [
        *ssh_prefix,
    ]

    if command and command[0] == "sshpass":
        # sshpass ... ssh -> scp 로 치환
        sshpass_prefix = command[:3]
        ssh_options = command[3:]
        if ssh_options and ssh_options[0] == "ssh":
            ssh_options = ssh_options[1:]
        command = [*sshpass_prefix, "scp"]
        for option in ssh_options:
            if option == "-p":
                command.append("-P")
            else:
                command.append(option)
    else:
        if command and command[0] == "ssh":
            command[0] = "scp"
        command = [item if item != "-p" else "-P" for item in command]

    command.extend([local_path, destination])

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout + 5,
        )
    except Exception as exc:
        logger.error("업로드 텍스트 원격 전송 오류: %s", exc, exc_info=True)
        return {
            "ok": False,
            "remote_path": None,
            "message": str(exc),
        }

    if result.returncode != 0:
        error_message = (result.stderr or result.stdout or "scp failed").strip()
        logger.warning(
            "업로드 텍스트 원격 전송 실패: file=%s, dest=%s, error=%s",
            local_path,
            destination,
            error_message,
        )
        return {
            "ok": False,
            "remote_path": None,
            "message": error_message,
        }

    logger.info(
        "업로드 텍스트 원격 전송 성공: file=%s -> %s",
        local_path,
        remote_path,
    )
    return {
        "ok": True,
        "remote_path": remote_path,
        "message": None,
    }
