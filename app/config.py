import os
from pathlib import Path

from dotenv import load_dotenv


ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", str(ROOT_DIR / "data" / "study_agent.db")))
MATH_KNOWLEDGE_DIR = ROOT_DIR / "knowledge" / "11408-math"
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "").rstrip("/")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "")
KT_BATCH_SIZE = 4
