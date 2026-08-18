"""Configuration loaded from .env + iris_settings table."""
import os
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


def _read_version(default: str = "0.0.0") -> str:
    """Versiunea din fisierul VERSION (sursa unica de adevar, actualizat la fiecare livrare).
    Inainte era hardcodata aici si rămânea in urma: /api/v1/health raporta 0.46.10 cand
    aplicatia era la 0.48.0."""
    try:
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "VERSION")
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read().strip() or default
    except Exception:
        return default


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # App
    app_name: str = "NordLogistics Cargo360"
    app_version: str = _read_version("0.48.0")
    app_port: int = 8500
    app_env: str = "production"
    log_level: str = "INFO"

    # DB
    db_host: str = "localhost"
    db_port: int = 5433
    db_name: str = "mailguard"
    db_user: str = "mailguard"
    db_password: str = ""

    # Redis
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 2  # Option C: separate from parser-email-op (db 0)

    # Auth
    jwt_secret: str = ""
    jwt_algorithm: str = "HS256"
    jwt_expires_minutes: int = 60 * 24  # 1 day
    # Skip IRIS SSO pe laptop — doar cu LOCAL_AUTH_BYPASS=true in .env local
    local_auth_bypass: bool = False
    local_auth_email: str = ""

    # Microsoft Graph (loaded later from settings table or .env)
    ms_tenant_id: str = ""
    ms_client_id: str = ""
    ms_client_secret: str = ""
    ms_user_email: str = ""

    # IRIS Gateway
    iris_api_url: str = "https://iris.cargotrack.ro"
    iris_api_key: str = ""
    # Cheia dedicata CARGO360 pentru /clients/contact-list. Declarata aici ca sa fie citita si din
    # `.env` (pydantic), nu doar din environment: sub systemd exista `EnvironmentFile`, dar orice
    # rulare in afara lui (dev local, script manual, cron fara env) vedea `os.getenv` gol si
    # sync-ul de clienti/vehicule/contracte se opreste cu "IRIS_MAILGUARD_API_KEY missing".
    iris_mailguard_api_key: str = ""

    # CTS ADMIN (API key generated random; CTS ADMIN trebuie să-l folosească)
    cts_admin_api_key: str = ""

    # CTS feed (faza 1: backoffice CTS preia DOAR emailurile clean prin /api/v1/cts/feed)
    cts_feed_api_key: str = ""

    # CTS clasificare (faza 2): include si Categorie + Departament in feed-ul get_emails.
    # Default False => PRODUCTIA ramane neschimbata (campurile sunt ABSENTE) pana la PR-ul CTS.
    # Pe STAGING punem CTS_SEND_CLASSIFICATION=1 in .env ca sa testam fara a afecta CTS live.
    cts_send_classification: bool = False

    @property
    def db_dsn(self) -> str:
        return f"postgresql+psycopg2://{self.db_user}:{self.db_password}@{self.db_host}:{self.db_port}/{self.db_name}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
