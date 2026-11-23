# Edu-Agent 教师工作台

帮高中老师走完一节课的教学闭环：**01 备课设计 → 02 出题改题 → 03 作业批改 → 04 学情分析 → 05 讲题答疑**，讲评后可以再练一轮（05 → 02），也可以带着这节课的学情进入下一课时（05 → 01）。覆盖语数英物化生政史地和人工智能 10 个学科。

核心原则：**模型起草，老师确认；能核对的由服务器核对。** 每一步的产物都要老师确认后才传给下一步；备课方案要对应课程标准原文，批改要逐字引用学生原文，学情归纳要有作答证据；每次模型调用的输入、输出和参考了哪些内容都记录下来，可以导出研究。

当前在 `teacher-agent` 分支开发，还没有真实教师参与的使用评估。原来的学生端教学 harness（`harness/`，v0.10）已冻结，见[学生端说明](docs/STUDENT_HARNESS.md)。

## 目录

- [能做什么](#能做什么)
- [快速开始](#快速开始)
- [配置](#配置)
- [代码结构](#代码结构)
- [开发与测试](#开发与测试)
- [已知限制](#已知限制)
- [学生端 harness（已冻结）](#学生端-harness已冻结)
- [相关文档](#相关文档)

## 能做什么

| 阶段 | 老师做什么 | 系统做什么 |
|---|---|---|
| 01 备课设计 | 填课时信息（知识模块、课时、本课时内容、学生基础），可选附材料；核对对应的课标条目 | 按课标条目生成一份备课方案：教学目标（含达成标准）、重点、难点、课堂活动。每条目标标出依据的课标条目；要求高于课标用词、没有依据的目标会被标出。整体确认 |
| 02 出题改题 | 选题型、数量、用途、难度；核对、修改、确认 | 按已确认的目标出题，参考课标条目、课堂活动和上一课时学情。每道题由另一个模型独立解题并核对参考答案、审核是否考到目标、是否超范围；发现问题自动带着意见重写一次；目标覆盖一目了然；可以单题“换一道”、采用独立解题的答案；部分题目另有一个替代版本，可以比较，不比较也可以 |
| 03 作业批改 | 导入学生名单和作答（CSV，或拍照导入一名学生的整份试卷并核对识别出的文字）；处理复核队列 | 逐个评分要点判定：能用工具核对的（选择、填空、数值与单位、代数式、化学方程式）以工具为准，其余由模型判定并必须逐字引用学生原文；高置信度的失分由另一家模型盲审复核；没把握的转老师。写作只给建议分 |
| 04 学情分析 | 确认问题模式 | 服务器统计得分率和失分标签；模型归纳问题模式，每个模式必须引用学生原文，服务器核对；证据不足时给出补查问题 |
| 05 讲题答疑 | 选择要讲的问题；确认讲评稿 | 生成讲评稿（讲解重点、关键步骤、典型错误辨析、课堂追问），典型错误必须是学生原文；生成针对问题模式的再练题（进入第 2 轮），两轮对比；“进入下一课时”把学情快照带进下一课时的备课 |

**一节课的闭环**（[首版设计](docs/LESSON_LOOP_DESIGN.md)，`lesson-loop` 分支）：第一次打开时填一次课程背景（课程名称、学科、年级、班级和学生名单），之后每节课自动带入。首页是“我的课”：每节课一行，标出五个阶段做到哪一步，“继续：……”直接打开要做的那一步。“新的一节课”写下主题，再选从备课开始、我有作业要批改（在 03 里直接导入试卷）或我想讲一道题（直接到 05）。题目确认后就可以导入作答批改，不需要“布置”；题目或评分标准做了实质修改，用旧版本得出的批改和学情会标“需要更新”。讲评后再练是这节课的第 2 轮，“进入下一课时”把已确认的学情带进下一节课的备课；新建一节课时也可以勾选带上最近一节课的学情。多班级、布置记录、课时安排、主题单元等能力仍在数据层（见[课程工作台设计](docs/COURSE_WORKSPACE_DESIGN.md)），首版界面不显示；研究记录和旧项目整理在“后台管理”。

贯穿各阶段的机制：

- **版本与确认**：所有产物（备课方案、题目、评分标准、批改、问题模式、讲评稿……）只增加版本、不覆盖；确认后才传给下一步。
- **依据已变化**：上游做了实质修改，引用旧版本的产物会被标记，可以保留原结果或一键重新批改。
- **已参考**：每次生成后显示模型实际读取了哪些内容（由上下文白名单记录，不是事后描述）。
- **阶段标题区**：本阶段完成后才出现“下一步”按钮（只在 01–05 之间跳转）；没完成时标题区不放按钮，要做的事在页面里。这一页还用不了时，标题下只有一条提示，写明原因和“去 0N ……”按钮。闭环走到哪一步，看标签上的“待办”。
- **课程标准**：10 个学科的课标条目从教育部官方 PDF 抽取，逐字核对，记录页码（见[开发参考第 6 节](docs/DEVELOPER_REFERENCE.md#6-课程标准)）。

## 快速开始

```bash
cd /default-filesys/workspace/ysb/Edu/edu-harness
export EDU_LLM_API_KEY=...            # 模型网关密钥，只放在环境变量里，不写进任何文件
.envs/harness/bin/python -m uvicorn edu_app.api:app --host 127.0.0.1 --port 7870
```

在自己电脑上转发端口后打开 http://127.0.0.1:7870 ，接口文档在 `/docs`：

```bash
ssh -N -L 7870:127.0.0.1:7870 <服务器>
```

- 不设密钥也能启动，可以查看已有数据；需要模型的按钮会提示“未配置模型”。
- 数据在 `data/edu.sqlite`。账号默认关闭（`EDU_AUTH=1` 启用，见下方配置），关闭时没有权限检查，只应监听 127.0.0.1；服务端也会拒绝非本机来源的请求（需要远程访问请设置 `EDU_ALLOW_REMOTE=1`，同时启用账号并前置 HTTPS）。
- 页面怎么用见[使用说明](docs/TEACHER_AGENT_GUIDE.md)。

## 配置

| 环境变量 | 默认 | 作用 |
|---|---|---|
| `EDU_LLM_API_KEY` | 无 | 网关密钥 |
| `EDU_LLM_BASE_URL` | `https://lightingtheword.com/v1` | OpenAI 兼容网关 |
| `EDU_MODEL_GENERATOR` / `_GRADER` / `_ANALYST` | `DeepSeek-V4.1-Flash` | 备课与出题、批改、学情分析 |
| `EDU_MODEL_REVIEWER` | `claude-sonnet-5-5` | 失分判定盲审、出题独立解题与题目审核、满分抽检 |
| `EDU_MODEL_READER` | `gemini-3.8-flash` | 拍照导入时把手写作答转成文字（需能看图；网关上的 DeepSeek-V4.1-Flash 看图加结构化输出会报 `bad_alloc`） |
| `EDU_AUDIT_RATE` | `0.05` | 判满分的作答被抽检的比例 |
| `EDU_AB_RATE` | `0.5` | 每条教学目标再独立生成一个版本、供老师比较的概率（按目标抽样，每目标最多一个比较，不设每日上限） |
| `EDU_BACKGROUND_CHECKS` | `1`（应用） | 出题接口先返回题目，自检和自动重写在后台完成；设为 `0` 则同步完成。A/B 两版随题目一起返回 |
| `EDU_MAX_TOKENS` | `6000` | 单次输出上限（推理模型的思考也占输出 token，太小会返回空内容） |
| `EDU_CONCURRENCY` | `8` | 批改、自检的并发数 |
| `EDU_DB` | `data/edu.sqlite` | 数据库路径 |
| `EDU_AUTH` | 无 | 设为 `1` 启用账号：需要登录，服务端逐条检查记录权限；用 `scripts/edu_users.py add <名字> [--admin]` 建账号 |
| `EDU_ALLOW_REMOTE` | 无 | 设为 `1` 才接受非本机请求；默认只允许 127.0.0.1 / ::1 访问 |

## 代码结构

```text
edu_core/              # 教师端核心，不依赖 Web 框架
├── workflow/          # Workspace：一个阶段一个模块
│   ├── __init__.py    # Workspace 由各阶段模块组合而成
│   ├── base.py        #   共用的存储、模型与产物操作，备课方案查询
│   ├── courses.py     #   课程、班级、任务、课时安排、本班学情、首页、旧项目整理
│   ├── assignments.py #   布置记录：固定版本、名单快照、更正本次评分依据
│   ├── projects.py    #   项目（任务工作区）、材料、课标选择、进入下一课时
│   ├── lesson_plan.py #   01 备课设计
│   ├── questions.py   #   02 出题改题（出题、审核、独立解题、A/B、导入试卷、换一道）
│   ├── submissions.py #   03 作业批改（名单、作答、批改、复核队列、改判）
│   ├── photos.py      #   03 拍照导入作答：识别成文字，老师核对后导入
│   ├── analysis.py    #   04 学情分析
│   ├── review.py      #   05 讲题答疑、讲评后再练、两轮对比
│   ├── records.py     #   依据已变化、概览（“下一步”）、导出
│   ├── scope.py       #   学生作答的作用域：布置记录（固定版本、名单快照）或项目自己的作答
│   ├── schemas.py     # 模型输出的 pydantic 结构
│   ├── prompts.py     # 各任务的系统提示词
│   ├── common.py      # 评分要点、题目内容、备课方案整理等公共函数
│   └── steps.py       # next_steps、stage_status（“下一步”与阶段状态）
├── artifacts.py       # 产物：版本、确认、上游引用、依据已变化
├── context.py         # 每类任务的上下文白名单与“已参考”
├── grading.py         # 逐要点批改、引用核对、复核、抽检
├── tools.py           # 确定性核对：选项、判断、数值与单位、代数式、化学方程式
├── subjects.py        # 10 个学科包：题型、判定方式、错误标签
├── curriculum/        # 课程标准条目（10 个学科的 JSON）与匹配、等级检查
├── llm.py             # 网关客户端：结构化输出、校验、一次修复
├── storage.py         # SQLite（WAL）表结构与事务
├── config.py、text.py
edu_app/               # FastAPI 接口与单页前端（static/：index.html、app.js、app.css）
scripts/edu_*.py       # 课标构建、真实模型冒烟测试、批改评测
tests/test_edu_*.py    # 教师端测试（脚本化假模型）
harness/               # 学生端 harness（v0.10，已冻结）
```

模块职责、数据模型、各阶段流程和接口列表见[开发参考](docs/DEVELOPER_REFERENCE.md)。

## 开发与测试

```bash
.envs/harness/bin/python -m pytest -q tests     # 全部测试（教师端 + 学生端），假模型、临时数据库，约 35 秒
EDU_LLM_API_KEY=... .envs/harness/bin/python scripts/edu_smoke.py --db /tmp/edu-smoke.sqlite   # 真实模型走一遍闭环
EDU_LLM_API_KEY=... .envs/harness/bin/python scripts/edu_eval_grading.py \
  --data bench/data/stepverify-zh.jsonl --out reports/edu/grading-stepverify-zh.json            # 批改准确性评测
.envs/harness/bin/python scripts/edu_build_curriculum.py <subject> <课标 PDF>                     # 重新生成课标条目
```

批改评测（中文数学 392 份，DeepSeek 主判、qwen 复核）：错误解答判为正确 3.1%，正确解答判为错误 0.5%，转老师 5.1%。这个数据集是翻译的小学应用题，不能代表高中各科。

## 已知限制

- **没有真实教师的使用评估**；各阶段的效果来自单元测试、真实模型抽查和浏览器走查。
- **课标匹配靠关键词**：课标没有出现老师用的词时（如生物课标写“叶绿体捕获光能”，不写“光合作用”），自动匹配会弱，需要老师自己勾选或搜索；“要求高于课标”的检查只认识“了解/知道/理解/掌握”一类用词。课标只收必修和选择性必修。
- **题目审核和独立解题依赖另一个模型**：画图、多问类题目容易被误标“答案存疑”。
- **拍照导入只做转写**：一次一名学生，识别结果必须由老师核对后才导入，批改依据的是核对后的文字；手写识别只用一份手写作业实测过（12 题中 10 题准确，有涂改的地方模型不会主动标“没把握”）。“导入试卷”还不支持照片。
- **单机部署**：账号默认关闭，关闭时没有权限检查，只接受本机请求；SQLite 只适合单机单进程。
- **账号是访问码登录**：没有密码找回和单点登录；公网部署仍需前置 HTTPS。教师文件不依赖第三方库，Word 为基本排版，PDF 由浏览器打印生成。
- **一节课的闭环是首版**：只面向一个班；用假模型测试和浏览器走查过，还没有真实教师试用。原课程工作台（多班级）：假模型测试、浏览器走查，加一次真实模型冒烟（2026-11-20，`scripts/edu_smoke_course.py` 19/19 通过，约 6 分钟、17 万 token，记录在 `reports/edu/smoke-course-2026-11-20.log`）；冒烟只说明流程走得通，不代表批改和分析的质量；阶段 C、D 与账号已有首版实现，限制见[设计文档 13.3](docs/COURSE_WORKSPACE_DESIGN.md)。
- **coursemaker 一侧尚未接入**：本系统只生成交换包、接收回传课件（见[设计文档 13.3](docs/COURSE_WORKSPACE_DESIGN.md)），没有修改 coursemaker 代码。

## 学生端 harness（已冻结）

`harness/` 是之前给 OmniEdu-9B 做的学生端教学 harness（v0.10，标签 `v0.10-student`）：学生作答由确定性工具和模型（必须引用原文）判定，教学动作由纯函数策略决定，回复经审核后发出，老师可以复核改判。主要结果：错误解答被判为正确从裸模型的 35–77% 降到 1.5–2.0%。完整说明、部署方式和评测见 [docs/STUDENT_HARNESS.md](docs/STUDENT_HARNESS.md)。

## 相关文档

**教师工作台**
- [docs/DEVELOPER_REFERENCE.md](docs/DEVELOPER_REFERENCE.md)：开发参考（最新）：架构、数据模型、各阶段流程、提示词与校验、课程标准、接口、测试。
- [docs/TEACHER_AGENT_GUIDE.md](docs/TEACHER_AGENT_GUIDE.md)：使用说明与验收记录。
- [docs/TEACHER_AGENT_DESIGN.md](docs/TEACHER_AGENT_DESIGN.md)：设计文档（决策背景、模型选型、评测计划，以及各次迭代的设计记录）。

**学生端 harness 与调研**
- [docs/STUDENT_HARNESS.md](docs/STUDENT_HARNESS.md)：学生端 harness 的完整说明（原 README）。
- [docs/FEATURES_AND_TESTS.md](docs/FEATURES_AND_TESTS.md)：学生端的功能说明，以及真实模型与浏览器验收记录。
- [docs/DESIGN_OVERVIEW.md](docs/DESIGN_OVERVIEW.md)：设计思路（讲解用）：为什么需要 harness、如何设计、教学系统如何设计、如何辅助教学。
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)：系统架构（技术参考），分 harness 和交互系统（后端、前端）两部分说明实现。
- [docs/HARNESS_AND_TEACHING_SYSTEM.md](docs/HARNESS_AND_TEACHING_SYSTEM.md)：harness 与教学辅助系统的区别和联系，以及本项目的定位。
- [docs/EDU_HARNESS_RESEARCH.md](docs/EDU_HARNESS_RESEARCH.md)：教育模型 harness 的作用、相关研究与开源实现、本项目的改进方向。
- [HARNESS_REVIEW.md](HARNESS_REVIEW.md)：学生端评估报告、逐项处理状态和全部评测结果。
- [CHANGELOG.md](CHANGELOG.md)：各版本的更新说明。
- [REFERENCES.txt](REFERENCES.txt)：设计参考的开源项目来源。
