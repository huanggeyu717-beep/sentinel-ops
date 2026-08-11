"""W5 第二段第三批 · 评审方独立复算探针 (只读归档, 不依赖 evals/ 的任何代码)。

存在的理由: 复核的结论必须是跑出来的, 不是从完成报告里抄的。
本脚本不 import evals.*, 只吃 results.jsonl。
"""
import json, sys, collections, pathlib

RUNS = {
    "A''L2-v1.1": "20260810-160934-L2",
    "A'L2-v1.2":  "20260810-182626-L2",
    "A L2-v1.3":  "20260811-003128-L2",
    "B''C1-v1.1": "20260810-161625-C1",
    "B'C1-v1.2":  "20260810-183549-C1",
    "B C1-v1.3":  "20260811-003755-C1",
}
CATS = ["simple","combo","ambiguous","illegal","repairable",
        "capability_gap","tool_fault","prompt_injection"]

def load(rid):
    p = pathlib.Path("evals/runs")/rid/"results.jsonl"
    return [json.loads(l) for l in p.open()]

def cat_of(r):
    return r["category"]

print("### 1. 分类别通过数 / macro / micro (独立重算)")
hdr = f"{'类别':<16}" + "".join(f"{k:>12}" for k in RUNS)
print(hdr)
data = {k: load(v) for k, v in RUNS.items()}
for c in CATS:
    line = f"{c:<16}"
    for k in RUNS:
        rows = [r for r in data[k] if cat_of(r) == c]
        line += f"{sum(r['passed'] for r in rows):>6}/{len(rows):<5}"
    print(line)
line = f"{'macro':<16}"
for k in RUNS:
    per = []
    for c in CATS:
        rows = [r for r in data[k] if cat_of(r) == c]
        if rows: per.append(sum(r['passed'] for r in rows)/len(rows))
    line += f"{sum(per)/len(per)*100:>11.1f}%"
print(line)
line = f"{'micro':<16}"
for k in RUNS:
    rows = data[k]
    line += f"{sum(r['passed'] for r in rows)/len(rows)*100:>11.1f}%"
print(line)

print()
print("### 2. 澄清轮次分布 (clarify_rounds)")
for k in RUNS:
    d = collections.Counter(r["clarify_rounds"] for r in data[k])
    exhausted = [r for r in data[k] if r["clarify_rounds"] >= 3]
    print(f"{k:<12} " + " ".join(f"{i}轮={d.get(i,0)}" for i in range(4))
          + f"   3轮里passed={sum(r['passed'] for r in exhausted)}  n={len(data[k])}")

print()
print("### 3. 注入得逞 (observations.injection_got_through) + must_not_hits 是否为空")
for k in RUNS:
    inj = [r for r in data[k] if r["category"] == "prompt_injection"]
    got = [r for r in inj if r["observations"].get("injection_got_through")]
    print(f"{k:<12} 得逞 {len(got)}/{len(inj)}: " + ", ".join(
        f"{r['case_id']}(hits={len(r['observations'].get('must_not_hits') or [])},"
        f"submitted={r['submitted']},legit={r['has_legitimate']})" for r in got))

print()
print("### 4. blind_answers 合计 / llm_calls==0 却 passed 的用例 (假绿探测)")
for k in RUNS:
    ba = sum(r.get("blind_answers") or 0 for r in data[k])
    print(f"{k:<12} blind_answers 合计={ba}")
print("-- 全仓所有 run 扫一遍 零调用却 passed 的注入用例 --")
for p in sorted(pathlib.Path("evals/runs").glob("*/results.jsonl")):
    rows = [json.loads(l) for l in p.open()]
    bad = [r for r in rows if r["category"] == "prompt_injection"
           and (r.get("llm_calls") or 0) == 0 and r["passed"]]
    if bad:
        print(f"  {p.parent.name}: {len(bad)} 条 -> {[r['case_id'] for r in bad]}")

print()
print("### 5. 三轮耗尽用例: 每轮问的槽位 (artifact.missing_slots)")
for k in ("A'L2-v1.2", "A L2-v1.3"):
    ex = [r for r in data[k] if r["clarify_rounds"] >= 3]
    print(f"-- {k}: {len(ex)} 条")
    for r in ex:
        print(f"   {r['case_id']:<16} rounds={r['artifact'].get('missing_slots')} passed={r['passed']}")

print()
print("### 6. v1.2 走到三轮的用例, 在 v1.3 各自落到第几轮")
for a_prev, a_now, tag in (("A'L2-v1.2","A L2-v1.3","L2"), ("B'C1-v1.2","B C1-v1.3","C1")):
    prev = {r["case_id"]: r for r in data[a_prev]}
    now  = {r["case_id"]: r for r in data[a_now]}
    ex = [cid for cid, r in prev.items() if r["clarify_rounds"] >= 3]
    moved = collections.Counter(now[cid]["clarify_rounds"] for cid in ex if cid in now)
    turned = [cid for cid in ex if cid in now and now[cid]["passed"] and not prev[cid]["passed"]]
    print(f"{tag}: v1.2 到三轮 {len(ex)} 条 -> v1.3 落点 {dict(sorted(moved.items()))}; "
          f"转为通过 {len(turned)} 条 {turned}")
