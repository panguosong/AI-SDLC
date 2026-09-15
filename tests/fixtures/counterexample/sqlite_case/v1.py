"""使用独立连接核对保存值。"""

import json
import pathlib
import sqlite3
import sys

connection = sqlite3.connect(
    pathlib.Path(sys.argv[1]).resolve().as_uri() + "?mode=ro", uri=True
)
value = connection.execute("SELECT value FROM state").fetchone()[0]
connection.close()
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
