ReqCodingAgent：面向需求理解与可验证演进的编程智能体

代码仓：https://github.com/PiIIowFighter/ReqCodingAgent

本项目未使用 Agent 框架，独立实现模型—工具循环、上下文管理、工具解析与执行、预算和停止策略、错误恢复、checkpoint、验证及 patch 收集；模型服务仅通过厂商客户端或 OpenAI 兼容接口接入。

一、环境与安装

需要 Python 3.11、Git 和 uv；实时命令验证需要 Docker。克隆仓库后运行：

    git clone https://github.com/PiIIowFighter/ReqCodingAgent.git
    cd ReqCodingAgent
    py -3.11 -m pip install uv
    uv sync --frozen

二、模型配置

演示配置为 configs/agent/demo-openai.json，默认采用 OpenAI Responses 协议和 gpt-4o-mini。PowerShell 设置：

    $env:OPENAI_BASE_URL="https://api.openai.com/v1"
    $env:OPENAI_API_KEY="你的密钥"

使用其他兼容网关时，修改配置中的 model、protocol、base_url_env、api_key_env，并设置对应环境变量。可先执行：

    uv run reqagent doctor --config configs/agent/demo-openai.json --live

三、GUI 启动

Windows PowerShell：

    .\demo_gui\start_openai.ps1 -Workspace D:\path\to\workspace

打开 http://127.0.0.1:8765。GUI 支持多轮需求访谈、需求基线确认、需求维度覆盖、执行轨迹、Ontology 浏览及 patch 预览/下载。GUI 会原地修改所选目录，请使用空目录或干净 Git 仓库根目录。

四、CLI 启动

先设置 OPENAI_BASE_URL 和 OPENAI_API_KEY，再运行：

    uv run reqagent run --workspace <clean-repo> --task "任务描述" --config configs/agent/demo-openai.json

CLI 默认在隔离副本中执行并保存 artifacts；仅在明确需要时添加 --in-place。可用 --task-file 读取较长任务，用 resume 恢复支持的未完成运行。

五、三次迭代

迭代一先建立可复现评测环境：以 SWE-bench 完整题面为 Original，并借鉴 FallibleUser 构造省略、指代歧义和具体性降低的模糊需求，回答“如何证明 Agent 变强”。迭代二实现基础 Coding Agent，包括仓库调查、文件读写、补丁修改、隔离命令、测试验证、上下文压缩和终止控制。迭代三引入冻结的需求 Ontology 和自适应路由：面对模糊任务先暂停编码，围绕变更意图、代码范围、约束和验证进行主动多轮澄清，经用户确认需求基线后再执行，从“直接生成代码”推进为“需求工程驱动的软件开发”。需求增强后，完整与模糊需求场景均达到 10/12；详细口径与可追溯证据见 audit/。
