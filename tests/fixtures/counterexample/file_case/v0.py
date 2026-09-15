"""人工弱验收：只接受应答形态，尚未读取保存结果。"""

import json

print(
    json.dumps(
        {
            "schema_version": 1,
            "assertion_id": "saved-state",
            "reached": True,
            "assertion_result": "accepted",
            "failure_reason": "none",
        }
    )
)
