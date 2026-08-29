ReqCodingAgent 迭代二实现检查点
仓库：https://github.com/PiIIowFighter/ReqCodingAgent

项目保留迭代一 SWE-bench 环境，并提供独立基础 Coding Agent 包 reqagent。它维护模型—工具循环，含六个本地工具、路径保护、预算、重复检测、上下文压缩、checkpoint/resume 和 patch 收集。

环境：Python 3.11；迭代一 Docker 流程使用 WSL2。

离线配置检查：
python -m reqagent.cli doctor --config configs/agent/offline-scripted.json

查看命令：
python -m reqagent.cli --help
python -m reqagent.cli run --help
python -m reqagent.cli resume --help

离线手工运行：
python -m reqagent.cli run --workspace CLEAN_GIT_REPO --task "任务文本" --config CONFIG.json

快速测试：
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -m "not integration"

live-template.json 仅为占位模板；doctor --live 在 provider、协议、模型、endpoint 或凭据未确认时拒绝且不联网。生产 run_command 在隔离执行器未注入时 fail closed，不回退宿主 shell。

真实模型 API、Docker smoke、开发集、E1/E2 和 baseline freeze 均未完成。迭代一 smoke-report 保留；15×2 replay 与 validate-all 仍是 optional full profile。integration 测试须显式运行：pytest -m integration。
