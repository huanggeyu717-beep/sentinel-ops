"""模型客户端接口: 打桩 / 真实方舟 / 录制回放, 同一个 LLMClient 协议的三个实现。

接口在第一段就定死 (SPEC-002 第九节), 状态机只认 LLMClient —— 第一段那批
打桩驱动的确定性测试原样继续跑, 本段 (第二段) 加真实客户端与录制回放。

打桩必须能**按脚本吐一串固定响应**, 不是只吐一个: 修复循环的测试需要"第一次吐
错的 zone、第二次吐对"这样的序列, 澄清的测试需要"连错两次、第三次改口问人"。

录制回放 (SPEC-002 第九节三条落地规矩):
1. 键只由"会改变模型输出的输入"决定 —— 模型名、prompt 版本、思考开关、消息、
   可用工具。task_id、时间戳、随机串一律不进键: 这类东西每次都不一样, 键跟着变,
   回放永远命中不了, 而症状是"跑得通但每次都在真花钱", 看不到任何报错;
2. replay 下没命中**直接抛 ReplayMiss**, 不回退真模型 —— 回退是让 CI 在你
   不知道的时候花钱, 并把"离线可复跑"这条验收变成假的;
3. 进 git 的录制 (tests/cassettes/) 与临时录制 (.llm-cache/) 分开放, 分工见
   仓库根 .gitignore 的注释。手工编辑过的录制必须在文件里就地标注 (hand_edited
   字段), 否则下一个人会把它当成模型的真实行为去分析。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from time import perf_counter
from typing import Any, Protocol

import httpx


@dataclass(frozen=True)
class LLMToolCall:
    """模型要调的工具。arguments 是模型给的原始参数, 服务端各层照常各判各的 ——
    工具清单裁剪只是减少模型做无用功, 不是安全措施 (SPEC-002 第五节)。"""

    tool: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class LLMRequest:
    """一次模型调用的完整输入。

    messages 是 OpenAI 兼容的 {role, content} 列表 (第二段走火山方舟的兼容协议,
    接口形状现在就对齐, 免得换实现时连请求都要重定义);
    tools 是**按当前 stage 裁剪过的**工具 JSON Schema 列表。
    """

    task_id: int
    stage: str
    messages: list[dict[str, str]]
    tools: list[dict[str, Any]]


@dataclass(frozen=True)
class LLMResponse:
    """一次模型响应。tool_call 与 text 至少有一个; 计量字段每次调用落 ai_usage
    (SPEC-002 第九节), 打桩实现也落 —— 单任务 LLM 调用总数 ≤12 这条硬上限
    跨轮累加不重置, 靠数 ai_usage 的行数实现, 打桩不落账就数不到。"""

    tool_call: LLMToolCall | None = None
    text: str | None = None
    model: str = "stub"
    prompt_version: str = "stub-v0"
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    cache_hit: bool = False
    # 按 config 单价估的成本, **人民币元** (0009 起字段名与币种一致, 老名字
    # estimated_cost_usd 是 W4 的已知债)。价目会变, 只用于消融实验的相对比较,
    # 不是财务口径; 回放命中时为 0 (没花钱), 打桩为 0。
    estimated_cost_cny: float = 0.0


class LLMClient(Protocol):
    """状态机唯一认识的模型入口。实现: ScriptedLLMClient (本段) /
    录制回放与真实方舟客户端 (第二段)。"""

    async def complete(self, request: LLMRequest) -> LLMResponse: ...


class ScriptExhausted(Exception):
    """脚本吐完了还在要响应 —— 状态机走的路比测试脚本预期的长, 这是测试要修的
    信号, 不静默循环最后一条 (那会把状态机的死循环伪装成正常运行)。"""

    def __init__(self, task_id: int, stage: str, served: int) -> None:
        super().__init__(task_id, stage, served)
        self.task_id = task_id
        self.stage = stage
        self.served = served


@dataclass
class ScriptedLLMClient:
    """打桩实现: 按脚本顺序吐响应, 完全确定性。

    requests 记录每次调用的入参 (含裁剪后的工具清单), 测试可以断言
    "repairing 阶段只给了 update_policy_draft"这类裁剪行为。
    """

    script: list[LLMResponse]
    requests: list[LLMRequest] = field(default_factory=list)

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if len(self.requests) > len(self.script):
            raise ScriptExhausted(request.task_id, request.stage, len(self.script))
        return self.script[len(self.requests) - 1]


# ===== 真实模型才有的失败类别 (打桩下永远看不到) =====


class ModelProtocolError(Exception):
    """模型没按 OpenAI 兼容协议来: tool_calls 的 arguments 不是合法 JSON /
    既无 tool_calls 又无文本。归 model_protocol_error 落 failed —— 这是模型
    输出的问题不是系统故障, 用户重说一遍就能重开 (SPEC-002 第四节)。

    usage 带着这次调用真实花掉的 token: 协议错的调用照样计费, 调用方
    (agent_runtime) 要先落 ai_usage 再收口 —— 账单不会因为输出坏了而退款。
    """

    def __init__(self, detail: str, usage: LLMResponse | None = None) -> None:
        super().__init__(detail)
        self.usage = usage


class LLMCallTimeout(Exception):
    """单次 LLM 调用超时 (agent_llm_timeout_seconds)。与单工具 10 秒是两码事:
    工具是本地查询, 这是网络往返 (SPEC-002 第三节上限表)。"""


class LLMUnavailable(Exception):
    """网络/服务端错误, 模型没给出任何输出 (连不上、5xx、鉴权失败)。"""


class ReplayMiss(Exception):
    """replay 模式下没命中录制。直接失败, 不回退真模型 (SPEC-002 第九节)。"""

    def __init__(self, key: str, stage: str) -> None:
        super().__init__(key, stage)
        self.key = key
        self.stage = stage


# 请求的温度定值。W5 评测的六个臂全部温度 0 (SPEC-007 第四节配置矩阵), 不进
# config —— 进了 config 就成了一个可以被 .env 悄悄改掉、却不体现在任何 run
# 快照里的自由度; 消融要换温度时由 runner 显式构造客户端并写进 manifest。
DEFAULT_TEMPERATURE = 0.0


# ===== 真实方舟客户端 (OpenAI 兼容协议) =====


@dataclass
class ArkLLMClient:
    """火山方舟 chat/completions。key 只进请求头, 不进任何会被录制的结构。

    请求路径核对过 (W4 第二段补录修补四): 同一个 base_url 下方舟另有 /responses
    接口 (控制台示例用它), 请求与响应格式不同。本客户端走 OpenAI 兼容的
    /chat/completions, 工具调用字段 tools / choices[0].message.tool_calls,
    已用真实调用验证 (scripts/dev/record_cassettes.py smoke)。
    """

    base_url: str
    api_key: str
    model: str
    prompt_version: str
    timeout_seconds: float
    price_input_per_mtok: float
    price_output_per_mtok: float
    # 深度思考开关。seed-2.1 默认开思考, 单步 80 秒级, 60 秒调用上限必爆 ——
    # 默认值与取舍见 config.llm_thinking 注释。它改变模型输出, 必须进回放键。
    thinking: str = "disabled"
    # 温度显式设 0, 不走服务端默认 (SPEC-007 第五节): 把人话翻译成受限 DSL 是
    # 有唯一或少数正确答案的翻译, 不需要创造性; 温度在飘, 消融两臂的差可能只是
    # 运气, agentic 轨迹也会在第二步就分叉、后面全部 miss。
    # **温度 0 不等于确定性** —— 厂商侧批处理、MoE 路由、模型版本灰度都会分叉,
    # 它是把噪音压小, 不是消掉; 这也正是为什么还要归档轨迹 (SPEC-007 已知边界)。
    temperature: float = 0.0
    transport: httpx.AsyncBaseTransport | None = None  # 测试注入 MockTransport 用

    async def complete(self, request: LLMRequest) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": request.messages,
            "thinking": {"type": self.thinking},
            "temperature": self.temperature,
        }
        if request.tools:
            # 中立形状 {name, description, parameters} 在这里才换成 OpenAI 的
            # function 嵌套 —— 协议细节不外溢给状态机与录制键
            payload["tools"] = [
                {"type": "function", "function": t} for t in request.tools
            ]
        t0 = perf_counter()
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds, transport=self.transport
            ) as client:
                resp = await client.post(
                    f"{self.base_url}/chat/completions",
                    json=payload,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
        except httpx.TimeoutException as e:
            raise LLMCallTimeout(
                f"单次 LLM 调用超过 {self.timeout_seconds} 秒 (stage={request.stage})"
            ) from e
        except httpx.HTTPError as e:
            raise LLMUnavailable(f"LLM 请求失败: {type(e).__name__}: {e}") from e
        latency_ms = int((perf_counter() - t0) * 1000)
        if resp.status_code != 200:
            raise LLMUnavailable(
                f"LLM 返回 {resp.status_code}: {resp.text[:200]}"
            )
        return self._parse(resp.json(), latency_ms)

    def _parse(self, data: dict[str, Any], latency_ms: int) -> LLMResponse:
        usage = data.get("usage") or {}
        input_tokens = int(usage.get("prompt_tokens") or 0)
        output_tokens = int(usage.get("completion_tokens") or 0)
        # 价目会变, 单价在 config 里; 这个数字只用于相对比较, 不是财务口径
        cost = (
            input_tokens * self.price_input_per_mtok
            + output_tokens * self.price_output_per_mtok
        ) / 1_000_000
        base = LLMResponse(
            model=self.model, prompt_version=self.prompt_version,
            input_tokens=input_tokens, output_tokens=output_tokens,
            latency_ms=latency_ms, estimated_cost_cny=cost,
        )
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as e:
            raise ModelProtocolError(f"响应里没有 choices[0].message: {e}", base) from e
        tool_calls = message.get("tool_calls") or []
        text = message.get("content") or None
        if not tool_calls:
            if text is None:
                # 空 tool_calls 又不给文本: 真模型最常见的协议违约之一
                raise ModelProtocolError("模型既没调工具也没给文本", base)
            return replace(base, text=text)
        function = tool_calls[0].get("function") or {}
        raw_arguments = function.get("arguments") or "{}"
        try:
            # OpenAI 协议里 arguments 是一个 JSON **字符串**, 且可能不是合法
            # JSON —— 必须在这里归成 model_protocol_error, 不能让裸的
            # JSONDecodeError 冒进 _tool_step 的通用兜底落成 dead_letter
            arguments = json.loads(raw_arguments)
        except json.JSONDecodeError as e:
            raise ModelProtocolError(
                f"tool_calls.arguments 不是合法 JSON: {e}: {raw_arguments[:200]}", base
            ) from e
        if not isinstance(arguments, dict):
            raise ModelProtocolError(
                f"tool_calls.arguments 不是对象: {raw_arguments[:200]}", base
            )
        return replace(
            base,
            tool_call=LLMToolCall(tool=str(function.get("name")), arguments=arguments),
            text=text,
        )


# ===== 录制回放 (record / replay; off = 不套这层直接用 ArkLLMClient) =====


def cassette_key(
    model: str, prompt_version: str, request: LLMRequest,
    thinking: str = "disabled", temperature: float = 0.0,
) -> str:
    """回放键: 只含会改变模型输出的输入 —— 模型名、prompt 版本、思考开关、
    温度、消息、可用工具。

    task_id / stage / 时间戳都不进键 (stage 已完整体现在 system prompt 与工具
    清单里, 单独进键只会让"同内容不同标签"失配)。thinking 与 temperature 进键
    的理由与模型名一样: 一翻输出就不同, 老录制冒充新配置的行为比 miss 更糟
    (SPEC-007 第五节, 对 SPEC-002 的修订 3)。
    """
    canonical = json.dumps(
        {
            "model": model,
            "prompt_version": prompt_version,
            "thinking": thinking,
            "temperature": temperature,
            "messages": request.messages,
            "tools": request.tools,
        },
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass
class RecordReplayLLMClient:
    """录制回放包装: record 真调 inner 并落盘, replay 只读盘、没命中就死。

    cassette 是一个 JSON 文件: request 部分给人看 (键由哪些字段算出来一目了然),
    response 部分是回放时真正读的。手工编辑过的 cassette 必须加 hand_edited
    字段就地说明改了什么 (SPEC-002 第九节)。
    """

    inner: LLMClient | None  # replay 模式允许为 None (离线复跑不需要真客户端)
    directory: Path
    mode: str  # record / replay
    model: str
    prompt_version: str
    thinking: str = "disabled"    # 进回放键, 见 cassette_key
    temperature: float = 0.0      # 同上 (SPEC-007 对 SPEC-002 的修订 3)

    def __post_init__(self) -> None:
        if self.mode not in ("record", "replay"):
            raise ValueError(f"未知回放模式 {self.mode!r} (off 不套这层)")

    def _path(self, key: str) -> Path:
        return self.directory / f"{key}.json"

    async def complete(self, request: LLMRequest) -> LLMResponse:
        key = cassette_key(
            self.model, self.prompt_version, request, self.thinking, self.temperature
        )
        path = self._path(key)
        if self.mode == "replay":
            if not path.exists():
                # 不回退真模型: 回退看着 friendly, 实际是让 CI 在你不知道的
                # 时候花钱, 并把"离线可复跑"这条验收变成假的
                raise ReplayMiss(key, request.stage)
            recorded = json.loads(path.read_text())["response"]
            tool_call_data = recorded.pop("tool_call", None)
            tool_call = (
                LLMToolCall(tool=tool_call_data["tool"],
                            arguments=tool_call_data["arguments"])
                if tool_call_data is not None else None
            )
            recorded.pop("latency_ms", None)
            recorded.pop("estimated_cost_cny", None)
            # 改名前录的老 cassette 里成本键还叫 estimated_cost_usd。不 pop 掉
            # 它就会作为未知关键字参数传进 LLMResponse 直接 TypeError —— 重录后
            # 老键不再出现, 但正确性不许指望重录来掩盖 (W5 第一段 prompt 第 5 条)。
            recorded.pop("estimated_cost_usd", None)
            # cache_hit 真填 (W5 算成本要用); 成本与延迟归零 —— 回放没花钱也
            # 没等网络, 留着录制值会让"拿回放回填预算"这种错误测量看起来可信
            return LLMResponse(
                **recorded, tool_call=tool_call,
                cache_hit=True, latency_ms=0, estimated_cost_cny=0.0,
            )
        if self.inner is None:
            raise ValueError("record 模式必须有 inner 客户端")
        response = await self.inner.complete(request)
        self.directory.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "key_fields": {
                        "model": self.model,
                        "prompt_version": self.prompt_version,
                        "thinking": self.thinking,
                        "temperature": self.temperature,
                        "messages": request.messages,
                        "tools": [t["name"] for t in request.tools],
                    },
                    "stage": request.stage,  # 不进键, 只为让人认得出这是哪一步
                    # 同样不进键: 评测 runner 靠它把 cassette 归到用例
                    # (results.jsonl 的 cassette_keys 列, SPEC-007 第五节归档格式)
                    "task_id": request.task_id,
                    "response": _response_dict(response),
                },
                ensure_ascii=False, indent=2,
            )
        )
        return response


def _response_dict(response: LLMResponse) -> dict[str, Any]:
    d: dict[str, Any] = {
        "tool_call": (
            {"tool": response.tool_call.tool, "arguments": response.tool_call.arguments}
            if response.tool_call is not None else None
        ),
        "text": response.text,
        "model": response.model,
        "prompt_version": response.prompt_version,
        "input_tokens": response.input_tokens,
        "output_tokens": response.output_tokens,
        "latency_ms": response.latency_ms,
        "estimated_cost_cny": response.estimated_cost_cny,
    }
    return d


def build_llm_client(
    *, prompt_version: str, directory: Path | None = None, mode: str | None = None
) -> LLMClient:
    """按配置组装模型客户端 (第三段 HTTP 层与脚本的统一入口)。

    mode: record / replay 套录制回放层, off 直连真模型。
    replay 模式不建真客户端 —— 离线复跑不需要 key、不该有连网的可能。
    """
    from ..config import settings

    cfg = settings()
    # A0 档的 prompt 是另一种 (v3-a0), 版本号在这里收口而不是在路由层:
    # 路由层不随消融档变 (每臂一个 API 进程, 档位来自 config), 而 prompt_version
    # 进 ai_usage 与回放键, 档位分不开的话两处都串味 (SPEC-007 补入 28)。
    if cfg.agent_ablation_level == "A0":
        from . import agent_prompts

        prompt_version = agent_prompts.PROMPT_VERSION_A0
    resolved_mode = mode if mode is not None else cfg.llm_replay_mode
    resolved_dir = directory if directory is not None else Path(cfg.llm_record_replay_dir)
    ark = (
        None
        if resolved_mode == "replay"
        else ArkLLMClient(
            base_url=cfg.llm_base_url, api_key=cfg.llm_api_key,
            model=cfg.llm_model, prompt_version=prompt_version,
            timeout_seconds=cfg.agent_llm_timeout_seconds,
            price_input_per_mtok=cfg.llm_price_input_per_mtok,
            price_output_per_mtok=cfg.llm_price_output_per_mtok,
            thinking=cfg.llm_thinking,
            temperature=DEFAULT_TEMPERATURE,
        )
    )
    if resolved_mode == "off":
        assert ark is not None
        return ark
    return RecordReplayLLMClient(
        inner=ark, directory=resolved_dir, mode=resolved_mode,
        model=cfg.llm_model, prompt_version=prompt_version,
        thinking=cfg.llm_thinking,
        temperature=DEFAULT_TEMPERATURE,
    )
