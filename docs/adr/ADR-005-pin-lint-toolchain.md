# ADR-005: lint 工具锁版本 + 规则显式声明

日期: 2026-08 | 状态: 已接受

## 背景

W1 收尾时 CI 的 `engine` job 变红, 而同一份代码在开发机上 `All checks passed`。
根因: CI 用 `pip install ruff` 装的是当时的最新版, 开发机上是几周前装的旧版;
ruff 0.16 扩大了"默认规则集", 于是同一份代码被新版判出 11 处问题。
代码没变、提交没变, CI 结果却变了 —— 构建不可重现。

## 决策

1. **锁死版本**: CI 与 `apps/api/pyproject.toml` 的 dev 依赖统一写 `ruff==0.16.1`。
   升级 ruff 变成一次独立的、可 review 的提交。
2. **显式声明规则集**: 仓库根新增 `ruff.toml`, `[lint] select` 明确列出
   `E/W/F/I/UP/B/SIM/RUF`, 不再依赖"默认规则"。默认值属于工具的实现细节, 不是契约。
3. **单一配置源**: 删掉 `apps/api/pyproject.toml` 里的 `[tool.ruff]`。
   ruff 按"离文件最近的配置"生效, 分散配置会让不同目录悄悄用不同规则。
4. 中文注释触发的 `RUF001/002/003`(全角标点视为易混淆字符) 整体 ignore。

5. **CI 步骤下沉成脚本**: `.github/workflows/ci.yml` 里不再写具体命令, 只做
   "准备环境 + `bash scripts/ci/xxx.sh`"。命令写在 workflow 里就只能在 GitHub 上跑,
   本机无法复现 —— 而本次事故的性质正是"两边执行的东西不一致"。
   脚本用环境变量 `CI` 区分两种模式: CI 上自己装锁定版本的依赖,
   本机上不碰用户环境、改为先跑 `check-tool-versions.sh` 校验版本一致再执行。
   `make lint` / `make test` 与 CI 从此调用同一份文件。
6. **工具版本单一事实源**: 根目录 `requirements-dev.txt`。只有 ruff 锁到补丁版本 ——
   lint 工具会对没改过的代码给出不同结论, 测试框架不会, 后者封主版本即可。

## 代价与边界

锁版本意味着不会自动获得新规则, 需要定期主动升级。这是刻意的取舍:
宁可显式承担"升级成本", 也不接受"某天早上 CI 无故变红"。
同一原则适用于后续引入的 mypy / eslint。
