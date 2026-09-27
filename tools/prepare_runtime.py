# -*- coding: utf-8 -*-
"""让 runtime\\ 里的官方 embeddable Python 能找到雷达自己的模块。

    runtime\\python.exe tools\\prepare_runtime.py runtime

【要改的是那个 `pythonXY._pth`】。官方 embeddable 包里带着这个文件，它的作用是
**把 sys.path 钉死**（同时不加载 site）。默认内容只有标准库的 zip 和运行时目录
自己，所以 `import json` 正常，而 `import ui` 会报 ModuleNotFoundError——一个看
起来像是"下载坏了"的错误，实际上只是少了一行路径。

雷达的 .py 文件在 runtime\\ 的上一层，所以这里把 `..` 写进去。**相对路径是相对
运行时目录的**，不是相对当前工作目录，所以整个文件夹拷到哪里都成立——这正是
"绿色版"要的性质。

这个脚本可以反复跑，不会重复添加。
"""

import pathlib
import sys

APP_DIR_ENTRY = ".."


def main():
    if len(sys.argv) < 2:
        print("用法：python tools/prepare_runtime.py <runtime 目录>")
        return 2

    runtime = pathlib.Path(sys.argv[1]).resolve()
    candidates = sorted(runtime.glob("python*._pth"))
    if not candidates:
        print(f"在 {runtime} 里找不到 python*._pth——这个目录不像官方 embeddable 包。")
        return 1

    pth = candidates[0]
    lines = pth.read_text(encoding="utf-8").splitlines()
    if APP_DIR_ENTRY in [line.strip() for line in lines]:
        print(f"{pth.name} 里已经有 {APP_DIR_ENTRY} 了，不动。")
        return 0

    # 【加在最前面】：万一将来运行时目录里出现同名文件，也应该是雷达自己的那份
    # 优先。顺序在这里几乎不会有影响，但"自己的代码优先"是更安全的默认。
    lines.insert(0, APP_DIR_ENTRY)
    pth.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"已把 {APP_DIR_ENTRY} 写进 {pth.name}：")
    for line in lines:
        print("   ", line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
