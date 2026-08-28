# 第一次迭代可复现评测环境设计

## 1. 目标与边界

本迭代只建立冻结、可复现、可追溯的 SWE-bench 外部评测环境。实现数据准备与哈希校验、严格模式、Docker/WSL2 预检、官方 harness 适配、15 题 no-op 与 gold replay、恢复、结构化日志、报告、公开脱敏审计和可执行隔离证明。

本迭代不实现 Coding Agent、LLM 调用、自动澄清、上下文/工具循环、OntoAgent 算法或 E1-E4 正式实验。

唯一实施基线是 `计划/第一次迭代-可验证评测环境实施规范.md` 1.1。不得修改 `资料/` 或 `计划/` 的已有文件，不得改变 12 个 test 配对样本、3 个 dev 配对样本、模糊文本、Oracle、评分口径或三个固定上游提交。

## 2. 架构

采用“固定上游 checkout + 薄 Python 适配层”：

1. 项目仓库只保存评测系统源码、严格 schema、冻结的小型 manifest、私有 evaluator 元数据、测试和脱敏审计结果。
2. SWE-bench 官方仓库与 Hugging Face 数据缓存位于项目根目录之外的可配置缓存根目录；不得形成嵌套 Git 仓库。
3. 所有三个上游对象都按锁定提交验证：SWE-bench `7a21e05772954cc81471ae19d56f436cecf43c54`、Verified `78f471bf655a3137b2e8a75af1501690ec009ec3`、Lite `b0dde1093fe417d83b7184254edf8199c1f0dff5`。
4. 运行时分别读取固定版 Verified 和 Lite，证明 15 个 instance_id 同时存在；完整任务字段只取自固定 Verified。
5. replay 调用固定提交的官方 SWE-bench harness，但由本项目负责空补丁 no-op 的显式执行、逐项测试结果校验、状态分类、恢复和审计。

## 3. 数据与模式

`benchmark/source-lock.json` 保存三个固定提交与来源 URL。`benchmark/manifests/paired-cases.jsonl` 保存每题的 full/fuzzy 公共记录，`benchmark/private/oracles.jsonl` 保存 fuzzy Oracle，变换规范保存为冻结的确定性数据。

`prepare-data` 从固定 Verified 数据中提取 15 个实例，逐字验证 `problem_statement` 的 UTF-8 SHA-256，并核对它们全部属于固定 Lite。生成 full prompt 时逐字复制；fuzzy prompt 严格按冻结条目确定性变换。生成后计算 prompt 哈希。

所有 JSON/JSONL 均使用 `additionalProperties: false` 的 JSON Schema。跨记录验证另外检查：12/3 配对数量、4/4/4 与 1/1/1 分布、允许列表、pair_id 唯一、full/fuzzy 官方字段和测试集合一致、证据来自原题、ontology_mapping 可空。

## 4. Preflight 与跨平台

Python 基线为 3.11。Windows 通过 Ubuntu WSL2 使用 Linux Docker Engine。路径由 `pathlib` 处理，子进程全部使用参数数组，不拼接 shell 命令。

preflight 检查 Python、Git、WSL2、Docker server 的 Linux 架构、外部缓存目录、三个固定来源可达性和磁盘空间。它还创建一个带中文与空格名称的临时目录，实际 bind-mount 到 Docker 容器，完成宿主到容器读取及容器到宿主写回验证。失败必须返回可操作的阻塞信息。

## 5. Replay、逐项判定与资源边界

每个任务和模式都从干净环境开始。no-op 不依赖官方 harness 对空 prediction 的默认行为：适配层必须确保官方测试真实启动，并解析每个 FAIL_TO_PASS/PASS_TO_PASS 测试的状态。no-op 通过条件为所有 F2P 失败且所有 P2P 通过；gold 通过条件为全部 F2P/P2P 通过。不能只依据进程退出码或顶层 resolved 字段。

每个 replay 记录环境准备、补丁应用、测试阶段、逐项测试状态、镜像、开始/结束时间、wall time、版本和日志相对路径。最终状态严格为 `passed`、`test_failed`、`infra_failed`、`timeout` 或 `invalid`。

每次运行的 Docker 容器/资源都带 `run_id` 标签。超时终止整个子进程组，并只枚举和清理带该 run_id 标签的资源；禁止 `docker system prune`、全局容器清理或其他全局破坏操作。

## 6. 恢复与完整性

可恢复单元为单个 instance/mode。复用要求同时满足：

- 输入指纹一致；
- 原子完成标记存在；
- 结果通过严格 schema；
- stdout、stderr、逐项结果等关键产物存在且 SHA-256 与完成标记一致；
- 结果状态为已完成的终态。

任一条件失败即拒绝复用并重跑。所有结果先写临时文件、同步后原子替换，防止中断留下伪完成状态。

## 7. Agent/evaluator 隔离

工作区构造器只把干净任务仓库和单一公共 prompt variant 复制/挂载到 Agent 根目录；不挂载项目根目录。`benchmark/private`、gold patch、test patch、hints、`计划`、`资料`、evaluator 日志与缓存均不进入工作区。

隔离证明必须来自实际构造：创建带诱饵私有文件的 evaluator fixture，构造 Agent 工作区，再从宿主和容器内执行负向探测，证明禁止路径和诱饵内容不可见，同时证明允许的 prompt 与任务文件可见。静态 allowlist 检查仅作为补充。

## 8. 报告与审计

完整运行产物保存在被 Git 忽略的 `artifacts/`。报告器生成机器可读 JSON/JSONL、Markdown 汇总和 15 题 no-op/gold 矩阵。

`audit/iteration1/` 由实际运行结果生成：

- `validation-summary.json`
- `noop-gold-matrix.md`
- `run-manifest.json`
- `test-summary.txt`
- 可执行隔离证明摘要

公开文件只含相对路径、版本、输入/结果哈希、状态和短错误分类；自动扫描 API key、SSH 信息、环境变量、用户绝对路径和大段日志。

## 9. 测试与提交门禁

按 TDD 实施，覆盖实施规范第 18 节全部自动检查以及外部 checkout、Lite 交集、空补丁测试执行、逐项判定、恢复完整性、进程组超时、run_id 定向清理、中文空格 bind-mount 和可执行隔离负向测试。

`validate-all` 依次执行 preflight、三锁验证、数据准备/缓存验证、数据校验、no-op、gold、聚合、审计和最终门禁，并支持恢复。

达到内部一致且具有对应测试证据的检查点后，可以创建并普通推送职责单一的中间提交；不得用中间提交声称迭代一已经完成。第 19 节全部满足且 30 次 replay 与最终审计摘要完成后，才可将迭代一标记为完成。

每次提交后执行普通 push，禁止 force push 或改写已推送历史，并核对远程 `main` 与本地 `HEAD` 哈希一致。若 replay 或基础设施未满足最终验收，保留不可覆盖的实现和运行证据，明确报告未完成状态。
