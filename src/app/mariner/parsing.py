"""문서 파싱 유틸리티 — DQJFAttacher JAR 호출"""

import os
import subprocess


def parse_file(file_path: str, timeout: int = 120) -> str:
    backend_dir = os.path.dirname(os.path.abspath(__file__))
    work_home = os.path.join(backend_dir, "filter")
    jar_path = os.path.join(work_home, "lib", "DQJFAttacher.jar")
    conf_path = os.path.join(work_home, "conf", "jfattacher.conf")
    library_path = os.path.join(work_home, "lib", "linux_64bit")

    if not os.path.exists(file_path):
        raise FileNotFoundError(f"File not found: {file_path}")

    cmd = [
        "java",
        f"-Djava.library.path={library_path}",
        "-cp", jar_path,
        "com.diquest.jnifilter.DQJFAttacher",
        "test", conf_path, file_path,
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Document parsing timeout") from exc

    if result.returncode != 0:
        error_message = (result.stderr or result.stdout or "Document parsing failed").strip()
        raise RuntimeError(error_message)

    return (result.stdout or "").strip()
