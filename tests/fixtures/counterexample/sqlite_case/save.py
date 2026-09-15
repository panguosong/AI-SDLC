"""受控事务保存样例；测试会显式构造未提交写入。"""

import json
import sqlite3
import sys

COMMIT = True
connection = sqlite3.connect(sys.argv[1])
connection.execute("UPDATE state SET value = ?", (sys.argv[2],))
if COMMIT:
    connection.commit()
connection.close()
print(json.dumps({"ack": "saved"}))
