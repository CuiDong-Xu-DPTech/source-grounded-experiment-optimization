# `io_guard.py`：运行时合同与完整性保护接口

本文对应 `scripts/io_guard.py` 版本 `2.0.0`（主集成可保留 v1 调用方式，按 v2 的 raw-domain/processing 语义接入）。它是执行期工具，不是优化器，也不是数学或科学验证器。

- 仅使用 Python 标准库；不会调用模型、联网或安装依赖。
- 若运行环境已经安装 `jsonschema`，会按需导入并执行真实校验；找不到库时明确报告 `executed=false`，绝不声称 schema-valid。
- 每个子命令均非交互，成功和普通失败均在 **stdout 输出恰好一个 JSON 对象**，诊断写 **stderr**。
- 顶层及每个子命令的 `--help` 也是 stdout JSON，其中 `usage` 和 `help` 字段保存可读帮助。
- JSON 输出使用 `allow_nan=false`。输入使用严格解析：拒绝重复对象键、`NaN`、`Infinity`、`-Infinity`，以及解析后成为非有限浮点数的数值。
- 不生成 benchmark score、baseline 增益或任何优化/科学结论。

Harness 中的典型路径为：

```bash
IO=/skills/weekly-skill/scripts/io_guard.py
CASE=/input/case.json
WORK=/app/work
OUT=/app/output
TASK=/input/task.md
```

## 1. 推荐调用顺序

```bash
python3 "$IO" bootstrap \
  --case "$CASE" --work "$WORK" --output "$OUT" --task "$TASK"

mkdir -p "$WORK"
cp "$OUT/result.json" "$WORK/candidate.json"
# Agent 实际阅读输入、完成任务，并编辑 WORK/candidate.json；必要时也编辑
# OUT/run_record.json 中的阅读状态、证据、claims 和 manual_review。

python3 "$IO" checkpoint \
  --case "$CASE" --candidate "$WORK/candidate.json" \
  --work "$WORK" --output "$OUT"

python3 "$IO" finalize \
  --case "$CASE" --work "$WORK" --output "$OUT"
```

需要单独查看审计时：

```bash
python3 "$IO" audit \
  --case "$CASE" --result "$OUT/result.json" \
  --work "$WORK" --output "$OUT"
```

可选优化报告均通过 `--optimization-report PATH` 添加；该文件必须是严格 JSON。工具只读诊断，不执行其中的算法。

## 2. 精确 CLI

### 2.1 顶层及帮助

```text
python3 io_guard.py --help
python3 io_guard.py bootstrap --help
python3 io_guard.py audit --help
python3 io_guard.py checkpoint --help
python3 io_guard.py finalize --help
python3 io_guard.py record-change --help
```

不接受长选项缩写。未知选项、缺选项或多余位置参数返回用法错误。各 `--case`、`--candidate`、`--result` 等值均为文件系统路径，不会解释成 URL。

### 2.2 `bootstrap`

```text
python3 io_guard.py bootstrap \
  --case /input/case.json \
  --work /app/work \
  --output /app/output \
  [--task /input/task.md]
```

要求 case 根是对象，且实际包含 `case_id` 和 `output_contract`。`case_id` 不强制转成字符串：其 JSON 值和原始类型都会在 run record 中保留。缺失 case、缺字段、非对象 case 或坏 JSON 是输入错误。

在输出目录生成：

- `/app/output/run_record.json`
- `/app/output/result.json`
- `/app/output/validation.json`

写入采用同目录临时文件、flush/fsync、`os.replace`。如果三个文件已经存在且均可严格解析、case ID 的类型和值相同、原 output contract 相同，则命令返回 `status="reused"`，三个文件全部保留。若只有部分文件存在、已有文件损坏、case ID 不同或合同改变，返回状态冲突（退出码 6），不自动覆盖可能的 last-known-good。没有 `--force` 危险旁路。

#### 原始合同快照

`run_record.output_contract_snapshot` 包含：

- `value`：未经 Schema 假设的原始 `case.output_contract` 深拷贝。
- `sha256`：对原始值作 UTF-8、排序键、紧凑 JSON 规范化后的 SHA256；不是外网资源 ID。
- `value_paths`：合同值自身所有递归路径，从 `/output_contract` 开始；数组使用实际索引。
- `declared_fields`：结果应覆盖的全部声明字段，不只是 `required`。
- `kind`：`json_schema`、`declarative_fields`、`prose` 或 `unsupported`。
- `detection`：识别方法和外部引用标记。

`run_record.all_declared_fields` 是声明字段路径列表。审计会重新从当前原始合同推导并比对，防止通过修改覆盖元数据跳过字段。

结果字段路径使用 JSON Pointer 风格：`/plan`、`/plan/steps`、`~0` 和 `~1` 分别转义 `~` 和 `/`。数组元素使用 `*`，例如 `/candidates/*/parameters`；空数组的项目属性记为不适用，但数组本身为空仍可在 complete 检查中构成空占位。

#### JSON Schema、local ref 与非 Schema 行为

布尔 Schema、显式 `$schema`，或结构合法的 JSON-Schema 关键字会进入 Schema 模式。`type` 接受单值或数组时，元素必须全部来自标准 JSON 类型。带 `fields` 的 field-map config（例如 `type="field-map"`）不会被误认为 JSON Schema；`type="object" + properties` 等有效结构仍按 Schema 处理。不能机械判断的部分保持 declarative/manual，而不是空声明通过 complete。

本地引用规则：

- 支持 `#`、`#/...` 及唯一 `$anchor` 的 `#result`；本地片段形式 `$id="#result"` 也可解析。字段展开、partial skeleton 和 case_id 许可解析共享同一引用解析器。
- unresolved/dynamic/recursive local ref 与重复 anchor 会在 `output_contract_snapshot.detection.local_reference_issues` 和 validation 中明确列出。`covered=null`/pending 或拒绝，不会变成“声明字段为空所以 complete”。
- external ref 仍禁止；dynamic local ref 的覆盖语义无安全静态实现时需要人工复核。

