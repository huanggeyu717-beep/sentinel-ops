"""report_render 纯函数档的全部单元测试 (SPEC-008 第一/二/三节 + 第十一节变异 1/2/5/6/7/8/9/10/11)。

本文件不连库: 被测模块必须是纯的, 这条边界由文件开头的传递 import 断言守着
(照 evals/tests/test_grader_io_boundary.py 的做法) —— "注释说这个模块是纯的"
不算数。事实来源全部手造 (IncidentFactsRaw 直接构造)。
"""
from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from app.services import report_render as rr

# ===== 一、纯度: 入口白名单 + 传递闭包黑名单 =====

APP_DIR = Path(__file__).resolve().parent.parent

# 传递到的 app.* 模块用黑名单 (SPEC-008 第二节): 数据库驱动、ORM、HTTP 客户端、
# 以及 app.db (引擎与连接池住在那) 都算 IO。黑名单按前缀匹配。
FORBIDDEN_PREFIXES = ("asyncpg", "sqlalchemy", "httpx", "app.db")

# 入口模块 report_render 用**白名单** (定稿决定 N, 变异 8 的守卫): 黑名单挡不住
# `import os` + `os.environ.get(...)`, 而模块开头明写着"不读环境变量", tz 显式
# 传入的整个理由就是快照可复现 —— 黑名单只挡得住上次那个, 白名单挡的是下次那个。
# 实测见 scripts/dev/probe_render_purity.py。
#
# `from __future__ import annotations` 不是运行期依赖, 但扫描器不区分, 会把它
# 收成 `__future__` —— 本实现选择**把它放进白名单**而不是在扫描器里排除,
# 选的是哪种由下面的钉住测试固定, 不许碰巧通过。
ENTRY_MODULE = "app.services.report_render"
ENTRY_IMPORT_WHITELIST = (
    "__future__", "re", "collections.abc", "dataclasses",
    "datetime", "typing", "zoneinfo",
)


def _whitelisted(target: str) -> bool:
    # 前缀匹配要容得下带点的子模块: collections.abc 自己, 以及 from 它 import
    # 进来的 collections.abc.Mapping 这类名字, 都算 collections.abc 名下。
    return any(
        target == allowed or target.startswith(allowed + ".")
        for allowed in ENTRY_IMPORT_WHITELIST
    )


def _import_targets(path: Path, dotted: str) -> set[str]:
    """文件里出现的 import 目标, 相对导入解析成绝对模块名 (同 grader 边界测试)。"""
    package = dotted if path.name == "__init__.py" else dotted.rpartition(".")[0]
    targets: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            targets.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                base = node.module or ""
            else:
                parts = package.split(".")
                keep = len(parts) - node.level + 1
                head = package if node.level == 1 else ".".join(parts[:keep])
                base = f"{head}.{node.module}" if node.module else head
            if not base:
                continue
            targets.add(base)
            # from X import y 里被 import 的名字可能本身就是模块
            targets.update(f"{base}.{alias.name}" for alias in node.names)
    return targets


def _resolve_app_module(dotted: str) -> Path | None:
    if dotted != "app" and not dotted.startswith("app."):
        return None
    candidate = APP_DIR.joinpath(*dotted.split("."))
    if (candidate / "__init__.py").is_file():
        return candidate / "__init__.py"
    if candidate.with_suffix(".py").is_file():
        return candidate.with_suffix(".py")
    return None


def _reachable(entry: str) -> dict[str, set[str]]:
    """入口模块出发, 沿 app.* 的 import 求传递闭包; 返回 {模块名: 它的 import 目标}。"""
    path = _resolve_app_module(entry)
    assert path is not None, entry
    seen: dict[str, set[str]] = {}
    queue = [(entry, path)]
    while queue:
        dotted, file = queue.pop()
        targets = _import_targets(file, dotted)
        seen[dotted] = targets
        for target in targets:
            resolved = _resolve_app_module(target)
            if resolved is not None and target not in seen:
                queue.append((target, resolved))
    return seen


