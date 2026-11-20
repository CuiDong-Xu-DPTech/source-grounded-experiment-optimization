# Recovery：分类处置、last-good 与最终化

本文件与 `/skills/weekly-skill/references/workflow.md` 的时间门槛、`evidence.md` 的记录规则配套。完整退出码和字段以 `io-api.md`、`optimization-api.md` 为准。恢复的目标是保存真实进展并明确阻断范围，不是让结果“看起来通过”。

## 1. 先分类，再恢复

每个异常建立 issue：`issue_id`, `category`, `observed_error`, `source/input_refs`, `affected_plan_versions`, `affected_claims/fields`, `blocking_scope`, `actions_taken`, `recovery_owner`, `status`。只写真实命令、退出码、错误和实际处置；不得把猜测写成故障原因。

| 类别 | 识别问题 | 核心动作 | 不能自动宣称 |
|---|---|---|---|
| `missing_material` | 应有文件/条款未提供或 manifest `found=false`，还是只是没找到路径？ | 定位具体缺口及受影响字段，继续其余可核验工作 | 不可读为“无此限制”，不可用相似资料冒充 |
| `indeterminate` | 材料已读，但信息不足、条件不匹配、响应未测或 unknown 可能改变决定？ | 保留 unknown、并列和 unranked，说明还需什么实际信息/观测 | 不选确定赢家，不把 0/false/pass 当未知 |
| `no_feasible` | 有效请求完整枚举后全部明确 infeasible，且无 feasibility-unknown？ | 保留失败候选和 binding constraints，输出情景内无解 | 不静默放宽安全、伦理、设备或合同约束 |
| `conflict` | 来源值/定义/适用范围、case 身份或输出状态互相冲突？ | 并存记录 anchors 和版本，阻断受影响结论，转指定确认人 | 不平均、不挑有利值、不用新文件抹掉历史 |
| `tool_permission_runtime` | 是工具用法、输入、权限、运行时还是平台服务失败？ | 保存实际错误，使用已登记的合法恢复路径和 last-good | 不以其他状态名掩饰工具失败，不绕过权限 |
| `partial` | 已有真实部分成果但仍有明确缺口？ | 保存具体 limitations/pending，维持 last-good，尽早 finalize honest partial | finalized partial 不是 audit/science/approval pass |

上述类别可以并存，但必须独立建 issue。例如 PDF 不存在是 missing；PDF 存在且能读但表格语义不明是 indeterminate；无权限是 tool/permission；有效域求解无解才是 no-feasible。`invalid_request` 是请求错误，不是 no-feasible。

## 2. 六类独立流程

### 2.1 Missing material

1. 对照 `/input/case.json`、`/input/task.md` 和 input manifest，记录预期路径/条款、实际 `found/readable/content_reviewed` 状态及缺失检测方式。没有实际见过的附件不能写进 Sources 作为已读证据。
2. 列出被阻断的 output-contract 字段、三版各自的参数/响应/成本和安全确认。其它可独立验证的部分继续，但删除基于缺失资料的推荐。
3. 可选恢复只有：找到任务明确提供的真实替代路径；或请求/登记所需原始材料。禁止使用 domain label、论文题名、常见设备参数或网络相似案例补造。
4. 缺口仍存在时，将相关 Parameter/Response 标 unknown、future measured response 按合同保持 null，并保存 partial。若缺失安全/伦理资料，仅可交付计划草案，不能写成已可安全开机。

### 2.2 Indeterminate / unresolved

1. 区分根因：缺 response、未知约束、目标方向缺失、来源不可迁移、未知计算或仍有未评估/未确认可行候选。
2. canonical request 中 unknown 输入不带 value；优化报告保留 `unranked_candidate_ids` 和 issues。遇到候选 cap 时报告 coverage，并指明未评估空间可能改变排序。
3. 只把确定可行的评估描述为局部/情景结果。`unresolved`、`pareto complete`、`feasibility complete` 均不自动生成唯一推荐。
4. 解决项必须真实：补读原文可以解决语义不明，新增实际观测才能更新经验响应，用户/合同明确方向才能改变 decision。不得通过编造一轮或多轮实验“收敛”。
5. 若结束前未知仍可能改变 winner/安全性，最终保持 partial 或提交不含唯一 winner 的完整合同结果（仅在合同和审计允许时），并完整披露。

### 2.3 No feasible