JSON Schema partial skeleton 会：

- 尽力展开本地 `$ref`/`$anchor`；所有声明属性都能保留在原合同快照。`allOf` 合并，oneOf/anyOf/if/then/else 在审计时按当前 result 的**实际 jsonschema 验证结果**选择 active 分支。任意分支 `anyOf` 合并所有 active，不施加互斥字段的全集。
- `oneOf` 不是恰好一个有效分支，或 `anyOf` 没有有效分支、数组不同元素走不同分支、条件无法求值、未装 jsonschema 时，明确 `manual_review_required`；不退回全部字段都必需。
- 对象按安全子结构生成，数组生成 `[]`。字符串只有被当前子 Schema 接受时才生成空串；非 nullable number/boolean 等不填 null。缺字段属于 pending/incomplete，但不能用类型错误的 null 当 partial 占位。
- `const` 是合同明确固定值，可用于固定示例标签/状态；`default` 和任意 enum 成员不会被冒充已确认科研值。无法确认真实 case_id 的骨架值会被删除，first partial 记 pending，原 ID 在 run record 保留；formal complete 仍检查 case_id 存在、值和原 JSON 类型。
- 默认通用账本放 run record，不再把通用 `access`、`role`、`code`、timestamp 等注入闭合的子 Schema。`case_id`、`status="partial"`、`contract_sha256`、`run_record_ref`、`example_label` 等只有属性和 literal 都被实际子 Schema 接受才嵌入。schema 自生的空通用账本不会遮蔽 sidecar 中的真实 pending。
- 根为 `additionalProperties=false`、数组/标量或复杂条件不可安全判断时，不强行增加辅助字段。bootstrap 可以有合同要求的未填字段，但不会自己引入额外属性或错误类型。

声明式 Pointer 使用正确 JSON Pointer escape/unescape，不截断首字符、不双重转义。例如 `/a~1b/~0key` 对应实际结构 `{"a/b":{"~key":...}}`。相对 `name` 按当前嵌套基址解释；表示键中的 `/` 应使用 `~1`。声明式合同支持 `fields`、`declared_fields`、`field_paths`、`all_declared_fields` 的列表/字段映射形式；无法机械抽取的部分必须人工映射。

#### Active 覆盖与原始完整合同

`run_record.all_declared_fields` 永远来自原始合同的完整字段集合。`validation.active_coverage` 则记录当前 result 的 active 分支选择，`coverage` 只检查其活动字段；无法选择时为 `covered=null` 和 `manual_review_required`。因此既不会漏掉原合同，也不会把交替分支的字段扁平化为同时必需。缺 active 字段或 jsonschema 验证失败仍然拒绝。

#### Selected `not:{required:[...]}` 排除

即使字段声明位于根 `properties` 而不是分支内部，当前活跃 schema 上下文中的简单否定也参与覆盖。例如根声明 `a,b`，分支 `required:[a] + not:{required:[b]}` 被实际 validator 选中时，`/a` 仍须存在，`/b` 不会因缺失而报错。若 result 实际提供 `/b`，事务不会借此通过：该 active 分支不再匹配，且真实 JSON Schema 验证继续决定最终拒绝。

- 排除仅发生在已选择分支的实际结果路径上，支持 local `$ref` 引用、嵌套对象和 allOf 中的简单否定合并。
- `negation_exclusions` 逐项保留依据，`excluded_result_paths` 明确记录从 active declarations 中过滤的字段。只过滤 active 覆盖，不删减原始合同快照或 `all_declared_fields`。
- 仅直接支持 `not:{required:[非空字符串...]}`。布尔表达式、嵌套组合、空列表或其他复杂 `not` 不会猜测排除；会报 `unsupported_active_negation` 并使 active coverage 转为 manual/undecided，其它声明字段不会被静默丢弃。未实现整个 boolean SAT。

#### 声明式科研合同：字段、封闭集合与 bootstrap

非 JSON Schema 合同支持以下真实声明形状，彼此可以组合：

- `field_requirements`、`fields`、`top_level_fields`：字段名到自然语言规则或 descriptor object 的映射；descriptor 可含 `required`、`type`、`rule`、`constraint`、`allowed` / `allowed_values`、`nullable` 等。
- `required_fields` 数组：补充声明和 coverage，不会把字段映射里的非 required 声明删掉。
- 直接 `科学字段名 -> 中文自然语言 required rule` 或 `科学字段名 -> {required,type,rule,allowed,nullable}`。配置头与字段分离，不按 JSON Schema 解释。

`fields` / `required_fields` 如果是 opaque literal assignment 或其他不能可靠枚举的内容，不伪造字段列表。它会留下 explicit/unsupported declaration 诊断：pending partial 可以接受真实限制，formal complete 不能凭 manual flag 或无声明表静默通过。人工字段映射/复核记录必须包含 reviewer、UTC、复核 notes 和原合同 hash，且 notes 要说明实际字段、来源和边界，不能把一句 `status=completed` 当科学正确证明。

额外顶层字段的不同等价键统一识别，包括 `additional_top_level_fields`、`additional_top_level_fields_allowed`、`additional_top_level_fields_permitted`、`additional_top_level_keys`、`additional_top_level_keys_allowed`、`additional_fields`、`additional_fields_allowed`、`allow_additional_top_level_fields`、`allow_additional_fields`、`allow_extra_fields`、`allow_extra_top_level_fields`。显式 `false` 表示 closed：result 只允许**实际声明的**科学字段，`required_fields` 与字段映射都要取并集，不是只取 required。在 descriptor 或 task 中只描述 additional 规则而没有明确 bool 时，不臆造封闭约束。

封闭 declarative bootstrap 的规则：

