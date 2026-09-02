ReqCodingAgent：需求工程驱动的编程智能体

代码仓：https://github.com/PiIIowFighter/ReqCodingAgent

本项目不使用 Agent 框架，自行实现模型—工具循环、上下文、本地工具、输出解析、终止与恢复、验证及 patch 收集。冻结需求 Ontology 含 4 类 11 槽位：明确任务直接执行；模糊任务暂停编码，经 2—3 轮访谈并确认需求基线后实施。

环境：Python 3.11、Git、uv；隔离与评测另需 WSL2、Docker。安装：

    uv sync --frozen

配置：configs/agent/openai-responses.json。PowerShell：

    $env:OPENAI_BASE_URL="https://api.openai.com/v1"
    $env:OPENAI_API_KEY="你的密钥"

GUI：

    .\demo_gui\start_openai.ps1 -Workspace D:\path\to\clean-repo

打开 http://127.0.0.1:8765，可查看访谈、维度覆盖、需求基线、执行轨迹、Ontology 及 patch；目标须为空目录或干净 Git 仓库。

CLI：

    uv run reqagent run --workspace <clean-repo> --task "任务描述" --config configs/agent/openai-responses.json

CLI 默认在隔离副本执行；原地执行加 --in-place。

三次迭代：①冻结数据、隔离执行和可复现评测；②实现基础 Coding Agent 的调查、修改、验证与恢复闭环；③加入需求 Ontology、自适应路由和主动澄清。

12 组冻结任务结果：基础版完整/模糊需求为 9/12（75.0%）、8/12（66.7%）；增强版均为 10/12（83.3%）。完整场景多解 1 题，提升 8.3 个百分点（相对 11.1%）；模糊场景多解 2 题，提升 16.7 个百分点（相对 25.0%）。最终数据见 audit/，过程见 Git 历史。
