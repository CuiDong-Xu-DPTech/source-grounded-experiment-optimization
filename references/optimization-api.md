# 有限参数优化 API（canonical request v1.0.0）

## 用途与边界

`scripts/optimize.py` 是供调用 Agent 使用的、非交互的有限域穷举器。原始案例结构可以先由 Agent 规范化成本文请求；本工具**不直接猜测原始案例 schema**。它只做来源约束下的公式求值、硬约束过滤和用户指定序的搜索，不替用户选择优化方向，不做等权加权或隐式标量化。

- 仅使用 Python 标准库，无网络、随机数、时钟或 Python/hash 顺序依赖。
- 不使用 `eval` 或 `exec`；expression 只能是下述白名单 AST。
- 数字按 JSON 源 token 构造 `Fraction(str(token))`，不会先经过二进制浮点。
- 不换算单位。单位换算、公式来源和取整规则由规范化输入负责，并应有 evidence 支撑。
- 只枚举声明的 plans 与参数域，不证明声明域之外的科学最优性。
- 请求结构见 `schemas/optimization_request.schema.json`。Schema 做结构检查；重复逻辑 ID、跨引用、类型、单位、DAG、有效网格等由 CLI 进一步检查。不需要安装 `jsonschema` 包。

## 调用

从提交根目录运行：

```bash
python3 scripts/optimize.py --input request.json --output report.json
python3 scripts/optimize.py --input request.json --output report.json --max-candidates 1000
```

`--max-candidates` 是所有 plan-version 合计的评估上限，接受规范写法的非负整数（包括 `0`）。输出使用同一目录临时文件、`fsync` 和 `os.replace` 原子替换。有效请求，包括 `partial`、`infeasible` 和 `unresolved`，退出码为 `0`；科学输入/JSON/引用校验失败为 `invalid_request`，退出码为 `2`，仍写出结构化报告。参数解析错误和无法写输出也返回 `2`。

## 顶层请求

所有字段都是必需的，版本固定为 `1.0.0`：

| 字段 | 内容 |
|---|---|
| `schema_version` | 字符串 `"1.0.0"`。 |
| `case_id` | 非空案例标识，原样进入报告。 |
| `evidence` | map：`evidence_id -> {locator, quote, type}`，三个值均为非空字符串。`locator` 是文献页码、合同条款等稳定定位，不要求本地文件存在。 |
| `parameters` | 非空数组，定义各 plan 共享的有限参数域。 |
| `plans` | 非空数组；每个元素至少有 `plan_id`、`version`、`evidence_refs`，可含 `assignments` 和 `domain_overrides`。 |
| `input_values` | map：参数外的合同值、观测值、缺失响应等。 |
| `derived_values` | 可由参数、输入和其他 derived 组成的 DAG。 |
| `objectives` | 数值 expression 及声明单位。 |
| `constraints` | 布尔 expression；每条都是硬约束，逐候选保留 `pass/fail/unknown`。 |
| `decision` | `feasibility`、`single`、`lexicographic` 或 `pareto`。 |

任何 `evidence_refs` 都必须引用顶层 `evidence` 中实际存在的 key；空数组允许，但是否足够支撑科学声明由调用方负责。重复引用是输入错误。JSON 重复对象 key 也会拒绝，包括 map 中重复 evidence/input ID。

### Evidence

```json
{
  "evidence-id": {
    "locator": "stable-document-id#page=2,table=1",
    "quote": "原文短引文",
    "type": "paper"
  }
}
```

`type` 和 `nature` 是调用方定义的非空来源标签，不会凭标签推断事实。候选报告聚合 plan、参数、输入、derived、objective 和 constraint 的 evidence；各计算项与约束也保留自己的 refs。

### Parameters 与有限域

参数结构：

```json
{
  "id": "dose",
  "type": "number",
  "unit": "mg",
  "domain": {"lower": 0, "upper": 0.2, "step": 0.1, "inclusivity": "closed"},
  "evidence_refs": ["evidence-id"]
}
```

`type` 为 `number`、`integer`、`string` 或 `boolean`。数值域有两种互斥形式：

