# Decision audit：候选版本责任链的机械门

`audit_decision.py` 只核验 ledger 中**显式写出**的身份、来源指针、canonical 参数对象、变更、算术比较、QA 状态和选择状态。它不是科学准确性证明、论文认可、设备可用性证明、安全审批或执行许可。

一个 Agent 仍必须回到原始 case/PDF 核对事实，独立复算价格、公式、人员和日程。工具不会理解“拆分后还是原计划”这类句子，也不会替 Agent 发明版本号、单元数、日期、阈值、价格、人员或 slots。

## 1. 调用、依赖和退出状态

只使用 Python 3 标准库，无网络、外部 API、评测器或 native 依赖。

```bash
python3 audit_decision.py \
  --case /input/case.json \
  --ledger /app/work/decision-ledger.json \
  --output /app/output/decision-validation.json
```

`--help` 显示同一 CLI 的帮助。成功写出 JSON 报告时不打印推测性结论到 stdout。

| 情形 | `final_validation.outcome` | 退出码 | `can_finalize` |
|---|---|---:|---:|
| 完整的原版规划选择，或有依据的行政性拒绝记录 | `complete` | 0 | `true` |
| 有条件规划、修订提案或未定选择，且无结构错误/已知违规选择 | `conditional` | 0 | `false` |
| 身份、来源、schema 一致性问题，或 `invalid_selection` | `invalid` | 1 | `false` |
| 文件、UTF-8、严格 JSON 或顶层输入错误 | `invalid`，且 `processing_status=error` | 2 | `false` |

输入错误会产生明确的 error/partial 信息，不故意暴露 `ValueError` traceback。无法写 output 时返回 2 并向 stderr 报告。

报告不含运行时生成的时间戳。相同 case、ledger 字节及脚本产生确定性的、逐字节一致的 output；output 路径不进入报告。

## 2. 严格 JSON 与 case 绑定

两个输入都必须是 UTF-8 JSON object：

- 任意嵌套层级的重复 object key 都拒绝。
- `NaN`、`Infinity`、`-Infinity` 拒绝；会转成非有限 float 的数（例如 `1e999`）也拒绝。
- ledger 的 `case_id` 必须等于 case 的 `case_id`。
- `source_case_sha256` 是**所提供 case 文件原始字节**的小写 SHA256，不是描述文字、重排版 JSON 或另一个 case 的哈希。

```python
import hashlib
from pathlib import Path
case_sha256 = hashlib.sha256(Path("case.json").read_bytes()).hexdigest()
```

`source_status.status=verified` 仅指 case ID 和原始字节哈希绑定成功，不表示来源中的科学陈述为真。

## 3. 顶层 ledger schema

必需字段如下；附加的说明性字段不会替代必需证明。

| 字段 | 类型与规则 |
|---|---|
| `case_id` | 非空字符串，等于输入 case ID |
| `source_case_sha256` | 64 位小写 hex，等于 case 原始字节 SHA256 |
| `planning_scope` | 非空字符串，明确规划边界；不能当执行批准 |
| `execution_authorized` | 必须是 boolean；工具绝不自动设为 `true` |
| `execution_approval_evidence` | 可选 array；任一个授权 flag 为 `true` 时必须有至少一个有效记录 |
| `candidates` | array，元素见下节 |
| `selection` | object，见“选择与最终状态” |

每条执行批准证据至少是：

```json
{
  "source_ref": {"locator": "/actual/approval/record", "quote": "actual approval excerpt"}
}
```

记录可以额外保留审批人、范围、依赖、撤销/rollback 信息等，但工具不凭依赖名称推断批准。若没有真实的源记录，应保持两个 `execution_authorized` 都为 `false`，而不是制造 approval object。

## 4. 候选身份：稳定路径优先

每个 `candidates[i]` 支持并约束以下字段：

