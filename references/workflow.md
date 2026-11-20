# 科学执行工作流：来源约束下的有限参数优化

本文件规定一次实际案例的执行顺序。证据字段、锚点和单位规则见 `/skills/weekly-skill/references/evidence.md`；失败分流、状态转移和恢复见 `/skills/weekly-skill/references/recovery.md`。命令的完整参数与输出语义分别以 `optimization-api.md`、`io-api.md` 为准，此处不复制整篇 API。

## 1. 不可越过的事实边界

1. **先读实际输入，再谈方案。** 阅读 `/input/case.json`、`/input/task.md`、其中实际引用的原始 PDF/附件及运行环境声明。原始字节、任务目标、合同约束和论文原文是事实依据；文件名、领域标签、常识或相似论文不能代替原文。 authoring 时没有实际案例内容，不得预埋正式答案。
2. **只执行可见的评分和硬阈值。** 若实际输入确实向 Agent 展示了 16 条 public rubrics/hard thresholds，建立逐项矩阵：`criterion_id + 原文 + locator + affected_plan_versions + evidence_refs + result`。未展示的编号、阈值、权重、合格线和内部细则不得猜测；评分维度概要不等于 16 条细则。
3. **三版是并列候选，不是三轮实验。** 保留实际给出的三个 `(plan_id, version)`，在同一请求中独立评估。不得去重版本、覆盖旧版或把枚举轮次写成真实实验轮次。修改方案须新建版本并记录真实 change record；只有出现真实观测及真实运行记录时才建立 experimental round。
4. **模型守语义，脚本守算术。** 模型阅读来源、绑定实体/条件/单位、规范化请求并解释结果；精确枚举、算术、单位检查和工具支持的验证交给脚本。不能用“心算正确”替代可复跑记录。
5. **没有观测，就没有经验代理模型。** 无真实 observations 时，只评估输入明确给出的数学模型、规则和成本；不拟合 GP，不默认 BO，不生成经验响应曲线。未来的实测 response 保持未知；若输出合同允许，字段写 `null` 并同时标明 `unmeasured/pending`，否则保留 honest partial，不能为了满足类型而填数。canonical request 的 unknown 输入则**不得携带 value/null 占位**。
6. **计划场景不是外部事实。** organizer 给定的 prospective price/resource/booking contract 只用于该场景下计划计算。没有独立来源，分别不得称为供应商正式报价、已采购、已预订、现场可用或已付款。未来响应、收益和增益同理。
7. **不推荐不安全的绕过。** 不行默加权、不静默放宽硬约束、不擅改安全/伦理边界。目标方向、优先级或 Pareto 口径必须来自任务/合同/用户显式说明。

## 2. 时间与模型动作预算

平台总预算是 64 model requests、900 秒；本流程采用更严的 **至多 40 次模型动作、810 秒内容冻结线**，其余留给最终 I/O 与平台波动。一次模型请求记一次模型动作，工具批次不拆算成“模型思考轮”。使用 runner 的真实计数和实际经过时间；没有记录时不得补造计时。

| 截止（秒） | 动作额度 | 必须形成的可保存成果 | 未达门槛时 |
|---:|---:|---|---|
| 60 | 1–6 | bootstrap；读取 case/task 和 output contract；输入清单；具体未知项；首次 honest partial checkpoint | 停止扩展阅读，先保存最小 partial，列出缺失材料和阻断字段 |
| 300 | 7–18 | 实际来源阅读与原生 PDF 抽取；evidence registry；三版参数/约束/方向映射；canonical request；可行时已有 optimization report | 不声称优化完成；保存请求草稿或具体阻塞原因和 last-good partial |
| 570 | 19–28 | 三版并列比较；完整实验方案；资源、计划成本、critical path；search/stop、风险、确认项 | 删除无来源的精细内容，保留已核验部分并降级为 partial，而不是补齐想象值 |
| 720 | 29–34 | 精确算术复核、来源/claim recheck、安全和合同审计；准备最高可被证据支持的状态 | 不进入 complete；修复可修复错误，否则保留/恢复 last-good partial |
| 810 | 35–38（39–40 仅留作此冻结线前的 finalizer 应急） | 对实际输出执行最后 checkpoint/finalize；冻结科学内容 | 只处理落盘和 last-good 恢复，不再增加新方案或新主张 |
| 810–900 | 0；不再发起模型动作 | 平台 I/O、失败记录和必要的只读核验 | 遇 finalizer 失败按 recovery 记录，不能改写为成功 |

