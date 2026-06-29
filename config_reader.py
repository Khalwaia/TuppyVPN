from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import SecretStr


class Settings(BaseSettings):
    bot_token: SecretStr
    admin_id: int

    # YooKassa
    yookassa_shop_id: str
    yookassa_secret_key: SecretStr

    # CryptoBot
    cryptobot_token: SecretStr

    # Remnawave
    remnawave_url: str = "https://panelhub.tuppyhub.store"
    subscription_url: str = "https://subhub.tuppyhub.store"
    remnawave_api_token: SecretStr

    model_config: SettingsConfigDict = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8"
    )


config = Settings()