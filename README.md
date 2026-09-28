# 微孔板样本谱系追踪 API

基于 **Python · FastAPI · SQLite** 的微孔板谱系台账服务：

- 登记孔位中的初始样本与体积；
- 一批移液操作**按提交顺序在单个事务中原子执行**：每次吸取按来源孔的组成占比带走各初始样本，目标孔按现有液体与新增液体重新混合；**任一步体积不足，整批回滚**；
- 按孔查询当前体积、各初始样本占比，以及涉及该孔的完整批次记录；
- **误操作批次撤销**：批次成功提交时在同一事务保存其首次触及各孔的完整批前快照；在孔位未被后续操作（补液或仍生效的已提交批次）改动的前提下，可凭快照原子恢复全部触及孔，并写入关联原批次的撤销审计（原批次保留不删除）；
- 所有体积运算使用 `Decimal`（统一量化到小数点后 9 位），不使用 float；
- 通过 `request_key` 幂等去重：相同键重复提交不会重复执行，直接返回首个结果（失败批次同样被记录并消耗该键；撤销请求同样幂等）。

## 快速启动（Docker，唯一需要的命令）

```bash
docker compose up --build
```

启动后在本机访问：

| 地址 | 说明 |
| --- | --- |
| http://localhost:8000/health | 健康检查 |
| http://localhost:8000/docs | Swagger 交互式文档（可直接试调） |
| http://localhost:8000/redoc | ReDoc 文档 |

SQLite 数据落在命名卷 `lineage-data` 中，容器重启不丢失；`docker compose down -v` 可清空。

## 本地开发

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload --port 8000   # 同样监听本机 8000 端口
pytest                                       # 运行测试
```

数据库文件默认在 `./data/lineage.db`，可用环境变量 `LINEAGE_DB` 指定其他路径。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/wells` | 登记孔位初始样本与体积（同孔重复登记视为补液混匀） |
| `GET` | `/wells/{well}` | 孔位当前体积 + 各初始样本的量与占比 |
| `GET` | `/wells/{well}/batches` | 涉及该孔的完整批次记录 |
| `POST` | `/batches` | 原子执行一批移液（`request_key` 幂等） |
| `GET` | `/batches` / `/batches/{id}` | 全部 / 单个批次记录（含失败批次） |
| `POST` | `/batches/{id}/reversals` | 撤销已提交批次（`request_key` 幂等；不满足前提返回 409 并列出阻塞孔位） |

孔位标签为「字母 + 数字」（如 `A1`、`B12`），不区分大小写；本服务按单块板建模。

## 调用示例

```bash
# 1. 登记初始样本：A1 加入 100 µL 样本 S1，A2 加入 60 µL 样本 S2
curl -s -X POST http://localhost:8000/wells \
  -H 'Content-Type: application/json' \
  -d '{"well": "A1", "sample": "S1", "volume": "100"}'
curl -s -X POST http://localhost:8000/wells \
  -H 'Content-Type: application/json' \
  -d '{"well": "A2", "sample": "S2", "volume": "60"}'

# 2. 原子批量移液：A1 -> B1 40，A2 -> B1 30（B1 混合为 S1:S2 = 4:3）
curl -s -X POST http://localhost:8000/batches \
  -H 'Content-Type: application/json' \
  -d '{
        "request_key": "run-2026-09-22-001",
        "operations": [
          {"source": "A1", "destination": "B1", "volume": "40"},
          {"source": "A2", "destination": "B1", "volume": "30"}
        ]
      }'

# 3. 查询 B1 的体积与谱系占比
curl -s http://localhost:8000/wells/B1
# => {"well":"B1","volume":"70","composition":[
#      {"sample":"S1","amount":"40","fraction":"0.571428571"},
#      {"sample":"S2","amount":"30","fraction":"0.428571429"}]}

# 4. 体积不足 -> 整批回滚，返回 409，状态不变
curl -s -X POST http://localhost:8000/batches \
  -H 'Content-Type: application/json' \
  -d '{
        "request_key": "run-2026-09-22-002",
        "operations": [
          {"source": "B1", "destination": "C1", "volume": "50"},
          {"source": "B1", "destination": "D1", "volume": "50"}
        ]
      }'
# => 409 {"detail":{"message":"孔位 B1 体积不足：可用 20，请求吸取 50", ...}}

# 5. 重复提交同一 request_key -> 200 且 replayed=true，不会重复执行
# 6. 查询涉及 B1 的全部批次
curl -s http://localhost:8000/wells/B1/batches

# 7. 撤销误操作批次（未被后续操作碰过触及孔时）：液体原子回到批前
curl -s -X POST http://localhost:8000/batches/1/reversals \
  -H 'Content-Type: application/json' \
  -d '{"request_key":"undo-2026-09-22-001"}'
# => 201 {"reversal_id":1,"batch_id":1,"restored_wells":[{"well":"A1",...},{"well":"B1",...}], ...}
# 若 B1 之后被补液/被其他已提交批次改动：
# => 409 {"detail":{"code":"blocked","blocked_wells":[{"well":"B1","blockers":[
#        {"type":"batch","batch_id":2,...},{"type":"top_up",...}]}], ...}}  液体状态不变
# 同一 request_key 重放撤销 -> 200 且 replayed=true；同键换目标批次 -> 409
```