每个具有实质改进的候选都 checkpoint，而非等最后一次才保存。临近截止时优先顺序为：原始合同覆盖与诚实状态 > 安全和硬约束 > 可回溯证据/算术 > 完整方案 > 排版。

## 3. 启动、native PDF 阅读与早期 partial

从安装根路径使用实际脚本：

```bash
IO=/skills/weekly-skill/scripts/io_guard.py
OPT=/skills/weekly-skill/scripts/optimize.py
CASE=/input/case.json
TASK=/input/task.md
WORK=/app/work
OUT=/app/output

python3 "$IO" bootstrap --case "$CASE" --work "$WORK" --output "$OUT" --task "$TASK"
cp "$OUT/result.json" "$WORK/candidate.json"
```

随后立即做以下事项：

- 保留原类型和值的 `case_id`；snapshot 原始 `output_contract`，为**全部声明字段**建立映射，不只处理 required。辅助账本只能放在合同允许的位置或 sidecar，不能破坏 `additionalProperties=false`。
- 核对 `run_record.input_manifest` 与实际文件。存在、size 和 SHA256 只证明字节可读取；必须实际检查语义后，才把对应 `content_reviewed` 改为 true。确实无需语义检查时，记录 `review_required=false` 的具体理由。
- 将 bootstrap 的骨架改成有具体 `sources/uncertainty/limitations/change_records` 的早期 partial 并 checkpoint。骨架本身不是已完成的科学研究。

### PDF 处理顺序

1. 从 case/task 的实际引用解析 PDF 路径并登记原文件；不假定存在预抽取答案层。记录稳定 document ID、字节 SHA256、物理页码映射和抽取方法。
2. 从 `/input/case.json`、task 及随案实际提供的 `environment/resources.yaml` 查明并选择已登记的 pdf-text 工具，按其真实 CLI/help 运行；不得臆造路径或选项。该清单不是 Skill 自带文件。优先抽取**原文本层**，保留页分隔和版面信息。对将使用的每个数字、表头和脚注，回看对应原始 PDF 页，确认 `document#page=N,table=M,row=...,column=...` 锚点。
3. 文本层为空、乱码、表格错位或图表含关键数据时，使用已登记的 render 工具检查实际页面。OCR 只作可读性辅助；用于主张的数值、上下标、单位、行列归属须与原页视觉复核，并披露 OCR/版面不确定性。
4. PDF 无权限、缺页、损坏、加密或工具不可用时，记录真实错误并走 recovery。不得绕过权限、把未读内容写成已读，或为了便利上传到未批准的外部服务。
5. 论文 printed page 与 PDF physical page 分开记录。论文题目只用于识别 Source，不能作为 Claim 证据；每条科学断言仍需原文短引文和可重定位 anchor。

## 4. Evidence registry → canonical request

先按 `evidence.md` 建立 Source/Evidence/Claim/Parameter/Response/Cost/Booking/Validation 记录，再生成 `/app/work/optimization-request.json`。优化器的 evidence map 是 registry 的投影（每项 `locator/quote/type`），不是纸质出处的替代品。

请求固定使用 `schema_version="1.0.0"`，并完整填写 `case_id/evidence/parameters/plans/input_values/derived_values/objectives/constraints/decision`，不省略 API 声明的必需顶层字段。优化 request 的 case ID 必须是非空字符串；最终 result 始终保留原输入 case ID 的 JSON 类型和值。若原 ID 不满足优化器合同，作为请求输入问题走 recovery，不擅自改写身份。

规范化顺序如下：

