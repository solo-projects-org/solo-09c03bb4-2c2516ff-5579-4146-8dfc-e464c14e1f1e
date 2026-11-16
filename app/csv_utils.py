"""CSV 解析工具：容忍表头别名、BOM 与 GBK 编码，整文件严格校验。"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone

from .errors import ServiceError

# 表头别名（全部转为小写、去空白后匹配）
STATION_HEADER_ALIASES = {
    "来源": "source",
    "源站号": "source_station_id",
    "站名": "name",
    "source": "source",
    "source_id": "source_station_id",
    "source_station_id": "source_station_id",
    "station_id": "source_station_id",
    "name": "name",
    "station_name": "name",
}

OBS_HEADER_ALIASES = {
    "来源": "source",
    "源站号": "source_station_id",
    "utc时刻": "obs_time",
    "utc 时刻": "obs_time",
    "时刻": "obs_time",
    "温度": "temperature",
    "temperature": "temperature",
    "temp": "temperature",
    "obs_time": "obs_time",
    "time": "obs_time",
    "utc_time": "obs_time",
}


def decode_bytes(raw: bytes) -> str:
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            return raw.decode("gbk")
        except UnicodeDecodeError as exc:
            raise ServiceError(f"CSV 编码无法识别（仅支持 UTF-8/GBK）：{exc}")


def parse_csv(raw: bytes, aliases: dict[str, str], required: set[str]) -> list[dict[str, str]]:
    """解析 CSV 为字典列表；表头缺失或数据行有空字段都整文件拒绝。"""
    text = decode_bytes(raw)
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise ServiceError("CSV 文件为空")
    header = [h.strip().lower() for h in header]
    cols: list[str | None] = []
    for h in header:
        cols.append(aliases.get(h))
    if any(c is None for c in cols):
        unknown = [h for h, c in zip(header, cols) if c is None]
        raise ServiceError(f"CSV 含无法识别的表头列：{', '.join(unknown)}")
    if not set(required).issubset(set(cols)):
        missing = required - set(c for c in cols if c)
        raise ServiceError(f"CSV 缺少必需列：{', '.join(sorted(missing))}")

    rows: list[dict[str, str]] = []
    for line_no, raw_row in enumerate(reader, start=2):
        if not raw_row or all(not cell.strip() for cell in raw_row):
            continue  # 容忍空行
        if len(raw_row) != len(cols):
            raise ServiceError(
                f"CSV 第 {line_no} 行列数（{len(raw_row)}）与表头（{len(cols)}）不一致"
            )
        record: dict[str, str] = {}
        for col, cell in zip(cols, raw_row):
            value = cell.strip()
            if col in required and not value:
                raise ServiceError(f"CSV 第 {line_no} 行必填字段为空")
            record[col] = value
        record["_line"] = str(line_no)
        rows.append(record)
    if not rows:
        raise ServiceError("CSV 没有任何数据行")
    return rows


def parse_station_csv(raw: bytes) -> list[dict[str, str]]:
    return parse_csv(
        raw, STATION_HEADER_ALIASES, {"source", "source_station_id", "name"}
    )


def parse_utc_time(value: str, line_no: int) -> str:
    """把 UTC 时刻规范化为 ``YYYY-MM-DDTHH:MM:SS[.ffffff]Z``；拒绝无时区时间。"""
    text = value.strip().replace(" ", "T")
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        raise ServiceError(f"CSV 第 {line_no} 行时刻格式无法解析：{value}")
    if dt.tzinfo is None:
        raise ServiceError(
            f"CSV 第 {line_no} 行时刻缺少时区信息，必须为 UTC（带 Z 或 +00:00）：{value}"
        )
    dt = dt.astimezone(timezone.utc)
    canon = dt.strftime("%Y-%m-%dT%H:%M:%S")
    if dt.microsecond:
        canon += f".{dt.microsecond:06d}"
    return canon + "Z"


def parse_observation_csv(raw: bytes) -> list[dict[str, str]]:
    rows = parse_csv(
        raw,
        OBS_HEADER_ALIASES,
        {"source", "source_station_id", "obs_time", "temperature"},
    )
    out: list[dict[str, str]] = []
    for row in rows:
        line_no = row["_line"]
        try:
            temperature = float(row["temperature"])
        except ValueError:
            raise ServiceError(f"CSV 第 {line_no} 行温度不是数字：{row['temperature']}")
        if temperature != temperature or temperature in (float("inf"), float("-inf")):
            raise ServiceError(f"CSV 第 {line_no} 行温度必须为有限数值")
        out.append({
            "source": row["source"],
            "source_station_id": row["source_station_id"],
            "obs_time": parse_utc_time(row["obs_time"], int(line_no)),
            "temperature": str(temperature),
            "_line": line_no,
        })
    return out
