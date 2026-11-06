"""临时排查脚本：打印设备级配置里的服务商与模型目录（不含密钥明文）。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lawbuddy import keyring  # noqa: E402
from lawbuddy.storage import init_profile_database  # noqa: E402

root = Path(sys.argv[1] if len(sys.argv) > 1 else r"C:\Users\14347\AppData\Roaming\LawBuddy\profile")
dek = keyring.load_or_create_dek(root)
db = init_profile_database(root / "profile.db", dek)

for row in db.query("SELECT key, value_json FROM settings"):
    key = row["key"]
    if key in ("model.provider", "model.providerOverrides", "model.customProviders"):
        print(f"== {key} ==")
        data = json.loads(row["value_json"])
        print(json.dumps(data, ensure_ascii=False, indent=1))
        print()
    elif key.endswith("ApiKey") or key == "model.apiKey":
        print(f"{key}: <加密值，已省略>")
