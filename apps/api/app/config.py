from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # 端口 5433 不是默认的 5432: 本机 Homebrew postgresql@17 占着 loopback 5432
    # (别的项目在用, 不动它), 本项目的 Docker Postgres 让路, 宿主机侧映射到 5433
    # (docker-compose.yml)。容器内部与 CI 的 Postgres 各自独立, 仍是 5432。
    database_url: str = "postgresql+asyncpg://sentinel:sentinel@localhost:5433/sentinel"
    jwt_secret: str = "dev-only-change-me"
    # 非 development 环境: 默认 JWT 密钥直接拒绝启动, 且会话 cookie 加 Secure (SPEC-004)
    environment: str = "development"

    # --- 登录限流 (SPEC-004 决策 10, 按来源 IP) ---
    login_rate_limit_attempts: int = 10      # 窗口内允许的失败次数
    login_rate_limit_window_seconds: int = 300
    # 只有部署在自己的反向代理后面才打开; 打开后必须由代理覆盖 X-Forwarded-For
    trust_proxy_headers: bool = False

    # --- W1 运行时行为 ---
    apply_dev_seed: bool = True          # 启动时写入演示门店/传感器/员工, 生产置 false
    heartbeat_timeout_seconds: int = 60  # 沿用 legacy status-api Lambda 的在线判定语义
    default_wet_threshold: int = 500     # 事件未带 state 时, 由 value 推导 wet 的兜底阈值

    # --- W3 策略引擎 (SPEC-006) ---
    # 引擎 tick 间隔 (秒)。必须与回放模块的 DEFAULT_TICK_SECONDS 保持一致,
    # 否则模拟结果对线上没有预测力 (SPEC-001 第一节)。
    # W2 的自动关单稳定窗口配置项已整体删除: 其职责归 sensor_dry_for 触发器的
    # dry_for_s 参数, 且现在可以按区配不同的值 (SPEC-006 第四节)。
    engine_tick_seconds: int = 10
    # 引擎状态里已解决事故的保留条数, 超出丢最旧 (SPEC-001 第四节末: 内存必须有
    # 上界)。未解决的不受此限 —— 数量由数据库的 partial unique index 封顶。
    engine_incident_history: int = 200

    # --- W2 Dashboard 演练 (SPEC-005 前置 B) ---
    drill_speed: float = 10.0     # 演练回放的加速倍率 (与 make sim 一致), 回显在 GET /drills/{id}
    drill_history_limit: int = 20  # 内存中保留的最近演练记录数, 超出丢最旧 (决策 4: 必须有上界)

    # --- W4 Agent 编排 (SPEC-002) ---
    # 打卡间隔与判死阈值是一对, **判死必须远大于打卡** (这里是 12 倍):
    # 改小判死平时看不出问题, 一次垃圾回收停顿或虚拟机暂停就会误杀正在好好跑的
    # 任务 —— 这是那种"上线才炸"的数字。一个进程没法判断另一个进程是死了还是
    # 只是很慢, 所以判死不是"确定它死了", 而是"把它踢出局", 由 runner_id 闸保证
    # 它就算活过来也写不进去 (SPEC-002 第二节, 租约与栅栏)。
    agent_heartbeat_seconds: int = 5
    agent_lease_timeout_seconds: int = 60
    agent_round_budget_seconds: int = 120   # 单轮执行预算, 只算机器在跑的时间, 跨轮不累加
    agent_task_ttl_hours: int = 24          # 停在 clarifying 超过它 -> dead_letter
    agent_max_clarify_rounds: int = 3       # 超出 -> failed
    agent_max_llm_calls: int = 12           # 硬上限, 跨轮累加不重置 (防来回绕圈烧账单)
    agent_tool_timeout_seconds: int = 10    # 单工具; 已实测 simulate 中位 16ms, 绰绰有余
    # 单次 LLM 调用超时。与"单工具 10 秒"是两码事: 工具是本地查询与纯函数,
    # LLM 是网络往返。必须**小于 agent_round_budget_seconds**, 否则一次卡住的
    # 调用就把整轮拖死, 谁超时的都分不清 (SPEC-002 第三节上限表)。
    agent_llm_timeout_seconds: int = 60
    # 本进程内**同时在跑的 Agent 后台协程数**上界 (W4 第三段 HTTP 层)。管的是
    # asyncio.create_task 拉起的任务轮 (每条 = 若干次真实模型调用), 超出时
    # POST /agent-tasks 与 reply 返回 429, 不建行、不开协程。SPEC-005 决策 4
    # "内存里的东西必须有上界"的同一条理由 —— 一个人可以同时提交多条不同输入,
    # 没上界等于把账单与进程交给客户端节流。只在 clarifying 里等人的任务不占额
    # (那时没有协程在跑)。
    agent_max_concurrent_tasks: int = 4
    # 修复次数上限 2 是每轮澄清后重置的 (与 max_llm_calls 相反, SPEC-002 第三节
    # 写死的一对), 不进配置 —— 它是状态机语义的一部分, 见 agent_runtime。

    # --- LLM (W4 起使用) ---
    llm_base_url: str = "https://ark.cn-beijing.volces.com/api/v3"  # 火山方舟, OpenAI 兼容
    llm_api_key: str = ""
    # W4 第二段实开的模型 (W1 填的 doubao-lite-32k 早已下线)。另开通了同代的
    # Doubao-Seed-2.1-turbo, 本轮不用, 留给 W5 做同代降档的对比臂。
    llm_model: str = "doubao-seed-2-1-pro-260628"
    # 深度思考开关 (方舟 chat/completions 的 thinking.type: enabled/disabled/auto)。
    # 默认 disabled: seed-2.1-pro 默认开思考, 实测同一个 parsing 请求默认档 83 秒 /
    # 2533 个 reasoning token, 单次 LLM 调用 60 秒的上限直接爆掉; 关掉后 1.9 秒
    # (scripts/dev/probe_parsing_latency.py 两臂对照量的)。W5 消融可翻 enabled
    # 比质量。它改变模型输出, 所以进回放键 (llm_client.cassette_key)。
    llm_thinking: str = "disabled"
    # 录制回放三态 (SPEC-002 第九节): record = 真调并落盘; replay = 只读录制,
    # **没命中直接失败, 不回退真模型** (回退是让 CI 在你不知道的时候花钱);
    # off = 直连真模型不落盘。CI 固定 replay。
    llm_replay_mode: str = "record"
    llm_record_replay_dir: str = ".llm-cache"  # 开发时随手产生的录制, 不进版本库;
    # 测试与评测依赖的录制在 apps/api/tests/cassettes/, 进版本库 (见 .gitignore 注释)
    # 单价是**人民币元 / 百万 token** (方舟 2026-08 刊例价, 不折美元 —— 折算引入
    # 一个天天在变的汇率, 同一次调用今天和下周算出来不一样, 可复现性就没了)。
    # 算出的成本暂时落在名叫 estimated_cost_usd 的列里: 列名与币种不符是已知债,
    # 改列名的迁移留给 W5 真正用到成本口径时一并做 (SPEC-002 第九节末), 在那之前
    # 这一列存的就是人民币元。价目会变, 只用于消融实验的相对比较, 不是财务口径。
    llm_price_input_per_mtok: float = 6.00
    llm_price_output_per_mtok: float = 30.00

    model_config = SettingsConfigDict(
        env_prefix="SENTINEL_", env_file=".env", extra="ignore"
    )


@lru_cache
def settings() -> Settings:
    return Settings()
