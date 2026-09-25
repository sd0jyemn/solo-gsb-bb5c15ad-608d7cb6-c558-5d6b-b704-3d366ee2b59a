# 自适应测验选题 API（FastAPI + SQLite）

基于 **2PL IRT（项目反应理论）** 的计算机自适应测验（CAT）后端：

- 题库保存每题的**难度 `b`、区分度 `a`、曝光次数 `exposure_count`**；
- 考生提交答案后，服务端用**一次 Newton-Raphson 更新**估计能力值 `θ`；
- 下一题在满足**曝光上限**的候选题中，按 **Fisher 信息量最大**选择，信息量并列时取**最小（稳定的）题目 ID**；
- `response_id` 为客户端幂等键：**重复提交返回首次结果，不重复更新能力值、不重复计曝光**；
- 支持可选测验时限 `duration_seconds`（60～86400 秒）：创建时按 UTC 记录截止时间 `expires_at`，到期后取题/作答原子结束会话并返回 `409`；
- 活动会话可**暂停 / 恢复**：暂停时冻结剩余秒数、暂停期间不消耗时限，恢复时按剩余秒数重算 UTC 截止时间；无时限会话同样可以暂停/恢复；暂停期间取题与新作答返回 `409`，恢复后挂起题、已有作答与已计曝光原样保留；
- 支持可选**内容标签最低配额** `tag_minimums`：按 `content_tag` 声明本场至少要答的题数；剩余答题位仅够补缺口时只从欠额标签选题，欠额标签题量不足时以明确的 `finish_reason` 结束且不额外计曝光；
- 支持可选**能力估计精度停测** `target_se` + `min_precision_items`：每次作答更新 θ 后，在当前 θ 处重算本场全部已答题的 2PL Fisher 信息总和，`SE = 1/√Σ信息`；已答数达到下限、`SE ≤ target_se` 且标签配额均已满足时，在同一事务内结束会话（`finish_reason = precision_reached`）；
- 提供会话进度查询与完整答题记录查询，会话状态/进度/取题响应均返回当前标准误 `standard_error` 与停测配置；
- 支持**题库批量导入与预检** `POST /api/items/import`：一次提交题目数组与 `dry_run`，逐行校验 ID、选项键、答案、难度/区分度、`content_tag` 与批内重复 ID；预检只报告 `created`/`updated` 计划不写库，正式提交在单个 `BEGIN IMMEDIATE` 事务内原子新增或更新（更新保留 `exposure_count` 与启用状态），任一错误整批不变；
- 支持**题目撤回 / 启用** `PATCH /api/items/{id}/availability`：撤回题不再参与新取题；会话挂起题被撤回后，再次 `/next` 在同一事务内清除挂起项并按既有规则重选（已计曝光不回退）；对撤回题提交新作答返回 `409`（不写答题记录、不更新能力），已保存的 `response_id` 重放仍返回首次结果；
- 提供**单题质量复盘** `GET /api/items/{id}/analysis`：只统计**已结束会话**的历史作答，并使用作答时冻结保存的 `score`（之后修改题目答案不影响复盘结果）；对每个答过目标题且同场至少另答一题的会话，以「目标题得分 + 同场其余题答对比例」组成样本，返回有效样本数、历史正确率及两者的 Pearson 相关系数（样本少于 5 或任一序列无方差时为 `null`；未知题目 `404`；接口只读）；
- 提供**答卷雷同复核** `GET /api/sessions/{id}/similarity`：面向**已结束会话**，按 `item_id` 对齐双方历史作答，仅「双方选择同一错误选项」计为同错匹配（正确作答不计），每个匹配按该错误选项在全体已结束会话中的稀有程度加权（`log((该题错误作答总数+2)/(选择该错误选项的次数+1))`），分数为权重之和除以共答题数；候选至少共答 `min_shared` 题（默认 4），按分数、同错数降序及会话 ID 升序返回前 `limit` 条；统计只读 `responses` 中冻结的 `score` 与 `response`，题库改答案后结果不变；
- 自带 30 题确定性演示题库，容器启动后即可调用。

## 目录结构

```
.
├── app/
│   ├── main.py        # FastAPI 路由：会话 / 选题 / 作答 / 暂停恢复 / 进度 / 记录 / 批量导入 / 雷同复核
│   ├── irt.py         # 2PL 概率、Fisher 信息量、单次 Newton 更新
│   ├── analysis.py    # 单题质量复盘：历史样本构建与 Pearson 相关系数
│   ├── similarity.py  # 答卷雷同复核：同错选项加权相似度（已结束会话）
│   ├── importer.py    # 题库批量导入的逐行校验（ID/选项/答案/难度区分度/标签/重复 ID）
│   ├── database.py    # SQLite 连接、建表、初始化
│   ├── models.py      # Pydantic 请求/响应模型
│   └── seed.py        # 确定性演示题库（ITEM-001 ~ ITEM-030）
├── examples/demo.py   # 仅用标准库的端到端示例（含幂等演示）
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 快速开始

### 方式一：Docker Compose（推荐）

单条命令即可构建镜像并在后台启动服务：

```bash
docker compose up -d --build
```

前台运行（直接看日志，`Ctrl+C` 停止）可用：

```bash
docker compose up --build
```

启动后服务监听 **8000** 端口：

- API 根地址： http://localhost:8000
- 交互式文档（Swagger UI）： http://localhost:8000/docs
- 健康检查： http://localhost:8000/health
- SQLite 数据持久化在命名卷 `quiz-data`（容器内 `/data/quiz.db`）

### 方式二：本地直接运行

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

uvicorn app.main:app --host 0.0.0.0 --port 8000
```

