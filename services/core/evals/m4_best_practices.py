"""60 parameterized templates: configuration, missing fields and source gates."""

from __future__ import annotations

import tempfile
from pathlib import Path


def main() -> int:
    from lawbuddy import best_practices, docgen
    from lawbuddy.cases import create_case
    from lawbuddy.storage.db import reset_database
    from lawbuddy.workspace import ensure_workspace

    configs = best_practices.catalog()
    assert len(configs) == len({c["stableId"] for c in configs}) == 60
    with tempfile.TemporaryDirectory(prefix="lawbuddy_templates_") as temp:
        try:
            ensure_workspace(str(Path(temp) / "ws"))
            case = create_case("", "other", "", default_mode="document_generation")
            installed = best_practices.install(case["id"])
            assert len(installed) == 60
            assert {c["templateId"] for c in installed} == {
                c["templateId"] for c in best_practices.install(case["id"])
            }
            for config in installed:
                template = docgen.get_template(config["templateId"])
                assert template["status"] == "active"
                assert set(f["name"] for f in config["fields"]) == set(f["name"] for f in template["fields"])
                assert config["sourceMap"]["sha256"] == best_practices.SOURCE_HASH
                assert config["outline"] and len(config["writingChecks"]) >= 4
                draft = docgen.create_draft(case["id"], template["id"], config["docType"], "合成测试目的")
                docgen.update_outline(draft["id"], [
                    {"heading": title, "points": "合成测试", "sourceDoc": ""} for title in config["outline"]
                ])
                docgen.confirm_outline(draft["id"])
                missing = docgen.render_body(draft["id"])
                assert missing["blockingCount"] == len(template["fields"])
                try:
                    docgen.confirm_draft(draft["id"])
                except docgen.DocGenError:
                    pass
                else:
                    raise AssertionError("Missing inputs cannot finalize")
                values = [{"name": f["name"], "value": "用户确认的合成信息", "sourceType": "user_confirmed"}
                          for f in template["fields"]]
                docgen.fill_slots(draft["id"], values)
                full = docgen.render_body(draft["id"])
                assert full["blockingCount"] == 0 and "{{" not in full["bodyMd"]
                docgen.fill_slots(draft["id"], [{**values[0], "sourceType": "pending_verify"}])
                try:
                    docgen.confirm_draft(draft["id"])
                except docgen.DocGenError:
                    pass
                else:
                    raise AssertionError("Pending facts cannot finalize")
                print(f"[PASS] {config['stableId']} fields / missing / pending source")
            print("[PASS] 60/60 deterministic configurations; legal/model quality still requires review")
        finally:
            reset_database()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