- `{"allowed_values": [v1, v2, ...]}`：至少一个值，类型匹配、无重复。
- `{"lower": x, "upper": y, "step": s, "inclusivity": i}`：只用于数值，`s > 0`，枚举 `lower + k*step`；integer 域要求端点和 step 都是整数。

边界用语从 lower 端视角描述：`closed` 为 `[lower, upper]`，`open` 为 `(lower, upper)`，`left_closed` 为 `[lower, upper)`，`right_closed` 为 `(lower, upper]`。端点是否恰好在 step 网格上也按 `Fraction` 判断；空网格、反向边界或步长不合法是科学输入错误，而不是“无可行解”。字符串/布尔 categorical 只能使用 `allowed_values`。

`unit` 可省略；省略与 `""` 表示无量纲。数字之外的参数不能有非空单位。

### Plans 与版本

```json
{
  "plan_id": "plan-a",
  "version": "contract-v1",
  "assignments": {"mode": "fixed-mode"},
  "domain_overrides": {
    "dose": {"allowed_values": [0.1, 0.2]}
  },
  "evidence_refs": ["evidence-id"]
}
```

未固定参数按 override 后的 plan-specific 有限域完整枚举；未固定且未 override 的参数使用全局域。固定值必须属于 override 后的有效域。省略 `assignments` / `domain_overrides` 等价于空对象。

`(plan_id, version)` 是唯一身份。**只有 version 不同也绝不去重**；相同参数赋值可在多个版本下成为不同候选。重复 plan-version 是输入错误。

**耦合域不要伪笛卡尔展开。** 若合同只允许 plan×profile 的若干联合组合，就为每个组合建立明确的 evaluation plan，以其 assignments 固定相关档位、资源公式和实验口径；联合 `plan_id/version/profile_id` 映射保留在证据账本与原结果中。不要为了复用一个上游 plan 的三维参数网格而生成大量之后还须全部排除的组合。自由参数仍可使用全局或 override 有限域；未给的档位、插值或“混合 profile”不能生成候选。optimizer 的 distribute plan 是搜索身份，不得抹掉原案例实际 plan/version 的对账记录。

### Input values，known / unknown

```json
{
  "observed-response": {
    "status": "unknown",
    "type": "number",
    "unit": "percent",
    "nature": "missing_response",
    "evidence_refs": ["evidence-id"]
  }
}
```

`status: "known"` 必须有匹配 `type` 的 `value`。`status: "unknown"` **不得带 `value`**，不能用 `0`、`1`、`null`、分类占位符等代替。`unit` 在此 map 中是显式必需字段，无量纲写 `""`。输入 ID、参数 ID、derived ID 和 objective ID 共用同一引用命名空间，不允许重复。

## Expression AST

节点恰为以下一种：

- 引用：`{"ref": "value-id"}`。
- 常量：`{"const": 0.1}`，值只能是有限 JSON number/string/boolean，不接受 null；数值常量可写成 `{"const": 50, "unit": "percent"}` 参与同单位计算或比较。
- 运算：`{"op": "add", "args": [...]}`。
- 域成员判断：`{"op": "in_domain", "value": {...}, "domain": {...}}`。

支持的 `op` 与参数数：

| 运算 | 参数与含义 |
|---|---|
| `add`, `mul`, `min`, `max` | 2 个或更多数值。 |
| `sub`, `div` | 恰好 2 个数值；除零产生 unknown，不填零。 |
| `floor`, `ceil` | 1 个数值，精确取整为 integer，保留单位。 |
| `ceil_div` | 2 个数值，`ceil(a/b)`，integer 结果，单位按商组合；除零为 unknown。 |
| `ceil_to_step` | `value, step[, origin]`； smallest `origin + k*step >= value`，缺省 origin 为精确零；step 必须为正，否则 unknown。 |
| `eq`, `ne`, `lt`, `le`, `gt`, `ge` | 恰好 2 个参数。排序比较仅用于数值；string/boolean 支持 `eq/ne`。 |
| `in_domain` | 对 expression 值做 allowed-values 或精确 grid 成员判断；string categorical 由此支持成员判断。 |
| `all`, `any` | 1 个或更多布尔值，三值 Kleene 逻辑。 |
| `not` | 1 个布尔值；`not unknown = unknown`。 |
| `implies` | 2 个布尔值，等价于 `not antecedent OR consequent` 的三值逻辑。 |

