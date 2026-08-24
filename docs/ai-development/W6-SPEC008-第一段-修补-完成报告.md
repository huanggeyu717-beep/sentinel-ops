# W6 SPEC-008 第一段 修补 — 完成报告

> 执行: CC, 2026-08-23。范围: 按更新后的 SPEC-008 (定稿决定 L / M / N / O / P)
> 修补四处 —— 中文口径、cross_zone 黑名单、纯度白名单、截断 label。
> 只动了 `report_render.py`、`test_report_render.py` 与探针
> `probe_bare_fact_cn.py` (仅两处 ruff 修正, 见第三节), 别的文件一行未动。

## 一、与 SPEC 不一致的地方

1. **无实质不一致。** 四件事全部按 SPEC 第二节、第三节判据 2 与 3、定稿决定
   L / M / N / O 的字面落地: 中文半边换成
   `(?<!第)[数词含半]+(约量词个/来/多/余)?(分钟|小时|秒钟|秒|天)`;
   `cross_zone` 进 `_PROPER_NOUN_FACT_IDS`; 入口模块纯度断言改白名单;
   截断后半段 label 带"末段"、`tl_truncated` 插在 tl_5 与 tl_6 之间。

2. **两处 SPEC 未定口径, 实现自定并有测试钉住**:
   - **`__future__` 的处置** (任务书易错点 4 预告的二选一): 扫描器会把
     `from __future__ import annotations` 收成 `__future__`, 本实现选择**放进
     白名单**而不是在扫描器里排除。钉住的是
     `test_purity_whitelist__future_import_collected_hence_whitelisted`:
     扫描器哪天改成跳过它, 这条红 (白名单里那项成死项); 从白名单里删掉它,
     白名单主测红 —— 两个方向都不许碰巧通过;
   - **"时间线末段第 N 条"的 N 取 tl 序号** (6..20, 与 id `tl_6..tl_20` 对齐),
     不取"末段内第几条"。SPEC 第二节明写 id 与顺序不变、只改 label,
     N 跟 id 走是唯一不产生第二套编号的读法。

3. **已知边界照 SPEC 抄进了 `_CN_QUANTITY_RE` 的注释** (没有另发明说法):
   丢掉的三条 (`两次` / `两人` / `二十余条`) 是故意的 —— 计数类事实的 text
   本身带阿拉伯数字, 抄它只会撞 `_ARABIC_RE`; 残留的洞是"模型主动把 2 次译成
   两次", 以及"三分"/"一个多小时"/"好几个小时"这类不带标准单位的写法。
   序数放行的代价 ("第三次报警"的"三"拦不住) 与 `(?<!第)` 只挡紧邻一个字,
   也都写在同一段注释里。

## 二、新增分支与写操作, 各由哪条测试守着

本轮**零写操作**: 不碰迁移、不碰 service, 全部改动在纯函数层与它的测试。

| 分支 / 改动 | 守着它的测试 |
|---|---|
| 中文半边新口径: 只拦时间与时长, 允许一个约量词, 数词含"半" | `test_bare_fact_counts_duration_with_quantifier__natural_chinese` (5 例, 各断言恰为 1) |
| 序数"第X"整体放行 + 量词"次/级"出单位表 | `test_bare_fact_allows__ordinal_and_counter_shapes` (4 例: "第一次报警"/"一次跨区派单"/"一次性"/"高风险") |
| 中文计数故意不拦 (已知边界钉住, 防止有人把 次/条/人 加回去) | `test_bare_fact_passes_chinese_counts__dropped_by_design` (3 例) |
| `cross_zone` 进专名黑名单 | `test_bare_fact_counts_cross_zone_text__written_out_verbatim` ("本区派单"与"跨区派单"各断言计数 1) |
| 入口 import 白名单 (re / collections.abc / dataclasses / datetime / typing / zoneinfo / `__future__`), 断言消息点名多出来的模块 | `test_render_module_purity__entry_imports_whitelist_only` (含"扫描器非空"自检); 传递闭包黑名单测试原样保留 |
| `__future__` 放白名单这个选择本身 | `test_purity_whitelist__future_import_collected_hence_whitelisted` |
| 截断后半段 label 带"末段"、`tl_truncated` 插在缺口处 | `test_fact_pack_truncation_gap_visible__tail_labels_and_marker_position` |
| 不截断时没有"末段" label | `test_fact_pack_timeline_no_truncation__twenty_or_fewer` (补两条 label 断言) |