1. **绑定对象。** 每个值绑定 entity、参数/响应定义、材料/设备/方法、plan/version、时间范围和其他来源限定条件。不同实体或实验条件下的数字不能仅因符号相同而共用。
2. **登记有限域。** 数值域只使用来源允许的 `allowed_values` 或精确 grid；分类域逐项列出。检查端点开闭、step、固定值属于域、互斥关系和环境/安全约束。域缺失是 unknown，不得用“典型范围”补齐。
3. **统一单位再调用优化器。** 优化器不做单位换算。按 evidence 的精确换算表记录原值、倍率、新值及温度 absolute/delta 语义；JSON 数值保留 number token，不能用数字字符串代替 literal。
4. **只录入有来源的方程。** derived/objective/constraint 的每个 ref、单位、取整规则和 evidence_refs 均须可回溯。特别是 run 数、batch、包装/计费向上取整、通量、产率、资源用量和成本的方程，只能使用实际 case/合同/论文明确给出的关系；不得因公式“常见”就添加。无来源公式不进入 canonical request。
5. **区分 known 与 unknown。** measured/historical 观测输入须有真实记录；缺 response、未执行实验或未定价格用 `status="unknown"`，不带 value。除零、上游未知和 unknown 硬约束不可转成 0/false/pass。显式给定模型的输出标 `prediction` 或 `computed`，不能标 `measured`。
6. **显式指定 decision。** 优先用任务声明的 feasibility、single、lexicographic 或 pareto。没有权重就不能构造总分，没有统一方向就不能擅自 choose winner；保持并列和 Pareto 集。

若 case 已有结构明确的 `candidates[].cost_items`，先读 `resource-audit.md` 并优先调用 `audit_resources.py --case "$CASE" --output "$OUT/resource-audit.json`；有真实 hard caps 时按其格式写 `$WORK/resource-caps.json` 并加 `--caps ... --emit-request "$WORK/optimization-request.json"`。它生成精确费用行、源指针和分类资源账，并可能直接形成同一 canonical request；对 quantity 与 charge_events 的语义重复、缺失价格或符号公式会保留 unresolved，不得按预期总额强行编译。完整科学约束仍需按实际 case 另行编码，成本 pass 不是科学批准。

调用形式：

```bash
python3 "$OPT" --input "$WORK/optimization-request.json" --output "$WORK/optimization-report.json"
# 只有资源预算确有需要时，先将 CAP 设为实际非负整数，再运行：
python3 "$OPT" --input "$WORK/optimization-request.json" --output "$WORK/optimization-report.json" --max-candidates "$CAP"
```

cap 是三版合计上限，应记录其来源和覆盖影响。退出 0 只表示请求语义有效，`optimal/complete/infeasible/unresolved/partial` 的含义必须另查 report；`invalid_request`、坏 grid、类型/引用错误不是科学上的 no-feasible。

## 5. 三版比较与搜索停止

三版共享相同的来源约束和 decision 规则，但各自保留 assignments 与 domain overrides。比较表至少包含：

- `(plan_id, version)` 和完整参数赋值、固定/枚举身份；
- 每条硬约束的 pass/fail/unknown，以及 binding constraint/failure reason；
- 各 derived/objective 的精确值、单位、evidence_refs 和 issues；
- ranked/unranked/Pareto/best 身份，截断覆盖率及未评估候选影响；
- 响应口径、资源/critical path、计划成本、来源可迁移性和待确认项；
- 每版的取舍理由及其对应证据，而非一段跨三版通用的宣传性结论。

保留所有已评估候选，包括 fail、unknown、dominated 和非赢家。仅在完整枚举、无未知潜在赢家且 decision 明确时，按报告声明 exact `optimal`；最优只针对**声明域和显式模型**。Pareto `complete` 不等于唯一赢家，feasibility `complete` 不等于最优，`partial` 结果只能描述为当前已评估范围的临时排序。

后续实际实验的 search/stop 条款须写清初始点、每批候选、真实观测登记、资源上限、失败处置和停止条件。没有真实 observations 时不得虚构“第 N 轮后的提升”。只有获得与当前目标/条件匹配的真实响应观测后，才可提出下一批序贯设计或 BO；此时还要说明为什么不采用更简单的网格/筛选、训练/验证数据、模型不确定度和安全约束。GP/BO 是后续可选工具，不是本工作流默认步骤。

