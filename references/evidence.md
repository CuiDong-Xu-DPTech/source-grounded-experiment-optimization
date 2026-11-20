# Evidence registry：最小记录、锚点与来源质量

本文件定义执行期证据模型。执行顺序见 `/skills/weekly-skill/references/workflow.md`，异常状态见 `/skills/weekly-skill/references/recovery.md`。以下是记录规则，不是任何正式案例的数据或预填答案。

## 1. 存储和投影规则

- 丰富记录可保存在 `/app/work/evidence-registry.json`，再投影到合同允许的 `result.json` 字段或 `run_record.json` 的 `sources/evidence/claims`。若合同禁止额外字段，不强行内嵌。
- 给 `optimize.py` 的 evidence map 只投影为 `{locator, quote, type}`；给 io_guard 的 Source 投影至少保留 `source_id` 与 `locator=path_or_url`，Evidence/Claim 另外保留可解析 ID、引用和 `recheck.method/status`。完整参数、响应、成本及限定条件不能因为优化器字段较少而丢失。
- `evidence_refs` 只引用已登记 ID。重复 ID、失效引用、来源不存在、未经阅读的附件不得被包装为已核验事实。
- 未知使用显式 status 并省略未知 value。不能用 `N/A`、`TBD`、空串、0 或分类哨兵代替；canonical request 的 unknown `input_values` 不得带 value。最终输出若合同允许，可用 `null` 表示未测的 future response，同时给出 pending/limitation；若合同不允许，只能保留 partial 和具体缺口，不能为满足 Schema 造数。
- 所有时间戳、hash、阅读/复核状态来自真实执行。文件名和论文标题能定位来源，但不能证明其中的任何主张。

## 2. 所有记录共享的绑定原则

一个可用值不是孤立的 number，而是：

```text
(value, unit, entity_id, definition_id, applicability,
 qualifier_set, value_nature, temporal_scope, evidence_refs)
```

其中 `applicability` 至少指出适用的 plan/version 或全局 shared domain；`qualifier_set` 只登记来源实际给出的材料、设备、方法、批次、压力、介质、时间窗等条件。主体、定义、单位或限定条件不同，就必须使用不同记录或显式转换关系，不能只靠相同符号合并。

`plan_id + version` 是候选身份，不能因参数赋值相同而折叠。`observed_round` 只对真实实验观测填写；优化枚举、文档修订和模型重算都不是实验轮次。

### 最小字段约定

下表中的“最小字段”是创建该记录必须有的字段；某些条件字段只在适用时有值。字段缺失不能用占位符补形。

| Record | 最小字段 | 必须额外绑定的条件字段 |
|---|---|---|
| **Source** | `source_id`, `kind`, `path_or_url`, `role`, `found`, `readable`, `content_reviewed` | 本地文件实际可读后记录 `media_type`, `size_bytes`, `sha256`；有则记录 `edition/version`, `publication_date`, `doi`。网络页面只有在实际取得内容后才作为 Evidence 来源 |
| **Evidence** | `evidence_id`, `source_id`, `locator`, `quote`, `type`, `extraction_method`, `recheck` | 表格需 `row/column/header_path`；PDF 页码类型需 `physical/printed` 区分；换算或 OCR 需 method detail 与限定条件 |
| **Claim** | `claim_id`, `statement`, `epistemic_status`, `evidence_refs`, `recheck` | `entity_id`, `parameter/response/constraint ID`, `qualifier_set`, `applicability`；预测还需 `model_ref`，计算还需 `formula/derived_ref` |
| **Parameter** | `parameter_id`, `entity_id`, `definition`, `type`, `unit`, `value_status`, `value_nature`, `evidence_refs` | known 时须有 `domain_or_assignment`；`plan_id/version` 或 shared-scope；grid 的 `inclusivity/step`，categorical 的 allowed values，固定值、材料/设备限定及量程/安全依据 |
| **Response** | `response_id`, `entity_id`, `definition`, `measurement_method`, `unit`, `aggregation_rule`, `value_status`, `value_nature`, `evidence_refs` | 采样时间窗、分子/分母、重复规则、缺失/异常值规则、acceptance criterion 及其来源；实测时加 `observed_at/source_round`，预测时加 `model_ref` |
| **Cost** | `cost_id`, `item`, `plan_id/version`, `value_status`, `value_nature`, `unit`, `currency`, `price_status`, `scenario_id`, `formula_ref`, `evidence_refs` | 数量已知才记录 `quantity`，单价已知才记录 `unit_price`，不能用 0 补缺；计算总价加 `derived_ref`。`price_status` 至少区分 `prospective_scenario/vendor_quote/actual_charge` |
| **Booking** | `booking_id`, `resource_id`, `plan_id/version`, `scenario_id`, `requested_window_or_duration`, `quantity`, `booking_status`, `evidence_refs` | confirmed 必须有实际外部确认引用和时间；requested 不能写成 confirmed。仅用于规划的资源窗口标记 `proposed` |
| **Validation** | `validation_id`, `target_ref`, `check_kind`, `execution_status`, `evidence_refs` | 实际执行才有 `observed_result`，同时保存 command、input/output hash、exit code、tool/version；人工复核保存真实 reviewer/time/notes；未执行保存 `reason_not_executed`，不虚构输出 |