def test_render_module_purity__no_io_in_transitive_imports():
    reachable = _reachable("app.services.report_render")
    # 空集上的全称命题恒真 —— 先断言入口真的被解析出了 import
    assert reachable["app.services.report_render"], "入口一个 import 都没解析到, 扫描器坏了"
    for module, targets in sorted(reachable.items()):
        for target in sorted(targets):
            assert target not in FORBIDDEN_PREFIXES and not target.startswith(
                tuple(p + "." for p in FORBIDDEN_PREFIXES)
            ), (
                f"{module} (由 report_render 传递 import 到) import 了 {target} —— "
                f"事实包/渲染器/两道检查一行 IO 都不许有 (SPEC-008 第二节)"
            )


def test_purity_walker__actually_resolves_first_party_imports():
    """扫描器的非空校验: report_service 的闭包必须走到 report_render。

    走不到说明相对导入解析坏了 —— 那时上面的纯度断言检查的是空集, 恒绿。
    """
    reachable = _reachable("app.services.report_service")
    assert "app.services.report_render" in reachable


def test_render_module_purity__entry_imports_whitelist_only():
    """变异 8 的守卫: 入口多 import 任何一个白名单外的模块 (哪怕是 os) 就红。"""
    path = _resolve_app_module(ENTRY_MODULE)
    assert path is not None
    targets = _import_targets(path, ENTRY_MODULE)
    assert targets, "入口一个 import 都没解析到, 扫描器坏了"
    extras = sorted(t for t in targets if not _whitelisted(t))
    assert not extras, (
        f"report_render import 了白名单之外的 {extras} —— 入口模块只许 "
        f"{ENTRY_IMPORT_WHITELIST} (SPEC-008 第二节, 定稿决定 N)"
    )


def test_purity_whitelist__future_import_collected_hence_whitelisted():
    """钉住 __future__ 的处置: 扫描器把 from __future__ import annotations 收成
    __future__, 白名单因此**必须**含它。扫描器哪天改成跳过它, 这条会红 ——
    那时白名单里那一项成了死项, 该一起删; 反过来把它从白名单里删掉,
    上面那条会红。两个方向都不许碰巧通过。"""
    path = _resolve_app_module(ENTRY_MODULE)
    assert path is not None
    assert "__future__" in _import_targets(path, ENTRY_MODULE)
    assert "__future__" in ENTRY_IMPORT_WHITELIST


# ===== 手造事实来源 =====

TZ = "Asia/Shanghai"
OPENED = datetime(2026, 8, 20, 6, 0, tzinfo=UTC)


def make_raw(**over: Any) -> rr.IncidentFactsRaw:
    incident: dict[str, Any] = {
        "id": 42, "zone_id": 1, "zone_name": "生鲜区", "sensor_id": 3,
        "severity": "high", "status": "resolved",
        "assigned_employee_id": 1, "assigned_employee_name": "Alex Chen",
        "assigned_employee_zone_id": 1,
        "acknowledged_by_employee_id": 2, "acknowledged_by_employee_name": "Bo Wang",
        "opened_at": OPENED,
        "assigned_at": OPENED + timedelta(minutes=3),
        "acknowledged_at": OPENED + timedelta(minutes=12),
        "resolved_at": OPENED + timedelta(hours=1, minutes=12),
        "resolved_by": "employee:2",
    }
    incident.update(over.pop("incident", {}))
    events = over.pop("events", [
        {"kind": "opened", "actor": "system", "detail": None, "at": OPENED},
        {"kind": "acknowledged", "actor": "employee:2", "detail": None,
         "at": OPENED + timedelta(minutes=12)},
        {"kind": "resolved", "actor": "employee:2", "detail": None,
         "at": OPENED + timedelta(hours=1, minutes=12)},
    ])
    return rr.IncidentFactsRaw(
        incident=incident, events=events,
        sensor_30d_count=over.pop("sensor_30d_count", 2),
        zone_concurrent=over.pop("zone_concurrent", 0),
    )


def make_facts(**over: Any) -> list[rr.Fact]:
    return rr.build_fact_pack(make_raw(**over), tz=TZ)


def make_body(**over: str) -> dict[str, str]:
    body = {
        "summary": "事故 {{incident_id}} 于 {{opened_at}} 开单, 已于 {{resolved_at}} 解决。",
        "handling": "主管把单派给了 {{assigned_to}}, 实际到场刷卡的是 {{ack_by}}, "
                    "从开单到接单用了 {{response_duration}}。",
        "impact": "全程耗时 {{handle_duration}}。",
        "notable": "",
        "suggestion": "",
    }
    body.update(over)
    return body


