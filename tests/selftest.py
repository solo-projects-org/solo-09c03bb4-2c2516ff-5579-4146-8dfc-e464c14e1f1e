"""端到端自测：通过 FastAPI TestClient 验证全部关键业务规则。

运行：python3 tests/selftest.py
使用临时数据库，不影响 /data 下的真实数据。
"""

from __future__ import annotations

import io
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="station_selftest_")
os.environ["STATION_DB"] = os.path.join(_TMP, "test.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app import database as db  # noqa: E402

PASS, FAIL = 0, 0
USED_UNIFIED_IDS: set[int] = set()


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {detail}")


def csv_upload(client: TestClient, url: str, content: str, base_ver=None):
    files = {"file": ("data.csv", content.encode("utf-8"), "text/csv")}
    data = {}
    if base_ver is not None:
        data["base_ver"] = str(base_ver)
    return client.post(url, files=files, data=data)


def collect_ids(state: dict) -> set[int]:
    ids = {g["unified_id"] for g in state["groups"]}
    USED_UNIFIED_IDS.update(ids)
    return ids


def gbk_upload(client: TestClient, url: str, content: str, base_ver=None):
    files = {"file": ("data.csv", content.encode("gbk"), "text/csv")}
    data = {}
    if base_ver is not None:
        data["base_ver"] = str(base_ver)
    return client.post(url, files=files, data=data)


def main() -> int:
    with TestClient(app) as client:
        print("== 1. 健康检查与初始状态 ==")
        r = client.get("/api/health")
        check("健康检查 200", r.status_code == 200, r.text)
        state = client.get("/api/state").json()
        check("初始版本 v1", state["version"] == 1)
        check("初始 0 站", state["source_station_count"] == 0)
        check("上限 100", state["cap"] == 100)

        print("== 2. 上传来源站：每个来源站获得独立统一号 ==")
        csv1 = "来源,源站号,站名\nA,s1,甲站\nA,s2,乙站\nB,s3,丙站\n"
        r = csv_upload(client, "/api/stations/upload", csv1, base_ver=1)
        check("上传 3 站成功", r.status_code == 200, r.text)
        body = r.json()
        check("新增 3", body["inserted"] == 3, str(body))
        check("独立号 [1,2,3]", body["created_unified_ids"] == [1, 2, 3], str(body))
        state = client.get("/api/state").json()
        check("状态升至 v2", state["version"] == 2)
        check("当前 3 组", state["current_group_count"] == 3)
        first_ids = collect_ids(state)
        check("每组恰好 1 个来源站", all(len(g["members"]) == 1 for g in state["groups"]))

        print("== 3. 重复上传：站名一致跳过、不一致整批拒绝 ==")
        r = csv_upload(client, "/api/stations/upload", "来源,源站号,站名\nA,s1,甲站\n", base_ver=2)
        check("一致重复被跳过", r.status_code == 200 and r.json()["inserted"] == 0, r.text)
        r = csv_upload(client, "/api/stations/upload", "来源,源站号,站名\nA,s1,错名站\n", base_ver=2)
        check("站名冲突整批拒绝 400", r.status_code == 400, r.text)
        check("文件内重复拒绝", csv_upload(
            client, "/api/stations/upload",
            "来源,源站号,站名\nA,s9,重复\nA,s9,重复\n", base_ver=2).status_code == 400)
        check("缺列表头拒绝", csv_upload(
            client, "/api/stations/upload", "来源,源站号\nA,s1\n").status_code == 400)
        check("GBK 编码可解析", gbk_upload(
            client, "/api/stations/upload",
            "来源,源站号,站名\nB,s4,丁站\n", base_ver=2).status_code == 200)
        state = client.get("/api/state").json()
        check("现有 4 站 4 组", state["source_station_count"] == 4
              and state["current_group_count"] == 4, str(state))
        collect_ids(state)

        print("== 4. 归并现站：受影响组获得全新号，旧号退役 ==")
        r = client.post("/api/groups/merge", json={"unified_ids": [1, 2], "base_ver": 2})
        check("归并 1+2 被拒（版本过期 v2≠当前）", r.status_code == 409, r.text)
        check("409 返回当前版本", r.json().get("current_version") is not None)
        r = client.post("/api/groups/merge", json={"unified_ids": [1, 2], "base_ver": 3})
        check("归并 1+2 成功", r.status_code == 200, r.text)
        check("新号为 5（不复用 1/2）", r.json()["new_unified_id"] == 5, str(r.json()))
        check("只选 1 个拒绝", client.post(
            "/api/groups/merge", json={"unified_ids": [5], "base_ver": 4}).status_code == 400)
        check("退役号不能再归并", client.post(
            "/api/groups/merge", json={"unified_ids": [1, 3], "base_ver": 4}).status_code == 400)
        state = client.get("/api/state").json()
        collect_ids(state)
        check("归并后组数 3", state["current_group_count"] == 3, str(state))
        g5 = next(g for g in state["groups"] if g["unified_id"] == 5)
        check("新组含 2 个来源站", len(g5["members"]) == 2)

        print("== 5. 拆分：按来源站拆分，每个新组分配全新号 ==")
        pks = {m["source"] + m["source_station_id"]: m["source_station_pk"] for m in g5["members"]}
        pk_s1, pk_s2 = pks["As1"], pks["As2"]
        r = client.post("/api/groups/split", json={
            "unified_id": 5, "partitions": [[pk_s1], [pk_s2]], "base_ver": 4})
        check("拆分成功", r.status_code == 200, r.text)
        new_ids = r.json()["new_unified_ids"]
        check("拆分产生两个全新号 [6,7]", new_ids == [6, 7], str(new_ids))
        check("拆分成员不全被拒绝", client.post(
            "/api/groups/split", json={"unified_id": 6,
            "partitions": [[pk_s1], [999]], "base_ver": 5}).status_code == 400)
        check("单组拆分拒绝", client.post(
            "/api/groups/split", json={"unified_id": 6,
            "partitions": [[pk_s1]], "base_ver": 5}).status_code == 400)
        state = client.get("/api/state").json()
        collect_ids(state)
        check("拆分后回到 4 组", state["current_group_count"] == 4)
        check("统一号全局不复用（1..7 均曾出现）",
              USED_UNIFIED_IDS == {1, 2, 3, 4, 5, 6, 7}, str(USED_UNIFIED_IDS))

        print("== 6. 旧号查询：按产生时来源站查当前归属，跨多站标歧义 ==")
        # 把 6（A/s1）与 3（B/s3）归并 -> 8；7（A/s2）保持独立
        r = client.post("/api/groups/merge", json={"unified_ids": [6, 3], "base_ver": 5})
        check("再归并 6+3 成功", r.status_code == 200, r.text)
        check("新号 8", r.json()["new_unified_id"] == 8, str(r.json()))
        state = client.get("/api/state").json()
        collect_ids(state)
        cur_map = {m["source"] + m["source_station_id"]: g["unified_id"]
                   for g in state["groups"] for m in g["members"]}
        check("s1 现归 8", cur_map["As1"] == 8)
        check("s2 现归 7", cur_map["As2"] == 7)

        r = client.get("/api/query/unified/5")  # 旧号 5 曾含 s1+s2
        d = r.json()
        check("旧号 5 可查询", r.status_code == 200, r.text)
        check("旧号标记退役", d["group"]["current"] is False)
        check("跨两个现站 => 歧义", d["ambiguous"] is True, str(d))
        check("列全两个当前归属 {7,8}",
              {g["unified_id"] for g in d["current_attribution"]} == {7, 8}, str(d))
        r = client.get("/api/query/unified/1")  # 更老的号，仅 s1
        d = r.json()
        check("更老旧号 1 归属唯一无歧义", d["ambiguous"] is False
              and [g["unified_id"] for g in d["current_attribution"]] == [8], str(d))
        check("不存在号码 404", client.get("/api/query/unified/999").status_code == 404)

        r = client.get("/api/query/source", params={"source": "A", "source_station_id": "s1"})
        d = r.json()
        check("来源站可追溯全部历史号", r.status_code == 200 and
              [h["unified_id"] for h in d["group_history"]] == [1, 5, 6, 8], str(d))
        check("当前统一号为 8", d["current_unified_id"] == 8)
        check("未知来源站 404", client.get(
            "/api/query/source", params={"source": "X", "source_station_id": "x"}).status_code == 404)

        r = client.get("/api/groups/lineage/5")
        d = r.json()
        check("沿革包含上下游", set(d["ancestor_ids"]) == {1, 2}
              and set(d["descendant_ids"]) == {6, 7, 8}, str(d))

        print("== 7. 观测入仓：按当前归属；历史保留接纳时统一号 ==")
        ver = client.get("/api/state").json()["version"]
        obs = ("来源,源站号,UTC时刻,温度\n"
               "A,s1,2026-11-15T00:00:00Z,18.2\n"
               "A,s2,2026-11-15T00:00:00Z,20.1\n"
               "B,s4,2026-11-15T00:00:00Z,9.9\n")
        r = csv_upload(client, "/api/observations/ingest", obs, base_ver=ver)
        check("首批 3 条入仓", r.status_code == 200 and r.json()["ingested"] == 3, r.text)
        check("批次号 1", r.json()["batch_id"] == 1)
        check("入仓采用当前版本", r.json()["identity_version"] == ver)
        items = client.get("/api/observations", params={"limit": 100}).json()["items"]
        snap = {(o["source"], o["source_station_id"]): o for o in items}
        check("s1 接纳时号=8", snap[("A", "s1")]["snapshot_unified_id"] == 8)
        check("s2 接纳时号=7", snap[("A", "s2")]["snapshot_unified_id"] == 7)
        check("当前归属与接纳时一致", all(not o["identity_changed"] for o in items))

        print("== 8. 交错演进：再拆分后历史观测显示新当前归属 ==")
        g8 = next(g for g in client.get("/api/state").json()["groups"]
                  if g["unified_id"] == 8)
        pk_s3 = next(m["source_station_pk"] for m in g8["members"]
                     if m["source_station_id"] == "s3")
        r = client.post("/api/groups/split", json={
            "unified_id": 8,
            "partitions": [[pk_s1], [pk_s3]],
            "base_ver": ver})
        check("拆 8 -> 两个新号", r.status_code == 200, r.text)
        new9, new10 = r.json()["new_unified_ids"]
        items = client.get("/api/observations", params={"limit": 100}).json()["items"]
        o_s1 = next(o for o in items if o["source"] == "A" and o["source_station_id"] == "s1")
        check("历史观测仍保留接纳时号 8", o_s1["snapshot_unified_id"] == 8)
        check("逐条显示当前新归属", o_s1["current_unified_id"] == new9, str(o_s1))
        check("身份变化被标记", o_s1["identity_changed"] is True)
        o_s2 = next(o for o in items if o["source"] == "A" and o["source_station_id"] == "s2")
        check("未受影响观测保持一致", o_s2["current_unified_id"] == 7
              and o_s2["identity_changed"] is False)

        print("== 9. 入仓失败不留半批 + 过期页面拒绝 ==")
        cur_ver = client.get("/api/state").json()["version"]
        bad_obs = ("来源,源站号,UTC时刻,温度\n"
                   "A,s1,2026-11-16T00:00:00Z,11.1\n"
                   "B,UNKNOWN,2026-11-16T00:00:00Z,22.2\n")
        before = client.get("/api/observations").json()["total"]
        r = csv_upload(client, "/api/observations/ingest", bad_obs, base_ver=cur_ver)
        check("未知来源站整批拒绝", r.status_code == 400, r.text)
        after = client.get("/api/observations").json()["total"]
        check("无半批残留（总数不变）", before == after)

        dup_obs = ("来源,源站号,UTC时刻,温度\n"
                   "A,s1,2026-11-15T00:00:00Z,18.2\n")  # 与已入仓主键冲突
        r = csv_upload(client, "/api/observations/ingest", dup_obs, base_ver=cur_ver)
        check("重复观测整批拒绝", r.status_code == 400, r.text)

        r = csv_upload(client, "/api/observations/ingest",
                       "来源,源站号,UTC时刻,温度\nA,s1,2026-11-16T01:00:00Z,12.0\n",
                       base_ver=cur_ver - 1)
        check("过期页面入仓被拒 409", r.status_code == 409, r.text)

        bad_time = "来源,源站号,UTC时刻,温度\nA,s1,2026-11-16 01:00:00,12.0\n"  # 无时区
        check("无时区时刻拒绝", csv_upload(
            client, "/api/observations/ingest", bad_time, base_ver=cur_ver).status_code == 400)
        bad_temp = "来源,源站号,UTC时刻,温度\nA,s1,2026-11-16T01:00:00Z,abc\n"
        check("非数字温度拒绝", csv_upload(
            client, "/api/observations/ingest", bad_temp, base_ver=cur_ver).status_code == 400)

        print("== 10. 操作沿革与 100 站上限 ==")
        ops = client.get("/api/operations").json()["operations"]
        check("沿革记录完整", len(ops) >= 6 and ops[0]["kind"] in {"split", "merge", "upload"})
        check("沿革含版本变迁", all("from_ver" in o and "to_ver" in o for o in ops))

        big = "来源,源站号,站名\n" + "\n".join(
            f"BULK,k{i},批量站{i}" for i in range(97))  # 现有 4 站 + 97 = 101
        r = csv_upload(client, "/api/stations/upload", big, base_ver=cur_ver)
        check("超过 100 站上限整批拒绝", r.status_code == 400, r.text)
        ok = "来源,源站号,站名\n" + "\n".join(
            f"BULK,k{i},批量站{i}" for i in range(96))  # 4 + 96 = 100
        r = csv_upload(client, "/api/stations/upload", ok, base_ver=cur_ver)
        check("恰好 100 站可接纳", r.status_code == 200 and
              client.get("/api/state").json()["source_station_count"] == 100, r.text)
        r = csv_upload(client, "/api/stations/upload",
                       "来源,源站号,站名\nBULK,extra,超额站\n",
                       base_ver=client.get("/api/state").json()["version"])
        check("101 站被拒", r.status_code == 400, r.text)

    print("== 11. 重启保留沿革与观测（重新初始化数据库连接）==")
    db_path = os.environ["STATION_DB"]
    db.init_db(db_path)  # 模拟重启：schema 幂等执行
    with TestClient(app) as client2:
        st = client2.get("/api/state").json()
        check("重启后 100 站仍在", st["source_station_count"] == 100)
        check("重启后身份版本保留", st["version"] >= 6)
        check("重启后观测保留", client2.get("/api/observations").json()["total"] == 3)
        check("重启后沿革保留", len(client2.get("/api/operations").json()["operations"]) >= 6)
        r = client2.get("/api/query/unified/5")
        check("重启后旧号仍可追溯", r.status_code == 200 and r.json()["ambiguous"] is True)

    print(f"\n结果：✅ {PASS} 通过，❌ {FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