- `case_id` 只在声明时嵌入，原 JSON 类型和值不变。`status` 优先 `partial`，否则才可能选允许的 `undetermined`、`conditional`、`conditional_choice`，绝不默认 complete 或 no-feasible。
- 已声明 array/object 类型可生成安全空结构；nullable selection 可生成 null；无法安全确定类型/嵌套内容的字段保留 missing/pending。缺失科学答案可以后续补写，绝不伪造。语义阅读仍登记为待办。
- `sources`、`uncertainty`、`limitations`、`change_records`、`contract_sha256`、`run_record_ref`、`contract_mapping` 等通用记账属性默认放在 sidecar。不会因为手写 result 可以被覆写，就擅自把这些键撒到 closed result 顶层。
- 非封闭的 declarative/open 合同仍兼容原有通用辅助字段；显式开放的 additional 规则和文中示例只控制 result，不改变受保护 sidecar 的职责分配。

schema-mode 继续沿用原来的 JSON Schema anchor、local `$ref`、`not:{required}`、active branch、真实校验和严格 partial skeleton 逻辑。declarative 与 JSON Schema 不混用 `required` 语义替代品。

#### 输入登记不等于已阅读

run record 的 `input_manifest` 逐项记录：

```text
input_id, path, role, origin, found, readable, media_type,
size_bytes, sha256, content_reviewed, review_required,
review_note, error
```

输入静态发现采用保守语法，而不是对任意字符串猜路径：

- 明确的 `path`/`file`、`file_path`、`local_path`、`*_paths`、`*_files` 等字段；绝对路径原样登记。
- `materials`、`files`、`documents`、`attachments`、`input_files`、`source_files`、`pdfs` 的直接数组元素对象中，形如具体文件名的 `name`，包括 `materials[].name`。仅当名称具实际扩展名/字母等合理文件名特征时登记，caption、自由描述和数值标签不算文件。
- 相对路径优先相对 case 所在目录解析；如果 case 实际位于一个名为 `input` 的目录中，例如 `.../some-bundle/input/case.json`，则 `path="input/primary.pdf"` 可按该根惯用形式剥离**恰好一个开头的 `input/`**，得到 `.../some-bundle/input/primary.pdf`，绝不先重复拼成 `input/input/primary.pdf`。只支持真实片段前缀，不匹配任意子串；大小写和不符合该目录语义的载体不做这种剥离。
- 仅有唯一真实候选时才选取；如普通 case 相对路径 `/input` 回退和该根剥离形式同时命中不同真实文件，写入 `ambiguous_references` 并保留候选路径和解析方式，不静默首取。缺失文件继续记录 manifest/pending，不创建证据文件。
- 每份文件 manifest 还记录 `resolved_by`（`input_root_literal_prefix`、`case_relative`、`system_input_root` 或 `absolute_case_input_realpath`）。绝对输入路径使用其 authorized realpath；相对路径被限制在实际 case/input 或已有 `/input` 根内，任何越界的 `..` 都是拒绝/歧义项，不从 cwd、用户 home 或其他未声明位置猜。work/output 中的临时或最终输出不会被当成原始输入。
- URL/scheme、http(s)、百分号标签、自由 `material_source` 和含空白的描述不会直接变成本地路径。真正无法静态确定的标记存于 `input_discovery.ambiguous_references`，complete 前需要真实 `input_manifest_review`，含 `status="completed"`、reviewer、`reviewed_at_utc`、notes 和当前 `case_raw_sha256`。

`input_discovery.references` 与 manifest 一同保存。audit 从 **current case** 重新推导这些语法引用，核对 discovery 快照、登记覆盖、目录中所有实际文件、case 原件 realpath 和 `case_raw_sha256`，并重算各文件 hash。删 manifest 项、换 case、增删引用或修改字节均不能掩盖。计算 SHA256 必须读取字节，但 `content_reviewed` 表示的是**语义内容已经实际检查**，不是“文件存在”或“哈希已算”。除已严格解析的 case JSON 外，bootstrap 默认该值为 `false`。

`--task` 未指定或指定路径不存在时，按顺序探查：

1. 明确传入的 `--task`（如有）；
2. `/input/task.md`；
3. `/app/task.md`；
4. `/app/work/task.md`；
5. `/task.md`。

选择第一个实际文件；任务缺失不使 bootstrap 强行失败。`task_discovery.probed` 标记每个探查位置，缺失任务候选也进入清单。缺失或不可读的正式引用输入会阻止 complete。

Agent 真正阅读某输入后，可把对应 `content_reviewed` 改为 `true`；只有确实不需要语义检查时，才可设 `review_required=false` 并留下理由。审计还会重新计算字节数和 SHA256，发现变动不允许原样宣称 complete。

### 2.3 `audit`

```text
python3 io_guard.py audit \
  --case /input/case.json \
  --result /app/output/result.json \
  --work /app/work \
  --output /app/output \
  [--optimization-report /app/work/optimization.json]
```

读取 result 后定位 run record 时必须证明 sidecar 是**唯一且受保护的**：

- 缺省、`null` 或空字符串 ref 表示尚未指定引用，按原约定只能回退到 `--output/run_record.json`；这不是外部 sidecar 许可。空值本身仍是 complete 检查中的 pending/占位，formal complete 应改成非空 canonical ref，partial 可以保留具体限制。
- 非空 ref 必须是字符串，数组等其他类型拒绝。独立 audit 中，相对 ref 相对被审 result 所在目录解析；checkpoint/finalize 在预期最终 `--output` 位置解释相对 ref，temporary staging 不改变其语义。
- 解析后的 `realpath` 必须严格等于 `--output/run_record.json` 的 realpath。`./x/../run_record.json`、symlink 到同一路径等 canonical 形式可接受；相对/绝对逃逸、其他 sidecar 和歧义路径为 fatal。绝不先加载外部 ref 再靠本轮 override 掩盖差异。

`validation.run_record_reference` 保存 raw ref、解析 realpath、受保护 realpath、基准目录和判定结果。

审计更新 `--output/validation.json`。若 `--result` 指向其他候选，该报告描述的是那个候选，因此保护性更新应使用 `checkpoint`，不要直接对随意外部文件 audit 后误以为 checkpoint 已更新。

#### Result 原始字节 SHA 与 canonical JSON SHA

