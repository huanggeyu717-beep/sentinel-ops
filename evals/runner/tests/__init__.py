"""runner 里**需要第三方依赖**的那部分测试 (httpx / asyncpg)。

为什么与 `evals/tests/` 分开: 分界线是**装得上什么**, 不是**测的是什么**。
`evals/tests/` 跑在 CI 的 engine job 里 —— 那个 job 只装 requirements-dev.txt
(ruff / mypy / pytest / pydantic / PyYAML) 加两个 -e 包, 没有 httpx 也没有 asyncpg,
往那里放一个 import httpx 的测试会让整个 job 在**收集期**就挂掉 (W5 就是这么红的)。

所以按依赖归档: 本目录归 api job (它本来就装了 apps/api 全套依赖并起了 Postgres),
`evals/tests/` 归单元档。runner 里那些**不碰网络也不碰库**的模块
(metrics / aggregate) 的测试仍然住在 `evals/tests/` —— 按实际 import 判, 不按文件名猜。

边界由 `evals/tests/test_grader_io_boundary.py` 的传递 import 断言守着:
`evals/tests/` 下任何一条 (含它 import 到的 evals 模块) 碰了单元档装不到的东西, 当场红。
"""