任务书点名的回归风险逐一核过: `test_bare_fact_skips_severity_and_resolved_kind`
在 cross_zone 加入黑名单后仍绿 (severity / resolved_kind 没跟着进);
约量词写的是 `(?:个|来|多|余)?` 而不是 `.{0,1}`;
"这是一次跨区派单"用例跑在默认事实包上 (cross_zone 的 text 是"本区派单"),
放行验证的是判据 2, 不会被判据 3 顺手接住。

## 三、数字与产生它的配置

- **`test_report_render.py` 52 条全绿** (修补前 36 条, 净增 16 条):
  从仓库根 `pytest apps/api/tests/test_report_render.py`, 库为 docker-compose
  的 Postgres 16 (宿主机 5433, 库 sentinel_test; 本文件自身零库调用,
  连库是目录级夹具所致, 上一份报告第一节第 4 条已说明)。
- **api 档全量 381 passed / 1 skipped** (`bash scripts/ci/test-api.sh`),
  修补前 365 —— 净增的 16 条即上表。
- **单元档 299 passed** (`bash scripts/ci/test-unit.sh`), 与修补前持平。
- **探针一 (中文口径)**: 误伤 3/9 -> **0/9**, 漏拦 5/10 -> **3/10**,
  "现在拦"与"候选拦"两栏完全重合 (输出见下)。
- **探针二 (纯度)**: `import os + os.environ` 那一行从修补前的
  "**全绿 — 没人守**"变成"**红 (有人守)**", 红的正是新增的白名单测试 (输出见下)。
- **探针脚本自身的两处 ruff 修正** (文件边界内的"必要时"): 去掉一个失效的
  `noqa: E402`、`str(now)` 改 `now!s` —— 修补前 `lint.sh` 在这两处退出码 1,
  修后 0。探针逻辑一个字符没动。

## 四、五个 CI 脚本退出码与两个探针输出

| 脚本 | 退出码 | 备注 |
|---|---|---|
| `bash scripts/ci/lint.sh` | 0 | ruff 0.16.1 + mypy, 151 个文件 (第一遍红在探针脚本的两处 ruff, 见第三节, 修后全绿) |
| `bash scripts/ci/test-unit.sh` | 0 | 299 passed |
| `bash scripts/ci/test-api.sh` | 0 | 381 passed, 1 skipped |
| `bash scripts/ci/test-eval-smoke.sh` | 0 | 10 条全过, 零回放 miss, 零注入得逞 |
| `bash scripts/ci/test-docker.sh` | 0 | 容器内迁移与断言全过, 含 SSE 首事件断言 |

探针一 (`cd apps/api && python3 ../../scripts/dev/probe_bare_fact_cn.py`) 尾部:

```
误伤 现在 0/9 -> 候选 0/9
漏拦 现在 3/10 -> 候选 3/10
```

探针二 (`python3 scripts/dev/probe_render_purity.py`) 全文:

```
import asyncpg (直接违规)                    退出码=1  红 (有人守)
    FAILED tests/test_report_render.py::test_render_module_purity__no_io_in_transitive_imports
    FAILED tests/test_report_render.py::test_render_module_purity__entry_imports_whitelist_only
import os + os.environ (声称不做, 但没人守)      退出码=1  红 (有人守)
    FAILED tests/test_report_render.py::test_render_module_purity__entry_imports_whitelist_only

还原后基线 退出码=0 失败=(无)
```

