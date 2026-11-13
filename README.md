# OmniEdu 教学 Harness

当前 bysz1 默认推理后端已切换为 **vLLM 0.23.0**；部署参数与命令见文末“bysz1 部署”。下文 Transformers 双卡设置保留为后备方案与历史记录。项目已纳入 git 管理。

## v0.7：vLLM 适配与教学策略补强

- **图片**：上传图片统一校正 EXIF 方向并缩到最长边1536像素（约1.7k视觉 token），可用 `OMNIEDU_IMAGE_MAX_SIDE` 调整；页面上传上限放宽到30MB。超出上下文的请求单独提示“输入材料或图片过长”，不再报“模型服务不可用”。
- **结构化输出**：`ModelClient.structured` 通过 `response_format: json_schema` 约束解码，原有任务说明与一次修复重试保留；`OMNIEDU_JSON_SCHEMA=0` 可关闭。约束只保证格式，不保证判定正确。
- **并发**：整题评估与各分项评估并行发出，共享同一轮调用预算（线程安全）。
- **分级提示**（策略 `open-task-policy-v3`）：同一任务内连续请求提示依次为概念提示、步骤提示、同构半完成例子；旧轨迹按原策略版本回放。
- **复习间隔**：按连续独立通过次数取1/3/7/14/30天，出现困难后重置；这是规则建议，不是遗忘模型。
- **教师复核队列**：抽检由固定 `id%10` 改为风险分层（负面判定、低置信、仅模型判定的独立检查、模型生成参考答案、分项未定），其余按哈希抽样约10%；每项列出风险原因。
- **部署**：上下文32768、最多32路并发、60%显存；启动后先预热文本、图片、长输入、批处理和 JSON schema 路径，再标记就绪。参数在 `service-config.json` 的 `vllm` 段配置。`requirements-vllm.lock` 改为完整版本锁定。

## v0.6：跨学科任务协议与批量备课

继续支持任意学科与多课程，不限制为单个单元。新增默认教学类型为开放任务、解题推理、概念理解、阅读证据、写作表达、实验分析和编程算法；类型用于选择教学指导和回退反馈，不是学科白名单。

### 页面操作

1. 打开 **批量备课与发布**，填写班级、备课包名称、参考材料，并在目标表中填写课程、知识点、学科、教学类型和学习目标。一次生成最多6个目标，以控制本轮模型预算；可多次创建不同课程的备课包。
2. 生成内容保存在草稿中。逐题编辑题目、评分要求、参考答案、独立检查要求和分项标准；可保存草稿，使用备课包编号恢复。分项表中的题号从1开始。
3. 填写学生名单，确认已核对全部内容并填写审核说明，然后发布。页面上的编辑与发布在同一事务提交；失败不会发布部分任务。留空学生名单时仅发布到教师题库。
4. 学生在 **我的班级任务** 填写班级并加载任务，下拉选择课程任务后直接开始，无需复制每题编号。
5. 原有教师学情、风险复核、课程路径、批量补救和改判失效机制继续使用。备课包可含多个课程；课程路径仍按课程组织。

### 教学契约与评分

- `task_type`：教学类型，可通过服务器端注册扩展；未注册类型使用开放任务策略。
- `check_requirements`：最多8条可迁移的检查要求，每条最多200字，例如“解释证据与结论的关系”，不要填写本题答案。
- `criteria`：最多4项评分维度。新生成检查题必须保留原分项的编号、类型、描述和知识点关联，重新计算新题的预期值；缺项会拒绝生成，不推进状态。完整原rubric也进入出题和审核上下文。语义覆盖仍有模型审核局限。
- `check_assignment_ids`：教师可选已审核检查题，须属于同一班级、课程、知识点和教学类型，并覆盖独立检查要求和分项。配置后从未见题中选择；题库耗尽时请教师补题，不自动降低到未审核题。未配置时仍支持模型生成、审核和参考答案检查。
- 分项通过不能覆盖整题的错误、未知或未完成判定；冲突进入教师风险队列。最终判定依赖模型整题判断时统一标记 `independent_model_assessed`，不把局部工具验证宣称为完整确定性证明。
- 对明确要求“引用并解释”的任务，只有指定引文、没有额外解释的作答不授予完成。该规则只覆盖直接缺失解释的情况，不证明论证正确。
- 分项模型评分接收当前已确认的图像证据；独立检查继续隔离旧题提示和图像。

