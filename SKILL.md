---
name: weekly-skill
description: >-
  Source-grounded experimental parameter optimization for cases with finite
  domains, equipment/material/safety constraints, budgets, bookings, or
  versioned candidate plans. Reads actual case files and scientific PDFs,
  compares plans with reproducible arithmetic, preserves evidence and unknowns,
  and produces executable-or-explicitly-blocked experiment recommendations.
  用于基于真实科研材料、有限参数域和设备/材料/预算约束的实验参数优化、
  三版候选方案比较、成本—响应权衡、风险处置与可复核结果交付。
---

# 来源约束的实验参数优化

把**实际输入和原始论文**转化为受约束、可复算、预算内且能复核的参数建议。交付的是本次案例的真实运行结果，绝不能从示例、文件名、领域标签或模型记忆预填案例答案。

## 启动即做

1. 依次实际阅读 `/input/case.json`、真实 `task.md` 和 case 指向的全部相关材料。记录原始 `case_id`、整个 `output_contract`、任务目标、资源/价格/预订合同、全部候选计划及其版本。若附件与 `environment/resources.yaml` 随案提供，查明真实路径和工具；不要假定它在 Skill 目录中。
2. 保留原合同**全部声明字段**，不只 required 字段。只有当前输入实际展示的 public rubrics/hard thresholds 才能逐条执行；未提供的细则、阈值和白板不得猜测。
3. 开始长阅读前先初始化输出。使用真实绝对路径，不依赖当前目录：

```bash
python3 /skills/weekly-skill/scripts/io_guard.py bootstrap \
  --case /input/case.json --work /app/work --output /app/output \
  --task /input/task.md
cp /app/output/result.json /app/work/candidate.json
```

`task.md` 不在该位置时，让 bootstrap 探查并核对实际发现路径。后续科学内容只编辑 `/app/work/candidate.json`；`result.json` 与 `validation.json` 由保护工具更新。可在 `/app/output/run_record.json` 追加真实阅读、证据和复核记录，但不得手改其 case ID、合同快照、受保护状态或 validation；先补具体 uncertainty/limitations 并尽早保存合法 partial，再增量改善。所有最终实际结果与可编辑产物放在 `/app/output`，研究中间件在 `/app/work`。

4. 首次只读 [workflow](references/workflow.md) 和 [evidence](references/evidence.md)；遇到失败时读 [recovery](references/recovery.md)。构造优化请求前读 [优化 API](references/optimization-api.md) 和 [请求 Schema](schemas/optimization_request.schema.json)，检查点/合同问题按需查阅 [I/O API](references/io-api.md) 对应章节而非反复通读。完整使用与维护说明见 [README](README.md)。遵守 workflow 的 60/300/570/720/810 秒检查点和至多 40 次模型动作，64 请求与 900 秒是平台硬上限而非可透支目标。

## 一、从证据构造可行域

### 实际读取

- 以原始文件、论文、补充材料、设备手册、SDS、历史记录与 organizer contract 各自支持的范围为边界。真实原 PDF 没有预抽取答案层：按 workflow 用环境已登记工具读取原生文字/表格，相关页必要时渲染/OCR并回看原件。读不了、读不清和内容不支持结论必须分开记录。
- 不依赖标题、标签、网页摘要或“领域内一般如此”补事实。哈希登记只证明文件字节，只有实际检查内容后才可将 manifest 的 `content_reviewed` 改 true。
- 不为全面而跑出预算；优先与目标、设备、硬约束、三版差异、响应口径、价格和排期有关的页段，明确读取范围。

### 建立证据而非只列文献

在 work 中建立 registry，并把可复核投影放进合同允许的结果字段或 `/app/output/run_record.json`。每个关键参数、阈值、价格、时长、响应定义和结论至少能追溯：

```text
claim/evidence ID → 真实来源 → 页/表/单元格/JSON Pointer/行段 → 原文短引文
                 → 主体 + 原始值 + 单位 + 限定条件 + 适用版本 + 证据性质
```

必须绑定 `value + unit + entity + qualifier`，尤其是表头、脚注、极性、精确设备型号/附件、材料级别和测量口径。文献观测、历史数据、前瞻资源/价格合同、确定性推导、模型预测、待验证假设和未来实测不得混写。没实测的未来响应不能编数；外部安全审批、真实报价、booked 状态也不能由计划推断。

