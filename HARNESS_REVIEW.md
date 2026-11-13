# OmniEdu 教学 Harness 评估报告

评估日期：2026-11-13
评估对象：本目录 `harness/`（v0.6.0，已据此更新到 v0.7.0）及配套脚本、报告
参考项目：[OmniEdu](https://haolpku.github.io/Omni-Edu/)（arXiv:2609.23088）

## 结论

本 harness 在"证据可信度"这一层做得比大多数开源教育项目都严谨，但目前更像一个"带审计的单人辅导原型"，离"在真实课堂里辅助教师教学"还差三块：

1. 没有用好 OmniEdu 模型本身的长处（课程对齐、诊断、辅导）。推理已切到 vLLM 并支持批处理并发，但上下文仍限 4096，harness 侧也还没用上结构化输出和并发（见第五节）。
2. 存储架构和串行的模型调用链撑不住一个班同时在线；推理端已不再是瓶颈。
3. 教学策略、学习者建模和评测都偏薄；教师侧最常用的能力（批量批改、学情讲评、组卷备课）基本没有。

评估时测试全部通过（109 个），Transformers 后端时期的真实模型日志共 390 次调用，单次调用中位数 3.6 秒、P90 7.6 秒；单轮教学操作实测 2–23 秒。切换到 vLLM 后的实测见第五节。

## v0.7 处理情况（2026-11-13）

下文第四、五节保留原评估内容；以下是 v0.7 已处理的部分，细节见 README 的 v0.7 一节。

已解决：
- **git 管理**（P0-3）：已 `git init`，基线提交为切换 vLLM 后的 v0.6。
- **vLLM 下大图片失败**（第五节问题 1）：上传图片统一校正方向并缩到最长边 1536 像素；超长请求单独提示。
- **上下文 4096**（第五节问题 2）：放开到 32768，最多 32 路并发，显存 60%，KV cache 约 115 万 token。
- **结构化输出**（P0-1、第五节问题 3）：改用 `json_schema` 约束解码，保留原说明文字与重试。
- **串行调用**（P0-2 的一部分、第五节问题 4）：整题评估与分项评估并行。
- **未在 vLLM 上回归**（第五节问题 5）：118 个单元测试通过；在 vLLM 上重跑教学端到端 4 项全部通过（总耗时 75.6 秒降到 15.7 秒），6 个评分案例全部通过。
- **运维**（第五节问题 6）：启动后先预热再就绪（8 路并发验证由 3.5 秒降到 0.46 秒）；锁文件改为完整版本。
- **分级提示**（P1-4 的一部分）：概念 → 步骤 → 同构半完成例子，策略升级为 v3，旧轨迹可回放。
- **固定 24 小时复习**（P1-5 的一部分）：改为按连续独立通过次数 1/3/7/14/30 天的规则间隔。
- **固定比例抽检**（P1-6 的一部分）：改为按风险分层，并列出风险原因。

初步对照数据：`scripts/teaching_eval.py --compare` 在 6 个编写案例上，裸模型判对 3 个（其中把缺少解释的作答判为正确），harness 判对 6 个。样本太小，只能说明方向，不能替代第四节建议的正式对照评测。

仍未处理：PostgreSQL 与任务队列、OmniEdu 系统指令对齐、课标知识点库、诊断前测与错因库、BKT/IRT 估计、异构模型交叉复核、PDF 解析与混合检索、公开基准与模拟学生评测、教师端工作流与登录、`OpenEngine.operation` 拆分与轨迹存差异、服务开机自启。

## 一、OmniEdu 提供了什么

- 4B / 9B / 27B 三个规模，BF16 与 FP8 两种权重，训练序列长度 32,768。
- 围绕"课程对齐 → 解题 → 诊断 → 辅导"闭环训练，69,999 条指令数据，20 种任务专用系统指令。
- 论文中最突出的两项：课程对齐（K12-Bench、MathFish、EDUMATH）和教学辅导（MathTutor-S/SH、TutorBench、LongTutor）。
- 官方推荐部署方式：较新的 vLLM，关闭 thinking，`--max-model-len 32768`。

harness 的价值应在于把这两项能力变成教师可用的工作流。

## 二、教育领域开源项目调研

| 项目 | 定位 | 值得借鉴的点 |
|---|---|---|
| [HKUDS/DeepTutor](https://github.com/HKUDS/DeepTutor) | 学习工作台（Apache-2.0，约 5.8 万星） | 多种检索后端（LlamaIndex / LightRAG / PageIndex / GraphRAG）、MinerU 解析 PDF、三层可编辑记忆（轨迹 / 事实 / 综合）、区分讲解 / 辅助练习 / 独立掌握、知识库检索评测（`kb eval`） |
| [amosblomqvist/learnharness](https://github.com/amosblomqvist/learnharness) | 学习 agent harness（AGPL-3.0） | "测量 → 规划 → 教学 → 练习"四段分离；教之前先测出理解边界；学生"出声思考"字段；跨会话命题级知识图谱 |
| [ArnaudGuiovanna/tutor-mcp](https://github.com/ArnaudGuiovanna/tutor-mcp) | MCP 形式的智能辅导引擎（MIT） | "生成式教学 + 确定性教学法"：BKT、FSRS、先修规则和评测证据决定下一步，模型只负责表达；推荐理由可解释；机构内共享课程并审核 |
| [Tencent/WeSmartFlow](https://github.com/Tencent/WeSmartFlow) | 中文自适应学习框架（MIT） | ReAct 工具化辅导、交互式可视化（EduViz）、即时测验、多 agent 课程生成、运行预算 |
| [THU-MAIC/OpenMAIC](https://github.com/THU-MAIC/OpenMAIC) | 多 agent 互动课堂（MIT） | 由材料一键生成课件、测验、模拟与 PBL；导出 PPTX / 离线 HTML；服务端生成可断点续跑 |
| [GoogleCloudPlatform/ursa-weaver](https://github.com/GoogleCloudPlatform/ursa-weaver) | 图结构辅导框架 | 技能 DAG 解锁路径；选择题、命令行检查、MCP 工具结果、LLM rubric 等可扩展验证 |
| [CAHLR/OATutor](https://github.com/CAHLR/OATutor) | 经典智能辅导系统 | 分步作答、分级提示、帮助惩罚（本项目已借鉴） |
| [TutorGym](https://github.com/Teachable-AI-Lab/tutor_gym) | 评测框架 | 在 223 个真实 ITS 领域中逐步评测 AI 当导师的能力；发现大模型识别错误步骤的准确率不高于随机 |
| [MRBench / BEA 2025](https://github.com/kaushal0494/UnifyingAITutorEvaluation) | 导师回复评测 | 8 个教学维度（发现错误、定位错误、是否泄露答案、给出引导、可操作性、连贯、语气、拟人），有人工标注 |
| [eth-lre/mathtutorbench](https://github.com/eth-lre/mathtutorbench) | 评测 | 分别评估对学生理解的判断与教学行为 |
| [umass-ml4ed/sim-student-eval](https://github.com/umass-ml4ed/sim-student-eval) | 模拟学生评测 | 衡量模拟学生的真实度；提示法模拟学生表现差，SFT / DPO 明显更好 |
| [eth-lre/PedagogicalRL](https://github.com/eth-lre/pedagogicalrl) | 训练 | 用模拟学生做多轮 GRPO，训练"会教而不是直接给答案"的导师 |
| [pyKT](https://github.com/pykt-team/pykt-toolkit)、[EduCDM](https://github.com/bigdata-ustc/EduCDM)、[EduStudio](https://github.com/HFUT-LEC/EduStudio)、pyBKT、py-fsrs | 学习者建模库 | 知识追踪、认知诊断（IRT / DINA / NCDM 等）、间隔复习调度 |

## 三、现有 harness 做得好的地方（建议保留）

- **模型不能直接写学习结论。** 区分 `assisted_complete`、`independent_model_assessed`、`independent_verified`；学生说"懂了"不算正确；独立检查必须用未见过的新题，并隔离历史提示和图片。
- **纯函数策略，可回放。** 策略有版本号，每轮记录输入、决策、上下文来源、生成候选与校验结果，可以不调用模型重放决策。
- **教师改判可传播。** 改判会让依赖这条证据的下游会话和跟进任务连带失效，原始事件不删除。
- **工程稳健性。** 写入接口有幂等键和版本冲突保护；模型异常时回滚；有答案泄漏、引用不实、冒充权威检查；评分项工具只判定自己负责的维度，不能替代整题判定。
- **诚实的边界说明。** README 和报告明确写出"不是学习效果证据""不是校准的掌握概率"等限制。

## 四、不足与改进建议

### P0：不解决就进不了课堂

#### 1. 没有用上 OmniEdu 的长处

现状：
- 所有提示词都是自写的通用中文指令加 JSON 格式约束（`harness/open_model.py`、`harness/model.py`），没有对齐模型训练时的 20 种系统指令。9B 模型在陌生格式下结构化输出更容易失败，日志中有不少格式重试。
- 课程对齐是 OmniEdu 最强的一项，但 `concept_id` 和先修关系全靠教师手填，没有课标或知识点体系，也没有让模型自动标注题目的知识点和学段。
- 推理已切换为 vLLM 0.23.0（见第五节），解决了单并发和纯 PyTorch 线性注意力的问题；但仍沿用 4096 token 上下文（模型训练用 32k），harness 仍靠提示词约束 JSON 加一次重试，没有使用 vLLM 的结构化输出。

建议：
- 放开到 32k 上下文；在 `ModelClient.structured` 中改用 `response_format: json_schema`（本机实测可用），从根上减少 JSON 格式失败。
- 从 OmniEdu 数据集中抽取 20 种任务的系统指令，按任务映射到 `plan` / `assess` / `respond` / `review` 等调用。
- 建立"课标 → 知识点 → 学段 → 先修"知识库，由模型自动为题目打知识点，教师只需确认。

#### 2. 并发和存储撑不住一个班

现状：
- `harness/storage.py` 的 `Snapshot` 每个请求都把整个 SQLite 数据库备份到内存，再按整库 `data_version` 判断冲突；只要有其他写入发生，本轮就返回 409。
- 一轮作答要串行调用模型 3–7 次（整体评估、最多 4 个分项评估、反馈生成、反馈审核），实测单轮 2–23 秒。推理端换成 vLLM 后已能批处理，但 harness 内部仍逐个串行调用，最多 4 个分项评估本可并行。40 个学生同时提交时几乎每个请求都会冲突。
- `dashboard`、`memory.summarize`、`correct` 都是全表扫描加 JSON 解析；改判传播是对所有会话和所有轨迹的不动点循环，数据量上去后会明显变慢。

建议：迁移到 PostgreSQL，按会话行做乐观锁；模型调用放入任务队列，异步返回；分项评估并行发出，利用 vLLM 批处理；为常用查询建结构化列和索引，不要每次解析 state JSON。

#### 3. 没有版本管理

目录不是 git 仓库，README 写的是"源码修改前快照位于 `/tmp/edu-before-v06.tar.gz`（临时目录）"。建议立即 `git init`，并把 `.envs/`、`models/`、`data/`、`logs/`、`.cache/` 加入 `.gitignore`。

### P1：教学质量

#### 4. 教学策略偏薄

现状：开放任务里"请求提示"永远映射到同一个动作，没有分级提示，也没有逐步撤除支架：

```python
# harness/tasks.py
if o['kind'] == 'hint':
    return {'action': 'scaffold', 'reason': '根据当前困难和已用帮助给出下一步提示。'}
```

整个策略只有约 8 个动作，由几条 if 规则决定。旧代数课程还有三级提示，开放任务反而退化了。记忆中每次干预的效果一律记为 `effect: not_established`，从未被策略使用。

建议：
- 参照 LearnHarness，在教学前加入诊断前测，找出先修缺口。
- 恢复分级提示（概念 → 步骤 → 半完成例题），并按表现逐步撤除支架。
- 加入"先预测再解释"、正反例辨析、按学科整理的常见错因库。
- 利用干预后的作答观察切换教学方法，而不是只看连续失败次数。

#### 5. 学习者模型只是简单状态机

现状：复习间隔固定 24 小时（`harness/memory.py`）；跨会话关联靠 `goal_key` 对知识点编号或"学科 + 目标文本"精确匹配，措辞不同就无法归到同一知识点；没有知识追踪或间隔复习调度。

建议：坚持"不把估计当事实"的立场，同时参考 tutor-mcp，引入 BKT / IRT（pyBKT、EduCDM）的掌握度估计和 FSRS 复习时间，作为"可解释的估计"与证据并列展示并明确标注；用知识点体系（见 P0-1）代替文本精确匹配。

#### 6. 出题、审核、评分由同一个模型完成

现状：检查题由同一个 9B 模型生成、审核，再用它给参考答案打分；反馈的答案泄漏审核也是同一个模型。TutorGym 的结果表明大模型识别错误步骤并不可靠。教师抽检只是 `row['id'] % 10 == 0` 的固定取样。

建议：判分与泄漏审核等关键环节使用异构模型或 27B 交叉复核；用教师已标注样本统计误判率并做校准；教师抽检按风险分层（低置信、工具与模型冲突、新题型、新学生）而非固定比例。

#### 7. 材料检索很简单

现状：材料按 600 字切块，用中文双字词重叠计数排序（`harness/toolbox.py` 的 `retrieve`）；只支持 txt / md，无 PDF 或教材解析，无向量检索。

建议：借鉴 DeepTutor，用 MinerU 解析 PDF 和扫描教材，采用 BM25 + 向量的混合检索，保留页码和原图引用；为检索建立召回率评测集。

#### 8. 评测太弱

现状：只有 6 个自编案例（`evaluations/teaching_cases.json`），验证的是执行链路，不是教学效果；无教师盲评、无模拟学生、无公开基准。

建议：
- 接入 MRBench / BEA 2025 的维度（发现错误、定位错误、给出引导、可操作性、是否泄露答案）和 mathtutorbench。
- 复用 OmniEdu 论文自己的 MathTutor-S/SH、LongTutor 评测协议。
- 参考 sim-student-eval 构造多种类型的模拟学生，做多轮对话评测。
- 最关键的是对照实验："OmniEdu 裸用"对比"OmniEdu + harness"，在同一批任务上量化 harness 带来的提升；后续每项改动都用这套评测回归。

### P2：从"辅导学生"走向"辅助教师"

#### 9. 教师侧能力缺口

现状缺少课堂中教师最常用的能力：
- 批量批改拍照作业（目前一次一张图，且只能由学生上传）；
- 全班错因聚类与讲评课素材（已有 `support_groups` 可作为起点）；
- 按课标、知识点和难度组卷；
- 教案、课件生成（可参考 OpenMAIC）；
- 家长或学生个人学情报告。

交互上，教师需要手动输入任务、跟进、路径等编号；界面为 Gradio 演示；配置认证后仅开放 API，没有带登录的网页、账号管理或 LMS 集成。

建议：围绕"布置 → 批改 → 讲评 → 补救"的教师工作流设计页面；以班级、作业为入口，编号由系统内部管理；补齐登录与角色权限。

#### 10. 代码结构与可维护性

- `OpenEngine.operation`（`harness/open_engine.py`）一个函数将近 180 行，所有操作在一个大 if/elif 里，大量压缩成单行的写法，难以维护和审查。
- v0.1 到 v0.6 层层叠加，旧引擎（`engine.py`、`gateway.py`、`course.py`、`policy.py`、`runtime.py`）仍在主路径旁边；README 变成了累积的更新日志。
- 每条轨迹存完整的 `state_before` 与 `state_after`：现有 76 条轨迹约 404KB，而作答记录只有约 55KB；图片以 base64 直接存入事件表。

建议：将各操作拆成独立处理函数；旧示例课程迁出主包；轨迹改存状态差异；图片改用文件或对象存储；README 改为"当前用法 + 架构说明"，更新日志另存 `CHANGELOG.md`。

## 五、vLLM 部署现状（2026-11-13 复核）

推理已切换到 vLLM，默认后端由 `service-config.json` 指定，Transformers 服务保留为后备。部署本身是成功的，但参数沿用了旧后端的保守限制，harness 也还没用上 vLLM 带来的能力。

### 部署方式

- 主机是 bysz1：8 张 **H20**（96GB，Hopper 数据中心卡），驱动 550.127.05（原生 CUDA 12.4）。原报告中"8 张 4090、驱动 535、cuda-compat 不可用"的排查针对的是之前的主机，不适用于 bysz1。
- vLLM 0.23.0 + torch 2.11.0+cu130 + Transformers 5.22.2，独立环境 `.envs/vllm`（Python 3.12），由 `scripts/bootstrap_vllm.sh` 重建。
- 通过 `/usr/local/cuda-13.0/compat`（580 版兼容库）在 550 驱动上运行 CUDA 13 程序，不改系统驱动；H20 是数据中心卡，前向兼容包可用。
- 启动参数（`scripts/service.py`）：单卡（GPU 1）、TP=1、BF16、`--max-model-len 4096`、`--max-num-seqs 16`、`--gpu-memory-utilization 0.40`、单图、关闭 thinking、线性注意力预填充用 Triton。
- 已有验证：20 次文本 / 多轮 / 图片请求、8 路并发、流式输出、Edu 结构化诊断全部通过（`reports/vllm-deployment.json`）。

### 复核实测

| 项目 | 结果 |
|---|---|
| 单请求生成 200 token | 1.28 秒，约 156 token/s |
| 8 路并发 | 1.43 秒，总吞吐约 1,100 token/s |
| 32 路并发（超出 16 路上限部分排队） | 3.0 秒，总吞吐约 2,100 token/s |
| KV cache 容量 | 42 万 token，按 4096 上下文可支持约 100 路 |
| `response_format: json_schema` | 可用，输出严格符合 schema |
| 1280×960 作业图片 | 通过，约 1,200 个输入 token |
| 3024×4032 手机照片 | **失败**：11,860 个输入 token，超过 4096 被拒绝（HTTP 400） |
| 约 3,100 token 输入 + `max_tokens=1024` | **失败**：总长超过 4096 被拒绝 |

`reports/vllm-validation.json` 里 8 路并发每路 3.5 秒，是首批请求触发 Triton 内核即时编译造成的（日志中有 JIT 告警），不代表稳定状态。

### 问题

1. **手机拍的作业照片会直接失败。** 旧 Transformers 服务会把图片缩到最长边 768 像素，vLLM 路径和 harness 都没有缩放。原图走 vLLM 后视觉 token 远超 4096，请求被拒；harness 把 HTTP 错误统一报成"模型服务不可用或超时"，教师和学生看不出真实原因。这是切换后引入的回归。
2. **4096 上下文已不是硬件限制。** 单卡只用了 40% 显存，KV cache 就有 42 万 token；放到 32768 仍可支持十几路满长请求。结构化调用固定 `max_tokens=512/1024`，输入一长（材料、历史、图片）就会超限。
3. **harness 没用上结构化输出。** `ModelClient.structured` 仍是提示词 + 失败重试一次。注意：实测中仅给 schema、不给判定说明时，模型把"2(x+3) 写成 2x+3"判为 correct。启用 `json_schema` 只能保证格式，不能省掉现有的任务说明，切换后要用评测集确认判定准确率没有下降。
4. **harness 仍逐个串行调用模型。** 推理端能批处理，但分项评估、审核等调用仍串行，单轮延迟没有因并发能力而下降。
5. **harness 层没有在 vLLM 上回归。** 109 个测试和 `reports/bysz1-deployment.json` 都是切换前在 Transformers 后端上跑的；vLLM 只做了接口级冒烟和验证，`teaching_v06_e2e` 等真实教学端到端脚本没有重跑。两个后端在 chat template、图片预处理、采样默认值上不同，判定结果可能变化。
6. **运维细节。** 服务不随服务器重启自动启动；启动预热约 90 秒，没有覆盖到的 Triton 内核会在首批真实请求时编译，造成延迟尖峰；`requirements-vllm.lock` 只固定了 vLLM 和 Transformers 两个包（完整版本在 `reports/vllm-pip-freeze.txt`）。

### 建议

- 立即：在 harness 上传入口把图片缩到最长边 1280–1536 像素（同时控制 token 数），或启动时用 `--mm-processor-kwargs` 限制 `max_pixels`；把 400 超长错误单独提示为"图片或输入过长"。
- 放开 `--max-model-len 32768`、`--max-num-batched-tokens` 相应调大；视班级规模把显存比例提到 0.6–0.8，或在空闲卡上起第二个实例做负载均衡。
- `structured` 改用 `json_schema`，保留现有说明文字，用 `evaluations/` 案例和历史日志对比判定是否一致。
- 分项评估并行发出。
- 在 vLLM 上重跑 109 个测试和 `teaching_v06_e2e`，与 Transformers 时期的报告对比判定结果和格式重试率。
- 启动后用一组覆盖文本、图片、长输入的请求做预热再对外标记就绪；用 systemd 或 supervisor 托管；锁文件改为完整 `pip freeze`。

## 六、建议路线

1. **一到两周**
   - 纳入 git 管理。
   - 修复 vLLM 下大图片超长失败；放开 32k 上下文；harness 改用结构化输出并并行化分项评估；在 vLLM 上重跑全部测试和端到端评测。
   - 对齐 OmniEdu 系统指令。
   - 搭建"裸模型 vs harness"对照评测（MRBench 维度、MathTutor 协议、模拟学生）。
2. **一个月**
   - 迁移 PostgreSQL，加入模型任务队列。
   - 建课标知识点库与自动打标。
   - 分级提示、诊断前测、错因库。
   - 引入 FSRS 与 BKT / IRT 估计。
   - PDF 教材解析与混合检索。
3. **之后**
   - 拍照作业批量批改、全班学情讲评、组卷备课、教案课件生成。
   - 带登录的教师端网页。
   - 真实教师盲评，然后做小规模课堂试点。

优先做对照评测：没有它，后续每项改动都无法证明确实提升了教学效果。