以下三个字段属于不同的东西，不能混用：

| 字段 | 计算对象及用途 |
|---|---|
| `validation.result_sha256` | **本次 `--result` 所指实际文件读回的 UTF-8 原始 bytes** 的 SHA256。单纯重排空格、缩进或换行也会产生不同的 raw SHA。finalize 读取实际 `output/result.json`。 |
| `validation.result_canonical_sha256` | 对已严格解析的 JSON 值运行 `hash_value(value)`：`canonical_bytes` 按排序键、紧凑分隔符输出后计算的 SHA256。它只辅助比较规范化内容，不是磁盘字节哈希。 |
| `validation.result_sha256_basis` | raw digest 的来源或直接调用不能提供 raw digest 的原因。正常实际 CLI 为 `supplied_raw_result_bytes`，表示调用链已传入刚刚从目标读到的原始 bytes 的 digest。 |

实际 CLI 直接从 `load_json` 已经读回的原始 bytes 计算 digest，**不会先把文件 canonicalize，再假装那是文件 raw SHA**。独立 `audit --result candidate-b.json` 计算的是 candidate-b 的字节，哪怕 output 里还存在先前接受的 result-a，也绝不把后者的哈希充作 candidate-b 的哈希。

`canonical_bytes` 和 `hash_value` 的其它既有用途保持不变，包括原始 output contract 值快照及 change journal 记录哈希；这些规范化 digest 没有被全局改成文件字节 digest。写出普通 `json_bytes` 的缩进/换行形式也不等同于 `canonical_bytes` 格式。

- 同一 JSON 对象有两个不同缩进的文件：raw SHA 可以不同，canonical SHA 应相同。因此 raw SHA 变化本身不等于科学内容改动；只有实际 JSON 值及相应证据检查才能说明科学内容是否更改。canonical 相同不证明科学正确、来源支持、最优性或执行许可。
- **比较协议固定在不同字段各自所属的域内**：raw 对 raw，canonical 对 canonical。不得把一个原始文件 SHA 与另一域的 canonical SHA 当作不匹配证据。canonical 也不能替代既定的 last-good 原始已接受字节证明或绕过任何合同/降级检查。
- `run_record.last_checkpoint.candidate_sha256` 继续是候选原始文件 bytes 的 SHA。checkpoint 先将同一 bytes 写到 staging，再原子保存到 `output/result.json`；post-transition 审计显式复用 `candidate_data` 的 SHA，而不是读取提交前可能仍存在的旧 output 文件。因此新 `validation.result_sha256`、最终实际 result 文件 SHA 及这个 candidate SHA 在成功提交后相同，canonical 字段另列。
- `audit_documents(..., result_raw_sha256=None)` 的额外 keyword 保持既有位置调用兼容。直接调用者如传入该值，必须传入**实际候选原始 bytes** 的 SHA。未传入时，只有 `result_path` 真正可读、能严格解析且其 JSON 值和类型与 `result` 参数一致，才直接计算这些 bytes 的 SHA，basis 为 `actual_result_path_bytes_verified_against_result`。
- 直接调用的文件不存在、不可读或不是同一对象时，不得把 canonical digest 伪装成 raw digest：`result_sha256=null`，basis 分别以 `unavailable_result_bytes_not_supplied_or_readable` 或 `unavailable_result_path_does_not_match_supplied_object` 明确说明。需要 raw 文件绑定时应修复文件/参数或显式传入正确实际 bytes 的 digest，不得改用 canonical 值补齐。

直接 audit 要求 result 与 run record 的 **processing status** 一致；raw domain status 可以因科学含义不同而不同。有意进行交付状态转移必须走 checkpoint，不能靠替换 raw status 文本掩盖 `audit_status_mismatch`。

机械检查包括：

- case ID 的值与 JSON 类型；active 结构能够接受 case_id 时 result 必须保留。`additionalProperties=false` 且未声明 case_id 时，run record 中的原 ID 独立提供身份锚点，不能为了复制 ID 破坏闭合 Schema。
- 原 output contract 的快照、hash、全声明字段元数据；检查 active 分支覆盖而非只检查 required。
- 所有递归 JSON 值为有限数，合法 negative number 不受影响。用作已取得值的裸 `N/A`、`NA`、`TBD`、`unknown`、`not available`、`not applicable` 及对应中文哨兵拒绝；带伪值前缀的 `"N/A - ..."` 也拒绝。化学元素正确大小写 `"Na"` 不按 `NA` 处理。
- **结构化状态例外**：`status`、`feasibility`、`*_status` 等字段的 unknown/pending/failed 可保留。对应 `value` 或 `*_value=null` 在 unknown/pending/failed/error 状态下可保留。legacy complete 的 unknown/pending 用 `blocking_unknown_pending` 阻止；partial 为 incomplete。显式允许的 scientific domain-final 若有实际 cause/confirmation/recheck，可记录 `domain_uncertainty_documented` warning，而不是 fatal 的裸 N/A。failed/error 可用于已知失败记录，具体 details/issues 按实际结构保留。
- **明确空语义例外**：`adverse_events`、`conflicts`、`incidents`、`deviations`、`errors`、`warnings` 等事件列表可为空，表示无记录事件。其他泛化 required 空数组需同层的 `<field>_note`、`<field>_reason`、`<field>_empty_reason` 或 `empty_reasons[field]` 给出真实说明。`uncertainty`、`limitations`、`evidence`、`claims`、`evidence_refs` 等仍按各自账本规则检查，不能全局免检。
- result 内嵌账本和 run record 中实际被引用的账本、evidence、claims；不能通过把哨兵移到 sidecar 绕过检查。
- 状态、输入清单、来源和变更记录结构。
- 可选的真实 JSON Schema 校验、人工合同复核、证据账本、优化报告诊断。

#### Raw domain status 与 processing status

**科学最终状态和任务交付状态分离。** `result.status` 保留结果声明的 raw domain 值，不从字面强制改写为 `complete`、`partial` 或 `failed`。另一个 canonical `processing_status` 只用于交付审计与状态迁移：

