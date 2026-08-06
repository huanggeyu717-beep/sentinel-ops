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

    # --- W2 事故生命周期 (SPEC-003) ---
    # 传感器连续干燥超过该秒数才自动解决事故, 防止读数在阈值附近抖动时事故反复开关
    auto_resolve_dry_seconds: int = 300

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
