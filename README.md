# Weekly Skill：来源约束的有限参数实验优化

本提交交付的是供 Agent 装载的 **Skill 及必要运行资源**，不是三个官方案例的预写答案、白板、评分结果或运行报告。`SKILL.md` 规定 Agent 行为；本文说明安装、接口、复核与维护；`examples/minimal/` 只是接口演示。

## 1. 用途、适用条件与新增能力

本 Skill 用于把真实科研材料、设备/资源合同和有限的候选计划整理成可复核的实验参数建议。它帮助 Agent：

- 从实际论文、手册、历史记录和组织方合同中提取变量、有限域、单位、硬约束、响应口径、成本与计划版本；
- 对声明范围内的候选作精确算术、硬约束过滤及调用方指定的单目标、字典序或 Pareto 比较；
- 保留来源定位、未决值、候选账本、冲突和风险，并对中间结果作完整性保护；
- 在缺材料、约束冲突、无可行解、非法输入或运行失败时给出可检查的降级结果，而不是补造数据。

适用于参数可被来源支持的**有限集合或精确网格**，且 `plan_id + version` 已能区分候选身份的任务。它不自动选择优化方向，不作等权加权，不把有限枚举扩展成连续域或全局科学最优证明。

## 2. 依赖、安装与真实 runtime 路径

核心的 `io_guard.py`、`optimize.py`、`audit_resources.py`、`audit_decision.py` 只依赖 **Python 3 标准库**，不要求联网、`pip install`、外部模型服务或作者个人目录。`io_guard.py` 会在环境**已经安装** `jsonschema` 时可选调用它；未安装时明确记录 `executed=false`、`valid=null`，不自动安装依赖，且不能据此声明 complete 已通过真实 JSON Schema 校验。新增的 `verify_sources.py` 仅在执行 PDF 逐字引文核验时使用，动态导入环境已有的 PyMuPDF（`fitz`）；缺少它或 PDF 不可读会留下 partial，不能假称引文已验证，也不自动安装或联网获取依赖。

- 制作时提交根目录：`/app/submission`。
- 评测器将同一提交安装到：`/skills/weekly-skill`。
- 实际输入：`/input/case.json`、`/input/task.md`，以及 case 指向并实际提供的论文、PDF、材料与制品。
- 中间工作区：`/app/work`。
- 正式结果与可编辑制品：`/app/output`；至少保留 `result.json`、`run_record.json`、`validation.json`。
- 正式运行时原始 `/input` 只读；不得用本仓库示例覆盖真实 case、正式 `result.json` 或报告。

不要根据文件名猜测论文内容。PDF 必须读取实际原文件；平台若有 PDF/OCR/渲染工具，按实际环境和 Agent 指令使用，它们不是本提交的 Python 包依赖。

## 3. 调用顺序

### 3.1 Agent 先装载并读懂任务

1. Agent 装载 `/skills/weekly-skill/SKILL.md`，遵循其中的输入检查、证据、规划和停止规则。
2. 阅读真实 `/input/case.json` 的 `case_id`、全部原始 `output_contract` 声明字段和输入路径，并阅读实际 `/input/task.md`。再逐个阅读真实材料，不能用标签、最小示例或预置答案代替内容。
3. 在任何长计算之前 bootstrap，先建立可保存的 honest partial。相同 case ID（同时检查 JSON 类型和值）及相同原始合同才可复用；已有输出残缺、损坏或合同不同会冲突，不能强覆盖。

```bash
IO=/skills/weekly-skill/scripts/io_guard.py
CASE=/input/case.json
TASK=/input/task.md
WORK=/app/work
OUT=/app/output

python3 "$IO" bootstrap \
  --case "$CASE" --work "$WORK" --output "$OUT" --task "$TASK"
mkdir -p "$WORK"
cp "$OUT/result.json" "$WORK/candidate.json"
```

### 3.2 Evidence、阅读状态与 normalization