def fact_by_id(facts: list[rr.Fact], fact_id: str) -> rr.Fact:
    matches = [f for f in facts if f.id == fact_id]
    assert len(matches) == 1, fact_id
    return matches[0]


# ===== 二、事实包 =====


def test_fact_pack_covers_static_ids__full_incident():
    # 自动解决的事故才可能全清单无缺失 (人工解决时 resolve_policy 本来就没有)
    facts = make_facts(incident={"resolved_by": "policy:12@v3"})
    assert tuple(f.id for f in facts[:len(rr.STATIC_FACT_IDS)]) == rr.STATIC_FACT_IDS
    for fact in facts[:len(rr.STATIC_FACT_IDS)]:
        assert fact.value is not None, fact.id
        assert fact.text != rr.MISSING_TEXT, fact.id


def test_fact_pack_keeps_missing_entry__no_ack():
    """变异 7 的守卫: 缺失事实必须仍出条目, 不是不给 (定稿决定 E)。"""
    facts = make_facts(incident={
        "assigned_employee_id": None, "assigned_employee_name": None,
        "assigned_employee_zone_id": None,
        "acknowledged_by_employee_id": None, "acknowledged_by_employee_name": None,
        "acknowledged_at": None,
    })
    for fact_id in ("assigned_to", "ack_by", "cross_zone",
                    "response_duration", "onsite_duration", "acknowledged_at"):
        fact = fact_by_id(facts, fact_id)
        assert fact.value is None, fact_id
        assert fact.text == rr.MISSING_TEXT, fact_id
    # 缺一半不缺另一半: 开单到解决的时长照常在
    assert fact_by_id(facts, "handle_duration").text == "1 小时 12 分"


def test_fact_pack_durations__formatted_chinese():
    facts = make_facts()
    assert fact_by_id(facts, "response_duration").text == "12 分"
    assert fact_by_id(facts, "handle_duration").text == "1 小时 12 分"
    assert fact_by_id(facts, "onsite_duration").text == "1 小时"


def test_fact_pack_timestamps__use_passed_tz_not_environment():
    raw = make_raw()
    shanghai = rr.build_fact_pack(raw, tz=TZ)
    utc = rr.build_fact_pack(raw, tz="UTC")
    assert fact_by_id(shanghai, "opened_at").text == "2026-08-20 14:00"
    assert fact_by_id(utc, "opened_at").text == "2026-08-20 06:00"


def test_fact_pack_resolved_kind__all_three_prefixes():
    """resolved_by 有三个前缀 (SPEC-003 决策 4): employee: / user: 人工, policy: 自动。"""
    assert fact_by_id(make_facts(), "resolved_kind").text == "人工"  # employee:2
    assert fact_by_id(
        make_facts(incident={"resolved_by": "user:2"}), "resolved_kind"
    ).text == "人工"
    auto = make_facts(incident={"resolved_by": "policy:12@v3"})
    assert fact_by_id(auto, "resolved_kind").text == "自动"
    policy = fact_by_id(auto, "resolve_policy")
    assert policy.value == {"policy_id": 12, "version": 3}
    assert policy.text == "策略 12 第 3 版"


def test_fact_pack_resolve_policy__missing_for_human_resolution():
    facts = make_facts()  # employee:2
    assert fact_by_id(facts, "resolve_policy").text == rr.MISSING_TEXT
    # 未解决的事故: 解决来源整个缺失, 但条目还在
    unresolved = make_facts(incident={"resolved_by": None, "resolved_at": None})
    assert fact_by_id(unresolved, "resolved_kind").text == rr.MISSING_TEXT


def test_fact_pack_cross_zone__mismatch_and_null_zone_both_count():
    cross = make_facts(incident={"assigned_employee_zone_id": 2})
    assert fact_by_id(cross, "cross_zone").value is True
    nozone = make_facts(incident={"assigned_employee_zone_id": None})
    assert fact_by_id(nozone, "cross_zone").value is True
    same = make_facts()
    assert fact_by_id(same, "cross_zone").value is False
    assert fact_by_id(same, "cross_zone").text == "本区派单"


def _timeline(n: int) -> list[dict[str, Any]]:
    return [
        {"kind": f"k{i}", "actor": None, "detail": None,
         "at": OPENED + timedelta(minutes=i)}
        for i in range(n)
    ]


