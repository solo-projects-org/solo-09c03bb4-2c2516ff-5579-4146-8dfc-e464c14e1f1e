"""FastAPI 应用：测站身份统一数仓的全部 HTTP 接口与网页。"""

from __future__ import annotations

import os

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from . import database as db
from . import service
from .csv_utils import parse_observation_csv, parse_station_csv
from .errors import ServiceError

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = FastAPI(title="气象测站身份统一数仓", version="1.0.0")


@app.on_event("startup")
def _startup() -> None:
    db.init_db()


@app.exception_handler(ServiceError)
def _service_error_handler(_request: Request, exc: ServiceError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.message,
                 "current_version": getattr(exc, "current_version", None)},
    )


def _read_upload(file: UploadFile) -> bytes:
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise ServiceError("仅支持 .csv 文件")
    raw = file.file.read()
    if not raw:
        raise ServiceError("上传文件为空")
    return raw


# ---------------- 页面 ----------------

@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(os.path.join(BASE_DIR, "static", "index.html"))


# ---------------- 身份状态 ----------------

@app.get("/api/state")
def get_state() -> dict:
    conn = db.connect()
    try:
        return service.current_state(conn)
    finally:
        conn.close()


@app.post("/api/stations/upload")
async def upload_stations(
    file: UploadFile = File(...),
    base_ver: int | None = Form(default=None),
) -> dict:
    rows = parse_station_csv(_read_upload(file))
    conn = db.connect()
    try:
        return service.upload_stations(conn, rows, base_ver)
    finally:
        conn.close()


@app.post("/api/groups/merge")
async def merge_groups(payload: dict) -> dict:
    ids = payload.get("unified_ids")
    base_ver = payload.get("base_ver")
    if not isinstance(ids, list) or not ids:
        raise HTTPException(400, "unified_ids 必须是非空数组")
    if not isinstance(base_ver, int):
        raise HTTPException(400, "base_ver 必须为整数（页面当前身份状态版本）")
    conn = db.connect()
    try:
        return service.merge_groups(conn, [int(x) for x in ids], base_ver)
    finally:
        conn.close()


@app.post("/api/groups/split")
async def split_group(payload: dict) -> dict:
    uid = payload.get("unified_id")
    partitions = payload.get("partitions")
    base_ver = payload.get("base_ver")
    if not isinstance(uid, int):
        raise HTTPException(400, "unified_id 必须为整数")
    if not isinstance(partitions, list) or len(partitions) < 2:
        raise HTTPException(400, "partitions 必须是至少两个分组的数组（成员为来源站主键）")
    if not isinstance(base_ver, int):
        raise HTTPException(400, "base_ver 必须为整数（页面当前身份状态版本）")
    clean_parts = []
    for part in partitions:
        if not isinstance(part, list) or not part:
            raise HTTPException(400, "每个拆分分组必须是非空整数数组")
        clean_parts.append([int(x) for x in part])
    conn = db.connect()
    try:
        return service.split_group(conn, uid, clean_parts, base_ver)
    finally:
        conn.close()


# ---------------- 查询 ----------------

@app.get("/api/groups/lineage/{unified_id}")
def lineage(unified_id: int) -> dict:
    conn = db.connect()
    try:
        return service.group_lineage(conn, unified_id)
    finally:
        conn.close()


@app.get("/api/query/unified/{unified_id}")
def query_unified(unified_id: int) -> dict:
    conn = db.connect()
    try:
        return service.query_unified_id(conn, unified_id)
    finally:
        conn.close()


@app.get("/api/query/source")
def query_source(source: str, source_station_id: str) -> dict:
    conn = db.connect()
    try:
        return service.query_source_station(conn, source, source_station_id)
    finally:
        conn.close()


@app.get("/api/operations")
def operations() -> dict:
    conn = db.connect()
    try:
        return {"operations": service.list_ops(conn)}
    finally:
        conn.close()


# ---------------- 观测 ----------------

@app.post("/api/observations/ingest")
async def ingest(
    file: UploadFile = File(...),
    base_ver: int = Form(...),
) -> dict:
    rows = parse_observation_csv(_read_upload(file))
    conn = db.connect()
    try:
        return service.ingest_observations(conn, rows, base_ver)
    finally:
        conn.close()


@app.get("/api/observations")
def observations(source: str | None = None,
                 source_station_id: str | None = None,
                 unified_id: int | None = None,
                 batch_id: int | None = None,
                 limit: int = 100, offset: int = 0) -> dict:
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    conn = db.connect()
    try:
        return service.list_observations(
            conn, source=source, source_station_id=source_station_id,
            unified_id=unified_id, batch_id=batch_id, limit=limit, offset=offset,
        )
    finally:
        conn.close()


@app.get("/api/health")
def health() -> dict:
    conn = db.connect()
    try:
        return {"status": "ok", "version": db.get_version(conn)}
    finally:
        conn.close()