- 在 `/app/output/run_record.json` 中核实 `input_manifest`。只有 Agent 真正语义阅读某输入后，才把对应 `content_reviewed` 改为 `true`；存在文件、读取字节和计算 SHA256 本身都不等于读过了。确实无需语义检查时才能设 `review_required=false` 并写真实理由。
- 建立可核对的 `sources`，必要时在 run record 建立 `evidence`、`claims` 和逐项 `recheck`；给页码、表格、条款、真实行号或稳定文档 ID。不能把来源标签 `paper`、`contract`、`measured` 当作事实本身。
- 规范化单位、取整、响应定义和硬约束；保留原始定位。优化器不换算单位。若需要优化，构造符合 `schemas/optimization_request.schema.json` 的 canonical request：顶层必填 `schema_version="1.0.0"`、`case_id`、`evidence`、非空 `parameters`/`plans`、`input_values`、`derived_values`、`objectives`、`constraints`、`decision`。
- `known` 输入有匹配类型的 `value`；`unknown` 输入不带 `value`，不能用 `0`、`1`、`null` 或 `"unknown"` 占位。所有表达式只用白名单 AST，所有 decision 方向由调用方明确给出。

### 3.3 仅在实际需要时运行有限域优化

以下输入必须由真实 case 规范化产生，下称 `/app/work/optimization_request.json` 仅为 runtime 文件位置，不是随包正式请求。`--max-candidates` 是所有 plan-version 合计上限；必须记录实际使用的上限。

```bash
python3 /skills/weekly-skill/scripts/optimize.py \
  --input /app/work/optimization_request.json \
  --output /app/work/optimization_report.json \
  --max-candidates 1000
```

没有规范化请求或不需要优化时，不运行此命令，也不创建空报告冒充已运行。`io_guard.py` **没有** `optimize` 子命令。

### 3.4 Checkpoint 与 finalize：只附真实报告

Agent 编辑 `/app/work/candidate.json`，保留原 `case_id` 类型/值、覆盖原合同所有声明字段，并维护来源、限制和变更记录。若上一步确实生成了报告，`checkpoint` 和 `finalize` 必须对同一个 `/app/work/optimization_report.json` 加 `--optimization-report`；没有实际报告时必须省略该参数，不能拿示意图或静态文件充数。`REPORT_READY=1` 只可在本轮 optimize 实际成功、严格解析报告并确认其 case ID 匹配后设置；新一轮 work 不继承旧标记，单纯 `-f` 文件存在不证明报告属于本次运行。

```bash
REPORT=/app/work/optimization_report.json
if [ "${REPORT_READY:-0}" = "1" ] && [ -f "$REPORT" ]; then
  python3 "$IO" checkpoint \
    --case "$CASE" --candidate "$WORK/candidate.json" \
    --work "$WORK" --output "$OUT" --optimization-report "$REPORT"
  python3 "$IO" finalize \
    --case "$CASE" --work "$WORK" --output "$OUT" \
    --optimization-report "$REPORT"
else
  python3 "$IO" checkpoint \
    --case "$CASE" --candidate "$WORK/candidate.json" \
    --work "$WORK" --output "$OUT"
  python3 "$IO" finalize \
    --case "$CASE" --work "$WORK" --output "$OUT"
fi
```

先尽早 checkpoint 一个合法 partial，再增量改进。checkpoint 在 work 下暂存和审计候选，通过后才更新受保护输出；finalize 审计的是实际 `/app/output/result.json`，不是任意的外部候选。合法 partial/failed 也能 finalized，但 pending、unknown 和限制会保留。被拒绝的 complete 不会被工具自动改成 partial。

每次真实修改可追加独立 hash 链日志；`operation/path/reason` 是实际动作、路径与理由：

```bash
python3 "$IO" record-change \
  --work "$WORK" --operation update-plan --path /main_result \
  --reason "记录实际发生的修改" --evidence-ref ev-1
```

`change_records.jsonl` 不接受缺末尾换行、损坏 hash 链或倒写的历史。需要单独审计当前正式结果时才用 `audit --case ... --result /app/output/result.json --work ... --output ...`；不要用对随意外部候选的 audit 覆盖 last-known-good 诊断。直接 audit 的状态与 run record 不一致时，应按实际转移走 checkpoint。

## 4. 输出字段与状态边界

### 4.1 正式结果字段

原 case 的 `output_contract` 始终优先；不能用本文或最小示例的简化 Schema 替换正式合同。常见顶层字段含义如下，嵌套合同声明的字段也全部必须满足。