`quote` 只放该物理页**逐字连续**的短片段；仅允许折叠原生折行/多空格。不同片段拆成 `spans`，不同事实/条件拆证据，解释放 `context_note` 而非原话。严禁换主语、改写符号、跨段拼接或用省略号凑引文。按 [来源核验说明](references/source-verification.md) 建 registry，并用实际工具复核；文字 exact match 不等于科学支持。

按 [evidence](references/evidence.md) 保留原始与规范值、精确换算和计算链。范围、步进、量程、分辨率、精度、推荐运行范围、绝对最大值与安全限值不是同一个概念。不同来源冲突时同时保留原值、适用条件与版本，不能取平均、投票或偷偷删掉不利证据。

### 三版方案与参数域

- 实际提供的每个 `(plan_id, version)` 均单独保留与比较，通常是三版；没有三版不能复制凑数。它们是**候选版本，不是已做三轮实验**。新版本追加，不覆盖旧史；新版本不自动更优。
- 每个参数填写建议值、原始/规范单位、明示有限候选值或精确步进、可行区间及证据。硬约束取设备、材料、安全、任务与资源合同中同时适用约束的交集；交集为空是问题结论，不能静默放行。
- 只将来源支持的 `allowed_values/grid` 送入优化器。无限连续域、缺失步长、无目标方向、缺币种换算或缺公式时，不得自行补默认值形成伪精确优化。

## 二、比较与计算，不虚构响应

### 完整科学方案

三版使用同一可比字段，但保留真实差异。每版或共享引用的方案至少覆盖：

- 可检验假设、independent/dependent/nuisance variables、固定条件和能排除混杂的 controls；
- 有序步骤、具体设备/材料绑定、参数设定与容差、独立实验单位、重复/技术读数与随机化或区组（仅有依据时）；
- 响应的分子/分母、单位、仪器/方法、测量时点、聚合/波动及接受或停止判据。不能默认 `n=3`、显著性或统计功效；
- 样品/对照/重复数，耗材、测量、机时与人工成本，准备/等待/执行/检测/排期的依赖和关键路径；不能把并行步骤简单相加；
- 候选怎样产生，已评估/淘汰结果、资源覆盖、停止原因、风险触发、降级/转人工，以及安全/伦理/设备可用性/专家确认 gate。

价格、包装、最小计费单位、税率、batch cap、共享 setup、currency 和 booking 时区只按实际合同计算；未提供即为 unknown，而非零或免费。技术重复不能被当成独立重复。响应未知时仍要给测量设计和验证计划，但不得借成本可算就发明“产率最优”。

### 确定性有限域后端

若合同已含结构明确的直接费用项，优先用 `scripts/audit_resources.py` 编译账本，避免手工搬运数字；按 [资源审计说明](references/resource-audit.md) 传递实际 hard-constraint 来源明确的 caps。它只做支持的数字关系，某行金额、币种或数量语义冲突时必须保留 unresolved 并回查原始来源，不能为生成 request 强算。部分 profile、符号公式、全生命期排程或实际价格确定不了时，回到来源约束与独立精确核算，不伪造完整率或科学赢家。

只有存在来源支持的有限参数域且能够规范化时，生成 `/app/work/optimization-request.json`，再运行：

```bash
python3 /skills/weekly-skill/scripts/optimize.py \
  --input /app/work/optimization-request.json \
  --output /app/work/optimization-report.json
```

先在枚举前求各 plan 域大小的乘积及总组合数；很小且可完整枚举时不设 cap。若总量可能耗尽时间/内存，从资源预算推导 candidate cap 并明确使用 `--max-candidates`，保存 total/evaluated/coverage/stop_reason。不要自动抽样或为得到唯一赢家放大预算；截断只能是 partial。

此工具只做：从输入证据投影的有限域枚举、白名单 AST 精确有理数运算、单位一致性检查、三值硬约束与显式目标比较。它**不会**从原 case 自动猜字段、换算不同单位、安装求解器或证明科学公式适用。模型负责来源和语义，工具负责它实际支持的算术。

