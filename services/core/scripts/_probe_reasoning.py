"""临时验证：generate_json（流式 + 非流式）在档位被拒时也能自动改派。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lawbuddy import keyring  # noqa: E402
from lawbuddy.agent import model_gateway as mg  # noqa: E402
from lawbuddy.storage import init_database, init_profile_database  # noqa: E402

ws = Path(r"D:\workagent\lawbuddy")
prof = Path(r"C:\Users\14347\AppData\Roaming\LawBuddy\profile")
init_database(ws / "lawbuddy.db", keyring.load_or_create_dek(ws))
init_profile_database(prof / "profile.db", keyring.load_or_create_dek(prof))

schema = '{"type":"object","properties":{"answer":{"type":"string"}},"required":["answer"]}'
for label, on_delta in (("流式", lambda _t: None), ("非流式", None)):
    try:
        data, usage = mg.generate_json(
            "你是测试助手。", "回复 JSON：answer 字段写「收到」。", schema,
            model="glm-5.3", reasoning_effort="medium", on_delta=on_delta,
        )
        print(f"{label}: OK {data} usage={usage}")
    except Exception as exc:  # noqa: BLE001
        print(f"{label}: FAIL {type(exc).__name__}: {str(exc)[:200]}")