| 字段 | 含义与检查边界 |
|---|---|
| `case_id` | 原始值和 JSON 类型都保持不变。 |
| `status` | 保留原合同允许的 raw domain status。`partial`、`complete`、`failed` 是默认任务交付状态；若案例明确允许 `conditional`、`conditional_choice`、`undetermined` 或带证据的不可行状态，不强制改成 failed。审计另有 `processing_status` 三态，详见 io-api。 |
| `main_result` | 按原合同填研究目标、假设、变量/对照、候选及版本、参数、步骤、资源、响应口径、取舍、判读标准、风险、异常处理和确认项；不能把未确认建议写成可执行事实。 |
| `sources` | 列表；每项至少有唯一 `source_id`/`id` 和具体 `locator`/`source_locator`。定位可检查不等于来源支持了结论。complete 至少一个实际来源。 |
| `uncertainty` | 非空对象须写具体 `detail`/`description`；缺响应、未决候选用 `blocking_complete=true` 如实阻断 complete。 |
| `limitations` | 具体字符串或带 `detail`/`description` 的对象；partial/failed 至少要有具体限制或未知说明。 |
| `change_records` | 每项至少有 `operation`、`path`、`reason`；complete 每项还需真实 `timestamp_utc`。它与 JSONL hash 链互补，**不会自动同步**。 |
| `run_record_ref` | 通常为 `"run_record.json"`；相对路径相对 result 所在目录解析。绝对路径直接使用；无字符串时回退 `--output/run_record.json`。 |

result 内嵌的 `sources`、`uncertainty`、`limitations`、`change_records` 等优先于 run record；使用 sidecar 时须清除 result 中同名 stale 占位账本或保持两处一致。若 `additionalProperties=false`，不要强塞辅助字段，辅助账本可留在 run record。

`complete` 要求实际审计通过：`checkpoint_eligible` 与 `audit_passed=True` 同时成立，并且合同覆盖、输入阅读、账本、Schema（适用时）及人工复核没有阻断项。它仍不证明数学或科学正确。`partial` 可含 incomplete/warning，用于保存诚实中间结果，但必须具体说明缺口；`failed` 表示运行或交付失败，不等于科学上的无可行解。避免 `N/A`、`unknown` 等哨兵字符串伪装已填字段；使用结构化状态与具体解释。

### 4.2 优化器报告不是外层状态

`optimize.py` 报告含 `case_id`、`status`、`search`、排名/Pareto/unranked/best 列表、`decision_summary`、`candidates`、`diagnostics`。所有已评估候选（失败、未知、被支配者）都有参数、derived/objective、constraint、evidence 和排除理由账本；数字以精确字符串输出。其状态为：

| 报告状态 | 允许的结论 |
|---|---|
| `optimal` | single/lexicographic 完整枚举、方向明确、无未知潜在赢家；保留全部并列最优，`exact_optimal=true`。 |
| `complete` | feasibility 已完整解释，或 Pareto 已知前沿完整且无未决候选；不表示唯一赢家。 |
| `infeasible` | 完整枚举后既没有 feasible 也没有 feasibility-unknown 候选；这是有效科学结果，不是请求格式错误。 |
| `unresolved` | 枚举完整，但 unknown 仍可能改变决定。 |
| `partial` | 截断于 candidate cap；只能对已评估覆盖范围给临时排名，不能称全局最优。 |
| `invalid_request` | JSON、引用、重复 ID、单位、网格、DAG、固定值等输入错误。 |

hard constraint 采用三值逻辑：任一 fail 即不可行；无 fail 但有 unknown 即可行性未知；全 pass 才可行。unknown 不参与硬排名或 Pareto 支配。非 `optimal` 时 `best_candidate_ids` 为空；Pareto 并列不虚构唯一 winner。优化报告的 `infeasible` 不自动决定外层是 failed；若原任务已完整、如实地回答“无可行解”，外层状态应按其合同和真实审计决定。

## 5. Append-only 版本与变更机制

