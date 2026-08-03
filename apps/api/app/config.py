from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://sentinel:sentinel@localhost:5432/sentinel"
    jwt_secret: str = "dev-only-change-me"

    # --- W1 运行时行为 ---
    apply_dev_seed: bool = True          # 启动时写入演示门店/传感器/员工, 生产置 false
    heartbeat_timeout_seconds: int = 60  # 沿用 legacy status-api Lambda 的在线判定语义
    default_wet_threshold: int = 500     # 事件未带 state 时, 由 value 推导 wet 的兜底阈值

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
