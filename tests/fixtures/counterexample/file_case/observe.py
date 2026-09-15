"""从另一个进程读取磁盘值，不消费保存命令的应答。"""

import json
import pathlib
import sys

value = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))["value"]
print(
    json.dumps(
        {"schema_version": 1, "typed_actual": {"type": "string", "value": value}}
    )
)
