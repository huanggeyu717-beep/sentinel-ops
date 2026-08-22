"""消融 runner (SPEC-007 第二段): 走 HTTP 驱动真实 API, 结果归档进 evals/runs/。

模块分工:
- arms       五个臂的配置矩阵 (模型/思考/超时/样本/预估), 单一事实源是 app.config
- sampling   C2 的确定性抽样 (core 40 按类别等比取 20)
- extract    评测库重置 + 从库里提取 CaseOutcome 与计量 (grader 不连库, 这里连)
- apiproc    每臂一个独立 API 子进程 (uvicorn), 迁移与种子由它的启动流程自己跑
- client     HTTP 客户端 (登录/建任务/自动回答澄清/轮询) + 并发编排 + 成本护栏
- grading    事件装载缓存 + 调 evals.graders 判分 (判分逻辑全部在 graders, 不在这)
- metrics    五个指标函数, 吃 results.jsonl 行; 缺配置快照字段直接报错 (验收 1/2)
- archive    manifest / results.jsonl / summary.md / mutants 归档 + COST.md 流水
- cli        入口: --dry-run 先行, 真花钱模式必须命令行显式给出
"""

# 数据集版本 (随 build_manifest 进每份归档的 dataset_version)。放在包级而不是
# archive.py 里, 是 import 分档所迫: engine 档的 evals/tests/test_dataset_lint
# 要拿它与数据集 README 对钉, 而 archive 自 W5 第四批起 import arms (门槛措辞的
# 唯一事实源), arms 的链上静态可达 app.config —— engine job 装不到那套依赖,
# evals/tests 的 import 边界护栏 (test_grader_io_boundary) 会红。
DATASET_VERSION = "v1.3"