`derived_values` 的 unit 是 expression 计算并化简后的单位；省略表示 `""`。objective 同样如此，且必须静态为数值。静态检查会拒绝非法运算符/参数数、非法 ref、derived 自环或一般环、类型错误。derived 只可读 parameter、input、其他 derived；objective 和 constraint 可读这三类，但不能引用其他 objective，避免决策 DAG 歧义。

数值 `add/sub/min/max` 及所有二元比较要求单位一致。`mul/div/ceil_div` 做精确单位幂次代数。单位表达式区分大小写，支持 atom、`*`、`/`、括号及整数指数，例如 `USD*mg^-1`、`mL`、`kg*m*s^-2`；`""` 或 `"1"` 为无量纲。不会把 `mg` 换成 `g`，也不会把不同温标视为等同。`ceil_to_step` 各操作数必须同单位。

缺失输入、未知的上游计算、除零及非法动态取整步长传播 unknown，并记录 issue；不会转成通过、失败、0 或 1。三值约束采用保守规则：任一硬约束 fail 即 infeasible；无 fail 但存在 unknown 即 feasibility unknown；全部 pass 才 feasible。

## Decision 方向

方向必须逐一由用户/调用方明确给出，代码和 schema 没有默认方向：

```json
{"type": "single", "objective_id": "cost", "direction": "minimize"}
```

```json
{
  "type": "lexicographic",
  "objectives": [
    {"id": "safety-margin", "direction": "maximize"},
    {"id": "cost", "direction": "minimize"}
  ]
}
```

```json
{
  "type": "pareto",
  "objectives": [
    {"id": "response", "direction": "maximize"},
    {"id": "cost", "direction": "minimize"}
  ]
}
```

同一个 decision 中 objective ID 不可重复。lexicographic 的数组顺序就是优先级；不计算加权总分。Pareto 使用“每个目标不劣且至少一个更优”的支配定义；完全并列互不支配，全部保留。`pareto_candidate_ids` 是已知值、确定可行候选中的前沿，不虚构唯一 winner。

未知 feasibility 或未知决策 objective 的候选进入 `unranked_candidate_ids`，不参加硬排名，也不参与支配剪枝。其他已评估的 derived/objective 即使不参与 decision，也仍保留在账本中；它们是否阻断 exact optimal 取决于是否实际用于 decision。遇到 unknown 时，报告只给已知候选之间的结果，不能把它解释为全局确定结果。

## 输出与最优性声明

顶层字段：

- `case_id`
- `status`
- `search`
- `ranked_candidate_ids`
- `pareto_candidate_ids`
- `unranked_candidate_ids`
- `best_candidate_ids`
- `decision_summary`
- `candidates`
- `diagnostics`

`candidates` 保留**所有已评估候选**，包括 fail、unknown、dominated 和非赢家；每项含：

- `candidate_id`、`plan_id`、`plan_version`；
- 每个参数的 type/value/unit/fixed/evidence_refs；
- 每个 derived 和 objective 的 `known/unknown`、精确 value、type、unit、evidence_refs、issues；
- 每条 constraint 的 `pass/fail/unknown`、布尔值或 null、refs、issues；
- `feasibility`、聚合 `evidence_refs`、`evaluation_issues`、`elimination_reasons`、`decision_status`。

输出数字统一编码为字符串以避免精度丢失：有限小数写 `"0.3"`，整数写 `"2"`，无法有限表示的有理数写 `"1/3"`。string/boolean 保持 JSON 原类型；unknown 的 value 是 null，并由 `status` 和 issues 区分。输入 JSON 中不能用这些数字字符串代替数值 literal。

`search` 含 `total`、`evaluated`、`exhaustive`、`truncated`、`stop_reason`、`max_candidates`、`coverage`、`plan_count` 和固定枚举顺序说明。`coverage.fraction` 也是精确数字字符串。预算截断时 `stop_reason = "candidate_cap_exceeded"`；完整枚举为 `enumeration_complete`；输入失败为 `validation_failed`。未评估候选没有伪造账本，可由 total/coverage 判断覆盖范围。