首次启动自动建表并灌入 30 道演示题。

### 一键跑示例

服务启动后，另开终端：

```bash
python examples/demo.py
```

脚本会：创建带 600 秒时限的会话（演示 `duration_seconds` 越界校验与 `expires_at`）→ 循环「取题 → 作答」5 轮 → 演示重复 `response_id` 幂等、重复 `/next` 不重复计曝光 → 打印进度与答题记录 → 演示 `tag_minimums` 配额选题（含配额校验、末段只从欠额标签选题、无法补齐时 `tag_minimums_unfulfillable` 结束且不增加曝光）→ 演示精度停测（参数校验、SE 逐题下降、`precision_reached` 触发与重复 `response_id` 不重复触发状态变化）→ 演示**暂停 / 恢复**（冻结 `remaining_seconds`、暂停期间取题/新作答/重复暂停返回 409、恢复后重算 `expires_at` 且挂起题与曝光原样保留、无时限会话暂停/恢复、已结束会话 409）→ 演示**题目撤回 / 启用**（未知题目 404、撤回题不再被选中、挂起题撤回后 `/next` 同事务清除并重选且曝光不回退、撤回题新作答 409、已保存 `response_id` 重放仍返回首次结果、批量导入更新保留启用状态）。

## 核心算法

### 2PL 模型与信息量

```
P(θ) = 1 / (1 + exp(-a(θ - b)))          # 答对概率
I(θ) = a² · P(θ) · (1 - P(θ))            # 单题 Fisher 信息量
SE(θ) = 1 / sqrt( Σ_j I_j(θ) )           # 本场已答题总信息量对应的估计标准误
```

### 作答后一次 Newton 更新

对单条二元作答 `u`（1 对 / 0 错），对数似然关于 θ 的一、二阶导数为：

```
g  = a(u - P(θ))
h  = -a²·P·(1-P) = -I(θ)
θ' = θ - g/h = θ + a(u - P) / I(θ)       # 仅一步
```

为数值稳定：分母有 `1e-4` 下限，θ 被截断在 `[-6, 6]`。注意题目要求的是
**一次** Newton 更新（非迭代至收敛），实现见 `app/irt.py: newton_step`。

### 选题规则（`POST /api/sessions/{id}/next`）

候选题必须同时满足：

1. 题目处于**启用**状态（`is_active = 1`，撤回题不参与新取题）；
2. `exposure_count < exposure_limit`（**全局**曝光上限，所有会话共享）；
3. 该题在本会话中尚未作答（不重复出题）。

候选题按 `(-信息量, 题目 ID)` 排序取第一题 —— 信息量最大优先，**完全并列时
ID 更小的题胜出**，因此选择结果确定可复现。题目被选中的同一事务内
`exposure_count += 1`。

- 若存在「已取出但未作答」的挂起题，重复调 `/next` 返回**同一题且不重复加曝光**；
- 若挂起题已被**撤回**，`/next` 在**同一事务内**清除挂起项并按上述规则重选
  （该题已计曝光**不回退**），响应 `note` 会说明；无题可重选时按既有规则结束会话；
- 已答满 `max_items` → 会话结束，`finish_reason = max_items`；
- 没有满足曝光上限的候选题 → 结束，`finish_reason = no_available_items`；
- 设置了标签配额且欠额标签已无法补齐 → 结束，
  `finish_reason = tag_minimums_unfulfillable`（见下节）。
- `precision_reached` 不在 `/next` 内产生：精度停测只在作答提交、θ 更新之后判定（见下节）。

### 内容标签最低配额（可选 `tag_minimums`）

- 创建会话时可传 `tag_minimums`：键必须是题库中**现有且非空的 `content_tag`**，
  值必须为**正整数**，且所有配额之和 **不超过 `max_items`**；
  不传或传 `{}` 表示不设配额，行为与旧版完全一致。校验失败返回 `422`。
- 配额存入独立的 `session_tag_minimums` 表并随会话持久化；旧版本数据库在
  服务启动时自动建表迁移（旧会话无配额行）。
- `/next` 在原有的「未作答 + 曝光上限」候选范围内，按 `responses` 已答记录
  统计各标签**缺口** = 配额 − 已答数：
  - **剩余答题位 = 缺口总数**时，候选只保留欠额标签的题，再按信息量最大、
    ID 最小的既有规则选出（保证每场测验结束时配额一定被满足）；
  - 其余时候仍在全部候选题中选题，缺口约束只在「末段」介入。
- 一旦发现某欠额标签的**可用题数已不足以补齐**（题已答完或达到全局曝光上限），
  立即结束会话：`status = finished`、
  `finish_reason = tag_minimums_unfulfillable`，`note` 给出无法满足的缺口；
  本次**不选题、不返回题目、不增加任何曝光计数**（挂起题的幂等返回不受影响）。
