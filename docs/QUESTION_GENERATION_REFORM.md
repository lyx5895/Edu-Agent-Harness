# 出题改造方案：独立 A/B 与教师偏好闭环

状态：待评审  
日期：2026-11-23  
范围：02 出题改题（`edu_core/workflow/questions.py`、`edu_core/workflow/prompts.py`、`edu_app`、相关存储与提示词）  
关联文档：[AB_AND_TRAINING_DATA.md](AB_AND_TRAINING_DATA.md)、[DESIGN_OVERVIEW.md](DESIGN_OVERVIEW.md)、[FEATURES_AND_TESTS.md](FEATURES_AND_TESTS.md)

---

## 0 结论

1. **A/B 是教师可见的在线偏好实验**：产品界面就是标注界面，教师的选择就是标签。不把实验藏到后台，也不另起一套实验系统。
2. **两版独立生成**：同一份任务输入，A 用基础提示词，B 用另一个提示词变体，两版互不可见；并行调用，不增加串行等待，代价是 token 翻倍。
3. **比较要干净**：随机展示位置并记录；两版都自检；只有两边都通过才展示比较卡；教师选择、理由、耗时和后续结果分别记录。
4. **先修正确性，再谈实验**：B/导入题的校验缺口、答案键合法性、判等价用错模型、采纳答案后评分要点不同步，这几项不修，偏好数据本身不可信。
5. **延迟按"先给可用结果"处理**：生成本身同步，替代版本与两版的并行调用同批返回；自检、重写、单题修改后的复检放后台；慢目标用截止时间和部分成功兜底。

现有 [AB_AND_TRAINING_DATA.md](AB_AND_TRAINING_DATA.md) 已经规定了共同输入、随机位置、单一因素、标签分开、训练授权等原则。本文不重复这些原则，只说明**当前代码与规范的差距**以及**具体怎么改**。

---

## 1 现状盘点

### 1.1 生成与 A/B

