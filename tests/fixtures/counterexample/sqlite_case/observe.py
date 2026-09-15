"""新数据库连接观察已提交状态，与保存进程分离。"""

import json
import pathlib
import sqlite3
import sys

# 只读连接禁止意外创建数据库，资源原件另行核对日志副文件是否改变。
connection = sqlite3.connect(
    pathlib.Path(sys.argv[1]).resolve().as_uri() + "?mode=ro", uri=True
)
value = connection.execute("SELECT value FROM state").fetchone()[0]
connection.close()
print(
    json.dumps(
        {"schema_version": 1, "typed_actual": {"type": "string", "value": value}}
    )
)