- **计划身份只认 `(plan_id, version)`。** 修改步骤、参数域、资源合同、约束、模型假设或材料兼容性时创建新版本，不复用旧版身份，不静默改写已评估计划；只有 version 不同也绝不去重。
- 保留旧请求、报告和来源账本，新增计划版本及派生记录；正式 `requests/responses` 的来源约束下再比较，不能为迎合结论删去不利候选。
- `record-change` 只追加 `/app/work/change_records.jsonl`，带连续 `sequence`、UTC 时间、`previous_entry_sha256` 与 `entry_sha256`。变更前验证全链，损坏即拒绝追加；省略 before/after 时保留 null，不捏造磁盘状态。
- result 内的 `change_records` 是另一份可读列表，需 Agent 按事实同步维护。append-only 说的是计划历史和变更链；checkpoint 对当前输出采用原子替换、last-good 保护和必要时回滚，不声称三个文件组成一个跨文件原子事务，更不是允许任意覆盖历史。
- bootstrap 没有 `--force` 旁路。遇到状态冲突，先保留现场、读取诊断并选择新的空 work/output 或修正真正输入，不删除 last-good 开路。

## 6. 科学与安全边界

- 穷举只对**已声明 plans、版本和有限参数域**精确成立。`complete/optimal` 不表示连续空间、域外参数、替代设备、真实响应或未来实验的全局最优。
- 算术精确不等于公式、单位来源、模型或引文正确；`io_guard` 默认不重算公式，也不验证科学/数学最优性。`algorithm_executed=false`、`scientific_optimality_verified=false` 的事实边界必须保留。
- 代理模型、文献关系或历史数据仅在其适用域内有条件使用，标明训练数据、假设、误差和外推风险。本提交没有内置预测模型，也不从缺失响应推出 `Y` 百分比。
- 规划场景中的价格、预算和资源/机时预订不等于供应商报价、真实支付、成功预订或设备可用确认。执行任务前仍需逐项核实时间、币种、税费、数量和有效期。
- 文献中的 SafeOpt、代理模型或约束方法不自动构成本设备的安全证明。安全、伦理、危化物、生物材料、设备量程与联锁、材料耐温/兼容性及专家批准必须由实际责任人确认。
- 不把未经验证的兼容性、安全阈值或设备状态改成 known。危险冲突、越界或缺关键审批时应停止执行建议、转人工，而不是“优化掉”安全约束。

## 7. Robust failure handling

| 现象 | 处理 |
|---|---|
| 缺 case、坏/重复键 JSON、非有限数、合同缺字段 | 区分输入访问/格式错误；保留诊断和已有结果，不用示例代替正式输入。io_guard 通常返回 2（用法）或 3（JSON/缺输入/读写），权限错误为 4。 |
| 科学材料或响应缺失 | 早存 concrete partial；标 `blocking_complete`、具体所需材料/测量和定位，unknown 不填值。 |
| 约束冲突或全不可行 | 列出每个冲突来源和版本，不偷偷放宽阈值。完整证明无可行与尚未解决 Unknown 分开报告。 |
| canonical request 无效 | 修复实际规范化输入；`optimize.py` 的 `invalid_request` 退出码为 2，仍写结构化报告。有效 `partial/infeasible/unresolved` 退出码为 0，不代表已经满足外层 complete。 |
| 搜索到 cap | 保存 `search.total/evaluated/coverage/stop_reason`；只陈述已评估范围，不外推。需要时由调用方显式调整预算再跑。 |
| audit=5 但合法 partial | 读 `validation.json` 的 pending；`audit_passed=false` 可仍有 `checkpoint_eligible=true`，不是系统崩溃。 |
| checkpoint=5/6 | 原 result/validation 不会被该次拒绝覆盖。读 stdout issues，修 candidate 后重试；不要随后盲目 audit 外部坏候选覆盖有用诊断。 |
| 输出冲突、last-good 降级或日志链损坏 | 不强制覆盖、不回退降级、不追加坏链。先保留证据，再在新工作区恢复或作真实状态转移。 |
| 超时、请求预算紧张、读写/权限错误 | 复用已接受的 partial，缩小的是本轮新增工作而非事实披露。70（内部错误/回滚失败）和 130（中断）绝不能视为完成。 |

stdout 的 io_guard JSON 才是机器可读命令结果，stderr 是诊断。参数解析失败、无法写输出也可能非零；要检查实际退出码和文件，而不是只看文件存在。

## 8. Selfcheck 与三次运行一致性

### 8.1 JSON 与 CLI 合同