### 可扩展教学和工具

`harness/pedagogy.py` 的 `register_profile(key, label, strategy)` 注册教学类型；`harness/criteria.py` 的 `register_grader(kind, callable)` 注册服务器端分项验证器。模型和学生不能安装或执行插件。

现有代数、数值、单位、解释缺项与因式分解验证继续使用，新增：

| 类型 | 预期值 | 验证范围 |
|---|---|---|
| `source_quote` | 材料中的指定原文 | 仅核对引文出现，不证明推断正确；缺少可核对材料时保留未知 |
| `word_count` | `最小值:最大值`，例如 `30:80` | 英文单词和中文单字计数，仅检查长度，不评价质量 |
| `model` | 可选参考要求 | 模型评价，必须有有效学生原文引用；不是确定性验证 |

实验数据可组合 `numeric`、`unit` 和解释分项；阅读和写作可组合来源、长度和语义分项。编程类型目前提供教学指导，没有运行学生代码的沙箱；不声称已覆盖每个学科的专门验证器。

### 备课 API

| 接口 | 用途 |
|---|---|
| `POST /api/teacher/packs` | 保存教师输入的备课草稿，最多30题 |
| `POST /api/teacher/packs/generate` | 按最多6个目标生成待审核草稿 |
| `GET /api/teacher/packs?class_id=...` | 查询本班备课包 |
| `GET /api/teacher/packs/{id}` | 恢复备课包 |
| `PUT /api/teacher/packs/{id}` | 携带version编辑草稿 |
| `POST /api/teacher/packs/{id}/publish` | 审核确认后原子发布，可附学生名单 |
| `GET /api/learning/inbox?class_id=...&learner=...` | 查询学生任务箱，不暴露参考答案或未见检查题 |

写入接口携带 `request_id`；发布接口需要 `version`、`reviewed: true`、`review_note` 和可选 `learners`。草稿有版本冲突保护，已发布包不可原位修改；重新备课请建新包。生成草稿标记为 `model_generated_unverified`，教师确认发布后标记为 `teacher_reviewed_model`。审核记录保存在 `preparation_events`。

新 API 沿用原班级认证隔离：学生不能访问草稿和答案，教师不能操作其他班级备课包。页面仍为本地可信演示；配置 `HARNESS_ACCOUNTS_FILE` 后仅开放认证 API，尚无登录网页。

### 验证与已知边界

```bash
.envs/harness/bin/python -m pytest tests -q --junitxml=reports/teaching-v06-tests.xml
.envs/harness/bin/python scripts/teaching_v06_e2e.py
```

真实模型脚本使用临时数据库，覆盖跨学科草稿生成、阅读完整性、物理教师题库检查和英语检查契约。结果见 `reports/teaching-v06-real.json`，首次失败见 `reports/teaching-v06-real-initial.json`。这些是固定案例执行与评分回归，不是教师盲评或学生学习收益实验。

本版未引入PDF教材库、自动知识图谱、校准后的掌握概率、LMS或并发数据库重构。复习周期、模型上下文和推理并发限制仍见后文；多课程工作流可用不代表已经通过整班并发验收。源码修改前快照位于 `/tmp/edu-before-v06.tar.gz`（临时目录，不是长期备份）。


默认入口已改为自定义教学任务，可输入不同学科的题目或学习目标、确认题目图片、提供学习材料。固定代数题库仅作为可选示例和旧会话兼容层。

## v0.5：分项评分、课程路径与教师跟进（历史版本，评分聚合已由v0.6修订）

在 v0.4 基础上增加以下闭环，原记录继续兼容：

