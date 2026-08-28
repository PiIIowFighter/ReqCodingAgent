ReqCodingAgent 第一次迭代检查点
仓库：https://github.com/PiIIowFighter/ReqCodingAgent

本阶段建立冻结、可复现、可恢复的 SWE-bench Verified/Lite 评测环境：固定三项上游版本，严格校验 15 题配对、哈希与分布；用官方 harness 逐项判定 no-op/gold；保存结构化日志、确定性报告和脱敏审计；以宿主及容器探针证明 Agent 看不到 gold patch、test patch、hints 与 Oracle。

需 Python 3.11。Windows 必须启用 WSL2，并使用 Linux Docker Engine；缓存应放在项目目录外。

常用命令：
python -m evalsys.cli preflight
python -m evalsys.cli prepare-data
python -m evalsys.cli validate-data
python -m evalsys.cli replay --mode noop --split all
python -m evalsys.cli replay --mode gold --split all
python -m evalsys.cli validate-all --task-repo TASK_REPO --isolation-workspace WORKSPACE
python -m evalsys.cli report RUN_DIRECTORY

恢复时为 validate-all 提供 --resume --run-id，并可用 --noop-run-id/--gold-run-id 指定已有子运行。只有实际 30 次 replay 全部通过且第19节全部验收后，迭代一才算完成；当前检查点不作完成声明。