`recheck` 最小为 `method + status`；passed/verified 状态必须对应实际复核，并保存实际 `checked_at_utc`。Claim 的 `epistemic_status` 可区分 `supported/assumption/conflicted/rejected`；它描述证据状态，不替代安全和批准状态。

## 3. Source anchors：能回到同一事实最小单元

anchor 由稳定 document/source ID 加片段坐标构成；短引文用于核对语义，不能代替坐标。数组索引按原 JSON 的实际顺序，引用之后若修改源文件必须更新 hash 并复核全部锚点。

| 来源 | 定位格式 | 质量要求 |
|---|---|---|
| PDF 正文 | `document_id#page=7` 加精确原文短引文；必要时加 `section`/`paragraph` | `page` 默认从 1 开始的物理页；论文印刷页另写 `printed_page`。抽取文本行号只能辅助，不能替代原页核对 |
| PDF 表格 | `document_id#page=7,table=2,row=...,column=...` | row/column 优先使用原表头路径或稳定标签；保留被引 cell 及决定含义的行列表头、单位和脚注，不能只摘一个数字 |
| PDF 图/方程 | `document_id#page=7,figure=...,panel=...` 或 `equation=...` | 图像/OCR 数字须视觉复核；坐标轴、误差线、log scale、上下标和条件不能被省略 |
| JSON | `/input/case.json#/plans/0/domain/lower`，可同时附 `/input/case.json#L20-L25` | 使用 JSON Pointer 转义 `~0/~1`；line span 为 1-based 的原始 UTF-8 文本行。逻辑含义以 Pointer 为主，源 hash 保证版本 |
| Markdown/文本 | `document_id#L10-L18` 加 section 标题和短引文 | 只引用决定该 claim 的最小连续行段；跨段条件不能被切断 |
| 表格文件 | `document_id#row=...,column=...` 加原表头 | 固定使用的 sheet、行标识和原始列名；排序后的临时行号不能充当稳定锚点 |

Source 的 `path_or_url` 与 Evidence 的 `locator` 是不同层级：前者识别整个来源，后者进入具体 page/cell/JSON Pointer。只写 DOI、论文标题、URL 首页或“见附件”不满足 Claim 的证据定位。

## 4. historical / planned / computed / prediction / measured

`value_nature` 说明值的产生方式，`temporal_scope` 说明时间；两者不能混用。

| 类别 | 必须如何证明 | 禁止的推断 |
|---|---|---|
| `historical` | 有原始历史合同、日志、材料规格或论文事实及当时条件，保留实际日期/版本 | 不自动代表当前设备/材料仍相同，也不自动代表目标体系可复现 |
| `planned` | 有本次计划、prospective scenario 或明确 proposed 设定 | 不是已执行、已采购、已预订、已批准，也不是供应商承诺 |
| `computed` | 已知 operands、单位、来源支持的 formula、精确计算命令/derived ID 和结果 | 算术精确不等于公式适用于该体系，更不等于实测结果 |
| `prediction` | 有显式模型、训练/来源范围、输入和不确定度/适用边界 | 无真实 observations 不得伪称数据拟合/BO 更新；预测不得标 measured |
| `measured` | 有真实观测记录、方法、时间、批次/对象和原始/派生 response 关系 | 论文中的历史 measured value 不是本方案的 future response；未执行实验保持 unknown |

历史实测可写 `value_nature=measured, temporal_scope=historical`，并引用真实原记录。由显式给定模型算出的 future response 可以保持模型输出为 `prediction/computed`，但“未来将测得值”字段必须仍为 `null`（若合同允许）和 `value_status=unmeasured`。没有 observations 时，优化只涵盖输入明确给出的模型/成本，不能拟合经验响应。

## 5. 精确单位换算与摄氏温度

所有换算先进入带 evidence_refs 的 conversion ledger：`original_value/unit`, `target_unit`, `exact_multiplier_or_relation`, `rounding_rule`, `script_command`, `converted_value`, `evidence_refs`。模型提供映射，脚本用精确有理数计算；`optimize.py` 本身不换算单位，也不会自动理解 `degC` 与 `K`。

