"""C2 臂的确定性抽样: core 40 按类别等比取 20 (SPEC-007 第四节)。

确定性 = 同一份数据集永远抽出同一批 id: 类别内按 id 字典序取前 N; 配额按
"数量减半", 小数部分按最大余数法补齐, 余数并列时按类别名字典序 —— 全程无随机。
抽中的 id 列表进 run 快照 (manifest), 半年后能核对当时抽了谁。
"""
from __future__ import annotations

from typing import Any

C2_TARGET = 20


def sample_c2(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    core = [c for c in cases if c.get("core")]
    by_cat: dict[str, list[dict[str, Any]]] = {}
    for case in sorted(core, key=lambda c: str(c["id"])):
        by_cat.setdefault(str(case["category"]), []).append(case)
    quotas = {cat: len(members) / 2 for cat, members in by_cat.items()}
    take = {cat: int(q) for cat, q in quotas.items()}
    remaining = C2_TARGET - sum(take.values())
    if remaining < 0:
        raise ValueError(f"配额溢出: {take}")
    by_fraction = sorted(
        by_cat, key=lambda cat: (-(quotas[cat] - take[cat]), cat)
    )
    for cat in by_fraction[:remaining]:
        take[cat] += 1
    picked: list[dict[str, Any]] = []
    for cat in sorted(by_cat):
        picked.extend(by_cat[cat][: take[cat]])
    if len(picked) != C2_TARGET:
        raise ValueError(f"抽样数 {len(picked)} != {C2_TARGET}, 配额: {take}")
    return picked