## 设计要点

- **Decimal 精度**：体积与组分量在库中以 TEXT 存 Decimal 字符串，运算全部走 `decimal.Decimal`，结果统一量化到 9 位小数（`ROUND_HALF_UP`），接口返回值中的数字均为字符串以避免 JSON 精度损失。
- **按比例吸取（整数单位最大余量法）**：从孔中吸取体积 `v`（孔总量 `V`，均以 9 位小数的最小计量单位 QUANTUM 计）时，每个初始样本先分得比例份额 `amount × v/V` 的整数单位，剩余单位按小数余量从大到小分配；余量相同按样本 ID 升序。任何样本分得的单位数不超过其原有量，且各样本带走量之和恰好等于 `v`——保证每一步之后来源孔与目标孔都满足「组分量之和 = 体积」；目标孔将各组分量累加，即视为立即完美混合。
- **原子性**：一个批次的所有操作在单个 SQLite 事务中按序执行，任一步 `InsufficientVolume` 即回滚；失败批次以 `status="failed"` 落库（操作明细随回滚清空），方便审计。
- **幂等**：`batches.request_key` 有唯一约束。重复提交返回首个批次记录（`replayed=true`，HTTP 200）；并发下同键请求由唯一约束兜底，不会执行两次。
- **批次撤销**：
  - 每个已提交批次在执行的同一事务内，于每一步执行前保存其来源/目标孔「首次触及前」的完整快照（孔是否存在、体积、各初始样本量，`batch_well_snapshots` / `snapshot_composition`）；批次失败回滚时快照一并回滚，不产生可撤销记录。
  - `POST /batches/{id}/reversals` 在一个 `BEGIN IMMEDIATE` 写事务内先做全部前提检查再恢复：批次须 `committed`、尚未撤销、拥有快照，且每个触及孔之后都没有补液（`registrations` 审计）、也没有仍生效的已提交批次触碰；后续批次若自身已被撤销（效果已回滚）则不再阻塞。检查与恢复在同一事务，冲突时只返回 409，液体一行不改。
  - 恢复逐孔覆盖：批前不存在的孔（如本批新建的目标孔）整孔删除，存在的孔把体积与组成覆盖回快照值；随后写入关联原批次的撤销审计（`reversals`，独立的 request_key 幂等空间），**原批次与其操作明细、快照均保留**，批次记录新增 `reversed` / `reversal` 字段。
  - 冲突响应按孔列出阻塞项及后续操作（`type=batch` 给出批次与具体一步操作，`type=top_up` 给出补液登记）；冲突不落审计、不消耗撤销键，排除阻塞后可用同一键重试。
  - **上线前已存在的老批次没有快照**，撤销明确返回 `409 snapshot_missing`，绝不凭当前状态猜测恢复。
  - 补液登记同样在 `BEGIN IMMEDIATE` 事务中写入「登记时刻已提交批次的最大 id」，保证补液与批次的先后判定在并发下严格有序。
- **状态码**：登记/批次/撤销成功 `201`；幂等重放 `200`；体积不足或撤销前提不符 `409`；孔位/批次不存在 `404`；参数非法 `422`。

## 项目结构

```
app/
  main.py     # FastAPI 路由
  ledger.py   # 谱系台账核心逻辑（Decimal 运算、事务内批量执行）
  db.py       # SQLite 连接与建表
  schemas.py  # 请求体验证
tests/        # pytest 端到端测试
Dockerfile / compose.yaml
```
