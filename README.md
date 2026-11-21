# 抄表簿

骨架：抄表读数装载 → 估抄/实抄对齐 → 差额处置 → 账期封印。

- 前端 http://127.0.0.1:6010（勿用 6000，浏览器会拦）
- 接口 http://127.0.0.1:11000

## 批量装载架构（全局唯一口径）

系统只贯彻**架构一：以正式∪隔离行集合为准**——库内落库集合是唯一事实源，回执是它的审计投影，
不存在「回执分项自成事实源」的第二套口径。`settings.load_mode` 只决定整批命运：

- `atomic`（缺省）：任一行校验失败 → 整批回滚，正式表零写入、不进隔离；回执 `failed` 列失败行。
- `split`：合法行写正式表，非法行写 `load_quarantine`；回执 `partial` 分 success/fail。
- 账期封印是账期级硬闸：任一行指向 sealed 账期 → 两种架构都整批 `rejected`，不写正式表也不进隔离。
- 对齐只吃正式表中带 `receipt_id` 的可解释行；无溯源章的孤儿行直接拒绝对齐，隔离行不可能进 matched/variances。
- 所有装载/对齐/封印在 `BEGIN IMMEDIATE` 单事务内复查账期状态与装载模式并提交，异常即回滚。

### 五链收口（回执 / 正式 / 隔离 / 总览 / 对齐输入）

每条装载事务提交前执行**落库重放**（`loader._replay_receipt`），以本批 `receipt_id` 回查正式表与隔离表：

- 正式行键集合必须等于回执宣称成功键集合（缺行补不了、幽灵行留不下）；
- 隔离行集合必须等于应隔离行（split 失败行；atomic/rejected 隔离必须为空，多报/少报都回滚）；
- `total = success + fail` 必须闭合；`failed`/`rejected` 回执在正式表必须零行；
- 任一不符 → 整事务回滚（含半截正式行与回执本身），返回 500，杜绝「回执失败但半截正式读数」。

`GET /api/health` 与 `GET /api/overview` 返回 `five_chain_aligned` 全局重放结果：
正式表总数 = 回执 success 累计；隔离区总数 = split 回执 fail 累计；正式行无孤儿回执。
总览装载计数一律跟库，不跟回执粉刷值。旧库迁移会把历史行回填到合成回执 `R-LEGACY-*`，升级即对齐。

接口：`POST /api/estimates/batch`、`POST /api/actuals/batch`（body `{"rows":[...]}`），
`GET /api/quarantine`、`GET /api/receipts`，`PUT /api/settings/load_mode`。
回执 `success_count` 累计 = 正式表装载来源笔数；split 的 `fail_count` = 隔离区笔数，原因码两边可对上。

## 测试

```
cd backend
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests -q
```

## 启动

```powershell
cd backend
C:\Users\18435\AppData\Roaming\uv\python\cpython-3.11.15-windows-x86_64-none\python.exe -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\uvicorn app.main:app --host 127.0.0.1 --port 11000 --app-dir .
```

```powershell
cd frontend
npm install
npm run dev -- --host 127.0.0.1 --port 6010
```