状态语义：

| `status` | 含义 |
|---|---|
| `optimal` | single/lexicographic 已完整枚举，没有未知潜在赢家，且选择序明确；`best_candidate_ids` 保留全部最优并列，`exact_optimal = true`。 |
| `complete` | feasibility 已完整解释，或 Pareto 已知前沿已完整枚举且无未决候选；不表示唯一 winner。 |
| `infeasible` | 全枚举后没有任何 feasible 或 feasibility-unknown 候选；这是有效优化结果，不是 schema/科学输入错误。 |
| `unresolved` | 枚举虽完整，但缺失响应、unknown 硬约束或未知决策值仍可能改变决定。 |
| `partial` | 达到 candidate cap，覆盖不完整；排名和 Pareto 集只能视为当前已评估范围的临时结果。 |
| `invalid_request` | JSON、类型、重复 ID、失效 refs、单位、网格、DAG、固定值或其他科学输入错误。 |

`decision_summary.pareto_has_unique_winner` 恒为 false。非 `optimal` 状态的 `best_candidate_ids` 为空，以免把 provisional 排名冒充 exact optimal。

## 稳定顺序与复跑

参数按 ID 排序，plan 按 `(plan_id, version)` 排序，allowed values 按精确数值或规范 categorical 顺序排序；grid 由 lower 锚定递增。全局 candidate cap 按此顺序消耗。排名先按用户给定的精确目标序，再按 `candidate_id`；候选 ID 是 plan/version 和排序后参数赋值的紧凑 JSON 编码，不使用随机 hash。候选并列不被折叠。同一有效请求与 cap 产生字节级稳定 JSON；输出路径、运行时间、主机时间和 Python hash seed 不进入报告。

## EXAMPLE-ONLY：完全虚构的最小接口示例

以下仅演示格式，`EXAMPLE-ONLY`、来源、价格和参数都不是正式案例、真实报价或科研答案。

```json
{
  "schema_version": "1.0.0",
  "case_id": "EXAMPLE-ONLY",
  "evidence": {
    "fake-contract": {
      "locator": "EXAMPLE-ONLY://contract#table-1",
      "quote": "Synthetic grid and synthetic unit price for interface demonstration only.",
      "type": "synthetic_contract"
    }
  },
  "parameters": [
    {
      "id": "dose",
      "type": "number",
      "unit": "mg",
      "domain": {"lower": 0, "upper": 0.2, "step": 0.1, "inclusivity": "closed"},
      "evidence_refs": ["fake-contract"]
    }
  ],
  "plans": [
    {
      "plan_id": "EXAMPLE-PLAN",
      "version": "v1",
      "evidence_refs": ["fake-contract"]
    }
  ],
  "input_values": {
    "price": {
      "status": "known",
      "type": "number",
      "unit": "USD*mg^-1",
      "nature": "synthetic_price",
      "value": 0.1,
      "evidence_refs": ["fake-contract"]
    }
  },
  "derived_values": [
    {
      "id": "computed-cost",
      "unit": "USD",
      "expression": {"op": "mul", "args": [{"ref": "price"}, {"ref": "dose"}]},
      "evidence_refs": ["fake-contract"]
    }
  ],
  "objectives": [
    {
      "id": "cost",
      "unit": "USD",
      "expression": {"ref": "computed-cost"},
      "evidence_refs": ["fake-contract"]
    }
  ],
  "constraints": [],
  "decision": {"type": "single", "objective_id": "cost", "direction": "minimize"}
}
```

调用后，其**顶层示意摘录**（不是完整报告）应体现三个精确网格点与零成本最优；候选完整 derived/constraint/evidence 账本由实际 CLI 写出：

```json
{
  "case_id": "EXAMPLE-ONLY",
  "status": "optimal",
  "search": {"total": 3, "evaluated": 3, "exhaustive": true},
  "best_candidate_ids": ["candidate:[\"EXAMPLE-PLAN\",\"v1\",[[\"dose\",\"0\"]]]"]
}
```

不得把该虚构示例当作真实来源、设备确认、实验批准或正式案例输出。