| 类别 | Raw domain 示例 | Canonical processing 语义 |
|---|---|---|
| Legacy `complete` | `complete` | `complete`，仍须满足全部合同门槛，不代表实验已执行或已获批准 |
| Legacy `partial` / `failed` | `partial`、`failed` | 对应的交付降级/失败状态，可有实际 pending，保持 v1 兼容 |
| Explicit scientific final | `undetermined`、`conditional`、`conditional_choice` | 若已被该 case 明确允许且解释/证据/合同检查齐全，可作为完整 task 交付收尾，不强制改写为 failed |
| 不可行科学结论 | `no_feasible_candidate`、`no_feasible_plan`、`infeasible` | 同样需要真实不可行证据，不能靠 status 或 selection=null 获取 free pass |

allowed vocabulary 的优先级为：status descriptor 的 JSON Schema `const`/`enum`、declarative `allowed` / `allowed_values`、其约束或 rule 文本中明确出现的标准 domain-final 词表。**未说明时不泛化接受任意新状态**，未声明的任意 `*_final` 仍拒绝。schema-mode 的真实 `jsonschema` 校验和 case_id 独立身份检查不被这层映射替代。

run record 保存：

```text
status                  # canonical processing status，连接旧记录
processing_status       # complete | partial | failed
raw_domain_status       # 保留用户结果原始状态，JSON 类型/值不改写
last_checkpoint.accepted_processing_status
```

`checkpoint` 同时记录 `accepted_status`（raw）和 `accepted_processing_status`，last-good 依据 processing 以及已接受的 result 原始字节 SHA256 判断是否受保护。`finalize` 的 `attempted_status` 保留 raw domain，同时保存 processing；科学 conditional 等得到允许的 completed task 的**交付**状态，不等于强行赋予科学 `complete`。

API 层面 `validation.effective_status` 和主命令 stdout `status` 是 raw domain。`processing_status` 是附加字段，供 v2 主集成读取。旧 consumer 可仍按 v1 兼容字段读取，但不要误解 raw `infeasible` 为 runtime failed。

#### Nullable selection 与 domain uncertainty

- selection 由 descriptor 显式声明 `nullable=true`，或类型含 `null`，或由规则明确写出 nullable/may be null/或 null 时，才可走允许 null 的语义信道。自然语言 "nullable object" 同样可识别。
- 即使允许 null，也必须在 uncertainties、confirmations、cause/consequence/needed_confirmation 或结构化 recheck 中提供真实原因和解决路线。没有这些路径会有 `nullable_explanation_required`。null 不是对未核实可行性的批准，也不是未测量 zero。
- `undetermined`、`conditional`、`conditional_choice` 需要真实条件说明域；可明确识别为 future observation 的结构化 unknown 可以带 `status`、cause、确认责任或 recheck 保留。多个同类记录合并为 `domain_uncertainty_grouped`：按状态分组，包含 `total_count` 和全部 `affected_paths`，原始 facts 在 result/run_record 中不删改。它不是“所有 unknown 一律通过”，也不允许被转换为已测 result/hard approval。
- safety、execution approvals、optimizer/possible winner 及其他无法明确区分 future observation 语义的 structured unknown 保持 `blocking_unknown_pending` 逐项处理，不被打包掩盖；只有完全满足合同域和真实上下文的 scientific conditional task 才能完成规划审计。legacy partial 仍保留 incomplete。
- 缺交付字段、类型错误、真实 Schema 错误、伪造 N/A/TBD、缺解释 unknown 或显式 `blocking_complete=true` 仍然阻止。optimizer 的 unknown response 仍不得参与 ranking、hard approval 或虚构 exact winner。

`checkpoint_eligible` 与 `audit_passed` 仍不同：普通 task 的 processing `complete` 只有在无 fatal/error/incomplete/manual 时成立；`partial`/`failed` 可保留诚实 pending 和 warning，但必须有具体 uncertainty/limitations。到 checkpoint/finalize 受理科学状态描述本身，并不替代其他字段、证据或人工审查。

#### 不可行结论的证据门槛

`no_feasible_candidate`、`no_feasible_plan`、`infeasible` 额外要求以下至少一个可复核入口：

1. 同一 case_id（原类型/值匹配）的真实 optimization report，`status=infeasible`、`search.exhaustive=true`、非空真实 candidates，**全部明确 infeasible 且无 unranked/unresolved**。错 case、未穷尽、部分搜索、未知潜在赢家或空 candidates 均不能代替证明。
2. `infeasibility_evidence`（result 优先，缺省回退 run record）描述案例自身清晰约束冲突：每项含实际 `source_id`（必须在有效 sources 中）、`locator`、非空 `constraint_ids`、具体冲突 details，以及 `recheck.method + recheck.status=passed/verified/confirmed/matched/complete`。空泛 claim、人工 flag、无 recheck 仍拒绝为 `no_feasible_evidence_required`。

若没有将优化报告传给 `checkpoint`，不能在 `finalize` 隐式沿用“曾经看过”/self claim。finalize 也必须复传同一 `--optimization-report` 核对，或提供完整的 case-conflict 证据。这里只核对证据是否存在及其可复核链，并不宣称数学/科学正确性永不需专家复核。

`complete`/conditional 从不表示数学、来源支持或科学正确性被无条件证明。`scientific_or_mathematical_validity_claimed` 永远为 false。

#### JSON Schema 校验的真实性

`validation.schema_validation` 字段包括 `library_found`、`executed`、`valid`、`errors` 和 `reason_not_executed`。

- 已安装 `jsonschema`：用其 draft 推断 validator、`check_schema` 和 `FormatChecker` 实际校验。上限列出 100 条 Schema 错误，若有截断会记录。
- 未安装：`executed=false`、`valid=null`，不安装依赖；partial 为 warning，complete 不能据此声称通过 Schema。
- 任意外部 `$ref`、`$dynamicRef`、`$recursiveRef`：为避免库隐式访问网络/外部 Schema，不执行校验；complete 被阻止。
- 非 Schema：`applicable=false`、`executed=false`、`valid=null`。

