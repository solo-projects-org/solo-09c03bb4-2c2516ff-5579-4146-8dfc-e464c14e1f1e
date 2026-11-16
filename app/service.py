"""领域服务：身份状态机（上传/归并/拆分）、追溯查询与观测入仓。

所有写函数都在单个 ``BEGIN IMMEDIATE`` 事务内完成：
* 先校验页面携带的 ``base_ver``，过期则 409 拒绝（拒绝过期页面的分组操作）；
* 任何校验失败都抛 ``ServiceError``，事务回滚，不留半批；
* 受影响的新组一律领取全新统一号，旧号退役但永不复用。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from . import database as db
from .errors import ServiceError, StaleStateError


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _check_base_version(conn: sqlite3.Connection, base_ver: int | None) -> int:
    ver = db.get_version(conn)
    if base_ver is not None and int(base_ver) != ver:
        raise StaleStateError(ver)
    return ver


def _log_op(conn: sqlite3.Connection, kind: str, base_ver: int | None,
            from_ver: int, to_ver: int, detail: dict) -> int:
    cur = conn.execute(
        "INSERT INTO op_log(kind, base_ver, from_ver, to_ver, detail_json, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (kind, base_ver, from_ver, to_ver,
         json.dumps(detail, ensure_ascii=False), _now()),
    )
    return int(cur.lastrowid)


def _station_brief(conn: sqlite3.Connection, pk: int) -> dict:
    row = conn.execute(
        "SELECT id, source, source_station_id, name FROM source_station WHERE id=?",
        (pk,),
    ).fetchone()
    return {
        "source_station_pk": int(row["id"]),
        "source": row["source"],
        "source_station_id": row["source_station_id"],
        "name": row["name"],
    }


def _group_payload(conn: sqlite3.Connection, unified_id: int) -> dict:
    g = conn.execute(
        "SELECT * FROM station_group WHERE unified_id=?", (unified_id,)
    ).fetchone()
    if g is None:
        raise ServiceError(f"统一号 {unified_id} 不存在", 404)
    pks = db.group_member_pks(conn, unified_id)
    return {
        "unified_id": int(g["unified_id"]),
        "current": bool(g["current"]),
        "created_ver": int(g["created_ver"]),
        "retired_ver": int(g["retired_ver"]) if g["retired_ver"] is not None else None,
        "members": [_station_brief(conn, pk) for pk in pks],
    }


# ---------------- 当前身份状态 ----------------

def current_state(conn: sqlite3.Connection) -> dict:
    groups = [
        _group_payload(conn, int(r["unified_id"]))
        for r in conn.execute(
            "SELECT unified_id FROM station_group WHERE current=1 ORDER BY unified_id"
        ).fetchall()
    ]
    stations = [
        {
            "source_station_pk": int(r["id"]),
            "source": r["source"],
            "source_station_id": r["source_station_id"],
            "name": r["name"],
        }
        for r in conn.execute(
            "SELECT id, source, source_station_id, name "
            "FROM source_station ORDER BY id"
        ).fetchall()
    ]
    return {
        "version": db.get_version(conn),
        "cap": db.STATION_CAP,
        "source_station_count": db.count_source_stations(conn),
        "current_group_count": len(groups),
        "groups": groups,
        "source_stations": stations,
    }


# ---------------- 上传来源站 ----------------

def upload_stations(conn: sqlite3.Connection, rows: list[dict],
                    base_ver: int | None = None) -> dict:
    with db.write_tx(conn):
        from_ver = _check_base_version(conn, base_ver)

        keys: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for row in rows:
            key = (row["source"], row["source_station_id"])
            if key in seen:
                raise ServiceError(
                    f"CSV 内部存在重复来源站：{key[0]} / {key[1]}"
                )
            seen.add(key)
            keys.append(key)

        existing = {
            (r["source"], r["source_station_id"]): r
            for r in conn.execute(
                "SELECT * FROM source_station WHERE "
                + " OR ".join("(source=? AND source_station_id=?)" for _ in keys),
                [v for key in keys for v in key],
            ).fetchall()
        } if keys else {}

        new_rows: list[dict] = []
        for row in rows:
            key = (row["source"], row["source_station_id"])
            old = existing.get(key)
            if old is not None and old["name"] != row["name"]:
                raise ServiceError(
                    f"来源站 {key[0]} / {key[1]} 已存在且站名不一致"
                    f"（库中为「{old['name']}」，本次为「{row['name']}」），整批拒绝"
                )
            if old is None:
                new_rows.append(row)

        if db.count_source_stations(conn) + len(new_rows) > db.STATION_CAP:
            raise ServiceError(
                f"总库至多 {db.STATION_CAP} 站：现有 "
                f"{db.count_source_stations(conn)} 站，本次新增 {len(new_rows)} 站，超限"
            )

        to_ver = from_ver
        created_ids: list[int] = []
        station_records: list[dict] = []
        if new_rows:
            new_ids = db.alloc_unified_ids(conn, len(new_rows))
            to_ver = db.bump_version(conn)
            for row, uid in zip(new_rows, new_ids):
                cur = conn.execute(
                    "INSERT INTO source_station(source, source_station_id, name, created_ver) "
                    "VALUES (?, ?, ?, ?)",
                    (row["source"], row["source_station_id"], row["name"], to_ver),
                )
                pk = int(cur.lastrowid)
                db.create_group(conn, uid, to_ver, [pk], op_id=None)
                created_ids.append(uid)
                station_records.append({
                    "unified_id": uid,
                    "source": row["source"],
                    "source_station_id": row["source_station_id"],
                    "name": row["name"],
                })

        op_id = _log_op(conn, "upload", base_ver, from_ver, to_ver, {
            "created_group_ids": created_ids,
            "stations": station_records,
            "skipped_existing": [
                {"source": k[0], "source_station_id": k[1], "name": existing[k]["name"]}
                for k in keys if k in existing
            ],
        })
        if created_ids:
            conn.executemany(
                "UPDATE station_group SET created_op=? WHERE unified_id=?",
                [(op_id, uid) for uid in created_ids],
            )

    return {
        "inserted": len(new_rows),
        "skipped_existing": len(rows) - len(new_rows),
        "created_unified_ids": created_ids,
        "version": db.get_version(conn),
    }


# ---------------- 归并现站 ----------------

def merge_groups(conn: sqlite3.Connection, unified_ids: list[int],
                 base_ver: int) -> dict:
    with db.write_tx(conn):
        from_ver = _check_base_version(conn, base_ver)
        if len(unified_ids) < 2:
            raise ServiceError("归并至少选择两个当前统一站")
        if len(set(unified_ids)) != len(unified_ids):
            raise ServiceError("归并列表中存在重复统一号")

        member_pks: list[int] = []
        for uid in unified_ids:
            g = db.get_current_group(conn, int(uid))
            if g is None:
                raise ServiceError(f"统一号 {uid} 不是当前站（可能已退役），无法归并")
            member_pks.extend(db.group_member_pks(conn, int(uid)))

        to_ver = db.bump_version(conn)
        new_id = db.alloc_unified_ids(conn, 1)[0]
        op_id = _log_op(conn, "merge", base_ver, from_ver, to_ver, {
            "retired_group_ids": [int(u) for u in unified_ids],
            "created_group_ids": [new_id],
            "members": {str(new_id): member_pks},
        })
        for uid in unified_ids:
            db.retire_group(conn, int(uid), to_ver)
        db.create_group(conn, new_id, to_ver, member_pks, op_id)

    return {
        "retired_unified_ids": [int(u) for u in unified_ids],
        "new_unified_id": new_id,
        "version": db.get_version(conn),
    }


# ---------------- 按来源站拆分 ----------------

def split_group(conn: sqlite3.Connection, unified_id: int,
                partitions: list[list[int]], base_ver: int) -> dict:
    with db.write_tx(conn):
        from_ver = _check_base_version(conn, base_ver)
        g = db.get_current_group(conn, int(unified_id))
        if g is None:
            raise ServiceError(f"统一号 {unified_id} 不是当前站（可能已退役），无法拆分")
        if len(partitions) < 2:
            raise ServiceError("拆分至少要形成两个新组")

        current_pks = set(db.group_member_pks(conn, int(unified_id)))
        flat: list[int] = []
        for part in partitions:
            if not part:
                raise ServiceError("拆分存在空分组")
            for pk in part:
                flat.append(int(pk))
        if len(flat) != len(set(flat)):
            raise ServiceError("拆分分组中存在重复来源站")
        if set(flat) != current_pks:
            unknown = set(flat) - current_pks
            missing = current_pks - set(flat)
            detail = []
            if unknown:
                detail.append(f"含不属于该组的来源站 {sorted(unknown)}")
            if missing:
                detail.append(f"缺少组成员 {sorted(missing)}")
            raise ServiceError("拆分成员必须与该组当前来源站完全一致：" + "；".join(detail))

        n = len(partitions)
        if db.count_current_groups(conn) - 1 + n > db.STATION_CAP:
            raise ServiceError(
                f"拆分后当前站数将超过总库上限 {db.STATION_CAP}，操作拒绝"
            )

        to_ver = db.bump_version(conn)
        new_ids = db.alloc_unified_ids(conn, n)
        op_id = _log_op(conn, "split", base_ver, from_ver, to_ver, {
            "retired_group_ids": [int(unified_id)],
            "created_group_ids": new_ids,
            "members": {str(uid): part for uid, part in zip(new_ids, partitions)},
        })
        db.retire_group(conn, int(unified_id), to_ver)
        for uid, part in zip(new_ids, partitions):
            db.create_group(conn, uid, to_ver, [int(pk) for pk in part], op_id)

    return {
        "retired_unified_id": int(unified_id),
        "new_unified_ids": new_ids,
        "version": db.get_version(conn),
    }


# ---------------- 沿革与新旧号查询 ----------------

def _op_edges(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM op_log ORDER BY op_id").fetchall()
    ops = []
    for r in rows:
        detail = json.loads(r["detail_json"])
        ops.append({
            "op_id": int(r["op_id"]),
            "kind": r["kind"],
            "base_ver": r["base_ver"],
            "from_ver": int(r["from_ver"]),
            "to_ver": int(r["to_ver"]),
            "created_at": r["created_at"],
            "detail": detail,
        })
    return ops


def list_ops(conn: sqlite3.Connection) -> list[dict]:
    return list(reversed(_op_edges(conn)))


def group_lineage(conn: sqlite3.Connection, unified_id: int) -> dict:
    target = conn.execute(
        "SELECT * FROM station_group WHERE unified_id=?", (unified_id,)
    ).fetchone()
    if target is None:
        raise ServiceError(f"统一号 {unified_id} 不存在", 404)

    ops = _op_edges(conn)
    parents: dict[int, set[int]] = {}
    children: dict[int, set[int]] = {}
    nodes: set[int] = set()
    for op in ops:
        d = op["detail"]
        created = d.get("created_group_ids", [])
        retired = d.get("retired_group_ids", [])
        for c in created:
            nodes.add(int(c))
            for r in retired:
                parents.setdefault(int(c), set()).add(int(r))
                children.setdefault(int(r), set()).add(int(c))
        for r in retired:
            nodes.add(int(r))

    start = int(unified_id)
    ancestors: set[int] = set()
    stack = [start]
    while stack:
        cur = stack.pop()
        for p in parents.get(cur, ()):  # 旧号 -> 更早的号
            if p not in ancestors:
                ancestors.add(p)
                stack.append(p)
    descendants: set[int] = set()
    stack = [start]
    while stack:
        cur = stack.pop()
        for c in children.get(cur, ()):  # 新号 -> 更晚的号
            if c not in descendants:
                descendants.add(c)
                stack.append(c)

    def node_info(uid: int) -> dict:
        return _group_payload(conn, uid)

    edges = []
    for op in ops:
        d = op["detail"]
        for r in d.get("retired_group_ids", []):
            for c in d.get("created_group_ids", []):
                if int(r) in ancestors | descendants | {start} or \
                        int(c) in ancestors | descendants | {start}:
                    edges.append({"from": int(r), "to": int(c), "via_op": op["op_id"]})

    return {
        "unified_id": start,
        "group": node_info(start),
        "ancestor_ids": sorted(ancestors),
        "descendant_ids": sorted(descendants),
        "related_groups": [node_info(uid) for uid in
                           sorted(ancestors | descendants | {start})],
        "edges": edges,
        "operations": [op for op in ops
                       if start in op["detail"].get("created_group_ids", [])
                       or start in op["detail"].get("retired_group_ids", [])],
    }


def query_unified_id(conn: sqlite3.Connection, unified_id: int) -> dict:
    """查统一号（含已退役旧号）：按其产生时的来源站集合查当前归属。"""
    group = _group_payload(conn, unified_id)
    member_pks = [m["source_station_pk"] for m in group["members"]]
    membership = db.current_membership(conn)

    current_by_uid: dict[int, list[int]] = {}
    for pk in member_pks:
        cur_uid = membership.get(pk)
        if cur_uid is not None:
            current_by_uid.setdefault(cur_uid, []).append(pk)

    current_attribution = []
    for uid in sorted(current_by_uid):
        payload = _group_payload(conn, uid)
        payload["matched_from_original"] = [
            m for m in payload["members"]
            if m["source_station_pk"] in set(member_pks)
        ]
        current_attribution.append(payload)

    ambiguous = len(current_attribution) > 1
    return {
        "query": {"type": "unified", "unified_id": int(unified_id)},
        "group": group,
        "current_attribution": current_attribution,
        "ambiguous": ambiguous,
        "ambiguous_note": (
            "该旧号的来源站现归属多个当前站，已全部列出，归属存在歧义"
            if ambiguous else "该号来源站当前归属唯一"
        ),
    }


def query_source_station(conn: sqlite3.Connection, source: str,
                         source_station_id: str) -> dict:
    row = conn.execute(
        "SELECT * FROM source_station WHERE source=? AND source_station_id=?",
        (source, source_station_id),
    ).fetchone()
    if row is None:
        raise ServiceError(
            f"来源站不存在：{source} / {source_station_id}", 404
        )
    pk = int(row["id"])
    history_rows = conn.execute(
        "SELECT g.* FROM station_group g "
        "JOIN group_member gm ON gm.unified_id = g.unified_id "
        "WHERE gm.source_station_pk=? ORDER BY g.unified_id",
        (pk,),
    ).fetchall()
    membership = db.current_membership(conn)
    current_uid = membership.get(pk)
    return {
        "query": {"type": "source", "source": source,
                  "source_station_id": source_station_id},
        "source_station": {
            "source_station_pk": pk,
            "source": row["source"],
            "source_station_id": row["source_station_id"],
            "name": row["name"],
            "created_ver": int(row["created_ver"]),
        },
        "current_unified_id": current_uid,
        "current_group": _group_payload(conn, current_uid) if current_uid else None,
        "group_history": [
            {
                "unified_id": int(r["unified_id"]),
                "current": bool(r["current"]),
                "created_ver": int(r["created_ver"]),
                "retired_ver": int(r["retired_ver"])
                if r["retired_ver"] is not None else None,
            }
            for r in history_rows
        ],
    }


# ---------------- 观测入仓与查询 ----------------

def ingest_observations(conn: sqlite3.Connection, rows: list[dict],
                        base_ver: int) -> dict:
    with db.write_tx(conn):
        ver = _check_base_version(conn, base_ver)  # 整批采用同一完整身份状态
        keys = [(r["source"], r["source_station_id"]) for r in rows]
        station_rows = conn.execute(
            "SELECT * FROM source_station WHERE "
            + " OR ".join("(source=? AND source_station_id=?)" for _ in keys),
            [v for key in keys for v in key],
        ).fetchall() if keys else []
        station_map = {(r["source"], r["source_station_id"]): r for r in station_rows}

        unknown = sorted({k for k in keys if k not in station_map})
        if unknown:
            raise ServiceError(
                "以下来源站尚未登记，整批拒绝："
                + "；".join(f"{s}/{i}" for s, i in unknown)
            )

        in_file_seen: set[tuple[str, str, str]] = set()
        for r in rows:
            k = (r["source"], r["source_station_id"], r["obs_time"])
            if k in in_file_seen:
                raise ServiceError(
                    f"CSV 内部存在重复观测：{k[0]} / {k[1]} @ {k[2]}，整批拒绝"
                )
            in_file_seen.add(k)

        # 逐条主键查重（单批规模有限，且在事务内）；任一冲突整批拒绝
        conflicts = []
        for s, sid, t in sorted(in_file_seen):
            hit = conn.execute(
                "SELECT 1 FROM observation WHERE source=? AND source_station_id=? "
                "AND obs_time=? LIMIT 1",
                (s, sid, t),
            ).fetchone()
            if hit:
                conflicts.append(f"{s}/{sid}@{t}")
        if conflicts:
            raise ServiceError(
                "以下观测已入仓（同来源/源站号/时刻），整批拒绝："
                + "；".join(conflicts[:10])
                + (" …" if len(conflicts) > 10 else "")
            )

        membership = db.current_membership(conn)
        batch_row = conn.execute(
            "SELECT COALESCE(MAX(batch_id), 0) + 1 AS b FROM observation"
        ).fetchone()
        batch_id = int(batch_row["b"])
        now = _now()
        involved: set[int] = set()
        payload = []
        for r in rows:
            pk = int(station_map[(r["source"], r["source_station_id"])]["id"])
            uid = membership[pk]
            involved.add(uid)
            payload.append((
                batch_id, r["source"], r["source_station_id"], r["obs_time"],
                float(r["temperature"]), uid, ver, now,
            ))
        conn.executemany(
            "INSERT INTO observation(batch_id, source, source_station_id, obs_time, "
            "temperature, snapshot_unified_id, ingest_ver, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            payload,
        )

    return {
        "batch_id": batch_id,
        "ingested": len(rows),
        "identity_version": ver,
        "involved_unified_ids": sorted(involved),
    }


def list_observations(conn: sqlite3.Connection, *, source: str | None = None,
                      source_station_id: str | None = None,
                      unified_id: int | None = None,
                      batch_id: int | None = None,
                      limit: int = 100, offset: int = 0) -> dict:
    where, params = [], []
    if source:
        where.append("source=?")
        params.append(source)
    if source_station_id:
        where.append("source_station_id=?")
        params.append(source_station_id)
    if batch_id is not None:
        where.append("batch_id=?")
        params.append(int(batch_id))
    if unified_id is not None:
        # 按“当前统一号”过滤：解析该当前组的来源站主键，再在 SQL 层限定
        g = db.get_current_group(conn, int(unified_id))
        if g is None:
            return {"total": 0, "limit": limit, "offset": offset, "items": []}
        member_pks = db.group_member_pks(conn, int(unified_id))
        if not member_pks:
            return {"total": 0, "limit": limit, "offset": offset, "items": []}
        marks = ",".join("?" for _ in member_pks)
        pk_rows = conn.execute(
            f"SELECT source, source_station_id FROM source_station "
            f"WHERE id IN ({marks})", member_pks,
        ).fetchall()
        pair_clauses = " OR ".join("(source=? AND source_station_id=?)"
                                   for _ in pk_rows)
        where.append("(" + pair_clauses + ")")
        for r in pk_rows:
            params.extend([r["source"], r["source_station_id"]])
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    total = int(conn.execute(
        f"SELECT COUNT(*) AS c FROM observation{where_sql}", params
    ).fetchone()["c"])
    rows = conn.execute(
        f"SELECT * FROM observation{where_sql} "
        f"ORDER BY obs_time DESC, obs_id DESC LIMIT ? OFFSET ?",
        params + [int(limit), int(offset)],
    ).fetchall()

    pk_rows = conn.execute("SELECT id, source, source_station_id FROM source_station")
    pk_map = {(r["source"], r["source_station_id"]): int(r["id"]) for r in pk_rows}
    membership = db.current_membership(conn)

    items = []
    for r in rows:
        pk = pk_map.get((r["source"], r["source_station_id"]))
        current_uid = membership.get(pk) if pk is not None else None
        items.append({
            "obs_id": int(r["obs_id"]),
            "batch_id": int(r["batch_id"]),
            "source": r["source"],
            "source_station_id": r["source_station_id"],
            "obs_time": r["obs_time"],
            "temperature": r["temperature"],
            "snapshot_unified_id": int(r["snapshot_unified_id"]),
            "current_unified_id": current_uid,
            "identity_changed": current_uid != int(r["snapshot_unified_id"]),
            "ingest_ver": int(r["ingest_ver"]),
            "created_at": r["created_at"],
        })
    return {"total": total, "limit": limit, "offset": offset, "items": items}
