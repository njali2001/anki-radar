# -*- coding: utf-8 -*-
"""绿色版装好之后，验证它真的能干活。

    runtime\\python.exe tools\\verify_runtime.py

退出码 0 = 都过了；1 = 有一项没过（会说清楚是哪一项）。

【为什么要有这一步】：一个"能启动"的 Python 不等于一个"能用"的 Python。打包和
绿色化最常见的翻车点是 **HTTPS**：`_ssl.pyd` 在，证书链却拿不到，于是所有源都
报 URLError，而你会以为是网络问题。所以这里不查"import ssl 成不成功"，而是真去
拿一次 HTTPS——拿的还是雷达自己要用的那个域名。

第二个常见翻车点是 `pythonXY._pth` 没配好：`import json` 正常，`import ui` 失败。
所以这里把雷达的每个模块都 import 一遍。
"""

import sys
import urllib.error
import urllib.request

PROBE_URL = "https://forums.ankiweb.net/site.json"
MODULES = ["paths", "store", "sources", "ai", "usage", "configpage", "ui", "facts", "radar"]


def check(title, fn):
    print(f"  {title:<28}", end="")
    try:
        detail = fn()
    except Exception as exc:  # noqa: BLE001 —— 这里要的就是"任何失败都报出来"
        print(f"失败：{type(exc).__name__}: {exc}")
        return False
    print(f"通过  {detail or ''}")
    return True


def probe_https():
    request = urllib.request.Request(
        PROBE_URL, headers={"User-Agent": "anki-radar runtime check"}
    )
    with urllib.request.urlopen(request, timeout=25) as response:
        body = response.read(200)
    return f"{PROBE_URL} → HTTP {response.status}，读到 {len(body)} 字节"


def probe_sqlite():
    import sqlite3

    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE t (x TEXT)")
    db.execute("INSERT INTO t VALUES ('ok')")
    value = db.execute("SELECT x FROM t").fetchone()[0]
    db.close()
    return f"sqlite {sqlite3.sqlite_version}，读写 {value}"


def probe_modules():
    import importlib

    for name in MODULES:
        importlib.import_module(name)
    return f"{len(MODULES)} 个模块都能导入"


def probe_paths():
    import paths

    return f"数据目录 {paths.DATA_DIR}"


def main():
    print(f"Python {sys.version.split()[0]}（{sys.executable}）")
    print()
    results = [
        check("标准库里的 sqlite3", probe_sqlite),
        check("雷达自己的模块", probe_modules),
        check("数据目录", probe_paths),
        check("HTTPS（证书链）", probe_https),
    ]
    print()
    if all(results):
        print("都过了。双击 run.bat 就能用。")
        return 0
    print("有没过的项，上面写着是哪一项。绿色版先别拿去用。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
