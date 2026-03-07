import os
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Actovator"
    version: str = "0.1.0"
    environment: str = "development"
    google_api_key: str = ""
    e2b_api_key: str = ""
    nvidia_api_key: str = ""
    github_token: str = ""

    model_config = SettingsConfigDict(
        env_file=".env.local", env_file_encoding="utf-8", case_sensitive=False
    )


settings = Settings()

if not os.environ.get("GOOGLE_API_KEY"):
    os.environ["GOOGLE_API_KEY"] = settings.google_api_key