| 字段 | 规则 |
|---|---|
| `candidate_id` | ledger 内全局唯一、非空；`selection.candidate_id` 按此 ID 精确解析 |
| `source_case_path` | 必需的 `/candidates/N`，指向输入 case 的实际源候选对象 |
| `source_plan_id` | 必须等于该指针处源候选的 `plan_id` |
| `source_version` | 原版必须等于源 `version`；修订必须是新版本 |
| `revision_kind` | `original` 或 `revision` |
| `parent_plan_ref` | 原版禁止出现非 null 值；修订必需且必须等于 `source_case_path` |
| `parameter_case_path` | 指向父版实际参数对象；有 canonical 对象或参数哈希时必需 |
| `source_parameters_sha256` | 原版可选，修订必需；必须能由 `canonical_parameters` 重算 |
| `canonical_parameters` | 修订必需的实际修订后 JSON object；不是描述 string |
| `changes` | array；原版必须为空，修订必须非空且完整覆盖实际 diff |
| `checks` | array；元素见检查项规范 |

同一 `(source_case_path, source_version)` 不得被多个 ledger candidate 重复声称。全局 `candidate_id` 也不得重复。不同 case 候选即使碰巧拥有相同 `plan_id/version`，仍由 `/candidates/N` 区分；不会按唯一性不明的自然属性猜测候选。

原版有 parent、原版有 changes、原版版本变化、修订版本不变、修订 parent 不可解析、修订 body/hash 不变，均为 blocking identity error。账本中更少的哈希信息不会被描述文字补足。

当 `parameter_case_path == source_case_path` 且父对象含 `plan_id` 或 `version` 时，`canonical_parameters` 内嵌的这些字段还必须分别与 ledger 的 `source_plan_id`、`source_version` 相等，防止两个身份层互相矛盾。

原版未提供参数哈希是允许的，但报告的 `parameter_hash_status=not_provided`：这不是原始参数完整性证明。提供对象而未声明哈希时状态为 `computed_not_declared`。

## 5. Canonical 参数哈希与变更证明

本工具定义的 canonical JSON 为：

```python
import hashlib
import json

def canonical_sha256(value):
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()
```

这是本项目明确规定的序列化，不声称兼容其他“canonical JSON”标准。`1` 与 `1.0` 不必具有相同 canonical bytes。

对修订执行以下交叉核验：

1. 由 `parameter_case_path` 在 **case** 中解析父对象，计算父哈希。
2. 由 ledger 实际的 `canonical_parameters` 计算新哈希，要求与 `source_parameters_sha256` 相同且不同于父哈希。
3. 每个 change 必须含 `path/before/after/reason/evidence_refs`。
4. `path` 是相对父对象和修订对象的同一非空 JSON Pointer；`before`、`after` 必须与两端实际值精确一致。
5. 父对象与修订对象的所有递归差异，都必须被某个 change 路径本身或其祖先路径覆盖；不得只记录有利变化、遗漏资源/日程变化。
6. 新增或删除单个值不能靠不可解析的 pointer 隐去；应替换一个两端都存在的包含对象，并在 change 中给出完整的 `before/after`。

因此，schedule、resources、formula、staffing、cost 的任何实际变化都必须放进修订对象并留下变更证明。将改动对象仍标作 `original` 不合法。工具不根据路径名称判断改动是否“重大”；**任何改动都不会因被叫作 minor 而获得原版豁免**。

`parameter_case_path` 必须覆盖所有声称变化所在的共同父对象。若只提交 `finiteparams`，它不能证明 `schedule/resources` 的同步更新；需要跨域传播时通常应锚定包含这些部分的更高层对象。

## 6. 来源引用

每个 check 必需 `source_ref={locator,quote}`；change 必需非空 `evidence_refs` array，check 也可用 `evidence_refs` 补充来源。每个引用接受两类 locator：

1. **case JSON Pointer**：以 `/` 开头，必须在本次输入 case 中实际解析成功。数组下标不能越界，不能用其他 case 的 ID 或路径。
2. **安全 PDF 物理页定位**：形如 `source.pdf#physical_page=3` 或 `source.pdf#physical_page=3-4`。路径必须相对、无 `..`、且精确出现在 case 的 `source_materials[].path`；页码从 1 开始，结束页不得小于开始页。源材料提供数字字符串 `physical_pages` 时还检查范围上限。

