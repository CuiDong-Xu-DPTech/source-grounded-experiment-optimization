# `verify_sources.py`：PDF 引文逐字复核

本工具只回答一个窄问题：登记的非空引文字符串，是否是指定 PDF **物理页原生文字抽取结果**中的连续子串（仅折叠空白、保持大小写与 Unicode）。它不是论文真伪检测器、科学支持证明、OCR、视觉版面理解或引用质量评分器，不自动计算科学加分或 benchmark 分数。

## 1. 调用、依赖与退出状态

```bash
python3 /skills/weekly-skill/scripts/verify_sources.py \
  --case /input/case.json \
  --registry /app/work/evidence-registry.json \
  --output /app/output/source-validation.json
```

- 非交互；使用 Python 标准库，并在运行时动态导入已安装的 PyMuPDF (`fitz`)。不会联网、`pip install` 或自动 OCR。缺少/无法导入 PyMuPDF 时进入 `unreadable` 并保持 `status="partial"`。
- JSON 总是严格解析：拒绝重复对象键、`NaN`、`Infinity`、`-Infinity` 和非 UTF-8 输入；没有 `--strict` 宽松模式。`--help` 保持上述同一 CLI。
- 输出不带虚构时间戳，键排序且项目按 `item_id` 排序。case、registry 和实际 PDF 的 SHA256 均来自本轮真实读取。
- 退出码：`0` 仅表示至少一个真实引文检查项且全部实测通过；`1` 表示 mismatch、材料/依赖不可读、未知结构、范围未覆盖或没有真实引文，不能因“没有错误，因为根本没有 quote”而通过；`2` 为 CLI 用法错误；`3` 为报告写入失败。任何退出码都不自动验证科学支持。

## 2. 输入不是单一 Source 列表

### 2.1 PDF source catalog

registry 顶层 `sources` 支持记录列表、以 ID 为键的对象或单条对象，记录使用 `source_id`（映射键或 `id` 可回退）及本地路径。URL 不作为本地 PDF 读取。

本地路径字段为 `path`、`path_or_url`、`file`、`file_path`、`source_path`；它们是同义 alias，不会改变 `source_id`，也不会使任何字段自动成为 Source。实际使用时必须提供至少一个非空字符串；`null` 或空字符串不解析为文件，非字符串、目录和 URL 均拒绝。

同一记录可同时声明多个 alias。工具分别执行 input-prefix 处理、confinement 和 realpath 规范化：**全部指向同一个实际文件时才允许继续**。例如相对写法不同但 realpath 相同是可接受的；`path` 与 `file` 指向不同文件会产生 `conflicting local path declarations`，按 unreadable 处理，不能按声明顺序取 first。

`case.source_materials` 也可为 list 或 dict，但工具会逐项辨识内容，而不是把每个值强转为 PDF Source：

- PDF catalog record：至少有可确定的 `source_id` / `id`（映射键可回退）及实际提供的一个或多个上述路径字段。
- `source_materials.primary`：除显式 `source_id` 可为论文 ID 外，具有 primary 角色。声明的 primary 必须实际存在于 confinement 内的 regular PDF；`provided:false`、缺路径、逃逸路径、冲突路径或非 PDF 均不能假通过。
- case source 若有声明 `sha256`，本轮实际 PDF hash 必须一致，否则该来源 unreadable；结果记录 `declared_sha256_matches`，不以名称和旧元数据替代当前文件。
- registry Evidence/span 使用 `source_id` 或受约束的同一组局部路径 alias。case 同 ID catalog 优先，registry source 用于未由 case 声明的来源；局部路径与声明来源冲突会报 unreadable。非 PDF 图片/文本提示不被这些 alias 放宽。

### 2.2 有意未提供的材料

非 primary 的记录若明确 `provided:false` 且上述路径字段均为 `null`/空串或省略，进入 `material_notes`，保留 `source_note`，不把它误报为一个坏 Source，也不计入引文数。它**不是已提供、已阅读或已验证的来源**；若 Evidence 引用它，仍因无法解析实际 PDF 而失败。