partial 的 null/empty 导致的 Schema 不完整可保留；错误的实际类型/值会产生 error，不能借 partial 掩盖非法候选。

#### 可读/散文合同的 manual review

非 JSON Schema 合同要求 run record 或 result 中存在以下真实记录；result 中非空 `manual_review` 优先。`contract_mapping.manual_review_required=true` 这样的待办标记本身不算完成复核：

```json
{
  "manual_review": {
    "status": "completed",
    "contract_sha256": "原合同快照的64位哈希",
    "reviewer": "实际复核者标识",
    "reviewed_at_utc": "实际UTC时间",
    "notes": "实际字段映射与边界复核说明"
  }
}
```

未满足时 validation 明确为 `manual_review_required`；partial 可保存，complete 不通过。填这个结构只记录复核声明，不证明科学正确性。

### 2.4 账本结构与 complete 门槛

结果对象中的下列键优先于 run record；result 未包含的键才回退到 run record：

```text
sources, uncertainty, uncertainty_note,
limitations, limitations_note, change_records,
evidence, claims
```

`evidence`/`claims` 另有一项防 stale 规则：若 result 中是 `[]`，而 run record 对应列表非空，不能由空表面隐藏真实证据；审计将使用非空 sidecar 列表，并记录 `nonempty_sidecar_fallback` warning。result 中的非空 evidence/claims 始终优先，即使它们非法，run record 中另有合法副本也不能将其掩盖。所有实际仍有效的列表都会接受来源、ID、recheck、source_id 链接和空占位检查。

选择 sidecar 模式时，不要遗留无意义的空覆盖层；ref 必须按上节 confinement 规则指向受保护 run record。

#### `sources`

必须是列表。每项是对象，至少有唯一 `source_id`（或 `id`）及具体 `locator`（或 `source_locator`）。complete 至少一个来源。引用本身不证明该来源支持结论。

#### `uncertainty` 与 `limitations`

必须是列表，项目使用非空 `detail`/`description`（limitations 也可直接为具体字符串）。

- partial/failed：二者至少要有具体限制或未知说明，不能只写 `N/A`。
- complete：带 `blocking_complete=true` 的 uncertainty 会拒绝 complete。其他如实披露的预测/科学不确定性可以保留，不要求虚假“零不确定性”。
- 若 complete 的 `uncertainty=[]`，需要 `uncertainty_note` 含字面量 **`none-known`** 和具体解释。
- 若 complete 的 `limitations=[]`，需要 `limitations_note` 含 **`none-known`** 和具体解释。

推荐说明“此处 none-known 仅指本次复核未发现，并不消除残余科学不确定性”，不要为了通过检查编造随机证据或绝对保证。

#### `change_records`

必须为列表，每项至少有 `operation`、`path`、`reason`；complete 的每项还需 `timestamp_utc`。bootstrap 会写第一条真实初始化记录。此列表与下面的 append-only JSONL 互补，并不会自动同步。

#### 通用 evidence/claims 账本

result 或 run record 如存在 `evidence` 或 `claims`，有关列表都会接受机械检查，适用上一节的优先级及 fallback。

Evidence 项目：

```json
{
  "evidence_id": "ev-1",
  "source_id": "src-material",
  "locator": "/input/source.pdf#page=4",
  "recheck": {
    "method": "re-read cited page and compare exact source span",
    "status": "passed",
    "checked_at_utc": "实际UTC时间"
  }
}
```

接受 `id` 替代 `evidence_id`，接受 `source_locator` 或 `source.locator`。每项必须 ID 唯一、具有来源定位及 `source_id`；`source_id`（也可位于 `source.source_id`）必须解析到**当前有效 sources 集合**中的实际 source ID，悬空 source_id 或缺失均给出诊断。`recheck` 必须有 `method` 和 `status`；完成状态为 `pass`、`passed`、`verified`、`confirmed`、`matched`、`complete` 之一。pending 不会通过 complete。

Claim 项目：

```json
{
  "claim_id": "cl-1",
  "statement": "可独立检查的主张",
  "evidence_refs": ["ev-1"],
  "recheck": {"method": "复核主张与定位原文", "status": "passed"}
}
```

接受 `id`、`text`、`evidence_ids` 对应别名。每个引用必须解析到已登记 evidence ID；每个 claim 也必须有 recheck。ID、引用和 recheck 齐全仍不让工具推断 scientific correctness。

### 2.5 `checkpoint`

```text
python3 io_guard.py checkpoint \
  --case /input/case.json \
  --candidate /app/work/candidate.json \
  --work /app/work \
  --output /app/output \
  [--optimization-report /app/work/optimization.json]
```

候选在 `--work` 下独占临时目录中复制并审计；不直接信任 candidate 路径。checkpoint 始终使用 `--output/run_record.json` 作为受保护基准，并按最终 output 位置解释相对 ref，避免 staging 改变指代。缺省、`null` 或空字符串 ref 表示尚未指定引用，只能**回退到同一个受保护 sidecar**；空值仍作为 pending 被记录，不会帮助 complete 通过。实际运行时推荐显式写 `run_record.json`。指向其它文件的字符串 ref、非空但非法类型，以及任何 `..`/绝对路径逃逸均拒绝；canonical 路径只在最终指向同一受保护文件时接受。

- `complete`：必须 `audit_passed=true`。
- `partial`/`failed`：允许 pending 覆盖和占位，但必须有具体限制，且不能有 fatal/error。
- 坏 JSON、重复键、非有限数、case ID 不符、合同快照不符、实际 Schema 类型错误等均拒绝。
- 已由 audit 通过的 complete 不允许被 partial/failed 降级；返回 `last_good_downgrade_refused`。已通过 checkpoint 的 complete 还由 `last_checkpoint.accepted_status` 和当前 result 原始字节 SHA256 独立证明；后来一次失败的诊断 audit 覆盖了 validation 状态，也不会清除这项降级保护。
- 拒绝时原 `output/result.json` 和 `output/validation.json` 不动，问题通过 stdout JSON 返回。