- `GET /api/sessions/{id}/progress` 返回 `tag_minimums`（配额）与
  `tag_answered`（各标签已答数；出现过的标签与全部配额标签均会列出）。
- 未设置配额的会话即便无题可出，仍沿用原 `no_available_items` 结束原因。

### 测验时限（可选 `duration_seconds`）

- 创建会话时可传 `duration_seconds`，取值 **60～86400 秒**；不传则无时限（行为与旧版一致）。
- 服务端在创建时按 **UTC** 计算并持久化截止时间 `expires_at = created_at + duration_seconds`
  （ISO-8601，秒级），在创建响应以及 `GET /api/sessions/{id}`、`/progress` 中一并返回。
- `POST /next` 与 `POST /responses` 在**写事务内最先检查**截止时间：一旦 `now(UTC) >= expires_at`，
  即在同一事务内原子地把会话置为 `finished`、`finish_reason = time_expired`、清空挂起题，
  并以 **HTTP 409** 拒绝本次操作；**不会**增加任何曝光计数，也**不会**写入答题记录。
  未到期时完全保留原有行为（包括挂起题不重复计曝光、`response_id` 幂等）。
- 只读的会话/进度/记录查询不触发自动结束；客户端可据 `expires_at` 自行倒计时。

### 暂停与恢复（`POST /pause`、`POST /resume`）

- `POST /api/sessions/{id}/pause`：仅 **`active` 且尚未到期** 的会话可暂停。
  - 暂停瞬间把剩余秒数 `expires_at − now(UTC)` 冻结到 `remaining_seconds`，记录 `paused_at`（UTC），
    会话状态变为 `paused`；暂停期间**不消耗时限**（`expires_at` 保持不动）。
  - **已到期会话不能通过暂停续命**：暂停请求与 `/next`、`/responses` 一样先做到期检查，
    到期时原子结束会话（`finish_reason = time_expired`）并返回 **409**。
  - 已暂停或已结束的会话重复暂停返回 **409**。
  - **无时限会话**（未传 `duration_seconds`）也可以暂停与恢复，`remaining_seconds` 始终为 `null`。
- `POST /api/sessions/{id}/resume`：仅 **`paused`** 的会话可恢复，重复恢复（或对已结束会话恢复）返回 **409**。
  - 恢复时按冻结的剩余秒数重算截止时间：`expires_at = now(UTC) + remaining_seconds`，
    随后清空 `paused_at` / `remaining_seconds`，状态回到 `active`。
  - 恢复**原样保留**：当前待答题（挂起题）、全部已有作答、能力值 θ 以及已计的全局曝光。
    恢复后重复调用 `/next` 仍返回同一道待答题，**不会再次增加曝光**。
- 暂停期间的访问规则：
  - `POST /next` 取题 → **409**（挂起题保留，不增加曝光）；
  - `POST /responses` 提交**新**作答 → **409**（不写入答题记录、不更新 θ、不增加曝光）；
  - 已存储 `response_id` 的**幂等重放**仍返回首次结果（HTTP 200），与已结束会话上的重放行为一致；
  - 只读查询（会话状态 / 进度 / 答题记录）在暂停期间照常可用。
- 暂停状态与剩余时长在会话状态、进度查询及取题响应中返回：
  `paused`（布尔）、`paused_at`（UTC，未暂停为 `null`）、`remaining_seconds`
  （暂停时为冻结值；进行中为按 UTC 实时计算的值，下限 0；无时限为 `null`）。
  答题记录查询额外返回 `status`（可能为 `paused`）。

### 能力估计精度停测（可选 `target_se` + `min_precision_items`）

- 创建会话时可同时传：
  - `target_se`：目标能力估计标准误，须为**正有限**浮点数（`> 0`，不接受 `0`、负数、`Infinity`、`NaN`）；
  - `min_precision_items`：触发停测前的**最少作答数**，正整数，取值 **1～`max_items`**。
  - 两者必须**同时提供或同时省略**；校验失败返回 `422`。不传时不启用精度停测，流程与旧版完全一致。
- 配置持久化在 `sessions` 表（`target_se` / `min_precision_items` 两列）；旧版本数据库在服务启动时
  通过 `ALTER TABLE ... ADD COLUMN` 自动迁移（旧会话两列为 `NULL`，行为不变）。
- 每次成功作答并完成 θ 更新后，在**同一写事务**内于**当前 θ** 处，对本场**全部已答题**重新计算
  2PL Fisher 信息总和 `Σ I_j(θ)`，标准误为 `SE = 1/√Σ信息`。当：
  1. 已答数 `>= min_precision_items`，且
  2. `SE <= target_se`，且
  3. 所有 `tag_minimums` 标签配额均已满足（未设置配额时自动满足），

  即在该事务内把会话置为 `finished`、`finish_reason = precision_reached`、清空挂起题；
  任一条件不满足则继续原有「信息量最大」选题流程。信息总和为零（非正）时 `standard_error` 返回
  **`null`**，此时不可能触发停测。
- 原有规则优先级不变：`time_expired`（到期最先检查）与 `max_items`（答满）优先于精度停测；
  精度停测只发生在作答提交之后，`/next` 不会替它结束会话。
