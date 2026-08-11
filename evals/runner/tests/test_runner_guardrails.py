"""成本护栏与抽样: dry-run 零网络 (验收 18)、超限当场停且已完成部分归档 (验收 19)、
C2 确定性抽样 (SPEC-007 第四节)。运行时不连库不连网 (httpx 走 MockTransport),
但 import 链上有 httpx 与 asyncpg —— 所以住 evals/runner/tests/ 归 api 档, 见本包
__init__ 的说明。"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from evals.runner import apiproc, archive, cli
from evals.runner.client import CostLedger, run_cases
from evals.runner.sampling import C2_TARGET, sample_c2

# evals/runner/tests/ -> evals/  (parents: [0]=tests [1]=runner [2]=evals)
EVALS = Path(__file__).resolve().parents[2]
DATASET = EVALS / "datasets" / "policies_v1.jsonl"


def load_cases():
    return [json.loads(line) for line in DATASET.read_text().splitlines() if line.strip()]


# ===== 验收 18: dry-run 零网络请求 =====


def test_dry_run__zero_network_requests(monkeypatch, capsys):
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(500)

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def counting_async_client(*args, **kwargs):
        kwargs["transport"] = transport  # 任何被创建的客户端都只能打到计数器上
        return real_async_client(*args, **kwargs)

    def no_subprocess(*args, **kwargs):
        raise AssertionError("dry-run 不许拉起任何子进程")

    def no_sync_get(*args, **kwargs):
        raise AssertionError("dry-run 不许发同步请求")

    monkeypatch.setattr(httpx, "AsyncClient", counting_async_client)
    monkeypatch.setattr(httpx, "get", no_sync_get)
    monkeypatch.setattr(apiproc.subprocess, "Popen", no_subprocess)

    rc = cli.main(["--arm", "L0", "--mode", "record", "--dry-run"])

    assert rc == 0
    assert requests == []  # MockTransport 一次都没被打到
    out = capsys.readouterr().out
    assert "预估" in out and "配置快照" in out


def test_record_without_max_cost__refused(monkeypatch, capsys):
    # 不给 --max-cost-cny 的 record 真跑必须被 argparse error 挡下 (退出码 2)
    with pytest.raises(SystemExit):
        cli.main(["--arm", "L0", "--mode", "record"])


# ===== --cases-file 的两条中止路径 =====
#
# 两条都是"不许静默少跑几条"的同一条规矩 (SPEC-007 第五节, 与"回放 miss 不许静默
# 跳过"同源): **少跑几条的冒烟照样是绿的, 而它守的东西已经没了。** 在此之前这两条
# 分支没有任何测试守着。


def _cases_file(tmp_path: Path, case_ids: list[str]) -> Path:
    path = tmp_path / "cases.json"
    path.write_text(json.dumps(
        {"run_id": "t", "dataset_version": "v1.3", "case_ids": case_ids}
    ))
    return path


def test_cases_file__unknown_id_aborts_instead_of_running_fewer(tmp_path, capsys):
    """清单指向数据集里已经不存在的 id -> 当场 parser.error (退出码 2), 不静默少跑。"""
    path = _cases_file(tmp_path, ["simple-001", "ghost-999"])

    with pytest.raises(SystemExit) as exc:
        cli.main(["--arm", "L0", "--mode", "replay", "--cases-file", str(path)])

    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "ghost-999" in err  # 报出**是哪条**不存在, 不是一句"清单有问题"
    assert "simple-001" not in err  # 存在的那条不进错误信息


def test_cases_file__empty_list_aborts(tmp_path):
    """清单为空 -> 中止。空集上跑什么都会绿, 是最不该放过的一种"通过"。

    走 cli.main 而不是直接调 load_case_ids: 要守的是"这条清单喂进 runner 会不会
    真的把它拦下来", 不是那个函数自己会不会抛。
    """
    path = _cases_file(tmp_path, [])

    with pytest.raises(SystemExit) as exc:
        cli.main(["--arm", "L0", "--mode", "replay", "--cases-file", str(path)])

    assert "空" in str(exc.value.code)  # 中止理由随退出一起报出来


# ===== 验收 19: 超限当场停, 已完成部分归档 =====


def test_budget_exceeded__stops_new_cases_keeps_completed(tmp_path):
    ledger = CostLedger(limit_cny=2.5)
    cases = [{"id": f"case-{i:02d}"} for i in range(10)]

    async def run_one(case):
        ledger.add(1.0)  # 每条 ¥1
        return {"case_id": case["id"]}

    results, skipped, errored = asyncio.run(
        run_cases(cases, run_one, concurrency=1, ledger=ledger)
    )
    # 第 3 条结束后累计 3.0 > 2.5 -> 之后的全部停发; 已完成的 3 条保留
    assert len(results) == 3
    assert len(skipped) == 7
    assert not errored
    assert ledger.exceeded

    # 已完成部分照常归档 (不是丢弃): write_run 对部分结果照写
    run_dir = tmp_path / "run"
    manifest = {"aborted_by_budget": True, "run_id": "t"}
    rows = [{"case_id": cid} for cid in results]
    archive.write_run(run_dir, manifest, rows, "# partial\n")
    assert (run_dir / "manifest.json").exists()
    archived = (run_dir / "results.jsonl").read_text().strip().splitlines()
    assert len(archived) == 3


def test_budget_not_set__never_stops():
    ledger = CostLedger(limit_cny=None)
    ledger.add(10_000)
    assert not ledger.exceeded


# ===== C2 抽样: 确定性 + 配比 =====


def test_sample_c2__exactly_twenty_from_core_by_category():
    cases = load_cases()
    picked = sample_c2(cases)
    assert len(picked) == C2_TARGET
    assert all(c["core"] for c in picked)
    by_cat = {}
    for c in picked:
        by_cat[c["category"]] = by_cat.get(c["category"], 0) + 1
    # core 40 (7/6/5/4/2/3/3/10) 减半 + 最大余数法 (并列按类别名字典序):
    # ambiguous 与 capability_gap 拿到进位
    assert by_cat == {
        "simple": 3, "combo": 3, "ambiguous": 3, "illegal": 2,
        "repairable": 1, "capability_gap": 2, "tool_fault": 1,
        "prompt_injection": 5,
    }


def test_sample_c2__deterministic_and_id_ordered():
    cases = load_cases()
    first = [c["id"] for c in sample_c2(cases)]
    second = [c["id"] for c in sample_c2(list(reversed(cases)))]
    assert first == second  # 输入顺序打乱, 抽样不变
    # 类别内取 id 字典序前 N
    simple_ids = [i for i in first if i.startswith("simple-")]
    assert simple_ids == sorted(simple_ids)


# ===== 故障注入表生成 =====


def test_fault_entries__only_inject_cases():
    cases = load_cases()
    entries = cli.fault_entries(cases)
    assert len(entries) == 8  # tool_fault 8 条, 其余用例不进表
    assert {e["fault"] for e in entries} == {"timeout_once", "unretryable"}


# ===== 注入按用例激活窗口 (L0 停顿点定的改法) =====


def test_fault_window__entry_only_while_active(tmp_path):
    window = cli.FaultWindow(tmp_path / "active.json")
    case = {"id": "fault-001", "input": "同一句话",
            "inject": {"tool": "list_zones", "fault": "timeout_once"}}

    async def go():
        assert json.loads((tmp_path / "active.json").read_text()) == []
        await window.activate(case)
        entries = json.loads((tmp_path / "active.json").read_text())
        assert entries == [{"case_id": "fault-001", "input": "同一句话",
                            "tool": "list_zones", "fault": "timeout_once"}]
        await window.deactivate("fault-001")
        assert json.loads((tmp_path / "active.json").read_text()) == []

    asyncio.run(go())


def test_fault_window__same_text_cases_share_one_lock(tmp_path):
    window = cli.FaultWindow(tmp_path / "active.json")
    lock_a = window.text_lock("后场湿了  给经理发邮件")
    lock_b = window.text_lock("后场湿了 给经理发邮件")  # 空白差异归一化后同键
    assert lock_a is lock_b
    assert window.text_lock("另一句话") is not lock_a


# ===== 注入未生效断言 =====


def _fault_case(tool="list_zones", fault="timeout_once"):
    return {"id": "fault-x", "input": "x", "inject": {"tool": tool, "fault": fault}}


def test_inject_effectiveness__timeout_needs_retry_trace():
    fired = {"task": {"error_detail": None},
             "steps": [{"tool_name": "list_zones", "retry_count": 1}]}
    silent = {"task": {"error_detail": None},
              "steps": [{"tool_name": "list_zones", "retry_count": 0}]}
    absent = {"task": {"error_detail": None}, "steps": []}
    assert cli.inject_effectiveness(_fault_case(), fired) is True
    assert cli.inject_effectiveness(_fault_case(), silent) is False
    # fault-005 那个洞的形状: 工具从没被调用 -> 注入未生效, 必须能亮出来
    assert cli.inject_effectiveness(_fault_case(tool="get_available_actions"),
                                    absent) is False


def test_inject_effectiveness__unretryable_reads_error_detail():
    fired = {"task": {"error_code": "tool_error",
                      "error_detail": "工具 validate_policy 不可重试错误: "
                                      "EvalInjectedFault: 评测注入的不可重试故障 "
                                      "(tool=validate_policy)"},
             "steps": []}
    silent = {"task": {"error_code": None, "error_detail": None}, "steps": []}
    case = _fault_case(tool="validate_policy", fault="unretryable")
    assert cli.inject_effectiveness(case, fired) is True
    assert cli.inject_effectiveness(case, silent) is False


def test_inject_effectiveness__none_for_regular_cases():
    assert cli.inject_effectiveness({"id": "simple-001", "input": "x"},
                                    {"task": {}, "steps": []}) is None