`quote` 必须非空。工具仅验证其存在及 locator 结构/解析，**不会擅自判定 quote 是否忠实支持事实**。报告把每条标为 `presence_only_manual_source_review_required`。错引、断章取义、把 pending 写成 approved、把父版数值当修订值，仍须人工回源复核；工具不会为了通过而猜引文。

修订后的数值可以再提供 `actual_proof_path`：它是相对该候选 `canonical_parameters` 的 JSON Pointer，必须可解析，解析值必须与 `actual` 精确 canonical 相等。`source_ref` 用来指向限制/规则来源，`actual_proof_path` 用来证明修订实际值，两者不可冒充。

## 7. Checks、精确比较和单位

每项 check 的必需字段：

- `check_id`：同一 candidate 内唯一。不同 candidate 可以使用相同 ID，owner scope 不混淆。
- `kind`：非空字符串；`constraint`、`self_qa`、`control` 有特殊规则，其他 kind 仍按 blocking 规则处理。
- `status`：`pass`、`fail`、`unknown`。
- `severity`：`blocking` 或 `advisory`；**只有 control 可为 advisory**，constraint/self_qa 不得降级。
- `constraint_id`：可选非空字符串，但同一 candidate 内不能重复。选择原版或修订候选时，case 的每个 `hard_constraints[].constraint_id` 都必须有一项显式覆盖；缺失成为 unknown，不是静默通过。
- `source_ref`：必需。可用 `evidence_refs` 添加其他来源。

如果 values 可以计算，必须同时提供 `actual/comparator/limit/unit/derived_formula`。只提供三元组中的一部分是 `incomplete_computation`，不得表示成通过。

数值形式二选一：

```json
{"actual": "14", "comparator": "le", "limit": "8", "unit": "hour/day"}
```

```json
{
  "actual": {"value": "14", "unit": "hour/day"},
  "comparator": "le",
  "limit": {"value": "8", "unit": "hour/day"},
  "unit": "hour/day",
  "derived_formula": "acquisition hours per day <= daily cap"
}
```

规则：

- actual/limit 的数值必须是 arithmetic string，由 `fractions.Fraction` 精确解析；不接受用 JSON float 冒充精确金额或剂量。
- comparator 仅支持 `le/lt/ge/gt/eq`。
- 单位是 naive string equality：不做单位换算，operand unit 与顶层 unit 必须逐字一致。
- 区间端点和开闭关系必须拆成明确的 `lt/le/gt/ge` 检查；不从 interval 名称猜是否含端点。
- 算术结果 false 使检查成为 fail；声明 pass/unknown 与 false 不一致会记录 contradiction。算术 true 不会把显式 fail 或 unknown 自动洗成 pass。
- 数字缺失、类型错误、除零、单位不一致产生明确 diagnostic 和 unknown computation；显式 fail 仍保持 fail，声明 pass 的不可计算检查还会成为 blocking error。
- 完全不提供比较字段时，工具只保留显式 status；这不证明 Agent 已独立核算。

工具不是 price-book 推导器。它不会自行寻找费率、倍率或乘法链；Agent 必须逐项写出数量、单价、单位、公式及 proof path，并另设总额比较。缺失价格、人时或 slots 时应为 unknown，不能隐式取 0。

## 8. Self QA 与负对照绝不混淆

`self_qa` 可用以下任一显式位置提供 boolean `matches`：

- `matches`
- `actual.matches`
- `qa.matches`

`matches=false` 强制该 self_qa 为 fail；若同时声称 `status=pass`，额外产生 `self_qa_matches_false`。它意味着自身、算术或选择验证需要修复，**不是科学负结果**，不能删除该字段来转绿。单纯的 `actual.matches` 不会被误当数值三元组。

