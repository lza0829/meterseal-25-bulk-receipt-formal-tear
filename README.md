# 抄表簿

骨架：抄表读数装载 → 估抄/实抄对齐 → 差额处置 → 账期封印。

- 前端 http://127.0.0.1:6010（勿用 6000，浏览器会拦）
- 接口 http://127.0.0.1:11000

## 批量装载架构（全局唯一口径）

`settings.load_mode` 决定整批命运，全系统只贯彻一种，可在「设置」页切换后用同一坏文件对照：

- `atomic`（缺省）：任一行校验失败 → 整批回滚，正式表零写入、不进隔离；回执 `failed` 列失败行。
- `split`：合法行写正式表，非法行写 `load_quarantine`；回执 `partial` 分 success/fail。
- 账期封印是账期级硬闸：任一行指向 sealed 账期 → 两种架构都整批 `rejected`，不写正式表也不进隔离。
- 对齐只吃正式表，隔离行不可能进 matched/variances。
- 所有装载/对齐/封印在 `BEGIN IMMEDIATE` 单事务内复查账期状态并提交，异常即回滚，杜绝「回执失败但半截正式读数」。

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
