"""主窗口：人机对弈、残局训练、开局训练与中局战术训练多模式整合。

布局：左侧棋盘控件 + 右侧操作面板。
引擎计算在独立线程中执行，避免阻塞界面。
"""
from typing import Optional
from pathlib import Path
import threading
import os
import time
import traceback

from PySide6.QtCore import Qt, QThread, QObject, Signal, QTimer, QPointF
from PySide6.QtGui import (QAction, QFont, QIcon, QPainter, QPen, QColor,
                         QPainterPath, QLinearGradient)
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QLabel, QPushButton,
    QComboBox, QListWidget, QGroupBox, QFrame, QMessageBox, QInputDialog,
    QSizePolicy, QSplitter, QScrollArea, QGridLayout, QFileDialog, QDialog,
    QToolTip, QTextBrowser, QDialogButtonBox,
)

from ..board.board import Board
from ..board.pieces import Color, Piece, PieceType
from ..board.position import Pos, move_to_ucci, parse_ucci_move
from ..board import rules
from ..board.notation import move_to_chinese
from ..resources import bundle_root
from .about_dialog import show_about
from ..engine.engine_manager import EngineManager
from ..training.endgame_puzzles import all_puzzles, Puzzle
from ..training.trainer import TrainingSession, ProgressStore, MoveVerdict, default_progress_path
from ..training.training_plan import TrainingPlanStore, DAILY_TASKS, PlayStatsStore
from ..training import game_records
from ..training.midgame_puzzles import all_tactics
from ..training.midgame_trainer import MidgameSession, TacticVerdict
from .sound_manager import sound
from ..training.tactics_puzzles import all_tactics_puzzles, tactics_puzzle_by_id
from ..training.mindset import all_mindsets, mindset_categories, mindsets_by_category
from ..training.opening_book import all_lessons
from ..training.opening_trainer import OpeningSession, OpeningVerdict
from .board_widget import BoardWidget


# ----------------- 引擎工作线程 -----------------

class _EngineWorker(QObject):
    """在工作线程中调用引擎求最佳走法。"""
    finished = Signal(object)   # (frm_pos, to_pos) 或 None

    def __init__(self, engine: EngineManager, board: Board):
        super().__init__()
        self._engine = engine
        self._board = board

    def run(self):
        try:
            move = self._engine.best_move(self._board)
        except Exception:
            move = None
        self.finished.emit(move)


class _EngineAnalyzeWorker(QObject):
    """在工作线程中分析当前局面，返回 {move, score_cp, pv} 用于讲解。"""
    finished = Signal(dict)

    def __init__(self, engine: EngineManager, board: Board, movetime_ms: int = 1500):
        super().__init__()
        self._engine = engine
        self._board = board
        self._movetime_ms = movetime_ms

    def run(self):
        try:
            info = self._engine.analyze(self._board, movetime_ms=self._movetime_ms)
        except Exception:
            info = {}
        self.finished.emit(info or {})


class _MateLineWorker(QObject):
    """在工作线程求完整杀法线，通过信号安全回传结果（跨线程不丢）。"""
    finished = Signal(object, bool, str)  # (steps, verified, err)

    def __init__(self, engine: EngineManager, session):
        super().__init__()
        self._engine = engine
        self._session = session

    def run(self):
        err = ""
        try:
            steps, verified = self._session.full_mate_line(self._engine)
        except Exception as e:  # noqa: BLE001
            import traceback
            steps, verified = [], False
            err = f"{e!r}\n{traceback.format_exc(limit=6)}"
        self.finished.emit(steps, verified, err)


class _ReviewEvalWorker(QObject):
    """复盘全程评估：逐半步 analyze（红方视角 cp），支持软中断。
    step_eval 同时返回该局面的 bestmove（当前方视角）用于败着对比。"""
    step_eval = Signal(int, float, str)   # (半步序号 i, 走完第 i 步后的局面分, 走子前的 bestmove UCCI)
    progress = Signal(int, int)           # (已完成, 总数)
    finished = Signal()

    def __init__(self, engine, moves):
        super().__init__()
        self._engine = engine
        self._moves = moves
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        try:
            b = Board.start_position()
            n = len(self._moves)
            for i, u in enumerate(self._moves):
                if self._stop:
                    break
                # 走子前 analyze → 得到 bestmove（当前方视角）
                info = self._engine.analyze(b, movetime_ms=250)
                bestmove_ucci = info.get("bestmove", "") if info else ""
                # 走完当前步，再 analyze 得到走完后的局势分
                frm, to = parse_ucci_move(u)
                b.make_move(frm, to)
                after_info = self._engine.analyze(b, movetime_ms=120)
                after_cp = after_info.get("score_cp") if after_info else None
                cp_to_emit = float(after_cp) if after_cp is not None else 0.0
                self.step_eval.emit(i, cp_to_emit, bestmove_ucci or "")
                self.progress.emit(i + 1, n)
        except Exception:
            pass
        self.finished.emit()


class ScoreCurveWidget(QWidget):
    """复盘评估曲线：逐半步局势分折线（红方视角），点击跳步，败着标红点。

    - 红优区域淡红填充、黑优区域淡灰填充，优势方与幅度一眼可辨
    - 纵轴 ±2/±4/±8 兵参考线；横轴步数刻度
    - 悬浮显示该步评分与败着信息；点击跳到该半步
    - 纵轴截断 ±8 兵（±800cp）：超出部分视为碾压，保证主变化区间可读
    """

    positionClicked = Signal(int)

    _CP_MAX = 800.0

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scores = {}       # idx -> cp（红方视角）
        self._total = 0         # 局面总数（含起始局面）
        self._marks = {}        # idx -> loss（你方视角败着）
        self._cn_moves = []     # 中文记谱（长度 = total - 1）
        self._cursor = 0
        self._hover_idx = None
        self.setMinimumHeight(130)
        self.setMouseTracking(True)
        self.setToolTip("评估曲线：上=红优，下=黑优；点击任意位置跳到该步")

    # ---------- 数据 ----------
    def set_data(self, scores: dict, total: int, marks: dict = None, cn_moves=None):
        self._scores = dict(scores or {})
        self._total = max(1, int(total))
        self._marks = dict(marks or {})
        self._cn_moves = list(cn_moves or [])
        self.update()

    def reset(self):
        self._scores, self._marks, self._cn_moves = {}, {}, []
        self._total, self._cursor = 0, 0
        self._hover_idx = None
        self.update()

    def set_cursor(self, idx: int):
        self._cursor = int(idx)
        self.update()

    # ---------- 坐标 ----------
    def _x_of(self, idx: float) -> float:
        m = 10.0
        if self._total <= 1:
            return m
        return m + (self.width() - 2 * m) * idx / (self._total - 1)

    def _y_of(self, cp: float) -> float:
        mid = self.height() / 2.0
        amp = max(10.0, self.height() / 2.0 - 16.0)
        v = max(-self._CP_MAX, min(self._CP_MAX, cp))
        return mid - (v / self._CP_MAX) * amp

    def _idx_at(self, x: float) -> int:
        if self._total < 2:
            return 0
        i = round((x - 10.0) / max(1.0, (self.width() - 20.0)) * (self._total - 1))
        return max(0, min(self._total - 1, int(i)))

    def _hover_text(self, idx: int) -> str:
        cp = self._scores.get(idx)
        txt = f"第 {idx}/{self._total - 1} 半步"
        # idx > 0 时，该局面由第 idx-1 步走完形成
        if 0 < idx <= len(self._cn_moves):
            color = "红" if (idx - 1) % 2 == 0 else "黑"
            txt += f"　{color}：{self._cn_moves[idx - 1]}"
        if cp is not None:
            txt += f"\n红方视角 {cp / 100:+.1f} 兵"
        mark = self._marks.get(idx)
        if mark is not None:
            txt += f"　⚠ 败着：此步亏 {abs(mark) / 100:.1f} 兵"
        return txt

    # ---------- 交互 ----------
    def mousePressEvent(self, ev):
        if self._total < 2:
            return
        self.positionClicked.emit(self._idx_at(ev.position().x()))

    def mouseMoveEvent(self, ev):
        if self._total < 2:
            return
        i = self._idx_at(ev.position().x())
        if i != self._hover_idx:
            self._hover_idx = i
            self.update()
        QToolTip.showText(ev.globalPosition().toPoint(),
                          self._hover_text(i), self)

    def leaveEvent(self, _ev):
        if self._hover_idx is not None:
            self._hover_idx = None
            self.update()

    # ---------- 绘制 ----------
    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        mid = h / 2.0
        p.fillRect(0, 0, w, h, QColor("#FBF8EF"))
        # 兵值参考线（±2/±4/±8 兵）
        p.setFont(QFont("Microsoft YaHei", 7))
        for cp in (-800, -400, -200, 200, 400, 800):
            y = self._y_of(cp)
            p.setPen(QPen(QColor("#EAE2CD"), 1))
            p.drawLine(6, int(y), w - 6, int(y))
            p.setPen(QColor("#A79A7C"))
            p.drawText(w - 22, int(y) + 4, f"{cp // 100:+d}")
        # 零轴（均势）
        p.setPen(QPen(QColor("#B8AD92"), 1, Qt.PenStyle.DashLine))
        p.drawLine(6, int(mid), w - 6, int(mid))
        p.setPen(QColor("#8D8067"))
        p.setFont(QFont("Microsoft YaHei", 8))
        p.drawText(10, 14, "红优")
        p.drawText(10, h - 5, "黑优")
        if not self._scores:
            p.setFont(QFont("Microsoft YaHei", 9))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                       "📈 点「⚡ 全程评估」生成曲线\n上红优 / 下黑优")
            p.end()
            return

        pts = [(i, self._x_of(i), self._y_of(cp))
               for i in range(self._total)
               if (cp := self._scores.get(i)) is not None]
        if self._total >= 2 and len(pts) >= 2:
            # 优势区域渐变填充：红优区从顶部浓红→零轴透明；黑优区从零轴透明→底部浓灰
            for k in range(len(pts) - 1):
                i1, x1, y1 = pts[k]
                i2, x2, y2 = pts[k + 1]
                cp1, cp2 = self._scores[i1], self._scores[i2]
                if (cp1 >= 0) == (cp2 >= 0):
                    segs = [((x1, y1), (x2, y2), cp1 >= 0)]
                else:
                    t = cp1 / (cp1 - cp2)
                    xc = x1 + (x2 - x1) * t
                    segs = [((x1, y1), (xc, mid), cp1 >= 0),
                            ((xc, mid), (x2, y2), cp2 >= 0)]
                for (sx1, sy1), (sx2, sy2), above in segs:
                    path_seg = QPainterPath()
                    path_seg.moveTo(sx1, sy1)
                    path_seg.lineTo(sx2, sy2)
                    path_seg.lineTo(sx2, mid)
                    path_seg.lineTo(sx1, mid)
                    path_seg.closeSubpath()
                    if above:
                        grad = QLinearGradient(0, 0, 0, mid)
                        grad.setColorAt(0.0, QColor(231, 76, 60, 70))
                        grad.setColorAt(1.0, QColor(231, 76, 60, 8))
                    else:
                        grad = QLinearGradient(0, mid, 0, h)
                        grad.setColorAt(0.0, QColor(84, 110, 122, 8))
                        grad.setColorAt(1.0, QColor(84, 110, 122, 70))
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(grad)
                    p.drawPath(path_seg)
            # 主折线
            path = QPainterPath()
            path.moveTo(pts[0][1], pts[0][2])
            for _i, x, y in pts[1:]:
                path.lineTo(x, y)
            p.setPen(QPen(QColor("#1565C0"), 1.8))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(path)
            # 横轴步数刻度
            step = max(1, round(self._total / 12))
            p.setPen(QColor("#A79A7C"))
            p.setFont(QFont("Microsoft YaHei", 7))
            for i in range(step, self._total, step):
                x = self._x_of(i)
                p.drawText(int(x) - 6, h - 4, str(i))
        # 败着红点（白描边加强对比）
        p.setBrush(QColor("#E74C3C"))
        p.setPen(QPen(QColor("#FFFFFF"), 1.6))
        for i in self._marks:
            cp = self._scores.get(i)
            if cp is None:
                continue
            x, y = self._x_of(i), self._y_of(cp)
            p.drawEllipse(int(x) - 4, int(y) - 4, 8, 8)
        # 悬浮参考线
        if self._hover_idx is not None and self._total >= 2:
            hx = self._x_of(self._hover_idx)
            p.setPen(QPen(QColor(90, 90, 90, 140), 1, Qt.PenStyle.DotLine))
            p.drawLine(int(hx), 4, int(hx), h - 4)
            hcp = self._scores.get(self._hover_idx)
            if hcp is not None:
                p.setPen(QPen(QColor("#546E7A"), 1.4))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawEllipse(int(hx) - 3, int(self._y_of(hcp)) - 3, 6, 6)
        # 当前位置光标（橙色竖线 + 光标处点位圆环）
        cx = int(self._x_of(self._cursor))
        p.setPen(QPen(QColor("#E67E22"), 1.8))
        p.drawLine(cx, 4, cx, h - 4)
        ccp = self._scores.get(self._cursor)
        if ccp is not None:
            p.setPen(QPen(QColor("#E67E22"), 2.0))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(int(cx) - 4, int(self._y_of(ccp)) - 4, 8, 8)
        p.end()


# ----------------- 主窗口 -----------------