接受前会更新内存中的 run record 状态，再执行一次无状态转移豁免的审计。接受后更新：

```text
output/run_record.json
output/result.json       # 保留候选原始 JSON 字节
output/validation.json
```

先在各自目标目录完成临时 copy 和 fsync，再分别 `os.replace`。已有文件会暂时备份；提交中发生可捕获异常时恢复已替换文件，清理临时文件。单文件替换是原子的；多文件集合无法进行跨文件系统级原子事务，回滚也失败时返回明确的 `checkpoint_rollback_failed`（退出码 70），不会谎称完整提交成功。

### 2.6 `optimize` 不存在：优化报告只读诊断

可向 `audit`、`checkpoint`、`finalize` 添加：

```text
--optimization-report /app/work/optimization.json
```

检查逻辑是通用结构诊断，不实现搜索或代理模型。它同时适配同目录 `optimize.py` 的真实报告和旧 generic 形状。

#### 候选索引、可行性与角色

真实报告的典型结构如下，ID 是索引键而不是文件系统路径或 JSON Pointer：

```text
candidates[]                      # 候选字典，candidate_id 为主键
ranked_candidate_ids[]            # 完整 ranked order
pareto_candidate_ids[]            # Pareto 推荐集合
unranked_candidate_ids[]          # 未决集合
best_candidate_ids[]              # single/lexicographic exact winners，保留全部并列
decision_summary.unresolved_candidate_ids[]
```

- 真实 `feasibility: "feasible" | "unknown" | "infeasible"` 被直接识别；`feasible` 是已确认可行，`unknown` 不是可行，也不等同于明确 infeasible。旧形式 `feasible=true + confirmed_feasible=true`、`feasibility.confirmed=true` 等仍受支持。
- ID 引用统一链接到 candidates 索引。不同对象使用相同 `candidate_id` 会产生 `candidate_id_collision`，不能静默覆盖后选择第一个；包含 `/`、`~`、JSON 括号等字符的 ID 不参与路径展开。
- 完整 ranked order 中每项都必须确认可行。`pareto_candidate_ids`、`best_candidate_ids` 按 selected/recommended 角色检查；旧 `ranked`、`ranked_candidates`、`ranking` 及 `selected`、`selected_candidate_id`、`recommended_candidate` 也继续检查。
- candidates 全表**不自动全是推荐项**。表内未选、未 rank 的 unknown 只进入未决诊断，不产生 selected-feasibility 错误；一旦其 ID 被 best/Pareto/selected/recommended 引用，就必须确认可行。
- 同一候选同时出现在 ranked 和 best/Pareto/selected 时合并为一项 feasibility check，并保留全部角色。`unranked_candidate_ids`、`decision_summary.unresolved_candidate_ids` 和 diagnostics 中重复出现的 ID 也按候选合并，避免 double selected/重复未决报告。

#### unresolved 与 exact optimality

`unresolved_candidates` 记录每项 `may_change_selection=true`，来源包括可行性 unknown、`decision_status=unranked`、显式 unresolved ID，以及 `search.exhaustive=false`、截断或 partial 暗示的未评估搜索空间。明确 infeasible 候选项不作为可能赢家。

报告 `exact_optimal=true` 必须同时满足：

1. 明确且一致的 `search.exhaustive=true`，不能截断；
2. 报告 `status="optimal"`；
3. `best_candidate_ids` 非空，并且全部链接到真实、确认可行的 candidates；
4. 没有可能改变选择结果的未决或未评估候选；
5. 报告不得是 `status="partial"` 或带 `partial=true`。

`exact_optimal=false + search.exhaustive=true` 是合法组合，**不再报 mismatch**。例如 Pareto/feasibility 的 `status=complete`、无可行解的 `infeasible`、有未决输入的 `unresolved` 都可以建立在完整枚举上。`best_candidate_ids` 在非 exact 时应为空。`status=optimal` 与显式非 true 的 exact 标志也不一致。

#### 非结论报告与外层 result 解耦

`status="unresolved" | "infeasible" | "partial" | "invalid_request"` 或 `partial=true` 只会让报告本身标记为 nonconclusive，**不会仅因此而否决普通的 `result.status=complete`**。例如，已经完整地提出并说明“无可行解”的方案可以是普通 planning-complete；其限制仍须由 result 的 uncertainty/limitations 和其他合同字段如实表达。

若 result 另行声明完成/精确优化，则 nonconclusive 报告不能支撑这种声明。会检查的标志包括：

- `optimization_completed=true`、`completed_optimization=true`、`optimization_complete=true`、`deterministic_optimization_complete=true`；
- `optimization_status` / `optimization_result_status` 为 `complete`、`completed`、`optimal`、`exact_optimal` 等，及 `optimization`、`optimization_result`、`optimization_report` 内的相应状态；
- result 的 `exact_optimal=true` 或其他精确最优声明。此时 linked report 也必须含有一致、可审计的 `exact_optimal=true`，并满足上节全部条件；
- result 选择 `best_candidate_ids`、`exact_candidate_id`、`selected_exact_candidate_id`、`selected_best_candidate_id` 等 exact candidate。普通 selected ID 若指向报告的 best ID，也按 exact selection 处理。

这些声明在 final result 为 complete 时产生 error；在诚实的 partial/failed 阶段保留为 incomplete 诊断。nonconclusive 本身仅产生 warning，不替代外层状态判断。

即使全部结构和标志一致，也不证明实际搜索穷尽或全局最优；工具不重新枚举、不计算目标值、不验证数学证明。validation 中 `algorithm_executed=false`、`scientific_optimality_verified=false` 保持事实边界。

### 2.7 `finalize`

```text
python3 io_guard.py finalize \
  --case /input/case.json \
  --work /app/work \
  --output /app/output \
  [--optimization-report /app/work/optimization.json]
```