## 五、变异测试 (SPEC 第十一节, 第一段做除第 3 条外的全部 10 条)

每条都是**真改坏、真跑、看红、改回**; 全部还原后 52 条复归全绿,
五个 CI 脚本在还原后跑, 全 0。

| # | 改坏的是哪一行 | 红的是哪条测试 |
|---|---|---|
| 1 | `check_bare_facts` 函数体首行插 `return []` | 11 条红: 5 条既有 counts 测试 + 新增的 5 例约量词与 1 条 cross_zone |
| 2 | `check_dangling_refs` 函数体首行插 `return []` | 3 条 `test_dangling_ref_*` 红 |
| 4 | 真库 `DROP INDEX incident_reports_one_active` (等价于迁移里拆掉那条 CREATE) | `test_incident_reports_one_active__second_nondiscarded_blocked` 红; 红跑期间第二行 draft 真插进去了, 还原时先 TRUNCATE 再重建索引 |
| 5 | `DraftCheckResult.bare_fact_attempts` 改成 `return 1` | **两个方向一次看全**, 19 条红: "正常输入 -> 0"方向红了 zero / idiomatic 4 例 / ordinal 4 例 / dropped-by-design 3 例等; "该加的时候加对了数"方向红了 counts_arabic (期望 2)、counts_three (期望 3) 等 |
| 6 | `_strip_placeholders` 改成返回原文 (不剔 `{{...}}`) | `test_bare_fact_ignores_placeholder_digits__timeline_refs` 红 |
| 7 | `ack_by` 缺失分支改成不产条目 + 注释掉产全自检断言 | 3 条红: render 档 2 条 + `test_report_service.py::test_facts_end_to_end__unacked_incident_renders_missing` |
| 8 | `import os` + `os.environ.get(...)` 写进 `report_render` (由探针二执行, 真改真跑真还原) | `test_render_module_purity__entry_imports_whitelist_only` 红, 断言消息点名 `os` —— 修补前同一变异**全绿** |
| 9 | `cross_zone` 从 `_PROPER_NOUN_FACT_IDS` 拿掉 | `test_bare_fact_counts_cross_zone_text__written_out_verbatim` 红 |
| 10 | 后半段 label 改回"时间线第 N 条" + `tl_truncated` 挪回末尾 | `test_fact_pack_truncation_gap_visible__tail_labels_and_marker_position` 红 |
| 11 | 正则里 `(?:个|来|多|余)?` 去掉 | `test_bare_fact_counts_duration_with_quantifier__natural_chinese` 的"花了两个小时"与"十来分钟"两例红 ("半小时"/"一个半小时"/"三天"不经约量词, 仍拦得住, 不红是对的) |

## 六、改动文件清单与建议 commit message

修改 (共三个文件, 无新增):

- `apps/api/app/services/report_render.py`
- `apps/api/tests/test_report_render.py`
- `scripts/dev/probe_bare_fact_cn.py` (仅两处 ruff 修正)
- `docs/ai-development/W6-SPEC008-第一段-修补-完成报告.md` (本文件)

建议 commit message (git 由本人执行):

```
fix(report): 收紧第一段四处口径 —— 中文时长/专名黑名单/纯度白名单/截断 label (SPEC-008 定稿决定 L-O)

- E_BARE_FACT 中文半边只拦时间与时长, 允许一个约量词, 序数"第X"整体放行;
  误伤 3/9 -> 0/9, 漏拦 5/10 -> 3/10 (scripts/dev/probe_bare_fact_cn.py 实测)
- cross_zone 进专名黑名单: "跨区/本区派单"就是那条事实本身, 不是日常词
- 纯度断言入口改 import 白名单, import os + 读环境变量当场红
  (scripts/dev/probe_render_purity.py 实测), 传递闭包黑名单保留
- 时间线截断后 label 标"末段"、tl_truncated 插在缺口处 —— label 不许说谎
- 变异测试补到 10 项逐一改坏验红; 52 条单测与五个 CI 脚本全绿
```
