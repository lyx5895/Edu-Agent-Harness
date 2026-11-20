# 系统架构：Harness 与交互系统

本文介绍本项目由哪两大块组成、它们的分界在哪里、各自如何设计。两块可以分开讲：

- **第一部分：Harness**：包在模型外面的一层，负责约束模型的行为、核对模型的输出。
- **第二部分：交互系统**：后端 API、权限、存储和教学业务，加上前端界面，负责让老师和学生用起来。

本文是技术参考。对外讲解请看不含代码细节的 [DESIGN_OVERVIEW.md](DESIGN_OVERVIEW.md)。两者的概念区分见 [HARNESS_AND_TEACHING_SYSTEM.md](HARNESS_AND_TEACHING_SYSTEM.md)，研究背景见 [EDU_HARNESS_RESEARCH.md](EDU_HARNESS_RESEARCH.md)。

---

## 0. 两块是分开的吗

**基本是分开的**，中间只隔着一个很窄的接口。注意有两种接口：

- 浏览器和后端之间走 **HTTP REST API**；
- 后端和 harness 在**同一个进程**里，后端直接调用 Python 函数（`api.py` → `Gateway` → `OpenEngine.operation()`），不经过网络；
- 只有调用模型（OmniEdu-9B 在 8000 端口，复核模型在 8002 端口）时，harness 才通过 HTTP 访问 vLLM 的 OpenAI 兼容接口。

```
                 ┌──────────────────────────────────────────┐
  浏览器         │  交互系统                                 │
  student.js ──► │  api.py → access.py(权限) → storage(事务) │
  teacher.js     │  teaching/(班级、布置、练习包、看板)      │
                 └───────────────┬──────────────────────────┘
                                 │  进程内的 Python 函数调用（9 个入口，见下表）
                                 │  学生：create_task / operation / session
                                 │  老师：traces / replay / review_detail / correct / confirm_review / learning_memory
                 ┌───────────────▼──────────────────────────┐
                 │  Harness                                  │
                 │  策略 → 判定 → 写回复 → 审核 → 记录证据   │
                 │            ▲ 调用 OmniEdu-9B / 复核模型   │
                 └──────────────────────────────────────────┘
```

判断方法有两条：

1. **把前端整个换掉**，例如换成小程序或者命令行，harness 一行都不用改。学生的每个操作最后都是一次 `operation()` 调用。
2. **把模型或教学策略换掉**，例如换成 v5 策略，或者接入别的模型，前端和 API 一行都不用改。前端只显示 harness 返回的回复、状态和 `delivered_action`，不知道背后的策略是什么。

两块分开测试：harness 有策略、判定、审核、回放的单元测试；交互系统有权限测试，要求每个 `/api` 路由都必须经过权限检查，此外还有存储并发测试和浏览器脚本检查。

### Harness 对外的接口

后端调用 harness 的入口共有 9 个，都是 `OpenEngine` 的方法：

| 接口 | 谁调用（HTTP 路由） | 做什么 | 返回 |
|---|---|---|---|
| `create_task(learner, request / material / image / assignment_id / followup_id, …)` | 学生新建任务 `POST /api/sessions` | 建立会话。自己出的题由模型整理成任务（目标、评分要求、参考答案），检查合格才开始；老师布置的题直接使用 | 会话视图 |
| **`operation(sid, request_id, action, body, version)`** | 学生的每个动作 `POST /api/sessions/{sid}/{action}` | **核心入口**，执行第一部分 1.2 的整条流水线。`action` 包括 `attempt`（作答）、`hint`、`explain`、`reply`（回答追问）、`check`（独立检查）、`resume_practice`、`image`、`confirm` 等 | 回复、判定状态、证据类型、教学动作，以及更新后的会话视图 |
| `session(sid)` | `GET /api/sessions/{sid}` | 读取会话的学生视图：题目（不含参考答案）、待回答的追问、是否完成、是否被改判、对话历史（含 `delivered_action`） | 会话视图 |
| `traces(sid)` | 老师 `GET /api/teacher/sessions/{sid}/trace` | 读取每一轮的完整轨迹：策略输入、决策、模型调用、审核结果、状态变化 | 轨迹列表 |
| `replay(sid)` | 老师 `GET …/replay` | 用记录的策略版本重算每一轮决策，核对是否和当时一致，不调用模型 | 每轮是否一致 |
| `review_detail(sid, event_id)` | 老师打开某次作答 | 读取那次作答当时的题目快照、学生原文、判定依据 | 作答详情 |
| `correct(sid, event_id, status, reason, request_id)` | 老师改判 `POST …/corrections` | 记录改判，并沿着证据依赖把下游会话全部标为需要重新检查 | 受影响的会话列表 |
| `confirm_review(sid, event_id, reason, request_id)` | 老师确认 `POST …/reviews` | 老师确认模型的判定没有问题，从复核队列中移除 | 确认结果 |
| `learning_memory(learner, class_id)` | 老师 `GET /api/teacher/memory/{learner}` | 由证据推导出的学习记录（每个目标的状态、证据、下一步建议） | 学习记录 |