- 会话状态 `GET /api/sessions/{id}`、进度 `/progress` 与取题响应 `/next` 均返回
  `target_se`、`min_precision_items` 与当前 `standard_error`（无已答题时为 `null`）。
- 重复提交同一 `response_id` 走幂等回放分支：以 `200` 返回首次结果，**不会**重算 θ、重算 SE，
  也**不会**再次触发或改变 `precision_reached` 状态。

### 幂等作答（`POST /api/sessions/{id}/responses`）

- 客户端为每次作答生成唯一 `response_id`（建议 UUID），与请求体一起提交；
- 服务端首次收到时评分（提交的选项键与题库 `answer` 比对）、做一次 Newton
  更新、清空挂起题、写入不可变记录；
- **同一 `response_id` 再次提交：以 `200` 返回首次存储的结果（首次为 `201`），不再更新 θ、不再计曝光。**
  若该 `response_id` 对应的是其他会话/题目，返回 `409`。

所有写操作使用 `BEGIN IMMEDIATE` 事务，SQLite WAL 模式下并发请求串行化，
避免曝光计数与能力更新丢失；`responses(id)` 主键与 `UNIQUE(session_id, item_id)`
在数据库层面兜底并发竞争。

### 题目撤回与启用（`PATCH /api/items/{id}/availability`）

题目支持**撤回**（不再参与新取题）与**重新启用**，请求体只需一个布尔字段：

```jsonc
// PATCH /api/items/ITEM-001/availability
{"is_active": false}   // 撤回；true 表示重新启用
```

- **语义**：`is_active=false` 撤回题目，`true` 重新启用；操作幂等，重复设置同一状态返回 `200`
  与当前题目行；`is_active` 必须是 JSON 布尔值（非布尔如 `1`、`"false"` 返回 `422`）；
  **未知题目返回 `404`**。响应为题库行（含 `is_active`、`exposure_count` 等）。
- **不参与新取题**：`/next` 的候选题必须 `is_active = 1`，撤回题不会被新会话或进行中的
  会话再次选中；已产生的历史答题记录、曝光计数与复盘统计全部保留。
- **挂起题被撤回**：若某会话「已取出但未作答」的挂起题被撤回，下一次 `/next` 会在
  **同一事务内**清除挂起项并按既有选题规则（启用 + 曝光上限 + 未作答，信息量最大、
  ID 最小）重选一题；**该题已计的曝光计数不回退**，响应 `note` 会说明本次清除与重选；
  若没有可重选的题，按既有规则结束会话（`no_available_items` /
  `tag_minimums_unfulfillable`）。
- **作答限制**：对**已撤回**的题目提交**新**作答返回 **`409`**——不写答题记录、不更新
  能力值 θ；而已保存的 `response_id` **幂等重放仍返回首次结果**（HTTP 200），与暂停/
  结束会话上的重放行为一致。
- **导入交互**：批量导入**更新既有题目时保留其启用状态**（重新导入不会把撤回题重新启用），
  **新导入的题目默认启用**。
- **查询与迁移**：题库列表 `GET /api/items` 与详情 `GET /api/items/{id}` 均返回
  `is_active`；旧版本数据库在服务启动时自动迁移（`items` 表新增
  `is_active INTEGER NOT NULL DEFAULT 1`），**既有题目全部迁移为启用**，行为与旧版一致。

### 题库批量导入与预检（`POST /api/items/import`）

一次提交一个题目数组（1～1000 行）与预检开关：

```jsonc
{
  "dry_run": false,                 // true=只预检不写库
  "items": [
    {
      "id": "ITEM-101",             // 必填，非空白字符串（≤128），题库主键
      "difficulty": 1.25,           // 必填，有限数值，范围 [-6, 6]
      "discrimination": 1.1,        // 必填，正有限数值，范围 (0, 5]（a<=0 无信息量）
      "content_tag": "几何",         // 字符串或 null；纯空白串归一化为 null
      "stem": "...",                // 必填，非空白题干
      "options": [                  // 必填，2~8 个 {key, text}
        {"key": "A", "text": "..."},
        {"key": "B", "text": "..."}
      ],
      "answer": "A"                 // 必填，必须出现在本行 options 的 key 中
    }
  ]
}
```

- **逐行校验，一次报全**：不会在第一个坏行处中断。每行独立检查
  `id`（非空白、长度）、`options`（2～8 项、`key`/`text` 非空白、行内键唯一）、
  `answer`（属于选项键）、`difficulty`/`discrimination`（有限数且在界内；
  布尔值、`NaN`、`Infinity` 均拒绝）、`content_tag`（字符串或 null）、
  以及**批内重复 ID**（同一 ID 首次出现正常、之后每行报 `duplicate_id_in_batch`，
  并指明首次出现的行号）。标量/数组等不是对象的行报 `invalid_row`。
  选项键与答案自动去首尾空白并转大写（如 `"a"` → `"A"`），ID 区分大小写、仅去空白。
- **稳定行号**：请求体（非数组/空数组/超过 1000 行/`dry_run` 非布尔）在 422 边界处理；
  语义错误全部进 200/422 响应体的 `errors`，每条带
  `row`（**1 基行号**，与提交数组下标一一对应）、`field`、`code`、中文 `message`，
  可解析出 ID 时附 `id`；`errors` 与 `results` 均按行号升序，方便对回原始表格行。
