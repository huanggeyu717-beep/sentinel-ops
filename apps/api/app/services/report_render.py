"""事故报告的事实包、占位符渲染器与两道硬拦 (SPEC-008 第一、二、三节)。

**本模块一行 IO 都不许有**: 不 import 数据库、不 import 网络、不读环境变量。
读库的那一半在 report_service.load_incident_facts (它产出 IncidentFactsRaw,
本模块只吃这个)。这条边界由 tests/test_report_render.py 守着, 不靠人记得 ——
本模块 (入口) 用 **import 白名单**: 只许 re / collections.abc / dataclasses /
datetime / typing / zoneinfo, 多一个就红; 传递到的 app.* 模块用 IO 黑名单。
入口用白名单是实测逼出来的 (定稿决定 N): 黑名单挡不住 `import os` + 读环境变量,
而"注释说这个模块是纯的"不算数, 本项目在这上面栽过。

机制一句话: 模型正文里只能写 ``{{fact_id}}``, 渲染时由代码换成事实的 text ——
每一个数字与专名都可溯源到一条 SQL 查出来的事实, "不许编"是语法层面的, 不是叮嘱。

已知边界 (SPEC-008 文末, 按约定写进代码注释, 被问到照实说):

- **定性形容词拦不住**: "很快就到场了"里没有数字, 占位符机制对它无效。
  这套设计管的是可核对的事实, 管不了语气与判断;
- **severity / resolved_kind 的裸写拦不住** (判据 3 的代价): 它们的中文取值
  ("一般/高/紧急"、"人工/自动") 都是日常词, 进裸写黑名单会把正常句子全打掉。
  两者各只有几个固定取值, 编造空间接近零 —— 这是权衡不是遗漏;
- **"用了对的占位符但放错句子"没有任何东西测得出来** (第三道检查, 随第六节的
  答案键一起砍了)。校验器拦得住编造与悬空引用, 拦不住错配。
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

# ===== 报告的形状 (SPEC-008 第一节) =====

REPORT_FIELDS = ("summary", "handling", "impact", "notable", "suggestion")

# 上限判在**渲染之后**的正文上 (定稿决定 G): 判在占位符原文上, 模型每多用一个
# 占位符就多吃 20 个字符额度, 越守规矩越容易撞上限 —— 约束方向和设计目标反着来。
FIELD_CHAR_LIMITS = {
    "summary": 200, "handling": 300, "impact": 200, "notable": 200, "suggestion": 200,
}

# 渲染前另一道宽松硬顶: 防模型写出一串爆炸性引用把渲染结果撑爆。
# 它是防呆不是风格约束, 正常输出碰不到 —— 与上面的字段上限是两件事, 不合并。
PRE_RENDER_CHAR_CAP = 800

MISSING_TEXT = "无此记录"

# ===== 事实包 (SPEC-008 第二节) =====


@dataclass(frozen=True)
class IncidentFactsRaw:
    """load_incident_facts 的产出、build_fact_pack 的输入 —— 原始行, 未格式化。

    契约定义在本模块 (纯的一侧), service 按它组装; 依赖方向只能是
    report_service -> report_render, 反过来 import 会被纯度断言拦下。

    incident 的键与 report_service._INCIDENT 查询的列一一对应; events 为
    (kind, actor, detail, at) 的时间线, 已按 (at, id) 升序。
    sensor_30d_count / zone_concurrent 在传感器或区域缺失时为 None。
    """

    incident: Mapping[str, Any]
    events: Sequence[Mapping[str, Any]]
    sensor_30d_count: int | None
    zone_concurrent: int | None


@dataclass(frozen=True)
class Fact:
    id: str          # 语义 id, 蛇形命名, 正文里以 {{id}} 引用
    label: str       # 人话标签
    value: Any       # 原始值, 供判分与重渲染; 缺失时为 None
    text: str        # 已格式化好的中文串, 渲染时替换进正文; 缺失时为 "无此记录"


# 固定事实的静态清单 (顺序即产出顺序)。事实包**一律产全**: 这里的每一条都出条目,
# 该事故没有的那条 value=None / text="无此记录" —— "事实不足时不许编"因此是机制
# 不是叮嘱: 无人接单时模型只能写 {{ack_by}}, 由渲染器吐出"无此记录", 它写不出
# 一个编造的人名 (定稿决定 E)。时间线 tl_1..tl_n 与 tl_truncated 按条数动态追加。
STATIC_FACT_IDS = (
    "incident_id", "zone", "sensor", "severity",
    "opened_at", "acknowledged_at", "resolved_at",
    "response_duration", "handle_duration", "onsite_duration",
    "assigned_to", "ack_by", "cross_zone",
    "resolved_kind", "resolve_policy",
    "sensor_30d_count", "zone_concurrent",
)

# 时间线超过 20 条截断: 最早 5 + 最晚 15, 并多产一条 tl_truncated 明写截掉几条
# —— "明写截断了几条"也是机制, 不是叮嘱 (SPEC-008 第二节)。
TIMELINE_KEEP_HEAD = 5
TIMELINE_KEEP_TAIL = 15

_SEVERITY_TEXT = {"normal": "一般", "high": "高", "critical": "紧急"}

_EVENT_KIND_TEXT = {
    "opened": "开单", "assigned": "派单", "reassigned": "改派",
    "acknowledged": "接单", "resolved": "解决", "escalated": "升级",
    "sensor_still_wet": "传感器持续报湿", "sensor_dry": "传感器转干",
    "notify": "通知决策", "set_led": "点灯决策",
}

_RESOLVE_POLICY_RE = re.compile(r"^policy:(\d+)@v(\d+)$")


def _fmt_ts(dt: datetime, tz: ZoneInfo) -> str:
    return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M")


def _fmt_duration(delta: timedelta) -> str:
    """时长 -> 中文串。**不足一小时时带上秒, 不许把余数丢掉。**

    2026-08-24 修。原实现 `minutes, _ = divmod(seconds, 60)` 直接丢余数,
    于是 60–119 秒一律印成 "1 分"。要命的是**三条时长按定义相加**
    (开→接单 + 接单→解决 = 开→解决), 而它们印在同一句话里 —— 第一次真实生成
    的报告当场撞上: "从开单到解决共 1 分, 响应 27 秒, 到场 56 秒",
    而 27 + 56 = 83。读的人一做加法就发现算不平, 而他会怀疑的恰好是本项目
    最不能被怀疑的那件事: 这些数字是不是编的。
    **一处看得见的算不平, 比十处没人注意的 bug 都贵。**

    一小时以上仍然只到分: 误差 <60 秒, 相对量级可忽略, 且带上秒会让
    "1 小时 12 分 7 秒"这种串挤占字段上限。这是取舍, 写在这里不藏。
    """
    seconds = max(0, int(delta.total_seconds()))
    if seconds < 60:
        return f"{seconds} 秒"
    if seconds < 3600:
        minutes, rest = divmod(seconds, 60)
        return f"{minutes} 分" if rest == 0 else f"{minutes} 分 {rest} 秒"
    hours, rest = divmod(seconds, 3600)
    minutes = rest // 60
    return f"{hours} 小时" if minutes == 0 else f"{hours} 小时 {minutes} 分"


def _missing(fact_id: str, label: str) -> Fact:
    return Fact(id=fact_id, label=label, value=None, text=MISSING_TEXT)


def _duration_fact(
    fact_id: str, label: str, start: datetime | None, end: datetime | None
) -> Fact:
    if start is None or end is None:
        return _missing(fact_id, label)
    delta = end - start
    return Fact(id=fact_id, label=label, value=int(delta.total_seconds()),
                text=_fmt_duration(delta))


def _ts_fact(fact_id: str, label: str, dt: datetime | None, tz: ZoneInfo) -> Fact:
    if dt is None:
        return _missing(fact_id, label)
    return Fact(id=fact_id, label=label, value=dt.isoformat(), text=_fmt_ts(dt, tz))


def build_fact_pack(raw: IncidentFactsRaw, *, tz: str) -> list[Fact]:
    """原始行 -> 有序事实表。纯函数: 同一份 raw + 同一个 tz 永远得到同一份产出。

    tz **显式传入, 不读环境变量** (定稿决定随第二节): 读环境变量会让同一份快照
    在不同机器上渲染出不同的时间 —— 与"报数字必须带产生它的配置"是同一条纪律。
    调用方给 "Asia/Shanghai"。
    """
    zone_info = ZoneInfo(tz)
    inc = raw.incident
    facts: list[Fact] = []

    # 一、基本
    facts.append(Fact("incident_id", "事故编号", inc["id"], f"#{inc['id']}"))
    if inc.get("zone_name") is None:
        facts.append(_missing("zone", "区域"))
    else:
        facts.append(Fact("zone", "区域", inc["zone_id"], str(inc["zone_name"])))
    if inc.get("sensor_id") is None:
        facts.append(_missing("sensor", "传感器"))
    else:
        facts.append(Fact("sensor", "传感器", inc["sensor_id"],
                          f"{inc['sensor_id']} 号传感器"))
    severity = str(inc["severity"])
    facts.append(Fact("severity", "严重级别", severity,
                      _SEVERITY_TEXT.get(severity, severity)))

    # 二、时刻
    facts.append(_ts_fact("opened_at", "开单时刻", inc["opened_at"], zone_info))
    facts.append(_ts_fact("acknowledged_at", "接单时刻",
                          inc.get("acknowledged_at"), zone_info))
    facts.append(_ts_fact("resolved_at", "解决时刻", inc.get("resolved_at"), zone_info))

    # 三、时长
    facts.append(_duration_fact("response_duration", "响应时长 (开单到接单)",
                                inc["opened_at"], inc.get("acknowledged_at")))
    facts.append(_duration_fact("handle_duration", "处理时长 (开单到解决)",
                                inc["opened_at"], inc.get("resolved_at")))
    facts.append(_duration_fact("onsite_duration", "到场时长 (接单到解决)",
                                inc.get("acknowledged_at"), inc.get("resolved_at")))

    # 四、人。缺失是常态不是边角: SPEC-003 决策 3 明写跳过分配时
    # assigned_employee_id 保持为空不回填 —— 那条路径在这里就是三个"无此记录"。
    if inc.get("assigned_employee_id") is None:
        facts.append(_missing("assigned_to", "派单给"))
    else:
        facts.append(Fact("assigned_to", "派单给", inc["assigned_employee_id"],
                          str(inc["assigned_employee_name"])))
    if inc.get("acknowledged_by_employee_id") is None:
        facts.append(_missing("ack_by", "实际接单人"))
    else:
        facts.append(Fact("ack_by", "实际接单人", inc["acknowledged_by_employee_id"],
                          str(inc["acknowledged_by_employee_name"])))
    if inc.get("assigned_employee_id") is None:
        # 是否跨区以派单为前提, 没派过单就没有这回事
        facts.append(_missing("cross_zone", "是否跨区派单"))
    else:
        # 员工 zone 为空视为不属于任何区域, 按跨区处理 (SPEC-003 决策 7 同口径)。
        # 用的是员工**当前**的区域: 派单当时的区域在 audit_log 里, 但为一个演示里
        # 不会变的字段去解析审计日志不值得 —— 边界照实写在这里。
        cross = (
            inc.get("assigned_employee_zone_id") is None
            or inc["assigned_employee_zone_id"] != inc.get("zone_id")
        )
        facts.append(Fact("cross_zone", "是否跨区派单", cross,
                          "跨区派单" if cross else "本区派单"))

    # 五、解决来源。判据是 resolved_by 的前缀 (SPEC-003 决策 4): policy: 为自动,
    # employee: / user: 为人工。W2 的旧口径 (如 auto_sensor_dry) 不猜, 按缺失处置。
    resolved_by = inc.get("resolved_by")
    prefix = resolved_by.partition(":")[0] if resolved_by else ""
    if prefix == "policy":
        facts.append(Fact("resolved_kind", "解决来源", "auto", "自动"))
    elif prefix in ("employee", "user"):
        facts.append(Fact("resolved_kind", "解决来源", "human", "人工"))
    else:
        facts.append(_missing("resolved_kind", "解决来源"))
    match = _RESOLVE_POLICY_RE.match(resolved_by) if resolved_by else None
    if match is None:
        facts.append(_missing("resolve_policy", "解决策略"))
    else:
        policy_id, version = int(match.group(1)), int(match.group(2))
        facts.append(Fact("resolve_policy", "解决策略",
                          {"policy_id": policy_id, "version": version},
                          f"策略 {policy_id} 第 {version} 版"))

    # 六、上下文
    if raw.sensor_30d_count is None:
        facts.append(_missing("sensor_30d_count", "该传感器近三十天开单数"))
    else:
        facts.append(Fact("sensor_30d_count", "该传感器近三十天开单数",
                          raw.sensor_30d_count, f"{raw.sensor_30d_count} 次"))
    if raw.zone_concurrent is None:
        facts.append(_missing("zone_concurrent", "同区同期并发未结事故数"))
    else:
        facts.append(Fact("zone_concurrent", "同区同期并发未结事故数",
                          raw.zone_concurrent, f"{raw.zone_concurrent} 条"))

    # 固定部分产全且顺序与静态清单一致 —— E_DANGLING_REF 因此查的是一张固定的表
    assert tuple(f.id for f in facts) == STATIC_FACT_IDS

    # 时间线逐条。截断后 tl 序号按**保留下来的顺序**重排 (tl_1..tl_20): id 是引用
    # 句柄不是原始行号, 原始事件原样在 value 里。
    #
    # 但截断之后, **缺口要在事实包里看得见** (定稿决定 O): tl_truncated 插在
    # tl_5 与 tl_6 之间 (缺口处), 不排末尾; 后半段的 label 写"时间线末段第 N 条"。
    # 模型读的是 label —— 排末尾又都叫"第 N 条", 它会以为 tl_6 紧接 tl_5, 写出
    # "随后第 6 条记录显示……"式的连贯叙述, 而中间实际断着。id 是引用句柄可以重排,
    # **label 是给人和模型看的话, 不许说谎**。
    events = list(raw.events)
    omitted = 0
    if len(events) > TIMELINE_KEEP_HEAD + TIMELINE_KEEP_TAIL:
        omitted = len(events) - TIMELINE_KEEP_HEAD - TIMELINE_KEEP_TAIL
        events = events[:TIMELINE_KEEP_HEAD] + events[-TIMELINE_KEEP_TAIL:]
    for i, event in enumerate(events, start=1):
        if omitted and i == TIMELINE_KEEP_HEAD + 1:
            facts.append(Fact("tl_truncated", "时间线截断说明", omitted,
                              f"另有 {omitted} 条时间线未列入"))
        at = event["at"]
        kind = str(event["kind"])
        in_tail = omitted and i > TIMELINE_KEEP_HEAD
        facts.append(Fact(
            f"tl_{i}", f"时间线末段第 {i} 条" if in_tail else f"时间线第 {i} 条",
            {"kind": kind, "actor": event.get("actor"),
             "at": at.isoformat(), "detail": event.get("detail")},
            f"{_fmt_ts(at, zone_info)} {_EVENT_KIND_TEXT.get(kind, kind)}",
        ))

    return facts


# ===== 两道硬拦 (SPEC-008 第三节, 与 ADR-007 同源: 不靠模型自觉, 让它写不进去) =====

VIOLATION_BARE_FACT = "E_BARE_FACT"
VIOLATION_DANGLING_REF = "E_DANGLING_REF"
# 下面三个码 SPEC 未命名, 本实现补起 (完成报告第二节点名):
VIOLATION_RAW_OVERFLOW = "E_RAW_OVERFLOW"            # 渲染前 800 硬顶
VIOLATION_RENDERED_OVERFLOW = "E_RENDERED_OVERFLOW"  # 渲染后字段上限
VIOLATION_BAD_SHAPE = "E_BAD_SHAPE"                  # 字段缺失/多余/不是字符串


@dataclass(frozen=True)
class Violation:
    code: str
    field: str    # 五个字段之一; E_BAD_SHAPE 时是出问题的键名
    detail: str   # 撞上判据的那个片段 / id / 人话说明


_PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")

# 判据 2: 阿拉伯数字 0 容忍 (含全角) —— 它是编造数字的主要载体, 中文报告正文里
# 正常不需要它。一段连续数字算一个违规项。
_ARABIC_RE = re.compile(r"[0-9０-９]+")

# 判据 2 的中文半边 (定稿决定 L, 对照实测见 scripts/dev/probe_bare_fact_cn.py):
# 只拦**时间与时长** —— 中文数词后面跟 分钟/小时/秒钟/秒/天 才算, 数词与单位之间
# 允许一个约量词 (个/来/多/余, 可选**一个**, 不是任意字符), 数词含"半",
# 序数"第X"整体放行 (负向后行断言)。
#
# 为什么不是按个例修: 初稿单位表 (分/秒/时/天/次/条/人/区/级) 两头都不成立 ——
# 单字"分"/"时"误伤验收点名放行的"十分明显"/"第一时间", 而只把它们换成两字并不够,
# 同形的"第一次报警"/"这是一次跨区派单"/"一次性"照样被"一+次"拦下; 另一头,
# 没有约量词, "两个小时"/"半小时"/"一个半小时"/"十来分钟"一条都拦不住。
# **误伤会在 repairing 里暴露出来, 漏拦不会 —— 它悄无声息**, 所以两害相权先堵漏拦。
# 实测: 误伤 3/9 -> 0/9, 漏拦 5/10 -> 3/10。
#
# 已知边界 (SPEC-008 文末; 残下的漏拦 3/10 即"两次"/"两人"/"二十余条", 是故意丢的):
# - **中文写的计数拦不住**: 单位表去掉了 次/条/人/区/级。丢得起的理由是计数类事实
#   的 text 本身就带阿拉伯数字 ("2 次"/"0 条"/"3 号传感器"), 模型抄它只能抄出
#   阿拉伯数字, 已被上面的 _ARABIC_RE 兜住; 残留的洞只有一种 —— 模型主动把
#   "2 次"译成"两次"。同理"三分"、"一个多小时"、"好几个小时"这类不带标准单位的
#   写法也漏。照实说, 不含糊;
# - **序数一律放行**: "第三次报警"里的"三"拦不住 —— 排除"第X"是为了放行
#   "第一时间"与"第一次", 两者形状相同, 按个例修一个漏一个, 所以按规则整体放行。
#   (?<!第) 只挡"第"紧邻的那一个字; "第 3 次"是阿拉伯数字, 由 _ARABIC_RE 管。
_CN_QUANTITY_RE = re.compile(
    r"(?<!第)[零〇一二三四五六七八九十百千万亿两半]+(?:个|来|多|余)?(?:分钟|小时|秒钟|秒|天)"
)

# 判据 3: text 裸写只查**专名类**事实。severity / resolved_kind 刻意不进黑名单
# (取值是"一般/高/紧急"、"人工/自动"这类日常词, 拦它们等于拦掉中文本身)。
# cross_zone **在黑名单里** (定稿决定 M): "跨区派单"/"本区派单"不是日常词,
# 它就是那条事实本身 —— 与 severity 那两条不同类, 不跟着一起豁免。
_PROPER_NOUN_FACT_IDS = frozenset(
    {"assigned_to", "ack_by", "zone", "sensor", "resolve_policy", "cross_zone"}
)


def _strip_placeholders(text: str) -> tuple[list[str], str]:
    """返回 (引用的 id 列表, 剔除全部占位符后的残余文本)。

    判据 1: 检查必须跑在残余文本上 —— 占位符自己就含数字 ({{tl_3}}、
    {{incident_id}}), 不先剔掉, 模型每写一条时间线引用都被自己拦死, 而
    "全都拦下来"从跑分上看和"检查很严格"长得一模一样。
    换行做分隔符: 剔除不能把两段无关文字拼成一个新的数字串。
    """
    ids = _PLACEHOLDER_RE.findall(text)
    return ids, _PLACEHOLDER_RE.sub("\n", text)


def check_bare_facts(body: Mapping[str, str], facts: Sequence[Fact]) -> list[Violation]:
    """E_BARE_FACT: 正文裸写事实。按违规项累加 —— 一轮里三处裸写就是 3 条。"""
    proper_nouns = sorted(
        (f.text for f in facts
         if f.id in _PROPER_NOUN_FACT_IDS and f.value is not None and len(f.text) >= 2),
        key=len, reverse=True,  # 长的先匹配, 避免短名是长名子串时重复计数
    )
    violations: list[Violation] = []
    for field in REPORT_FIELDS:
        _, residual = _strip_placeholders(body[field])
        # 专名先查先剔: "3 号传感器"整段裸写算一个违规项, 不再按里面的数字重复计
        for noun in proper_nouns:
            count = residual.count(noun)
            if count:
                violations.extend(
                    Violation(VIOLATION_BARE_FACT, field, noun) for _ in range(count)
                )
                residual = residual.replace(noun, "\n")
        for pattern in (_ARABIC_RE, _CN_QUANTITY_RE):
            violations.extend(
                Violation(VIOLATION_BARE_FACT, field, m.group(0))
                for m in pattern.finditer(residual)
            )
    return violations


def check_dangling_refs(body: Mapping[str, str], facts: Sequence[Fact]) -> list[Violation]:
    """E_DANGLING_REF: 引用了事实包里不存在的 id。每一处引用算一个违规项。"""
    known = {f.id for f in facts}
    return [
        Violation(VIOLATION_DANGLING_REF, field, ref)
        for field in REPORT_FIELDS
        for ref in _strip_placeholders(body[field])[0]
        if ref not in known
    ]


def render_body(body: Mapping[str, str], facts: Sequence[Fact]) -> dict[str, str]:
    """{{id}} -> 该事实的 text。缺失的事实照常渲染成"无此记录" —— 不是不给。

    只应在 check_dangling_refs 通过后调用; 撞到未知 id 直接抛错, 不静默留原文。
    """
    text_by_id = {f.id: f.text for f in facts}

    def substitute(match: re.Match[str]) -> str:
        ref = match.group(1)
        if ref not in text_by_id:
            raise ValueError(f"悬空引用 {{{{{ref}}}}}: 先跑 check_dangling_refs")
        return text_by_id[ref]

    return {field: _PLACEHOLDER_RE.sub(substitute, body[field]) for field in REPORT_FIELDS}


@dataclass(frozen=True)
class DraftCheckResult:
    violations: tuple[Violation, ...]
    rendered: dict[str, str] | None  # 全部通过才给; 拒收的草稿没有渲染产物

    @property
    def ok(self) -> bool:
        return not self.violations

    # 两个数分开命名、分开报, 不加成一个"幻觉率" (SPEC-008 第三节)。
    # 为 0 不说明模型老实, 只说明这一跑里它没试; 大于 0 恰是机制在干活的证据。
    @property
    def bare_fact_attempts(self) -> int:
        return sum(1 for v in self.violations if v.code == VIOLATION_BARE_FACT)

    @property
    def dangling_ref_attempts(self) -> int:
        return sum(1 for v in self.violations if v.code == VIOLATION_DANGLING_REF)


def check_draft(body: Mapping[str, Any], facts: Sequence[Fact]) -> DraftCheckResult:
    """一份草稿过全部检查: 形状 -> 800 硬顶 -> 两道硬拦 -> 渲染 -> 渲染后字段上限。

    任何违规都是拒收 (调用方回 repairing), 但**计入两个倾向计数的只有
    E_BARE_FACT / E_DANGLING_REF 两类** —— 超长与形状错是格式问题, 不是编造倾向。
    """
    violations: list[Violation] = []
    for key in body:
        if key not in REPORT_FIELDS:
            violations.append(Violation(VIOLATION_BAD_SHAPE, str(key), "多余的字段"))
    for field in REPORT_FIELDS:
        if not isinstance(body.get(field), str):
            violations.append(Violation(VIOLATION_BAD_SHAPE, field, "缺失或不是字符串"))
    if violations:
        return DraftCheckResult(tuple(violations), None)

    narrow: dict[str, str] = {field: body[field] for field in REPORT_FIELDS}
    for field, raw_text in narrow.items():
        if len(raw_text) > PRE_RENDER_CHAR_CAP:
            violations.append(Violation(
                VIOLATION_RAW_OVERFLOW, field,
                f"渲染前 {len(raw_text)} 字符, 硬顶 {PRE_RENDER_CHAR_CAP}",
            ))
    if violations:
        # 超过防呆硬顶的输入不值得继续扫 (上限就是为了给后面的检查一个有界的输入)
        return DraftCheckResult(tuple(violations), None)

    violations.extend(check_bare_facts(narrow, facts))
    dangling = check_dangling_refs(narrow, facts)
    violations.extend(dangling)
    if dangling:
        return DraftCheckResult(tuple(violations), None)

    rendered = render_body(narrow, facts)
    for field, rendered_text in rendered.items():
        limit = FIELD_CHAR_LIMITS[field]
        if len(rendered_text) > limit:
            violations.append(Violation(
                VIOLATION_RENDERED_OVERFLOW, field,
                f"渲染后 {len(rendered_text)} 字符, 上限 {limit}",
            ))
    if violations:
        return DraftCheckResult(tuple(violations), None)
    return DraftCheckResult((), rendered)