finalize 对**实际** `output/result.json` 再审计，写全 `output/validation.json`，并更新 run record 的：

```text
updated_at_utc
finalization.state                 # finalized 或 rejected
finalization.finalized_at_utc
finalization.attempted_status
finalization.audit_passed
finalization.pending_validations
finalization.result_sha256                 # 实际 output/result.json 原始 bytes 的 SHA256
finalization.result_canonical_sha256       # 同一解析对象的辅助规范化内容 SHA256
finalization.result_sha256_basis           # 本次实际 bytes digest 的调用链来源
```

成功 checkpoint 后，finalization 的 raw result SHA 应与当时实际 result 文件及 `last_checkpoint.candidate_sha256` 一致。旧记录的该字段可能是 canonical 值；迁移到本版后的新报告会同时给出并明确区分两者，不能依据字段相同名称把旧 canonical 值与新 raw 值直接比较。旧文件不会被工具自动重算、迁移或重写；只有实际执行审计/完成相应命令的输出才反映新语义。

合法 partial/failed 可 `finalized`，但限制和 pending 会完整保留。非法 complete 得到 `rejected` 和退出码 5，不自动篡改成 partial，也不声称完成。

最终报告明确四级：

| 级别 | 工具报告的实际范围 |
|---|---|
| `structural` | JSON、身份、合同覆盖、占位、账本、Schema 执行结果 |
| `arithmetic` | 默认 `not_run`；有限数检查不等于重算公式、目标或最优性 |
| `source_anchor` | 来源 ID、定位、引用、recheck 机械结构；不推断引文支持结论 |
| `scientific_review` | 永非无条件验证；需要实际领域复核 |

即使 structural 通过，最终 pending 仍包含 arithmetic 未重算和科学复核边界。这是刻意保留的限制，而不是自相矛盾的 complete 宣称。

### 2.8 `record-change`

```text
python3 io_guard.py record-change \
  --work /app/work \
  --operation OPERATION \
  --path PATH \
  --reason REASON \
  [--before-sha 64_HEX] \
  [--after-sha 64_HEX] \
  [--evidence-ref REF [--evidence-ref REF ...]]
```

只追加 `/app/work/change_records.jsonl`，不写 home 配置或其他文件。operation/path/reason 必须非空；before/after SHA 若提供，必须是 64 个十六进制字符并规范为小写。多个 `--evidence-ref` 按出现顺序保存。

每项包含：

```text
timestamp_utc        实际 UTC，毫秒精度，Z 结尾
sequence            从 1 连续递增
operation, path, reason
before_sha256, after_sha256
evidence_refs
previous_entry_sha256
entry_sha256        对不含本字段的规范化记录计算 SHA256
```

追加前校验现有每行的严格 JSON、entry hash、previous hash 链和 sequence；空行、缺末尾换行或任何损坏都会拒绝追加。以 append 模式打开、flush/fsync。省略 before/after 时保留 `null`，不会虚构磁盘状态。

## 3. stdout、stderr 与退出码

错误 stdout 形状：

```json
{
  "ok": false,
  "status": "error",
  "error": {
    "code": "invalid_json",
    "message": "严格解析诊断",
    "details": {"path": "/实际路径"}
  }
}
```

checkpoint 的候选审计拒绝使用 `status="rejected"` 并携带 issues；它不改旧的输出文件。stderr 是单行 `io_guard: ...` 诊断，不含需要机器读取的唯一答案。

| 退出码 | 含义 |
|---:|---|
| 0 | 命令按语义成功；bootstrap 可产生 partial，checkpoint 可接受合法 partial，finalize 可 finalized partial |
| 2 | CLI 用法错误、空 operation/path/reason、错误 SHA 参数 |
| 3 | 缺输入、坏/重复键/非有限 JSON、读写失败、缺 case 字段、无效 checkpoint JSON |
| 4 | permission denied |
| 5 | audit 未通过、checkpoint 候选拒绝、非法 complete/finalize rejected |
| 6 | 状态冲突：bootstrap 已有文件冲突、直接 audit 状态不一致、last-good 降级、change journal 完整性冲突 |
| 70 | 意外内部错误或 checkpoint 回滚也失败；不得视为完成 |
| 130 | 键盘中断 |

对于 checkpoint 返回 5/6，先检查 stdout issues 和旧文件；不要随后盲目 `audit` 外部非法候选覆盖有用的 last-good validation。修正 candidate 后重新 checkpoint 即恢复路径。

## 4. 最小合成示例（不是官方案例或 Harness 验证）

以下只演示接口，不携带科研答案或评分：

```bash
# 假设临时 case.json 的 output_contract 声明 case_id/status/notes，并允许附加账本。
python3 /skills/weekly-skill/scripts/io_guard.py bootstrap \
  --case /input/case.json --work /app/work --output /app/output --task /input/task.md

python3 /skills/weekly-skill/scripts/io_guard.py checkpoint \
  --case /input/case.json --candidate /app/work/candidate.json \
  --work /app/work --output /app/output

python3 /skills/weekly-skill/scripts/io_guard.py finalize \
  --case /input/case.json --work /app/work --output /app/output

python3 /skills/weekly-skill/scripts/io_guard.py record-change \
  --work /app/work --operation manuscript-review --path /app/output/result.json \
  --reason "实际检查后的说明" --evidence-ref ev-1
```

一个合法 early bootstrap stdout 会是类似下面的**示意**结构；具体路径、hash、合同字段和计数来自实际运行：

```json
{
  "ok": true,
  "command": "bootstrap",
  "status": "partial",
  "case_id": "示意ID，原类型保留",
  "reused": false,
  "existence_or_hashing_means_content_reviewed": false,
  "result": "/app/output/result.json",
  "run_record": "/app/output/run_record.json",
  "validation": "/app/output/validation.json",
  "audit_passed": false,
  "checkpoint_eligible": true
}
```

不要把示例输出复制为正式答案。正式输出必须来自真实 case、真实输入检查与真实命令运行。
