"""端到端功能测试（离屏）：模拟对弈走子+引擎应招、残局训练通关/走错。"""
import os, sys, traceback
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtWidgets import QApplication, QMessageBox
from PySide6.QtCore import QTimer, QElapsedTimer

# 拦截模态弹窗，改为记录
_dialogs = []
_orig_info = QMessageBox.information
_orig_about = QMessageBox.about


def _info(parent, title, text):
    _dialogs.append(("info", title, text))
    return QMessageBox.StandardButton.Ok


def _about(parent, title, text):
    _dialogs.append(("about", title, text))
    return


QMessageBox.information = staticmethod(_info)
QMessageBox.about = staticmethod(_about)


def _excepthook(etype, val, tb):
    traceback.print_exception(etype, val, tb)


def wait(ms, app):
    """处理事件 ms 毫秒。"""
    t = QElapsedTimer(); t.start()
    while t.elapsed() < ms:
        app.processEvents()


def main():
    sys.excepthook = _excepthook
    app = QApplication(sys.argv)
    from src.gui.main_window import MainWindow
    from src.engine.engine_manager import EngineManager
    from src.board import rules
    from src.board.position import Pos
    from src.training.trainer import MoveVerdict

    em = EngineManager()
    # 测试隔离：训练计划/战绩走临时文件，绝不污染用户真实打卡数据
    import tempfile, os as _os
    _tmpdir = tempfile.mkdtemp(prefix="chess_ft_")
    w = MainWindow(em,
                   plan_path=_os.path.join(_tmpdir, "plan.json"),
                   stats_path=_os.path.join(_tmpdir, "stats.json"))
    w.show()
    wait(300, app)  # 等待延迟的引擎初始化与 start_play

    # ---------- 对弈模式：用户走一步 + 引擎应招 ----------
    print("\n--- 对弈模式测试 ---")
    assert w.mode == w.PLAY_MODE
    start_moves = rules.generate_legal_moves(w.board)
    assert len(start_moves) == 44, f"开局走法应44，实际{len(start_moves)}"
    frm, to = start_moves[0]
    from src.board.position import move_to_ucci
    print("用户走:", move_to_ucci(frm, to))
    w.board_widget.moveAttempted.emit(frm.row, frm.col, to.row, to.col)
    wait(200, app)
    assert len(w.history) == 1, "用户走法应已记录"
    # 等待引擎应招（内置引擎深度2-3，应很快）
    waited = 0
    while len(w.history) < 2 and waited < 5000:
        wait(100, app); waited += 100
    print("引擎应招后历史步数:", len(w.history))
    assert len(w.history) >= 2, "引擎未应招"
    print("引擎走:", w.history[-1][3])
    # 应轮到红方（用户）
    from src.board.pieces import Color
    assert w.board.side is Color.RED, "引擎走后应轮到红方"
    print("  -> 对弈走子+引擎应招 ✓")

    # ---------- 残局训练：通关 ----------
    print("\n--- 残局训练通关测试 (双车错·横车照将) ---")
    w._load_puzzle("sc001")
    assert w.mode == w.TRAIN_MODE
    assert w.session is not None
    # 解法 i7d7 = (2,8)->(2,3)
    w.board_widget.moveAttempted.emit(2, 8, 2, 3)
    wait(200, app)
    print("已通关:", w.session.finished)
    assert w.session.finished, "一步杀应已通关"
    assert any(d[0] == "info" and "通关" in d[1] for d in _dialogs), "应弹出通关对话框"
    print("  -> 一步杀通关 ✓")

    # ---------- 残局训练：走错判定 ----------
    print("\n--- 残局训练走错测试 (马后炮，移动马破杀形) ---")
    _dialogs.clear()
    w._load_puzzle("mh001")
    # 马后炮题只有 i5e5 一步杀。把马移开应破坏杀形 -> WRONG
    # 候选：马的各类走法
    from src.board.position import Pos as _P
    horse = _P(2, 4)
    horse_moves = rules.pseudo_targets(w.board, horse)
    wrong_triggered = False
    for t in horse_moves:
        # 先重置再判定：上轮若为 correct 走法，_handle_train_move 已推进 w.board，
        # 马会离开 (2,4)，导致后续 is_legal 在已推进的棋盘上误判为非法。
        w._load_puzzle("mh001")
        if not rules.is_legal(w.board, horse, t):
            continue
        _dialogs.clear()
        w.board_widget.moveAttempted.emit(horse.row, horse.col, t.row, t.col)
        wait(300, app)
        if any("再想想" in d[1] for d in _dialogs):
            print(f"  马走 {move_to_ucci(horse, t)} 触发走错提示 ✓")
            wrong_triggered = True
            break
    assert wrong_triggered, "应能找到一步破坏杀形的走错走法"
    print("  -> 走错判定 ✓")

    # ---------- 开局训练：走偏提示 ----------
    print("\n--- 开局训练走偏测试 (第1课首着跳马而非中炮) ---")
    w._load_lesson("op1")
    assert w.mode == w.OPENING_MODE
    assert w.opening_session is not None
    from src.board.position import parse_ucci_move as _pu
    frm, to = _pu("h0g2")  # 马二进三，偏着
    _dialogs.clear()
    w.board_widget.moveAttempted.emit(frm.row, frm.col, to.row, to.col)
    wait(300, app)
    assert any("偏离正着" in d[1] for d in _dialogs), "走偏应弹出纠正对话框"
    assert w.opening_session.ply == 0, "走偏后主线不应推进"
    print("  -> 走偏纠正 ✓")

    # ---------- 开局训练：偏着惩罚演示 ----------
    print("\n--- 开局训练偏着演示测试 (第1课 兵七进一节点) ---")
    w._load_lesson("op1")
    for ucci in ["h2e2", "h0g2", "i0h0", "c3c4"]:  # c3c4 配有偏着演示
        frm, to = _pu(ucci)
        w.board_widget.moveAttempted.emit(frm.row, frm.col, to.row, to.col)
        wait(300, app)
    assert not w.btn_o_pitfall.isHidden(), "配演示的正着应亮出演示按钮"
    w._show_pitfall()
    assert w._demo is not None, "演示应启动"
    wait(7500, app)  # 快进动画（0.6s 启动 + 4半步 × 1.4s + 缓冲）
    assert "结论" in w.lbl_coach.text(), "演示结束应显示结论"
    w._exit_pitfall()
    assert w._demo is None, "退出后应清理演示状态"
    assert w.board is w.opening_session.board, "应恢复主线局面"
    assert w.board_widget.interactive, "应恢复交互"
    print("  -> 偏着演示 ✓")

    # ---------- 开局训练：正着走完第1课 ----------
    print("\n--- 开局训练通关测试 (第1课·当头炮对屏风马全线) ---")
    w._load_lesson("op1")
    for ucci in ["h2e2", "h0g2", "i0h0", "c3c4", "b0c2", "b2a2", "a0b0"]:  # 用户(红)7步（先分炮后亮左车，双向陷阱规避）
        frm, to = _pu(ucci)
        w.board_widget.moveAttempted.emit(frm.row, frm.col, to.row, to.col)
        wait(300, app)
    s = w.opening_session
    assert s.finished, "第1课应已完成"
    assert len(w.history) == 13, f"主线13半步，实际{len(w.history)}"
    assert any("本课完成" in d[1] for d in _dialogs), "应弹出完成对话框"
    # 教练应招均记录（中文记法）
    assert w.history[1][4] == "马8进7", f"教练应招中文记法错误: {w.history[1][4]}"
    print("  教练应招:", w.history[1][4], w.history[3][4], w.history[5][4],
          w.history[7][4], w.history[9][4], w.history[11][4])
    print("  -> 开局课通关 ✓")

    # ---------- 中局训练：走错判定 ----------
    print("\n--- 中局训练走错测试 (mg001捉双，车去左翼闲逛) ---")
    w._load_tactic("mg001")
    assert w.mode == w.MIDGAME_MODE
    assert w.midgame_session is not None
    _dialogs.clear()
    frm, to = _pu("e5a5")  # 错着：不捉双，调车左翼
    w.board_widget.moveAttempted.emit(frm.row, frm.col, to.row, to.col)
    wait(300, app)
    assert any("再想想" in d[1] for d in _dialogs), "错着应弹出提示对话框"
    assert w.midgame_session.ply == 0, "错着后局面不应推进"
    print("  -> 中局走错判定 ✓")

    # ---------- 中局训练：偏着惩罚演示 ----------
    print("\n--- 中局训练偏着演示测试 (mg001 e5e7捉双节点) ---")
    frm, to = _pu("e5e7")  # 正解：车捉双，配有偏着演示
    w.board_widget.moveAttempted.emit(frm.row, frm.col, to.row, to.col)
    wait(300, app)
    assert not w.btn_m_pitfall.isHidden(), "配演示的正着应亮出演示按钮"
    assert "对手走" in w.lbl_mid_coach.text(), "教练应讲解对手的按谱应手"
    w._show_pitfall()
    assert w._demo is not None, "偏着演示应启动"
    wait(8000, app)  # 快进动画（0.6s 启动 + 4半步 × 1.4s + 缓冲）
    assert "结论" in w.lbl_mid_coach.text(), "演示结束应显示结论"
    w._exit_pitfall()
    assert w._demo is None, "退出后应清理演示状态"
    assert w.board is w.midgame_session.board, "应恢复主线局面"
    assert w.board_widget.interactive, "应恢复交互"
    print("  -> 中局偏着演示 ✓")

    # ---------- 中局训练：正着通关 ----------
    print("\n--- 中局训练通关测试 (mg001·捉双收官吃炮) ---")
    _dialogs.clear()
    frm, to = _pu("e7b7")  # 车吃炮，净得一子
    w.board_widget.moveAttempted.emit(frm.row, frm.col, to.row, to.col)
    wait(300, app)
    assert w.midgame_session.finished, "主线走完应通关"
    assert any("战术达成" in d[1] for d in _dialogs), "应弹出战术达成对话框"
    assert w.progress.is_solved("mg001"), "通关应记录进度"
    print("  -> 中局题通关 ✓")

    print("\n全部端到端功能测试通过 ✓✓✓")
    app.quit()


if __name__ == "__main__":
    main()
