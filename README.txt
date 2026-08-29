ReqCodingAgent 迭代二实现检查点
仓库：https://github.com/PiIIowFighter/ReqCodingAgent

项目包含冻结的迭代一 SWE-bench 评测环境，以及独立的基础 Coding Agent 包 reqagent。基础 Agent 自行维护模型—工具—反馈循环，提供 list_files、read_file、search_text、apply_patch、run_command、submit 六个本地工具；默认在干净 Git 仓库的隔离副本中工作，实施路径保护、预算、重复动作检测、上下文压缩、checkpoint/resume 和最终 patch 收集。

环境要求：Python 3.11。安装后可用命令行入口，也可直接运行模块。

离线配置检查：
python -m reqagent.cli doctor --config configs/agent/offline-scripted.json

查看命令：
python -m reqagent.cli --help
python -m reqagent.cli run --help
python -m reqagent.cli resume --help

离线手工运行：
python -m reqagent.cli run --workspace CLEAN_GIT_REPO --task "任务文本" --config CONFIG.json

默认快速测试：
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -m "not integration"

live-template.json 仅是显式占位模板。doctor --live 会在 provider、协议、模型、endpoint 环境变量或凭据未确认时拒绝，并且不会发起网络请求。当前未实现或运行真实模型 API、SWE-bench 开发/正式矩阵、E1/E2、baseline 冻结或第三次迭代澄清功能。evalsys 中的新入口保持关闭，只有配置、确认和冻结证据完备后才能开放。

迭代一的数据准备、replay、smoke-report 和 validate-all 命令仍保留；完整用法见计划与 audit。integration 测试需显式运行：pytest -m integration。