- **预检 `dry_run=true`**：只读、不写库（HTTP 始终 `200`）。对每行给出
  `action`：库中不存在 → `created`，已存在 → `updated`；同时返回
  `created_count` / `updated_count` 计划数。即使存在错误行也照常报告全部错误。
- **正式提交 `dry_run=false`（缺省）**：
  - 全部行合法时，在**单个 `BEGIN IMMEDIATE` 事务**内以
    `INSERT ... ON CONFLICT(id) DO UPDATE` 批量 upsert：新 ID 新增、旧 ID 就地更新；
  - 更新覆盖 `difficulty` / `discrimination` / `content_tag` / `stem` / `options` / `answer`，
    **保留 `exposure_count` 与 `is_active`**——重新导入题库不会把全局曝光计数清零，
    也不会把已撤回的题目重新启用（新导入的题目默认启用）；已被会话引用的题目照常可用；
  - **任一错误整批不变**：只要有一行校验失败即不写任何数据，返回 HTTP `422`
    （`valid=false`、两个计数均为 0、`results` 为空、`errors` 列出全部问题）；
    事务内的数据库异常同样整体回滚。
- 成功响应（预检与正式提交同构）：

  ```json
  {
    "dry_run": false, "valid": true,
    "created_count": 1, "updated_count": 1, "total": 2,
    "results": [
      {"row": 1, "id": "ITEM-101", "action": "created"},
      {"row": 2, "id": "ITEM-005", "action": "updated"}
    ],
    "errors": []
  }
  ```

  有错误的正式提交返回 HTTP `422` 且 `valid=false`、计数为 0；
  预检即使有错误也是 HTTP `200`，便于把预检集成进上传 UI。

### 单题质量复盘（`GET /api/items/{id}/analysis`）

只读地复盘单道题在历史测验中的表现，用于发现偏易/偏难或区分度异常的题目：

- **样本来源**：仅统计状态为 `finished` 的**已结束会话**；进行中/暂停中的会话不计入。
  每个会话必须**答过目标题且同场至少另答一题**，否则不成样本。
- **样本构成**：每个有效会话贡献一对数值：
  - `x` = 该会话在目标题上的得分（`responses.score`，1 对 / 0 错）；
  - `y` = 同场**其余**已答题的答对比例（其余题得分之和 / 其余题题数），可看作该考生本场整体水平的代理。
- **使用作答时保存的 `score`**：复盘只读历史 `responses` 记录，**不**按题库当前答案重新判分；
  之后通过批量导入修改该题答案，不会改变历史复盘结果。
- **返回字段**：
  - `sample_count`：有效样本数；
  - `correct_rate`：目标题在有效样本中的历史正确率（无样本时为 `null`）；
  - `pearson_correlation`：`x` 与 `y` 的 Pearson 相关系数——正值表示目标题答得好的考生
    同场其他题也答得好（区分方向正常）。**样本少于 5 个，或任一序列无方差
    （如所有样本目标题都答对）时相关系数未定义，返回 `null`。**
- 接口完全只读：不创建/结束会话、不改变曝光计数、不写题库。
- 未知题目返回 **HTTP 404**。

```jsonc
// GET /api/items/ITEM-001/analysis
{
  "item_id": "ITEM-001",
  "sample_count": 12,
  "correct_rate": 0.6666666666666666,
  "pearson_correlation": 0.7144345080641217
}
// 样本不足 5 或存在常值序列：
// {"item_id":"ITEM-002","sample_count":3,"correct_rate":1.0,"pearson_correlation":null}
```

### 答卷雷同复核（`GET /api/sessions/{id}/similarity`）

只读地复核一份**已结束**答卷与其他已结束会话的雷同程度，用于发现抄袭/串通作答：

- **目标会话**：必须存在（未知返回 **404**）且已结束（`status = finished`；
  进行中/暂停中的会话返回 **409**）。接口完全只读，不改变任何会话、曝光或题库数据。
- **候选会话**：所有其他 `finished` 会话；与目标会话按 `item_id` 对齐历史作答，
  **共同作答的题数（共答数）至少为 `min_shared`**（查询参数，默认 `4`，范围 `2～50`）
  才进入排名。无候选时返回空列表（`count = 0`）。
- **同错匹配**：一道共答题仅当**双方选择了同一选项且双方冻结的 `score` 均为 0**
  （即选了同一个错误选项）时计为一次匹配；答对的题、以及「一方对一方错」或
  「错但选项不同」的题均不计。
- **匹配权重**（自然对数，IDF 式——越少人选的错误选项越可疑）：

  ```
  weight = log((该题已结束会话错误作答总数 + 2) / (选择该错误选项的次数 + 1))
  ```

  两个计数都来自**已结束会话**在 `responses` 中冻结的 `score` 与 `response`；
  `+2`/`+1` 平滑保证权重恒为正。
- **分数**：`score = 同错匹配权重之和 / 共答题数`（除以共答数而非同错数，
  共答题多本身不会抬高分数）。
- **返回**：按 **分数降序 → 同错数降序 → 会话 ID 升序** 排列，最多返回
  `limit` 条（查询参数，默认 `20`，范围 `1～50`）；每条包含
  `session_id`、`shared_count`（共答数）、`same_wrong_count`（同错数）与 `score`（分数）。