- `unknown` 不带 value；不得用 `0`、`1`、`null`、空串或 `N/A` 代替未知。错误输入、未知可行性和确认无可行解是不同状态。
- `decision` 仅使用任务/用户明示的 `feasibility/single/lexicographic/pareto` 和方向；未给偏好不添加等权总分。Pareto 前沿不是唯一赢家，所有并列保留。
- `optimal` 只指完整声明域、明示公式/方向、无未知潜在赢家时的精确最优，不是连续空间或科学体系的全局最优。
- `complete` 可表示 Pareto/feasibility 完成；`unresolved/infeasible/partial` 按 API 如实解释。候选数少不必引入复杂模型，候选多也不得只改结论文本掩盖 coverage。

无真实历史观测与适用模型时，不拟合 GP、不伪造代理响应或置信区间。获得真实逐轮数据后才可**建议后续**序贯设计/贝叶斯优化；那仍是未来计划而不是本案已完成的闭环实验。即使使用模型，也要报告适用域、训练/验证和 uncertainty，不能抵消已知硬约束。

### 候选版本与选择责任门

不得把拆批、改日期、重排时窗、缩短每批工作时长、换费率或换设备当作原版的“排程说明”来绕过约束。原候选的 blocking fail 必须保留为失败或不明朗；如因这种原因选择修订，必须：

- 给新 `candidate_id` 和**新版本**，链接原始 plan/version，保留旧版评价；
- 给完整 canonical 参数对象、实际新哈希及每项 before/after/理由/来源；
- 对受影响的成本、人数、设备、日历和参数关系重新计算；新增资源/批准而不确定时保持 unknown，绝不按零费用或已获批准处理；
- 将选择明确标为 revision proposal/conditional planning，而不是原版合格、exact optimum 或 execution authorized。

按 [选择审计说明](references/decision-audit.md) 构造 ledger 并实际检查。不要因某种 `conditional` 名称而给 blocking fail 放行。若一个不可分割的周期或 session 已经超过对应的日/窗口硬上限，就不能靠 `ceil(total_hours/cap)` 算出“最小可行天数”；应记录原子约束失败，修订或维持未定，不能输出可满足该上限的伪数值。`self_qa.matches=false`、同一 owner 的重复检查 ID 和矛盾验收必须修正；失败的早期检查保留在变更历史，不能只删掉：

**所有自检声明都必须能就地复核。** 写“ID 唯一”“checks 一致”“matches=true”“已验证 N 项”时，输出中要有该 owner 的稳定 `check_id` 或其它合同允许 ID、实际 `matches/status` 与来源定位。若约束闭合顶层不允许新增 self_QA 键，就在其验证/风险/确认记录及 sidecar 中明确引用真正的 `result/validation/decision-validation/source-validation` 产物，不得没有 ID 却声称唯一、混用全目录验证和局部 quote exact-match 成一句“全部通过”，也不能删除 `matches=false` 来制造绿色。

```bash
python3 /skills/weekly-skill/scripts/audit_decision.py \
  --case /input/case.json --ledger /app/work/decision-ledger.json \
  --output /app/output/decision-validation.json
```

`invalid` 就不能宣称已满足最优性/完成核验，需修复或保留真实 partial；`conditional` 也不是科学批准。这里的选择账本只是对显式记录的约束与身份检查，仍须逐项回到原案例独立复算其规则和内容。

## 三、把结果变成可复核交付

先保存和映射原合同，再写完整内容。不要用本 Skill 的内部 schema 替换原 `output_contract`，也不要因 `additionalProperties=false` 强塞辅助字段。元信息允许内嵌时就地填全；不允许时置于 `run_record.json` 并用合同允许的 ref 指向。参考但不照抄 [最小合成示例](examples/minimal/README.md)，它绝不是正式案例或运行记录。

最终结果必须保留原类型/值的 `case_id`、主方案/选择、来源定位、状态、不确定性、限制、变更记录和 claim/evidence 复核。候选表不能只留赢家；至少保留已评估的参数、每约束 pass/fail/unknown、精确派生值、资源/成本、支配或淘汰理由与可能改变决定的未知项。

### 自检和独立复核

在最终声明前，做与写作者不同的反向检查：

