# 迭代三：需求增强 Agent

`final-results.json` 是稳定的最终入口：baseline-v3 在完整与模糊需求上均解决 10/12，未解决项均为 T-R2 与 T-S4。`reports/baseline-v3.json` 保留逐任务结果，`reports/comparison-v1-v3.json` 给出 v1→v3 对比；哈希见 `final-results.sha256`。

原实验规范把 E3/E4 标为 fuzzy/full，实际冻结计划标为 full/fuzzy，因此公开结果使用无歧义的 `full`、`fuzzy` 名称，并在 manifest 中同时保留两套标签。baseline-v1 的规范结果为 full 9/12、fuzzy 8/12。

逐次运行和中间 baseline-v2 报告已归档于 Git 历史提交 `a4eac341ae1f59d04b24d992d3df50fc7a7ddb00`；本目录保留最终报告、索引、环境、隔离与测试凭据。
