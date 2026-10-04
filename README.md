# 跨海光缆故障与抢修协调

纯Python标准库实现的跨海光缆故障与抢修协调原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：故障单状态转换、海况、船机许可、备缆窗口和接续质量和冲突检查。
- `src/formation_rules.py`：抢修编队规则——用缆量汇总、资源缺口评估与方案状态决策。
- `src/repository.py`：SQLite建表（含老库升级回填标记）、事务和查询。
- `src/service.py`：故障单与编队用例编排、权限检查、乐观并发、幂等确认和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算、失败场景与抢修编队台测试。

## 抢修编队台

台风后相邻海缆多处中断时，编队台把故障单、出海时段和用缆量收进同一编队统一出海：

- **编队聚合**：一个编队包含多张故障单、海域建议出海时段（携带版本号）、船/接续班组/备缆批次，用缆量按故障单`required_spare_km`汇总。
- **时间窗互斥**：同一时间窗（半开区间，首尾相接不算重叠）内，一艘船、一个班组、一批备缆只服务一个**已确认**编队；备缆批次同时按库存扣减占用量。
- **待命方案**：资源不齐（未指派、被占、备缆余量不足）时方案保留为`standby`，返回体`gaps`列清每个缺口；调整资源后`replan`重算。
- **时段变更失效**：建议出海时段更新即升版本，该海域所有未确认编队自动置`invalidated`，必须`replan`后才能确认；已确认编队与资源占用不受影响。
- **并发与崩溃恢复**：编队`reference`唯一（两名调度员同时提交同一编队只有一版生效）；确认带`expected_version`乐观锁与`idempotency_key`，预订写入与状态翻转在单事务内提交。服务崩溃后从完整编队恢复，重放同一确认不重复占用或扣减备缆。
- **升级回填**：既有数据库启动时为`records`增加`formation_ready`标记，老数据默认未回填；回填完成前不能创建/重算编队（返回409与进度），但故障单详情、列表照常可查。新库（无历史数据）回填状态直接为完成。

编队状态：`proposed`（待确认）→ `confirmed`（已确认占用资源）→ `completed`；任一方案可因缺口为`standby`、因时段变更为`invalidated`；`replan`按最新时段重算，`cancel`取消并释放占用。

## 启动

```bash
python3 app.py --db ./data.db --port 8330
```

默认端口为`8330`，默认数据库位于项目目录。服务启动时自动建表并升级老库。

## 主要接口

故障单（既有）：

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情（回填期间仍可查，含`formation_ready`标记）。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。

抢修编队台：

- `GET /api/backfill/status`：回填进度`{total,ready,pending,status}`。
- `POST /api/backfill/run`：管理员分批回填，请求体`{"batch_size":200}`，幂等可重跑。
- `POST /api/resources`：登记/更新资源，`data`为`{"type":"vessel|crew|cable_batch","code":"...","name":"...","length_km":30}`（备缆批次才需要长度）。
- `GET /api/resources?type=`：资源台账，备缆批次含`allocated_km`与`remaining_km`。
- `POST /api/advisories/{area}`：发布/更新海域建议时段，`data`为`{"window_start":"ISO8601","window_end":"ISO8601"}`；窗口变化时返回`invalidated_formation_ids`。
- `GET /api/advisories`：建议时段列表。
- `POST /api/formations`：编排编队，`{"reference":"GRP-1","data":{"area":"东海","record_ids":[1,2],"vessel_code":"CS-1","crew_code":"T-A","cable_batch_code":"BX"}}`，资源可留空进入待命。
- `GET /api/formations?state=&area=` / `GET /api/formations/{id}`：编队列表/详情（含`record_ids`、`gaps`、`spare_demand_km`）。
- `POST /api/formations/{id}/actions/replan`：按最新时段重算，`{"expected_version":2,"data":{"vessel_code":"CS-2"}}`（资源字段可部分覆盖，传`null`取消指派）。
- `POST /api/formations/{id}/actions/confirm`：确认并占用资源，`{"expected_version":2,"idempotency_key":"uuid"}`；仍有缺口时保持`standby`不占用。
- `POST /api/formations/{id}/actions/cancel`：取消（释放备缆占用），`{"data":{"reason":"..."}}`。
- `POST /api/formations/{id}/actions/complete`：已确认编队标记完成。
- `GET /api/formations/{id}/events`：编队事件时间线。

权限：除`/health`和`/`外需`X-User-Id`、`X-Role`，可选`X-Org`。调度员`dispatcher`负责建议时段与编队编排，`repair_manager`负责资源台账与故障单审批，回填仅`admin`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

覆盖故障单完整流程、规则计算、权限与版本冲突，以及编队的时间窗互斥、待命缺口、时段改版失效、并发提交/确认、幂等重放（含崩溃后重建）、取消释放和老库升级回填门禁。
