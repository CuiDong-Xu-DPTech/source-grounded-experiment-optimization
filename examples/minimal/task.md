# SYNTHETIC_INTERFACE_DEMO / EXAMPLE-ONLY

这是自制接口任务，不是正式科研案例或实测答案。只使用本目录的虚拟输入，不访问或推断任何正式案例。

1. 阅读 `material.txt`，引用至少一个带真实 `L<num>` 行号的 locator。
2. 列出三个计划及其各自的 `plan_id/version`；从对应有限集合中选一个时长仅用于演示。
3. 将金额表达为 planning price，不是报价；slots 是未预订的规划情景。
4. 展示 `PLAN-B/1.1` 与 C1 的价格上限冲突。保留 PLAN-C 兼容性材料缺口和所有计划未实验的状态。
5. 响应 `Y` 没有测量，不能编造 `Y=<数值>%`。因此示例结果只可为 partial，不能宣称 winner、全局最优、完全可行或改进分数。
6. 按 `case.json.output_contract` 写一个小型候选对象，含一致的 `case_id`、sources、uncertainty、limitations、change_records 和 `run_record_ref`。静态手写示例用 `run_record_ref=null`，表示它尚不是真实运行输出。
7. 可用根 README 所示 io_guard 流程检查此 synthetic candidate；不得生成或附加伪造的 optimization report。
