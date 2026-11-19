# 教师工作台开发参考

本文描述 `teacher-agent` 分支上教师工作台（`edu_core/` + `edu_app/`）的**当前实现**，供接手开发、排查问题或对接其他系统（如 coursemaker）时查阅。设计背景和决策过程见 [TEACHER_AGENT_DESIGN.md](TEACHER_AGENT_DESIGN.md)，页面使用见 [TEACHER_AGENT_GUIDE.md](TEACHER_AGENT_GUIDE.md)。代码和本文不一致时以代码为准，并请更新本文。

最后更新：2026-11-19。

## 目录

1. [原则](#1-原则)
2. [运行与目录](#2-运行与目录)
3. [数据模型](#3-数据模型)
4. [模型接入与上下文](#4-模型接入与上下文)
5. [各阶段流程](#5-各阶段流程)
6. [课程标准](#6-课程标准)
7. [批改](#7-批改)
8. [阶段状态与“下一步”](#8-阶段状态与下一步)
9. [接口](#9-接口)
10. [前端](#10-前端)
11. [测试](#11-测试)
12. [开发约定](#12-开发约定)
13. [已知局限](#13-已知局限)

## 1. 原则

- **模型起草，老师确认。** 模型的输出都先存为草稿（或候选），老师确认后才能作为下一步的输入。下游只读已确认的版本。
- **能核对的由服务器核对。** 选择/判断/填空、数值与单位、代数式、化学方程式由工具判定；模型给出的学生原文引用、课标条目编号、证据编号都由服务器核对，核对不过的丢弃或转老师。
- **上下文显式声明。** 每类模型任务只能读白名单里的内容；实际读取的清单随每次调用保存，也是界面上“已参考”的来源。
- **只增版本，不覆盖。** 任何修改都产生新版本，旧版本保留；上游实质修改后，下游被标记“依据已变化”。
- **一切可追溯。** 每次模型调用（角色、请求模型、实际返回模型、提示词版本、上下文清单、请求、响应、用量、耗时）都写入 `generations` 表，可导出。

## 2. 运行与目录

启动、环境变量见 [README](../README.md#快速开始)。入口 `edu_app.api:app`（`create_app(workspace)` 可注入任意 `Workspace`，测试用假模型）。

| 文件 | 行数（约） | 职责 |
|---|---|---|
| `edu_core/workflow/` | 2750 | `Workspace`：由各阶段模块组合而成。`base`（存储、模型、产物操作、`_plan`）、`projects`（项目、材料、课标、进入下一课时）、`lesson_plan`（01）、`questions`（02）、`submissions`（03）、`analysis`（04）、`review`（05）、`records`（依据已变化、概览、导出）；`schemas`（模型输出结构）、`prompts`（系统提示词）、`common`（`normalize_plan`、`usable_drafts` 等）、`steps`（`next_steps`、`stage_status`） |
| `edu_core/artifacts.py` | 250 | 产物的创建、编辑、确认、引用、过期标记与解除、按谱系查询 |
| `edu_core/context.py` | 95 | 上下文白名单 `WHITELIST`、`Context`、`describe()`（“已参考”文字） |
| `edu_core/grading.py` | 340 | 无会话批改 `grade()` |
| `edu_core/tools.py` | 310 | 确定性核对：`match_choice`、`match_true_false`、`check_numeric`、`algebra_equal`、`chem_balanced` |
| `edu_core/subjects.py` | 115 | 学科包：名称、允许题型、错误标签、写作维度；`QUESTION_TYPES`、`KEYED_TYPES`、`SUGGESTED_ONLY_TYPES` |
| `edu_core/curriculum/` | 150 + JSON | 课标条目加载、匹配、上下文、等级检查；`<subject>.json` 共 10 个 |
| `edu_core/llm.py` | 160 | 网关客户端 `Gateway`、`strict_schema`、`parse_json`、`ModelUnavailable` |
| `edu_core/storage.py` | 140 | 表结构、迁移（启动时补列）、`Store.tx()` / `read()`、`record_generation`、`event` |
| `edu_core/text.py` | 75 | `verbatim_quote`、`loose_match`、`normalize_answer`、`sha`、`dump` |
| `edu_app/api.py` | 460 | FastAPI 路由、错误码映射、静态资源版本化 |
| `edu_app/static/` | 1400 | 单页前端：`index.html`、`app.js`、`app.css` |
| `scripts/edu_build_curriculum.py` | 525 | 从课标 PDF 构建条目 JSON |
| `scripts/edu_smoke.py`、`edu_eval_grading.py` | | 真实模型冒烟测试、批改评测 |

## 3. 数据模型

SQLite（WAL），默认 `data/edu.sqlite`。表结构在 `storage.py` 的 `SCHEMA`。改动表结构时在 `MIGRATIONS` 里加一个新版本号（按顺序执行，记在 `PRAGMA user_version`），新建库和已有库走同一套迁移；第 1 版补上了 `projects` 表的 `knowledge_module`、`lesson`、`learner_profile`、`prev_project_id`、`carryover`、`lesson_scope`、`curriculum`。

| 表 | 内容 |
|---|---|
| `projects` | 一个课时一个项目：标题、学科、年级、说明；课时信息（知识模块、课时、本课时内容、学生基础）；`curriculum`（JSON：`ids`、`off`、`chosen_by` = `auto`/`teacher`）；`prev_project_id` 与 `carryover`（JSON 学情快照，见 5.6） |
| `materials` | 参考材料全文，`family_id` 标记同源材料（研究切分用） |
| `learners` | 学生代号（不存真实姓名） |
| `artifacts` | 所有产物的所有版本 |
| `artifact_refs` | 产物引用了哪些上游版本（含内容 sha 和角色） |
| `stale_marks` | “依据已变化”标记及处理结果 |
| `generations` | 每次模型调用的完整记录 |
| `choices` | A/B 选择结果 |
| `events` | 业务事件流水（创建、确认、改判、导入……） |

**产物**（`artifacts.TYPES`）：`lesson_plan` 备课方案、`question` 题目、`rubric` 评分标准、`submission` 学生作答、`grading` 批改、`analysis` 学情分析、`problem_pattern` 问题模式、`comparison` 两轮对比、`review_plan` 讲评稿（`objective` 为旧类型，现由备课方案内的目标取代）。

- **版本**：同一谱系（`lineage_id`）内 `version` 递增。状态 `candidate`（A/B 候选）→ `draft` → `confirmed` → `superseded`。确认新版本时，同谱系旧的已确认版本变为 `superseded`。`A.current()` 返回每个谱系最新的非 superseded 版本，并附 `confirmed_version` 和 `stale`。
- **编辑**：`A.edit()` 以老师身份在同谱系新建草稿（`origin='edited'`，`edit_of` 指向来源），继承上游引用。确认已确认过的谱系时要给 `change_kind`：`substantive`（实质修改）或 `cosmetic`（文字修改）。
- **过期传递**：实质修改确认后，沿 `artifact_refs` 标记下游。规则：
  - `submission` 永不标记；
  - `lesson_plan`、`question`、`rubric`、`review_plan`（老师确认的内容）会被标记，但标记不再往下传；
  - `grading`、`analysis`、`problem_pattern`、`comparison`（计算结果）继续往下传。
  标记在“记录与导出”里处理：保留原结果，或重新批改（批改类）。
- **轮次**：`round` 字段。第 1 轮是首次练习；“讲评后再练”产生第 2、3…轮，各轮的题目、作答、批改、分析独立。
- **备课方案的内容**（`normalize_plan`）：`objectives`（`id` 为稳定编号 O1、O2…，编辑时保留，新目标顺延；`text`、`success_criterion`；可选 `standard_ids`、`standard_refs`、`checks`、`beyond_reason`、`inferred`）、`key_points`、`difficulties`、`activities`（`name`、`minutes`、`description`、`objective_ids`）、`notes`、`curriculum`（起草时提供的课标条目编号）。
- **题目的内容**：`no`（题号，轮内唯一，修改和换题不变）、`type`、`stem`、`options`、`answer_key`、`accepted`、`reference`、`objective_ids`（备课方案目标编号）、`target_patterns`（再练题针对的问题模式谱系）、`round`、`difficulty_requested`、`purpose`、`checks`（见 5.2），换题版本另有 `regenerated_from`、`instruction`。评分标准单独成谱系，`question_lineage` 指向题目谱系，`points` 每项有 `id`、`description`、`score`、可选 `check`（`kind` + `expected`）和 `target_pattern`。

## 4. 模型接入与上下文

**网关**（`llm.Gateway`）：OpenAI 兼容的 `/chat/completions`，`response_format` 用 `json_schema`（`strict_schema()` 把 pydantic 模型转成严格 schema，保留属性名）。返回内容用 pydantic 校验；不通过时把错误发回模型修一次，仍不通过抛 `ModelUnavailable`（接口返回 503）。网络错误重试 3 次。记录 `model_requested` 和 `model_returned`（网关可能路由到不同版本）。推理模型的思考占输出 token，`max_tokens` 默认 6000。

**角色**（`config.DEFAULT_MODELS`，可用 `EDU_MODEL_<ROLE>` 覆盖）：

| 角色 | 默认模型 | 任务 |
|---|---|---|
| `generator` | DeepSeek-V4.1-Flash | 备课方案、出题、换题、导入试卷、再练题、讲评稿 |
| `grader` | DeepSeek-V4.1-Flash | 批改 |
| `analyst` | DeepSeek-V4.1-Flash | 学情分析、易错点预判 |
| `reviewer` | qwen3.8-flash | 失分判定盲审、满分抽检、出题独立解题、题目审核 |

主判与复核用不同厂商的模型，降低同源偏差。

**上下文白名单**（`context.WHITELIST`）：任务读取白名单外的内容会抛 `ContextViolation`；产物默认必须是已确认版本。

| 任务 | 可读 |
|---|---|
| `draft_lesson_plan` | 项目、材料、课程标准、上一课时学情 |
| `generate_questions`（含换题） | 项目、材料、备课方案、课程标准、上一课时学情 |
| `import_paper` | 项目、材料 |
| `self_check_solve` | 题干（不含答案） |
| `review_question` | 题干、备课方案、课程标准 |
| `grade` / `review` | 题目、评分标准、这一份作答（不含该生历史、其他学生作答） |
| `analyze` | 项目、备课方案、题目、评分标准、批改、作答、已有问题模式 |
| `pitfalls` | 项目、备课方案、题目、评分标准 |
| `practice` | 项目、备课方案、题目、评分标准、问题模式、讲评稿 |
| `draft_review` | 项目、备课方案、题目、评分标准、学情分析、问题模式 |

`describe(manifest)` 把清单转成“已参考：项目背景、备课方案第 2 版（已确认）、课程标准 3 条……”。

**提示词版本**（写入 `generations.prompt_version`）：`lesson-plan-v4`、`generate-v3`、`regenerate-v1`、`self-check-v2`（独立解题与题目审核）、`import-v1`、`grade-v1`、`analyze-v1`、`pitfalls-v1`、`practice-v1`、`review-plan-v1`。改提示词时升版本号。

## 5. 各阶段流程

方法都在 `Workspace` 上。模型调用一律在事务外进行，结果算好后再开短事务写入（见第 12 节）。

### 5.1 01 备课设计

- `update_project(pid, knowledge_module=, lesson=, lesson_scope=, learner_profile=, notes=, curriculum_ids=, curriculum_off=)`：课时信息。改学科只在还没有题目时允许。知识模块或本课时内容变化时，自动选择的课标会清空重选；老师的选择不受影响。
- `curriculum(pid, q=None)`：对应课标的候选和当前选择（见第 6 节）。
- `draft_lesson_plan(pid)`：
  1. 前提：有知识模块或材料，否则 409。
  2. 解析课标：老师选择 → 自动选择（并保存为 `auto`）→ 无；勾选“不参考课标”时不用。
  3. 输入：学科、年级、知识模块、本课时内容、课时、学生基础、老师说明、材料（≤8000 字）、课程标准（内容要求原文及编号、同主题的学业要求和教学提示）、上一课时学情（如有）。
  4. 提示词 `LESSON_PLAN_SYSTEM`：2–4 条可检验目标，重点 2–4、难点 1–3、活动 3–6 且总时长不超过课时；有课标时每条目标填 `standard_ids`，要求不高于课标用词，超出须 `beyond_standard` + 理由；有本课时内容时只围绕它；有上一课时学情时安排简短回顾。
  5. 服务器：丢弃不在提供范围内的课标编号；活动的目标序号越界则去掉；`normalize_plan` 编号；`CUR.plan_checks()` 写入 `checks`（未对应课标、要求高于课标）和 `standard_refs`。
  6. 存为草稿，与已有方案同谱系。老师编辑（`edit`）时按目标编号继承引用，并重新检查。确认后才能出题。

### 5.2 02 出题改题

- `generate_questions(pid, types=, count=, difficulty=, purpose=, notes=, objective_ids=, ab=)`：
  1. 前提：备课方案已确认。`objective_ids` 只为指定目标出题（“补 1 道”）。
  2. 输入：已确认目标与达成标准、重点、难点、本课时内容、目标所引课标条目原文与教学提示、课堂活动（目标序号换算成本次列表的序号）、备课备注、上一课时学情、学生基础、材料（≤6000 字）、允许题型、每目标题数、用途、难度、出题说明。
  3. A/B：勾选或按 `EDU_AB_RATE` 随机，出两版（第二版提示偏真实情境），位置随机，存为 `candidate`。`choose(pid, group, verdict)` 记录 A/B/差不多/都不合适，采用的一版转草稿并编号。
  4. `usable_drafts`：题型不在允许范围的丢弃；没有评分要点的，选择/判断题补一个“答案正确”，其他丢弃；丢弃原因返回给界面。
  5. 每道题并发 `_self_check(p, d, review)`，结果写入 `checks`：
     - **题目审核** `_quality`（reviewer）：是否考查目标、是否超范围（前置知识可用）、估计难度、题目问题 → `checks.difficulty`、`checks.flags`；
     - **数值核对**：要点的 `check_expected` 与参考答案最终数值不一致 → `reference_mismatch`；
     - **独立解题**（reviewer，看不到答案；选项带字母；输出 `final_answer`），再用 `grading.grade` 按评分要点评这份解答，满分为 `agree`，否则 `disagree`；写作题跳过；
     - `answer_suspect = disagree 或 reference_mismatch`。
- `regenerate_question(qid, instruction, difficulty)`：同题型、同目标（再练题同问题模式），把原题和老师要求交给模型，经自检与审核后存为**同一题目谱系的新版本**（题号不变），评分标准也存为原评分标准谱系的新版本。
- `adopt_solver_answer(qid)`：用独立解题的最终答案和过程替换标准答案和参考答案，存为新草稿。
- `coverage(pid, round)`：每条目标的题数、确认数、待核对题号，以及没有对应目标的题。
- `import_paper(pid, text)`：保留原文整理成题目；反推目标，已有目标复用序号，新目标合并进备课方案的新草稿版本（标 `inferred`）。
- `create_question(pid, question, rubric)`：手工添加。
- `confirm(qid)`：题目和它的评分标准一起确认（批改两样都要）。

### 5.3 03 作业批改

- `add_learners`、`import_submissions(pid, csv, round_)`：CSV 每行 `学生代号,题号,作答`；名单外的代号列出，不导入。
- `grade_round(pid, round_)`：对没有批改或依据已变化的作答调用 `grading.grade`（第 7 节），并发执行；没有变化不重批。
- `review_queue(pid, round_)`：标记 `needs_teacher` 的草稿批改（有待定要点、复核分歧、写作建议分等风险）。`override(gid, points, note)` 老师逐要点改判，存为新版本。`confirm_gradings(pid, round_, ids)` 批量确认。

### 5.4 04 学情分析

- `analyze(pid, round_)`：本轮还没有作答时，只给出“可能的易错点”预判（`mode='pitfalls'`，上下文 `pitfalls`），不作为学情结论；有作答时，前提是本轮批改都已确认。
  1. 服务器统计：各题、各要点的得分率，失分的错误标签分布。
  2. 把失分作答编成证据 E1、E2…（含学生原文），连同已有问题模式交给 analyst。
  3. 模型输出问题模式（每个必须引用证据编号和逐字原文）、概述、证据不足时的补查问题。
  4. 服务器：编号不存在或引文不是原文的证据丢弃；没有证据的模式丢弃并记录；沿用已有模式的存为该谱系新版本；按证据计算涉及学生数。
  5. 全部满分时不调用模型，直接给出概述。

### 5.5 05 讲题答疑

- `draft_review(pid, round_, pattern_ids)`：讲解重点、每个问题模式的关键步骤、典型错误（必须从该模式的学生原文中逐字摘录，服务器核对，不合格清空并注明）、错误辨析、课堂追问、结语。序号越界的段落丢弃。
- `practice(pid, pattern_ids, count, round_)`：针对已确认的问题模式出再练题，进入新一轮；参考已确认讲评稿的重点；经自检和审核（审核时对照问题模式）。
- `compare(pid, round_a, round_b)`：按问题模式统计改善、仍存在、新出现、未再测的学生。

### 5.6 进入下一课时

`next_lesson(pid, knowledge_module=, lesson=, title=)`：前提是有已确认的问题模式或学情概述。新建项目，沿用学科、年级、说明、学生基础和名单；课时“第 N 课时”自动加一；本课时内容和课标选择留空。`carryover` 冻结保存：上一课时的已确认问题模式（含最近一次两轮对比得出的再练情况）、学情概述、讲评重点、目标。之后上一课时的修改不影响它。备课和出题都会读它（上下文 `carryover`）。

### 5.7 导出

`export(pid)`：项目、材料、学生代号、全部产物版本、引用、模型调用、A/B 选择、事件、过期标记、每次编辑改了哪些字段，以及统计清单。学生只以代号出现。

## 6. 课程标准

**数据**：`edu_core/curriculum/<subject>.json`，由 `scripts/edu_build_curriculum.py <subject> <pdf>` 从教育部《普通高中课程方案和语文等学科课程标准（2017年版2020年修订）》官方附件生成（`source_url` 记录下载地址，`source_sha256` 记录 PDF 哈希；PDF 不入库）。收录必修与选择性必修；语文为 18 个学习任务群；人工智能用《信息技术》课标。

每个条目：

| 字段 | 含义 |
|---|---|
| `id` | 如 `math.B2.2.3.2`、`history.B1.1.8`、`english.B6.8`；`B` 必修、`X` 选择性必修 |
| `kind` | `content` 内容要求、`requirement` 学业要求、`hint` 教学提示（数学另有 `intro` 单元说明） |
| `label` | 位置路径，如 `必修·函数·幂函数、指数函数、对数函数·对数函数②` |
| `short` | 引用时的显示，如 `1.8 辛亥革命与中华民国的建立`（数学没有，取 `label` 末段） |
| `text` | 原文（PDF 文本规范化后的逐字子串） |
| `page` | PDF 页码 |
| `optional` | 选学内容（不作为考试要求），只有数学有 |

**构建与核对**：PDF 用 pypdf 抽文本，规范化（方正字体的数学斜体映射回字母、NFKC 但保留①②、去掉中文旁的空格、去页眉页脚和脚注）。数学单独解析；其他学科用配置（模块、分组、条目正则、两栏表、标题式条目、跳过块），语文、英语各有专门解析。每个条目记录由哪些连续行拼成，逐段检查是规范化全文的子串，任何一条不过就构建失败。改解析后要重跑全部学科并看条目数是否合理（测试里有每科的下限和检索用例）。

**匹配**（`CUR.search`）：查询是“知识模块 + 本课时内容”。按 2–4 字片段做 IDF 加权：条目自身标题 ×2、正文 ×1、所属单元/模块名 ×0.3；老师输入的完整词语（≥3 字）出现在条目标题路径中时另加分。结果保留不低于最高分 35% 的前 k 个。

**自动选择**（`Workspace._auto_pick`）：最高分低于 `WEAK_MATCH = 6` 视为没有明确对应，不选（界面提示老师自选）；否则选不低于最高分 75% 的条目，最多 4 条，不选选学条目。

**给模型的内容**（`CUR.context`）：选中的内容要求，加上同主题中与查询最相关的 2 段学业要求和 3 段教学提示。

**等级检查**（`CUR.plan_checks`）：动词等级 了解/知道/体会/感悟/感受/经历 = 1，理解 = 2，掌握/熟练/灵活运用 = 3；目标的最高等级高于所引条目的最高等级时标“要求高于课标”。其他学科常用的“说出、概述、阐明”等暂不分级。

**新增或更新一个学科**：在 `CONFIGS` 加配置（或写专门解析）→ 运行构建 → 抽查条目 → 在 `tests/test_edu_core.py` 的参数化测试里加检索用例。

## 7. 批改

`grading.grade(llm, subject=, question=, rubric=, answer=, review=True, audit_rate=0.0, rng=None)` 返回 `(result, metas)`。每个要点得到 `score`、`status`（`earned` / `partial` / `missed` / `undetermined`）、`source`（`key` / `tool` / `model` / `review` / `teacher`）、`quote`、`reason`、`error_tag`、`confidence`。

1. 选择、判断题和能匹配的填空答案由服务器判定（作答写的是选项内容时按内容对回字母）。
2. 带 `check` 的要点由工具判定（数值含单位换算、代数等价、化学方程式配平）；工具判不了的交给模型。
3. 其余要点一次模型调用。判定必须逐字引用作答（“没有作答”类失分除外）；引文不是原文时带反馈重试一次，仍不行则该要点待定。置信度不是 high 也算待定。错误标签必须来自学科包。
4. 高置信度且有引文的**失分**交给 reviewer 盲审（看不到主判结果）；两者都扣分才算扣分，有分歧转老师。
5. 写作类只给建议分，必须老师确认。判满分的作答按 `audit_rate` 随机抽检。

## 8. 阶段状态与“下一步”

- `stage_status`（针对最新一轮）：01 = 备课方案已确认；02 = 本轮至少一道题已确认；03 = 本轮作答都已批改且全部确认；04 = 有学情分析，且问题模式都已确认（或没有问题模式）；05 = 讲评稿已确认。
- `next_steps` 返回带 `tab`、`target`（页面元素选择器）、`hint` 的步骤：待选的 A/B 和依据已变化的产物优先，然后按 01–05 给出当前该做的事；第 2 轮起从 02 开始。
- 页面顶部的“下一步”只在阶段之间跳转（`CONTINUE` 映射），本阶段未完成时置灰并显示还差的那一步；05 有“生成巩固练习”和“进入下一课时”两个按钮，后者只要有已确认的问题模式就可用。

## 9. 接口

完整字段见运行中的 `/docs`。错误：`ValueError` → 400，`KeyError` → 404，`Conflict` → 409，`ContextViolation` → 403，`ModelUnavailable` → 503。

| 接口 | 用途 |
|---|---|
| `GET /api/meta` | 学科、题型、错误标签、模型配置状态 |
| `GET/POST /api/projects`、`GET/PATCH /api/projects/{pid}` | 项目列表、新建；概况（含 `next`、`stages`、`next_lesson`）、修改课时信息与课标选择 |
| `GET /api/projects/{pid}/curriculum?q=` | 对应课标候选与当前选择；`q` 为搜索 |
| `POST /api/projects/{pid}/next-lesson` | 进入下一课时 |
| `GET/POST /api/projects/{pid}/materials`、`POST .../materials/upload` | 材料；上传 .txt / .md / 文字版 PDF |
| `POST /api/projects/{pid}/lesson-plan/draft`、`GET .../lesson-plans` | 生成、读取备课方案 |
| `GET /api/projects/{pid}/artifacts?type=`、`GET/PUT /api/artifacts/{aid}`、`POST /api/artifacts/{aid}/confirm` | 按类型列产物；读取、编辑（新版本）、确认（`change_kind`） |
| `POST /api/projects/{pid}/questions/generate`、`GET/POST /api/projects/{pid}/questions` | 出题（可带 `objective_ids`）；本轮题目（按题号）；手工添加 |
| `GET /api/projects/{pid}/coverage?round=` | 目标覆盖 |
| `POST /api/questions/{qid}/regenerate`、`.../adopt-solver` | 换一道（`instruction`、`difficulty`）；采用独立解题的答案 |
| `POST /api/projects/{pid}/choices`、`.../import-paper` | A/B 选择；导入试卷 |
| `GET/POST /api/projects/{pid}/learners`、`GET/POST .../submissions` | 名单；作答（CSV） |
| `POST /api/projects/{pid}/grade`、`GET .../gradings`、`GET .../review-queue`、`POST /api/gradings/{gid}/override`、`POST .../gradings/confirm` | 批改、复核、改判、批量确认 |
| `POST /api/projects/{pid}/analyze`、`GET .../analyses`、`GET .../patterns` | 学情分析、问题模式 |
| `POST /api/projects/{pid}/review/draft`、`GET .../review-plans`、`POST .../practice`、`POST .../compare`、`GET .../comparisons` | 讲评稿、再练、两轮对比 |
| `GET /api/projects/{pid}/stale`、`POST /api/stale/{mark_id}/resolve` | 依据已变化的产物及处理 |
| `GET /api/projects/{pid}/export` | 研究导出（JSON） |

## 10. 前端

`edu_app/static/` 是无构建步骤的原生 JS 单页：

- `index.html`：顶部项目行；五个阶段标签（01–05）加“记录与导出”；每个阶段一个 `#tab-*`，顶部是 `.stage-head`（标题 + `.continue` 下一步）。
- `app.js`：全局状态 `S`（项目、概况、当前标签、课标勾选是否改动等）；`refresh()` 拉概况后渲染；每个阶段一个 `render*()`；按钮通过 `data-act` / `data-goto` / `data-continue` / `data-fill` 在少数委托监听器里处理；`busy()` 包装异步按钮；`api()` 统一请求和错误提示。
- 静态资源按内容哈希加 `?v=`，并返回 `Cache-Control: no-cache`，避免浏览器运行旧脚本。
- 页面上的文字面向老师，避免技术名词；每个阶段只放本阶段的内容，候选和次要信息折叠。

## 11. 测试

```bash
.envs/harness/bin/python -m pytest -q tests                 # 全部（教师端 + 学生端）
.envs/harness/bin/python -m pytest -q tests/test_edu_*.py   # 只跑教师端
```

- **假模型** `tests/edu_fake.py` 的 `FakeLLM(handlers)`：按任务名找处理函数，返回值经同样的 pydantic 校验；`calls` 记录每次调用的角色、任务、输入，测试可以检查模型实际收到了什么。新增模型任务时要在 `tests/test_edu_workflow.py` 的 `HANDLERS` 里加处理函数。务必用 `FakeLLM(dict(HANDLERS))`，测试会改处理函数。
- `test_edu_core.py`：工具、文本、批改、课标（每科条目与检索）；`test_edu_workflow.py`：完整闭环与各功能；`test_edu_api.py`：接口与错误码。
- **浏览器走查**：用假模型起一个服务（`create_app(Workspace(Store(db), FakeLLM(handlers), settings))`），再用 Playwright 走完 01–05、下一课时、记录与导出，检查页面没有脚本错误。脚本目前在开发者本地，没有入库。
- **真实模型**：`scripts/edu_smoke.py` 用临时库走一遍闭环（约 5 分钟、8 万 token）；`scripts/edu_eval_grading.py` 评测批改准确性。只用临时数据库，不要指向 `data/edu.sqlite`。

## 12. 开发约定

- **不要在事务里调用模型。** 先在只读快照里收集输入，事务外调用模型，最后开 `store.tx()`（BEGIN IMMEDIATE）一次写完。
- **密钥只从环境变量读取**（`EDU_LLM_API_KEY`），不写进代码、配置文件、文档或提交记录。
- **新增模型任务**：在 `WHITELIST` 声明可读内容 → 用 `Context` 收集并 `record_generation` 记录 → 输出定义 pydantic 模型 → 服务器校验模型给出的编号、引文、范围 → 在 `HANDLERS` 加假模型处理函数并写测试。
- **改提示词就升版本号**（`prompt_version`），研究导出靠它区分。
- **新增产物字段**要考虑：编辑时是否继承、是否影响过期传递、导出是否需要。
- 界面改动后用浏览器走一遍；只跑单元测试不能发现前端问题。

## 13. 已知局限

- 没有真实教师的使用评估。
- 课标匹配靠关键词，等级检查只覆盖部分用词；课标只收必修和选择性必修。
- 题目审核与独立解题依赖另一个模型，画图、多问类题目容易误报“答案存疑”。
- 作答只能以文字 CSV 导入；没有拍照作答、试卷照片切题。
- 单人本地使用，没有账号和权限；SQLite 只适合单机单进程。
- 与 coursemaker 的对接未做：可以把课时信息与对方的逐课目录互相映射，把确认后的备课方案作为对方讲义和课件的依据，见设计文档第 12 节。