- **分项评分**：教师可配置最多4项标准，分别判断结果、解释、数值、单位或因式分解形式。工具能确定的先验证，其余交给模型；无有效学生原文引用或置信不足则保留未知。配置分项后以分项聚合为最终判定，请完整列出必要要求。教师方程自动绑定符号验证；页面支持预览绑定和手动指定。
- **课程路径**：教师用知识点编号和先修编号发布无环课程图。下一步建议综合当前证据、未完成会话、到期任务与先修条件；历史独立通过后再次出错，不能靠辅助完成直接推进。
- **持久跟进**：按学生名单批量安排补救、复习或诊断，支持延期、撤销和审计记录。复习/诊断必须使用该学生未见过的题，作为独立检查；补救练习需要后续独立检查通过才完成。教师改判会将相关已完成跟进标为需重查。
- **教师复核**：优先显示未知判定、模型与工具冲突等风险，并按知识点和失败分项汇总建议补救组。可以“确认原判定”关闭待审项，也可改判并使依赖证据失效。学生仅看到分项状态，评分内部解释保留在教师证据中。
- **记忆修正**：跨会话事件按时间统一归并，避免会话创建顺序覆盖后发生的证据。

页面操作：先在“教师备课与跟进”发布任务及可选分项，再在“课程路径与补救安排”发布路径或安排跟进。学生填写跟进编号开始任务，可按路径编号查看下一步建议。

新增接口（请求字段与示例结构以 `/docs` 为准）：

| 接口 | 用途 |
|---|---|
| `POST /api/teacher/paths` | 发布知识点先修图 |
| `GET /api/learning/next?path_id=...&learner=...` | 查询下一步 |
| `POST /api/teacher/followups` | 批量安排跟进，携带 request_id |
| `GET /api/learning/followups?class_id=...&learner=...` | 查询持久跟进 |
| `PATCH /api/teacher/followups/{id}` | 延期或撤销 |
| `POST /api/teacher/tasks/{sid}/attempts/{event_id}/reviews` | 教师确认原判定 |

`POST /api/sessions` 新增 `followup_id`。新接口沿用班级和学生身份隔离。默认页面仍为本地演示；配置认证后仍仅开放API，没有新增登录网页。

验证：`python -m pytest tests -q --junitxml=reports/teaching-v05-tests.xml`；使用 `.envs/harness/bin/python scripts/teaching_eval.py --compare` 运行6题评分对照，使用 `scripts/teaching_v05_e2e.py` 验证真实模型的分项评分、复习完成、路径推进与改判恢复。脚本使用临时数据库。结果见 `reports/teaching-v05-*.json`，不是学生学习效果实验。

边界：课程图和复习日期由教师设置，没有自动知识图谱、FSRS或后台提醒；单位验证只检查指定单位，不做任意单位换算；代数工具仅覆盖受支持语法。分项模型判断尚未校准。材料库/PDF、LMS和高并发存储改造仍未实现。设计参考与自主发现的修复分别记录在 [REFERENCES.txt](REFERENCES.txt)。

## v0.4：教师任务、教学记忆与可信评分

本版本将教师备课、学生练习、跨会话证据与待复核工作接在一起。旧会话继续可读；新表与索引自动创建，不删除原学习记录。

### 页面使用

1. 在“教师备课与跟进”填写班级、课程、知识点编号、题目、评分要求、参考答案与教学材料，发布任务。
2. 学生在“学生练习”填写教师任务编号开始学习。无需再填题目；同一学生、班级、课程、知识点的不同任务会接续记忆。
3. 教师按班级查看学情摘要、下一步建议及待复核作答。通过“教师证据”修订具体作答。
4. 修改题目或评分规则时发布新任务版本；同一课程目标保持相同知识点编号。版本不可原位覆盖，旧判定的依据仍可回查。

### 教学证据与记忆

`harness/memory.py` 从追加事件重建学习状态，重启后保留。记忆包含作答证据、暂时误区、实际交付的教学动作、提示内容及其后的作答观察；不把相关性宣称为教学因果效果。独立检查次数与辅助完成分开，复习提示按连续独立通过次数取1/3/7/14/30天（v0.7起，原为固定24小时），是规则建议，不是已校准遗忘模型。自由任务只做精确目标关联，不自动将语义近似目标合并；跨题稳定关联请使用教师知识点编号。

教师修订使原会话及读取过其记忆的下游会话失效，包含传递依赖；原始事件不删除。新的教学上下文排除这些证据，教师可见失效记录并要求重新检查。独立检查清空历史提示、学习记忆和教师指导文字；检查题仍可能由同一模型生成和审核，不能作为外部标准答案。

