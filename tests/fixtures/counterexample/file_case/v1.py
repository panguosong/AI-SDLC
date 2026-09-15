"""强化断言直接读回结果；合法值通过，错误持久值拒绝。"""

import json
import pathlib
import sys

value = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))["value"]
accepted = value == sys.argv[2]
print(
    json.dumps(
        {
            "schema_version": 1,
            "assertion_id": "saved-state",
            "reached": True,
            "assertion_result": "accepted" if accepted else "rejected",
            "failure_reason": "none" if accepted else "target_assertion",
        }
    )
)
raise SystemExit(0 if accepted else 1)
