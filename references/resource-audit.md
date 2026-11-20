# 直接资源费用审计与 canonical request 编译

`scripts/audit_resources.py` 面向**已明确列出的有限候选**及其 `candidates[].cost_items`。它将直接计费字段转成精确算术、证据定位和分类任务量，并在数据完整时生成 `scripts/optimize.py` 的 v1.0.0 canonical request。只用 Python 标准库；不联网、不安装包、不执行表达式。

它不负责科学优劣、未知响应、实际报价、采购、档期、审批或“科学上全部可行”。没有 `--caps` 时只做资源算术，尤其不会从 hard-constraint 自然语言中猜预算。

## 调用

在提交根目录运行：

```bash
python3 scripts/audit_resources.py \
  --case /input/case.json \
  --output /app/output/resource-audit.json
```

带显式政策并生成优化请求：

```bash
python3 scripts/audit_resources.py \
  --case /input/case.json \
  --output /app/output/resource-audit.json \
  --caps /app/work/resource-caps.json \
  --emit-request /app/work/optimization-request.json

python3 scripts/optimize.py \
  --input /app/work/optimization-request.json \
  --output /app/work/optimization-report.json
```

`python3 scripts/audit_resources.py --help` 在 stdout 输出合法 JSON。所有输出文件使用同目录临时文件和原子替换。可正常写出的 partial audit 返回 0；无法读取/解析输入或写文件等运行错误返回 2。

## 支持的直接费用字段

每个 plan 原样复制 `plan_id`、`version` 和 `/candidates/i` 定位。每条成本保留 `/candidates/i/cost_items/j`，取 `item_id`，没有时取 `item`；两者都空则 unresolved。

仅支持下面两种互斥的三操作数格式：

- photo 型：`quantity_per_event * event_count * unit_price`。
- climate 型：`quantity * charge_events * unit_price`。

报告把两种输入都规范到 `quantity_per_event`、`event_count`，同时在 `source_operands` 保留源字段名、原始数字 token 和各自 JSON Pointer。`formula` 是本次派生算术；源 `formula` 仅作为 `source_formula` 留证，不用于求值。

支持的数字是 JSON number 或十进制/科学计数字符串。quantity 可带空格分隔的量纲说明，例如 `576 vials`，其中只取开头明确的数值并保留原文定位；`2*n`、引用名、缺失值、复杂公式均不猜、不算。

**字段同名不等于科学计费语义相同。** 如果 quantity 已经是总事件数，或与 charge_events 是同一计数而非真正两个独立乘法因子，就不能强行三者连乘。源 `amount_USD` 在这里充当反证：一致不等于科学正确，不一致时必须标 unresolved 并回查原 formula、waiting/recurrence 规则与单位；不要依据预期总价额或一个特有的 case 改公式让行自动通过。

`unit_price` 支持：

- `24`、`24.00` 或对应 JSON number；
- `14 USD/vial`：数值 `14`、显式币种 `USD`、计费实体 `vial`。

无显式币种时，依次采用明确的 item `currency`、price-book entry `currency`、case `price_book.currency`。也支持 `planning_contract.price_book.currency`。无法独立确定币种不会写 `USD`/`EUR`，更不会仅因字段叫 `amount_USD` 就推断币种。

缺少可解析 `unit_price` 时，若有非空 `price_id`，只查找：

```text
case.price_book.prices[price_id]
case.planning_contract.price_book.prices[price_id]
```

entry 可以是数字/数字字符串，或 `{unit_price, currency?, charging_unit?}`。找不到、结构不符或仍非数字均 unsupported；绝不自动填 0、1 或某个默认价。

`charging_unit` 或 price 中 `/` 后的实体用于 `charged_semantic`，是**计费的量纲定义，不是科学测量结论**。canonical 的 `unit_values` 将它规范为合法 unit atom，与价格单位相消，得到单位恰为币种的 `computed_cost`。原始数量值只参与乘法，不与机时、金额等相加。不做单复数推断、汇率或物理单位换算。

### 源金额交叉核验

若有可解析的 `amount_USD`，它与派生成本都以 Fraction 重新比较：

- `source_amount_check.status = match`：一致；
- `mismatch`：该行 unresolved，保留 source amount 和 derived cost，不能挑一个覆盖另一个；
- 未独立取得 USD 币种时不比较，也不由字段名补币种。

金额不一致、混合币种或任一成本行未解决时，`--emit-request` 为 withheld。真实源数据若把总量字段和事件数字段重复列出，三操作数结果可能与源金额冲突；工具不会擅自改成两操作数来“修复”它。

## 政策文件：只能显式书写

最小结构：

```json
{
  "constraints": [
    {
      "id": "HC1",
      "value": "3200.00",
      "unit": "EUR",
      "currency": "EUR",
      "sense": "le",
      "source": {
        "locator": "/hard_constraints/0",
        "quote": "planned committed total ≤ 3200.00 EUR"
      }
    }
  ]
}
```

这是**结构示例**，不是自动读取预算的指令。Agent 必须先读实际 case，再从相关 hard constraint 复制稳定 id、准确 bound、币种、方向、定位和短引文。上限用 `le`，下限用 `ge`；本工具要求 `unit == currency`，且 `value` 只能是无量纲数字 token，单位另列。

