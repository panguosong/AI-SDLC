"""受控文件保存样例；不是已发现的真实生产缺陷。"""

import json
import pathlib
import sys

pathlib.Path(sys.argv[1]).write_text(
    json.dumps({"value": sys.argv[2]}), encoding="utf-8"
)
print(json.dumps({"ack": "saved"}))
