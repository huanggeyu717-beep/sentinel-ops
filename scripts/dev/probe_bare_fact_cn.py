#!/usr/bin/env python3
"""探针: E_BARE_FACT 中文半边 (_CN_QUANTITY_RE) 的误伤与漏拦实测。

背景 (SPEC-008 第三节判据 2): 中文数字只拦"数量词"模式。第一段实现把单位表里的
单字"分"/"时"改成两字"分钟"/"小时", 以放行验收点名的"第一时间"与"十分明显"。
本探针复核那次修改**有没有推广到同形的其它写法**, 结论是没有:

- 误伤仍在: "第一次报警"、"一次性"、"这是一次跨区派单" 命中 一+次 被拦,
  与"第一时间"是同一个形状 (序数"第X"+单位), 只是没被验收点名;
- 漏拦更要紧: 量词"个"让最自然的中文时长整个漏过 —— "两个小时"、"半小时"、
  "一个半小时"、"十来分钟" 一条都拦不住, 而"写时长"正是这道检查存在的理由。

候选口径: 中文半边**只拦时间与时长单位**, 去掉纯量词 (次/条/人/区/级),
数词与单位之间允许一个约量词 (个/来/多/余), 序数"第X"整体放行。
计数类事实的 text 本身就带阿拉伯数字 ("2 次"/"0 条"/"3 号传感器"), 抄它只能抄出
阿拉伯数字, 由判据 2 前半兜住; 残留的洞是"模型主动把 2 次译成两次", 写进已知边界。

跑法: cd apps/api && python3 ../../scripts/dev/probe_bare_fact_cn.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps" / "api"))

from app.services import report_render as rr

CANDIDATE = re.compile(
    r"(?<!第)[零〇一二三四五六七八九十百千万亿两半]+(?:个|来|多|余)?(?:分钟|小时|秒钟|秒|天)"
)

NORMAL = [  # 正常中文, 必须放行
    "处理人第一时间赶到现场。", "地面积水痕迹十分明显。", "现场情况一般。",
    "本单为人工处理关闭。", "这是一次跨区派单。", "该探头本月第一次报警。",
    "一次性处置完毕。", "已一并处理。", "升级为高风险。",
]
FABRICATED = [  # 裸写事实, 最好拦住
    "前后耗了十二分钟。", "又等了两小时才复位。", "花了两个小时。", "半小时后到场。",
    "十来分钟就干了。", "一个半小时。", "三天后复查。", "二十余条记录。",
    "开了两次单。", "到场两人。",
]


def _row(text: str) -> str:
    now = bool(rr._CN_QUANTITY_RE.findall(text))
    cand = bool(CANDIDATE.findall(text))
    return f"  {text:<22} 现在拦={now!s:<6} 候选拦={cand}"


def main() -> int:
    print("正常中文 (必须放行):")
    for s in NORMAL:
        print(_row(s))
    print("\n裸写事实 (最好拦住):")
    for s in FABRICATED:
        print(_row(s))
    wrong_now = sum(bool(rr._CN_QUANTITY_RE.findall(s)) for s in NORMAL)
    wrong_cand = sum(bool(CANDIDATE.findall(s)) for s in NORMAL)
    miss_now = sum(not rr._CN_QUANTITY_RE.findall(s) for s in FABRICATED)
    miss_cand = sum(not CANDIDATE.findall(s) for s in FABRICATED)
    print(f"\n误伤 现在 {wrong_now}/{len(NORMAL)} -> 候选 {wrong_cand}/{len(NORMAL)}")
    print(f"漏拦 现在 {miss_now}/{len(FABRICATED)} -> 候选 {miss_cand}/{len(FABRICATED)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
