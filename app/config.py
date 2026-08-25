import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

APP_NAME = "cnnc-ops-agent"
APP_VERSION = "0.1.0-mvp"

KNOWLEDGE_MD_PATH = Path(os.getenv("KNOWLEDGE_MD_PATH", str(BASE_DIR / "data" / "docs" / "cnnc_ops_knowledge_pure.md")))

TEMPLATES_DIR = Path(os.getenv("TEMPLATES_DIR", str(BASE_DIR / "app" / "templates")))

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

READONLY_MODE = True
