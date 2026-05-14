from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

SQLITE_PATH = DATA_DIR / "bonds.sqlite"
LAST_PRICES_BATCH = 200

# Ключевая ставка ЦБ РФ, % годовых. Обновлять вручную после заседаний ЦБ.
# Используется для разложения купона флоатеров в формулу "CBR_RATE + X".
CBR_KEY_RATE = 14.5


def excel_path(ts: datetime) -> Path:
    return DATA_DIR / f"bonds_{ts:%Y-%m-%d_%H%M}.xlsx"