`provided:false` 却有任一路径 alias 属于冲突结构，进入 `unverified_scope`。primary 缺失始终阻塞。未提供 SI、空 Evidence 和只有辅助图片都不能单独产生 verified 状态。

### 2.3 `rendered_pages` 仅是视觉辅助

`source_materials.rendered_pages` 可以是单记录、list 或映射；每个元素分别处理。旧式 catalog list 中 `source_id="rendered_pages"`，以及明确图片路径/视觉辅助 kind，也归入此类。结果写入 `visual_auxiliary_records`：

- 逐个 canonicalize 路径并检查 confinement 内是否实际存在，记录 `exists`、`is_file` 和 `real_path`；只 stat 元数据，不打开 PNG/JPEG/TIFF 做原生抽取、OCR、hash 或引文验证。
- 目录形式的旧 rendered 记录也可如实登记，但不冒充逐页图片。
- 这些记录不计入 `total`、`exact_quote_verified`，不用于 native-text 通过，也不盲目制造 `unverified_scope`。
- 若某条真正 Evidence 用局部 `path` 指向图片或其它非 PDF，仍由 PDF resolver 标为 unreadable；视觉辅助角色不能让它绕过原页文字实测。

### 2.4 case `verified_anchors` 元数据

支持可选的 anchor 元数据，形式为 list、ID 映射或单对象。真正可执行的字段是：

```json
{
  "anchor_id": "A-example",
  "source": "paper-0006",
  "physical_page": "4",
  "location": "Methods below Table 1; human navigation only",
  "exact_quote": "We found the exact words."
}
```

- `source` 是明确的 source/catalog ID，也支持 catalog 映射中的显式别名（例如指向 primary 的 `primary`）。省略 source 时，只有祖先 catalog 中**恰好一个非歧义 primary**才用于回退；不猜测任一路径或唯一图片。
- `physical_page` 必填。允许正 JSON integer，也允许纯十进制正整数字符串（真实 case 的元数据形式），拒绝布尔、浮点、印刷页标签或区间。`location` 仅供人工导航，绝不推导或制造物理页。
- 每个 `exact_quote` 都生成 `provenance_kind="case_verified_anchor"` 的真实检查，仍须实际打开对应 PDF 和物理页并通过同一精确匹配。名字 `verified_anchors`、历史 recheck、转写 note 都不免测，`location` 也不证明语义。
- 重复 anchor ID、缺 quote、未知 source 类型、歧义 primary 或未知嵌套结构会明确进入 `unverified_scope`。列表不会整体 collapsed 成“non-object”。

若 registry 也有该 anchor，避免对同一段新增第二次计数，仅限以下保守关系：

1. 显式相同 `anchor_id` / Evidence ID，并且 source、physical page、原始 quote value 一致；或
2. source + physical page + **原始 quote value** 完全相同的唯一 registry 项。

匹配后执行一次真实 PDF 检查，保留 `provenance_kind="registry_evidence+case_verified_anchor"`，以及 `provenance.registry_evidence_ids`、`case_anchor_ids` 和 `anchor_location`。ID 冲突、多个候选或值不一致不会被静默合并；不同前后缀、相似文字、包含关系、空白之外的变化都不是去重理由。`case_anchor_total`、`case_anchor_merged_total`、`case_anchor_independent_total` 分别记录解析到的 anchors、合并数及独立实测数。

## 3. registry Evidence 与物理页

`evidence` 支持列表、ID 映射或单条对象；每个非空 `quote` 和每个 `spans[].verbatim` 分别是一项检查。span 可覆盖 Evidence 的 `source_id/path/locator/physical_page/page`。`context_note`、人工 paraphrase、section 名称或 locator 本身都不是 PDF quote。

Locator 支持 `main#page=2`、`main#physical_page=2`、`paper#section=Results` 等。这里的 `page=N` 与 `physical_page=N` 明确是 **1-based 物理页**，不是印刷页码；section/location-only 没有物理页时进入 `unverified_scope`。局部字段与 locator 同时出现时必须一致。形如 `paper#section=...` 可以帮 reviewer 找段，但本身不替代 `physical_page`。

