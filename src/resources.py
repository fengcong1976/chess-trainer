"""统一资源/数据路径解析（开发模式与 PyInstaller 单文件打包双兼容）。

打包（--onefile）时的目录语义：
    bundle_root()  = sys._MEIPASS            只读解包资源（引擎 exe、音效 wav）
    app_root()     = exe 所在目录            只读随包资源（开局书/）
开发模式时两者都等于项目根目录。

用户可写数据（进度/计划/战绩/棋谱）固定存放在
    %LOCALAPPDATA%\中国象棋培训大师\data
安装目录只读，避免写入 Program Files 类位置失败。
"""
import os
import shutil
import sys


def is_frozen() -> bool:
    """是否运行在 PyInstaller 打包环境中。"""
    return getattr(sys, "frozen", False)


def app_root() -> str:
    """随包资源根：打包 = exe 所在目录；开发 = 项目根。"""
    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    # src/resources.py → 上溯一级为项目根
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def bundle_root() -> str:
    """只读资源根：打包 = sys._MEIPASS 解包目录；开发 = 项目根。"""
    if is_frozen():
        return sys._MEIPASS
    return app_root()


def _migrate_dir(src: str, dst: str) -> None:
    """把旧数据目录内容逐项搬入新目录：优先 move，失败回退 copy。

    旧目录保留不删；单项失败不中断整体迁移。
    """
    for name in os.listdir(src):
        s = os.path.join(src, name)
        t = os.path.join(dst, name)
        try:
            shutil.move(s, t)
            continue
        except OSError:
            pass
        try:
            if os.path.isdir(s):
                shutil.copytree(s, t)
            else:
                shutil.copy2(s, t)
        except OSError:
            pass


def data_dir() -> str:
    """运行时数据目录（进度/计划/战绩/棋谱），固定在
    %LOCALAPPDATA%\\中国象棋培训大师\\data。自动创建。

    首次解析时若旧版数据（exe 旁 data/）存在且新目录为空，一次性迁入。
    """
    base = os.path.join(
        os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
        "中国象棋培训大师")
    d = os.path.join(base, "data")
    old = os.path.join(app_root(), "data")
    try:
        os.makedirs(d, exist_ok=True)
        if old != d and os.path.isdir(old) and not os.listdir(d):
            _migrate_dir(old, d)
    except OSError:
        pass
    return d


def books_dir() -> str:
    """开局书目录（exe 旁「开局书/」，用户可自由增删 .bin/.obk）。自动创建。"""
    d = os.path.join(app_root(), "开局书")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return d


def resource(rel: str) -> str:
    """只读打包资源路径（相对 bundle_root）。"""
    return os.path.join(bundle_root(), rel)