1. **约束反证**：尝试为推荐点找有限域、设备/材料、安全、预算、预约或互斥反例；任何 selected/best 必须确实 confirmed feasible。
2. **精确复算**：独立检查最终推荐与边界点的原始输入、单位、run/package/batch 取整、成本小计/总计、关键路径；不得将同一函数二次运行称为独立科学验证。
3. **证据回访**：逐条重读关键引文的真实 anchor，核对值、单位、对象、表头脚注和条件； version change 后重算受影响结论。
4. **选择审计**：查明推荐是否被另一可行候选支配、是否有 unknown 候选仍可能胜出，以及权重/模型/容差是否被暗中发明。
5. **诚实性扫描**：清除无来源的“已测量”“已预订”“已批准”“保证安全”“全局收敛”“提升 X%”。没有平台原始 baseline 和 traces，不比较/声称相对增益。
6. **合同和制品**：原字段无遗漏，非法 JSON、NaN/Infinity 及伪装已完成值的空占位、N/A/TBD 不得输出；每字段内容真实。partial 中合同认可的显式 unknown/null 和未完成集合可以保存，但必须同时有具体限制，且不能借此通过 complete。若案例要求 Office/可编辑图，实际生成并用真实渲染产生 PDF/PNG preview，不能只做扩展名改名。

将独立重算与 claim recheck 的真实执行证据写入运行记录。代码结构 PASS 不等于来源支持，来源存在不等于科学正确；schema、checkpoint、审计、safety approval 是四种不同事实。

### 检查点与最终化

每个实质改进执行（只有本轮确实生成了匹配 case 的优化报告才附带该参数）：

```bash
python3 /skills/weekly-skill/scripts/io_guard.py checkpoint \
  --case /input/case.json --candidate /app/work/candidate.json \
  --work /app/work --output /app/output \
  --optimization-report /app/work/optimization-report.json
```

应使用实际变更记录：

```bash
python3 /skills/weekly-skill/scripts/io_guard.py record-change \
  --work /app/work --operation revise-plan --path /main_result \
  --reason "写明真实修改原因" --evidence-ref 实际证据ID
```

最终执行：

```bash
python3 /skills/weekly-skill/scripts/io_guard.py finalize \
  --case /input/case.json --work /app/work --output /app/output \
  --optimization-report /app/work/optimization-report.json
```

未创建真实优化报告时，从 checkpoint/finalize 删除 `--optimization-report` 整行，绝不能拿示例报告代替。读取实际 stdout、`result.json`、`run_record.json` 和 `validation.json`；不能只看到退出码 0 或文件存在就称完成。合法 `partial/failed` 可 finalized 但依旧不是通过；非法 complete 遭拒时修复实际项或有意 partial，不能自动标绿。已有合法 complete 不允许降级覆盖。

**不要为了通过审计反复改状态。** 合同明确允许的 `conditional/conditional_choice/undetermined` 可表示已完成的规划比较，即使真实实验仍待批准或未来响应仍未知。交付审计的 `processing_status` 与实验执行许可是两件事。按 bootstrap 的 `manual_review_next` 和合同映射一次性完成真实人工复核；结构化记录 cause/confirmation，勿将 unknown 改成已知，勿删输入、硬约束或真实待确认项，勿重复试探 checkpoint 期待变绿。

## 失效与适用边界

- **材料缺失**：列缺什么、影响哪版/字段并给补充路径，不以相似论文补造。
- **内容已读但无法判断**：保留冲突与 unknown，报告可完成部分及需要的专家/观测，不标材料不存在。
- **无可行解**：只有有效请求完整求值且所有点明确 infeasible、无 feasibility-unknown 才可下结论；列 binding constraints，不选“违规最少”的点，不自行放宽安全/伦理门限。
- **工具/权限/服务失败**：保留真实错误、影响范围和 last-good，使用已登记替代工具；不绕权限、不偷装依赖、不伪造暂停或重试成功。
- **时间/请求接近预算**：冻结新增检索，优先保存完整合同、真实证据、硬约束和诚实 partial。不得为了更复杂的分析牺牲最终落盘。
- **非本优化范围**：不作真实实验执行、供应商确认、安全/伦理批准或未测科学效果承诺。可交付的是可执行且标好前置 gate 的方案，或精确知道为何不能执行的 partial。

最终回答简要指出 `/app/output/result.json`、真实状态、主要建议/候选、验证覆盖和仍需确认的事项，不把工具自检描述成官方 benchmark 成绩。