### 评分、检索与运行

- 符号工具声明只验证代数等价，不能覆盖解释、单位或目标形式；最终判定按完整任务要求聚合。缺少解释与因式分解形式的回归案例已加入测试。
- 教师提供的rubric和参考答案直接进入评分；模型生成任务仍明确标记来源。模型评分未进行教师盲评校准。
- 材料分块保留偏移和文档摘要，关键词/中文双字词检索允许无命中。任务规划可以查看材料开头作为概览，记录为`overview`；不把概览算成相关检索命中。仍仅支持txt/md与单张图片，没有PDF解析或向量数据库。
- 确认后的当前作答图片或教学题原图可再次提供给评分模型；轨迹只保存图片事件编号，独立检查不会带入旧教学题图片。一次只支持一张图，不构成完整的多模态教学系统。
- 反馈记录计划动作与实际交付动作；连续两次回退后请求教师复核。审核提示增加定义类题目的语义泄漏检查，但同模型审核不能保证阻止所有泄漏。
- 模型执行在内存数据库快照上进行，提交时短暂持有写锁并检查数据库是否变化。并发写入采用保守的数据库级冲突检测，返回409且不提交本轮；请刷新后重试。快照复制与全库冲突检测仍适合小规模内测，尚非班级高并发吞吐方案。
- 每轮12次调用/240秒派发预算约束底层HTTP等待时间；推理后端仍单并发。

### API与访问隔离

新增接口：

| 接口 | 内容 |
|---|---|
| `POST /api/teacher/assignments` | 发布教师任务；字段见页面或OpenAPI中的Assignment |
| `POST /api/sessions` | 传`learner`、`assignment_id`、`request_id`开始教师任务 |
| `GET /api/teacher/dashboard?class_id=...` | 班级学情与待复核队列 |
| `GET /api/teacher/memory/{learner}?class_id=...` | 指定学生的来源可追溯记忆 |

默认仍为**受信任的本地演示模式**，教师和学生标签不构成身份认证。共享使用时可设置`HARNESS_ACCOUNTS_FILE`为服务器上的账号JSON文件，格式如下（示例令牌必须替换为独立的高熵令牌，文件不要提交到仓库）：

```json
{
  "replace-with-teacher-token": {"role":"teacher","class_id":"七年一班"},
  "replace-with-student-token": {"role":"student","class_id":"七年一班","learner":"学生A"}
}
```

请求携带`Authorization: Bearer <token>`。认证模式要求学生通过所属班级的教师任务创建会话，并隔离学生和班级。教师班级汇总请求必须提供匹配的`class_id`。为避免Gradio回调绕过权限，**认证模式仅开放API，演示UI与旧课程API禁用**；尚未实现带登录的班级网页、账号管理或LMS集成。没有配置文件时不要将演示端口暴露给不受信任用户。

### 验证与边界

```bash
.envs/harness/bin/python -m pytest tests -q --junitxml=reports/teaching-v04-tests.xml
.envs/harness/bin/python scripts/teaching_eval.py
.envs/harness/bin/python scripts/teaching_memory_e2e.py
```

真实模型回归使用`evaluations/teaching_cases.json`中6个项目编写的固定评分案例，临时数据库不写入应用学生记录。结果保存在`reports/teaching-v04-real.json`，初次失败保留在`reports/teaching-v04-real-initial.json`。这是评分与执行回归，不是独立教师标注基准、模型对照实验或学习收益证据。完整参考来源及采用机制见`REFERENCES.txt`。

## 启动与访问

工作目录：`/home/miniconda/DCAI/Edu`。推理环境 `.envs/inference` 和应用环境 `.envs/harness` 相互独立。GPU 默认使用物理编号 1、2；其他进程的显存超过 1GB 时启动脚本会拒绝占用，不会清理其他任务。

```bash
cd /home/miniconda/DCAI/Edu
.envs/harness/bin/python scripts/service.py start inference
.envs/harness/bin/python scripts/service.py start harness
.envs/harness/bin/python scripts/service.py status inference
.envs/harness/bin/python scripts/service.py status harness
```

在自己的电脑上建立 SSH 转发，将 `<服务器地址>` 替换为实际 SSH 主机：