def test_fact_pack_timeline_truncation__over_twenty_keeps_head5_tail15():
    facts = make_facts(events=_timeline(27))
    tl = [f for f in facts if f.id.startswith("tl_") and f.id != "tl_truncated"]
    assert [f.id for f in tl] == [f"tl_{i}" for i in range(1, 21)]
    assert tl[0].value["kind"] == "k0"
    assert tl[4].value["kind"] == "k4"      # 最早 5 条
    assert tl[5].value["kind"] == "k12"     # 最晚 15 条从原第 13 条开始
    assert tl[19].value["kind"] == "k26"
    truncated = fact_by_id(facts, "tl_truncated")
    assert truncated.value == 7
    assert truncated.text == "另有 7 条时间线未列入"


def test_fact_pack_timeline_no_truncation__twenty_or_fewer():
    facts = make_facts(events=_timeline(20))
    assert len([f for f in facts if f.id.startswith("tl_")]) == 20
    assert all(f.id != "tl_truncated" for f in facts)
    # 没截断就没有"末段": label 全部是平铺的"时间线第 N 条"
    assert fact_by_id(facts, "tl_6").label == "时间线第 6 条"
    assert fact_by_id(facts, "tl_20").label == "时间线第 20 条"


def test_fact_pack_truncation_gap_visible__tail_labels_and_marker_position():
    """变异 10 的守卫 (定稿决定 O): label 不许说谎 —— tl_truncated 插在缺口处
    (tl_5 与 tl_6 之间, 不在末尾), 后半段 label 带"末段", 模型读 label 时不会把
    tl_6 当成紧接 tl_5 的第 6 条。id 与顺序不变, tl_1..tl_20 仍按保留顺序。"""
    facts = make_facts(events=_timeline(27))
    ids = [f.id for f in facts]
    assert ids.index("tl_truncated") == ids.index("tl_5") + 1
    assert ids.index("tl_6") == ids.index("tl_truncated") + 1
    assert ids[-1] == "tl_20"
    assert fact_by_id(facts, "tl_5").label == "时间线第 5 条"
    assert fact_by_id(facts, "tl_6").label == "时间线末段第 6 条"
    assert fact_by_id(facts, "tl_20").label == "时间线末段第 20 条"


# ===== 三、E_BARE_FACT (变异 1 / 5 / 6 / 9 / 11 的守卫) =====


def test_bare_fact_zero__clean_placeholder_prose():
    """变异 5 的反向: 正常输入计数必须为 0 —— 计数器写成恒返回 1 时这条要红。"""
    result = rr.check_draft(make_body(), make_facts())
    assert result.bare_fact_attempts == 0
    assert result.dangling_ref_attempts == 0
    assert result.ok, result.violations


# 验收点名的三个不误伤用例, 各写一条 (缺一条这道检查就可能被实现成噪音发生器);
# "十分明显"是判据 2 自己举的正常中文例子, 一并钉住。
@pytest.mark.parametrize("prose", [
    "处理人第一时间赶到现场。",
    "现场情况一般, 无扩散。",
    "本单为人工处理关闭。",
    "地面积水痕迹十分明显。",
])
def test_bare_fact_allows__idiomatic_chinese(prose: str):
    result = rr.check_draft(make_body(notable=prose), make_facts())
    assert result.bare_fact_attempts == 0, result.violations
    assert result.ok


# 与"第一时间"同形的三条 (序数"第X"+单位 / 量词"次"), 加上量词"级" —— 定稿决定 L:
# 按规则整体放行, 不按验收点名的个例修一个漏一个。
@pytest.mark.parametrize("prose", [
    "该探头本月第一次报警。",
    "这是一次跨区派单。",  # 事实包里 cross_zone 是"本区派单", 不撞判据 3 的专名
    "一次性处置完毕。",
    "升级为高风险。",
])
def test_bare_fact_allows__ordinal_and_counter_shapes(prose: str):
    result = rr.check_draft(make_body(notable=prose), make_facts())
    assert result.bare_fact_attempts == 0, result.violations
    assert result.ok


