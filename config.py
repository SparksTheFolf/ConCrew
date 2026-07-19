import os

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
from dotenv import load_dotenv
load_dotenv()

class Config:
    APP_NAME = os.environ.get("APP_NAME", "ConCrew")
    APP_VERSION = os.environ.get("APP_VERSION", "dev")
    SECRET_KEY = os.environ.get("SECRET_KEY")
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", f"sqlite:///{os.path.join(BASE_DIR, 'instance', 'volunteer.db')}"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    WTF_CSRF_ENABLED = True
    MAIL_SERVER = os.environ.get("MAIL_SERVER")
    MAIL_PORT = int(os.environ.get("MAIL_PORT", 587))
    MAIL_USE_TLS = os.environ.get("MAIL_USE_TLS", "true").lower() in ("1", "true", "yes", "on")
    MAIL_USE_SSL = os.environ.get("MAIL_USE_SSL", "false").lower() in ("1", "true", "yes", "on")
    MAIL_USERNAME = os.environ.get("MAIL_USERNAME")
    MAIL_PASSWORD = os.environ.get("MAIL_PASSWORD")
    MAIL_DEFAULT_SENDER = os.environ.get("MAIL_DEFAULT_SENDER")
    MAIL_SUPPRESS_SEND = os.environ.get("MAIL_SUPPRESS_SEND", "false").lower() in ("1", "true", "yes", "on")
    LOGIN_CODE_TTL_SECONDS = int(os.environ.get("LOGIN_CODE_TTL_SECONDS", 600))
    LOGIN_CODE_RESEND_COOLDOWN_SECONDS = int(os.environ.get("LOGIN_CODE_RESEND_COOLDOWN_SECONDS", 60))
    LOGIN_CODE_MAX_ATTEMPTS = int(os.environ.get("LOGIN_CODE_MAX_ATTEMPTS", 5))