class MainWindow(QMainWindow):
    PLAY_MODE = "play"
    TRAIN_MODE = "train"
    OPENING_MODE = "opening"
    MIDGAME_MODE = "midgame"
    TACTICS_MODE = "tactics"

    # 后台开局书导入完成（跨线程通知主线程刷新课程下拉框）
    auto_import_done = Signal()

    def __init__(self, engine_manager: EngineManager,
                 plan_path: str = None, stats_path: str = None):
        super().__init__()
        self.engine = engine_manager
        self.setWindowTitle("中国象棋培训大师")
        # 设置任务栏/窗口图标（开发/打包两种路径）
        try:
            from ..resources import bundle_root
            import os as _os
            ico_path = _os.path.join(bundle_root(), "app_icon.ico")
            if _os.path.isfile(ico_path):
                self.setWindowIcon(QIcon(ico_path))
        except Exception:
            pass
        self.resize(1080, 960)

        # 状态
        self.mode = self.PLAY_MODE
        self.board: Board = Board.start_position()
        self.player_color: Color = Color.RED          # 用户执方
        self.history: list = []                        # 走法记录 (frm,to,captured,ucci,cn)
        self.session: Optional[TrainingSession] = None
        self.opening_session: Optional[OpeningSession] = None
        self.midgame_session: Optional[MidgameSession] = None
        self.sf_session: Optional[TrainingSession] = None   # 基本杀法训练会话
        self._demo = None                 # 偏着演示状态
        self._current_pitfall = None      # 当前可演示的陷阱
        self.progress = ProgressStore(default_progress_path())
        self.plan_store = TrainingPlanStore(plan_path)
        self.play_stats = PlayStatsStore(stats_path)
        self._engine_thread: Optional[QThread] = None
        self._engine_worker: Optional[_EngineWorker] = None
        self._analyze_thread: Optional[QThread] = None
        self._analyze_worker: Optional[_EngineAnalyzeWorker] = None
        # 复盘状态（None=非复盘）
        self._review = None
        self._play_snapshot = None      # (board, history, over_flag)
        self._over_flag = False         # 对局已结束（无棋可走）
        self._review_eval_thread: Optional[QThread] = None
        self._review_eval_worker: Optional[_ReviewEvalWorker] = None
        self._review_play_timer = QTimer(self)
        self._review_play_timer.setInterval(700)
        self._review_play_timer.timeout.connect(self._on_review_play_tick)
        self._mateline_thread: Optional[QThread] = None
        self._mateline_worker: Optional[_MateLineWorker] = None
        self._eval_thread: Optional[QThread] = None
        self._eval_worker: Optional[_EngineAnalyzeWorker] = None
        self._thinking = False
        self._board_fullscreen = False   # 棋盘全屏状态标志

        self._build_ui()
        self._build_menu()
        self._build_statusbar()

        # 启动即隐藏非对弈面板：若 6 个模式面板同时可见，右侧布局最小高度约 1523px，
        # 超过屏幕高度会导致 showMaximized 最大化失败（窗口变成又窄又高、底部出屏）
        self.train_box.setVisible(False)
        self.opening_box.setVisible(False)
        self.midgame_box.setVisible(False)
        self.tactics_box.setVisible(False)

        # 延迟初始化引擎（避免阻塞界面构造），就绪后进入对弈模式
        QTimer.singleShot(50, self._init_engine)

    # ----------------- UI 构建 -----------------
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QHBoxLayout(central)
        layout.setContentsMargins(8, 8, 8, 8)

        # 棋盘
        self.board_widget = BoardWidget()
        self.board_widget.moveAttempted.connect(self._on_move_attempted)
        # 把棋盘点击"为什么点不动"的提示挂到状态栏，便于用户和我定位交互问题
        self.board_widget.hintMessage.connect(lambda msg: self.status.showMessage(msg, 3500))
        # 棋盘右下角 ⛶ 全屏切换
        self.board_widget.fullscreenToggled.connect(self._toggle_board_fullscreen)
        layout.addWidget(self.board_widget, 1)

        # 右侧面板
        self.side_panel = self._build_side_panel()
        layout.addWidget(self.side_panel, 0)

    def _toggle_board_fullscreen(self):
        """棋盘全屏：隐藏右侧面板+状态栏，主窗口全屏；再触发则还原。"""
        if not self._board_fullscreen:
            self._board_fullscreen = True
            self._was_maximized = self.isMaximized()
            self.side_panel.hide()
            self.statusBar().hide()
            self.showFullScreen()
        else:
            self._board_fullscreen = False
            self.side_panel.setVisible(True)
            self.statusBar().show()
            if getattr(self, "_was_maximized", False):
                self.showMaximized()
            else:
                self.showNormal()

    def keyPressEvent(self, event):
        # Esc 退出棋盘全屏（焦点在其它控件时也能生效）
        if event.key() == Qt.Key.Key_Escape and self._board_fullscreen:
            self._toggle_board_fullscreen()
            return
        super().keyPressEvent(event)

    def _wrap_scroll(self, label: QLabel, max_h: int) -> QScrollArea:
        """把文字标签包进无边框滚动区：内容短时随内容收缩，过长时出滚动条。"""
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        area.setMaximumHeight(max_h)
        area.setWidget(label)
        return area

    def _build_side_panel(self) -> QWidget:
        panel = QFrame()
        panel.setFixedWidth(300)
        v = QVBoxLayout(panel)
        v.setContentsMargins(6, 6, 6, 6)

        title = QLabel("🀄 中国象棋培训大师")
        title.setStyleSheet("font-size:18px;font-weight:bold;color:#8B4513;")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(title)

        self.lbl_engine = QLabel("引擎加载中…")
        self.lbl_engine.setStyleSheet("color:#666;font-size:11px;")
        self.lbl_engine.setWordWrap(True)
        v.addWidget(self.lbl_engine)

        # 模式切换（4+3 网格，7 个入口；精简文字配 tooltip 保持紧凑且醒目）
        mode_box = QGroupBox("模式")
        mh = QGridLayout(mode_box)
        mh.setSpacing(4)
        self.btn_play = QPushButton("♟ 对弈")
        self.btn_train = QPushButton("🎓 残局")
        self.btn_opening = QPushButton("📖 开局")
        self.btn_midgame = QPushButton("⚔ 中局")
        self.btn_sf = QPushButton("🎯 杀法")
        self.btn_mindset = QPushButton("🧠 口诀")
        self.btn_plan = QPushButton("📅 计划")
        self.btn_play.setToolTip("人机对弈")
        self.btn_train.setToolTip("残局训练")
        self.btn_opening.setToolTip("开局训练")
        self.btn_midgame.setToolTip("中局战术训练")
        self.btn_sf.setToolTip("基本杀法训练")
        self.btn_mindset.setToolTip("象棋思维口诀与逻辑")
        self.btn_plan.setToolTip("每日训练计划与打卡")
        self.btn_play.clicked.connect(self.start_play)
        self.btn_train.clicked.connect(self.start_training_default)
        self.btn_opening.clicked.connect(self.start_opening_default)
        self.btn_midgame.clicked.connect(self.start_midgame_default)
        self.btn_sf.clicked.connect(self.start_sf_default)
        self.btn_mindset.clicked.connect(self._show_mindset_dialog)
        self.btn_plan.clicked.connect(self._show_plan_dialog)
        self.btn_plan.setStyleSheet("font-weight:bold;")
        mh.addWidget(self.btn_play, 0, 0)
        mh.addWidget(self.btn_opening, 0, 1)
        mh.addWidget(self.btn_midgame, 0, 2)
        mh.addWidget(self.btn_train, 0, 3)
        mh.addWidget(self.btn_sf, 1, 0)
        mh.addWidget(self.btn_mindset, 1, 1)
        mh.addWidget(self.btn_plan, 1, 2)
        v.addWidget(mode_box)

        # 对弈面板
        self.play_box = QGroupBox("对弈")
        ph = QVBoxLayout(self.play_box)
        self.lbl_turn = QLabel("轮到：红方（你）")
        self.lbl_turn.setStyleSheet("font-size:14px;font-weight:bold;")
        ph.addWidget(self.lbl_turn)

        row_diff = QHBoxLayout()
        row_diff.addWidget(QLabel("对手棋力:"))
        self.cmb_diff = QComboBox()
        for i in range(1, 21):
            self.cmb_diff.addItem(f"{i}级·{self._strength_tier(i)}", i)
        self.cmb_diff.setCurrentIndex(9)   # 默认 10 级·中级
        self.cmb_diff.currentIndexChanged.connect(self._on_difficulty_changed)
        row_diff.addWidget(self.cmb_diff, 1)
        ph.addLayout(row_diff)

        # 执方选择：对弈时选择执红（先手）或执黑（后手，引擎先走）
        row_side = QHBoxLayout()
        row_side.addWidget(QLabel("执方:"))
        self.cmb_side = QComboBox()
        self.cmb_side.addItem("🔴 我执红（先手）", Color.RED)
        self.cmb_side.addItem("⚫ 我执黑（后手）", Color.BLACK)
        self.cmb_side.setCurrentIndex(0)
        row_side.addWidget(self.cmb_side, 1)
        # 切换执方后立即重开新局（避免对弈中途切阵营造成混乱）
        self.cmb_side.currentIndexChanged.connect(self._on_side_changed)
        ph.addLayout(row_side)

        row_btns = QHBoxLayout()
        self.btn_undo = QPushButton("↶ 悔棋")
        self.btn_new = QPushButton("⟳ 新局")
        self.btn_suggest = QPushButton("💡 建议")
        self.btn_foe = QPushButton("🔍 对方意图")
        self.btn_undo.clicked.connect(self.undo_move)
        self.btn_new.clicked.connect(self.start_play)
        self.btn_suggest.clicked.connect(self._request_suggestion)
        self.btn_foe.clicked.connect(self._request_foe_intent)
        row_btns.addWidget(self.btn_undo)
        row_btns.addWidget(self.btn_new)
        row_btns.addWidget(self.btn_suggest)
        row_btns.addWidget(self.btn_foe)
        ph.addLayout(row_btns)

        # 认输/求和：第二行按钮，避免与上方操作行拥挤
        row_end = QHBoxLayout()
        self.btn_resign = QPushButton("🏳 认输")
        self.btn_resign.setToolTip("主动认输（本局判负）")
        self.btn_resign.setStyleSheet("QPushButton{color:#C0392B;}")
        self.btn_draw = QPushButton("🤝 求和")
        self.btn_draw.setToolTip("向引擎提出和棋（引擎评估后决定接受或拒绝）")
        self.btn_draw.setStyleSheet("QPushButton{color:#8E44AD;}")
        self.btn_resign.clicked.connect(self._on_resign)
        self.btn_draw.clicked.connect(self._on_offer_draw)
        row_end.addStretch(1)
        row_end.addWidget(self.btn_resign)
        row_end.addWidget(self.btn_draw)
        ph.addLayout(row_end)

        # 对弈战绩：总盘数 / 胜 / 负 / 和 + 胜率，带重置按钮
        row_rec = QHBoxLayout()
        self.lbl_record = QLabel("")
        self.lbl_record.setStyleSheet("font-size:13px;font-weight:bold;color:#333;")
        row_rec.addWidget(self.lbl_record, 1)
        self.btn_review = QPushButton("🔍 复盘")
        self.btn_review.setToolTip("回放历史对局：逐步观棋 + 引擎全程评估 + 败着标记")
        self.btn_review.clicked.connect(self._show_review_dialog)
        row_rec.addWidget(self.btn_review)
        self.btn_reset_stats = QPushButton("🗑 重置战绩")
        self.btn_reset_stats.setToolTip("清空对弈胜/负/和统计（不可恢复）")
        self.btn_reset_stats.clicked.connect(self._reset_play_record)
        row_rec.addWidget(self.btn_reset_stats)
        ph.addLayout(row_rec)
        self._refresh_play_record()

        # 复盘导航工具条（复盘时显示；两行布局防挤压）
        self.review_bar = QWidget()
        rv = QVBoxLayout(self.review_bar)
        rv.setContentsMargins(0, 2, 0, 2)
        rv.setSpacing(2)
        # 第 1 行：回放导航
        row_r1 = QHBoxLayout()
        row_r1.setSpacing(3)
        self.btn_rev_first = QPushButton("⏮")
        self.btn_rev_prev = QPushButton("◀")
        self.btn_rev_next = QPushButton("▶")
        self.btn_rev_last = QPushButton("⏭")
        for b in (self.btn_rev_first, self.btn_rev_prev,
                  self.btn_rev_next, self.btn_rev_last):
            b.setFixedWidth(34)
            b.setToolTip("回放：首 / 上一步 / 下一步 / 末尾")
        self.lbl_rev_pos = QLabel("")
        self.lbl_rev_pos.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_rev_pos.setStyleSheet("font-size:13px;font-weight:bold;")
        self.btn_rev_first.clicked.connect(lambda: self._review_goto(0))
        self.btn_rev_prev.clicked.connect(lambda: self._review_goto(self._review["idx"] - 1) if self._review else None)
        self.btn_rev_next.clicked.connect(lambda: self._review_goto(self._review["idx"] + 1) if self._review else None)
        self.btn_rev_last.clicked.connect(lambda: self._review_goto(10**9) if self._review else None)
        row_r1.addWidget(self.btn_rev_first)
        row_r1.addWidget(self.btn_rev_prev)
        row_r1.addWidget(self.lbl_rev_pos, 1)
        row_r1.addWidget(self.btn_rev_next)
        row_r1.addWidget(self.btn_rev_last)
        rv.addLayout(row_r1)
        # 第 2 行：动作按钮
        row_r2 = QHBoxLayout()
        row_r2.setSpacing(3)
        self.btn_rev_play = QPushButton("▶ 自动播放")
        self.btn_rev_play.setToolTip("自动逐半步回放整盘棋（可随时暂停）")
        self.btn_rev_play.setCheckable(True)
        self.btn_rev_play.clicked.connect(self._toggle_review_play)
        self.cmb_rev_speed = QComboBox()
        self.cmb_rev_speed.setToolTip("自动播放速度")
        self.cmb_rev_speed.addItem("慢速", 1200)
        self.cmb_rev_speed.addItem("中速", 700)
        self.cmb_rev_speed.addItem("快速", 350)
        self.cmb_rev_speed.setCurrentIndex(1)
        self.btn_rev_eval = QPushButton("⚡ 全程评估")
        self.btn_rev_eval.setToolTip("引擎逐半步评估（每步约0.25秒），标出败着")
        self.btn_rev_exit = QPushButton("✕ 退出复盘")
        self.btn_rev_exit.setStyleSheet("font-weight:bold;color:#C0392B;")
        self.btn_rev_eval.clicked.connect(self._review_evaluate)
        self.btn_rev_exit.clicked.connect(self._review_exit)
        row_r2.addWidget(self.btn_rev_play)
        row_r2.addWidget(self.cmb_rev_speed)
        row_r2.addWidget(self.btn_rev_eval)
        row_r2.addStretch(1)
        row_r2.addWidget(self.btn_rev_exit)
        rv.addLayout(row_r2)
        # 第 3 行：评估曲线（⚡评估时实时生长；点击任意点跳到该半步）
        self.score_curve = ScoreCurveWidget()
        self.score_curve.setFixedHeight(150)
        self.score_curve.positionClicked.connect(self._review_goto)
        rv.addWidget(self.score_curve)
        # 第 4 行：评估/败着详情（可换行，信息完整展示）
        self.lbl_rev_eval = QLabel("")
        self.lbl_rev_eval.setWordWrap(True)
        self.lbl_rev_eval.setStyleSheet("font-size:12px;line-height:1.5;")
        self.lbl_rev_eval.hide()
        rv.addWidget(self.lbl_rev_eval)
        self.review_bar.hide()
        ph.addWidget(self.review_bar)

        # 教练建议讲解框
        self.lbl_suggest = QLabel("")
        self.lbl_suggest.setWordWrap(True)
        self.lbl_suggest.setStyleSheet("font-size:14px;color:#1B5E20;background:#E8F5E9;"
                                       "padding:6px;border:1px solid #A5D6A7;border-radius:4px;")
        ph.addWidget(self._wrap_scroll(self.lbl_suggest, 220), 2)
        v.addWidget(self.play_box, 3)

        # 训练面板
        self.train_box = QGroupBox("残局训练")
        th = QVBoxLayout(self.train_box)
        self.cmb_puzzle = QComboBox()
        for p in all_puzzles():
            self.cmb_puzzle.addItem(f"{p.title}（{p.goal}）", p.id)
        self.cmb_puzzle.currentIndexChanged.connect(self._on_puzzle_selected)
        th.addWidget(self.cmb_puzzle)

        self.btn_train_review = QPushButton("📌 错题复习")
        self.btn_train_review.setStyleSheet(
            "QPushButton { padding:3px 8px; background:#FFF3E0; font-weight:600; "
            "border:1px solid #FFB74D; border-radius:4px; color:#E65100; } "
            "QPushButton:hover { background:#FFE0B2; }"
        )
        self.btn_train_review.clicked.connect(
            lambda: self._review_wrong(self.cmb_puzzle, self._on_puzzle_selected,
                                       [p.id for p in all_puzzles()]))
        th.addWidget(self.btn_train_review)

        self.lbl_puzzle = QLabel("")
        self.lbl_puzzle.setWordWrap(True)
        self.lbl_puzzle.setStyleSheet("font-size:14px;color:#333;background:#FBF6E9;"
                                      "padding:6px;border:1px solid #E0D4B0;border-radius:4px;")
        th.addWidget(self._wrap_scroll(self.lbl_puzzle, 200), 1)

        trow = QHBoxLayout()
        self.btn_hint = QPushButton("💡 提示")
        self.btn_solution = QPushButton("👁 看解法")
        self.btn_reset = QPushButton("↺ 重置")
        self.btn_hint.clicked.connect(self._training_hint)
        self.btn_solution.clicked.connect(self._training_show_solution)
        self.btn_reset.clicked.connect(self._training_reset)
        trow.addWidget(self.btn_hint)
        trow.addWidget(self.btn_solution)
        trow.addWidget(self.btn_reset)
        th.addLayout(trow)

        trow2 = QHBoxLayout()
        self.btn_prev = QPushButton("◀ 上一题")
        self.btn_next = QPushButton("下一题 ▶")
        self.btn_prev.clicked.connect(self._training_prev)
        self.btn_next.clicked.connect(self._training_next)
        trow2.addWidget(self.btn_prev)
        trow2.addWidget(self.btn_next)
        th.addLayout(trow2)

        self.lbl_progress = QLabel("")
        self.lbl_progress.setStyleSheet("color:#2E7D32;font-size:12px;")
        th.addWidget(self.lbl_progress)
        v.addWidget(self.train_box, 3)

        # 开局训练面板
        self.opening_box = QGroupBox("开局训练")
        oh = QVBoxLayout(self.opening_box)
        # 来源分组：内置 / bin开局书 / OBK飞刀库，用下拉框选择库，课程列表只显示当前库
        self.cmb_source = QComboBox()
        self.cmb_source.addItem("🏛 内置课程", "builtin")
        self.cmb_source.addItem("📗 bin开局书", "bin")
        self.cmb_source.addItem("🗡 OBK飞刀库", "obk")
        self.cmb_source.currentIndexChanged.connect(self._on_source_changed)
        oh.addWidget(self.cmb_source)

        self.cmb_lesson = QComboBox()
        self.cmb_lesson.currentIndexChanged.connect(self._on_lesson_selected)
        oh.addWidget(self.cmb_lesson)
        # 各来源分组各自记住上次选中，切换库后可恢复
        self._lesson_cur = {"builtin": None, "bin": None, "obk": None}
        self._auto_import_running = False
        # 后台静默导入完成后刷新课程下拉框（信号跨线程，不用 QTimer）
        self.auto_import_done.connect(self._refresh_cmb_lesson)
        self._refresh_cmb_lesson()
        # 启动时自动静默导入「开局书/」目录下的 .bin/.obk 作为开局课（后台线程，不阻塞窗口出现）
        QTimer.singleShot(200, self._start_auto_import_thread)

        self.btn_import_book = QPushButton("📚 导入 .bin/.obk 开局书（自动拆成新课）")
        self.btn_import_book.setStyleSheet(
            "QPushButton { padding:4px 8px; background:#E3F2FD; font-weight:600; "
            "border:1px solid #90CAF9; border-radius:4px; color:#0D47A1; } "
            "QPushButton:hover { background:#BBDEFB; }"
        )
        self.btn_import_book.clicked.connect(self._import_book_lessons)
        oh.addWidget(self.btn_import_book)

        self.lbl_opening = QLabel("")
        self.lbl_opening.setWordWrap(True)
        self.lbl_opening.setStyleSheet("font-size:14px;color:#333;background:#FBF6E9;"
                                       "padding:6px;border:1px solid #E0D4B0;border-radius:4px;")
        oh.addWidget(self._wrap_scroll(self.lbl_opening, 200), 1)

        self.lbl_coach = QLabel("")
        self.lbl_coach.setWordWrap(True)
        self.lbl_coach.setStyleSheet("font-size:14px;color:#1B5E20;background:#E8F5E9;"
                                     "padding:6px;border:1px solid #A5D6A7;border-radius:4px;")
        self.lbl_coach.setMinimumHeight(96)
        oh.addWidget(self._wrap_scroll(self.lbl_coach, 300), 2)

        orow = QHBoxLayout()
        self.btn_o_hint = QPushButton("💡 正着")
        self.btn_o_pitfall = QPushButton("⚠ 演示偏着")
        self.btn_o_demo = QPushButton("👁 演示")
        self.btn_o_reset = QPushButton("↺ 重置")
        self.btn_o_hint.clicked.connect(self._opening_hint)
        self.btn_o_pitfall.clicked.connect(self._on_pitfall_clicked)
        self.btn_o_demo.clicked.connect(self._opening_demo)
        self.btn_o_reset.clicked.connect(self._opening_reset)
        orow.addWidget(self.btn_o_hint)
        orow.addWidget(self.btn_o_pitfall)
        self.btn_o_pitfall.setVisible(False)
        orow.addWidget(self.btn_o_demo)
        orow.addWidget(self.btn_o_reset)
        oh.addLayout(orow)

        self.lbl_opening_progress = QLabel("")
        self.lbl_opening_progress.setStyleSheet("color:#2E7D32;font-size:12px;")
        oh.addWidget(self.lbl_opening_progress)
        v.addWidget(self.opening_box, 3)

        # 中局战术面板
        self.midgame_box = QGroupBox("中局战术训练")
        gh = QVBoxLayout(self.midgame_box)
        self.cmb_tactic = QComboBox()
        for t in all_tactics():
            side_cn = "红" if t.side is Color.RED else "黑"
            self.cmb_tactic.addItem(f"{t.title}（你执{side_cn}）", t.id)
        self.cmb_tactic.currentIndexChanged.connect(self._on_tactic_selected)
        gh.addWidget(self.cmb_tactic)

        self.btn_mid_review = QPushButton("📌 错题复习")
        self.btn_mid_review.setStyleSheet(
            "QPushButton { padding:3px 8px; background:#FFF3E0; font-weight:600; "
            "border:1px solid #FFB74D; border-radius:4px; color:#E65100; } "
            "QPushButton:hover { background:#FFE0B2; }"
        )
        self.btn_mid_review.clicked.connect(
            lambda: self._review_wrong(self.cmb_tactic, self._on_tactic_selected,
                                       [t.id for t in all_tactics()]))
        gh.addWidget(self.btn_mid_review)

        self.lbl_midgame = QLabel("")
        self.lbl_midgame.setWordWrap(True)
        self.lbl_midgame.setStyleSheet("font-size:14px;color:#333;background:#FBF6E9;"
                                       "padding:6px;border:1px solid #E0D4B0;border-radius:4px;")
        gh.addWidget(self._wrap_scroll(self.lbl_midgame, 200), 1)

        self.lbl_mid_coach = QLabel("")
        self.lbl_mid_coach.setWordWrap(True)
        self.lbl_mid_coach.setStyleSheet("font-size:14px;color:#1B5E20;background:#E8F5E9;"
                                         "padding:6px;border:1px solid #A5D6A7;border-radius:4px;")
        self.lbl_mid_coach.setMinimumHeight(96)
        gh.addWidget(self._wrap_scroll(self.lbl_mid_coach, 300), 2)

        grow = QHBoxLayout()
        self.btn_m_hint = QPushButton("💡 提示")
        self.btn_m_pitfall = QPushButton("⚠ 看看不走这步会怎样")
        self.btn_m_demo = QPushButton("👁 演示正着")
        self.btn_m_reset = QPushButton("↺ 重置")
        self.btn_m_hint.clicked.connect(self._midgame_hint)
        self.btn_m_pitfall.clicked.connect(self._on_pitfall_clicked)
        self.btn_m_demo.clicked.connect(self._midgame_demo)
        self.btn_m_reset.clicked.connect(self._midgame_reset)
        grow.addWidget(self.btn_m_hint)
        grow.addWidget(self.btn_m_pitfall)
        self.btn_m_pitfall.setVisible(False)
        grow.addWidget(self.btn_m_demo)
        grow.addWidget(self.btn_m_reset)
        gh.addLayout(grow)

        mrow2 = QHBoxLayout()
        self.btn_m_prev = QPushButton("◀ 上一题")
        self.btn_m_next = QPushButton("下一题 ▶")
        self.btn_m_prev.clicked.connect(self._midgame_prev)
        self.btn_m_next.clicked.connect(self._midgame_next)
        mrow2.addWidget(self.btn_m_prev)
        mrow2.addWidget(self.btn_m_next)
        gh.addLayout(mrow2)

        self.lbl_midgame_progress = QLabel("")
        self.lbl_midgame_progress.setStyleSheet("color:#2E7D32;font-size:12px;")
        gh.addWidget(self.lbl_midgame_progress)
        v.addWidget(self.midgame_box, 3)

        # 基本杀法训练面板（复用残局训练的 TrainingSession 判定机制）
        self.tactics_box = QGroupBox("基本杀法训练")
        sh = QVBoxLayout(self.tactics_box)
        self.cmb_sf = QComboBox()
        for p in all_tactics_puzzles():
            self.cmb_sf.addItem(f"{p.title}（{p.goal}）", p.id)
        self.cmb_sf.currentIndexChanged.connect(self._on_sf_selected)
        sh.addWidget(self.cmb_sf)

        self.btn_sf_review = QPushButton("📌 错题复习")
        self.btn_sf_review.setStyleSheet(
            "QPushButton { padding:3px 8px; background:#FFF3E0; font-weight:600; "
            "border:1px solid #FFB74D; border-radius:4px; color:#E65100; } "
            "QPushButton:hover { background:#FFE0B2; }"
        )
        self.btn_sf_review.clicked.connect(
            lambda: self._review_wrong(self.cmb_sf, self._on_sf_selected,
                                       [p.id for p in all_tactics_puzzles()]))
        sh.addWidget(self.btn_sf_review)

        self.lbl_sf = QLabel("")
        self.lbl_sf.setWordWrap(True)
        self.lbl_sf.setStyleSheet("font-size:14px;color:#333;background:#FBF6E9;"
                                   "padding:6px;border:1px solid #E0D4B0;border-radius:4px;")
        sh.addWidget(self._wrap_scroll(self.lbl_sf, 200), 1)

        srow = QHBoxLayout()
        self.btn_sf_hint = QPushButton("💡 提示")
        self.btn_sf_solution = QPushButton("👁 看解法")
        self.btn_sf_reset = QPushButton("↺ 重置")
        self.btn_sf_hint.clicked.connect(self._sf_hint)
        self.btn_sf_solution.clicked.connect(self._sf_show_solution)
        self.btn_sf_reset.clicked.connect(self._sf_reset)
        srow.addWidget(self.btn_sf_hint)
        srow.addWidget(self.btn_sf_solution)
        srow.addWidget(self.btn_sf_reset)
        sh.addLayout(srow)

        srow2 = QHBoxLayout()
        self.btn_sf_prev = QPushButton("◀ 上一题")
        self.btn_sf_next = QPushButton("下一题 ▶")
        self.btn_sf_prev.clicked.connect(self._sf_prev)
        self.btn_sf_next.clicked.connect(self._sf_next)
        srow2.addWidget(self.btn_sf_prev)
        srow2.addWidget(self.btn_sf_next)
        sh.addLayout(srow2)

        self.lbl_sf_progress = QLabel("")
        self.lbl_sf_progress.setStyleSheet("color:#2E7D32;font-size:12px;")
        sh.addWidget(self.lbl_sf_progress)
        v.addWidget(self.tactics_box, 3)

        # 走法记录
        v.addWidget(QLabel("走法记录:"))
        self.move_list = QListWidget()
        self.move_list.setMaximumHeight(120)
        self.move_list.setStyleSheet("font-size:12px;")
        v.addWidget(self.move_list, 1)

        # 子力对比（对弈模式专用，走子时实时刷新）
        self.lbl_material = QLabel("")
        self.lbl_material.setStyleSheet(
            "font-size:13px; padding:6px; background:#FAFAFA;"
            "border:1px solid #DDD; border-radius:4px;")
        self.lbl_material.setWordWrap(True)
        self.lbl_material.setVisible(False)
        v.addWidget(self.lbl_material)

        return panel

    def _build_menu(self):
        mb = self.menuBar()
        m_train = mb.addMenu("训练")
        a_play = QAction("人机对弈", self)
        a_play.triggered.connect(self.start_play)
        a_trains = QAction("残局训练", self)
        a_trains.triggered.connect(self.start_training_default)
        a_opening = QAction("开局训练", self)
        a_opening.triggered.connect(self.start_opening_default)
        a_imp_bin = QAction("从 .bin/.obk 开局书导入开局课…", self)
        a_imp_bin.triggered.connect(self._import_book_lessons)
        m_train.addAction(a_play)
        m_train.addAction(a_trains)
        m_train.addAction(a_opening)
        m_train.addAction(a_imp_bin)
        a_midgame = QAction("中局战术训练", self)
        a_midgame.triggered.connect(self.start_midgame_default)
        m_train.addAction(a_midgame)
        a_sf = QAction("基本杀法训练", self)
        a_sf.triggered.connect(self.start_sf_default)
        m_train.addAction(a_sf)
        m_train.addSeparator()
        a_mindset = QAction("思维口诀与逻辑…", self)
        a_mindset.triggered.connect(self._show_mindset_dialog)
        m_train.addAction(a_mindset)
        m_train.addSeparator()
        a_sound = QAction("🔊 音效", self)
        a_sound.setCheckable(True)
        a_sound.setChecked(sound.enabled)
        a_sound.toggled.connect(lambda on: setattr(sound, "enabled", on))
        m_train.addAction(a_sound)

        m_engine = mb.addMenu("引擎")
        for lv in range(1, 21):
            a = QAction(f"难度 {lv}级·{self._strength_tier(lv)}", self, checkable=True)
            a.triggered.connect(lambda _=False, i=lv: self._set_difficulty(i))
            m_engine.addAction(a)
        m_engine.addSeparator()
        a_load_book = QAction("加载 .bin 开局书…", self)
        a_load_book.triggered.connect(self._load_opening_book)
        m_engine.addAction(a_load_book)
        self._a_toggle_book = QAction("启用开局书", self, checkable=True, enabled=False)
        self._a_toggle_book.triggered.connect(self._toggle_opening_book)
        m_engine.addAction(self._a_toggle_book)
        a_unload_book = QAction("卸载开局书", self)
        a_unload_book.triggered.connect(lambda: self._load_opening_book(force_unload=True))
        m_engine.addAction(a_unload_book)

        m_help = mb.addMenu("帮助")
        a_help = QAction("使用说明", self)
        a_help.triggered.connect(self._show_help)
        m_help.addAction(a_help)
        a_about = QAction("关于", self)
        a_about.triggered.connect(self._show_about)
        m_help.addAction(a_about)

    def _build_statusbar(self):
        self.status = self.statusBar()
        self.status.showMessage("就绪")

    # ----------------- 引擎 -----------------
    def _init_engine(self):
        self.status.showMessage("正在加载引擎…")
        name = self.engine.initialize()
        self._engine_name_base = name
        self._engine_has_pika = self.engine.using_pikafish
        self._refresh_engine_label()
        self.status.showMessage(f"引擎就绪：{name}", 4000)
        # 恢复上次选择的对手棋力（play_stats 持久化）
        self._set_difficulty(getattr(self.play_stats, "level", 10))
        # 引擎就绪后进入对弈模式（原先用 singleShot(60) 但引擎握手阻塞
        # 事件循环时，该定时器可能被推迟到用户走子之后才触发，从而重置棋盘）
        self.start_play()

    def _refresh_engine_label(self):
        suffix = ("" if self._engine_has_pika
                  else "（未检测到Pikafish，使用内置弱引擎。放入engine_bin/pikafish.exe可升级）")
        book = self.engine.opening_book_info
        if book != "未加载":
            suffix = f"{suffix}  |  开局书：{book}"
        self.lbl_engine.setText(f"当前引擎：{self._engine_name_base}{suffix}")
        # 菜单启用状态同步
        if hasattr(self, "_a_toggle_book"):
            self._a_toggle_book.setEnabled(self.engine.opening_book.loaded)
            self._a_toggle_book.setChecked(self.engine.opening_book.enabled)

    def _load_opening_book(self, force_unload: bool = False):
        """加载 Xiangqi Polyglot .bin 开局书（force_unload=True 则直接卸载）。"""
        if force_unload:
            self.engine.load_opening_book(None)
            self.status.showMessage("已卸载开局书", 3000)
            self._refresh_engine_label()
            return
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择中国象棋 .bin 开局书文件",
            "",
            "Xiangqi Polyglot 开局书 (*.bin);;所有文件 (*.*)",
        )
        if not path:
            return
        ok = self.engine.load_opening_book(path)
        if ok:
            self.status.showMessage(f"开局书加载成功：{self.engine.opening_book_info}", 5000)
        else:
            QMessageBox.warning(
                self,
                "加载失败",
                "该文件无法识别为有效的 Xiangqi Polyglot 开局书。\n"
                "请确认是中国象棋专用 .bin 格式（非国际象棋 Polyglot）。",
            )
        self._refresh_engine_label()

    def _toggle_opening_book(self, checked: bool):
        self.engine.set_opening_book_enabled(bool(checked))
        self._refresh_engine_label()
        self.status.showMessage(f"开局书 {'已启用' if checked else '已禁用'}", 2500)

    # ----------------- 对手棋力 -----------------
    @staticmethod
    def _strength_tier(level: int) -> str:
        """1-20 级 → 分段名称。"""
        if level >= 17:
            return "特级"
        if level >= 13:
            return "高级"
        if level >= 9:
            return "中级"
        if level >= 5:
            return "初级"
        return "新手"

    def _set_difficulty(self, level: int):
        self.engine.set_difficulty(level)
        self.play_stats.set_level(level)          # 记住等级，跨启动恢复
        self.cmb_difficulty_block = True
        self.cmb_diff.setCurrentIndex(max(0, min(19, int(level) - 1)))
        self.cmb_difficulty_block = False
        self.status.showMessage(f"难度设为 第{level}级", 3000)

    def _on_difficulty_changed(self, _idx):
        if getattr(self, "cmb_difficulty_block", False):
            return
        lvl = self.cmb_diff.currentData()
        if lvl:
            self.engine.set_difficulty(lvl)
            self.play_stats.set_level(lvl)       # 记住等级，跨启动恢复

    # ----------------- 对弈模式 -----------------
    def _on_side_changed(self, _idx):
        """切换"执方"后，若处于对弈模式则立即重开新局（沿用最新选择）。
        注意：start_play() 内部可能暂时改 mode，所以先把 mode==PLAY_MODE 的判定
        和信号阻断一起做，避免递归/反复触发。"""
        if self.mode != self.PLAY_MODE:
            return
        # 阻断信号：start_play() 内部若改下拉框（比如切回 RED）不能再反弹进来
        self.cmb_side.blockSignals(True)
        try:
            self.start_play()
        finally:
            self.cmb_side.blockSignals(False)

    # 内部辅助：按当前回合正确值 **强制写入** board_widget.interactive。
    # 历史教训：单一 QTimer / signal 链路容易在竞争条件下"丢解锁"，所以这里"永远允许被调用"。
    def _force_sync_interactive(self):
        """按回合真实情况把 interactive 调整到正确状态。永不抛出，永不拦截。

        注意：这里只看「当前轮到谁下 == 玩家执方」。**不再依赖 _thinking 标志。**
        理由：如果 Pikafish QThread 真的卡死、_thinking 永远 True，那 turn 显示、interactive、
        状态条三者还是要按 side 的真实值走——否则用户明明"轮到他了"却永远点不动。
        （真卡住时 mousePressEvent 已经会放行，这里再做 state 层同步。）
        """
        should_enable = (self.board.side is self.player_color)
        self.board_widget.set_interactive(should_enable)
        if should_enable:
            # 清掉可能残留的"引擎思考中…"状态栏永久文字
            self.status.clearMessage()
            self.status.showMessage("🎯 轮到你了！请点击带金色外环的己方棋子下棋。", 3000)
        self._update_turn_label()

    # 发送 N 次"定时脉冲"：在 300ms / 1s / 2s 三个时刻各跑一次 _force_sync_interactive。
    # 不管之前哪条链路丢了，脉冲过后最终状态一定正确。
    def _schedule_unlock_pulses(self):
        for delay_ms in (300, 1000, 2000):
            QTimer.singleShot(delay_ms, self._force_sync_interactive)

    def start_play(self):
        self.mode = self.PLAY_MODE
        self.session = None
        self.sf_session = None
        self._reset_demo_state()
        self.opening_session = None
        self.midgame_session = None
        self.board = Board.start_position()

        # 重置跨局状态标志（上一局终局后可能残留 True）
        self._over_flag = False
        self._thinking = False

        # 读取"执方"下拉框：我执红先手 / 我执黑后手
        if hasattr(self, "cmb_side"):
            chosen = self.cmb_side.currentData()
            if isinstance(chosen, Color):
                self.player_color = chosen
            else:
                self.player_color = Color.RED
        else:
            self.player_color = Color.RED

        self.history.clear()
        self._refresh_move_list()
        self.play_box.setVisible(True)
        self.train_box.setVisible(False)
        self.opening_box.setVisible(False)
        self.midgame_box.setVisible(False)
        self.tactics_box.setVisible(False)
        self.board_widget.set_player_color(self.player_color)
        # 翻转棋盘：执黑时让自己的黑子在下方，敌方红子在上方（模拟真实对弈视角）
        self.board_widget.flip(self.player_color is Color.BLACK)
        self.board_widget.set_board(self.board)
        self.lbl_suggest.setText("")
        self._update_turn_label()
        if self.player_color is Color.RED:
            self.status.showMessage("人机对弈：你执红先行", 4000)
            # 我执红：第一回合就是我的，棋盘默认交互打开
            self.board_widget.set_interactive(True)
        else:
            self.status.showMessage("人机对弈：你执黑后手，引擎走红第一步…", 4000)
            # 我执黑：第一步由引擎（红）走，先锁交互，引擎走子+动画结束后双通道自动解锁
            self.board_widget.set_interactive(False)
            # 不使用 QTimer.singleShot(80)：事件循环竞争可能导致没触发，直接同步调
            self._request_engine_move()

    def _update_turn_label(self):
        if self._thinking:
            self.lbl_turn.setText("引擎思考中…")
            self.lbl_turn.setStyleSheet("font-size:14px;font-weight:bold;color:#E67E22;")
        else:
            side = self.board.side
            you = "（你）" if side is self.player_color else "（引擎）"
            name = side.name_cn + you
            color = "#C0392B" if side is Color.RED else "#333"
            self.lbl_turn.setText(f"轮到：{name}")
            self.lbl_turn.setStyleSheet(f"font-size:14px;font-weight:bold;color:{color};")
        # 同步对弈按钮启用状态
        self._update_play_buttons()

    def _update_play_buttons(self):
        """统一管理对弈按钮启用状态：非对弈模式/引擎思考/终局时禁用对应按钮。"""
        if not hasattr(self, "btn_resign"):
            return
        in_play = self.mode == self.PLAY_MODE
        can_act = in_play and not self._thinking and not self._game_over
        # 认输/求和：对弈模式+非思考+非终局才可用
        self.btn_resign.setEnabled(can_act)
        self.btn_draw.setEnabled(can_act and len(self.history) >= 4)  # 至少走2回合才允许求和
        # 悔棋：对弈模式+非思考+有历史才可用
        self.btn_undo.setEnabled(in_play and not self._thinking and bool(self.history))

    # ----------------- 训练模式 -----------------
    def start_training_default(self):
        self.mode = self.TRAIN_MODE
        # 恢复上次训练到的题目
        saved = self.progress.get_current("endgame")
        if saved:
            for i in range(self.cmb_puzzle.count()):
                if self.cmb_puzzle.itemData(i) == saved:
                    self.cmb_puzzle.setCurrentIndex(i)
                    break
        idx = self.cmb_puzzle.currentIndex()
        self._load_puzzle(self.cmb_puzzle.itemData(idx))

    def _on_puzzle_selected(self, _idx):
        if self.mode != self.TRAIN_MODE:
            self.mode = self.TRAIN_MODE
        pid = self.cmb_puzzle.currentData()
        if pid:
            self._load_puzzle(pid)

    def _load_puzzle(self, pid: str):
        from ..training.trainer import get_puzzle
        puzzle = get_puzzle(pid)
        self.progress.set_current("endgame", pid)   # 保存训练进度
        self.mode = self.TRAIN_MODE
        self._reset_demo_state()
        self.opening_session = None
        self.midgame_session = None
        self.sf_session = None
        self.session = TrainingSession(puzzle, self.progress)
        self.board = Board.from_fen(puzzle.fen)
        self.history.clear()
        self._refresh_move_list()
        self.play_box.setVisible(False)
        self.train_box.setVisible(True)
        self.opening_box.setVisible(False)
        self.midgame_box.setVisible(False)
        self.tactics_box.setVisible(False)
        self.player_color = Color.RED
        self.board_widget.set_player_color(Color.RED)
        self.board_widget.set_interactive(True)
        self.board_widget.flip(False)
        self.board_widget.set_board(self.board)
        self.lbl_puzzle.setText(f"<b>{puzzle.title}</b><br>"
                                f"<span style='color:#888'>{puzzle.category} · 难度{'★'*puzzle.difficulty}</span><br>"
                                f"<b>目标：</b>{puzzle.goal}")
        self._refresh_progress()
        self.status.showMessage(f"残局训练：{puzzle.title} — {puzzle.goal}", 5000)

    def _refresh_progress(self):
        total = len(all_puzzles())
        solved = sum(1 for p in all_puzzles() if self.progress.is_solved(p.id))
        self.lbl_progress.setText(f"通关进度：{solved}/{total} 题")
        if self.session:
            mark = "✓ 已通关" if self.progress.is_solved(self.session.puzzle.id) else ""
            self.lbl_progress.setText(self.lbl_progress.text() + f"  |  本题{mark}")
        due = self.progress.due_count({p.id for p in all_puzzles()})
        if due:
            self.lbl_progress.setText(self.lbl_progress.text() + f"  |  📌 错题到期 {due}")

    def _training_hint(self):
        if not self.session:
            return
        self.status.showMessage(self.session.hint(), 8000)

    def _training_show_solution(self):
        if not self.session:
            return
        self._show_full_mate_line(self.session, "残局")

    def _show_full_mate_line(self, session, tag: str):
        """后台 QThread 求完整杀法线，finished 信号直连主窗口方法（无 lambda/闭包，避免 C++ 层生命周期崩溃）。"""
        if self._mateline_thread and self._mateline_thread.isRunning():
            self.statusBar().showMessage("上一条杀法线正在计算，请稍候…", 3000)
            return

        self._mateline_tag = tag
        self.statusBar().showMessage("引擎正在计算完整杀法线，请稍候…", 30000)

        self._cleanup_mateline()

        self._mateline_worker = _MateLineWorker(self.engine, session)
        self._mateline_thread = QThread(self)
        self._mateline_worker.moveToThread(self._mateline_thread)
        self._mateline_thread.started.connect(self._mateline_worker.run)
        # 信号直连实例方法，避免 lambda 闭包捕获中间对象导致 C++ 层生命周期竞争闪退
        self._mateline_worker.finished.connect(self._on_mateline_finished)
        self._mateline_thread.start()

    def _on_mateline_finished(self, steps, verified, err):
        """主线程处理杀法线结果；先 quit 线程（同步等待），再弹窗，最后清理资源。"""
        if self._mateline_thread is not None:
            self._mateline_thread.quit()
            self._mateline_thread.wait(3000)
        self._after_mate_line(steps, verified, getattr(self, "_mateline_tag", ""), err)
        self._cleanup_mateline()

    def _cleanup_mateline(self):
        """安全清理 worker/thread（均保留在主线程中 delete，跨线程不触发 C++ 析构竞争）。"""
        if self._mateline_thread is not None:
            try:
                self._mateline_thread.quit()
                self._mateline_thread.wait(2000)
            except Exception:
                pass
            self._mateline_thread.setParent(None)
            self._mateline_thread.deleteLater()
            self._mateline_thread = None
        if self._mateline_worker is not None:
            try:
                self._mateline_worker.setParent(None)
            except Exception:
                pass
            self._mateline_worker.deleteLater()
            self._mateline_worker = None

    def _after_mate_line(self, steps, verified, tag: str, err: str = ""):
        self.statusBar().clearMessage()
        if err:
            QMessageBox.warning(self, "完整杀法", f"引擎求解异常：{err}")
            return
        if not steps:
            QMessageBox.information(
                self, "完整杀法", "引擎暂时未能求得完整杀法线，请用「💡 提示」分步引导。")
            return
        rows = []
        for i, s in enumerate(steps, 1):
            coord = (f"<span style='font-size:12px;color:#999;'>"
                     f"（{s['ucci'][:2]}→{s['ucci'][2:]}）</span>")
            if s["side"] == "红":
                rows.append(
                    f"<span style='font-size:16px;font-weight:bold;color:#C0392B;'>"
                    f"{i}. {s['cn']}</span>{coord}")
            else:
                rows.append(
                    f"<span style='font-size:14px;color:#666;'>"
                    f"　　… 黑方应 {s['cn']}</span>{coord}")
        tail = ("✔ 已验证：最后一招后黑方无路可走，将杀成立！"
                if verified else
                "（引擎当前难度下的参考线，可照此思路在棋盘上推演）")
        html = ("<div style='line-height:1.9;'>" + "<br>".join(rows)
                + f"<br><span style='font-size:13px;font-weight:bold;"
                f"color:#2E7D32;'>{tail}</span></div>")
        QMessageBox.information(self, f"完整杀法 · 共 {len(steps)} 步", html)

    def _sf_show_solution(self):
        if not self.sf_session:
            return
        self._show_full_mate_line(self.sf_session, "基本杀法")

    def _training_reset(self):
        if not self.session:
            return
        self.session.reset()
        self.board = Board.from_fen(self.session.puzzle.fen)
        self.history.clear()
        self._refresh_move_list()
        self.board_widget.set_interactive(True)
        self.board_widget.set_board(self.board)
        self.status.showMessage("本题已重置", 3000)

    def _training_prev(self):
        i = self.cmb_puzzle.currentIndex()
        if i > 0:
            self.cmb_puzzle.setCurrentIndex(i - 1)

    def _training_next(self):
        i = self.cmb_puzzle.currentIndex()
        if i < self.cmb_puzzle.count() - 1:
            self.cmb_puzzle.setCurrentIndex(i + 1)

    # ----------------- 开局训练模式 -----------------
    def _current_source(self) -> str:
        """当前选中的开局库来源：builtin / bin / obk。"""
        return self.cmb_source.currentData() if hasattr(self, "cmb_source") else "builtin"

    def _on_source_changed(self, _idx):
        """切换开局库来源：课程列表只显示当前库，不与其它库混排。"""
        self._refresh_cmb_lesson()

    def _active_lesson_combo(self) -> QComboBox:
        """当前课程下拉框。"""
        return self.cmb_lesson

    def start_opening_default(self):
        self.mode = self.OPENING_MODE
        cmb = self._active_lesson_combo()
        # 恢复上次训练到的课程（在所有 source 中找）
        saved = self.progress.get_current("opening")
        if saved:
            for i in range(cmb.count()):
                if cmb.itemData(i) == saved:
                    cmb.setCurrentIndex(i)
                    break
        self._load_lesson(cmb.itemData(cmb.currentIndex()))

    def _on_lesson_selected(self, _idx):
        if self.mode != self.OPENING_MODE:
            self.mode = self.OPENING_MODE
        cmb = self.sender() if isinstance(self.sender(), QComboBox) else self._active_lesson_combo()
        lid = cmb.currentData()
        if lid:
            self._lesson_cur[self._current_source()] = lid
            self._load_lesson(lid)

    def _load_lesson(self, lid: str):
        if not lid:
            self.status.showMessage("该分组暂无课程，请先导入 .bin/.obk 开局书", 3000)
            return
        from ..training.opening_book import lesson_by_id
        lesson = lesson_by_id(lid)
        self.progress.set_current("opening", lid)   # 保存训练进度
        self.mode = self.OPENING_MODE
        self.session = None
        self._reset_demo_state()
        self.midgame_session = None
        self.sf_session = None
        self.opening_session = OpeningSession(lesson, self.progress)
        self.board = self.opening_session.board
        self.history.clear()
        self._refresh_move_list()
        self.play_box.setVisible(False)
        self.train_box.setVisible(False)
        self.opening_box.setVisible(True)
        self.midgame_box.setVisible(False)
        self.tactics_box.setVisible(False)
        side_cn = "红" if lesson.side is Color.RED else "黑"
        self.player_color = lesson.side
        self.board_widget.set_player_color(lesson.side)
        self.board_widget.set_interactive(True)
        self.board_widget.flip(False)
        self.board_widget.set_board(self.board)
        self.lbl_opening.setText(
            f"<b>{lesson.title}</b><br>"
            f"<span style='color:#888'>{lesson.category} · 难度{'★' * lesson.difficulty} · 你执{side_cn}方</span><br>"
            f"{lesson.intro.replace(chr(10), '<br>')}")
        self.lbl_coach.setText("（教练在此讲解每一步…）")
        self._refresh_opening_progress()
        self.status.showMessage(f"开局训练：{lesson.title} — 你执{side_cn}方", 5000)
        # 用户执黑时教练先走
        if not self.opening_session.is_user_turn():
            QTimer.singleShot(700, self._coach_first_move)

    def _coach_first_move(self):
        """教练先手（用户执黑的课程）。"""
        s = self.opening_session
        if not s or s.finished or s.is_user_turn() or self.mode != self.OPENING_MODE:
            return
        res = s.coach_play_next()
        if not res:
            return
        self.board = s.board
        frm, to = res.coach_reply
        self.history.append((frm, to, None, move_to_ucci(frm, to), res.coach_reply_cn))
        self._refresh_move_list()
        self.board_widget.set_board(self.board, animate_last=(frm, to),
                                    last_move_by=self.player_color.opponent)
        self.lbl_coach.setText(f"教练走 {res.coach_reply_cn}：{res.coach_comment}")
        if res.finished:
            self._opening_finished()

    def _handle_opening_move(self, frm: Pos, to: Pos):
        s = self.opening_session
        if not s or s.finished or self._demo is not None:
            return
        if not s.is_user_turn():
            return
        result = s.submit_move(frm, to)
        if result.verdict is OpeningVerdict.ILLEGAL:
            self.status.showMessage(result.message, 3000)
            return
        if result.verdict is OpeningVerdict.WRONG:
            sound.play("wrong")
            self.status.showMessage(result.message, 8000)
            QMessageBox.information(self, "偏离正着", result.message)
            return
        # 正确：同步棋盘与记录
        sound.play("move")
        self.board = s.board
        self.history.append((frm, to, None, move_to_ucci(frm, to), result.user_move_cn))
        text = f"你走 {result.user_move_cn}：{result.message}"
        if result.coach_reply:
            bfrm, bto = result.coach_reply
            self.history.append((bfrm, bto, None, move_to_ucci(bfrm, bto),
                                 result.coach_reply_cn))
            text += f"\n\n教练走 {result.coach_reply_cn}：{result.coach_comment}"
        self._refresh_move_list()
        # 若教练有应手，标注对手应手步（红色）；否则标注用户步（橙色）
        if result.coach_reply:
            bfrm, bto = result.coach_reply
            self.board_widget.set_board(self.board, animate_last=(bfrm, bto),
                                        last_move_by=self.player_color.opponent)
        else:
            self.board_widget.set_board(self.board, animate_last=(frm, to),
                                        last_move_by=self.player_color)
        self.lbl_coach.setText(text)
        if result.finished:
            self._opening_finished()
            return
        # 该正着若配有偏着演示，亮出按钮（知其所以然）
        pit = s.pitfall_for(move_to_ucci(frm, to))
        self._current_pitfall = (move_to_ucci(frm, to), pit) if pit else None
        self.btn_o_pitfall.setVisible(pit is not None)
        if pit is not None:
            self.btn_o_pitfall.setText("⚠ 看看不走这步会怎样")

    def _opening_finished(self):
        lesson = self.opening_session.lesson
        self._refresh_opening_progress()
        self.board_widget.set_interactive(False)
        sound.play("win")
        self.plan_store.record("opening")   # 每日计划：开局课 +1
        QMessageBox.information(self, "本课完成 🎓",
                                f"恭喜完成「{lesson.title}」！\n\n{lesson.summary}")

    def _opening_hint(self):
        s = self.opening_session
        if not s or s.finished:
            return
        cn = s.suggest_chinese()
        if not cn:
            self.lbl_coach.setText("当前没有需要走的正着（本课已完成或轮空）。")
            return
        self.lbl_coach.setText(
            f"<span style='font-size:16px;font-weight:bold;color:#C0392B;'>💡 正着参考：{cn}</span>"
            f"<span style='font-size:13px;color:#666;'>　（{s.expected_ucci()}）</span><br>"
            f"<span style='font-size:12px;color:#555;'>请自己把这步走出来，走完后教练会讲解这步的用意。</span>")

    def _opening_demo(self):
        """教练替用户演示当前正着。"""
        s = self.opening_session
        if not s or s.finished or not s.is_user_turn():
            return
        frm, to = parse_ucci_move(s.expected_ucci())
        self._handle_opening_move(frm, to)

    def _opening_reset(self):
        s = self.opening_session
        if not s:
            return
        self._reset_demo_state()
        s.reset()
        self.board = s.board
        self.history.clear()
        self._refresh_move_list()
        self.board_widget.set_interactive(True)
        self.board_widget.set_board(self.board)
        self.lbl_coach.setText("（本题已重置，教练在此讲解每一步…）")
        if not s.is_user_turn():
            QTimer.singleShot(700, self._coach_first_move)
        self.status.showMessage("本课已重置", 3000)

    def _refresh_opening_progress(self):
        total = len(all_lessons())
        solved = sum(1 for l in all_lessons() if self.progress.is_solved(l.id))
        self.lbl_opening_progress.setText(f"课程进度：{solved}/{total} 课")

    # ----------------- 中局战术训练模式 -----------------
    def start_midgame_default(self):
        self.mode = self.MIDGAME_MODE
        # 恢复上次训练到的战术题
        saved = self.progress.get_current("midgame")
        if saved:
            for i in range(self.cmb_tactic.count()):
                if self.cmb_tactic.itemData(i) == saved:
                    self.cmb_tactic.setCurrentIndex(i)
                    break
        idx = self.cmb_tactic.currentIndex()
        self._load_tactic(self.cmb_tactic.itemData(idx))

    def _on_tactic_selected(self, _idx):
        if self.mode != self.MIDGAME_MODE:
            self.mode = self.MIDGAME_MODE
        tid = self.cmb_tactic.currentData()
        if tid:
            self._load_tactic(tid)

    def _load_tactic(self, tid: str):
        from ..training.midgame_puzzles import tactic_by_id
        tactic = tactic_by_id(tid)
        self.progress.set_current("midgame", tid)   # 保存训练进度
        self.mode = self.MIDGAME_MODE
        self.session = None
        self._reset_demo_state()
        self.opening_session = None
        self.sf_session = None
        self.midgame_session = MidgameSession(tactic, self.progress)
        self.board = self.midgame_session.board
        self.history.clear()
        self._refresh_move_list()
        self.play_box.setVisible(False)
        self.train_box.setVisible(False)
        self.opening_box.setVisible(False)
        self.midgame_box.setVisible(True)
        self.tactics_box.setVisible(False)
        side_cn = "红" if tactic.side is Color.RED else "黑"
        self.player_color = tactic.side
        self.board_widget.set_player_color(tactic.side)
        self.board_widget.set_interactive(True)
        self.board_widget.flip(False)
        self.board_widget.set_board(self.board)
        self.lbl_midgame.setText(
            f"<b>{tactic.title}</b><br>"
            f"<span style='color:#888'>{tactic.category} · 难度{'★' * tactic.difficulty} · 你执{side_cn}方</span><br>"
            f"<b>目标：</b>{tactic.goal}")
        self.lbl_mid_coach.setText("（教练在此讲解每一步…）")
        self._refresh_midgame_progress()
        self.status.showMessage(f"中局战术：{tactic.title} — {tactic.goal}", 5000)

    def _handle_midgame_move(self, frm: Pos, to: Pos):
        s = self.midgame_session
        if not s or s.finished or self._demo is not None:
            return
        if not s.is_user_turn():
            return
        result = s.submit_move(frm, to)
        if result.verdict is TacticVerdict.ILLEGAL:
            self.status.showMessage(result.message, 3000)
            return
        if result.verdict is TacticVerdict.WRONG:
            sound.play("wrong")
            self.progress.record_wrong(s.tactic.id)   # 错题本
            self.status.showMessage(result.message, 8000)
            QMessageBox.information(self, "再想想", result.message)
            return
        # 正确：同步棋盘与记录
        sound.play("move")
        self.board = s.board
        self.history.append((frm, to, None, move_to_ucci(frm, to), result.user_move_cn))
        text = f"你走 {result.user_move_cn}：{result.message}"
        if result.reply:
            bfrm, bto = result.reply
            self.history.append((bfrm, bto, None, move_to_ucci(bfrm, bto),
                                 result.reply_cn))
            text += f"\n\n对手走 {result.reply_cn}：{result.reply_comment}"
        self._refresh_move_list()
        # 若对手有应手，标注对手应手步（红色）；否则标注用户步（橙色）
        if result.reply:
            bfrm, bto = result.reply
            self.board_widget.set_board(self.board, animate_last=(bfrm, bto),
                                        last_move_by=self.player_color.opponent)
        else:
            self.board_widget.set_board(self.board, animate_last=(frm, to),
                                        last_move_by=self.player_color)
        self.lbl_mid_coach.setText(text)
        if result.finished:
            self._midgame_finished()
            return
        # 该正着若配有偏着演示，亮出按钮（知其所以然）
        pit = s.pitfall_for(move_to_ucci(frm, to))
        self._current_pitfall = (move_to_ucci(frm, to), pit) if pit else None
        self.btn_m_pitfall.setVisible(pit is not None)

    def _midgame_finished(self):
        tactic = self.midgame_session.tactic
        self.progress.record_right(tactic.id)   # 间隔重复：通关推进
        self._refresh_midgame_progress()
        self.board_widget.set_interactive(False)
        sound.play("win")
        self.plan_store.record("midgame")   # 每日计划：中局 +1
        QMessageBox.information(self, "战术达成 🎉",
                                f"漂亮！「{tactic.title}」战术达成！\n\n{tactic.explanation}")

    def _midgame_hint(self):
        s = self.midgame_session
        if not s or s.finished:
            return
        self.lbl_mid_coach.setText(
            f"<span style='font-size:16px;font-weight:bold;color:#C0392B;'>💡 提示</span><br>"
            f"<span style='font-size:14px;'>{s.hint()}</span>")

    def _midgame_demo(self):
        """教练替用户演示当前正着。"""
        s = self.midgame_session
        if not s or s.finished or not s.is_user_turn():
            return
        frm, to = parse_ucci_move(s.expected_ucci())
        self._handle_midgame_move(frm, to)

    def _midgame_reset(self):
        s = self.midgame_session
        if not s:
            return
        self._reset_demo_state()
        s.reset()
        self.board = s.board
        self.history.clear()
        self._refresh_move_list()
        self.board_widget.set_interactive(True)
        self.board_widget.set_board(s.board)
        self.lbl_mid_coach.setText("（本题已重置，教练在此讲解每一步…）")
        self.status.showMessage("本题已重置", 3000)

    def _midgame_prev(self):
        i = self.cmb_tactic.currentIndex()
        if i > 0:
            self.cmb_tactic.setCurrentIndex(i - 1)

    def _midgame_next(self):
        i = self.cmb_tactic.currentIndex()
        if i < self.cmb_tactic.count() - 1:
            self.cmb_tactic.setCurrentIndex(i + 1)

    def _refresh_midgame_progress(self):
        total = len(all_tactics())
        solved = sum(1 for t in all_tactics() if self.progress.is_solved(t.id))
        self.lbl_midgame_progress.setText(f"战术进度：{solved}/{total} 题")
        due = self.progress.due_count({t.id for t in all_tactics()})
        if due:
            self.lbl_midgame_progress.setText(
                self.lbl_midgame_progress.text() + f"  |  📌 错题到期 {due}")

    # ----------------- 基本杀法训练模式 -----------------
    def start_sf_default(self):
        self.mode = self.TACTICS_MODE
        # 恢复上次训练到的杀法题
        saved = self.progress.get_current("tactics")
        if saved:
            for i in range(self.cmb_sf.count()):
                if self.cmb_sf.itemData(i) == saved:
                    self.cmb_sf.setCurrentIndex(i)
                    break
        idx = self.cmb_sf.currentIndex()
        self._load_sf_puzzle(self.cmb_sf.itemData(idx))

    def _on_sf_selected(self, _idx):
        if self.mode != self.TACTICS_MODE:
            self.mode = self.TACTICS_MODE
        pid = self.cmb_sf.currentData()
        if pid:
            self._load_sf_puzzle(pid)

    def _load_sf_puzzle(self, pid: str):
        from ..training.tactics_puzzles import tactics_puzzle_by_id
        puzzle = tactics_puzzle_by_id(pid)
        self.progress.set_current("tactics", pid)   # 保存训练进度
        self.mode = self.TACTICS_MODE
        self._reset_demo_state()
        self.session = None
        self.opening_session = None
        self.midgame_session = None
        self.sf_session = TrainingSession(puzzle, self.progress)
        self.board = Board.from_fen(puzzle.fen)
        self.history.clear()
        self._refresh_move_list()
        self.play_box.setVisible(False)
        self.train_box.setVisible(False)
        self.opening_box.setVisible(False)
        self.midgame_box.setVisible(False)
        self.tactics_box.setVisible(True)
        self.player_color = Color.RED
        self.board_widget.set_player_color(Color.RED)
        self.board_widget.set_interactive(True)
        self.board_widget.flip(False)
        self.board_widget.set_board(self.board)
        self.lbl_sf.setText(f"<b>{puzzle.title}</b><br>"
                            f"<span style='color:#888'>{puzzle.category} · 难度{'★'*puzzle.difficulty}</span><br>"
                            f"<b>目标：</b>{puzzle.goal}")
        self._refresh_sf_progress()
        self.status.showMessage(f"基本杀法：{puzzle.title} — {puzzle.goal}", 5000)

    def _handle_sf_move(self, frm: Pos, to: Pos):
        if not self.sf_session or self.sf_session.finished:
            return
        result = self.sf_session.submit_move(frm, to)
        if result.verdict == MoveVerdict.ILLEGAL:
            self.status.showMessage(result.message, 3000)
            return
        if result.verdict == MoveVerdict.WRONG:
            sound.play("wrong")
            self.progress.record_wrong(self.sf_session.puzzle.id)   # 错题本
            self.status.showMessage(result.message, 4000)
            QMessageBox.information(self, "再想想", result.message + "\n\n提示：" + self.sf_session.puzzle.hint)
            return
        # 正确（含将杀）：同步棋盘
        sound.play("move")
        self.board = self.sf_session.board
        self.history.append((frm, to, None, move_to_ucci(frm, to), result.user_move_cn))
        if result.black_reply:
            bfrom, bto = parse_ucci_move(result.black_reply)
            self.history.append((bfrom, bto, None, result.black_reply, result.black_reply_cn))
        self._refresh_move_list()
        if result.black_reply:
            bfrom, bto = parse_ucci_move(result.black_reply)
            self.board_widget.set_board(self.board, animate_last=(bfrom, bto),
                                        last_move_by=self.player_color.opponent)
        else:
            self.board_widget.set_board(self.board, animate_last=(frm, to),
                                        last_move_by=self.player_color)
        if result.verdict == MoveVerdict.SOLVED:
            sound.play("win")
            self.plan_store.record("tactics")   # 每日计划：杀法 +1
            self.progress.record_right(self.sf_session.puzzle.id)   # 间隔重复：通关推进
            QMessageBox.information(self, "通关 🎉",
                                    result.message + "\n\n" + self.sf_session.puzzle.explanation)
            self._refresh_sf_progress()
            self.board_widget.set_interactive(False)
        else:
            self.status.showMessage(result.message + (" 黑方应：" + result.black_reply if result.black_reply else ""), 5000)
            self._refresh_sf_progress()

    def _sf_hint(self):
        if not self.sf_session:
            return
        self.status.showMessage(self.sf_session.hint(), 8000)

    def _sf_reset(self):
        if not self.sf_session:
            return
        self.sf_session.reset()
        self.board = Board.from_fen(self.sf_session.puzzle.fen)
        self.history.clear()
        self._refresh_move_list()
        self.board_widget.set_interactive(True)
        self.board_widget.set_board(self.board)
        self.status.showMessage("本题已重置", 3000)

    def _sf_prev(self):
        i = self.cmb_sf.currentIndex()
        if i > 0:
            self.cmb_sf.setCurrentIndex(i - 1)

    def _sf_next(self):
        i = self.cmb_sf.currentIndex()
        if i < self.cmb_sf.count() - 1:
            self.cmb_sf.setCurrentIndex(i + 1)

    def _refresh_sf_progress(self):
        total = len(all_tactics_puzzles())
        solved = sum(1 for p in all_tactics_puzzles() if self.progress.is_solved(p.id))
        self.lbl_sf_progress.setText(f"杀法进度：{solved}/{total} 题")
        if self.sf_session:
            mark = "✓ 已通关" if self.progress.is_solved(self.sf_session.puzzle.id) else ""
            self.lbl_sf_progress.setText(self.lbl_sf_progress.text() + f"  |  本题{mark}")
        due = self.progress.due_count({p.id for p in all_tactics_puzzles()})
        if due:
            self.lbl_sf_progress.setText(self.lbl_sf_progress.text() + f"  |  📌 错题到期 {due}")

    # ----------------- 每日训练计划 -----------------
    def _show_plan_dialog(self):
        """每日训练计划弹窗：今日任务表（自动跟踪+手动打卡）+ 连续天数 + 训练心法。"""
        from PySide6.QtWidgets import (QDialog, QProgressBar, QHBoxLayout,
                                     QScrollArea, QTextBrowser, QWidget as QW)
        from datetime import date as _date

        store = self.plan_store
        counts = store.today_counts()
        streak = store.streak()

        dlg = QDialog(self)
        dlg.setWindowTitle("📅 每日训练计划")
        dlg.resize(560, 640)
        lay = QVBoxLayout(dlg)

        # 顶部：日期 + 连续打卡
        weekday_cn = "一二三四五六日"[_date.today().weekday()]
        head = QLabel(f"<span style='font-size:16px;font-weight:bold;'>"
                      f"📅 {_date.today().isoformat()}（周{weekday_cn}）</span>"
                      f"<span style='font-size:15px;color:#E67E22;font-weight:bold;'>"
                      f"　🔥 连续达标 {streak} 天</span>"
                      f"<span style='font-size:12px;color:#888;'>　（每日完成 ≥3 类任务即达标）</span>")
        head.setWordWrap(True)
        lay.addWidget(head)

        # 中部：任务列表（滚动区）
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QW()
        il = QVBoxLayout(inner)
        il.setSpacing(8)
        for key, title, target, auto in DAILY_TASKS:
            row = QHBoxLayout()
            done = min(counts.get(key, 0), target)
            ok = counts.get(key, 0) >= target
            icon = "✅" if ok else "⬜"
            lbl = QLabel(f"{icon} <b>{title}</b>")
            lbl.setWordWrap(True)
            lbl.setMinimumWidth(250)
            row.addWidget(lbl, 2)
            bar = QProgressBar()
            bar.setRange(0, target)
            bar.setValue(done)
            bar.setFormat(f"%v / %m")
            bar.setStyleSheet("""
                QProgressBar { border:1px solid #CCC; border-radius:4px;
                               background:#F5F5F5; text-align:center;
                               font-size:12px; height:18px; }
                QProgressBar::chunk { background:#4CAF50; border-radius:3px; }
            """)
            row.addWidget(bar, 1)
            if not auto:
                btn = QPushButton("✓ 打卡")
                btn.setToolTip("完成一次手动打卡")
                btn.clicked.connect(lambda _=False, k=key, d=dlg: (self.plan_store.manual_check(k), self._refresh_plan_dialog(d)))
                row.addWidget(btn)
            else:
                tag = QLabel("<span style='color:#888;font-size:11px;'>自动</span>")
                row.addWidget(tag)
            il.addLayout(row)
        il.addStretch(1)
        scroll.setWidget(inner)
        lay.addWidget(scroll, 2)

        # 底部：训练心法（高效涨棋路径）
        tips = QTextBrowser()
        tips.setHtml("""
<div style='line-height:1.65;font-size:13px;'>
<b style='font-size:14px;color:#8E44AD;'>📐 高效涨棋路径（按优先级）</b>
<p><b>① 杀法基本功</b>（每天15分钟）：先在脑中算完整条杀法线再落子，不要试错式走棋。
达标：一步杀3秒看出，三步杀1分钟算穿。</p>
<p><b>② 残局定式</b>（每天15分钟）：残局是「算力健身房」，子力少最适合练精确计算。
顺带记口诀原理（残棋炮归家、马不蹈边…）。</p>
<p><b>③ 实战 + 复盘</b>（涨棋最快的一环）：走每步前强制算3个回合，先猜对方最强应招再走。
下完点「对方意」验证你的猜测；输棋找引擎评估分骤降的那一步，那就是败着。</p>
<p><b>④ 开局体系</b>（够用就好）：先手精通一套中炮，后手精通一套屏风马。
目标：前10步不吃亏、阵型舒展，理解每步用意比背谱重要。</p>
<b style='font-size:14px;color:#C0392B;'>💡 三条心法</b>
<p><b>算力 &gt; 记忆</b>：坚持「先算后动」，算力是唯一随你上涨的能力。<br>
<b>复盘 &gt; 数量</b>：1盘认真复盘 &gt; 10盘随手下；引擎评估分就是你免费的私人教练。<br>
<b>规律 &gt; 灵感</b>：走错的题隔天重做，直到形成条件反射。</p>
</div>""")
        tips.setMaximumHeight(280)
        lay.addWidget(tips, 1)

        dlg.exec()

    def _refresh_plan_dialog(self, dlg):
        """打卡后刷新计划弹窗（关旧开新，保证进度条/按钮状态一致）。"""
        dlg.accept()
        self._show_plan_dialog()

    # ----------------- 思维口诀查看面板 -----------------
    def _show_mindset_dialog(self):
        """弹出象棋思维口诀与逻辑查看面板，按四大阶段分类展示。"""
        from PySide6.QtWidgets import QDialog, QTextBrowser
        from PySide6.QtCore import QSize
        dlg = QDialog(self)
        dlg.setWindowTitle("🧠 象棋思维口诀与逻辑")
        dlg.resize(620, 660)
        lay = QVBoxLayout(dlg)
        browser = QTextBrowser()
        cat_desc = {
            "开局原理": "前10步：快速出动大子、抢占要道、控制中路、两翼均衡",
            "中局战术": "10-30步：捉双、闪击、牵制、引离、兑子、先手",
            "残局原则": "30步后：老将助攻、过河兵、士象全、算度精确",
            "杀法要诀": "照将·封路·断援——炮架、马步、飞将、困毙",
        }
        # 字体大小按缩放系数动态计算；全屏时放大以充分利用空间
        font_scale = {"v": 1.0}

        def _render_html(scale: float):
            fs_title = int(18 * scale)       # 卡片标题
            fs_mnemonic = int(22 * scale)    # 口诀正文（最醒目）
            fs_explain = int(16 * scale)     # 讲解
            fs_apply = int(15 * scale)       # 实战
            fs_cat_sub = int(13 * scale)     # 分类描述
            pad = int(12 * scale)
            margin_v = int(10 * scale)
            indent = int(16 * scale)
            parts = ["<html><body style='font-family:微软雅黑;line-height:1.6;'>"]
            for cat in mindset_categories():
                items = mindsets_by_category(cat)
                parts.append(
                    f"<h2 style='color:#8B4513;border-bottom:2px solid #D7CCC8;"
                    f"padding-bottom:6px;margin-top:{margin_v+8}px;font-size:{int(22*scale)}px;'>"
                    f"{cat} <span style='font-size:{fs_cat_sub}px;color:#888;font-weight:normal;'>"
                    f"（{cat_desc.get(cat, '')}）</span></h2>")
                for i, m in enumerate(items, 1):
                    parts.append(
                        f"<div style='background:#FBF6E9;border:1px solid #E0D4B0;border-radius:8px;"
                        f"padding:{pad}px;margin:{margin_v}px 0;'>"
                        f"<p style='margin:4px 0;'><b style='color:#333;font-size:{fs_title}px;'>{i}. {m.title}</b></p>"
                        f"<p style='margin:6px 0 6px {indent}px;font-size:{fs_mnemonic}px;color:#C0392B;font-weight:bold;'>📖 {m.mnemonic}</p>"
                        f"<p style='margin:6px 0 6px {indent}px;font-size:{fs_explain}px;color:#333;'>{m.explanation}</p>"
                        f"<p style='margin:6px 0 2px {indent}px;font-size:{fs_apply}px;color:#1565C0;'>🎯 实战：{m.application}</p>"
                        f"</div>")
            parts.append("</body></html>")
            browser.setHtml("".join(parts))

        _render_html(font_scale["v"])
        lay.addWidget(browser, 1)
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)

        # 全屏/还原切换按钮：切换时同步放大字体
        btn_fullscreen = QPushButton("⛶ 全屏")
        is_maximized = {"v": False}
        normal_size = {"v": None}

        def _toggle_fullscreen():
            if not is_maximized["v"]:
                # 进入全屏：保存当前尺寸 → 最大化 → 放大字体 → 按钮变"还原"
                normal_size["v"] = dlg.size()
                dlg.showMaximized()
                font_scale["v"] = 1.35
                _render_html(font_scale["v"])
                btn_fullscreen.setText("🗗 还原")
                is_maximized["v"] = True
            else:
                # 还原：恢复原尺寸 → 字体恢复 → 按钮变"全屏"
                dlg.showNormal()
                if normal_size["v"] is not None:
                    dlg.resize(normal_size["v"])
                font_scale["v"] = 1.0
                _render_html(font_scale["v"])
                btn_fullscreen.setText("⛶ 全屏")
                is_maximized["v"] = False

        btn_fullscreen.setToolTip("全屏查看口诀（字体同步放大，再次点击还原）")
        btn_fullscreen.clicked.connect(_toggle_fullscreen)
        btn_row.addWidget(btn_fullscreen)

        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(dlg.accept)
        btn_row.addWidget(btn_close)
        lay.addLayout(btn_row)
        dlg.exec()

    # ----------------- 偏着惩罚演示（知其所以然） -----------------
    def _active_session(self):
        """当前训练会话（开局或中局），供演示机制统一使用。"""
        if self.mode == self.MIDGAME_MODE:
            return self.midgame_session
        return self.opening_session

    def _coach_label(self) -> QLabel:
        return self.lbl_mid_coach if self.mode == self.MIDGAME_MODE else self.lbl_coach

    def _pitfall_button(self) -> QPushButton:
        return self.btn_m_pitfall if self.mode == self.MIDGAME_MODE else self.btn_o_pitfall

    def _reset_demo_state(self):
        """清理演示状态（切课/重置/切模式时调用）。"""
        self._demo = None
        self._current_pitfall = None
        if hasattr(self, "btn_o_pitfall"):
            self.btn_o_pitfall.setVisible(False)
            self.btn_o_pitfall.setText("⚠ 看看不走这步会怎样")
            self.btn_o_demo.setEnabled(True)
            self.btn_o_reset.setEnabled(True)
            self.btn_o_hint.setEnabled(True)
        if hasattr(self, "btn_m_pitfall"):
            self.btn_m_pitfall.setVisible(False)
            self.btn_m_pitfall.setText("⚠ 看看不走这步会怎样")
            self.btn_m_demo.setEnabled(True)
            self.btn_m_reset.setEnabled(True)
            self.btn_m_hint.setEnabled(True)

    def _on_pitfall_clicked(self):
        if self._demo is not None:
            self._exit_pitfall()
        else:
            self._show_pitfall()

    def _show_pitfall(self):
        s = self._active_session()
        if not s or self._demo is not None or not self._current_pitfall:
            return
        ucci, pit = self._current_pitfall
        start = s.pitfall_start_board(ucci)
        if start is None:
            return
        self._demo = {"board": start,
                      "queue": [parse_ucci_move(u) for u in pit.line],
                      "pit": pit, "step": 0}
        self.board_widget.set_interactive(False)
        self._pitfall_button().setText("↩ 返回训练")
        self._set_demo_buttons_enabled(False)
        self.status.showMessage(f"偏着演示：{pit.title}", 5000)
        QTimer.singleShot(600, self._demo_step)

    def _set_demo_buttons_enabled(self, on: bool):
        for btn in (self.btn_o_demo, self.btn_o_reset, self.btn_o_hint,
                    self.btn_m_demo, self.btn_m_reset, self.btn_m_hint):
            btn.setEnabled(on)

    def _demo_step(self):
        d = self._demo
        if d is None or self.mode not in (self.OPENING_MODE, self.MIDGAME_MODE):
            return
        pit, s = d["pit"], self._active_session()
        if d["step"] < len(d["queue"]):
            frm, to = d["queue"][d["step"]]
            cn = move_to_chinese(d["board"], frm, to)
            mover_side = d["board"].side  # make_move 前的走子方
            mover = "你" if mover_side is s.user_side() else "对手"
            d["board"].make_move(frm, to)
            self.board_widget.set_board(d["board"], animate_last=(frm, to),
                                        last_move_by=mover_side)
            comment = pit.comments.get(d["step"], "")
            self._coach_label().setText(f"【{pit.title}】\n{mover}走 {cn}\n{comment}")
            d["step"] += 1
            QTimer.singleShot(1400, self._demo_step)
        else:
            self._coach_label().setText(f"【{pit.title}】· 结论\n{pit.conclusion}")
            self._pitfall_button().setText("↩ 返回训练")
            self.status.showMessage("演示完毕——点击『返回训练』继续", 6000)

    def _exit_pitfall(self):
        self._demo = None
        s = self._active_session()
        if s:
            self.board_widget.set_board(s.board)
            self.board_widget.set_interactive(not s.finished)
        self._pitfall_button().setText("⚠ 看看不走这步会怎样")
        self._set_demo_buttons_enabled(True)
        self.status.showMessage("已返回训练，继续沿主线走棋", 3000)

    # ----------------- 用户落子处理 -----------------
    def _on_move_attempted(self, fr, fc, tr, tc):
        frm, to = Pos(fr, fc), Pos(tr, tc)
        if self.mode == self.PLAY_MODE:
            self._handle_play_move(frm, to)
        elif self.mode == self.TRAIN_MODE:
            self._handle_train_move(frm, to)
        elif self.mode == self.TACTICS_MODE:
            self._handle_sf_move(frm, to)
        elif self.mode == self.MIDGAME_MODE:
            self._handle_midgame_move(frm, to)
        else:
            self._handle_opening_move(frm, to)


    def _handle_play_move(self, frm: Pos, to: Pos):
        # —— 回合真实性校验（不再依赖 _thinking 标志） ——
        if self.board.side is not self.player_color:
            self.status.showMessage(
                f"💡 现在轮到{self.board.side.name_cn}方走（你执{self.player_color.name_cn}），稍等一下…", 2500)
            return
        legal = rules.is_legal(self.board, frm, to)
        if not legal:
            self.status.showMessage("不合规的走法", 2500)
            return
        self._apply_move(frm, to, animate=True)
        self._check_game_over_play()
        if not self._game_over:
            self._thinking = False
            self._request_engine_move()


    # ----------------- 错题本（间隔重复） -----------------
    def _review_wrong(self, cmb, loader, pids):
        """跳到最早到期的错题；无到期题时提示。loader 为该模式选中题的加载函数。"""
        pid = self.progress.pick_due_pid(set(pids))
        if pid is None:
            self.status.showMessage("📌 错题本：今天没有到期的错题，保持住！", 4000)
            return
        i = cmb.findData(pid)
        if i < 0:
            self.status.showMessage("📌 错题本：该题已不在列表中", 4000)
            return
        if cmb.currentIndex() == i:
            loader(i)      # 已在该题也强制重载
        else:
            cmb.setCurrentIndex(i)   # 信号触发 loader

    def _handle_train_move(self, frm: Pos, to: Pos):
        if not self.session or self.session.finished:
            return
        result = self.session.submit_move(frm, to)
        if result.verdict == MoveVerdict.ILLEGAL:
            self.status.showMessage(result.message, 3000)
            return
        if result.verdict == MoveVerdict.WRONG:
            sound.play("wrong")
            self.progress.record_wrong(self.session.puzzle.id)   # 错题本
            self.status.showMessage(result.message, 4000)
            # 训练中走错：可选弹窗讲解
            QMessageBox.information(self, "再想想", result.message + "\n\n提示：" + self.session.puzzle.hint)
            return
        # 正确（含将杀）：同步棋盘
        sound.play("move")
        self.board = self.session.board
        self.history.append((frm, to, None, move_to_ucci(frm, to), result.user_move_cn))
        if result.black_reply:
            bfrom, bto = parse_ucci_move(result.black_reply)
            self.history.append((bfrom, bto, None, result.black_reply,
                                 result.black_reply_cn))
        self._refresh_move_list()
        # 若黑方有应手，标注黑方应手步（红色）；否则标注用户步（橙色）
        if result.black_reply:
            bfrom, bto = parse_ucci_move(result.black_reply)
            self.board_widget.set_board(self.board, animate_last=(bfrom, bto),
                                        last_move_by=self.player_color.opponent)
        else:
            self.board_widget.set_board(self.board, animate_last=(frm, to),
                                        last_move_by=self.player_color)
        if result.verdict == MoveVerdict.SOLVED:
            sound.play("win")
            self.plan_store.record("endgame")   # 每日计划：残局 +1
            self.progress.record_right(self.session.puzzle.id)   # 间隔重复：通关推进
            QMessageBox.information(self, "通关 🎉",
                                    result.message + "\n\n" + self.session.puzzle.explanation)
            self._refresh_progress()
            self.board_widget.set_interactive(False)
        else:
            self.status.showMessage(result.message + (" 黑方应：" + result.black_reply if result.black_reply else ""), 5000)
            self._refresh_progress()

    # ----------------- 走子与引擎 -----------------
    def _apply_move(self, frm: Pos, to: Pos, animate: bool):
        cn = move_to_chinese(self.board, frm, to)
        mover = self.board.side  # make_move 前的走子方
        captured = self.board.make_move(frm, to)
        ucci = move_to_ucci(frm, to)
        self.history.append((frm, to, captured, ucci, cn))
        # 音效：将军 > 吃子 > 落子（越紧急越优先）
        if rules.is_in_check(self.board, mover.opponent):
            sound.play("check")
        elif captured is not None:
            sound.play("capture")
        else:
            sound.play("move")
        self._refresh_move_list()
        self.board_widget.set_board(self.board, animate_last=(frm, to) if animate else None,
                                    last_move_by=mover)
        self._update_turn_label()

    def _request_engine_move(self):
        if self._thinking:
            return
        self._thinking = True
        self._update_turn_label()
        self.board_widget.set_interactive(False)
        # 注意：这里不能用永久显示（第二参数不写=永久），否则解锁后会残留"引擎思考中…"
        self.status.showMessage("引擎思考中…", 3000)

        self._engine_thread = QThread()
        self._engine_worker = _EngineWorker(self.engine, self.board.clone())
        self._engine_worker.moveToThread(self._engine_thread)
        self._engine_thread.started.connect(self._engine_worker.run)
        self._engine_worker.finished.connect(self._on_engine_reply)
        self._engine_worker.finished.connect(self._engine_thread.quit)
        self._engine_thread.start()

        # 引擎 15 秒看门狗：Pikafish 异常卡死或信号丢了 → 强制把交互还给用户。
        def _wd():
            if getattr(self, "_thinking", False):
                self.status.showMessage("⏱ 引擎响应超时，已解锁棋盘。可点「🔄 新局」重开一局。", 6000)
                self._thinking = False
                self._force_sync_interactive()
        QTimer.singleShot(15000, _wd)

    def _on_engine_reply(self, move):
        self._thinking = False
        if move is None:
            self.status.showMessage("引擎无走法（可能已终局）", 4000)
            self._force_sync_interactive()
            return
        frm, to = move
        self._apply_move(frm, to, animate=True)
        self._check_game_over_play()
        if self._game_over:
            self.board_widget.set_interactive(False)
            self._update_turn_label()
            return

        # —— 解锁：三道"定时脉冲" + animFinished 兜底 ——
        # 不论动画结束信号是否丢、事件循环是否竞争，最终一定会把 interactive 调到正确值。
        # 脉冲一：动画刚好跑完的时刻（animFinished）立刻同步。
        try:
            handler_box = {"ref": None}

            def _on_anim_done():
                try:
                    self.board_widget.animFinished.disconnect(handler_box["ref"])
                except (TypeError, RuntimeError):
                    pass
                self._force_sync_interactive()
            handler_box["ref"] = _on_anim_done
            self.board_widget.animFinished.connect(handler_box["ref"])
        except Exception:
            pass
        # 脉冲二（关键！）：300/1000/2000 ms 各跑一次，保证无论前面信号链路是否丢了都能解锁。
        self._schedule_unlock_pulses()

    # ----------------- 教练建议（对弈） -----------------
    def _request_foe_intent(self):
        """点击「🔍 对方意图」：解析对方刚才那步为什么这么走。"""
        from ..board.pieces import PieceType
        if self.mode != self.PLAY_MODE:
            return
        if not self.history:
            self.lbl_suggest.setText("🔍 还没有走子记录，对方尚未行棋。")
            return
        # 当前轮到用户走 = 对方刚走完，最后一步是对方的
        if self.board.side is not self.player_color:
            self.lbl_suggest.setText("🔍 当前不是你的回合（引擎思考中），无法解析对方意图。")
            return
        frm, to, captured, ucci, cn = self.history[-1]
        # clone 当前局面并悔一步，恢复对方走子前的局面
        tmp = self.board.clone()
        try:
            tmp.undo_move()
        except Exception:
            self.lbl_suggest.setText("🔍 无法恢复对方走子前的局面，解析失败。")
            return
        ply = len(self.history) - 1  # 对方这步的序号
        intent = self._explain_move_intent(frm, to, board=tmp, ply=ply)
        # 判断对方这步是否吃子、是否将军等已由 intent 包含
        cap_text = ""
        if captured is not None:
            type_cn = {PieceType.ROOK: "车", PieceType.CANNON: "炮", PieceType.HORSE: "马",
                       PieceType.ADVISOR: "士", PieceType.ELEPHANT: "象",
                       PieceType.PAWN: "兵/卒", PieceType.KING: "将/帅"}.get(captured.type, "子")
            cap_text = f"<span style='color:#C0392B;'>（吃了你的{type_cn}）</span> "
        self.lbl_suggest.setText(
            f"<span style='font-size:16px;font-weight:bold;color:#6A1B9A;'>🔍 对方刚走：{cn}</span>"
            f"<span style='font-size:13px;color:#666;'>　（{ucci}）</span> {cap_text}<br>"
            f"<span style='font-size:13px;color:#6A1B9A;'><b>对方意图：</b>{intent}</span><br>"
            f"<span style='font-size:12px;color:#777;'>知己知彼，看清对方意图才能从容应对。</span>")

    def _request_suggestion(self):
        """点击「💡 建议」：用引擎分析当前局面，给出推荐走法+理由。"""
        if self.mode != self.PLAY_MODE:
            return
        if self._thinking or self._game_over:
            return
        if self.board.side is not self.player_color:
            self.lbl_suggest.setText("当前不是你的回合，无法给出建议。")
            return
        if not self.engine.using_pikafish:
            self.lbl_suggest.setText(
                "💡 建议功能需要 Pikafish 引擎支持。\n"
                "当前使用内置弱引擎，无法分析局面。\n"
                "请放入 engine_bin/pikafish.exe 后重启。")
            return
        self.btn_suggest.setEnabled(False)
        self.lbl_suggest.setText("💡 教练分析中…（约1-2秒）")
        self._analyze_thread = QThread()
        self._analyze_worker = _EngineAnalyzeWorker(self.engine, self.board.clone(), movetime_ms=1500)
        self._analyze_worker.moveToThread(self._analyze_thread)
        self._analyze_thread.started.connect(self._analyze_worker.run)
        self._analyze_worker.finished.connect(self._on_suggestion)
        self._analyze_worker.finished.connect(self._analyze_thread.quit)
        self._analyze_thread.start()

    def _on_suggestion(self, info: dict):
        self.btn_suggest.setEnabled(True)
        if not info or not info.get("move"):
            self.lbl_suggest.setText("💡 教练暂未给出建议（引擎分析失败）。")
            return
        frm, to = info["move"]
        try:
            cn = move_to_chinese(self.board, frm, to)
        except Exception:
            cn = move_to_ucci(frm, to)
        ucci = move_to_ucci(frm, to)
        score = info.get("score_cp")
        pv = info.get("pv", [])
        # 局面评估（5档）——引擎分为红方视角，执黑时翻转成「你方」视角
        eval_text = ""
        if score is not None:
            my = score if self.player_color == Color.RED else -score
            if my >= 300:
                eval_text = f"当前局面你方明显占优（约 +{my/100:.1f} 兵）。"
            elif my >= 100:
                eval_text = f"当前局面你方略优（约 +{my/100:.1f} 兵）。"
            elif my > -100:
                eval_text = f"当前局面均势（约 {my/100:+.1f} 兵）。"
            elif my > -300:
                eval_text = f"当前局面你方稍差（约 {my/100:+.1f} 兵），需稳健应对。"
            else:
                eval_text = f"当前局面你方较劣（约 {my/100:+.1f} 兵），建议谨慎防守。"
        # 战术意图解析（为什么这么走）
        intent = self._explain_move_intent(frm, to)
        # 预计主线中文记法（最多展示前4步）
        pv_cn = ""
        if pv:
            try:
                tmp = self.board.clone()
                cn_list = []
                for u in pv[:4]:
                    pf, pt = parse_ucci_move(u)
                    cn_list.append(move_to_chinese(tmp, pf, pt))
                    tmp.make_move(pf, pt)
                pv_cn = "　预计主线：" + " ".join(cn_list) + " …"
            except Exception:
                pv_cn = ""
        self.lbl_suggest.setText(
            f"<span style='font-size:16px;font-weight:bold;color:#C0392B;'>💡 建议走：{cn}</span>"
            f"<span style='font-size:13px;color:#666;'>　（{ucci}）</span><br>"
            f"<span style='font-size:13px;'>{eval_text}</span><br>"
            f"<span style='font-size:13px;color:#1565C0;'><b>为什么这么走：</b>{intent}</span><br>"
            f"<span style='font-size:12px;color:#555;'>{pv_cn}</span><br>"
            f"<span style='font-size:12px;color:#777;'>点击棋盘走出这步，或按自己思路行棋——理解意图比照搬更重要。</span>")

    def _explain_move_intent(self, frm: Pos, to: Pos, board=None, ply=None) -> str:
        """解析一步走法的战术意图，给出"为什么这么走"的具体理由。
        board 默认当前局面；解析历史走法时传入走子前的局面快照。
        ply 为该步序号（0起），用于开局判断。"""
        from ..board.pieces import PieceType
        if board is None:
            board = self.board
        piece = board.get(frm)
        if piece is None:
            return "此步出动子力，调整阵形。"
        captured = board.get(to)
        reasons = []
        red = piece.color is Color.RED
        own = piece.color
        foe = own.opponent

        # 1. 吃子
        if captured is not None:
            cap_name = captured.type.name
            type_cn = {PieceType.ROOK: "车", PieceType.CANNON: "炮", PieceType.HORSE: "马",
                       PieceType.ADVISOR: "士", PieceType.ELEPHANT: "象",
                       PieceType.PAWN: "兵/卒", PieceType.KING: "将/帅"}.get(captured.type, "子")
            if captured.type in (PieceType.ROOK, PieceType.CANNON, PieceType.HORSE):
                reasons.append(f"直接吃掉对方{type_cn}，削弱对方攻防力量（大子兑换中占得物质便宜）")
            elif captured.type in (PieceType.ADVISOR, PieceType.ELEPHANT):
                reasons.append(f"破去对方{type_cn}，撼动其九宫防御，为后续攻杀铺路")
            else:
                reasons.append(f"吃掉对方{type_cn}，积累物质优势")

        # 2. 将军（试走后判断）
        try:
            tmp = board.clone()
            tmp.make_move(frm, to)
            if rules.is_in_check(tmp, foe):
                # 是否将杀
                if not rules.has_legal_move(tmp, foe):
                    reasons.append("此步形成绝杀，对方无解！")
                else:
                    reasons.append("此步将军，迫使对方应将，可夺取先手牵制对方")
        except Exception:
            pass

        # 3. 捉子（落点攻击对方无保护大子）
        try:
            tmp = board.clone()
            tmp.make_move(frm, to)
            my_targets = set()
            for r in range(10):
                for c in range(9):
                    p = tmp.get(Pos(r, c))
                    if p is not None and p.color is own:
                        for t in rules.pseudo_targets(tmp, Pos(r, c)):
                            tgt = tmp.get(t)
                            if tgt is not None and tgt.color is foe and tgt.type in (
                                    PieceType.ROOK, PieceType.CANNON, PieceType.HORSE):
                                my_targets.add((t.row, t.col))
            # 排除已被吃的子
            for tr, tc in my_targets:
                # 判断该对方大子是否被保护（吃它会不会被反吃）
                defender = False
                for r2 in range(10):
                    for c2 in range(9):
                        if (r2, c2) == (frm.row, frm.col):
                            continue
                        p2 = tmp.get(Pos(r2, c2))
                        if p2 is not None and p2.color is own:
                            if Pos(tr, tc) in rules.pseudo_targets(tmp, Pos(r2, c2)):
                                defender = True
                                break
                    if defender:
                        break
                if not defender:
                    reasons.append(f"捉对方大子（{Pos(tr,tc).to_ucci()}位），对方需应对，可争夺先手")
                    break
        except Exception:
            pass

        # 4. 控制中路 / 占据要道（车炮落中线 col4）
        if to.col == 4 and piece.type in (PieceType.ROOK, PieceType.CANNON):
            reasons.append("占据中路要道，控制全局攻防枢纽（中线车炮威力最大）")

        # 5. 过河（兵卒过河 或 大子进入对方半场）
        if piece.type is PieceType.PAWN:
            if not frm.crossed_river(own) and to.crossed_river(own):
                reasons.append("兵卒过河，可左右兼顾、深入敌阵，价值倍增")
            else:
                reasons.append("推进兵卒，压缩对方空间")
        elif piece.type in (PieceType.ROOK, PieceType.CANNON, PieceType.HORSE):
            if not frm.in_own_half(own) and to.in_own_half(own):
                pass  # 退回己方，一般是防守
            elif frm.in_own_half(own) and not to.in_own_half(own):
                reasons.append("大子过河投入战斗，加强对对方阵地的压力")

        # 6. 出动大子（开局阶段：大子从初始位置首次出动）
        is_opening = (ply is not None and ply < 4) or (ply is None and not self.history)
        if is_opening and piece.type in (PieceType.CANNON, PieceType.HORSE, PieceType.ROOK):
            type_cn = {PieceType.ROOK: "车", PieceType.CANNON: "炮",
                       PieceType.HORSE: "马"}.get(piece.type)
            reasons.append(f"开局出动{type_cn}，加快子力调度，抢占要位")

        # 7. 防守（落点保护己方将帅 或 退回己方半场）
        try:
            tmp = board.clone()
            tmp.make_move(frm, to)
            if rules.is_in_check(board, own):  # 走之前自己被将
                if not rules.is_in_check(tmp, own):
                    reasons.append("化解对方将军，确保将帅安全")
        except Exception:
            pass

        if piece.type in (PieceType.ADVISOR, PieceType.ELEPHANT):
            type_cn = "士" if piece.type is PieceType.ADVISOR else "象"
            reasons.append(f"补{type_cn}巩固九宫防御，为城垣添砖加瓦")

        # 8. 兜底：调整阵形
        if not reasons:
            reasons.append("调整阵形，改善子力位置，静待时机")

        # 去重并限制条数
        seen = set()
        uniq = []
        for r in reasons:
            if r not in seen:
                seen.add(r)
                uniq.append(r)
        return "；".join(uniq[:3]) + "。"

    # ----------------- 终局判定（对弈） -----------------
    @property
    def _game_over(self) -> bool:
        return getattr(self, "_over_flag", False)

    def _check_game_over_play(self):
        self._over_flag = False
        side = self.board.side
        if not rules.has_legal_move(self.board, side):
            # 无棋可走者负
            loser = side.name_cn
            winner = side.opponent.name_cn
            self._over_flag = True
            self.board_widget.set_interactive(False)
            you_won = side is not self.player_color
            # 战绩统计 + 每日计划实战任务自动 +1 + 自动存谱
            self.play_stats.record("win" if you_won else "loss")
            self.plan_store.record("play")
            self._save_current_game("win" if you_won else "loss")
            self._refresh_play_record()
            self._update_turn_label()   # 终局后刷新按钮启用状态
            msg = f"{'恭喜，你赢了！' if you_won else '引擎获胜。'}（{loser}无棋可走）"
            QMessageBox.information(self, "对局结束", msg)
            self.status.showMessage(msg, 6000)

    def _refresh_play_record(self):
        """刷新对弈战绩标签：N胜M负K和 · 总盘数 · 胜率。"""
        if not hasattr(self, "lbl_record"):
            return
        s = self.play_stats
        rate = s.win_rate() * 100
        self.lbl_record.setText(
            f"📊 战绩：{s.total} 盘　<b style='color:#2E7D32;'>胜 {s.wins}</b>"
            f"　<b style='color:#C0392B;'>负 {s.losses}</b>　和 {s.draws}"
            f"　<b style='color:#E67E22;'>胜率 {rate:.0f}%</b>")

    # ----------------- 认输 -----------------
    def _on_resign(self):
        """用户主动认输：确认后判负、存谱、锁交互。"""
        if self.mode != self.PLAY_MODE:
            return
        if self._game_over:
            self.status.showMessage("本局已结束，请点「⟳ 新局」开下一盘。", 3000)
            return
        if self._thinking:
            self.status.showMessage("引擎思考中，稍等片刻…", 3000)
            return
        ret = QMessageBox.question(
            self, "认输",
            "确定主动认输吗？\n\n本局将判你负，战绩 +1 负。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if ret != QMessageBox.StandardButton.Yes:
            return
        # 复用终局处理流程（_check_game_over_play 用的是 record("win"/"loss")）
        self._over_flag = True
        self.board_widget.set_interactive(False)
        self.play_stats.record("loss")
        self.plan_store.record("play")
        self._save_current_game("loss")
        self._refresh_play_record()
        self._update_turn_label()
        msg = "🏳 你主动认输，本局判负。"
        sound.play("lose")
        QMessageBox.information(self, "对局结束", msg)
        self.status.showMessage(msg, 6000)

    # ----------------- 求和 -----------------
    def _on_offer_draw(self):
        """向引擎提出和棋：异步评估当前局面，引擎根据优势大小决定接受或拒绝。"""
        if self.mode != self.PLAY_MODE:
            return
        if self._game_over:
            self.status.showMessage("本局已结束，请点「⟳ 新局」开下一盘。", 3000)
            return
        if self._thinking:
            self.status.showMessage("引擎思考中，稍等片刻…", 3000)
            return
        if not self.engine.using_pikafish:
            # 内置弱引擎不会分析，直接简单判定：走了 10 步以上就允许和棋
            if len(self.history) >= 20:
                self._finalize_draw("引擎同意和棋（内置引擎简化判定）。")
            else:
                self.status.showMessage("走子太少，内置引擎拒绝和棋。走完至少 10 步再试。", 3000)
            return

        self.btn_draw.setEnabled(False)
        self.status.showMessage("引擎评估中…", 2000)
        self._draw_thread = QThread()
        self._draw_worker = _EngineAnalyzeWorker(self.engine, self.board.clone(), movetime_ms=1200)
        self._draw_worker.moveToThread(self._draw_thread)
        self._draw_thread.started.connect(self._draw_worker.run)
        self._draw_worker.finished.connect(self._on_draw_evaluated)
        self._draw_worker.finished.connect(self._draw_thread.quit)
        self._draw_thread.start()

    # 引擎优势阈值（红方视角 score_cp）
    # |cp| < 200 → 引擎认为均势，同意和棋
    # |cp| >= 200 → 引擎认为自己明显占优，拒绝并展示局势
    _DRAW_ACCEPT_CP = 200

    def _on_draw_evaluated(self, info: dict):
        self.btn_draw.setEnabled(True)
        if not info:
            self.status.showMessage("引擎分析失败，求和未提交。", 3000)
            return
        score = info.get("score_cp")
        if score is None:
            self.status.showMessage("引擎未给出评估分，求和未提交。", 3000)
            return
        # 引擎视角=红方。用户执红：score直接用；执黑：翻转后看引擎认为自己优势多少
        eng_adv = score if self.player_color == Color.RED else -score
        # eng_adv > 0 → 引擎（红或黑，看执方）认为自己占优
        if abs(score) < self._DRAW_ACCEPT_CP:
            # 局面均势，引擎同意和棋
            self._finalize_draw(f"🤝 引擎同意和棋（局面均势，引擎评估 {score/100:+.1f} 兵）。")
        else:
            # 引擎认为自己明显占优，拒绝求和
            sign = "+" if eng_adv > 0 else "-"
            who = "引擎" if eng_adv > 0 else "你"
            reason = f"引擎认为{who}明显占优（{sign}{abs(score)/100:.1f} 兵），拒绝和棋。"
            self.status.showMessage(reason, 5000)

    def _finalize_draw(self, msg: str):
        """求和被接受后的统一终局处理。"""
        self._over_flag = True
        self.board_widget.set_interactive(False)
        self.play_stats.record("draw")
        self.plan_store.record("play")
        self._save_current_game("draw")
        self._refresh_play_record()
        self._update_turn_label()
        sound.play("draw")
        QMessageBox.information(self, "对局结束", msg)
        self.status.showMessage(msg, 6000)

    def _reset_play_record(self):
        """重置对弈战绩（二次确认）。"""
        ret = QMessageBox.question(
            self, "重置战绩",
            f"确定清空对弈战绩吗？\n\n当前：{self.play_stats.total} 盘"
            f"（胜 {self.play_stats.wins} / 负 {self.play_stats.losses}"
            f" / 和 {self.play_stats.draws}）\n\n此操作不可恢复。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if ret == QMessageBox.StandardButton.Yes:
            self.play_stats.reset()
            self._refresh_play_record()
            self.status.showMessage("对弈战绩已重置", 3000)

    # ----------------- 复盘模式 -----------------
    def _save_current_game(self, result: str):
        """对局分出胜负后自动存谱（data/games/）。

        防御性规则：至少走完 4 半步（双方各 1 回合）才存，
        防止引擎异常返回 None、新局误判终局等边缘情况产生垃圾棋谱。
        """
        try:
            moves = [h[3] for h in self.history]
            cn = [h[4] if len(h) > 4 else "" for h in self.history]
            if len(moves) < 4:
                self.status.showMessage(
                    f"对局太短（仅 {len(moves)} 半步），不保存棋谱。", 3000)
                return
            game_records.save_game(self.player_color.name.lower(),
                                   result, moves, cn)
        except Exception:
            pass  # 存谱失败不影响对局

    def _show_review_dialog(self):
        """复盘选局弹窗：最近 20 局。"""
        games = game_records.list_games(20)
        if not games:
            QMessageBox.information(self, "复盘",
                                    "还没有可复盘的对局。\n\n下完一整盘（分出胜负）后棋谱会自动保存。")
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("🔍 选择要复盘的对局")
        dlg.resize(430, 380)
        lay = QVBoxLayout(dlg)
        lst = QListWidget()
        for _p, g in games:
            me = "红" if g.get("player") == "red" else "黑"
            res = "胜" if g.get("result") == "win" else "负"
            ts = str(g.get("ts", "")).replace("T", " ")
            lst.addItem(f"{ts}　你执{me}　{res}　{len(g.get('moves', []))} 半步")
        if lst.count():
            lst.setCurrentRow(0)
        lst.itemDoubleClicked.connect(lambda _it: dlg.accept())
        lay.addWidget(QLabel("双击或选中后点「开始复盘」："))
        lay.addWidget(lst, 1)
        row = QHBoxLayout()
        row.addStretch(1)
        btn_go = QPushButton("开始复盘")
        btn_go.setStyleSheet("font-weight:bold;")
        btn_go.clicked.connect(dlg.accept)
        row.addWidget(btn_go)
        btn_cancel = QPushButton("取消")
        btn_cancel.clicked.connect(dlg.reject)
        row.addWidget(btn_cancel)
        lay.addLayout(row)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        it = lst.currentItem()
        if it is None:
            return
        path, g = games[lst.currentRow()]
        self._enter_review(g)

    def _enter_review(self, game: dict):
        """进入复盘：保存对弈现场，载入棋谱回放。"""
        if self._thinking or (self._engine_thread and self._engine_thread.isRunning()):
            self.status.showMessage("引擎工作中，请稍候再进入复盘", 3000)
            return
        self._play_snapshot = (self.board, list(self.history), self._over_flag)
        # 清除对弈面板残留（建议/对方意图文字、按钮状态、隐藏建议框避免挤复盘）
        self.lbl_suggest.setText("")
        self.lbl_suggest.hide()
        self.btn_suggest.setEnabled(False)
        self.btn_foe.setEnabled(False)
        self.btn_undo.setEnabled(False)
        self.btn_new.setEnabled(False)
        self.btn_resign.setEnabled(False)
        self.btn_draw.setEnabled(False)
        self._review = {
            "moves": list(game.get("moves", [])),
            "cn": list(game.get("cn", [])),
            "idx": 0,
            "evals": {},       # i -> cp（红方视角，走完第 i 步后局面）
            "bestmoves": {},   # i -> UCCI（第 i 步走子前的引擎最佳走法）
            "marks": {},       # idx -> loss_cp（败着：走完该步后骤降 ≥150 cp）
            "player": game.get("player", "red"),
            "result": game.get("result", ""),
        }
        self.review_bar.show()
        self.score_curve.reset()
        self.board_widget.set_interactive(False)
        self.status.showMessage("复盘模式：◀▶ 逐步回放，⚡评估后自动标出败着", 6000)
        self._review_goto(0)

    def _review_goto(self, idx: int):
        """回放到第 idx 半步后（idx=0 为起始局面之后第1步？——0=未走棋的起始局面显示，1=走完第1半步）。"""
        if not self._review:
            return
        n = len(self._review["moves"])
        idx = max(0, min(int(idx), n))
        self._review["idx"] = idx
        self.score_curve.set_cursor(idx)
        b = Board.start_position()
        for u in self._review["moves"][:idx]:
            frm, to = parse_ucci_move(u)
            b.make_move(frm, to)
        self.board = b
        # 假 history（兼容走法列表渲染）：(frm,to,captured,ucci,cn)
        self.history = [(None, None, None,
                         self._review["moves"][i],
                         self._review["cn"][i] if i < len(self._review["cn"]) else "")
                        for i in range(idx)]
        self.board_widget.set_board(b)
        self._refresh_move_list()
        # 位置标签（短文本）+ 评估/败着详情（独立行，可换行）
        self.lbl_rev_pos.setText(f"第 {idx}/{n} 步")
        ev = self._review["evals"].get(idx)
        if ev is not None or self._review.get("marks", {}).get(idx) is not None:
            parts = []
            if ev is not None:
                my = ev if self._review["player"] == "red" else -ev
                parts.append(f"<b style='color:#E67E22;'>评估 {my/100:+.1f}（你方视角）</b>")
            mark = self._review.get("marks", {}).get(idx)
            # ---- 败着/可改进：只要有 bestmove 且 ≠ 实际走法，就给建议 ----
            move_i = idx - 1  # 对应的 moves 索引
            actual_ucci = self._review["moves"][move_i] if 0 <= move_i < len(self._review["moves"]) else ""
            best_ucci = self._review.get("bestmoves", {}).get(move_i, "")
            player_is_red = self._review["player"] == "red"
            is_user_move = (move_i >= 0) and ((move_i % 2 == 0) == player_is_red)
            if is_user_move and mark is not None:
                parts.append(f"<b style='color:#C0392B;'>⚠ 败着（亏 {abs(mark)/100:.1f} 兵）</b>")
            elif is_user_move and best_ucci and best_ucci != actual_ucci:
                # 不是败着但走的不是最优 → 可改进提示
                if mark is None:
                    # 算一下这步的亏分
                    prev_cp = self._review["evals"].get(move_i, 0.0)
                    after_cp = ev if ev is not None else 0.0
                    drop = (after_cp - prev_cp)
                    my_drop = drop if player_is_red else -drop
                    if -80 < my_drop < -30:
                        parts.append(f"<b style='color:#E67E22;'>⚡ 可优化（亏 {abs(my_drop)/100:.1f} 兵）</b>")
            # 建议走法对比（败着或可改进时都显示）
            if is_user_move and best_ucci and best_ucci != actual_ucci and actual_ucci:
                before_b = Board.start_position()
                for u in self._review["moves"][:move_i]:
                    ff, tt = parse_ucci_move(u)
                    before_b.make_move(ff, tt)
                side_name = "红方" if before_b.side == Color.RED else "黑方"
                actual_f, actual_t = parse_ucci_move(actual_ucci)
                actual_cn = move_to_chinese(before_b, actual_f, actual_t)
                best_cn, bfrm, bto = "", None, None
                try:
                    bfrm = Pos.from_ucci(best_ucci[:2])
                    bto = Pos.from_ucci(best_ucci[2:4])
                    best_cn = move_to_chinese(before_b, bfrm, bto)
                except Exception:
                    pass
                cmp_lines = [
                    f"<span style='color:#666'>{side_name}走了：</span>"
                    f"<b style='color:#C0392B;text-decoration:line-through'>{actual_cn}</b>",
                ]
                if best_cn:
                    cmp_lines.append(
                        f"<span style='color:#666'>建议改走：</span>"
                        f"<b style='color:#2E7D32'>{best_cn}</b>"
                    )
                    self.board_widget.highlight_move(bfrm, bto, color=QColor(46, 125, 50, 120))
                parts.append("<br>".join(cmp_lines))
            elif is_user_move and not best_ucci:
                self.board_widget.clear_highlights()
            else:
                self.board_widget.clear_highlights()
            self.lbl_rev_eval.show()
            self.lbl_rev_eval.setText("<br>".join(parts))
        else:
            self.board_widget.clear_highlights()
            self.lbl_rev_eval.hide()
        # 同步走法列表选中行
        if idx > 0 and idx <= self.move_list.count():
            self.move_list.setCurrentRow(idx - 1)
        else:
            self.move_list.setCurrentRow(-1)

    def _review_exit(self):
        """退出复盘：停止评估/播放，恢复对弈现场。"""
        self._stop_review_eval()
        self._review_play_timer.stop()
        if hasattr(self, "btn_rev_play"):
            self.btn_rev_play.setChecked(False)
            self.btn_rev_play.setText("▶ 自动播放")
        self._review = None
        if self._play_snapshot is not None:
            self.board, self.history, self._over_flag = self._play_snapshot
            self._play_snapshot = None
        self.review_bar.hide()
        can_play = (self.mode == self.PLAY_MODE and self.board.side == self.player_color)
        self.board_widget.set_board(self.board)
        self.board_widget.set_interactive(can_play)
        self.board_widget.clear_highlights()
        # 恢复对弈面板按钮状态
        self.btn_undo.setEnabled(can_play)
        self.btn_new.setEnabled(True)
        self.btn_resign.setEnabled(can_play)
        self.btn_draw.setEnabled(can_play)
        self.btn_suggest.setEnabled(can_play and not self._thinking)
        self.btn_foe.setEnabled(can_play)
        self.lbl_suggest.setText("")
        self.lbl_suggest.show()
        self._refresh_move_list()
        self.status.showMessage("已退出复盘", 3000)

    def _toggle_review_play(self, checked: bool):
        """自动播放开/关。"""
        if not self._review:
            self.btn_rev_play.setChecked(False)
            return
        if checked:
            speed_ms = self.cmb_rev_speed.currentData()
            self._review_play_timer.setInterval(int(speed_ms))
            self._review_play_timer.start()
            self.btn_rev_play.setText("⏸ 暂停")
        else:
            self._review_play_timer.stop()
            self.btn_rev_play.setText("▶ 自动播放")

    def _on_review_play_tick(self):
        """自动播放定时器：逐半步推进，到末尾自动停。"""
        if not self._review:
            self._review_play_timer.stop()
            return
        n = len(self._review["moves"])
        nxt = self._review["idx"] + 1
        if nxt > n:
            self._review_play_timer.stop()
            self.btn_rev_play.setChecked(False)
            self.btn_rev_play.setText("▶ 自动播放")
            self.status.showMessage("自动播放完成", 2000)
            return
        self._review_goto(nxt)

    def _review_evaluate(self):
        """⚡全程评估：后台逐半步 analyze，完成后标败着。"""
        if not self._review:
            return
        if self._review_eval_thread and self._review_eval_thread.isRunning():
            self.status.showMessage("评估进行中…", 2000)
            return
        n = len(self._review["moves"])
        self.btn_rev_eval.setEnabled(False)
        self.lbl_rev_eval.show()
        self.lbl_rev_eval.setText(f"⏳ 评估中 0/{n}（每半步约0.25秒，可随时退出）…")
        self._review_eval_thread = QThread(self)
        self._review_eval_worker = _ReviewEvalWorker(self.engine, self._review["moves"])
        self._review_eval_worker.moveToThread(self._review_eval_thread)
        self._review_eval_thread.started.connect(self._review_eval_worker.run)
        self._review_eval_worker.step_eval.connect(self._on_review_step_eval)
        self._review_eval_worker.progress.connect(
            lambda done, total: self.lbl_rev_eval.setText(f"⏳ 评估中 {done}/{total} …"))
        self._review_eval_worker.finished.connect(self._on_review_eval_done)
        self._review_eval_worker.finished.connect(self._review_eval_thread.quit)
        self._review_eval_thread.start()

    def _stop_review_eval(self):
        if self._review_eval_worker is not None:
            self._review_eval_worker.stop()
        if self._review_eval_thread is not None and self._review_eval_thread.isRunning():
            self._review_eval_thread.quit()
            self._review_eval_thread.wait(2000)
        self.btn_rev_eval.setEnabled(True)

    def _on_review_step_eval(self, i: int, cp: float, bestmove_ucci: str):
        """每半步评估回调：缓存 + 败着判定（你方视角骤降 ≥80 cp）+ 存 bestmove。"""
        if not self._review:
            return
        self._review["evals"][i + 1] = cp    # 走完第 i 步（即局面 idx=i+1）
        # bestmove 是走第 i 步之前局面的最佳走法（当前方视角）
        if bestmove_ucci:
            self._review.setdefault("bestmoves", {})[i] = bestmove_ucci
        player_is_red = self._review["player"] == "red"
        my = cp if player_is_red else -cp
        prev_idx = i                          # 走子前局面 idx=i（i=0 → 起始，缺省 0 分）
        prev = self._review["evals"].get(prev_idx, 0.0)
        my_prev = prev if player_is_red else -prev
        is_user_move = ((i % 2 == 0) == player_is_red)
        if is_user_move and (my - my_prev) <= -80:
            self._review.setdefault("marks", {})[i + 1] = my - my_prev
        # 曲线实时生长
        self.score_curve.set_data(self._review["evals"],
                                  len(self._review["moves"]) + 1,
                                  self._review.get("marks", {}),
                                  cn_moves=self._review.get("cn", []))

    def _on_review_eval_done(self):
        self.btn_rev_eval.setEnabled(True)
        if not self._review:
            return
        marks = self._review.get("marks", {})
        # 统计可优化步数（你方走的步中 bestmove ≠ 实际走法）
        improvable = 0
        player_is_red = self._review["player"] == "red"
        moves = self._review.get("moves", [])
        bestmoves = self._review.get("bestmoves", {})
        for i, u in enumerate(moves):
            if ((i % 2 == 0) == player_is_red):
                bm = bestmoves.get(i, "")
                if bm and bm != u:
                    improvable += 1
        self._review_goto(self._review["idx"])   # 刷新位置标签
        if marks:
            worst = min(marks, key=lambda k: marks[k])
            steps_txt = "、".join(str(k) for k in sorted(marks))
            self.lbl_rev_eval.show()
            self.lbl_rev_eval.setText(
                f"<b style='color:#C0392B;'>⚠ {len(marks)} 步败着</b>"
                f"（第 {steps_txt} 步）"
                f"<br><b style='color:#E67E22;'>最亏</b>"
                f"：第 {worst} 步（亏 {abs(marks[worst])/100:.1f} 兵）"
                f"<br><span style='color:#888'>点曲线上的红点直接跳到败着</span>")
            self.status.showMessage(
                f"评估完成：{len(marks)} 步败着，共 {improvable} 步可改进", 6000)
        elif improvable:
            self.lbl_rev_eval.show()
            self.lbl_rev_eval.setText(
                f"<b style='color:#E67E22;'>⚡ {improvable} 步可优化</b>"
                f"<br><span style='color:#888'>跳到该步查看建议走法</span>")
            self.status.showMessage(
                f"评估完成：无严重败着，但有 {improvable} 步可优化", 6000)
        else:
            self.lbl_rev_eval.show()
            self.lbl_rev_eval.setText(
                "<b style='color:#2E7D32;'>✔ 全部走法接近最优</b>"
                "<br><span style='color:#888'>精彩！</span>")
            self.status.showMessage("评估完成：全程走法合理，无明显失误", 6000)

    # ----------------- 悔棋 -----------------
    def undo_move(self):
        if self.mode != self.PLAY_MODE or self._thinking:
            return
        # 悔到上次轮到用户：撤销引擎+用户两步
        steps = 0
        while self.history and steps < 2:
            self.board.undo_move()
            self.history.pop()
            steps += 1
            if self.board.side is self.player_color:
                break
        self._refresh_move_list()
        self.board_widget.set_board(self.board)
        self._update_turn_label()
        self.board_widget.set_interactive(True)
        self.status.showMessage("已悔棋", 2000)

    # ----------------- 走法记录 -----------------
    def _refresh_move_list(self):
        self.move_list.clear()
        for i, entry in enumerate(self.history):
            ucci = entry[3]
            cn = entry[4] if len(entry) > 4 else ""
            n = i + 1
            side = "红" if n % 2 == 1 else "黑"
            text = f"{cn}（{ucci}）" if cn else ucci
            self.move_list.addItem(f"{(n+1)//2}. {side} {text}")
        self._refresh_material()

    # ----------------- 子力对比（对弈模式专用） -----------------
    # 中国象棋标准整数分制：车9 / 马4 / 炮4 / 仕2 / 相2 / 兵1，帅将不计
    _PIECE_VALUE = {
        PieceType.ROOK: 9, PieceType.HORSE: 4, PieceType.CANNON: 4,
        PieceType.ADVISOR: 2, PieceType.ELEPHANT: 2, PieceType.PAWN: 1,
        PieceType.KING: 0,
    }
    _RED_PNAME = {PieceType.ROOK: "车", PieceType.HORSE: "马", PieceType.CANNON: "炮",
                  PieceType.ADVISOR: "仕", PieceType.ELEPHANT: "相", PieceType.PAWN: "兵"}
    _BLACK_PNAME = {PieceType.ROOK: "车", PieceType.HORSE: "马", PieceType.CANNON: "砲",
                    PieceType.ADVISOR: "士", PieceType.ELEPHANT: "象", PieceType.PAWN: "卒"}
    _MAT_ORDER = (PieceType.ROOK, PieceType.HORSE, PieceType.CANNON,
                  PieceType.PAWN, PieceType.ADVISOR, PieceType.ELEPHANT)

    def _refresh_material(self):
        """刷新对弈模式下的局面评估：复用 _EngineAnalyzeWorker（QThread+Signal 必达）。"""
        if not hasattr(self, "lbl_material"):
            return
        if self.mode != self.PLAY_MODE:
            self.lbl_material.setVisible(False)
            self.lbl_material.setText("")
            return
        # 子力计数占位（同步毫秒级）；复盘模式下只显示子力，不跑引擎评估
        # （避免和「⚡全程评估」抢 Pikafish 互斥锁，拖慢评估）
        red_cnt = {t: 0 for t in self._PIECE_VALUE}
        black_cnt = {t: 0 for t in self._PIECE_VALUE}
        red_score = black_score = 0
        for _pos, p in self.board.pieces_of(Color.RED):
            red_cnt[p.type] += 1
            red_score += self._PIECE_VALUE[p.type]
        for _pos, p in self.board.pieces_of(Color.BLACK):
            black_cnt[p.type] += 1
            black_score += self._PIECE_VALUE[p.type]

        def _fmt(cnt, names):
            return " ".join(f"{names[t]}{cnt[t]}" for t in self._MAT_ORDER)

        diff = red_score - black_score
        self.lbl_material.setVisible(True)
        diff_txt = ("子力均势" if diff == 0 else
                    ("红多 +%d" % diff if diff > 0 else "黑多 +%d" % (-diff)))
        self.lbl_material.setText(
            f"<div style='line-height:1.7;'>"
            f"<b style='color:#C0392B;'>红方</b>　{_fmt(red_cnt, self._RED_PNAME)}　= {red_score} 分<br>"
            f"<b style='color:#333;'>黑方</b>　{_fmt(black_cnt, self._BLACK_PNAME)}　= {black_score} 分<br>"
            f"<span style='color:#888;font-size:12px;'>局面分析中…　{diff_txt}</span></div>")

        # 上一次 eval 还在跑 → 跳过（用占位先撑着）；复盘模式下不跑引擎评估
        if self._review:
            return
        if self._eval_thread and self._eval_thread.isRunning():
            return
        # 复用已有 _EngineAnalyzeWorker，movetime_ms=600 足够出分
        self._eval_thread = QThread(self)
        self._eval_worker = _EngineAnalyzeWorker(self.engine, self.board.clone(),
                                                  movetime_ms=600)
        self._eval_worker.moveToThread(self._eval_thread)
        self._eval_thread.started.connect(self._eval_worker.run)
        self._eval_worker.finished.connect(self._eval_thread.quit)
        self._eval_worker.finished.connect(
            lambda info: self._apply_eval(info, red_cnt, black_cnt, red_score, black_score))
        self._eval_thread.finished.connect(self._eval_worker.deleteLater)
        self._eval_thread.finished.connect(self._eval_thread.deleteLater)
        self._eval_thread.start()

    def _apply_eval(self, info: dict, red_cnt, black_cnt, red_score, black_score):
        """worker 主线程回调：把引擎分补到 label 上（Signal 必达）。"""
        def _fmt(cnt, names):
            return " ".join(f"{names[t]}{cnt[t]}" for t in self._MAT_ORDER)

        if info and isinstance(info.get("score_cp"), (int, float)):
            cp = float(info["score_cp"])
            pawn = cp / 100.0
            if abs(pawn) < 0.2:
                tag, color = "局面均势", "#2E7D32"
            elif abs(pawn) < 0.5:
                tag, color = ("红方略优" if pawn > 0 else "黑方略优"), "#E67E22"
            elif abs(pawn) < 1.2:
                tag, color = ("红方明显优势" if pawn > 0 else "黑方明显优势"), "#C0392B"
            elif abs(pawn) < 2.5:
                tag, color = ("红方大优" if pawn > 0 else "黑方大优"), "#B71C1C"
            else:
                tag, color = ("红方胜势" if pawn > 0 else "黑方胜势"), "#7B1FA2"
            tail = (f"<br><span style='color:{color};font-size:14px;font-weight:bold;'>"
                    f"🚩 {tag}　（引擎评估 {pawn:+.1f} 兵）</span>")
        else:
            tail = "<br><span style='color:#888;font-size:12px;'>（内置引擎无子力评估）</span>"

        html = (
            f"<div style='line-height:1.7;'>"
            f"<b style='color:#C0392B;'>红方</b>　{_fmt(red_cnt, self._RED_PNAME)}　= {red_score} 分<br>"
            f"<b style='color:#333;'>黑方</b>　{_fmt(black_cnt, self._BLACK_PNAME)}　= {black_score} 分"
            f"{tail}</div>"
        )
        self.lbl_material.setText(html)
        self._eval_thread = None
        self._eval_worker = None

    # ----------------- 杂项 -----------------
    # ----------------- 开局书(.bin / .obk) → 开局训练课导入 -----------------
    def _start_auto_import_thread(self):
        """后台线程执行静默导入（OBK 大文件解析可达百秒级，绝不能阻塞主窗口）。"""
        self._auto_import_running = True
        self._refresh_cmb_lesson()   # 立即把占位提示切为“后台导入中…”
        threading.Thread(
            target=self._auto_import_default_book_lessons, daemon=True
        ).start()

    @staticmethod
    def _book_log(msg: str):
        """开局书静默导入全程日志（data/autobook.log）——排查打包环境不加载问题。"""
        try:
            from ..resources import data_dir
            with open(os.path.join(data_dir(), "autobook.log"), "a",
                      encoding="utf-8") as f:
                f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
        except Exception:
            pass

    def _auto_import_default_book_lessons(self):
        """后台导入入口：任何异常都记录日志（绝不静默死亡），结束后必刷新一次。"""
        try:
            self._auto_import_impl()
        except Exception:
            self._book_log(f"auto import CRASHED:\n{traceback.format_exc()}")
        finally:
            self._auto_import_running = False
            # 无论成败都刷新一次，清除“后台导入中…”占位（信号跨线程）
            self.auto_import_done.emit()

    def _auto_import_impl(self):
        """启动静默导入：扫描「开局书/」目录（exe旁/项目根）下所有 .bin/.obk → 最多注入 200 条开局课（用户无感）。"""
        self._book_log("---- auto import start ----")
        try:
            from ..resources import books_dir
            book_dir = books_dir()
        except Exception:
            _book_log_msg = f"books_dir FAILED:\n{traceback.format_exc()}"
            self._book_log(_book_log_msg)
            return
        if not os.path.isdir(book_dir):
            self._book_log(f"book_dir not exists: {book_dir}")
            return
        try:
            books = [
                os.path.join(book_dir, fn)
                for fn in os.listdir(book_dir)
                if fn.lower().endswith(".bin") or fn.lower().endswith(".obk")
            ]
        except OSError:
            return
        if not books:
            return
        try:
            from ..training.opening_book import (
                bin_to_lessons, obk_to_lessons, inject_lessons, all_lessons,
            )
        except Exception:
            self._book_log(f"import opening_book FAILED:\n{traceback.format_exc()}")
            return
        existing_ids = {l.id for l in all_lessons()}
        existing_titles = {l.title for l in all_lessons()}
        total_added = 0
        MAX_PER_RUN = 200
        self._book_log(f"books={ [os.path.basename(b) for b in sorted(books)] }")
        for bp in sorted(books):
            if total_added >= MAX_PER_RUN:
                break
            is_obk = bp.lower().endswith(".obk")
            prefix = "autoobk" if is_obk else "autobin"
            t0 = time.time()
            try:
                if is_obk:
                    # OBK 固定请求数（指纹稳定→缓存不因 bin 注入量变化而失效）；
                    # 总量上限由下方注入截断保证
                    lessons, _warns = obk_to_lessons(
                        bp, difficulty=2, max_lessons=MAX_PER_RUN,
                        min_steps=6, start_id_prefix=prefix,
                    )
                    lessons = lessons[:max(0, MAX_PER_RUN - total_added)]
                else:
                    lessons, _warns = bin_to_lessons(
                        bp, difficulty=2, max_lessons=max(10, MAX_PER_RUN - total_added),
                        min_steps=6, start_id_prefix=prefix,
                    )
            except Exception:
                self._book_log(f"{os.path.basename(bp)} EXCEPTION:\n{traceback.format_exc()}")
                continue
            self._book_log(f"{os.path.basename(bp)} -> {len(lessons)} lessons in "
                      f"{time.time()-t0:.1f}s warns={_warns[:3]}")
            # 过滤：避免 ID 或标题完全和已有课程重复的
            uniq = []
            for l in lessons:
                if l.id in existing_ids:
                    continue
                if l.title in existing_titles:
                    continue
                uniq.append(l)
                existing_ids.add(l.id)
                existing_titles.add(l.title)
            if not uniq:
                self._book_log(f"{os.path.basename(bp)} -> all duplicates, skipped")
                continue
            inject_lessons(uniq)
            total_added += len(uniq)
            self._book_log(f"{os.path.basename(bp)} -> injected {len(uniq)} (total {total_added})")
        self._book_log(f"auto import done, total_added={total_added}")

    @staticmethod
    def _lesson_source(lid: str) -> str:
        """按课程 id 前缀识别来源：obk / bin / builtin。"""
        if lid.startswith(("autoobk_", "obk_")):
            return "obk"
        if lid.startswith(("autobin_", "bin_")):
            return "bin"
        return "builtin"

    def _refresh_cmb_lesson(self):
        """开局课库有变化或切换来源时，刷新课程下拉框（只显示当前库，恢复该库上次选中）。"""
        from ..training.opening_book import all_lessons
        cur_src = self._current_source()
        # 先记住当前库的选中，clear 后 currentData 会变 None
        lid_now = self.cmb_lesson.currentData() if hasattr(self, "cmb_lesson") else None
        if lid_now:
            self._lesson_cur[cur_src] = lid_now
        items: dict[str, list] = {"builtin": [], "bin": [], "obk": []}
        for l in all_lessons():
            src = self._lesson_source(l.id)
            if src in items:
                side_cn = "红" if l.side is Color.RED else "黑"
                items[src].append((f"{l.title}（你执{side_cn}）", l.id))
        running = getattr(self, "_auto_import_running", False)
        placeholders = {
            "builtin": "（暂无内置课程）",
            "bin": "（后台导入 .bin 中…）" if running else "（暂无：导入 .bin 后自动出现）",
            "obk": "（后台导入 .obk 中…）" if running else "（暂无：导入 .obk 后自动出现）",
        }
        try:
            self.cmb_lesson.blockSignals(True)
            self.cmb_lesson.clear()
            if items[cur_src]:
                for text, lid in items[cur_src]:
                    self.cmb_lesson.addItem(text, lid)
            else:
                self.cmb_lesson.addItem(placeholders[cur_src], None)
            restore = self._lesson_cur.get(cur_src)
            if restore:
                i = self.cmb_lesson.findData(restore)
                if i >= 0:
                    self.cmb_lesson.setCurrentIndex(i)
        finally:
            self.cmb_lesson.blockSignals(False)
        # 下拉列表宽度自适应最长课程名（上限620px），避免中间"…"截断；悬停可看完整标题
        fm = self.cmb_lesson.fontMetrics()
        try:
            w = max(fm.horizontalAdvance(self.cmb_lesson.itemText(i))
                    for i in range(self.cmb_lesson.count()))
        except ValueError:
            w = 0
        self.cmb_lesson.view().setFixedWidth(min(max(w + 48, self.cmb_lesson.width()), 620))
        for i in range(self.cmb_lesson.count()):
            self.cmb_lesson.setItemData(
                i, self.cmb_lesson.itemText(i), Qt.ItemDataRole.ToolTipRole)

    def _import_book_lessons(self):
        """GUI 入口：从 .bin/.obk → 开局训练课（后台线程解析 + 预览确认 + 注入下拉框）。"""
        fname, _ = QFileDialog.getOpenFileName(
            self, "选择开局书文件（支持 Polyglot .bin / 秦大 OBK .obk）", "",
            "开局书文件 (*.bin *.obk);;Polyglot .bin (*.bin);;秦大OBK (*.obk);;所有文件 (*.*)",
        )
        if not fname:
            return
        fmt_tag = "OBK" if fname.lower().endswith(".obk") else ".bin"
        lv, ok = QInputDialog.getInt(self, f"导入{fmt_tag}开局书", "课程难度（1=入门～5=专业）：", 2, 1, 5, 1)
        if not ok:
            return
        mx, ok = QInputDialog.getInt(
            self, f"导入{fmt_tag}开局书", "最多展开多少条谱招链（开局课）：", 100, 5, 500, 5
        )
        if not ok:
            mx = 100
        # 后台线程解析
        prog_d = None
        try:
            from PySide6.QtWidgets import QProgressDialog
            prog_d = QProgressDialog(f"正在展开《{os.path.basename(fname)}》…", None, 0, 0, self)
            prog_d.setWindowModality(Qt.WindowModality.WindowModal)
            prog_d.setMinimumDuration(0)
            prog_d.show()
        except Exception:
            pass

        is_obk = fname.lower().endswith(".obk")

        def worker():
            try:
                from ..training.opening_book import bin_to_lessons, obk_to_lessons
                if is_obk:
                    # OBK：与静默导入共用同一份缓存（同 prefix/难度/min_steps），
                    # 真难度由 OBK 胜负统计自动计算，difficulty 参数仅作 fallback
                    lessons, warns = obk_to_lessons(
                        fname, difficulty=2, max_lessons=mx, min_steps=6,
                        start_id_prefix="autoobk",
                    )
                else:
                    lessons, warns = bin_to_lessons(
                        fname, difficulty=lv, max_lessons=mx, min_steps=6,
                        start_id_prefix="autobin",
                    )
            except Exception as e:  # noqa: BLE001
                lessons, warns = [], [f"解析异常：{e!r}"]
            QTimer.singleShot(
                0,
                lambda: self._after_book_imported(lessons, warns, fname, prog_d),
            )

        threading.Thread(target=worker, daemon=True).start()

    def _after_book_imported(self, lessons, warns, fname: str, prog):
        if prog is not None:
            prog.cancel()
        import os
        base = os.path.basename(fname)
        is_obk = fname.lower().endswith(".obk")
        fmt_tag = "OBK" if is_obk else ".bin"
        if not lessons:
            msg = f"未能从《{base}》展开任何可用开局课。\n"
            if warns:
                msg += "\n原因（前 6 条）：\n  • " + "\n  • ".join(warns[:6])
            if is_obk:
                msg += "\n提示：请确认是「秦大象棋 SQLite 版 OBK」开局库文件。"
            else:
                msg += "\n提示：请确认是「中国象棋专用 Polyglot .bin」（非国际象棋）。"
            QMessageBox.warning(self, f"{fmt_tag} 导入结果", msg)
            return
        # 统计：课程数/步数范围/分类分布
        total_steps = sum(len(l.moves) for l in lessons)
        min_s = min(len(l.moves) for l in lessons)
        max_s = max(len(l.moves) for l in lessons)
        cat_count: dict[str, int] = {}
        for l in lessons:
            cat_count[l.category] = cat_count.get(l.category, 0) + 1
        cat_lines = [f"    · {c}: {n} 课" for c, n in sorted(cat_count.items(), key=lambda x: -x[1])[:8]]
        msg_lines = [
            f"已解析《{base}》，准备导入开局训练库：",
            f"  · 新开局课：{len(lessons)} 门",
            f"  · 步数范围：{min_s}～{max_s} 半步（共 {total_steps}）",
            f"  · 导入难度：{'★' * lessons[0].difficulty}",
            f"  · 分类分布：",
            "\n".join(cat_lines),
        ]
        if warns:
            msg_lines.append("")
            msg_lines.append(f"⚠ 警告 {len(warns)} 条（前 5 条）：")
            for w in warns[:5]:
                msg_lines.append(f"  - {w}")
        msg_lines.append("")
        msg_lines.append("是否确认追加到「开局训练」下拉框？（原有课程保留，新课追加在末尾）")
        resp = QMessageBox.question(
            self, f"确认导入 {fmt_tag} 开局课",
            "\n".join(msg_lines),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if resp != QMessageBox.StandardButton.Yes:
            return
        from ..training.opening_book import inject_lessons
        inject_lessons(lessons)
        self._refresh_cmb_lesson()
        # 如果当前面板正处在开局训练模式，也顺便刷新课程介绍
        if hasattr(self, "opening_box") and self.opening_box.isVisible():
            try:
                self._load_lesson(self._active_lesson_combo().currentData())
            except Exception:
                pass
        QMessageBox.information(
            self, f"{fmt_tag} 开局课导入成功",
            f"已成功追加 {len(lessons)} 门开局课到训练库（共 {total_steps} 半步）！\n\n"
            f"👉 请切换到对应来源分组（{'🗡 OBK飞刀库' if is_obk else '📗 bin开局书'}）即可看到新课，选择后立即开始训练。"
        )

    def _show_help(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("📖 使用说明")
        dlg.resize(820, 640)
        layout = QVBoxLayout(dlg)

        html = """
<style>
body { font-family: "Microsoft YaHei", "微软雅黑", sans-serif; font-size: 14px; }
h2 { color: #c0392b; border-bottom: 2px solid #c0392b; padding-bottom: 4px; margin-top: 22px; }
h3 { color: #2c3e50; margin-top: 16px; }
.code { background: #f5f5f5; padding: 2px 6px; border-radius: 3px; font-family: Consolas, monospace; }
.tip { background: #fff8dc; border-left: 4px solid #f39c12; padding: 8px 12px; margin: 10px 0; }
.warn { background: #ffebee; border-left: 4px solid #e74c3c; padding: 8px 12px; margin: 10px 0; }
table { border-collapse: collapse; width: 100%; margin: 10px 0; }
th, td { border: 1px solid #ddd; padding: 6px 10px; text-align: left; }
th { background: #ecf0f1; }
</style>

<h2>🎮 一、人机对弈</h2>
<h3>基本操作</h3>
<ul>
  <li><b>走子</b>：点击己方棋子 → 绿色框选中 → 点击目标位置</li>
  <li><b>悔棋</b>：侧栏「悔棋」按钮，可连续撤销</li>
  <li><b>新局</b>：侧栏「新局」按钮，重置棋盘开始新对局</li>
  <li><b>认输 / 求和</b>：对弈中可主动认输或向对方提和</li>
</ul>
<h3>引擎选择</h3>
<ul>
  <li>软件自带<strong>内置引擎</strong>（弱，适合入门）</li>
  <li>将 <code>pikafish.exe</code> 和 <code>pikafish.nnue</code> 放入 <code>engine_bin\\</code> 目录 → 自动升级为 Pikafish 强引擎</li>
  <li>菜单栏「引擎」→ 可切换引擎难度、加载/卸载开局书</li>
</ul>
<div class="tip">💡 开局书能显著提升早期落子质量。卸载后引擎按纯计算落子，适合训练中局基本功。</div>

<h2>📖 二、开局训练</h2>
<p>课程化教学，覆盖常见开局体系：</p>
<table>
  <tr><th>课程</th><th>说明</th></tr>
  <tr><td>中炮</td><td>炮二平五开局，最主流的进攻型布局</td></tr>
  <tr><td>屏风马</td><td>马二进三 + 马八进七，弹性防守体系</td></tr>
  <tr><td>飞相局</td><td>相三进五开局，稳健型</td></tr>
  <tr><td>仙人指路</td><td>兵七进一开局，灵活多变</td></tr>
  <tr><td>……</td><td>持续扩充中</td></tr>
</table>
<p><b>训练方式</b>：按课程顺序逐步走子 → 走偏即纠正并讲解 → 中文记法（如「炮二平五」）</p>

<h2>⚔ 三、中局战术</h2>
<p>30+ 种中局战术，每次随机出题：</p>
<ul>
  <li><b>捉双</b>：一子攻击对方两子，必得其一</li>
  <li><b>闪击</b>：中间子移动后露出后方攻击</li>
  <li><b>马踩双</b>：马同时踩对方两子</li>
  <li><b>弃子入局</b>：主动弃子换取攻势</li>
</ul>
<p><b>判定方式</b>：逐线精确验证 → 错着给出偏着演示 → 显示正确解法</p>

<h2>🎓 四、残局训练</h2>
<p>37 道精选杀法残局，分为：</p>
<table>
  <tr><th>类别</th><th>示例 ID</th><th>杀法</th></tr>
  <tr><td>单车</td><td>ch001-003</td><td>单车困将、车帅白脸将</td></tr>
  <tr><td>马兵</td><td>mb002-004</td><td>马封九宫、兵吃士将军</td></tr>
  <tr><td>炮兵</td><td>pb002</td><td>闷宫杀（红兵当炮架）</td></tr>
  <tr><td>车马兵</td><td>cmb002</td><td>三强联合困毙</td></tr>
  <tr><td>……</td><td>……</td><td>持续扩充中</td></tr>
</table>
<p><b>训练方式</b>：选择题目 → 红方先行 → 必须走出唯一杀着才能通过</p>
<div class="tip">💡 困毙也是合法杀法：对方未被将军但无任何合法走法。</div>

<h2>🎯 五、基本杀法</h2>
<p>30 种经典杀法分类训练：</p>
<ul>
  <li><b>重炮</b>：后炮借前炮为架照将</li>
  <li><b>闷杀 / 闷宫</b>：炮借己方棋子为架，对方无法解将</li>
  <li><b>卧槽马 / 钓鱼马</b>：马跳到特定位置封锁将路</li>
  <li><b>双车错</b>：双车交替将军形成绝杀</li>
  <li><b>臣压君</b>：己方棋子挡住对方将的逃路</li>
  <li>……</li>
</ul>

<h2>🧠 六、思维口诀</h2>
<p>29 条经典象棋口诀，按四大阶段分类：</p>
<table>
  <tr><th>阶段</th><th>口诀示例</th></tr>
  <tr><td>开局原理</td><td>炮不轻发、士象补中不补边</td></tr>
  <tr><td>中局战术</td><td>双马连环、马踏八方勿蹈边线</td></tr>
  <tr><td>残局原则</td><td>残棋炮归家、车怕低头将怕高、马不蹈边</td></tr>
  <tr><td>杀法要诀</td><td>象棋十诀·千古心法</td></tr>
</table>

<h2>📅 七、每日计划</h2>
<p>自动生成每日训练任务，记录完成进度。</p>

<h2>🔍 八、复盘功能</h2>
<p>对弈结束后点击顶栏「🔍 复盘」按钮：</p>
<ul>
  <li><b>全程评估</b>：引擎逐步打分，生成评估曲线</li>
  <li><b>败着标记</b>：亏 ≥0.8 兵 → 红色败着；亏 0.3-0.8 兵 → 橙色可优化</li>
  <li><b>建议走法</b>：每步给出引擎最佳着法，与你的实际着法对比</li>
  <li><b>自动播放</b>：⏯ 按钮逐半步推进，速度可调节（慢 / 中 / 快）</li>
  <li><b>曲线导航</b>：点击曲线上任意点直接跳到该步</li>
</ul>

<h2>🖱 九、棋盘交互</h2>
<ul>
  <li><b>全屏按钮</b>：棋盘右下角 ⛶ 按钮，点击进入/退出全屏（Esc 也可退出）</li>
  <li><b>光标</b>：棋盘内显示蓝色手指光标，精准指向</li>
  <li><b>建议高亮</b>：复盘建议走法在棋盘上用绿色粗框标记</li>
</ul>

<h2>💾 十、训练进度</h2>
<p>残局 / 开局 / 中局 / 杀法 四大模块<strong>自动记忆</strong>你上次训练到哪一题，下次进入直接跳到该位置。</p>

<h2>⚙ 十一、常见问题</h2>
<div class="warn">⚠ 引擎弱？放入 <code>engine_bin/pikafish.exe</code> + <code>pikafish.nnue</code> 即可升级。</div>
<div class="tip">💡 所有训练数据保存在 <code>%LOCALAPPDATA%\中国象棋培训大师\data\</code> 下，可随时备份。</div>

<h2>📞 联系开发者</h2>
<p>深圳市创炫互动网络科技有限公司 · <a href="https://www.mrheedu.com">www.mrheedu.com</a></p>
        """

        browser = QTextBrowser()
        browser.setHtml(html)
        browser.setOpenExternalLinks(True)
        layout.addWidget(browser)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dlg.reject)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("关闭")
        layout.addWidget(buttons)

        dlg.exec()

    def _show_about(self):
        icon_path = Path(bundle_root()) / "app_icon.png"
        show_about("中国象棋培训大师", self,
                   icon_path=icon_path if icon_path.is_file() else None)

    def closeEvent(self, event):
        # 依次停掉所有工作线程
        for th in (self._engine_thread, self._analyze_thread, self._review_eval_thread,
                   self._mateline_thread, self._eval_thread):
            if th and th.isRunning():
                th.quit()
                th.wait(2000)
        # 关闭引擎子进程，防止 pikafish.exe 残留
        try:
            self.engine.shutdown()
        except Exception:
            pass
        super().closeEvent(event)