以下是实际 runtime 的语法检查，不是科学验证。`python3 -m json.tool` 只作一般 JSON 解析；重复键、非有限数、账本与状态还必须由 io_guard 严格检查。

```bash
python3 -m json.tool /input/case.json >/dev/null
python3 -m json.tool /app/work/candidate.json >/dev/null
python3 -m json.tool /app/output/result.json >/dev/null
python3 -m json.tool /app/output/validation.json >/dev/null

python3 /skills/weekly-skill/scripts/io_guard.py --help \
  | python3 -m json.tool >/dev/null
python3 /skills/weekly-skill/scripts/io_guard.py checkpoint --help \
  | python3 -m json.tool >/dev/null
python3 /skills/weekly-skill/scripts/io_guard.py finalize --help \
  | python3 -m json.tool >/dev/null
python3 /skills/weekly-skill/scripts/optimize.py --help >/dev/null
```

finalize 的 stdout 可重定向到 work 下再按 `python3 -m json.tool` 解析；即使在允许的 partial 上，也要核对 stdout 的 `finalization_state`、`audit_passed`、`pending_validations`，并核对 run record 中的 `finalization.state` 与 `validation.json` 的 `result_sha256`。

### 8.2 同一实际 canonical request 的三次确定性复跑

仅在确有真实请求时，在 work 下保留三份独立输出。相同有效请求和 cap 的报告按工具合同应字节一致，输出路径、时钟和 Python hash seed 不进入报告：

```bash
for run in 1 2 3; do
  python3 /skills/weekly-skill/scripts/optimize.py \
    --input /app/work/optimization_request.json \
    --output "/app/work/repeat-${run}.json" \
    --max-candidates 1000
done
cmp /app/work/repeat-1.json /app/work/repeat-2.json
cmp /app/work/repeat-1.json /app/work/repeat-3.json
```

还应核对三次枚举覆盖率、候选 ID、可行性和来源定位。模型生成的自然语言、run record/finalization 时间戳以及 Agent 整体表述不要求逐字节相同；应比较结构化方案、约束结论、不确定性、结果 hash 与合同覆盖，保留真实差异，不能为三跑一致性伪造同一时间戳。

自查至少覆盖：一个已知可行点、一个已知冲突、一个 unknown 输入、一个非法单位/失效 ref、一次 cap 截断和三次复跑；然后执行实际 bootstrap → checkpoint → finalize 流程并核对原合同所有字段。相对路径、主键/来源 ID、告警、退出码和恢复 last-good 也要检查。

**官方 DeepSeek Harness 当前未在本提交文档编制过程中验证。** 上述是复跑方法，不是官方运行记录；不能预称已在三个固定案例、三次重复或对比条件上通过，也不能预称 benchmark 分数或相对基础 Agent 的提升。正式 model、source/case 版本、runtime、预算和 native traces 以平台真实记录为准。

## 9. Maintenance

- 修改 Skill 流程、工具 CLI、请求 Schema 或本文时同步更新对应文档；实际 `--help`、Schema 与实现三者必须一致。不要保留描述旧状态、旧参数或旧路径的注释。
- 计划/合同变更用新 `plan_id,version` 和真实 append-only 记录；工具接口自身的兼容/不兼容变更则应重新评审并清楚递增版本，不能借计划版本掩盖工具变化。
- 每次维护后在隔离的 work/output 做正向、非法输入、缺材料、冲突、中断恢复、strict JSON 和三跑检查。原始 `output_contract` 改变时必须保留冲突并人工处理，不能修改快照元数据绕过检查。
- 来源文件改版时更新具体 locator 并实际重读引文；不要仅因标签或相似标题推定内容一致。维护记录只陈述做过的事务及其证据。

## 10. Versions