1. 先验证这是有效有限域、有效公式/单位和完整枚举的结果；grid 反向、step 非法、固定值不在域、非法 ref、单位错误或 unknown 约束都不是科学无解。
2. 确认报告 `status=infeasible` 的语义：所有候选明确失败，且没有 feasibility-unknown。保留全部失败原因和各硬约束结果。
3. 输出“指定版本、域、模型和场景下不可行”，列出 binding constraints 与对应来源；不得选失败最少者冒充 confirmed feasible。
4. 若要修改目标、预算、域或互斥条件，只能建立明确的新 scenario 和 `(plan_id,new_version)`，写 change record 与批准需求。涉及安全/伦理的硬边界不得由 Agent 自己放宽；需要材料/设备变更时转专家。
5. 无推荐候选本身不等于 result 必须 failed：若输出合同能完整表达 no-feasible、证据和后续确认项且审计通过，可按实际内容定状态；不得为了满足“推荐值”字段虚构一个候选。

### 2.4 Conflict

1. 科学来源冲突按 evidence.md 同时保留原值、单位、条件、时间/版本和最小 anchors，标注冲突影响。不能取平均、仅取更保守值或引用更高影响因子就结案。
2. 实际 case/task 的特定要求与原论文背景不一致时，先确认是否同一实体/定义；安全/伦理约束冲突永远不静默降级。需要权威解释时保持 blocked，并写明专家/负责人确认项。
3. 三版冲突不通过改名、覆盖或删除旧版解决；每版独立 issue 和 change record。相同 `(plan_id,version)` 重复或跨引用失败是请求校验问题。
4. case ID、原 output contract、已有输出或状态冲突时停止覆盖。`bootstrap` 仅在三个已存在输出合法且 case ID 类型/值和合同一致时复用；否则使用冲突恢复，不提供 force 旁路。
5. 解决来源冲突需要新增可定位的权威 Evidence、真实复核和 change record；旧 Claim 标 rejected/superseded，而不是从 history 消失。

### 2.5 Tool / permission / runtime

- **权限：** 保存实际 denied path/tool 和操作，不更改权限、不上传原 PDF、不尝试替代账号或绕过访问控制。仅使用任务明确授权且环境已登记的其它读取方式；无法读取走 missing/blocked partial。
- **工具用法/科学输入：** 读取真实 `--help` 和 stdout JSON diagnostic。io_guard 退出 2 是 CLI 用法等错误，3 是输入/JSON/读写等错误，4 为权限，5 为审计/候选拒绝，6 为状态冲突，70 为意外/回滚失败，130 为中断。不能仅凭“退出 0/非 0”改状态。
- **优化器：** valid 的 `partial/infeasible/unresolved` 退出码也是 0；退出 2 可能是 invalid request/参数解析/写输出失败。修复请求本体，不用删减硬约束来消错。
- **Schema：** 找不到 `jsonschema` 时保留 `executed=false, valid=null`，不静默安装依赖或写成 valid。非 Schema 合同必须由真实复核者完成 manual review；外部队 `$ref` 导致无法本地校验时不能标 complete 通过。
- **运行时/服务：** 记录原生错误和已保存进度。只有平台真实记录的服务故障才能称为 paused service failure；不能自行增加预算、虚构重试成功或伪造 native trace。
- **损坏 journal/输出：** 不用重写整条 chain 来掩盖损坏。保留现场、恢复到已验证 last-good，重新生成候选并记录。

工具恢复后重跑同一已保存请求可检查复现性，但不会自动证明单位、公式、科学结论或批准状态。

### 2.6 Partial

1. 优先保留：case ID/合同映射、已读 Sources 和原子 Claims、三版当前状态、失败/unknown 原因、已验证算术和待确认事项。
2. partial/failed 必须有具体 uncertainty 或 limitations，且不能有 fatal/error。写清“目前已知什么、哪个字段/版本被什么阻断、下一步需要什么”，不用 N/A 空壳。
3. `checkpoint_eligible=true` 只表示可保存中间结果；`audit_passed=false` 可伴随合法 partial，二者不矛盾，也不是完成。
4. 合法 partial/failed 可以被 finalize 为 `finalization.state=finalized`，但 pending 和限制必须保留。对外表述为“已保存/最终化 partial”，不是“通过验收”或“科学上成立”。

## 3. 状态、checkpoint 与 last-good

### 状态边界

- result 保留原合同允许的 raw domain status；默认 `partial | complete | failed`，案例明确允许的科学 final 状态不强制改写为 failed。交付审计使用独立的 canonical `processing_status`，不可行状态还须满足 io-api 的穷尽求解或案例冲突证据门槛。优化器的 `optimal/complete/infeasible/unresolved/partial/invalid_request` 是另一层状态，禁止机械映射。
- 直接 `audit` 要求 result 与 run record 状态一致；有意 `partial→complete` 必须通过 `checkpoint`。complete 需要 audit_passed 和 checkpoint_eligible，同时不存在阻断的 unknown/manual/schema/content-review 项。
- 工作流可以有意把未达门槛的候选修为 honest partial，但不能由 finalizer 自动降级。若 complete 已被 checkpoint 接受，之后不得用 partial/failed 覆盖；若变更引入问题，应修复并恢复该 accepted complete 对应内容，而不是重写 history。
- `partial/failed` 可保存不完整的科学工作，但仍要满足 JSON 类型、case ID、合同快照和安全保护；错误实际类型不能借 partial 隐藏。

