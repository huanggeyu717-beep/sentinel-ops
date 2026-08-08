from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://sentinel:sentinel@localhost:5432/sentinel"
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

    # --- LLM (W4 起使用) ---
    llm_base_url: str = "https://ark.cn-beijing.volces.com/api/v3"  # 火山方舟, OpenAI 兼容
    llm_api_key: str = ""
    llm_model: str = "doubao-lite-32k"
    llm_record_replay_dir: str = ".llm-cache"  # 评测回放缓存

    model_config = SettingsConfigDict(
        env_prefix="SENTINEL_", env_file=".env", extra="ignore"
    )


@lru_cache
def settings() -> Settings:
    return Settings()