```bash
ssh -N -L 7860:127.0.0.1:7860 -L 8000:127.0.0.1:8000 miniconda@<服务器地址>
```

浏览器访问 **http://127.0.0.1:7860**。推理 API 为 `http://127.0.0.1:8000/v1`，模型名 `omniedu-9b`。服务只监听 loopback，无公网链接，无真实多用户认证；教师与学生标签是演示视图。

```bash
.envs/harness/bin/python scripts/service.py stop harness
.envs/harness/bin/python scripts/service.py stop inference
```

脚本管理本项目的 PID 并验证进程身份。日志位于 `logs/inference.log` 和 `logs/harness.log`，数据位于 `data/learning.sqlite`。进程独立于 SSH 会话运行，但不随服务器开机自动启动。端口冲突时保留原进程并报告错误。

## 模型与环境复现

固定模型：[`lhpku20010120/Omni-Edu-9B`](https://huggingface.co/lhpku20010120/Omni-Edu-9B)，revision `7fda599d16f5c7aff770e2869450684c8e4d72f0`，未量化 BF16，权重 18,819,722,392 bytes。模型与许可文件存于 `models/Omni-Edu-9B`。

```bash
bash scripts/bootstrap.sh
# 标准下载器：支持 HF_ENDPOINT，失败时回退官方源
HF_ENDPOINT=https://hf-mirror.com .envs/harness/bin/python scripts/download_model.py
# 慢速单连接可改用分段下载器（先用标准下载器取得配置文件）
.envs/harness/bin/python scripts/download_parallel.py
.envs/harness/bin/python scripts/download_model.py --verify-only
```

镜像当前可能跳转官方站。分段下载器保留 `.parts` 实现断点续传，并核对响应范围与大小；合并后验证官方 LFS SHA256。已验证完成后可手动删除 `.parts` 释放约 18.8GB 空间。不要同时运行两个权重下载器。

实际后备后端为 Transformers 5.2.0 + PyTorch 2.8.0/CUDA 12.8，模型通过双卡层分配加载；这是**层分配**，并非 vLLM 张量并行。使用标准 PyTorch 的线性注意力回退，未安装 FlashAttention/FLA，不改系统驱动。当前 vLLM 发布包的 CUDA 13 依赖未通过此主机的驱动预检；未对旧版 vLLM 进行运行验证，不能将此解释为所有 vLLM 版本都不兼容。

`requirements-inference.lock`、`requirements-harness.lock` 记录实际安装版本。首次部署复用了服务器已有 PyTorch 包的**独立副本**，不依赖原环境运行；bootstrap 提供重新下载安装路径。

推理服务限制：单并发、4096 token 总上下文（输入＋输出）、单张图、图片缩放到最长边768、最大输出1024 token、关闭 thinking；仅支持非流式 chat completions。响应含 usage、生成首 token 时间及峰值 PyTorch allocated 显存。短上下文和保守配置优先服务原型，不代表吞吐上限。


## 当前版本 v0.3：自定义任务

VS Code 转发 **7860** 后刷新页面。默认输入题目或目标，选择“辅导这道题”或“根据目标或材料学习”；材料支持粘贴文字和 UTF-8 `.txt` / `.md` 文件，题目图片识别后需确认。没有学科白名单，不需要知识点编号。默认不会自动进入代数题。

教学链路：解析任务与目标 → 检索当前材料 → 适用工具验证或模型按规则评估 → 选择教学动作 → 生成并审核反馈 → 保存证据 → 等待学生。模型无法直接写入学习结论。对无法确定的作答返回不确定；引用的学生原文和材料原文必须能核对。

- `tasks.py` 定义题目、目标与评分协议；`open_engine.py` 负责状态、证据、预算与执行。
- `open_model.py` 不含代数学科角色；反馈允许必要的英语、数字与公式，通过答案泄漏/事实审核，而非禁止所有字母。
- `toolbox.py` 中验证器可注册或移除。SymPy 只是其中一个插件，无法处理时明确放弃判定，不限制任务创建。
- `gateway.py` 才导入旧课程引擎。旧会话可继续，新会话不要求固定题目或技能编号。
- 新检查题围绕当前目标动态生成，经题目审核和参考答案自洽检查后呈现；没有“只剩两道检查题”的限制。重复或不合格题最多重生成一次，本轮模型调用总预算为12次，仍不合格则不推进状态。完整讲解需用户明确请求，并使后续练习标记为辅助完成。

创建自定义任务：

```bash
curl --noproxy '*' http://127.0.0.1:7860/api/sessions \
  -H 'Content-Type: application/json' \
  -d '{"learner":"学生A","task":"解释哈希表中的哈希冲突。","mode":"problem","request_id":"create-demo-1"}'
```

可选字段：`material` 为材料全文；`image_url` 为图片 data URL。只传 `learner` 会要求提供任务，不再暗中选取代数题。显式传 `skill` 才使用旧示例课程。

会话操作仍用 `POST /api/sessions/{sid}/{action}`，请求体为 `request_id`、可选 `version` 和 `text`。支持：

| action | 用途 |
| --- | --- |
| attempt | 提交当前任务作答 |
| reply | 回答教师追问，不授予独立完成证据 |
| hint / explain | 请求提示 / 明确请求完整讲解 |
| check | 完成练习后创建新独立检查 |
| resume_practice | 从检查返回原任务继续学习 |
| image / confirm | 上传作答图片 / 确认转写 |
| confirm_task | 确认或修正创建时上传的题目图片 |

教师接口：`GET /api/teacher/sessions/{sid}/trace` 与 `/replay`；自定义任务修订使用 `POST /api/teacher/tasks/{sid}/attempts/{event_id}/corrections`。学生进度接口不返回隐藏参考答案。原始图片可由 `GET /api/teacher/tasks/{sid}/images/{event_id}` 查看。

**证据含义**：`independent_verified` 表示适用确定性工具验证；`independent_model_assessed` 表示无提示作答经模型评估，不是数学证明或已校准掌握概率。模型自己生成并审核的新题可能仍有错误，教师可以修订。材料检索是有来源编号的文本片段检索，未声称支持完整 PDF/扫描教材解析或互联网事实核验。

验证：

```bash
.envs/harness/bin/python -m pytest tests -q --junitxml=reports/open-task-tests.xml
.envs/harness/bin/python scripts/open_task_e2e.py
.envs/harness/bin/python scripts/open_task_recovery.py
.envs/harness/bin/python scripts/open_task_report.py
```

当前验收汇总见 `reports/open-task-acceptance.json`，真实跨学科逐轮结果见 `reports/open-task-e2e.json`。测试使用脚本作答；生成检查题的参考作答仅用于验证执行链路，不代表真实学生学习效果。下面 v0.1/v0.2 的固定课程说明仅适用于“示例课程”和历史验收。

## 旧示例课程：学习流程与证据

- 10个知识点、30道教学题、20道检查题；`course.json` 是生成的课程清单，权威模板在 `harness/course.py`。题目与规则均为项目编写。
- 学生每行写一个代数步骤。系统定位第一处不等价变形；只有完成展开、合并或求解才算完成。复杂文字或未支持语法由模型按rubric判断，并记录来源；不确定结果不计为掌握。
- 提示分三级：概念引导、步骤提示、完整讲解。连续两次有效失败增加一级。第一级模型文本若含公式等不允许内容，回退到课程提示。
- 辅助完成与独立检查分开记录。独立检查不提供提示；失败后回到教学题，再选新的检查题。同一演示学生的检查题跨会话不重复分配，题库耗尽时不再声称获得新的独立检查证据。
- 图片先转写，学生确认或修正后再评估。保存原图、转写、确认文本及关联事件；教师可用 `image_event_id` 查看原图。
- SQLite 保存会话和追加事件；每个提交有幂等键，冲突返回409。模型异常回滚本轮状态。教师修正保留原判定与修订历史，进度按最新修正重算，相关会话要求重新检查。
- 进度显示实际证据，不显示未经校准的掌握概率。通过检查24小时后标记待复习；首版没有后台通知或FSRS。两道检查题耗尽后需扩充题库才能取得新的独立题证据。

## 接口

应用 API 文档：`http://127.0.0.1:7860/docs`。

| 接口 | 用途 |
|---|---|
| `GET /api/skills` | 知识点与课程依据 |
| `POST /api/sessions` | `{learner, skill}` 创建会话 |
| `GET /api/sessions/{id}` | 恢复状态与学生可见历史 |
| `POST /api/sessions/{id}/attempt` | `{request_id, version?, text}` 提交步骤 |
| `POST /api/sessions/{id}/hint` | `{request_id, version?}` 请求下一层帮助 |
| `POST /api/sessions/{id}/image` | `{request_id, image_url}` 上传base64图片 |
| `POST /api/sessions/{id}/confirm` | `{request_id, text}` 确认或修改转写 |
| `GET /api/progress/{learner}` | 历史进度、待复习知识点、会话编号 |
| `GET /api/teacher/report?learner=...` | 聚合学情、原始作答、判定和修订 |
| `GET /api/teacher/images/{event_id}` | 原始图片及转写 |
| `POST /api/teacher/attempts/{event_id}/corrections` | `{request_id, status, reason}` 教师修正 |

一个request_id重试必须携带相同内容和版本；不同内容返回409。503表示模型不可用/格式失败，可按相同request_id重试。新建会话不支持幂等键。

## 验证

```bash
.envs/harness/bin/python -m pytest -q tests --junitxml=reports/unit-tests.xml
.envs/harness/bin/python scripts/smoke_inference.py
.envs/harness/bin/python scripts/real_e2e.py
.envs/harness/bin/python scripts/evaluate.py
```

真实验收会在数据库中创建以“验收学生”命名的演示记录；单元测试使用临时数据库和假模型。`reports/deployment-smoke.json` 保存20次真实请求，`reports/real-e2e.json` 保存教学闭环，`reports/comparison.json` 保存独立案例对照。最终验收状态以 `reports/acceptance.json` 为准，不能仅凭文件存在判断通过。

对照评测只有6道项目编写的独立案例，检查状态、首错位置和明确答案字符串泄漏，不能捕捉所有语义泄漏，也没有教师盲评或真实学生学习效果验证。日志记录token与耗时；本机GPU电价、折旧未知，因此不估造货币成本。环境与服务重启验证另行记录在reports目录。

## 教学 Harness v0.2：带证据的执行闭环

本次重构的设计来源与固定版本链接见 [REFERENCES.txt](REFERENCES.txt)。参考 OATutor 的历史/帮助计分、DeepTutor 的纯策略和教学证据、OpenTutor 的上下文组装与验证；这是独立实现，不是直接移植整个项目。

运行链路为：学生输入 → 符号验证及模型诊断 → 纯策略选择教学动作 → 带历史证据的 OmniEdu 表达 → 输出校验/课程回退 → 原子保存状态与教学轨迹。模型没有直接修改学习状态或执行任意工具的权限。

- `harness/policy.py`：版本化纯策略，区分原因探测、自行修正、基础规则复习、澄清、概念提示、步骤提示、完整讲解和独立检查。
- `harness/runtime.py`：相关历史上下文、教学执行、保守输出校验，以及不调用模型的策略回放。
- `POST /api/sessions/{sid}/reply`：回答教师追问；请求体与 attempt 相同。解释回答只产生 `dialogue_only` 证据，不授予掌握信用。
- `GET /api/teacher/sessions/{sid}/trace`：每轮策略输入、原因、上下文来源、生成候选、校验/回退、证据编号和状态版本。
- `GET /api/teacher/sessions/{sid}/replay`：以记录的输入重算策略，检查与原决策一致；不是重新生成模型回复，也不是完整数据库事件溯源重建。

页面使用：提交错误步骤后，在同一输入框写出对教师追问的回答，点击“回答老师追问”；如果已经修正数学步骤，直接点“提交作答”。教师页可按会话编号查看轨迹和回放。仍通过 VS Code 转发端口 7860 访问。

例如同样写出 `2(x+3)=2x+3`，系统先探测依据。回答“我知道每项都要乘，只是漏写了”应进入自行修正；回答“只乘第一项”应进入基础规则复习。任何提示后的正确答案只算辅助完成，只有未见检查题的正确作答提供独立证据。自述“懂了”即使模型高置信认可，也不能代替本课程要求的代数验证。

验证命令：

```bash
.envs/harness/bin/python -m pytest tests -q --junitxml=reports/runtime-tests.xml
.envs/harness/bin/python scripts/runtime_e2e.py
.envs/harness/bin/python scripts/runtime_recovery.py
```

本轮结果以 `reports/runtime-tests.xml` 和 `reports/runtime-e2e.json` 为准；原 `reports/acceptance.json` 是 v0.1 的历史验收，不代表新教学策略已证明有效。

当前边界：固定代数课程和检查题仍然保留；基础复习围绕当前技能规则，不是完整先修课程规划。策略依赖模型对学生原文的解释，原文引用正确不保证原因判断正确。生成式概念反馈使用保守词法约束，违规回退到课程内容；这不是语义答案泄漏的完整证明。二、三级帮助使用预设且明确分级的数学内容。真实多轮测试使用脚本学生，不代表真实学习效果；尚无教师盲评或长期实验。每个操作最多两项模型任务；结构化诊断最多修复一次，教学文字只生成一次并在不合规时回退。SQLite 写事务仍串行，适合个人内测。


## bysz1 部署

工作目录：`/default-filesys/workspace/ysb/Edu`。Conda：`/default-filesys/workspace/ysb/miniconda3/bin/conda`。应用使用 `.envs/harness`（Python 3.11），推理使用 `.envs/vllm`（Python 3.12）。默认后端在 `service-config.json` 中设为 vLLM。

vLLM 0.23.0 + PyTorch 2.11.0/CUDA 13.0 + Transformers 5.22.2，使用物理 GPU 1、BF16、TP=1、32768 token 上下文、最多32个并发序列、60%显存预算（KV cache 约115万 token）；参数见 `service-config.json` 的 `vllm` 段。启动后先预热再标记就绪。启用异步调度、分块预填充、FlashAttention 3、Triton/FLA 线性注意力及 CUDA Graph。思考模式默认关闭。

服务脚本为 vLLM 进程配置 `/usr/local/cuda-13.0/compat` 兼容库，不修改系统驱动；临时目录与编译缓存位于项目 `.cache/`。模型通过镜像取得并通过完整 SHA256 校验，原始权重及分词器未改动。

```bash
cd /default-filesys/workspace/ysb/Edu
# 查看状态
.envs/harness/bin/python scripts/service.py status inference
.envs/harness/bin/python scripts/service.py status harness
# 启动（默认 vLLM）
.envs/harness/bin/python scripts/service.py start inference
.envs/harness/bin/python scripts/service.py start harness
# 停止
.envs/harness/bin/python scripts/service.py stop harness
.envs/harness/bin/python scripts/service.py stop inference
```

重建环境：`bash scripts/bootstrap_vllm.sh`，使用 ysb Conda 与清华 PyPI 镜像。`requirements-vllm.lock` 锁定全部依赖版本（与 `reports/vllm-pip-freeze.txt` 一致）。模型下载仍可用 `HF_ENDPOINT=https://hf-mirror.com .envs/harness/bin/python scripts/download_model.py`。

在个人电脑执行：

```bash
ssh -N -L 7860:127.0.0.1:7860 -L 8000:127.0.0.1:8000 bysz1
```

浏览器打开 http://127.0.0.1:7860 ，推理接口为 `http://127.0.0.1:8000/v1`，模型名 `omniedu-9b`。当前为本地演示模式，服务不随服务器重启自动启动。

验证：正式端口上20次文本、多轮、图片请求全部通过；8路并发、流式输出、Edu结构化诊断与应用页面检查均通过。报告见 `reports/vllm-deployment.json`、`reports/vllm-validation.json`、`reports/vllm-production-smoke.json`。v0.7 在 vLLM 上重跑了教学端到端（`reports/teaching-v07-vllm-real.json`，4项通过，总耗时由75.6秒降到15.7秒）和评分案例（`reports/teaching-eval-v07-vllm.json`、对照 `reports/teaching-eval-v07-vllm-compare.json`：6个案例裸模型判对3个，harness判对6个）。

保留 `.envs/inference` 的 Transformers 后备环境。如需临时回退，先停止推理，再运行 `.envs/harness/bin/python scripts/service.py start inference --backend transformers`；后备仍使用 GPU 1、2，需空闲。此覆盖仅针对该次启动，默认配置仍为 vLLM。
