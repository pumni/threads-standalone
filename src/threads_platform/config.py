from pydantic import AnyHttpUrl
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="THREADS_PLATFORM_",
        env_ignore_empty=True,
        extra="ignore",
    )

    threads_api_base_url: AnyHttpUrl = AnyHttpUrl("https://graph.threads.net/v1.0/")
