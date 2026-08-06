"""scenario —— 场景数据的读取与规范化 (SPEC-005 方案 B 抽包)。

device-sim 与 API 的演练 (drill) 服务共用这一个包:
读场景 (YAML 剧本 / CSV 历史回放) -> 规范化事件流; 时间轴换算。

**IO 边界: 允许读场景文件; 禁止发网络请求、禁止碰数据库。**
HTTP 投递、进度打印、按 --speed 推进时间都属于调用方
(apps/device-sim/sim.py 或 API 的 drill 服务)。往这里塞 HTTP 客户端,
它就退化成第二个模拟器 —— 那正是 SPEC-005 选方案 B 要避免的。
(注意: packages/policy_engine 是"零 IO", 标准比这里更严, 两个包的边界不同, 不要混为一谈。)
"""
from .loader import HEARTBEAT_EVERY_S, Source, events_from_csv, events_from_yaml, load_source
from .timeline import resolve_epoch, to_payload

__all__ = [
    "HEARTBEAT_EVERY_S",
    "Source",
    "events_from_csv",
    "events_from_yaml",
    "load_source",
    "resolve_epoch",
    "to_payload",
]
