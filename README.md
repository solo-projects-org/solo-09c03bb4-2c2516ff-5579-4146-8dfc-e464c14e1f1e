# 气象测站身份统一数仓

统一各来源测站身份、纠正误归并、让分析组手里的旧站号始终可追溯的 Web 服务。
技术栈：**FastAPI + SQLite（WAL）+ 原生 HTML/JS 单页前端 + Docker Compose**。

## 一、核心身份模型

| 概念 | 含义 |
|---|---|
| 来源站 | `(来源, 源站号)` 唯一，业务主键；全库至多 **100** 个 |
| 统一号 | 每个“站组（代）”的身份号，由全局序列单调分配，**一旦分配永不复用** |
| 当前组 / 退役组 | `current=1` 的组构成当前身份状态；归并/拆分后旧组退役（行保留），新组领新号 |
| 身份状态版本 `version` | 每次上传新站、归并、拆分都使版本 +1，用于乐观并发控制 |
| 观测快照号 | 观测入仓时同时保存**接纳时统一号**与入仓版本；当前归属查询时动态解析 |

关键规则：

1. **上传**：每条新来源站先分配一个独立统一号（单站组）；重复 `(来源,源站号)` 站名一致则跳过，不一致整批拒绝。
2. **归并现站**：≥2 个当前组 → 旧号全部退役，**新组分配一个全新统一号**。
3. **按来源站拆分**：一个当前组按来源站拆成 ≥2 个新组，成员必须与原组完全一致；**每个新组各得一个全新统一号**，旧号退役。
4. **旧号追溯**：查任意统一号（含退役号）时，按该号**产生时包含的来源站**解析当前归属；若这些来源站现跨多个当前站，全部列出并标注 **歧义**。
5. **观测入仓**：按入仓时刻的当前归属记录快照统一号；任何一行校验失败整批回滚（不留半批）。历史观测永久保留接纳时统一号，列表逐条显示当前统一号与“身份是否变化”。
6. **交错一致性**：归并/拆分/入仓都要求页面携带 `base_ver`。每个写操作在单个 `BEGIN IMMEDIATE` 事务内先核对版本——过期页面返回 **409**，因此一批观测只可能采用同一个完整身份状态。
7. **持久化**：SQLite 文件位于命名卷 `/data/stations.db`，容器重启后沿革、观测、版本全部保留。

## 二、目录结构

```
.
├── app/
│   ├── main.py          # FastAPI 路由与异常处理
│   ├── service.py       # 领域服务：上传/归并/拆分/追溯/入仓（全部事务化）
│   ├── database.py      # SQLite 连接、schema、序列与事务辅助
│   ├── csv_utils.py     # CSV 解析（UTF-8/GBK、表头别名、UTC 时刻校验）
│   ├── errors.py        # 业务错误（含 409 过期状态）
│   └── static/index.html# 单页网页（上传/分组调整/新旧号查询/观测/沿革）
├── tests/selftest.py    # 71 项端到端自测（TestClient，临时库）
├── samples/             # 示例 CSV
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 三、启动

### Docker Compose（推荐）

```bash
docker compose up --build -d
# 打开 http://localhost:8000
docker compose logs -f
docker compose down          # 停止；命名卷 station-data 保留数据
```

数据保存在 Docker 命名卷 `station-data`（容器内 `/data/stations.db`）。
健康检查：`GET http://localhost:8000/api/health`。

### 本地运行

```bash
pip install -r requirements.txt
STATION_DB=./data/stations.db uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 四、CSV 格式

来源站 CSV（表头支持中英文别名，编码 UTF-8 或 GBK）：

```csv
来源,源站号,站名
CMA,58362,上海宝山站
EC,EC-SH-01,上海宝山观测点
```

观测 CSV（时刻必须带时区，`Z` 或 `+00:00`，无时区一律拒绝）：

```csv
来源,源站号,UTC时刻,温度
CMA,58362,2026-11-15T00:00:00Z,18.2
```

## 五、HTTP API 摘要

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/state` | 当前身份状态（版本、上限、所有当前组及成员） |
| POST | `/api/stations/upload` | multipart 上传来源站 CSV，可带 `base_ver` |
| POST | `/api/groups/merge` | `{unified_ids:[...], base_ver}` 归并现站 |
| POST | `/api/groups/split` | `{unified_id, partitions:[[来源站pk...],...], base_ver}` 拆分 |
| GET | `/api/query/unified/{id}` | 查统一号（含旧号）当前归属，跨多站标歧义 |
| GET | `/api/query/source?source=&source_station_id=` | 来源站的完整统一号沿革 |
| GET | `/api/groups/lineage/{id}` | 统一号上下游沿革图 |
| GET | `/api/operations` | 全部操作沿革（重启保留） |
| POST | `/api/observations/ingest` | multipart 上传观测 CSV，必须带 `base_ver` |
| GET | `/api/observations` | 观测列表（快照号 + 当前归属 + 变化标记，支持过滤分页） |
| GET | `/api/health` | 健康检查 |

错误响应统一为 `{"error": "中文原因", "current_version": n}`，版本冲突 HTTP 409。

## 六、自测

```bash
python3 tests/selftest.py     # 71 项检查，使用临时数据库，不影响真实数据
```

覆盖：独立发号、100 站上限、重复/冲突整批拒绝、GBK、归并/拆分发新号且不复用、
旧号歧义追溯、来源站沿革、入仓快照与当前归属、交错后身份变化标记、
409 过期拒绝、并发同版本只有一个成功、失败不留半批、重启持久化。

## 七、设计取舍

- **组即“代”**：组成员关系只增不改，归并/拆分通过退役旧组+新建新组实现，沿革天然完整，旧号永远可解析。
- **发号器与版本号分离**：`id_seq` 只在真正创建新组时推进，回滚的事务不消耗号码；号码不复用由单调序列保证。
- **乐观锁 + 立即写事务**：WAL 模式下 `BEGIN IMMEDIATE` 串行化写操作，`base_ver` 不匹配即 409，兼顾正确性与无锁读取。
- **观测不做物理迁移**：只存快照号，当前归属由查询时的成员关系动态计算，任何历史时刻的“接纳身份”都不会被后续归并/拆分篡改。