几点说明：

- **学生端只用到前三个**，其中真正"教"的只有 `operation`；
- **后六个服务于老师的监督**，对应 harness 的"可追溯、可纠正"；
- 写操作都带 `request_id`（重复提交返回同一结果），`operation` 还带 `version`（会话被并发修改时返回冲突）；
- harness 通过抛出领域错误表示失败：`Conflict`（状态不允许）、`ValueError`（输入不合法）、`ModelUnavailable`（模型不可用）。后端把它们翻译成 HTTP 409、400、503。

除了这 9 个接口，API 还会调用约 20 个**业务方法**，包括：

- 班级、布置、跟进任务：`classes`、`assignments`、`publish_assignment`、`inbox`、`assign_followups`……
- 练习包：`save_pack`、`generate_pack`、`publish_pack`……
- 学习路径：`publish_path`、`next_step`；
- 看板：`dashboard`；
- 知识点：`suggest_knowledge`。

这些方法属于交互系统。

**分得还不干净的地方**（讲的时候可以如实说明，也是下一步要改的）。其中最主要的一点是，上面的业务方法和 9 个 harness 接口目前通过 mixin 挂在同一个 `OpenEngine` 类上：

| 位置 | 问题 |
|---|---|
| [core/open_engine.py](../harness/core/open_engine.py)（722 行） | 两种代码混在一个类里。一种是对话编排（`operation`、`_teach`、`_decide`），属于 harness；另一种是会话创建、领取布置、题目去重，属于业务。应该拆成两个文件 |
| [persistence/storage.py](../harness/persistence/storage.py) | 存储本身是基础设施，但冲突重试依赖 harness 的回放机制：重跑时用已记录的模型结果，不会重新调用模型。这是两层设计在一起的地方 |
| [teaching/engine_teacher.py](../harness/teaching/engine_teacher.py) | 教师改判是业务入口，但"改判后哪些证据要跟着失效"由 harness 的 `dependencies` 计算 |

---

# 第一部分：Harness

## 1.1 它要解决什么问题

直接让大模型辅导学生，有四类风险：

1. **泄露答案**：学生要提示，模型却直接给出解法；
2. **误判**：答错了说对，或者答对了说错；
3. **虚假的"学会了"**：在提示帮助下做对，被当成已经掌握；
4. **不可追溯**：出了错，不知道是哪一步、哪条依据导致的，也没法纠正。

harness 的设计原则是：**模型负责理解和表达，判断由程序规则或可核对的证据来做。**

## 1.2 一轮对话在 harness 里怎么走

学生每做一个动作（提交作答、要提示、要讲解、回答追问、开始检查），harness 都按同样的流水线处理一次。代码见 `OpenEngine.operation()` 和 `_teach()`、`_decide()`。

```
学生动作
  │
  ▼
① 组装上下文      最近几轮对话、同一目标的历史证据、老师批注、材料片段
  │              （独立检查阶段清空历史，防止"带着提示做检查"）
  ▼
② 判定作答        确定性工具（代数）＋ 模型按评分要求判定
  │              → 判定必须引用学生原文，引用不上就不算证据
  │              → 可选：复核模型交叉判定，两个模型意见相反时转老师
  ▼
③ 选教学动作      纯函数策略：输入是观测，输出是动作（追问、定位、提示、讲解、转老师……）
  │              不调用模型，同一输入永远得到同一输出
  ▼
④ 写回复          模型按动作写回复
  │
  ▼
⑤ 审核回复        程序检查：不得出现参考答案或最终数值，不得替学生列式，追问只能有一个问句……
  │              模型审核：本模型或复核模型按同一份分级标准审核
  │              → 不通过：带着理由重写一次 → 还不通过：用按级别写好的模板
  ▼
⑥ 记录            回复、证据类型、完整轨迹（输入、决策、模型调用、状态变化）、证据依赖
```

## 1.3 关键设计

### 证据绑定：每个判定都要有学生原文作依据

