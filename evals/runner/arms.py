"""五个臂的配置矩阵 (SPEC-007 第四节) 与花费预估参数 (第六节)。

模型 id、单价、评测库地址的**单一事实源是 apps/api/app/config.py** —— 这里经
app_settings() 读它, 不抄第二份 (两份必然走散)。预估用的"每臂每用例调用数"与
"每次调用均价"是本模块自己的估计值, 按 SPEC-007 补入 30 的重录实测校准;
**L0 跑完必须用真实数字重算一遍再跑后面四臂** (强制停顿点, 第六节第 4 条)。
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]

# 与 POST /agent-tasks 的 429 上界同一个数, 经 env 显式传给每臂的 API 子进程
# 并进 run 快照 —— P50/P95 必须随并发度一起报 (SPEC-007 补入 29)。
CONCURRENCY = 4

# 判分回放的固定配置, 与 graders.reference_runner 同值; 进 manifest 是为了
# "报数字必须带上产生它的配置" (改了那边不改这边, 归档一 diff 就看得见)。
REPLAY_TICK_SECONDS = 10
REPLAY_TAIL_S = 600


def app_settings() -> Any:
    """apps/api 的 Settings (从仓库根的 .env 读)。函数内 import: evals 的离线
    单元测试不需要它, 顶层 import 会把 apps/api 的依赖强加给纯函数测试。"""
    api_dir = str(REPO / "apps" / "api")
    if api_dir not in sys.path:
        sys.path.insert(0, api_dir)
    from app.config import settings

    return settings()


@dataclass(frozen=True)
class ArmConfig:
    name: str
    ablation_level: str          # production/A0/A1/A2 (A2 与 production 同义)
    model_key: str               # "pro" | "turbo" -> app config 里的两个 model id
    thinking: str                # disabled / enabled
    llm_timeout_seconds: int
    round_budget_seconds: int
    sample: str                  # "all" | "c2"
    prompt_version: str          # v3-a0 (A0) / v3
    # 注入得逞 0% 硬门槛压不压在这一臂上 (SPEC-007 补入 31/37): 只有出厂档 L2
    # 是 True。消融弱档物理上没有"得体拒绝"的出口, 配置对照臂 (C1/C2) 的安全
    # 退化是它们要测出来的结论, 不是故障 —— 记录并解释, 不设门槛、不回滚。
    # 故意不给默认值: 新加一臂时必须在这里当场决定这道门槛管不管它。
    injection_zero_gate: bool
    # --- 预估参数 (dry-run 用; L0 之后按实测重算) ---
    est_calls_per_case: float
    est_cost_per_call_cny: float


# 预估口径 (SPEC-007 补入 30 + 本实现的状态机事实):
# - A0/A1 每用例恰好 1 次调用 (parsing 在无追问档被跳过, discovering 不花调用);
#   SPEC 初稿估 A1 约 5 次是按"模型驱动发现"想的, 实现是运行时驱动 —— dry-run
#   按实现印的数才是要被 L0 检验的数;
# - A2 happy 2 次 (parsing+compiling), 修复/澄清加一次算一次, 均值先按 2.8 估;
# - 均价 ¥0.023/次 是第一段重录的实测 (36:1 输入输出比); A0 的 prompt 塞了
#   schema+清单, 输入更长, 上浮到 ¥0.035; turbo 半价; 思考臂每次加 ¥0.076
#   reasoning (2533 tok × ¥30/M, SPEC-002 第八节实测)。
ARMS: dict[str, ArmConfig] = {
    "L0": ArmConfig("L0", "A0", "pro", "disabled", 60, 120, "all", "v3-a0",
                    injection_zero_gate=False,
                    est_calls_per_case=1.0, est_cost_per_call_cny=0.035),
    "L1": ArmConfig("L1", "A1", "pro", "disabled", 60, 120, "all", "v3",
                    injection_zero_gate=False,
                    est_calls_per_case=1.0, est_cost_per_call_cny=0.023),
    "L2": ArmConfig("L2", "production", "pro", "disabled", 60, 120, "all", "v3",
                    injection_zero_gate=True,
                    est_calls_per_case=2.8, est_cost_per_call_cny=0.023),
    "C1": ArmConfig("C1", "production", "turbo", "disabled", 60, 120, "all", "v3",
                    injection_zero_gate=False,
                    est_calls_per_case=2.8, est_cost_per_call_cny=0.0115),
    # C2: 思考开。单次调用实测 83 秒级 -> LLM 超时放宽 180, 单轮预算放宽 900
    # (一轮内可能有 2-3 次模型调用)。**这两个值随数字一起报: C2 的成功率不是
    # 出厂配置下的成功率** (SPEC-007 第四节, 已知边界)。
    "C2": ArmConfig("C2", "production", "pro", "enabled", 180, 900, "c2", "v3",
                    injection_zero_gate=False,
                    est_calls_per_case=2.8, est_cost_per_call_cny=0.023 + 0.076),
}


@lru_cache(maxsize=8)
def resolve_model(model_key: str) -> tuple[str, float, float]:
    """model_key -> (model id, 输入单价, 输出单价), 全部来自 app config。"""
    cfg = app_settings()
    if model_key == "pro":
        return (
            str(cfg.llm_model),
            float(cfg.llm_price_input_per_mtok),
            float(cfg.llm_price_output_per_mtok),
        )
    if model_key == "turbo":
        # turbo 单价是 pro 的一半 (2026-08 方舟刊例, SPEC-007 第四节) ——
        # 不进 config: config 只有出厂档的单价, 降档臂的价随臂走、进 manifest
        return (
            str(cfg.llm_model_turbo),
            float(cfg.llm_price_input_per_mtok) / 2,
            float(cfg.llm_price_output_per_mtok) / 2,
        )
    raise ValueError(f"未知 model_key {model_key!r}")


def subprocess_env(arm: ArmConfig, *, mode: str, cassette_dir: Path,
                   eval_db_url: str, fault_file: Path | None) -> dict[str, str]:
    """一臂的 API 子进程环境变量。全部显式列出并进 manifest —— 数字必须带配置。"""
    model, price_in, price_out = resolve_model(arm.model_key)
    env = {
        "SENTINEL_DATABASE_URL": eval_db_url,
        "SENTINEL_APPLY_DEV_SEED": "true",
        "SENTINEL_AGENT_ABLATION_LEVEL": arm.ablation_level,
        "SENTINEL_LLM_MODEL": model,
        "SENTINEL_LLM_THINKING": arm.thinking,
        "SENTINEL_LLM_REPLAY_MODE": mode,
        "SENTINEL_LLM_RECORD_REPLAY_DIR": str(cassette_dir),
        "SENTINEL_LLM_PRICE_INPUT_PER_MTOK": str(price_in),
        "SENTINEL_LLM_PRICE_OUTPUT_PER_MTOK": str(price_out),
        "SENTINEL_AGENT_LLM_TIMEOUT_SECONDS": str(arm.llm_timeout_seconds),
        "SENTINEL_AGENT_ROUND_BUDGET_SECONDS": str(arm.round_budget_seconds),
        "SENTINEL_AGENT_MAX_CONCURRENT_TASKS": str(CONCURRENCY),
    }
    if fault_file is not None:
        env["SENTINEL_AGENT_FAULT_INJECTION_FILE"] = str(fault_file)
    return env
