ReqCodingAgent 迭代一 v1.3
仓库：https://github.com/PiIIowFighter/ReqCodingAgent

本阶段提供冻结的 15 题 SWE-bench 配对数据、官方 harness replay、Agent/evaluator 隔离和可审计结果。需 Python 3.11；Windows 使用 WSL2 与 Linux Docker Engine，缓存放在项目外。在 replay 使用的同一默认 Ubuntu distribution 中，于固定 checkout 执行 `uv sync --locked --python python3.11`；先验证该 checkout 的 `.venv/bin/python`，再将 `EVALSYS_WSL_PYTHON` 指向它。

默认快速测试：
pytest -m "not integration"

离线数据准备与校验：
python -m evalsys.cli prepare-data
python -m evalsys.cli validate-data

Docker 健康且任务镜像、probe 镜像均已在本地时，仅运行开发题 django__django-11133：
python -m evalsys.cli replay --mode noop --split dev --instance-id django__django-11133
python -m evalsys.cli replay --mode gold --split dev --instance-id django__django-11133
python -m evalsys.cli smoke-report --noop-run NOOP_RUN --gold-run GOLD_RUN

迭代一强制验收为静态 15 题校验、默认快速测试、一次真实隔离证明及上述 1×2 smoke。15×2 replay 与 validate-all 保留为 optional full profile，不是完成门槛。integration 显式运行：pytest -m integration。