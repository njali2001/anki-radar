# -*- coding: utf-8 -*-
"""代码在哪、数据在哪——**只有这一处说得上算**。

    CODE_DIR   只读的东西：config.example.json、sample_posts.json
    DATA_DIR   会被写的东西：config.json、config.bak.json、radar.sqlite3、report.html

【为什么要分成两个】：这两样东西在三种运行方式下的位置**不一样**：

  · 直接跑 `python radar.py`（现在的做法）：两者都是代码目录。
  · 绿色版（官方 embeddable Python 放在 runtime\\ 下）：一样，代码和数据同目录，
    整个文件夹拷走就能跑。
  · 打包成单文件 exe（PyInstaller 那一类）：`__file__` 指向的是一个**启动时解压、
    退出时删掉的临时目录**。配置和数据库若跟着 `__file__` 走，就会被写进那个临时
    目录——"已处理/已忽略"的标记每次启动都归零，而且不会有任何报错。
  · 服务器/容器：数据在挂载卷上，代码在镜像里，两者必然分开。

【为什么要收到一个模块里】：原来 radar.py 和 configpage.py 各自算了一遍
`Path(__file__).parent`。只要有一天这两个算出来的不是同一个目录，配置页会写进
A、主程序会读 B，页面上照样显示"保存好了"，而设置根本没生效——这种错没有任何
症状，只有"我明明改了啊"。

【优先级】：环境变量 > 冻结后的 exe 目录 > 代码目录。环境变量放在最前面，是为了
让服务器上能把数据指到挂载卷；不设它的时候，行为和以前逐字节一致。
"""

import os
import pathlib
import sys

CODE_DIR = pathlib.Path(__file__).resolve().parent


def _data_dir():
    env = os.environ.get("RADAR_DATA_DIR", "").strip()
    if env:
        return pathlib.Path(env).expanduser().resolve()
    # PyInstaller 那一类打包器会设 sys.frozen；这时 sys.executable 才是 exe 自己
    # 的位置，而 __file__ 是临时解压目录。
    if getattr(sys, "frozen", False):
        return pathlib.Path(sys.executable).resolve().parent
    return CODE_DIR


DATA_DIR = _data_dir()

try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    # 【这里不报错】：真写不进去的时候，让具体那次读写去报——那条消息会说出是哪个
    # 文件、什么原因，比在 import 阶段抛一个只有路径的异常有用得多。
    pass

CONFIG = DATA_DIR / "config.json"
CONFIG_BACKUP = DATA_DIR / "config.bak.json"
REPORT = DATA_DIR / "report.html"

EXAMPLE = CODE_DIR / "config.example.json"
SAMPLE_POSTS = CODE_DIR / "sample_posts.json"


def database(name):
    """config.json 里的 `database` 怎么解释成一个真实路径。

    相对路径按数据目录算，**绝对路径原样用**——后者是为了让服务器上可以把库
    指到别的盘或别的挂载点，而不必连带把配置也搬过去。
    """
    path = pathlib.Path(name).expanduser()
    return path if path.is_absolute() else DATA_DIR / path
