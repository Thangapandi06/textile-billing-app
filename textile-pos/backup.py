"""Safe copy of shop.db. Run nightly (Windows Task Scheduler / cron). Point BACKUP_DIR at a Drive/OneDrive/USB folder."""
import sqlite3, sys
from datetime import date
from pathlib import Path
BACKUP_DIR = Path(sys.argv[1] if len(sys.argv) > 1 else "backups"); BACKUP_DIR.mkdir(exist_ok=True)
src = sqlite3.connect("shop.db"); dst = sqlite3.connect(BACKUP_DIR / f"shop-{date.today()}.db")
src.backup(dst); dst.close(); src.close(); print("Backup saved")
