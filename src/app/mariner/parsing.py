"""문서 파싱 유틸리티 — DQJFAttacher JAR 호출"""

import glob
import os
from pathlib import Path
import subprocess

from app.core.config import Config


def parse_file(file_path: str, timeout: int = 120) -> str:
    project_root = Path(__file__).resolve().parents[3]
    jar_dir = (Config.JAR_LIB_PATH or "").strip() or str(project_root / "jar_lib")
    jar_files = glob.glob(os.path.join(jar_dir, "*.jar"))
    classpath = os.pathsep.join(jar_files)
    conf_path = (Config.JF_ATTACHER_CONF_PATH or "").strip() or str(
        project_root / "src" / "app" / "mariner" / "filter" / "conf" / "jfattacher.conf"
    )
    library_path = (Config.JF_ATTACHER_NATIVE_LIB_PATH or "").strip() or str(
        project_root / "src" / "app" / "mariner" / "filter" / "lib" / "linux_64bit"
    )

    if not os.path.exists(file_path):
        raise FileNotFoundError(f"File not found: {file_path}")
    if not jar_files:
        raise RuntimeError(f"Parser JAR files not found: {jar_dir}")
    if not os.path.exists(conf_path):
        raise RuntimeError(
            "Parser config file not found: "
            f"{conf_path}. Set JF_ATTACHER_CONF_PATH in .env"
        )

    cmd = [
        "java",
        "-cp", classpath,
        "com.diquest.jnifilter.DQJFAttacher",
        "test", conf_path, file_path,
    ]
    if os.path.exists(library_path):
        cmd.insert(1, f"-Djava.library.path={library_path}")

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Document parsing timeout") from exc

    if result.returncode != 0:
        error_message = (result.stderr or result.stdout or "Document parsing failed").strip()
        raise RuntimeError(error_message)

    return (result.stdout or "").strip()