| 项 | 现状 | 位置 |
|---|---|---|
| 生成粒度 | 每条教学目标一次调用，返回该目标的 1–5 道题（默认 2） | [questions.py:165](../edu_core/workflow/questions.py#L165) |
| 调用方式 | 按目标拆 part，并行 | [questions.py:165-190](../edu_core/workflow/questions.py#L165-L190) |
| B 的生成 | 对着 A 重写：payload 里带“原题”，system 里加“不要照搬” | [questions.py:461](../edu_core/workflow/questions.py#L461)、[prompts.py:41](../edu_core/workflow/prompts.py#L41) |
| B 的自检 | 明确不做 | [questions.py:378-380](../edu_core/workflow/questions.py#L378-L380) |
| 抽样 | 逐题独立抽样，全局 `ab_rate` 默认 0.5 | [config.py:32](../edu_core/config.py#L32) |
| 同步性 | B 在响应返回前写完 | [questions.py:246-254](../edu_core/workflow/questions.py#L246-L254) |
| 因子 | `factor='prompt_variant_rewrite'`，同时含提示词变体、锚定重写、看到 A 三个变量 | [questions.py:442](../edu_core/workflow/questions.py#L442) |
| 展示位置 | 固定“当前版本”在左、“另一个版本”在右，没有随机 | [app.js:900](../edu_app/static/app.js#L900) |
| 记录 | `comparisons.display` 只存 A/B 两个 question id | [storage.py:142-145](../edu_core/storage.py#L142-L145) |
| 教师关闭 / 主动请求 | 没有；前端固定传 `ab: null` | [app.js:1684](../edu_app/static/app.js#L1684) |

### 1.2 校验与映射

| 项 | 现状 | 位置 |
|---|---|---|
| 判等价模型 | 盲解用 `reviewer`，判等价用 `grader`（与 generator 同一个模型） | [questions.py:745-761](../edu_core/workflow/questions.py#L745-L761)、[config.py](../edu_core/config.py) |
| 答案键校验 | 只要求非空；不校验字母越界、重复选项、多选数量、选项数 >8 | [base.py:246](../edu_core/workflow/base.py#L246) |
| 采纳盲解答案 | 只改 `reference`/`answer_key`，不同步 rubric 的 `check.expected` | [questions.py:1139](../edu_core/workflow/questions.py#L1139) |
| 撤回 A/B | 只撤回标签，不回滚已采纳的 B 内容 | [questions.py:934](../edu_core/workflow/questions.py#L934) |
| 导入试卷 | 不限学科题型、不校验目标序号、不做自检 | [questions.py:1004](../edu_core/workflow/questions.py#L1004) |
| 手工 / 合并 | 默认无目标；合并硬编码 round 1 | [questions.py:957](../edu_core/workflow/questions.py#L957) |
| 换一道 | 只按第一个目标生成，却保留原 `objective_ids`；上下文缺材料/活动/重点难点 | [questions.py:1164-1244](../edu_core/workflow/questions.py#L1164-L1244) |

### 1.3 延迟

| 项 | 现状 | 位置 |
|---|---|---|
| 快速路径 | 没有备课方案时，`_confirm_standards` + `_infer_goals` 两次串行调用 | [questions.py:109](../edu_core/workflow/questions.py#L109)、[projects.py:106](../edu_core/workflow/projects.py#L106) |
| 慢目标 | `_parallel` 按提交顺序等所有结果，一个慢目标拖住全部 | [base.py:137](../edu_core/workflow/base.py#L137) |
| 单题修改 | “换一道/按意见修改”同步跑 1 次生成 + 2 次检查 | [questions.py:1251](../edu_core/workflow/questions.py#L1251) |
| 并发 | `_finish_checks` 外层 8 个线程，每个再开 2 个，实际并发可达 16 | [questions.py:284-308](../edu_core/workflow/questions.py#L284-L308) |
| 后台任务 | 进程内线程，重启即丢，`runs` 会留下 `running` | [questions.py:484](../edu_core/workflow/questions.py#L484) |

---

## 2 目标流程

```
教学目标（一条）
   │
   ├─ 抽样决策（目标级）：这条目标是否进入比较？
   │      否 ──> 只生成 A
   │      是 ──> 建 candidate_group
   │
   ├─ A 调用（variant a）──┐
   └─ B 调用（variant b）──┤ 并行，同一份 payload，只是 system prompt 不同
                          │
                    usable_drafts 过滤
                          │
              A 存 draft，B 存 candidate
                          │
              两版各自自检（审核 + 独立解题）
                          │
              两边都通过？──否──> 记 not_comparable，只展示 A
                          │是
              建 comparison：随机 shown_first，记录 paired_a_index
                          │
              教师选择（A / B / 都可用 / 都不合适 / 跳过）
                          │
              记录 verdict、preference、理由、耗时
                          │
              后续结果回填：确认 / 编辑 / 换一道 / 需核对 / 学生得分
```

关键点：

- **抽样在生成之前决定**，这样两版可以并行，B 不需要等 A 写完。
- **B 不看 A**，payload 与 A 完全相同，只是 system prompt 换成 variant b。
- **配对是随机的**：一条目标生成 1 个 B，和该目标里随机一道 A 配对，记录 `paired_a_index`。
- **只有两边都通过自检才展示比较**，避免拿有问题的题去和好题比。

---

## 3 改造清单

### 3.1 生成路径（G）

- [ ] **G1 独立两臂**：同一条目标发两次调用，使用同一份 `request(part)`；A 的 system prompt 用 variant a，B 用 variant b。B 的 payload 删除 `原题`、`替代要求`，不再调用 `_alternative_draft` 的锚定逻辑。
- [ ] **G2 并行与分组**：在发请求前创建 `candidate_group`；两次调用并行；两个 `record_generation` 都带同一个 `group`，`position` 分别为 A/B，`variant` 分别为 a/b。
- [ ] **G3 目标级抽样**：抽样单位从“每道题 0.5”改为“每条目标按概率/预算选取”。建议每条目标最多 1 个 B，避免一次出 8 道题就有 4 张比较卡压给老师。
- [ ] **G4 随机配对**：B 与这条目标里随机一道 A 配对，记录 `paired_a_index`。
- [ ] **G5 单臂失败**：一侧失败或没有可用草稿时，照常交付另一侧；记 `b_failed` / `a_failed`，不建 comparison。
- [ ] **G6 因子命名**：`factor` 改为 `prompt_variant_independent`，明确这是“两种提示词的独立抽样”，不是“对着原题重写”。

### 3.2 提示词与结构化输出（P）

- [ ] **P1** variant b 只保留“优先贴近真实情境、从不同角度考查同一目标”这一条差异，作为唯一实验因子；不要给 B 加“不要照搬原题”之类额外指令。
- [ ] **P2** A 和 B 使用同一个 `QuestionSet` schema；B 的 `每个目标出题数` 设为 1。
- [ ] **P3** 两版必须同模型、同 temperature、同 max_tokens；`seed` 如果网关支持，也保持一致或显式记录。
- [ ] **P4** 记录 `prompt_version`、`model`、`temperature`、`seed` 到 generation meta 和导出记录；`llm.py` 目前固定 `temperature=0`，如果要放开，A/B 必须同温（见 3.6）。
- [ ] **P5** 保留锚定重写能力，但作为**另一个实验**（`factor='anchored_rewrite'`），不要和独立抽样共用同一个 comparison。

### 3.3 自检与展示门槛（C）

- [ ] **C1** B 也跑 `_self_check`：审核（是否考到目标、是否超范围、难度）+ 独立解题 + 数值/数据自洽检查。至少要有独立解题。
- [ ] **C2** 展示门槛：两边都不带 `answer_suspect`、不带 must-fix `flags` 时才建 comparison；否则记 `not_comparable`，老师只看到 A 及其提示。
- [ ] **C3** 比较卡上同时显示两版的检查摘要（难度、问题数、是否验过答案），让老师知道自己在两个可信选项之间选。
- [ ] **C4** 如果老师先确认了 A、B 后到，记 `not_compared` / `b_not_ready`，不要记成 `skip`。
- [ ] **C5** 自检失败（模型不可用）不能当成“通过”；记 `check_state='error'`，比较卡暂不展示或明确标注。

### 3.4 展示与交互（U）

- [ ] **U1 随机位置**：`display` 增加 `shown_first`（`'A'` 或 `'B'`），首次展示时随机；刷新和恢复不交换。语义上的 A/B 不变，按钮仍按“当前版本/另一个版本”映射，不按左右位置映射。
- [ ] **U2 教师关闭**：项目级或教师级开关；关闭后只生成 A，不产生 comparison，也不计入实验样本。
- [ ] **U3 主动请求**：允许老师在单题上主动要“另一个版本”，记录 `trigger='requested'`，与系统抽样分开统计。
- [ ] **U4 展示负担**：每批比较数量上限可配置（例如最多 3 个），达到上限后只生成 A；抽样框要记录，便于分析。
- [ ] **U5 文案**：把“出题与自检中…”改成与实际一致的说法；如果后台只做自检和重写，就不要暗示替代版本也在后台。

### 3.5 数据记录与导出（D）

- [ ] **D1 比较快照**：除了两个 question id，还要存：共同输入 manifest、两臂的 prompt/model/temperature/seed、两版输出、两版检查结果、`paired_a_index`、`shown_first`、`sample_rate`、`trigger`、`rule_version`、`ui_version`。
- [ ] **D2 选择记录**：verdict、preference、reason_tags、`decision_ms`、teacher/project/class/grade。教师是 rater，不是统一的质量分；导出时按 rater 分组。
- [ ] **D3 后续结果**：选中的那份后来是否被确认、编辑、换一道、标需核对、实际使用、学生得分。没有后续结果，只有“点击”，没有“点击是否正确”。
- [ ] **D4 导出视图**：一行一个比较样本，直接可用于偏好数据、SFT 和统计；`both_ok`/`neither`/`skip` 作为弱标签保留，不强行分胜负。
- [ ] **D5 绑定版本与去重**：反馈绑定教师实际看到的候选版本；候选出新版本不能静默迁移旧评价；按 `input_sha + teacher` 去重，避免同一道题反复进入实验。

### 3.6 延迟治理（L）

- [ ] **L1 两臂并行**：独立生成后，A/B 同批并行，老师等待约等于一次调用，而不是两次串行。注意 `concurrency` 要能覆盖两臂。
- [ ] **L2 部分成功**：`_parallel` 改为 `as_completed`，每个 part 带截止时间（建议 90s）；超时的目标在返回里写“第 X 条目标还在生成”，其余照常返回。
- [ ] **L3 合并前置调用**：快速路径把 `_confirm_standards` 和 `_infer_goals` 合并成一次调用：把候选课标条目一起给模型，一次返回“选中的条目 + 教学目标”。
- [ ] **L4 单题自检后台化**：“换一道/按意见修改”先交付新题干，自检在后台补；结果回来后更新提示，不阻塞老师。
- [ ] **L5 检查状态**：题目内容加 `checks.state = pending / running / done / error`；页面区分“还没检查”和“检查过没问题”，后台检查期间不禁用确认按钮。
- [ ] **L6 备选方案**：如果希望 B 和 A 同时到达又不想发两次调用，可以让一次调用返回 `count` 道 A + 1 道 B（`QuestionSet` 增加 `alternatives` 字段）。代价是单次输出更长、锚定更强；本次不默认采用，留作对照。

### 3.7 正确性修复（R）

这些不直接属于 A/B，但不修会让偏好数据和教学结果都不可信。

- [ ] **R1 判等价换角色**：`self_check_compare` 从 `grader` 改为 `reviewer`，避免和 generator 同一个模型给自己判卷。
- [ ] **R2 采纳答案同步评分要点**：`adopt_solver_answer` 同时清空或要求重填 rubric 里的 `check.expected`，避免批改继续按旧值判。
- [ ] **R3 答案键/选项校验**：在 `usable_drafts` 增加：字母 ⊆ 选项、选项去重、多选 ≥2、选项数 ≤8、判断题答案合法、填空/选择答案键非空；不合格丢弃或重试一次。
- [ ] **R4 撤回回滚**：`revoke_choice` 时，如果 A 的 lineage 最新版本来自 B，追加一个恢复原 A 内容的新版本；否则至少阻止撤回并提示用“换一道”。
- [ ] **R5 自动重写竞态**：把“版本未变”检查和创建新版本放进同一事务；后台重写前后都核对 `status` 和 `content_sha`。
- [ ] **R6 导入题补检**：导入试卷生成的题至少跑独立解题；试卷没有答案、由模型起草的题必须检。按学科 `pack['types']` 过滤题型，校验 `objective_index`，越界进 `dropped`。
- [ ] **R7 换一道的上下文**：复用 `request(part)` 的重点/难点/活动/材料/课标提示；解析不到目标时直接报 Conflict，不要用空目标生成；保存时更新 `objective_ids`。

### 3.8 映射与工程边界（M）

- [ ] **M1** 手工创建、合并候选题要显式选择目标，或明确显示“未关联目标”。
- [ ] **M2** `merge_candidates` 使用当前轮次，不硬编码 round 1。
- [ ] **M3** 跨批次重复检测：同目标已有题干一起送进提示词，或落库后比对；`_mark_similar` 改用更稳的分母，选项分段计算。
- [ ] **M4** 后台任务可恢复：用任务表 + 启动恢复替代裸线程；清理残留的 `running` run。
- [ ] **M5** 并发封顶：`_finish_checks` 的嵌套线程池改用共享执行器或信号量，保证 `concurrency` 是真实上限。
- [ ] **M6** 小项：`import_paper` 截断 30000 字符要提示；`usable_drafts` 默认 2 分改为按题型给出合理默认或要求模型补。

---

## 4 数据模型变更

### 4.1 优先用 JSON，避免大迁移

`comparisons.display` 目前是 TEXT JSON，可以直接加键，不需要迁移：

```json
{
  "A": {"question": "...", "generation": "..."},
  "B": {"question": "...", "generation": "..."},
  "shown_first": "B",
  "paired_a_index": 1,
  "b_status": "shown",
  "snapshot": {
    "input_manifest": [...],
    "arms": {
      "A": {"prompt_version": "generate-v5", "model": "...", "temperature": 0, "seed": null},
      "B": {"prompt_version": "generate-v5b", "model": "...", "temperature": 0, "seed": null}
    }
  }
}
```

### 4.2 建议新增的列（迁移 8）

如果需要在 SQL 里筛选和统计，再把这些从 JSON 提成列：

| 表 | 列 | 用途 |
|---|---|---|
| `comparisons` | `shown_first TEXT` | 位置偏差分析 |
| `comparisons` | `paired_a_index INTEGER` | 配对还原 |
| `comparisons` | `b_status TEXT` | shown / dropped / failed / not_comparable |
| `choices` | `decision_ms INTEGER` | 决策耗时，作为标签置信度的辅助信号 |
| `choices` | `rater_role TEXT` | teacher / researcher / synthetic |
| `generations` | `temperature REAL` | A/B 同温核对 |
| `generations` | `seed INTEGER` | 可复现 |

### 4.3 导出视图

新增一个只读视图或导出函数，一行一个比较样本：

- 任务：project、course、teacher、subject、grade、objective、difficulty、purpose；
- 输入：manifest 及哈希；
- A/B：两版题目、两版检查结果、两臂配置；
- 展示：`shown_first`、`sample_rate`、`trigger`、`rule_version`、`ui_version`；
- 标签：verdict、preference、reason_tags、decision_ms、acceptable；
- 结果：确认、编辑、换一道、需核对、使用、学生得分。

---

## 5 API 与 UI 变更

### 5.1 API

- `POST /api/projects/{pid}/questions/generate`
  - 响应里的 `alternatives` 增加 `shown_first`、`paired_a_index`、`checks` 摘要；
  - 新增 `comparisons_skipped`（因为两边未通过或达到上限而未展示的原因），便于前端解释。
- `POST /api/projects/{pid}/choices`
  - 增加 `decision_ms`（前端计时）和 `rater_role`（默认 teacher）；
  - 现有 verdict/preference/reason_tags 语义不变。
- `POST /api/projects/{pid}/questions/{qid}/alternatives`
  - 新增：教师主动请求第二个版本，`trigger='requested'`。
- 项目设置增加 A/B 开关和每批上限；关闭后不产生 comparison。

### 5.2 UI

- `comparisonView` 按 `shown_first` 渲染左右顺序；按钮仍按语义 A/B 映射。
- 比较卡同时显示两版的检查摘要；任意一版有问题时明确标出。
- 增加“另一个版本”主动请求入口（可选）和项目级关闭开关。
- 文案与后台实际行为一致：自检在后台时，不要写“出题与自检中”。

---

## 6 测试计划

### 6.1 单元测试

- [ ] A 和 B 的 payload 除 system prompt 外完全一致；B 不含 `原题`、`替代要求`。
- [ ] 同一条目标只发一次 A 和一次 B；两条目标各发一次；总调用数为 `2 × 抽样目标数 + 未抽样目标数`。
- [ ] 抽样在目标级；`paired_a_index` 在合法范围内。
- [ ] B 和 A 都调用 `_self_check`；任一侧有 flags 时不建 comparison，记 `not_comparable`。
- [ ] `shown_first` 持久化，刷新后不变；verdict 仍按语义 A/B 记录。
- [ ] 教师确认早于 B 到达时记 `not_compared`，不是 `skip`。
- [ ] `factor='prompt_variant_independent'`；两臂 `temperature`/`model` 一致。
- [ ] 撤回选择时，A 的 lineage 内容回到选定前；或明确拒绝撤回。

### 6.2 回归测试（R 项）

- [ ] 判等价走 `reviewer`。
- [ ] `adopt_solver_answer` 后，rubric 的 `check.expected` 不再指向旧答案。
- [ ] 非法答案键（字母越界、重复选项、多选单字母、选项 >8）被丢弃或重试。
- [ ] 导入试卷按学科限题型；目标序号越界进 `dropped`。
- [ ] 自动重写在题目被确认后不再追加草稿版本。
- [ ] 换一道使用完整上下文，并更新 `objective_ids`。

### 6.3 UI 走查

- [ ] 随机左右顺序；刷新不交换；按钮语义正确。
- [ ] 两版检查摘要可见；有问题的一版有明确标记。
- [ ] 关闭 A/B 后页面不再出现比较卡。
- [ ] 主动请求第二个版本后，比较卡在 B 到达时出现。

---

## 7 指标与验收

### 7.1 数据质量

- 双方通过率、`same` 丢弃率、`not_comparable` 率、`skip` 率、决策耗时分布。
- 明确偏好率（`preference='better'`）；`both_ok`/`neither` 的比例。
- 偏好矛盾率：选中的版本后来被编辑、换一道、标需核对的比例。
- 按 rater、学科、题型、难度分层统计，不把不同教师平均成一个分数。

### 7.2 生成质量

- 参考答案与独立解题一致率；
- 审核 must-fix 问题率；
- 数据自洽问题率；
- 难度命中率（要求 vs 审核估计）；
- 课标越界率；
- 答案键合法率。

### 7.3 产品体验

- 首次可看到题目的时间（time to first question）；
- 整批完成时间；
- 比较卡数量与老师实际参与率；
- 老师关闭 A/B 的比例（负担信号）。

### 7.4 验收场景

沿用 [AB_AND_TRAINING_DATA.md](AB_AND_TRAINING_DATA.md) 第 11 节的 7 条，并补充：

1. A/B 独立生成，输入除提示词外完全相同；B 不含原题。
2. 两版都自检；只有双方通过才展示。
3. 位置随机且稳定；反馈绑定实际展示版本。
4. 教师关闭或达到上限时，正常工作不受影响，样本记录为未进入比较。
5. 采纳、明确偏好、都可用、都不合适、跳过、未比较分别记录。
6. 后续结果可回填，偏好标签能与其对照。

---

## 8 里程碑

| 阶段 | 内容 | 预估 | 验收 |
|---|---|---|---|
| M0 | 随机位置 + `shown_first`；B 补独立解题；记录耗时 | 1–2 天 | 位置偏差消除，标签可用 |
| M1 | 独立两臂生成、目标级抽样、随机配对、展示门槛 | 3–5 天 | 两次并行调用，因子干净，比较可归因 |
| M2 | R1–R7 正确性修复 | 3–5 天 | 回归测试通过 |
| M3 | 部分成功、快速路径合并、单题自检后台、`checks.state` | 3–5 天 | 首题时间下降，慢目标不拖全批 |
| M4 | 导出视图、后续结果回填、关闭/主动请求 | 3–5 天 | 偏好数据可直接导出 |

预估不含 UI 细节打磨和多 worker 后台任务改造。

---

## 9 未决问题

1. **`both_ok` 的产品语义**：独立生成后，“两份都可用”是否意味着这条目标保留两道题？建议先维持“保留 A、关闭 B”，把 `both_ok` 当弱标签记录；如果以后要保留两道，需要改题号、覆盖统计和确认流程。
2. **采纳 B 的历史字段**：B 不是 A 的改写，`alternative_to` 这个名字会误导。建议改成 `adopted_from_group` 或 `paired_with`，保留“成为 A lineage 下一版本”的动作。
3. **知识点粒度**：当前一题一教学目标，适合判断目标是否达成，不适合知识点级诊断。是否要加一层知识点标签，是数据模型决策，不在本次范围。
4. **锚定重写是否保留**：如果还想研究“看着原题改写是否有帮助”，保留为独立实验（`factor='anchored_rewrite'`），不要和独立抽样混在一起。
5. **多 worker 后台任务**：当前是进程内线程；如果部署多 worker，需要任务表和启动恢复，否则后台结果会丢。
6. **教师训练授权**：偏好数据进入训练前，仍需遵循 [AB_AND_TRAINING_DATA.md](AB_AND_TRAINING_DATA.md) 第 9 节的授权与去标识要求；本文不改变这条。

---

## 10 改动文件清单

| 文件 | 改动 |
|---|---|
| `edu_core/workflow/questions.py` | 独立两臂生成、目标级抽样、随机配对、展示门槛、B 自检、撤回回滚、采纳同步评分、导入/换一道修复 |
| `edu_core/workflow/prompts.py` | variant b 作为唯一因子；去掉 B 的锚定指令；补充答案键约束 |
| `edu_core/workflow/common.py` | `usable_drafts` 增加答案键/选项校验；默认分策略 |
| `edu_core/workflow/base.py` | `confirm`/撤回/重写事务守卫；`_parallel` 的截止时间与并发控制 |
| `edu_core/workflow/projects.py` | 合并 `_confirm_standards` 与 `_infer_goals` 的输入 |
| `edu_core/llm.py` | 可选 temperature/seed；记录到 meta |
| `edu_core/storage.py` | 迁移 8：comparisons/choices/generations 新列 |
| `edu_app/api.py` | 响应字段、主动请求、项目开关、决策耗时 |
| `edu_app/static/app.js` | 随机展示顺序、检查摘要、开关/主动请求、文案 |
| `tests/test_edu_workflow.py` | 独立两臂、抽样/配对、门槛、撤回、回归 |
| `tests/test_edu_research.py` | 导出视图、标签字段、样本统计 |

---

## 11 一句话总结

把 A/B 当成一个**教师参与的在线偏好实验**：两版独立生成、并行返回；位置随机、两边都验；老师的选择连同理由、耗时和后续结果一起回流。产品和实验是同一条链路，需要额外做的是**控制偏差、保证两版都可信、把标签记完整**，而不是把实验藏起来。
