# 跨海光缆故障与抢修协调

纯Python标准库实现的跨海光缆故障与抢修协调原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、海况、船机许可、备缆窗口和接续质量和冲突检查。
- `src/formation.py`：抢修编队规则（出海窗口、资源互斥、待命缺口计算）。
- `src/repository.py`：SQLite建表、事务和查询（编队、资源、幂等键、回填、恢复）。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算、失败场景、编队并发/恢复/回填和HTTP集成测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8330
```

默认端口为`8330`，默认数据库位于项目目录。服务启动时自动建表；检测到旧版`records`表会自动加列并进入"待回填"状态。

## 抢修编队模型

一次编队把**多张故障单、一个出海时段和该航次用缆量**收进同一航次：

- 用缆量 = 编队内各故障`required_spare_km`之和。
- 三类资源：船机（vessel）、接续班组（crew）、备缆批次（spare_batch）。
- 同一时间窗（半开区间，首尾相接不算冲突）内，一艘船、一个班组、一批备缆只服务一个编队。
- 三类资源齐备 → `proposed`；任一缺口 → 保留`standby`待命方案，并在`gaps`逐项列出缺什么（窗口内无空闲船/班组、无空闲批次或批次余量不足）。
- 编队状态：`proposed` / `standby` / `confirmed` / `completed` / `cancelled` / `invalidated`。

### 建议时段与失效重算

- 建议出海时段带全局版本`epoch`（`GET /api/schedule`）。
- `POST /api/schedule/rebuild`（repair_manager）使epoch加一，所有未确认方案（proposed/standby）批量置为`invalidated`并释放故障绑定；已确认方案不受影响。
- 失效方案通过`replan`按新窗口重算，旧编队保留并以`superseded_by`指向新编队。

### 并发提交与幂等确认

- 编队提交必须带`client_key`：两名调度员同时提交同一编队时只有一版落库，后到者拿到先到版本（HTTP 200，`deduplicated=true`）。
- 确认必须带`idempotency_key`：占用三类资源与扣减备缆在**同一个`BEGIN IMMEDIATE`事务**内完成。服务崩溃后重放同一确认直接返回首次结果（`replayed=true`），不会重复占用窗口或重复扣减备缆。
- `POST /api/admin/recover`（admin/repair_manager）以confirmed/completed编队为唯一事实源重建占用表和批次余量，清除游离占用与漂移数据。

### 存量数据回填升级

- 旧库首次启动后，存量故障`formation_ready=0`，`GET /api/migration`显示`pending`。
- 回填完成前，存量故障**不能参加新编队**（409），但故障详情、列表、审计时间线照常可查；升级后新建的故障立即可用。
- `POST /api/admin/backfill`分批回填，每批补写`backfilled`审计事件，全部完成后状态为`completed`。

## 主要接口

故障记录（沿用原接口）：

- `GET /health`：健康检查。
- `GET /api/records` / `GET /api/records/{id}` / `GET /api/records/{id}/audit`。
- `GET /api/stats`。
- `POST /api/records`：`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：`{"expected_version":1,"data":{...}}`。

资源与备缆：

- `POST /api/resources`（repair_manager）：`{"data":{"rtype":"vessel|crew","code":"CS-1","name":"..."}}`。
- `GET /api/resources?type=vessel|crew`。
- `POST /api/spare-batches`（repair_manager）：`{"data":{"code":"B-1","name":"...","total_km":100}}`。
- `GET /api/spare-batches`。
- `GET /api/allocations?type=vessel|crew|spare_batch`：当前生效占用。

编队（新增角色`dispatcher`调度员）：

- `POST /api/formations`：`{"reference":"FM-1","client_key":"...","data":{"fault_ids":[1,2],"window_start":"2026-10-05T02:00:00Z","window_end":"2026-10-06T10:00:00Z"}}`。时间支持ISO-8601含`Z`后缀。
- `GET /api/formations?state=proposed` / `GET /api/formations/{id}` / `GET /api/formations/{id}/audit`。
- `POST /api/formations/{id}/confirm`：`{"data":{"idempotency_key":"..."}}`（dispatcher/repair_manager）。
- `POST /api/formations/{id}/replan`：`{"data":{"reference":"...","client_key":"...","window_start":"...","window_end":"..."}}`。
- `POST /api/formations/{id}/cancel`：`{"data":{"reason":"..."}}`（confirmed取消会释放占用并回补未用备缆）。
- `POST /api/formations/{id}/complete`（repair_manager）。
- `GET /api/schedule` / `POST /api/schedule/rebuild`（repair_manager）。

升级与恢复：

- `GET /api/migration`：回填状态与待处理数量。
- `POST /api/admin/backfill`（admin/repair_manager）：`{"data":{"batch_size":200}}`。
- `POST /api/admin/recover`（admin/repair_manager）：从完整编队重建占用与余量。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

覆盖完整抢修流程、规则计算、待命缺口、窗口互斥、并发提交去重、并发确认互斥、确认幂等与崩溃重放、调表失效重算、存量回填升级、取消释放资源以及HTTP集成。