**表格、单位和上下文：** 数字 cell、单位、行列标题、脚注、适用实体与反应条件必须保留决定含义的上下文。语义不同的 cell 或条件用不同 Evidence ID，再通过同一 Claim 的 `evidence_refs` 组合。工具只验证片段逐字出现，不证明它们属于同一行、同一实体或支持同一 Claim。

## 4. 来源路径边界

1. 相对路径以 `case.json` 所在目录为根。当该目录名恰为 `input`，原始 `input/primary.pdf` 只去掉一个前导 `input/`；不任意改写绝对路径，也不巡览 input 之外的目录。
2. PDF 和视觉辅助的 canonical realpath（包括符号链接目标）必须仍在 case 输入目录内。`../` 逃逸、指向其它个人文档的符号链接、URL 及未授权绝对路径均拒绝。
3. native-text resolver 只接受实际存在的 regular `.pdf`。找不到 source、PDF、物理页或 hash 不一致保留 unreadable/unknown，不 fallback 成 passed。
4. 工具不把 work/output 或个人目录当来源根目录，输出不覆盖 case 或 registry。

## 5. 精确匹配，不扩成模糊/OCR

页面文字来自 PyMuPDF `Page.get_text("text")`。引文与页面文字分别删除首尾 Unicode 空白，并把连续 Unicode 空白折成一个 ASCII 空格，然后执行大小写敏感的原样子串判断。每项仅限一个指定物理页，不跨 source、页、span 或 Evidence 拼接。

不做小写转换、NFKC/兼容字符折叠、全半角统一、上下标转换、连字符修复、拼写纠错、省略号展开、fuzzy/近似匹配或 OCR。视觉转写中的 `^`、上下标说明等若与原生抽取不同，要如实 mismatch，而不是按注释补字。`We found` 改成 `I found`、`−` 改成 `-`、跳段拼接均会失败。

多个独立连续片段可使用多个 span 和明确物理页；不同事实/条件应使用不同 Evidence ID。原生抽取为空列入 unreadable，人工视觉转写必须另留真实方法，不猜词。

## 6. 输出解释与修复

`total` 是 `verified_items + mismatches + unreadable + unverified_scope`；`exact_quote_verified` 只数真实精确匹配。`material_notes` 和 `visual_auxiliary_records` 不计入该总数，但它们也不会被冒充验证结果。

| 字段 | 含义 | 修复方式 |
|---|---|---|
| `verified_items` | 打开当前授权 PDF 的指定物理页并完成精确子串匹配 | 继续人工核对上下文、实体、单位和 Claim 逻辑 |
| `mismatches` | 页面有原生文字，但完整 quote 不是连续子串 | 复制真实连续原文；独立片段拆 spans/Evidence，不换主语、不改符号、不用省略号补缝 |
| `unreadable` | 来源逃逸/缺失、非 PDF、声明 hash 不符、依赖不可用、页码越界或空文字层等 | 修复授权输入和运行环境；扫描页走独立人工流程并记录方法 |
| `unverified_scope` | 无 quote/evidence、无物理页、去重歧义、未知或冲突的材料结构 | 明确 source、物理页和真实 quote；不支持的结构保留人工复核，不能靠删除结构错误获得通过 |

`exact_text_match_only` 恒为 `true`，`scientific_support_verified` 恒为 `false`。全部文字匹配也不能证明 PDF 真伪、抽取与视觉版面完全一致、表格角色正确、引文没有断章取义或科学 Claim 成立。

**真实 reviewer agreement 是另一道独立门槛：** 领域复核人须实际查看原页及必要的 rendered 辅助，核对实体、单位、极性、表头和 Claim 的逻辑关系，并留下真实 reviewer、时间、范围和结论。工具不会合成 reviewer/agreement，不把 `recheck.passed` 当实测，也不把文字匹配换算为科学 validation、优化结果或任何评分。