`source.locator` 可为实际 JSON Pointer、`case.json#/...` 或可解析的 hard-constraint id。空 id、空 quote、不能解析的 case 定位都会产生 diagnostic 并阻止 request。可选 `case_ref` 同样必须非空且可解析。

默认将 cap 应用于全部 plans。可选 scope：

```json
{"plans": ["P1", {"plan_id": "P2", "version": "v1.0"}]}
```

或单个 `{"plan_id":"P1","version":"v1.0"}`。匹配不到 plan、cap 与计划币种不一致、成本尚未完整解决，都不会被解释为 feasible。

默认 canonical 决策是：

```json
{"type":"feasibility"}
```

它只表示数值成本约束的 pass/fail/unknown。只有 policy 顶层明确加入以下对象才生成成本单目标：

```json
{"type":"single","direction":"minimize"}
```

此时 request 的 objective 是 `planning_cost`，并带 `planningcost_target` 与实际派生总额的逐 plan reconciliation。即使 optimizer 返回 `optimal`，含义也仅为这个显式成本目标的数学最优，不是科学赢家。其他 decision/direction 不在本编译器范围内；科学目标应由 Agent 按完整科学合同另行建模。

## 报告结构与样态

顶层是小型 plain-JSON `resource-audit-1.0.0`，没有单独 schema 文件：

- `coverage`：全部成本行及 pass/unresolved 计数；
- `plans[]`：身份、定位、成本行、按币种分开的 listed-direct totals、staff/task 汇总；
- `policy`：`not_provided` 或显式约束及逐 plan 结果；
- `canonical_request`：`not_requested`、`written` 或 `withheld`；
- `diagnostics`：缺失、unsupported、混币、冲突和无效 case ref 的定位；
- `scope.scientific_decision`：始终声明本工具不评科学赢家。

以下仅为**合成结构示例**，不是案例答案或报价：

```json
{
  "item_id": "example_consumable",
  "quantity_per_event": "2",
  "event_count": "3",
  "unit_price": "14",
  "computed_cost": "84",
  "currency": "USD",
  "case_json_pointer": "/candidates/0/cost_items/0",
  "operator": "mul",
  "formula": "quantity * charge_events * unit_price",
  "charged_semantic": "vial",
  "unit_values": {
    "quantity_per_event": "vial",
    "event_count": "",
    "unit_price": "USD*vial^-1",
    "computed_cost": "USD"
  },
  "status": "pass"
}
```

所有派生数字以字符串输出，非有限小数以 `n/d` 表示；canonical 输入中的数字则写回真正 JSON number，不能改成 numeric string。解析时直接用源 JSON number token 构造 `Fraction`，不会先经过 binary float，不使用 `eval`/`exec`。

`listed_direct_cost_totals` 只在同一币种内相加。完整时它包括所有已列直接行，包括带明确 `paid`、`excluded`、`contingency` 标记的行；原 flags 留在行内供主 Agent 进一步解释。若只覆盖部分行，`complete_plan_listed_direct_total=false`，此时只是计划级一致性核对前的 partial subtotal，**不是 plan total，更不是所有资源的 grand total**。不同币种绝不求和或换算；混币计划的受影响行也逐个标为 `unresolved`。

## 任务和人员量

对 `tasks[]` 中形如 `<role>_h`、`<role>_hours`、`<role>_min` 的明确数字字段，按 `(field, unit)` 分组：

- `chemist_h` 与其他 role 分列；
- `duration_h` 是任务时长求和，不是人员小时、机器小时、elapsed time 或 critical path；
- 不做小时/分钟换算，不把不同 role/unit 加总；
- 每组有稳定 group id、role、unit、task ids 和实际字段 pointers。

有缺数时只给 `known` 值的 partial subtotal、known/unresolved 计数；全缺时 `computed_sum=null`，不是 0。没有 `tasks` 就是 `not_provided`，不虚构依赖、时序或关键路径。

## Canonical 的构造与阻断条件

有效输出包含 v1.0.0 的全部必需字段。每条成本有独立 known inputs 和 `mul` derived，随后以 `add` 形成该 plan 的精确 line-cost sum。`charge_factor` 只是固定每 plan 分支的技术 integer selector，不是物理电荷、科学因子或未知等级。Evidence 均有实际 case/policy locator、非空 quote 和 type，引用完整性再由 optimizer 校验。

以下任何情况都保留 affected rows 和定位，但不生成完整 canonical request：

- plan/version/item id 无效，成本数组或必要操作数缺失；
- symbolic expression、无法支持的 price/book 结构、无明确 price 或 currency；
- source amount mismatch、混币、cap 无效或 cap 币种不符；
- 任一 plan 总成本不完整，或选择目标缺少共同已解决币种。

Withheld 时不会创建或改写 `--emit-request` 路径，防止把 partial audit 冒充 request；调用者也不能复用旧 request 文件。无 `--emit-request` 时明确标 `not_requested`，无 `--caps` 时明确标 `not_provided`。

## 不在范围内

通用表达式求值、任意 price-book schema、profile-indexed `price_items`、科学响应/等级、日程和依赖推断、税务/折扣/汇率、单位换算、真实询价/预订/主权审批、未来观测，以及由成本结果推出科学 winner。遇到这些输入应保留 unresolved/unsupported，由主 Agent 依据原 case 补齐或收窄结论。