@pytest.mark.parametrize("prose", [
    "花了两个小时。",
    "半小时后到场。",
    "十来分钟就干了。",
    "一个半小时。",
    "三天后复查。",
])
def test_bare_fact_counts_duration_with_quantifier__natural_chinese(prose: str):
    """变异 11 的守卫: 约量词 (个/来/多/余) 与数词"半"让最自然的中文时长写法
    拦得住 —— 这是复核查出的漏拦, 不钉住会随下一次改正则悄悄退回去。"""
    result = rr.check_draft(make_body(impact=prose), make_facts())
    assert result.bare_fact_attempts == 1, (prose, result.violations)


@pytest.mark.parametrize("prose", ["开了两次单。", "到场两人。", "二十余条记录。"])
def test_bare_fact_passes_chinese_counts__dropped_by_design(prose: str):
    """已知边界的钉住: 中文写的计数**故意不拦** (单位表只留时间与时长)。
    这条红了说明有人把 次/条/人 加回了单位表 —— 先读 _CN_QUANTITY_RE 的注释
    (计数类事实的 text 本身带阿拉伯数字, 抄它只会撞 _ARABIC_RE) 再动手。"""
    result = rr.check_draft(make_body(notable=prose), make_facts())
    assert result.bare_fact_attempts == 0, result.violations


def test_bare_fact_counts_cross_zone_text__written_out_verbatim():
    """变异 9 的守卫 (定稿决定 M): "本区派单"/"跨区派单"不是日常词, 它就是那条
    事实本身 —— 原本既没人拦也没写进边界, 掉在缝里。"""
    result = rr.check_draft(make_body(notable="经确认这属于本区派单。"), make_facts())
    assert result.bare_fact_attempts == 1, result.violations
    cross_facts = make_facts(incident={"assigned_employee_zone_id": 2})
    result = rr.check_draft(make_body(notable="经确认这属于跨区派单。"), cross_facts)
    assert result.bare_fact_attempts == 1, result.violations


def test_bare_fact_counts_arabic_digits__one_per_run():
    result = rr.check_draft(
        make_body(handling="接单用了 3 分钟, 到场 2 人处置。"), make_facts()
    )
    assert result.bare_fact_attempts == 2
    assert not result.ok
    assert result.rendered is None


def test_bare_fact_counts_chinese_quantity__number_plus_unit_only():
    result = rr.check_draft(
        make_body(impact="前后耗了十二分钟, 又等了两小时才复位。"), make_facts()
    )
    assert result.bare_fact_attempts == 2


def test_bare_fact_counts_proper_noun__employee_name_written_out():
    result = rr.check_draft(
        make_body(handling="实际到场刷卡的是 Bo Wang。"), make_facts()
    )
    assert result.bare_fact_attempts == 1
    codes = {v.code for v in result.violations}
    assert codes == {rr.VIOLATION_BARE_FACT}


def test_bare_fact_counts_three__three_violations_in_one_round():
    """按违规项累加, 不按轮: 一轮里三处裸写就加 3 (定稿决定 H)。"""
    result = rr.check_draft(
        make_body(handling="他 3 分钟就到了, 又等了两小时, 期间 Bo Wang 在场。"),
        make_facts(),
    )
    assert result.bare_fact_attempts == 3


def test_bare_fact_ignores_placeholder_digits__timeline_refs():
    """变异 6 的守卫: 检查跑在剔除 {{...}} 之后 —— 不剔, {{tl_3}} 被自己拦死。"""
    body = make_body(handling="{{tl_3}} 之后复查见 {{tl_12}}。")
    violations = rr.check_bare_facts(body, make_facts())
    assert violations == []


def test_bare_fact_skips_severity_and_resolved_kind__daily_words():
    """判据 3: 分类类事实不进裸写黑名单 —— 正文里的"高"/"人工"不许被拦。"""
    facts = make_facts()  # severity=high -> text "高"; resolved_kind -> "人工"
    result = rr.check_draft(
        make_body(notable="现场风险较高, 已由人工复核确认。"), facts
    )
    assert result.bare_fact_attempts == 0
    assert result.ok


def test_bare_fact_counts_zone_name__proper_noun_from_pack():
    result = rr.check_draft(make_body(summary="生鲜区发生水浸。"), make_facts())
    assert result.bare_fact_attempts == 1


