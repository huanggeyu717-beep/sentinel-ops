"""自证 v1.2 -> v1.3 **只动了 `clarify_answer` 的形状, 判据一个字节没动**。

手法与 v1.2 那轮相同 (`verify_v1_2_equivalence.py`): 把两版各自的
`expected.clarify_answer` 键整个剥掉, 序列化成同一种规范形式, 比内容哈希。
相等即证明 `reference` / `also_accept` / `expect_codes` / `must_not` /
`error_codes` / `scenarios` / `companions` / `known_equivalent` / `core` / `why`
以及一切别的字段都没被碰过 —— **不需要任何人逐条读 diff**。

v1.2 那一版从 git 里取 (只读), 不在仓库里另存一份副本: 两份走散是本项目最忌讳的事。

用法: python scripts/dev/verify_v1_3_equivalence.py [<v1.2 的 git ref>]
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
REL = "evals/datasets/policies_v1.jsonl"
DEFAULT_REF = "HEAD"  # v1.3 还没 commit 时, HEAD 上那份就是 v1.2


def canonical_without_clarify_answer(lines: list[str]) -> str:
    out = []
    for line in lines:
        if not line.strip():
            continue
        case: dict[str, Any] = json.loads(line)
        case.get("expected", {}).pop("clarify_answer", None)
        out.append(json.dumps(case, ensure_ascii=False, sort_keys=True))
    return "\n".join(out) + "\n"


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def main() -> int:
    ref = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_REF
    old = subprocess.run(
        ["git", "show", f"{ref}:{REL}"], cwd=REPO,
        capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    new = (REPO / REL).read_text().splitlines()

    old_cases = [json.loads(x) for x in old if x.strip()]
    new_cases = [json.loads(x) for x in new if x.strip()]
    old_kind = {type(c["expected"].get("clarify_answer")).__name__ for c in old_cases
                if c["expected"].get("clarify_answer") is not None}
    new_kind = {type(c["expected"].get("clarify_answer")).__name__ for c in new_cases
                if c["expected"].get("clarify_answer") is not None}

    old_h = digest(canonical_without_clarify_answer(old))
    new_h = digest(canonical_without_clarify_answer(new))
    same = old_h == new_h

    print(f"对照版本 (v1.2): {ref}:{REL}")
    print(f"  用例数        {len(old_cases)} -> {len(new_cases)}")
    print(f"  clarify_answer 形态 {sorted(old_kind)} -> {sorted(new_kind)}")
    print(f"  有 clarify_answer 的条数 "
          f"{sum(1 for c in old_cases if c['expected'].get('clarify_answer'))} -> "
          f"{sum(1 for c in new_cases if c['expected'].get('clarify_answer'))}")
    print()
    print("剥掉 clarify_answer 后的内容哈希:")
    print(f"  v1.2  sha256:{old_h}")
    print(f"  v1.3  sha256:{new_h}")
    print(f"  {'相等 —— 判据一个字节没动' if same else '不相等 —— 判据被动过了, 停下'}")

    # 完整文件的哈希 (进 CHANGELOG 与 README, 供 lint 钉住)
    print()
    print("完整文件内容哈希 (含 clarify_answer):")
    print(f"  v1.2  sha256:{digest(chr(10).join(old) + chr(10))}")
    print(f"  v1.3  sha256:{digest((REPO / REL).read_text())}")
    return 0 if same else 1


if __name__ == "__main__":
    raise SystemExit(main())
