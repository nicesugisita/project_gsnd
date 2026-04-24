from fastapi import FastAPI, Query
from pydantic import BaseModel
from typing import Optional
import time
import subprocess
import os
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

app = FastAPI()

BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "log"

TYPE_TO_COMMAND = {
    "full": "FULL",
    "inc": "INC",
    "update": "UPDATE",
    "pipe": "PIPE",
    "rebuild": "REBUILD",
    "rebuild_all": "REBUILD_ALL",
    "index_sync_all": "INDEX_SYNC_ALL",
}

INDEX_TASK_CLASSPATH = (
    "$IR5_HOME/lib/chiana.jar:$IR5_HOME/lib/jwsdp.jar:$IR5_HOME/lib/m5_mgr.jar:"
    "$IR5_HOME/lib/mysql-connector-java-ga-bin.jar:$IR5_HOME/lib/mariadb-java-client-1.1.3.jar:"
    "$IR5_HOME/lib/xerces.jar::$IR5_HOME/lib/informix.jar:$IR5_HOME/lib/m5_client.jar:"
    "$IR5_HOME/lib/m5_server.jar:$IR5_HOME/lib/ojdbc14.jar:$IR5_HOME/lib/commons.jar:"
    "$IR5_HOME/lib/javamail.jar:$IR5_HOME/lib/m5_common.jar:$IR5_HOME/lib/m5_util.jar:"
    "$IR5_HOME/lib/pg74.214.jdbc2.jar:$IR5_HOME/lib/cubrid_jdbc.jar:$IR5_HOME/lib/jdom.jar:"
    "$IR5_HOME/lib/m5_core.jar:$IR5_HOME/lib/msbase.jar:$IR5_HOME/lib/sql_server.jar:"
    "$IR5_HOME/lib/db2java.jar:$IR5_HOME/lib/jiana3.4.jar:$IR5_HOME/lib/dqdic-1.2.0.jar:"
    "$IR5_HOME/lib/logback-classic-1.2.11.jar:$IR5_HOME/lib/logback-core-1.2.11.jar:"
    "$IR5_HOME/lib/slf4j-api-1.7.25.jar:$IR5_HOME/lib/m5_extension.jar:"
    "$IR5_HOME/lib/mssqlserver.jar:$IR5_HOME/lib/sqljdbc.jar:$IR5_HOME/lib/db2jcc.jar:"
    "$IR5_HOME/lib/jtds.jar:$IR5_HOME/lib/m5_framework.jar:$IR5_HOME/lib/msutil.jar:"
    "$IR5_HOME/lib/tibero-jdbc.jar:$IR5_HOME/lib/Altibase5.jar:$IR5_HOME/lib/derbyclient.jar:"
)

class IndexRequest(BaseModel):
    dbWatcherIds: Optional[str] = None
    queryIds: Optional[str] = None
    label: Optional[str] = None


_DEFAULT_COLLECTION = os.getenv('MARINER_UPLOAD_COLLECTION', 'GSND_UPLOADED_FILE_V1')
_MARINER_LOCAL_HOST = os.getenv('MARINER_LOCAL_HOST', 'localhost')
_MARINER_LOCAL_PORT = os.getenv('MARINER_LOCAL_PORT', '5555')
_JAVA_MIN_MEMORY = os.getenv('JAVA_MIN_MEMORY', '32m')
_JAVA_MAX_MEMORY = os.getenv('JAVA_MAX_MEMORY', '512m')


@app.post("/collections/index")
def index_collection(
    collection: str = Query(_DEFAULT_COLLECTION, description="컬렉션 이름"),
    type: str = Query(..., description="색인 타입"),
    body: IndexRequest = None
):

    start_time = time.time()
    index_type = type.lower()
    payload = body or IndexRequest()

    command_type = TYPE_TO_COMMAND.get(index_type)
    if command_type is None:
        response_time = f"{int((time.time() - start_time) * 1000)}ms"
        return {
            "version": 51,
            "responseTime": response_time,
            "status": "400",
            "jobStatus": "failed",
            "messages": [f"unsupported index type: [{type}]"],
        }

    if index_type in {"inc", "update"} and not payload.dbWatcherIds:
        response_time = f"{int((time.time() - start_time) * 1000)}ms"
        return {
            "version": 51,
            "responseTime": response_time,
            "status": "400",
            "jobStatus": "failed",
            "messages": [f"dbWatcherIds is required for type: [{type}]"],
        }

    if index_type in {"inc", "update"} and not payload.queryIds:
        response_time = f"{int((time.time() - start_time) * 1000)}ms"
        return {
            "version": 51,
            "responseTime": response_time,
            "status": "400",
            "jobStatus": "failed",
            "messages": [f"queryIds is required for type: [{type}]"],
        }

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = LOG_DIR / f"bridge_index_{collection}_{index_type}_{ts}.log"

    inc_query_id = payload.queryIds if index_type == "inc" else ""
    field_update_query_id = payload.queryIds if index_type == "update" else ""

    index_cmd = [
        "java",
        "-server",
        f"-Xms{_JAVA_MIN_MEMORY}",
        f"-Xmx{_JAVA_MAX_MEMORY}",
        "-classpath",
        os.path.expandvars(INDEX_TASK_CLASSPATH),
        "com.diquest.ir5.client.apps.IndexTaskCommander",
        _MARINER_LOCAL_HOST,
        _MARINER_LOCAL_PORT,
        collection,
        command_type,
        payload.dbWatcherIds or "",
        inc_query_id,
        field_update_query_id,
        "",
        "",
        "",
        "",
    ]

    try:
        with open(log_path, "ab") as log_file:
            proc = subprocess.Popen(
                index_cmd,
                cwd=str(BASE_DIR),
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
    except Exception as exc:
        response_time = f"{int((time.time() - start_time) * 1000)}ms"
        return {
            "version": 51,
            "responseTime": response_time,
            "status": "500",
            "jobStatus": "failed",
            "messages": [f"failed to start index process: [{exc}]"],
        }

    message = (
        f"collection : [{collection}], type : [{type}] index process started "
        f"(pid={proc.pid}, log={log_path})"
    )

    response_time = f"{int((time.time() - start_time) * 1000)}ms"

    return {
        "version": 51,
        "responseTime": response_time,
        "status": "200",
        "jobStatus": "success",
        "messages": [message]
    }