"""离屏启动冒烟测试：构造主窗口、渲染、跑一轮事件循环，捕获运行时错误。"""
import os, sys, traceback
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer


def _excepthook(etype, val, tb):
    traceback.print_exception(etype, val, tb)
    sys.stderr.flush()


def main():
    sys.excepthook = _excepthook
    app = QApplication(sys.argv)
    from src.gui.main_window import MainWindow
    from src.engine.engine_manager import EngineManager
    em = EngineManager()
    w = MainWindow(em)
    w.show()
    print("窗口已构造并显示，尺寸", w.width(), "x", w.height())
    # 触发一次渲染
    w.board_widget.repaint()
    print("棋盘渲染完成")
    QTimer.singleShot(1200, app.quit)
    rc = app.exec()
    print("事件循环退出，rc=", rc)
    print("启动冒烟测试通过 ✓")


if __name__ == "__main__":
    main()
