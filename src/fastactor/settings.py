from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FASTACTOR_")

    mailbox_size: int = 1024
    call_timeout: float = 5
    stop_timeout: float = 60
    supervisor_max_restarts: int = 1
    supervisor_max_seconds: float = 5.0
    telemetry_enabled: bool = False
    telemetry_service_name: str = "fastactor"


settings = Settings()
