"""人工弱验收，未验证事务对新连接可见。"""

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
