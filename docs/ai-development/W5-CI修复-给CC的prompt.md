# W5 · CI engine job 修复 · 给 CC 的 prompt

> **先做这一份, 做完再做 `W5-第二段-澄清应答修补-给CC的prompt.md`。**
> 顺序不能反: v1.3 那一轮还会往 `evals/` 里加测试, 目录没按依赖拆开的话
> 新测试会加进错的地方, 要修两遍。

---

## 现象

CI run #14 的 `engine` job (「policy_engine + device-sim (纯函数, 秒级)」)
**16 秒后失败**, 其余三个 job (api / 镜像 / web) 全绿。
16 秒 = 装完依赖立刻挂, 是**收集期导入失败**, 不是测试失败。

## 根因 (已查证)

`scripts/ci/test-unit.sh` 把整个 `evals` 加进了单元档:

```
pytest packages/policy_engine packages/scenario apps/device-sim evals -q
```

而 `evals/` 里有一批依赖 httpx / asyncpg 的代码:

```
evals/runner/cli.py                     import httpx, asyncpg
evals/runner/extract.py                 import asyncpg
evals/runner/client.py                  import httpx
evals/runner/apiproc.py                 import httpx
evals/tests/test_runner_guardrails.py   import httpx
```

`engine` job 只装 `requirements-dev.txt` (ruff / mypy / pytest / pydantic /
PyYAML / types-PyYAML) 加两个 `-e` 包 —— **httpx 与 asyncpg 都没有**。

本机能过, 是因为你的 venv 里装了 apps/api 的全套依赖。
**这是本项目第三次"本机绿、CI 红"** (W1 ruff 版本、W2 mypy 没进 CI、这次依赖分档),
三次共同根因都是**本机环境比 CI 富, 所以本机验证不了 CI**。

## 不要这样修

**不要把 httpx / asyncpg 加进 `requirements-dev.txt`。**
那个 job 的名字就是"纯函数, 秒级、不连库不连网"——给它装一个数据库驱动,
这个名字当场失效, 而"声明的和执行的不是一回事"正是这个项目一路在修的毛病。

## 要这样修

### 1. 按依赖拆开, 而且按**目录**拆不按文件名列清单

- **真正离线的**留在单元档: 数据集 lint、6b、grader 金样、`case_grader`、
  IO 边界、判别性准入 —— 只依赖 `policy_engine` + `scenario` + 固定快照;
- **需要 httpx 或 asyncpg 的**挪到 api 档 (`scripts/ci/test-api.sh`),
  那个 job 本来就起了 Postgres、装了 apps/api 全套依赖。

**必须按目录拆。** 在 `test-unit.sh` 里列一串文件名是不行的 ——
那个脚本自己的注释就写着为什么:

> "packages/scenario 也要列进来: 它有自己的测试, 不列的话以后往那个目录加的
> 测试会静默地永远不执行 —— 和 mypy 检查目标漏加是同一类问题。"

按文件名列清单会把同一个坑再挖一遍。建议 `evals/tests/` (离线) 与
`evals/runner/tests/` (需依赖) 两个目录, 具体怎么分你定, 说明理由。

注意 `test_runner_metrics.py` / `test_runner_aggregate.py` 未必真需要依赖
(metrics / aggregate 看着是纯函数) —— **按实际 import 判断, 不要按文件名猜**。

### 2. 加一条断言, 让这个坑以后自己红

离线那个目录下**不许 import httpx / asyncpg / sqlalchemy / psycopg**。
手法照 `evals/tests/test_grader_io_boundary.py` (AST 扫 import), 它已经在对
`graders/` 做同样的事, 把范围扩到离线测试目录本身即可。

这是本项目第七次"把约定变成 CI 能拦的规则"。

### 3. 把那句注释改准

`test-unit.sh` 里现在写着:

> "evals 同理 (W5): 数据集 lint 与 grader 测试全部离线 (引擎 + 场景装载 +
> 固定快照, 不连库不连网), 归单元档。"

**这句话现在是错的** —— 它描述的是 grader 那一半, 而脚本加进去的是整个 `evals`。
按拆分后的事实改写。**过期注释被当成事实, 是这个项目本月栽过三次的坑**
(sensor 0 的来源、viewer 的来源、变异 21 的手法)。

### 4. 顺带确认 lint 档

`scripts/ci/lint.sh` 的 mypy 目标是不是也把 `evals/runner` 算进了不装依赖的档?
是的话一并处理 (mypy 遇到装不上的 import 通常只是 warning 而不是 error,
所以它可能正悄悄地什么都没检查 —— 那又是一个"看起来在守、实际守空气"的东西)。

---

## 验证方式 (这一条最要紧)

**用一个干净的临时 venv 复现 engine job, 不要用你现在的 venv。**

```bash
python3.12 -m venv /tmp/ci-repro && source /tmp/ci-repro/bin/activate
pip install -r requirements-dev.txt
pip install -e packages/policy_engine -e packages/scenario
bash scripts/ci/lint.sh && bash scripts/ci/test-unit.sh
deactivate
```

**用现有 venv 验证等于没验证** —— 那正是这次没抓到的原因。
临时 venv 建在 `/tmp` 可以 (它不是交付物, 是一次性探针), 但**不要在里面产生
任何要保留的文件**; 需要留下的脚本仍然放 `scripts/dev/`。

---

## 报告要写的

1. 拆分方案与依据 (哪些文件实际 import 了什么, **不要按文件名猜**);
2. 干净 venv 里 `lint.sh` + `test-unit.sh` 的完整输出;
3. 那条新断言的**变异测试**: 往离线目录塞一个 `import httpx` 的测试文件 →
   断言必须红; 还原后复绿;
4. 与 SPEC / prompt 不一致的地方; 自行新增的分支由哪条测试守着。

---

## 顺带记一笔

修完把这一条写进 `docs/ai-development/defect-log.md`:

> **"本机绿、CI 红"在本项目是第三次**。三次的形状一样:
> **本机环境比 CI 富, 所以本机跑绿证明不了 CI 会绿。**
> W1 是 ruff 版本 (本机旧、CI 装最新)、W2 是 mypy 根本没进 CI、
> 这次是依赖分档 (本机 venv 有 apps/api 全套依赖)。
> 对策不是"每次记得多跑一遍", 是**让本机能复现 CI 的环境** ——
> 这次给出的干净 venv 复现步骤应该固化成一条 make 目标。

**建议同时加一条 `make ci-unit-repro`** 把上面那段 venv 复现固化下来,
免得下一个人还得从 defect-log 里翻命令。这一条你判断值不值, 值就做, 不值就说理由。

---

## 老规矩

- **git 一律不执行**; `checkout` 不算只读。
- 临时脚本放 `scripts/dev/`; 密钥只进 `.env`。
- 改完列文件 + 建议 commit message, 由本人 commit 并 push。
- 这一份做完、CI 绿了之后, 再开始
  `docs/ai-development/W5-第二段-澄清应答修补-给CC的prompt.md`。
