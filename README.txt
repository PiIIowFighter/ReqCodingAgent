ReqCodingAgent：需求工程驱动的编程智能体

代码仓：https://github.com/PiIIowFighter/ReqCodingAgent

本项目不使用 Agent 框架，自行实现模型—工具循环、上下文管理、本地工具、输出解析、预算与停止、错误恢复、验证和 patch 收集。特色是利用冻结需求 Ontology（4 类、11 个槽位）识别信息缺口：明确任务直接执行；模糊任务暂停编码，经 2—3 轮主动访谈和用户确认需求基线后再修改、验证。

环境：Python 3.11、Git、uv；命令隔离与 SWE-bench 评测需 WSL2、Docker。克隆仓库后执行：

    uv sync --frozen

模型配置为 configs/agent/openai-responses.json。PowerShell：

    $env:OPENAI_BASE_URL="https://api.openai.com/v1"
    $env:OPENAI_API_KEY="你的密钥"

GUI：

    .\demo_gui\start_openai.ps1 -Workspace D:\path\to\clean-repo

打开 http://127.0.0.1:8765。可查看需求访谈、维度覆盖、需求基线、执行轨迹、Ontology 和 patch；GUI 会原地修改所选空目录或干净 Git 仓库。

CLI：

    uv run reqagent run --workspace <clean-repo> --task "任务描述" --config configs/agent/openai-responses.json

CLI 默认在隔离副本中执行；仅在明确需要时添加 --in-place。评测入口：uv run evalsys validate-all --help。

三次迭代：①冻结数据、隔离执行和可复现评测；②基础 Coding Agent 的调查、修改、验证与恢复闭环；③加入需求 Ontology、自适应路由和主动澄清，使完整与模糊需求场景均达到 10/12。最终数据见 audit/，完整过程见 Git 历史。