- **统计与题库解耦**：只读 `responses` 里作答时冻结的 `score`/`response`，
  之后通过批量导入修改题目正确答案**不会**改变复核结果。

```jsonc
// GET /api/sessions/<SID>/similarity?min_shared=4&limit=20
{
  "session_id": "<SID>",
  "min_shared": 4,
  "limit": 20,
  "count": 2,
  "results": [
    {"session_id": "ab12...", "shared_count": 6, "same_wrong_count": 4,
     "score": 0.156152},
    {"session_id": "cd34...", "shared_count": 4, "same_wrong_count": 2,
     "score": 0.122656}
  ]
}
// 无候选：{"session_id":"<SID>","min_shared":4,"limit":20,"count":0,"results":[]}
```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET  | `/health` | 健康检查、题库题量 |
| GET  | `/api/items` | 题库列表（含曝光次数与启用状态 `is_active`，分页 `limit/offset`） |
| GET  | `/api/items/{item_id}` | 单题详情（管理用，含正确答案与启用状态） |
| PATCH | `/api/items/{item_id}/availability` | **撤回 / 启用题目**（`{"is_active": bool}`；未知题目 404；撤回题不参与新取题） |
| GET  | `/api/items/{item_id}/analysis` | **单题质量复盘**（已结束会话样本、历史正确率、与同场表现的 Pearson 相关系数；只读） |
| POST | `/api/items/import` | **题库批量导入/预检**（逐行错误、dry_run、事务原子 upsert、更新保留曝光数与启用状态） |
| POST | `/api/sessions` | 创建测验会话 |
| GET  | `/api/sessions/{id}` | 会话状态 |
| GET  | `/api/sessions/{id}/progress` | **会话进度**（θ、已答数、挂起题、已答题目、暂停状态与剩余秒数） |
| GET  | `/api/sessions/{id}/responses` | **答题记录**（逐题评分与 θ 轨迹） |
| GET  | `/api/sessions/{id}/similarity` | **答卷雷同复核**（已结束会话的同错加权相似度排名；`min_shared`/`limit` 可调；只读） |
| POST | `/api/sessions/{id}/next` | 自适应选下一题 |
| POST | `/api/sessions/{id}/responses` | 提交答案（幂等，一次 Newton 更新） |
| POST | `/api/sessions/{id}/pause` | **暂停**活动会话（冻结剩余时限；无时限会话也可暂停） |
| POST | `/api/sessions/{id}/resume` | **恢复**暂停中的会话（按剩余秒数重算 UTC 截止时间） |

完整字段与可交互试用见 http://localhost:8000/docs。

## curl 调用示例

