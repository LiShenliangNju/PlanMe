import os
from pathlib import Path
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    BASE_DIR: Path = Path(__file__).resolve().parent.parent

    CALDAV_CONFIG_PATH: str = str(BASE_DIR / ".config" / "caldav" / "calendar.conf")

    HMWK_SCRN_CONFIG_PATH: str = str(BASE_DIR / ".config" / "hmwk_scnr" / "config.yaml")
    HMWK_DETECTOR_TEMPERATURE: float = 0.0

    OLLAMA_HOST: str = "http://localhost:11434"
    OLLAMA_MODEL: str = "qwen2.5:7b-instruct-q4_K_M"

    TIMEZONE: str = "Asia/Shanghai"

    class Config:
        env_file = ".env"

settings = Settings()

os.environ["CALDAV_CONFIG_FILE"] = settings.CALDAV_CONFIG_PATH
os.environ["HMWK_SCRN_CONFIG_FILE"] = settings.HMWK_SCRN_CONFIG_PATH