### 每个改进点的保存顺序

1. 只修改 `/app/work/candidate.json` 和允许的执行记录，不直接手改受保护的 output result。
2. 对真实修改运行 `record-change` 或在合同 change_records 中记录 operation/path/reason/实际时间；更新三版时新建版本，旧版仍可追溯。
3. 对候选执行 checkpoint；候选会被 staging 后以受保护的 `/app/output/run_record.json` 为基准审计。用实际 optimization report 做诊断，工具本身不会重跑搜索。
4. 只有 checkpoint 明确接受才把它视为新 last-good。保存实际 result/validation/run_record 的 hash 和 accepted status；不接受口头“应该已写入”。
5. checkpoint 返回 5/6 时先读 stdout issues、当前 output result 和 last-good validation。不要随后对非法外部 candidate 盲目 audit，覆盖有用的 last-good validation；修正 candidate 后重新 checkpoint。

单文件原子替换不等于三文件跨文件事务。若出现 `checkpoint_rollback_failed`，保留日志和现场、停止继续覆盖并报告需要运行负责人处理；绝不能声称完整提交成功。

## 4. Finalizer 失败与冻结恢复

`finalize` 审计的是实际 `/app/output/result.json`，不是 `/app/work` 中“本来希望交付”的 candidate。失败时：

1. 保存 finalize stdout/stderr、退出码、实际 result hash、run_record 的 `finalization.state/pending_validations/result_sha256` 和当前 validation。不得改写原始错误。
2. **非法 complete → rejected：** 不能声称完成，也不能称 finalizer 已自动改成 partial。若没有 accepted complete 保护，回到 work candidate 修复具体问题；确实无法达到 complete 时，基于事实有意设置 partial、补具体 limitation，再经 checkpoint 审计。
3. **已有 accepted complete：** 降级保护仍有效。恢复原 accepted result 对应字节并检查 hash；不要用后续失败诊断或 partial 覆盖它。validation 被失败诊断覆盖时，也应基于实际 last-good 重新审计，而不是修改 metadata 让它显绿。
4. **合法 partial/failed finalized：** 查看仍有的 arithmetic/source/scientific pending，并保留 honest 状态。finalized 不是 approval。
5. **写失败/rollback failed/中断：** 停止科学内容更新，以最后明确 accepted checkpoint 为恢复源；完成只读身份和 hash 核验后记录剩余故障。无法核验的交付标为未完成，不猜测文件状态。

810 秒内容冻结后只执行上述恢复和记录，不添加新来源、方案、轮次、收益或科学结论。若恢复也失败，把真实 last-good 路径、错误与未交付范围留给 reviewer。

## 5. Approval 不是自动 pass

以下四类检查必须分别报告：

| Gate | 能证明什么 | 不能证明什么 |
|---|---|---|
| structural / contract | JSON、case ID、声明字段、账本结构、实际执行的 Schema/manual review | 不证明公式、参数科学可行或现场批准 |
| arithmetic | 实际运行的精确公式计算、单位和独立复核范围 | 不证明公式适用；io_guard 默认 arithmetic `not_run` |
| source_anchor | ID、locator、引用和 recheck 可机械解析；人工重读另需真实记录 | 不自动证明原文支持 Claim，更不证明可迁移 |
| scientific / safety / ethics / expert approval | 只能由相应真实复核、机构和外部确认记录支持 | schema、optimization status、checkpoint、audit、finalize 均不能替代 |

因此：

- `audit_passed=true` 不是安全/伦理审批，也不是实验可执行许可；`scientific_or_mathematical_validity_claimed=false` 和工具保留的科学复核限制不能删除。
- `approval_required`、`submitted`、`approved` 只能按实际记录填写；安全审批未完成时可交付**待批准的实验计划**，不能声称“已获批准”或“保证安全”。若任务本身要求实际批准，缺失批准同时阻止 complete。
- 计划价格只产生 scenario cost，不产生 vendor quote/actual charge；计划资源窗口只产生 proposed booking，不产生 reservation。只有真实供应商/平台确认 Evidence 才能改变相应状态。
- 未执行实验时 future response 不得写成 measured，未运行对照不得声称质量/效率增益，未实际运行三版不得伪造实验 history。结构完整和现场事实是两条独立交付线。