模型判定时必须给出它依据的学生原句（`evidence_quote`）。程序会核对这句话是不是真的出现在学生的作答里（`evidence.ground()`）。核对不上就让模型重试一次；还不行，这次判定就记为"未知"并转给老师，**不算作证据**。

学生说"我懂了"、"会了"这类话，一律不算作答对的证据。

### 判定：工具优先，模型补充

见 [grading/assessment.py](../harness/grading/assessment.py) 的 `adjudicate()`：

- 代数工具能验证整道题时，以工具为准；
- 工具判错时，模型不能把结果推翻成对；
- 工具只能验证一部分时（比如只算对了数，没有解释、没写单位），剩下的由模型按评分要求判定；
- 配置了复核模型时：主模型拿不准，复核模型可以给出有原文依据的否定判定；主模型判错而复核模型高把握判对，就转给老师，不自动采纳任何一方。

### 教学策略：纯函数，可以回放

[core/tasks.py](../harness/core/tasks.py) 中的 `choose_v1` 到 `choose_v5`，后一版在前一版的基础上叠加规则，默认使用 v4。

| 版本 | 增加的规则 |
|---|---|
| v1 | 基本规则：答对 → 邀请独立检查；答错 → 追问依据；多次答错 → 换讲法；要提示 → 给提示 |
| v2 | 连续两次回复没通过审核 → 转老师；同一目标已有困难记录 → 先补先修知识 |
| v3 | **提示阶梯**：同一题第 1、2、3 次要提示，分别给概念级、步骤级、半完成例题 |
| v4 | 能逐字定位到学生的错误时，指出位置再追问（`locate`），不直接给改正结果 |
| v5 | 连续几轮追问没有进展时，自动升级到下一级提示；阶梯用完就转老师 |

策略不调用模型，所以每一轮的输入和决策都记在轨迹里。老师可以点"回放"，用同一版策略重算所有决策，核对是否一致，整个过程不调用模型。

### 回复审核：程序检查＋模型审核＋兜底

见 [core/engine_feedback.py](../harness/core/engine_feedback.py)。

- **程序检查**（确定性）：
  - 不得包含参考答案；
  - 提示里不得出现最终数值，也不得用题目里的数字替学生列式；
  - 完整讲解必须得出最终答案；
  - 不得声称"你已经掌握"；
  - 回复语言必须和学生一致。
- **模型审核**：写作和审核用的是同一份分级标准（`HINT_RULES` / `HINT_REVIEW`）。配置了复核模型时，提示和讲解由复核模型把关，不通过时也由它重写。
- **兜底**：重写后仍不通过，提示用按级别写好的模板，保证阶梯不中断；其他动作用通用追问句。连续两次落到通用句，就转给老师。
- **如实标注**：学生实际收到的是模型写的回复还是模板，记录在 `delivered_action` 里，界面按它显示标签。

### 证据类型：区分"做对了"和"学会了"

| 证据类型 | 含义 |
|---|---|
| `practice_complete` | 练习阶段，没有用过帮助就做对 |
| `assisted_complete` | 练习阶段，在提示或讲解帮助下做对 |
| `independent_model_assessed` | 独立检查（新题、不提供帮助）中做对，由模型判定 |
| `independent_verified` | 独立检查中做对，由工具验证 |
| `dialogue_only` / `interaction_only` | 只有对话，不构成能力证据 |

**独立检查必须用学生没见过的新题。** 题目从题库抽取，或者由模型生成后经过审核；同一目标下见过的题目都会排除。

### 学习记录：由证据推导，不由模型写入

[core/memory.py](../harness/core/memory.py) 按"班级 + 课程 + 知识点"汇总证据，得出每个目标的状态：

- 证据不足；
- 需要帮助；
- 练习完成；
- 辅助完成；
- 已独立完成；
- 需要重新检查。

这些状态全部由证据推导，**模型不能直接写入**，也没有未经校准的"掌握度分数"。复习间隔按 1、3、7、14、30 天的规则递增。学生开始新任务时，相关的历史记录会作为上下文提供给模型，但只限同一个班级和同一门课程。

### 改判传播

老师改判某次作答以后，所有用到这条证据的后续会话（例如参考了它的历史记录）都会被标记为"需要重新检查"。依赖关系在每轮对话时写入 `evidence_dependencies` 表，改判时用广度优先搜索沿着依赖链找出全部下游会话（见 [core/dependencies.py](../harness/core/dependencies.py)）。

### 预算与可重放的模型调用

