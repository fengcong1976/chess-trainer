"""中国象棋培训大师 - 程序入口"""
import sys
import os
import traceback

# 确保能导入 src 包（开发模式）；打包后 PyInstaller 自动处理导入
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# --windowed 打包下 stdout/stderr 为 None，任何 print 会崩 → 指向空设备
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")


def _crash_log(msg: str):
    """崩溃日志写到 data/crash.log（尽力而为）。"""
    try:
        from src.resources import data_dir
        log_path = os.path.join(data_dir(), "crash.log")
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass


def main():
    from PySide6.QtWidgets import QApplication
    from src.gui.main_window import MainWindow
    from src.engine.engine_manager import EngineManager

    app = QApplication(sys.argv)
    app.setApplicationName("中国象棋培训大师")

    # 初始化引擎管理器（统一管理 Pikafish 与内置回退引擎）
    engine_manager = EngineManager()

    window = MainWindow(engine_manager)
    # 启动即全屏（最大化铺满屏幕，保留任务栏）
    window.showMaximized()

    # 应用退出时关闭引擎进程
    app.aboutToQuit.connect(engine_manager.shutdown)
    sys.exit(app.exec())


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        _crash_log(traceback.format_exc())
        raise