```bash
# 1) 健康检查
curl -s http://localhost:8000/health
# {"status":"ok","bank_size":30}

# 1b) 题库批量导入：先预检（dry_run=true，不写库），看 created/updated 计划与逐行错误
curl -s -X POST http://localhost:8000/api/items/import \
  -H 'Content-Type: application/json' \
  -d '{
    "dry_run": true,
    "items": [
      {"id":"ITEM-101","difficulty":1.25,"discrimination":1.1,"content_tag":"几何",
       "stem":"一个三角形内角和是多少度？",
       "options":[{"key":"A","text":"90"},{"key":"B","text":"180"},
                  {"key":"C","text":"270"},{"key":"D","text":"360"}],
       "answer":"B"},
      {"id":"ITEM-001","difficulty":-2.9,"discrimination":0.9,"content_tag":"算术",
       "stem":"更新既有题目（曝光次数保留）",
       "options":[{"key":"A","text":"1"},{"key":"B","text":"2"}],"answer":"A"}
    ]
  }'
# {"dry_run":true,"valid":true,"created_count":1,"updated_count":1,"total":2,
#  "results":[{"row":1,"id":"ITEM-101","action":"created"},
#             {"row":2,"id":"ITEM-001","action":"updated"}],"errors":[]}

# 预检（或正式提交）遇到坏行：一次返回全部逐行错误（row 为 1 基行号）
curl -s -X POST http://localhost:8000/api/items/import \
  -H 'Content-Type: application/json' \
  -d '{"dry_run": true, "items": [
    {"id":"ITEM-201","difficulty":9.9,"discrimination":1.0,"stem":"难度越界",
     "options":[{"key":"A","text":"1"},{"key":"B","text":"2"}],"answer":"A"},
    {"id":"ITEM-201","difficulty":0.0,"discrimination":0.0,"stem":"重复ID且区分度非法",
     "options":[{"key":"A","text":"1"},{"key":"B","text":"2"}],"answer":"Z"},
    "不是对象的坏行"
  ]}'
# HTTP 200（预检）/ 422（正式提交）：
# {"valid":false,"created_count":0,"updated_count":0,"results":[],
#  "errors":[
#    {"row":1,"code":"out_of_range","field":"difficulty","message":"难度 difficulty必须在 [-6.0, 6.0] 之间，收到 9.9", ...},
#    {"row":2,"code":"out_of_range","field":"discrimination", ...},
#    {"row":2,"code":"answer_not_in_options","field":"answer","message":"正确答案 'Z' 不在选项键 ['A', 'B'] 中", ...},
#    {"row":2,"code":"duplicate_id_in_batch","field":"id","id":"ITEM-201",
#     "message":"题目 ID 'ITEM-201' 在本批第 1 行已出现，同一批次不能重复"},
#    {"row":3,"code":"invalid_row","field":null,"message":"每一行必须是 JSON 对象（键值对），收到 str", ...}
#  ]}

# 预检通过后正式提交（去掉 dry_run 或置 false）：单事务原子 upsert，
# 任一错误整批不变（HTTP 422，题库保持原样）；更新既有题目保留 exposure_count 与 is_active
curl -s -X POST http://localhost:8000/api/items/import \
  -H 'Content-Type: application/json' \
  -d '{"items":[ ...同上... ]}'
# {"dry_run":false,"valid":true,"created_count":1,"updated_count":1, ...}

# 1c) 题目撤回 / 启用：撤回后该题不再参与新取题；重复设置幂等
curl -s -X PATCH http://localhost:8000/api/items/ITEM-001/availability \
  -H 'Content-Type: application/json' \
  -d '{"is_active": false}'
# {"id":"ITEM-001", "is_active":false, "exposure_count":3, ...}
# 未知题目 -> HTTP 404 {"detail":"item not found"}；is_active 非布尔 -> HTTP 422
curl -s -X PATCH http://localhost:8000/api/items/ITEM-001/availability \
  -H 'Content-Type: application/json' \
  -d '{"is_active": true}'        # 重新启用
# 若某会话的挂起题被撤回：再次 /next 会在同一事务内清除挂起项并重选
# （已计曝光不回退，响应 note 有说明）；对撤回题提交新作答 -> HTTP 409，
# 已保存的 response_id 重放仍返回首次结果（HTTP 200）

# 2) 创建会话（初始 θ=0，最多 10 题，单题曝光上限 5）
curl -s -X POST http://localhost:8000/api/sessions \
  -H 'Content-Type: application/json' \
  -d '{"initial_theta": 0.0, "max_items": 10, "exposure_limit": 5}'
# 记下返回的 "id"，导出 SID=...

# 2b) 也可以在创建时指定测验时限（秒，60~86400）；
#     返回中会带 UTC 截止时间 "expires_at"
curl -s -X POST http://localhost:8000/api/sessions \
  -H 'Content-Type: application/json' \
  -d '{"initial_theta": 0.0, "max_items": 10, "duration_seconds": 600}'
# {"id":"...", "duration_seconds":600,
#  "expires_at":"2026-09-23T08:10:00+00:00", "status":"active", ...}

# 2c) 也可以在创建时声明内容标签最低配额 tag_minimums：
#     键须为题库现有非空 content_tag，值为正整数，总和 <= max_items
curl -s -X POST http://localhost:8000/api/sessions \
  -H 'Content-Type: application/json' \
  -d '{"initial_theta": 0.0, "max_items": 10,
       "tag_minimums": {"几何": 3, "概率统计": 2}}'
# {"id":"...", "max_items":10,
#  "tag_minimums":{"几何":3,"概率统计":2}, "status":"active", ...}
# 配额之和超过 max_items、值非正整数或标签不存在 -> HTTP 422

# 2d) 也可以启用能力估计精度停测：target_se 为正有限数，
#     min_precision_items 为 1~max_items 的正整数，二者必须同时提供
curl -s -X POST http://localhost:8000/api/sessions \
  -H 'Content-Type: application/json' \
  -d '{"initial_theta": 0.0, "max_items": 15,
       "target_se": 0.8, "min_precision_items": 5}'
# {"id":"...", "max_items":15, "target_se":0.8,
#  "min_precision_items":5, "standard_error":null, "status":"active", ...}
# 只给一个参数、target_se<=0/Infinity/NaN、min_precision_items>max_items -> HTTP 422

# 3) 选下一题
curl -s -X POST http://localhost:8000/api/sessions/$SID/next
# item.id / item.stem / item.options / information / exposure_count ...
# 若已过 expires_at：HTTP 409，
# {"detail":"测验时限已到（UTC 截止时间 ...），会话已结束：finish_reason=time_expired"}
# 若欠额标签已无题可补（不选题、不增加曝光）：
# {"status":"finished","finish_reason":"tag_minimums_unfulfillable",
#  "item":null,"note":"欠额标签的可用题数不足以补齐配额，无法满足的缺口 {...}"}

# 4) 提交答案（response_id 为客户端幂等键，务必唯一）
curl -s -X POST http://localhost:8000/api/sessions/$SID/responses \
  -H 'Content-Type: application/json' \
  -d '{"response_id": "'$(uuidgen)'", "item_id": "ITEM-xxx", "response": "A"}'
# 返回 correct、score、theta_before、theta_after、p_correct ...
# 到期后提交同样返回 409，且不写入答题记录、不增加曝光

# 5) 用完全相同的请求体重放一次 -> 返回首次结果，theta_after 不变

# 5b) 暂停 / 恢复（限时会话）
curl -s -X POST http://localhost:8000/api/sessions/$SID/pause
# {"id":"...", "status":"paused", "paused":true,
#  "paused_at":"2026-09-23T08:05:10+00:00",
#  "remaining_seconds":290,   # 暂停瞬间冻结的剩余秒数
#  "expires_at":"2026-09-23T08:10:00+00:00", ...}
# 暂停期间不消耗时限；/next 与新作答返回 409；
# 重复暂停、对已结束会话暂停也返回 409；到期会话不能借暂停续命（409 + time_expired）
curl -s -X POST http://localhost:8000/api/sessions/$SID/next
# HTTP 409 {"detail":"会话已暂停（paused_at=...）：暂停期间不能取题或作答，请先调用 POST /api/sessions/.../resume 恢复"}
curl -s -X POST http://localhost:8000/api/sessions/$SID/pause      # 重复暂停 -> 409

curl -s -X POST http://localhost:8000/api/sessions/$SID/resume
# {"id":"...", "status":"active", "paused":false, "paused_at":null,
#  "remaining_seconds":289,                     # 时钟重新走动
#  "expires_at":"2026-09-23T08:14:59+00:00",    # = 恢复时刻 + 冻结的剩余秒数
#  "current_item_id":"ITEM-...", ...}           # 挂起题保留
curl -s -X POST http://localhost:8000/api/sessions/$SID/resume     # 重复恢复 -> 409
# 恢复后再调 /next 仍返回同一道待答题，且不重复计曝光

# 6) 查询会话（含 expires_at / paused / remaining_seconds）、进度与答题记录
curl -s http://localhost:8000/api/sessions/$SID
curl -s http://localhost:8000/api/sessions/$SID/progress
# progress 还返回 "tag_minimums"（配额）、"tag_answered"（各标签已答数）
# 以及 "paused" / "paused_at" / "remaining_seconds"（暂停状态与剩余时长）、
# "target_se" / "min_precision_items"（停测配置）和
# "standard_error"（当前 θ 处 1/√ΣFisher 信息；无已答题为 null）
curl -s http://localhost:8000/api/sessions/$SID/responses
```