def test_bare_fact_skips_missing_facts__wu_ci_ji_lu_not_blacklisted():
    """缺失专名的 text 是"无此记录", 不进黑名单 —— 正文合法地写不出它之外的东西,
    但人话里真写了"无此记录"四个字也不该算裸写事实。"""
    facts = make_facts(incident={
        "acknowledged_by_employee_id": None, "acknowledged_by_employee_name": None,
        "acknowledged_at": None,
    })
    result = rr.check_draft(make_body(notable="接单人一栏无此记录。"), facts)
    assert result.bare_fact_attempts == 0


# ===== 四、E_DANGLING_REF (变异 2 的守卫) =====


def test_dangling_ref_rejected__unknown_id():
    result = rr.check_draft(make_body(handling="{{nonexistent}} 到场。"), make_facts())
    assert result.dangling_ref_attempts == 1
    assert not result.ok
    assert result.rendered is None


def test_dangling_ref_counts_each__two_unknown_refs():
    result = rr.check_draft(
        make_body(handling="{{nope}} 与 {{also_nope}} 到场。"), make_facts()
    )
    assert result.dangling_ref_attempts == 2


def test_dangling_ref_zero__all_refs_exist():
    result = rr.check_draft(make_body(), make_facts())
    assert result.dangling_ref_attempts == 0


def test_dangling_ref_truncated_timeline__dropped_tl_id_is_dangling():
    """截断后 tl_21 不存在 —— 引用它必须被拦, 而不是渲染成空。"""
    facts = make_facts(events=_timeline(27))
    result = rr.check_draft(make_body(handling="{{tl_21}}"), facts)
    assert result.dangling_ref_attempts == 1


# ===== 五、渲染与两道字符上限 =====


def test_render_replaces_placeholders__with_fact_text():
    result = rr.check_draft(make_body(), make_facts())
    assert result.ok
    assert result.rendered is not None
    assert "Bo Wang" in result.rendered["handling"]
    assert "12 分" in result.rendered["handling"]
    assert "{{" not in result.rendered["handling"]


def test_render_missing_fact__yields_wu_ci_ji_lu_not_a_name():
    """验收: 无人接单的事故, 报告里出现"无此记录", 不是空串也不是编造的人名。"""
    facts = make_facts(incident={
        "acknowledged_by_employee_id": None, "acknowledged_by_employee_name": None,
        "acknowledged_at": None,
    })
    result = rr.check_draft(
        make_body(handling="实际到场刷卡的是 {{ack_by}}。"), facts
    )
    assert result.ok
    assert result.rendered is not None
    assert "无此记录" in result.rendered["handling"]
    assert "Bo Wang" not in result.rendered["handling"]


def test_char_limit_after_render__long_placeholders_pass():
    """定稿决定 G: 占位符原文超 300 但渲染后不足 300 -> 通过, 不惩罚守规矩的模型。"""
    body = make_body(handling="{{response_duration}}" * 15)  # 原文 315 字符
    result = rr.check_draft(body, make_facts())
    assert result.ok, result.violations
    assert result.rendered is not None
    assert len(result.rendered["handling"]) < 300


def test_char_limit_after_render__rendered_overflow_rejected():
    result = rr.check_draft(make_body(handling="长" * 301), make_facts())
    assert not result.ok
    assert {v.code for v in result.violations} == {rr.VIOLATION_RENDERED_OVERFLOW}
    # 超长是格式问题不是编造倾向, 不进两个计数
    assert result.bare_fact_attempts == 0


def test_char_limit_before_render__800_hard_cap_on_raw():
    body = make_body(handling="{{response_duration}}" * 39)  # 原文 819 字符
    result = rr.check_draft(body, make_facts())
    assert not result.ok
    assert {v.code for v in result.violations} == {rr.VIOLATION_RAW_OVERFLOW}
    assert result.bare_fact_attempts == 0
    assert result.dangling_ref_attempts == 0


def test_check_draft_shape__missing_field_rejected():
    body = make_body()
    del body["suggestion"]
    result = rr.check_draft(body, make_facts())
    assert not result.ok
    assert {v.code for v in result.violations} == {rr.VIOLATION_BAD_SHAPE}


def test_check_draft_shape__extra_field_rejected():
    result = rr.check_draft(make_body() | {"extra": "x"}, make_facts())
    assert not result.ok
    assert rr.VIOLATION_BAD_SHAPE in {v.code for v in result.violations}


def test_render_body_raises__dangling_ref_not_silently_kept():
    with pytest.raises(ValueError):
        rr.render_body(make_body(handling="{{nope}}"), make_facts())