**日程修订不能偷渡原版选择。** 每批时长、日历、时窗与设备/人员/费用绑定后，任一项超出blocking cap均不能以重新排程的名义保留原版合格。原子时长已经超过窗口上限时，不能用简单总时长/上限换算一个“最小可行天数”。应核出原候选具体失败，并将所有变更建成新身份/版本、canonical对象/哈希和完整before→after，同时重算未知或受影响资源；conditional只允许修订提案而非已批准事实。执行 `audit_decision.py` 按 `decision-audit.md` 将选择、全部硬门和 self-QA 统一检查。`matches=false` 或重复检查ID是内部矛盾，不能进入“验证通过”文案。

**核验产物与声明一起交付。** 每个声称唯一/已复核的检查都有实际稳定ID、状态和证据位置。result内若合同无 self_QA 键，validations/confirmation/risk records和run_record要给出本 run 真实产物路径；不得口头声称无重复ID、全局通过或empty matches。`source-validation.status=partial`时不得压平为“全部源已证”，逐字exact count、未覆盖src、算术和实验结论分开报告。

## 6. 每个候选版本都要有的完整实验方案

共同内容可以共享引用，但必须指出版本差异。每版至少覆盖：

1. **目标与假设。** 可检验/可否证的命题、因果关系或仅关联的边界、适用对象和来源；将 source fact、assumption、prediction 分开。
2. **变量。** independent variables 及水平/域，dependent responses，固定 controls、nuisance factors、批次/设备协变量。每个推荐值与允许浮动都绑定单位和来源。
3. **实验对照。** 基线、空白、阴性/阳性对照按研究问题选择；不适用须解释，不得空写“无”。若需要并具备依据，说明随机化、区组、盲法、重复和 run order。
4. **执行步骤。** 准备、校准、材料/设备 ID、量程检查、加样/处理、采样、测量、数据记录和结束操作的有序步骤。没有依据的参数明确标为 proposed/pending，而不是现场事实。
5. **Responses 与判读。** 每项定义分子/分母、单位、仪器/方法、时间点、聚合规则、重复处理、缺失/异常值规则和可接受波动。阈值来自实际可见合同/任务或明确标为 proposed；未来 response 不填虚构观测。
6. **资源与计划成本。** 样品/对照/重复的数量，试剂和耗材、机时、人员、测量和计算资源；按实际场景单价和公式做精确计算。未定价项单列，不能悄悄当作零成本或“总价已确认”。
7. **时间与 critical path。** 将准备、等待、处理、测量、分析和必要审批画成依赖关系；总时长由关键路径而非所有步骤简单相加决定。只有资源确实独立时才并行。机时可用性和预订仍保持 planned/proposed，除非存在真实确认记录。
8. **搜索与停止。** 对应第 5 节，写清资源 cap、覆盖范围、收敛依据及停止后仍可能改变结论的未知项。
9. **风险和异常。** 设备量程、材料相容性、交叉污染、测量失败、数据缺失和无解分别按 recovery 处理，不自动放松安全约束。
10. **安全、伦理和专家确认。** 引用实际 SDS/设备手册/安全规则及机构要求（输入提供什么就读什么），列出设备可用性、校准、危险操作、废弃物、人员/动物/生物或数据伦理等适用确认项。未完成确认是实际实验前置 gate；建议书本身不能被称为已批准、安全保证或可直接开机指令。

## 7. 验算、合同 audit 与冻结

### 7.1 算术和科学检查