重复步骤 3-4 即可完成整场测验；当 `status` 变为 `finished` 时，可从
`finish_reason` 得知结束原因（`max_items` / `no_available_items` /
`time_expired` / `tag_minimums_unfulfillable` / `precision_reached`）。
其中 `precision_reached` 仅在启用了精度停测的会话上出现，且只由一次**新的**
成功作答在同一事务内触发；答满 `max_items` 与到期停测的优先级高于精度停测。

# 7) 单题质量复盘（只读；只统计已结束会话，使用作答时保存的 score）
curl -s http://localhost:8000/api/items/ITEM-001/analysis
# {"item_id":"ITEM-001","sample_count":12,"correct_rate":0.6667,
#  "pearson_correlation":0.7144}
# 样本少于 5 或任一序列无方差 -> "pearson_correlation":null
# 未知题目 -> HTTP 404 {"detail":"item not found"}

# 8) 答卷雷同复核（只读；目标会话须已结束）
#    min_shared：候选至少共答的题数（默认 4，2~50）；limit：返回条数上限（默认 20，1~50）
curl -s "http://localhost:8000/api/sessions/$SID/similarity?min_shared=4&limit=20"
# {"session_id":"...","min_shared":4,"limit":20,"count":1,
#  "results":[{"session_id":"...","shared_count":6,"same_wrong_count":4,
#              "score":0.156152}]}
# 结果按 分数降序 -> 同错数降序 -> 会话 ID 升序 排列；无候选时 results 为空列表
# 目标会话不存在 -> HTTP 404；目标会话未结束（active/paused）-> HTTP 409；
# min_shared/limit 越界 -> HTTP 422

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `QUIZ_DB_PATH` | `data/quiz.db`（本地）/ `/data/quiz.db`（镜像） | SQLite 文件路径 |
| `DEFAULT_EXPOSURE_LIMIT` | `5` | 创建会话未指定曝光上限时的默认值 |

## 说明与边界

- 模型为 2PL（无猜测参数）；演示题为简单算术题，重点展示选题与能力更新机制。
- 曝光计数是**跨会话全局**的：用于模拟真实 CAT 的曝光控制（曝光上限可按会话放宽/收紧）。
- 暂停/恢复**无需额外配置**：状态与冻结秒数随会话存放在同一 SQLite 库；服务启动时旧库自动迁移（新增 `paused_at` / `remaining_seconds` 列，并把 `status` 的 CHECK 放宽到 `active/paused/finished`），历史会话与答题记录原样保留。所有时间戳均为 **UTC**、秒级精度，因此暂停冻结与恢复重算的截止时间最多有 ±1 秒的取整误差。
- 题目撤回**无需额外配置**：旧库启动时自动为 `items` 表新增 `is_active` 列（`DEFAULT 1`），**既有题目全部迁移为启用**；撤回只影响新取题与新的作答，历史答题记录、曝光计数与单题复盘统计均不受影响。
- 答卷雷同复核**无需额外配置**：不新增任何表，直接复用现有 `sessions`/`responses`；只统计 `finished` 会话，且只读作答时冻结的 `score` 与 `response`——题库改答案、题目撤回都不影响复核结果；接口完全只读，可安全地对历史会话随时调用。
- SQLite + WAL 适合单机/演示与中小流量；写事务已串行化（暂停/恢复同样使用 `BEGIN IMMEDIATE`），水平扩展时可替换连接层。