同一 candidate 内重复 `check_id` 或重复 `constraint_id` 是全局 blocking contradiction，即使某项 status 写成 pass 也不能通过。跨 candidate 的同名 ID 只是各自作用域，不误报为重复。

只有 `kind=control` 且满足全部条件时，expected negative control 的 fail 才不构成 selection blocking failure：

```json
{
  "kind": "control",
  "status": "fail",
  "severity": "blocking",
  "is_expected_negative_control": true,
  "source_ref": {"locator": "/controls/actual", "quote": "actual result"},
  "expectation_source_ref": {"locator": "/controls/expected", "quote": "expected negative outcome rule"}
}
```

缺 `expectation_source_ref`、把它用于 constraint/self_qa，或试图豁免科学/资源硬约束，均不合法。被豁免的是该 control 的预期失败状态，不是其他 violation。

## 9. Selection、planning 与 execution 分离

`selection` 必需：

| 字段 | 规则 |
|---|---|
| `candidate_id` | 已存在的 ledger candidate ID，或 null；空字符串不是 null，会得到 unresolved selection |
| `kind` | 下列五种之一 |
| `execution_authorized` | boolean，必须与顶层 flag 一致 |
| `rationale` | 非空字符串，不替代显式 checks/changes |

五种 kind：

| kind | 选择约束与状态 |
|---|---|
| `original_preference` | 必须选择 original；所有 selected blocking known pass；self_qa 无失败；全部源 hard constraint 有记录；未知不允许 |
| `conditional_planning` | 可选 original/revision 或 null；允许未知，但已知 selected blocking fail 仍 invalid |
| `revision_proposal` | 必须选择 revision；允许未知；报告始终保留 `revision_pending_approval` |
| `rejection` | 可记录有已知 fail 的拒绝，允许 candidate null；不以未知形成完整拒绝结论；执行仍为假 |
| `undetermined` | 必须 candidate null，结果为 conditional |

`invalid_selection` 的关键规则：

- selected candidate 无法解析、版本/kind 错配、必需候选为 null。
- 非 rejection 选择引用了任何已知 blocking fail，包括 self_qa fail 或算术证明 false；conditional 字样不能洗掉该失败。
- selected blocking unknown 出现在 `original_preference` 或 `rejection`。
- 试图给失败、未知、修订提案或非执行选择附加执行授权。

只有 original preference 无错误、无未知且约束/QA 覆盖齐全时，`complete` 才表示**规划责任链可封闭**。一个正确记录的 rejection 也可以是行政处理上的 complete，但不表示候选可接受、科学有效或可执行。

`execution_ready` 是独立布尔值。它只能是 complete original preference、两个授权 flag 均 true、且有有效 approval evidence 的组合；没有批准时始终 false。conditional、revision proposal、rejection、undetermined、invalid 都不能 execution-ready。

任何 global identity、duplicate、source schema、hash、diff 或 self-contradiction 问题都会使整份结果为 invalid；不能因为出错 candidate 未被选中就删除责任链错误。

## 10. 报告内容与证明位置

报告包含：

- `counts`：候选、检查、pass/fail/unknown、blocking issue 和 unknown 数。
- `blocking_issues`：稳定 ledger 路径 `/candidates/N/...`、错误码和诊断。
- `conditional.unknowns`（同时提供 `conditional_unknowns` 别名）：所有显式未知、缺失覆盖和待审批记录。
- `source_status`：case ID/raw-bytes hash 绑定、严格 JSON 状态及未评估科学的声明。
- `validated_constraints`：每项 kind/status/severity、source refs、`actual/comparator/limit/unit/derived_formula`、`computed_result`、`parameter_case_path`、`actual_proof_path` 和 check path。
- `original_identity_records`：源路径、父/自身版本、父哈希、候选实际哈希、变更路径和 hash 状态。
- `decision.state`、`decision.execution_ready`、`decision.invalid_selection`、`can_finalize`。
- `final_validation.outcome` 和 `processing_status`。