- 每轮对话最多调用 12 次模型，每次结构化输出最多修复 1 次。
- 每次模型调用都经过 `ReplayableModel` 记录（[persistence/replay.py](../harness/persistence/replay.py)）。遇到数据库写冲突需要重跑时，直接复用已经记录的结果，不会重复调用模型，学生也不会看到两次不同的回复。

## 1.4 Harness 的代码位置

| 职责 | 文件 |
|---|---|
| 单轮流水线编排 | `core/open_engine.py` 中的 `operation`、`context`、`_teach`、`_decide` 等 |
| 教学策略、提示阶梯 | `core/tasks.py`、`core/pedagogy.py` |
| 证据绑定、泄露检查、兜底句 | `core/evidence.py` |
| 回复审核、重写、模板 | `core/engine_feedback.py` |
| 判定（工具、评分要求、分项评分、交叉复核） | `grading/` |
| 证据类型和学习记录 | `core/memory.py` |
| 改判传播 | `core/dependencies.py` |
| 提示词、结构化输出、分级标准 | `llm/` |
| 模型调用记录与重放 | `persistence/replay.py` |
| 给模型用的工具、知识点库 | `resources/toolbox.py`、`resources/knowledge.py` |

合计约 3100 行，约占全部 Python 代码的一半。

## 1.5 效果（v0.10 验收）

- 提示阶梯：配置复核模型时 16/16 走完三级，没有一次转老师，没有一次泄露答案；
- 完整讲解：16/16 送达，全部得出最终答案；
- 浏览器端到端检查 16/16，自动化测试 174 个全部通过。

详细数据见 [FEATURES_AND_TESTS.md](FEATURES_AND_TESTS.md)。

---

# 第二部分：交互系统（后端与前端）

## 2.1 总体结构

```
浏览器（原生 HTML/CSS/JS，无构建步骤）
  index.html    入口，选择身份
  student.html  学生工作台
  teacher.html  教师工作台
        │  REST / JSON，课堂模式下带 Bearer token
        ▼
FastAPI（harness/app.py，uvicorn 运行）
  interfaces/api.py     路由，把领域错误映射成 HTTP 状态码
  interfaces/access.py  账号与逐路由权限（Guard）
  teaching/             班级、布置、跟进任务、练习包、学习路径、看板、复核队列
  persistence/storage   SQLite（WAL 模式）、读快照、写冲突重试
        │
        ▼
  harness（第一部分）
```

## 2.2 后端

### 两种运行模式

| 模式 | 用途 | 认证方式 |
|---|---|---|
| 本地演示 | 单机开发、演示 | 不需要登录，所有功能都开放 |
| 课堂模式 | 真实使用 | 由 `HARNESS_ACCOUNTS_FILE` 指定账号文件，每个 token 对应角色（学生或老师）、班级和学生姓名 |

课堂模式下：

- token 用常量时间比较；
- 账号文件修改后自动重新加载；
- 文件读不了时返回 503，不会退回到无认证状态。

**本地演示模式不能暴露到公网。**

### 逐路由权限

每个 API 路由都必须声明一项权限检查（`Guard`），测试会逐一检查所有 `/api` 路由，漏掉一个测试就失败。

| 检查 | 含义 |
|---|---|
| `teacher(class_id)` | 只能是这个班的老师 |
| `member(class_id, learner)` | 学生只能访问自己的数据，老师只能访问本班学生 |
| `session(sid)` | 只能访问属于自己（或本班学生）的会话 |
| `local_only` | 旧版接口，只在本地演示模式下开放 |

### 主要 API

| 分组 | 接口 | 说明 |
|---|---|---|
| 身份 | `GET /api/me`、`/api/meta` | 当前账号、版本和配置 |
| 学生对话 | `POST /api/sessions`、`POST /api/sessions/{sid}/{action}`、`GET /api/sessions/{sid}` | 新建会话；执行动作（转给 harness 的 `operation()`）；读取会话 |
| 学生任务 | `GET /api/learning/inbox`、`/followups`、`/next` | 待做的布置、跟进任务、推荐的下一步 |
| 教师看板 | `GET /api/teacher/dashboard`、`/classes`、`/memory/{learner}` | 班级学情、复核队列、单个学生的学习记录 |
| 布置与内容 | `/api/teacher/assignments`、`/followups`、`/packs`、`/packs/generate`、`/paths` | 发布题目、布置跟进、练习包（含批量出题）、学习路径 |
| 复核与追溯 | `/api/teacher/sessions/{sid}/trace`、`/replay`、`/tasks/{sid}/attempts/{eid}/corrections`、`/reviews` | 查看轨迹、回放策略、改判、确认 |
| 辅助 | `/api/materials/extract`、`/teacher/algebra/preview`、`/teacher/knowledge/*` | 上传材料提取文本、代数检查预览、知识点建议 |

