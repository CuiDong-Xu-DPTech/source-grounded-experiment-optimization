# SYNTHETIC_INTERFACE_DEMO / EXAMPLE-ONLY

本目录是完全自制的中性虚拟接口示例，**不是三个正式案例之一、不是实测答案、不是优化报告，也没有经过 DeepSeek Harness**。所有 planning price、预算和 slot 都是规划情景，不是供应商报价、采购承诺或已完成的预订。所有候选均未实验，不能由此推断任何 `Y` 百分比，也不能宣称接口演示之外的实际可行性。

## 文件

- `case.json`：虚拟 case，含一个简单、闭合的 JSON Schema `output_contract`。
- `task.md`：只要求演示证据定位、候选版本、有限选择、冲突和 unknown。
- `material.txt`：虚拟材料；引用时使用真实 `L<num>` 行号。三个计划版本是 `PLAN-A/1.0`、`PLAN-B/1.1`、`PLAN-C/2.0`，不是正式 case 版本。
- `illustrative-output.json`：小型手写示意结果，`case_id` 与 case 相同。`status=partial` 和 `run_record_ref=null` 明确说明它不是工具实际产生的运行记录；真实执行后才应将 ref 指到实际 run record。

刻意演示的边界：`PLAN-B/1.1` 的 planning price 超过 C1 上限；`PLAN-C/2.0` 缺兼容性材料；所有响应都未测。示例不作总体最优或无可行解证明，不随包提供正式 `optimization_request.json`、`output.json` 或 `report.json`。

## 隔离调用示例

以下命令使用已安装 Skill 的容器绝对路径。仅可在隔离的演示工作区运行，不能覆盖真实 `/input` 或正式 `/app/output/result.json`。演示输出另放在 `/app/work/SYNTHETIC_INTERFACE_DEMO/` 下。

```bash
IO=/skills/weekly-skill/scripts/io_guard.py
CASE=/skills/weekly-skill/examples/minimal/case.json
TASK=/skills/weekly-skill/examples/minimal/task.md
WORK=/app/work/SYNTHETIC_INTERFACE_DEMO/work
OUT=/app/work/SYNTHETIC_INTERFACE_DEMO/output

python3 "$IO" bootstrap \
  --case "$CASE" --work "$WORK" --output "$OUT" --task "$TASK"

cp /skills/weekly-skill/examples/minimal/illustrative-output.json \
  "$WORK/candidate.json"

# 将静态示意候选交给真实保护流程；这只是 CLI 演示，不是实验或正式案例运行。
python3 "$IO" checkpoint \
  --case "$CASE" --candidate "$WORK/candidate.json" \
  --work "$WORK" --output "$OUT"

python3 "$IO" finalize \
  --case "$CASE" --work "$WORK" --output "$OUT"

python3 -m json.tool /skills/weekly-skill/examples/minimal/case.json >/dev/null
python3 -m json.tool /skills/weekly-skill/examples/minimal/illustrative-output.json >/dev/null
```

`checkpoint`/`finalize` 可接受带具体 uncertainty/limitations 的合法 partial；不会因此把它变成科学或 Schema 上已完成的结论。若环境没有 `jsonschema`，真实校验报告必须为 `executed=false`，不能声称 schema-valid。若这些路径已有其他 case 的文件，bootstrap 应按合同冲突处理；换一个新的隔离目录，不使用强制覆盖，也不删除正式结果。

正式 runtime 请改读真实 `/input/case.json`、`/input/task.md` 和实际材料，在 `/app/work` 工作，向 `/app/output` 交付。需要优化时另按真实材料生成 canonical request，并以绝对路径调用 `python3 /skills/weekly-skill/scripts/optimize.py --input ... --output ...`；只有该命令实际生成的报告才能经 `--optimization-report` 传给 checkpoint/finalize。