计数与 unknown 不等于科学评分。尤其 `complete` 仅说明提交的机械声明自洽；错误事实引文、虚假批准或不完整科学分析仍需独立审计。

## 11. EXAMPLE-ONLY ledger（不是实际 case 答案）

以下仅演示结构。尖括号哈希是**占位符**，不是可接受的 digest；真实使用时必须替换为按上述算法对实际输入计算出的 64 位小写 hex。假定示例 case 存在这些 pointer，并在 `source_materials` 声明 `source.pdf`。它不是批准、预约、真实版本或科学结论。

```json
{
  "case_id": "EXAMPLE-ONLY",
  "source_case_sha256": "<64-lowercase-hex-of-exact-case-bytes>",
  "planning_scope": "Example planning metadata only; no booking or execution approval.",
  "execution_authorized": false,
  "candidates": [
    {
      "candidate_id": "example-plan-v2-proposal",
      "source_case_path": "/candidates/0",
      "source_plan_id": "EXAMPLE-PLAN",
      "source_version": "v2",
      "parent_plan_ref": "/candidates/0",
      "revision_kind": "revision",
      "parameter_case_path": "/candidates/0/parameters",
      "source_parameters_sha256": "<64-lowercase-hex-of-revised-canonical-parameters>",
      "canonical_parameters": {
        "daily_session_hours": "7"
      },
      "changes": [
        {
          "path": "/daily_session_hours",
          "before": "14",
          "after": "7",
          "reason": "Example proposed session split; no availability inferred.",
          "evidence_refs": [
            {
              "locator": "/candidates/0/parameters/daily_session_hours",
              "quote": "14"
            },
            {
              "locator": "/planning_contract/daily_cap",
              "quote": "8 scheduled hours per day"
            }
          ]
        }
      ],
      "checks": [
        {
          "check_id": "EXAMPLE-DAILY-CAP",
          "constraint_id": "EXAMPLE-DAILY-CAP",
          "kind": "constraint",
          "status": "pass",
          "severity": "blocking",
          "actual": "7",
          "actual_proof_path": "/daily_session_hours",
          "comparator": "le",
          "limit": "8",
          "unit": "hour/day",
          "derived_formula": "revised daily_session_hours <= source daily cap",
          "source_ref": {
            "locator": "/planning_contract/daily_cap",
            "quote": "8 scheduled hours per day"
          }
        },
        {
          "check_id": "EXAMPLE-BOOKING",
          "constraint_id": "EXAMPLE-BOOKING",
          "kind": "constraint",
          "status": "unknown",
          "severity": "blocking",
          "source_ref": {
            "locator": "/planning_contract/booking_status",
            "quote": "unconfirmed"
          }
        },
        {
          "check_id": "EXAMPLE-REVISION-APPROVAL",
          "constraint_id": "EXAMPLE-REVISION-APPROVAL",
          "kind": "constraint",
          "status": "unknown",
          "severity": "blocking",
          "source_ref": {
            "locator": "/planning_contract/revision_approval",
            "quote": "pending"
          }
        },
        {
          "check_id": "EXAMPLE-SELF-QA",
          "kind": "self_qa",
          "status": "pass",
          "severity": "blocking",
          "actual": {"matches": true},
          "source_ref": {
            "locator": "/candidates/0/version",
            "quote": "v1"
          }
        }
      ]
    }
  ],
  "selection": {
    "candidate_id": "example-plan-v2-proposal",
    "kind": "revision_proposal",
    "execution_authorized": false,
    "rationale": "EXAMPLE-ONLY: proposed revision with unresolved booking and approval; not an original preference."
  }
}
```

若把同一体内容和新哈希描述直接用于源版本而不改变 `source_version/canonical_parameters`，工具会拒绝。若删除 booking/approval checks，缺失覆盖不会使 example 成为 complete；若把已知 cap fail 改成 unknown 或 advisory，也不会获得合法的 original preference。最终核算仍须由 Agent 回源完成。