- 对 request 做 CLI 完整校验，保存实际 request/report 路径和文件 hash。检查 DAG、跨引用、端点/step、固定值、单位、三版 identity、decision 方向以及 candidate cap/coverage。
- 用精确有理数复算最终推荐、并列候选及关键边界的成本、通量、时间和约束；记录换算与复算命令、输入和结果。相同 request 重跑得到相同字节只证明可复现，不是独立科学验证。若未完成独立重算，明确 `arithmetic=not independently rechecked`，不能因 io_guard 通过就声称已验算。
- 逐 claim 回到原页/原单元格/JSON Pointer 重读，核对主体、数值、单位、条件、极性和时间。检查来源外推、预测与实测混淆、报价与场景混淆、未知候选可能胜出的情况。
- 所有安全/伦理/设备确认项保留实际状态。`approval_required`、`schema valid`、`checkpoint accepted` 是不同事实。

**逐字引文验证：** 有 PDF quote/verbatim 时，不在 registry 中混填概括。用真实连续短片段，独立表格值／单位／脚注拆 spans，上下文放 context_note。运行：

```bash
python3 /skills/weekly-skill/scripts/verify_sources.py \
  --case "$CASE" --registry "$WORK/evidence-registry.json" \
  --output "$OUT/source-validation.json"
```

解析 registry 格式与 exact matching 边界见 `source-verification.md`。PyMuPDF 已在环境才可用；缺失、扫描文字层层缺失、任意 flexmatching、表格错位或不支持的语义范围返回 partial，不能换模糊匹配冒充通过。具体页面找不到字符串时用连续短 quote 改正登记，而不是删关键证据；不同事实/条件必须分开。逐字通过只证明文本存在，主体/单位/事物/表格归属、视觉图页和科学支持仍由实际领域复核判断。

### 7.2 结构与最终化

候选有实质更新后：

```bash
python3 "$IO" checkpoint --case "$CASE" --candidate "$WORK/candidate.json" \
  --work "$WORK" --output "$OUT" --optimization-report "$WORK/optimization-report.json"

python3 "$IO" finalize --case "$CASE" --work "$WORK" --output "$OUT" \
  --optimization-report "$WORK/optimization-report.json"
```

需要独立查看审计时才对相同状态的实际结果运行 `audit`；有意状态转换必须走 checkpoint。result 的 raw domain status 保留原合同允许值；默认交付状态为 `partial/complete/failed`，案例明确允许的 `conditional/conditional_choice/undetermined` 与有充分不可行证据的状态不得强行转成 failed。不要自造状态名。审计以另一层 canonical `processing_status` 记录 task 交付，具体语义与证据门槛见 io-api。complete 还要求真实 Schema 校验或合格的 manual contract review、来源/claims recheck、无阻断项和实际内容阅读；未安装 `jsonschema` 时，不得把 `executed=false` 写成通过。

冻结前确认：

- 最终 `result.json` 覆盖全部原合同字段、case ID 正确，sources、uncertainty、limitations 和 change records 为真实记录，无 N/A/TBD 等哨兵或空占位。
- 内嵌账本与 `run_record_ref` 的 sidecar 不互相陈旧；实际报告中的 `best/selected` 可解析且 confirmed feasible，未知潜在赢家未被隐藏。
- io_guard 的 `algorithm_executed=false`、`scientific_optimality_verified=false`、arithmetic 默认 `not_run` 和 scientific review 边界仍如实保留。工具审计通过不是科学批准。
- finalize 针对**实际 `/app/output/result.json`**。合法 partial/failed 可 finalized，但不是 pass；非法 complete 被 rejected 时按 recovery 处理。810 秒后不再改科学内容，也不新增未经证实的收益/基准数据。

## 8. 少量方法参考（不替代案例证据）

- NIST/SEMATECH, [How do you select an experimental design?](https://www.itl.nist.gov/div898/handbook/pri/section3/pri33.htm)：设计取决于目标、因素数、资源和错误风险，并建议为异常重做保留资源。
- BIPM, [SI Brochure](https://www.bipm.org/en/publications/si-brochure)（第 9 版，页面列有更新版）：SI 词头、时间和摄氏温度的权威定义。
- Brochu, Cora & de Freitas, [A Tutorial on Bayesian Optimization of Expensive Cost Functions](https://arxiv.org/abs/1012.2599)：BO 结合目标函数先验与已有证据选择下一次 observation；这也说明没有 observations 时不能伪称已做数据驱动的 BO 更新。