| 组件 | 本提交版本 | 说明 |
|---|---|---|
| `scripts/io_guard.py` | `4.0.0` | 严格 JSON、bootstrap、audit、checkpoint、finalize 和 append-only `record-change`；区分 raw domain status 与 canonical processing status，允许多种声明式合同和有证据的规划条件结论。 |
| `scripts/optimize.py` 与 canonical request | `1.0.0` | `schema_version`/`SCHEMA_VERSION` 固定为 `1.0.0`；有限域精确枚举及耦合 evaluation plan 约定。 |
| `scripts/audit_resources.py` | `1.0.0` | 直接费用行精确核算、证据定位和结构明确的 canonical request 编译。 |
| `scripts/audit_decision.py` | `1.0.0` | 原版/修订责任链、硬约束、self-QA 与执行授权分离检查；不合规选择必须阻断。 |
| `scripts/verify_sources.py` | `1.1.0` | 在授权 PDF 物理页做逐字连续引文检查，兼容 catalog/anchor/视觉辅助；不能替代证据支持或领域审查。 |
| `schemas/optimization_request.schema.json` | request `1.0.0` | JSON Schema 结构检查之外，跨引用、单位、DAG 等由 CLI 继续检查。 |

不虚构外部模型、正式 case、来源或 Harness 的版本；这些版本如果实际存在，应从本次 case 和平台记录读取。最小示例里的 `1.0/1.1/2.0` 仅是虚构候选计划版本，不是正式 case 或工具版本。

## 11. 精选来源与方法原始文献（6 篇）

以下文献只提供响应面、昂贵黑箱优化、约束采集和安全探索的**方法背景**，不是本提交效果的证据。当前脚本实现的是精确有限域穷举和 I/O 完整性保护，并未实现下列 GP、Entropy Search、SafeOpt 或约束贝叶斯优化算法。

1. Box, G. E. P.; Wilson, K. B. (1951). “On the Experimental Attainment of Optimum Conditions.” *Journal of the Royal Statistical Society: Series B* 13(1), 1–38. [doi:10.1111/j.2517-6161.1951.tb00067.x](https://doi.org/10.1111/j.2517-6161.1951.tb00067.x)。响应面方法原始论文。
2. Jones, D. R.; Schonlau, M.; Welch, W. J. (1998). “Efficient Global Optimization of Expensive Black-Box Functions.” *Journal of Global Optimization* 13, 455–492. [doi:10.1023/A:1008306431147](https://doi.org/10.1023/A:1008306431147)。昂贵黑箱 EGO/期望改进方法原始论文。
3. Hennig, P.; Schuler, C. J. (2012). “Entropy Search for Information-Efficient Global Optimization.” *JMLR* 13(57), 1809–1837. [论文页](https://www.jmlr.org/papers/v13/hennig12a.html)。
4. Snoek, J.; Larochelle, H.; Adams, R. P. (2012). “Practical Bayesian Optimization of Machine Learning Algorithms.” *Advances in Neural Information Processing Systems 25*. [论文页](https://proceedings.neurips.cc/paper/2012/hash/05311655a15b75fab86956663e1819cd-Abstract.html)。
5. Gardner, J.; Kusner, M.; Xu, Z.; Weinberger, K.; Cunningham, J. (2014). “Bayesian Optimization with Inequality Constraints.” *ICML, PMLR* 32(2), 937–945. [论文页](https://proceedings.mlr.press/v32/gardner14.html)。文中作者 `Zhixiang` 即 Z. Xu。
6. Sui, Y.; Gotovos, A.; Burdick, J.; Krause, A. (2015). “Safe Exploration for Optimization with Gaussian Processes.” *ICML, PMLR* 37, 997–1005. [论文页](https://proceedings.mlr.press/v37/sui15.html)。其安全保证有明确 GP 与正则性假设，不能外推成一般实验安全批准。

正式科学建议必须引用 case 实际提供的原始论文、手册和合同，并给精确 locator；上述六篇不能替代具体领域证据。

## 12. 最小示例与公开链接

`examples/minimal/` 是自制 `SYNTHETIC_INTERFACE_DEMO / EXAMPLE-ONLY`，演示含三个虚拟计划版本、有限选择、planning price、一个价格冲突和缺测量的 partial。它不是三个正式案例之一，不是实测答案，不是优化报告，且未经过 DeepSeek Harness。调用见 `examples/minimal/README.md`。

GitHub 公开附加分需用户另行批准并提供经未登录浏览器核验的真实公开 URL。批准后才能在提交根目录 `GITHUB_URL.txt` 第一行写该 URL，必要时第二行写本题作品相对路径；本文没有已批准 URL，不填占位/无效链接，不执行仓库发布或 `git push`，也不承诺附加分。