### 幂等与并发

- **幂等**：每个写操作都带一个请求键（`requestKey`）。同一个请求重复提交（网络重试、重复点击）时，直接返回第一次的结果。
- **乐观并发**：动作请求带上会话版本号，版本不一致返回 409，提示用户刷新。
- **存储**：SQLite 使用 WAL 模式。每个操作先在读快照里执行；只有真正需要写的时候才可能冲突，冲突就以写锁重跑，并复用已记录的模型结果。模型调用期间不持有写锁，所以一个学生在等模型回复时，不会挡住其他学生。

  | 指标 | 改进前 | 改进后 |
  |---|---|---|
  | 单次操作开销 | 27 ms | 2 ms |
  | 40 个并发写 | 3.5 s | 约 1 s |
  | 峰值内存 | 2.5 GB | 约 130 MB |

### 业务模块（`teaching/`）

| 模块 | 内容 |
|---|---|
| `teacher.py`、`engine_teacher.py` | 班级、布置、跟进任务、看板（学情行、复核队列、需要帮助的分组）、改判 |
| `preparation.py`、`engine_preparation.py` | 练习包：保存、编辑、批量生成、发布 |
| `planning.py`、`engine_planning.py` | 学习路径：按知识点编排步骤，根据学生表现推荐下一步 |

## 2.3 前端

### 技术选择

不用框架，也不需要构建：三个 HTML 页面、三个 JS 文件、一个 CSS 文件，由 FastAPI 直接提供静态文件。这样做的好处：

- 部署时不需要 Node 环境；
- 改完刷新就能看到效果；
- 代码量小，任何人都能读懂。

| 文件 | 内容 |
|---|---|
| `common.js` | API 调用、HTML 转义和模板、提示消息、请求键、图片读取、各种标签文字（教学动作、状态、证据类型）、课堂模式的登录 |
| `student.js` | 学生工作台 |
| `teacher.js` | 教师工作台 |
| `app.css` | 设计变量（颜色、间距）、深色模式、手机适配 |

### 学生工作台

左侧是任务列表（布置、跟进任务、自己发起的题目），右侧是对话区：

- 对话区按时间显示学生的作答和系统的回复，每条回复带标签（如"第二级提示"、"按模板提示"），标签取自 `delivered_action`，学生能看到自己实际收到的是哪一种帮助；
- 底部的输入框和按钮与 harness 的动作一一对应：提交作答、要提示、要讲解、回答追问、开始独立检查、回到原题、上传图片；
- 进入独立检查后，提示和讲解按钮自动隐藏，界面上也做不到"带着提示做检查"。

### 教师工作台

按页面地址的 `#` 部分切换五个页面：

| 页面 | 内容 |
|---|---|
| 看板 | 班级概况指标、每个学生的学习状态、需要复核的作答、需要帮助的学生分组 |
| 发布 | 编辑题目、评分要求、参考答案和检查要求，可以预览代数检查结果；未发布的草稿会自动保存 |
| 练习包 | 批量出题、编辑后发布到班级 |
| 学习路径 | 按知识点编排练习顺序 |
| 复核 | 查看单次作答的原文、判定依据、完整轨迹；改判、确认、回放策略 |

老师只有一个班时自动选中，有多个班时用按钮切换。

### 验证方式

- `scripts/ui_preview.py`：用脚本模型和预置的示例班级启动，不需要 GPU，适合开发和截图；
- `scripts/capture_ui.py`：用浏览器脚本检查各个页面；
- `scripts/capture_functional_audit.py`：接真实模型做端到端验收。

---

## 附：讲解建议

如果分两次讲：

1. **Harness**：先讲 1.1 的四类风险，再按 1.2 的流水线讲一轮对话，最后讲 1.3 中的证据绑定、提示阶梯、回复审核、证据类型。可以用 v0.10 提示阶梯的修复作为例子（[FEATURES_AND_TESTS.md 第 5 节](FEATURES_AND_TESTS.md#5-提示阶梯与完整讲解的修复)）。
2. **交互系统**：先讲第 0 节的分界，说明前端和后端只通过一个接口调用 harness；再讲两种运行模式和逐路由权限；最后现场演示学生和教师两个工作台。

两次都可以用第 0 节那张图开头，让听众先知道自己在听哪一层。