以下只是单位定义，不是任何案例参数。SI 词头、非 SI 时间单位及摄氏温度定义以 [BIPM SI Brochure](https://www.bipm.org/en/publications/si-brochure) 为权威方法来源。

| 量 | 精确关系 | 处理规则 |
|---|---|---|
| 体积 | `1 µL = 10^-3 mL`；`1 mL = 10^-3 L` | 保留原值与目标值，区分大小写和 `uL/µL` 输入别名，统一后只用一个 canonical unit |
| 质量 | `1 mg = 10^-3 g` | 不在优化器内混合 `mg` 与 `g`；配方中的质量分数不能因单位相同直接当质量 |
| 时间 | `1 min = 60 s`；`1 h = 60 min` | exposure、运行时长、排队等待是不同实体，换算不合并其语义 |
| 比例 | `1 percent = 10^-2 fraction` | 只有 Response/Parameter 定义确认是同一比例时才转换；明确输出用 `%` 还是无量纲 fraction |
| 绝对摄氏温度 | `T/K = t/°C + 273.15`，等价为精确有理数偏移 | 原子和字段标 `absolute_degC` 或 `K`；偏移先于任何比较/优化并保留锚点 |
| 摄氏温差 | `ΔT` 的 1 K 与 `Δt` 的 1 °C 间隔数值相等 | 使用 `delta_degC`/`delta_K` 标识；**不得加 273.15**，不得与 absolute temperature 混作同一操作数 |

优化器的单位代数不会因 `absolute_degC - absolute_degC` 自动建立新的 `delta_degC` 语义。绝对温度相减时保留物理“温差” Claim 的来源和语义；若另需换算成 K 温差，在规范化阶段按上表显式精确转换，不能擅自覆盖 derived expression 计算出的单位。

取整也必须有来源：包装量、batch 数、run 数或计费增量只有合同明确给出时才用 `ceil/ceil_div/ceil_to_step`。保留 origin、step 和 inclusivity；不得为方便而中途四舍五入。展示精度与计算精度分开，不能用过多有效数字暗示不存在的测量精度。汇率、物价日期和税/折扣没有实际场景依据时不换算、不猜测。

## 6. 来源冲突与 transferability

### 冲突不是一个值覆盖另一个值

对同一 `(entity, definition, qualifier_set, temporal_scope)` 的冲突值建立各自 Evidence 与 Claim，并生成 conflict issue，至少记录：

- 两个原始值/单位/条件和精确 anchors；
- 来源类型、版本/日期、适用范围及是否 organizer contract；
- 受影响的 plan/version、constraint、response 和输出字段；
- 待谁确认什么，以及确认前哪些结论 blocked。

实际 case/task 的具体目标和合同优先用于界定本次任务；原始论文支持科学方法，设备手册/SDS/机构规则支持各自安全边界。这个阅读顺序不是“后出现的数字覆盖先出现的数字”，也不允许借任务目标放宽安全/伦理要求。无法判定时标 `conflicted/indeterminate`；不得平均、取较好值或仅因数值更保守就宣称冲突已解决。经权威确认后，新增 resolving Evidence 与 change record，旧记录仍保留。

### 可迁移性必须逐条件论证

从文献、历史系统或其他设备迁移参数/响应前，做 source→target 对照：

- 材料/样品、设备与量程、反应/测量原理、尺度和几何；
- 介质、浓度、环境、操作顺序和采样时间；
- response 定义、仪器、校准、重复和统计口径；
- 已覆盖参数域、外推距离、不确定性及缺失条件。

每项标 `matched/different/unknown` 并附证据。相同论文体系或相同设备系列不是充分条件；差异未被科学论证时，只能形成待验证 hypothesis/pilot proposal，不能形成已证实最优或安全结论。organizer 的 prospective resource contract 只限定规划场景，不能补足科学可迁移性。

## 7. Claim quality gate：原子断言，不论文标题背书

每个 Claim 只表达一个可判真假的原子断言，能够回答“哪个主体、在什么条件下、哪个关系由哪个最小原文片段支持”。例如“某论文支持本方案”不合格；应拆成参数定义、某个设备上限、某个 response 计算口径、某个适用范围等独立 Claim。

**通过检查的最小组合：**

1. `statement` 含且只含一个主体—关系—对象，限定条件和 polarity 完整，不把相关写成因果。
2. 每条 evidence_ref 可解析到实际 Source 和最小 anchor；quote 与 page/cell/JSON Pointer 的原值一致，表头/脚注没有反向改变含义。
3. value、unit、entity、definition、plan/version、time 和 nature 全部绑定；来源值与目标值不同实体时有显式 transferability 论证。
4. 换算和 computed value 有精确脚本轨迹；科学公式另有来源，不能由代码存在反推公式成立。
5. `recheck` 描述实际“重读该页/单元格并逐字段比对”的方法和状态；一次 recheck 不自动覆盖其他 Claim。
6. 涉及硬约束、安全阈值、接受标准或价格时，逐项独立检查，不能用整篇论文、整个合同或一个通用来源同时兜底。

最后进行反向质询：原文是否只在另一材料/设备/时间范围成立？是否报告的是均值而方案需要单次上限？是否有 unknown 候选仍可成为赢家？是否把 prospective price 写成 quote、把 planned booking 写成 confirmed、把未获批准写成 approved？任何一项不能排除，就降低 Claim 强度或阻断相应结论，而不是增加更多泛引用。
