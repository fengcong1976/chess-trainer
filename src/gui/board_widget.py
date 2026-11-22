"""棋盘控件：QPainter 自绘棋盘与棋子，支持点击选子、拖拽落子、走子动画。

坐标：内部 (row, col) 与 board 一致（row0=黑方顶，row9=红方底）。
默认红方在下方；可翻转视角。
"""
from typing import Optional, List

from PySide6.QtCore import Qt, Signal, QPointF, QTimer, QRectF
from PySide6.QtGui import (QPainter, QColor, QPen, QBrush, QFont,
                         QLinearGradient, QRadialGradient, QPolygonF,
                         QCursor, QPixmap, QImage)
from PySide6.QtWidgets import QWidget, QPushButton

from ..board.board import Board
from ..board.pieces import Color, PieceType
from ..board.position import Pos
from ..board import rules


class BoardWidget(QWidget):
    """自绘象棋棋盘控件"""

    # 用户落子信号：(frm_row, frm_col, to_row, to_col)
    moveAttempted = Signal(int, int, int, int)
    # 点击提示信号（str）：用于把"为什么点不动"反馈到主窗口状态栏
    hintMessage = Signal(str)
    # 走子动画**真的结束了**才发这个信号（用于等动画结束再解锁棋盘）。
    # 不使用普通callable，避免 Qt 事件循环竞争导致回调丢失。
    animFinished = Signal()
    # 全屏切换请求（棋盘右下角 ⛶ 按钮 / Esc 键触发）
    fullscreenToggled = Signal()

    # 视觉常量
    BG_COLOR = QColor(238, 196, 124)          # 棋盘木色
    LINE_COLOR = QColor(92, 58, 30)           # 线条深棕
    RIVER_COLOR = QColor(92, 58, 30)
    RED_PIECE = QColor(192, 57, 43)           # 红子色
    BLACK_PIECE = QColor(40, 40, 40)          # 黑子色
    DISC_FILL = QColor(248, 235, 205)         # 棋子底色（米白）
    SELECT_COLOR = QColor(39, 174, 96, 160)   # 选中高亮
    LAST_COLOR = QColor(243, 156, 18, 180)    # 上一步底色高亮（自己）
    ARROW_COLOR = QColor(232, 120, 28, 235)   # 上一步箭头（自己，橙）
    FOE_COLOR = QColor(231, 76, 60, 235)      # 对方走步箭头/边框（醒目红）
    CHECK_COLOR = QColor(231, 76, 60, 180)    # 被将红框
    HINT_DOT = QColor(39, 174, 96, 180)       # 合法落点提示
    MINE_RING = QColor(241, 196, 15, 220)     # 己方棋子额外金环（一眼识别"哪些是我的棋"）

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(430, 480)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(self._make_hand_cursor())   # 棋盘内蓝色手指光标

        self.board: Board = Board.start_position()
        self.flipped: bool = False            # 视角翻转（黑方在下）
        self.player_color: Color = Color.RED  # 可操作方
        self.interactive: bool = True         # 是否允许用户操作

        self._selected: Optional[Pos] = None
        self._legal_targets: List[Pos] = []
        self._last_move: Optional[tuple] = None   # (frm, to)
        self._last_move_by: Optional[Color] = None  # 最近一步走子方
        self._check_pos: Optional[Pos] = None     # 被将的将位
        self._suggest_move: Optional[tuple] = None  # 建议走法高亮 (frm, to)
        self._suggest_color: QColor = QColor(46, 125, 50, 120)  # 绿色半透明

        # 拖拽相关
        self._dragging: bool = False
        self._drag_pos: Optional[QPointF] = None
        self._drag_from: Optional[Pos] = None

        # 走子动画
        self._anim: Optional[dict] = None  # {piece, frm, to, t0, dur}
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick_anim)

        # 右下角全屏按钮（悬浮在棋盘上，不占布局空间）
        self._btn_fullscreen = QPushButton("⛶", self)
        self._btn_fullscreen.setToolTip("棋盘全屏（再点一次或按 Esc 退出）")
        self._btn_fullscreen.setFixedSize(34, 34)
        self._btn_fullscreen.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_fullscreen.setStyleSheet(
            "QPushButton{background:rgba(92,58,30,120);color:#FFF;"
            "border:1px solid rgba(92,58,30,180);border-radius:6px;"
            "font-size:18px;font-weight:bold;}"
            "QPushButton:hover{background:rgba(92,58,30,190);}"
        )
        self._btn_fullscreen.clicked.connect(self.fullscreenToggled.emit)
        self._btn_fullscreen.show()
        self._reposition_fullscreen_btn()

    @staticmethod
    def _make_hand_cursor() -> QCursor:
        """用项目根目录的 鼠标图片.png 做手指光标：缩小到 30px、去近白背景，
        热点设在指尖。图片缺失时回退 ☝ 蓝色字形，再兜底系统手型。"""
        import os
        import sys
        candidates = []
        if getattr(sys, "_MEIPASS", None):   # PyInstaller 解包目录
            candidates.append(os.path.join(sys._MEIPASS, "鼠标图片.png"))
        candidates.append(os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "..", "鼠标图片.png")))
        path = next((c for c in candidates if os.path.isfile(c)), None)
        if path:
            src = QPixmap(path)
            if not src.isNull():
                small = src.scaled(128, 128, Qt.AspectRatioMode.KeepAspectRatio,
                                   Qt.TransformationMode.SmoothTransformation)
                # 该 PNG 无 alpha 通道 → 转 ARGB32 才能写入透明像素
                img = small.toImage().convertToFormat(QImage.Format.Format_ARGB32)
                # 去背景：1) 近白/浅灰  2) 浅蓝灰投影（高亮度低饱和且偏蓝）
                for y in range(img.height()):
                    for x in range(img.width()):
                        c = img.pixelColor(x, y)
                        if c.alpha() == 0:
                            continue
                        r, g, b = c.red(), c.green(), c.blue()
                        mx, mn = max(r, g, b), min(r, g, b)
                        if (mn > 200 and (mx - mn) < 18) or \
                           (mn > 165 and (mx - mn) < 45 and b >= r):
                            img.setPixelColor(x, y, QColor(0, 0, 0, 0))
                # 清除右下角 AI 水印残留（该区域没有手指内容）
                for y in range(int(img.height() * 0.86), img.height()):
                    for x in range(int(img.width() * 0.82), img.width()):
                        img.setPixelColor(x, y, QColor(0, 0, 0, 0))
                # ---- 填补内部透明空洞（如指尖高光白点被误抠）：与周边颜色一致 ----
                from collections import deque
                W2, H2 = img.width(), img.height()
                stride = img.bytesPerLine()
                buf = img.constBits().asstring(img.sizeInBytes())   # ARGB32 内存序 BGRA
                transp = [[buf[y * stride + x * 4 + 3] == 0 for x in range(W2)]
                          for y in range(H2)]
                # 从边缘泛洪：与外部连通的透明区 → outside
                outside = [[False] * W2 for _ in range(H2)]
                dq = deque()
                for x in range(W2):
                    if transp[0][x]:
                        outside[0][x] = True; dq.append((x, 0))
                    if transp[H2 - 1][x]:
                        outside[H2 - 1][x] = True; dq.append((x, H2 - 1))
                for y in range(H2):
                    if transp[y][0]:
                        outside[y][0] = True; dq.append((0, y))
                    if transp[y][W2 - 1]:
                        outside[y][W2 - 1] = True; dq.append((W2 - 1, y))
                while dq:
                    x, y = dq.popleft()
                    for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                        if 0 <= nx < W2 and 0 <= ny < H2 and transp[ny][nx] \
                                and not outside[ny][nx]:
                            outside[ny][nx] = True
                            dq.append((nx, ny))
                # 内部空洞 = 透明且不与外界连通；多源BFS用相邻像素均色回填
                col = [[None] * W2 for _ in range(H2)]
                hole = [[False] * W2 for _ in range(H2)]
                for y in range(H2):
                    for x in range(W2):
                        if transp[y][x]:
                            hole[y][x] = not outside[y][x]
                        else:
                            i = y * stride + x * 4
                            col[y][x] = (buf[i + 2], buf[i + 1], buf[i])
                dq = deque()
                for y in range(H2):
                    for x in range(W2):
                        if hole[y][x] and any(
                                0 <= x + dx < W2 and 0 <= y + dy < H2
                                and col[y + dy][x + dx] is not None
                                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))):
                            dq.append((x, y))
                while dq:
                    x, y = dq.popleft()
                    if col[y][x] is not None:
                        continue
                    rs = gs = bs = n = 0
                    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1),
                                   (1, 1), (1, -1), (-1, 1), (-1, -1)):
                        nx, ny = x + dx, y + dy
                        if 0 <= nx < W2 and 0 <= ny < H2 and col[ny][nx] is not None:
                            c = col[ny][nx]
                            rs += c[0]; gs += c[1]; bs += c[2]; n += 1
                    if n == 0:
                        continue
                    col[y][x] = (rs // n, gs // n, bs // n)
                    for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                        if 0 <= nx < W2 and 0 <= ny < H2 and hole[ny][nx] \
                                and col[ny][nx] is None:
                            dq.append((nx, ny))
                for y in range(H2):
                    for x in range(W2):
                        if hole[y][x] and col[y][x] is not None:
                            r, g, b = col[y][x]
                            img.setPixelColor(x, y, QColor(r, g, b, 255))
                pm = QPixmap.fromImage(img)
                # 热点=指尖：图片中指尖约在 (0.45, 0.12) 处
                return QCursor(pm, int(pm.width() * 0.45), int(pm.height() * 0.12))
        # ---- 回退：☝ 字形蓝色手指 ----
        size = 36
        pm = QPixmap(size, size)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        p.setPen(QColor("#1565C0"))          # 蓝色
        f = QFont()
        f.setFamily("Segoe UI Symbol")       # 单色字形，跟随画笔颜色
        f.setPixelSize(30)
        p.setFont(f)
        p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, "\u261d\uFE0E")
        p.end()
        # 检查是否真的画出了字形（缺字体可能是空/方框）
        img = pm.toImage()
        blue_pixels = sum(
            1 for y in range(0, size, 2) for x in range(0, size, 2)
            if img.pixelColor(x, y).alpha() > 0
            and img.pixelColor(x, y).blue() > img.pixelColor(x, y).red())
        if blue_pixels >= 5:
            return QCursor(pm, 18, 2)        # 热点=指尖（上中）
        return QCursor(Qt.CursorShape.PointingHandCursor)

    def _reposition_fullscreen_btn(self):
        """把全屏按钮固定在右下角。"""
        m = 8
        self._btn_fullscreen.move(self.width() - self._btn_fullscreen.width() - m,
                                  self.height() - self._btn_fullscreen.height() - m)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._reposition_fullscreen_btn()

    # ---------------- 对外接口 ----------------
    def set_board(self, board: Board, animate_last: Optional[tuple] = None,
                  last_move_by: Optional[Color] = None):
        """更新棋盘状态。animate_last=(frm,to) 时播放滑行动画。
        last_move_by 指明最近一步的走子方：与 player_color 不同（即对方）时
        用红色醒目标注，相同（自己）时用橙色标注。"""
        self.board = board
        self._last_move = animate_last
        self._last_move_by = last_move_by
        self._refresh_check()
        self._selected = None
        self._legal_targets = []
        if animate_last is not None:
            self._start_anim(*animate_last)
        else:
            self.update()

    def set_interactive(self, on: bool):
        self.interactive = on
        if not on:
            self._selected = None
            self._legal_targets = []
            self.update()

    def set_player_color(self, color: Color):
        self.player_color = color

    def flip(self, flipped: bool):
        self.flipped = flipped
        self.update()

    def clear_highlights(self):
        self._selected = None
        self._legal_targets = []
        self._last_move = None
        self._suggest_move = None
        self.update()

    def highlight_move(self, frm: Pos, to: Pos, color: QColor = None):
        """高亮建议走法（棋盘上画绿色半透明方框标记起点和终点）。"""
        self._suggest_move = (frm, to)
        if color is not None:
            self._suggest_color = color
        self.update()

    # ---------------- 坐标变换 ----------------
    def _geometry(self):
        """返回 (cell, ox, oy)：每格像素、左上原点像素。"""
        w, h = self.width(), self.height()
        # 8.88=8列+2×0.44棋子半径；9.88=9行+2×0.44棋子半径——保证首末行棋子不贴边
        cell = min((w - 16) / 8.88, (h - 16) / 9.88)
        cols_px = cell * 8
        rows_px = cell * 9
        ox = (w - cols_px) / 2
        oy = (h - rows_px) / 2
        return cell, ox, oy

    def _to_pixel(self, pos: Pos) -> QPointF:
        cell, ox, oy = self._geometry()
        r, c = pos.row, pos.col
        if self.flipped:
            r, c = 9 - r, 8 - c
        return QPointF(ox + c * cell, oy + r * cell)

    def _from_pixel(self, p: QPointF) -> Optional[Pos]:
        cell, ox, oy = self._geometry()
        c_raw = (p.x() - ox) / cell
        r_raw = (p.y() - oy) / cell
        # --- 第1层：越界点自动 clamp 到最近格子 ---
        # 历史教训：用户点最底部的车时，widget底边比棋盘逻辑边缘还低15像素，
        # 导致 16 次点击全部 BLOCKED_POS_OUT_OF_BOARD，看起来"点不动"。
        # 真实位置在半格容差内就放行，否则就把越界点往最近格"吸"。
        c = int(round(c_raw))
        r = int(round(r_raw))
        clamped = False
        if c < 0:
            c = 0
            clamped = True
        elif c > 8:
            c = 8
            clamped = True
        if r < 0:
            r = 0
            clamped = True
        elif r > 9:
            r = 9
            clamped = True
        # 第2层：吸到格上之后，再查离实际点击的像素距离是不是 <= 1.2格。
        # 这允许"点到棋子贴图突出边缘外一点"仍算点中（棋子半径=cell*0.44，允许0.76余量）。
        dx = (c - c_raw) * cell
        dy = (r - r_raw) * cell
        dist_cell = (dx * dx + dy * dy) ** 0.5 / cell if cell > 0 else 99
        if dist_cell > 1.2:
            return None
        real_r, real_c = r, c
        if self.flipped:
            real_r, real_c = 9 - r, 8 - c
        return Pos(real_r, real_c)

    # ---------------- 绘制 ----------------
    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        cell, ox, oy = self._geometry()
        cols_px = cell * 8
        rows_px = cell * 9

        # 背景
        grad = QLinearGradient(0, 0, 0, self.height())
        grad.setColorAt(0, QColor(248, 220, 160))
        grad.setColorAt(1, QColor(228, 188, 116))
        p.fillRect(self.rect(), QBrush(grad))

        # 棋盘外框
        p.setPen(QPen(self.LINE_COLOR, 3))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(QRectF(ox - 8, oy - 8, cols_px + 16, rows_px + 16))

        # 横线（10条）与纵线
        p.setPen(QPen(self.LINE_COLOR, 1.4))
        for r in range(10):
            y = oy + r * cell
            p.drawLine(QPointF(ox, y), QPointF(ox + cols_px, y))
        for c in range(9):
            x = ox + c * cell
            # 楚河汉界处纵线断开（第4、5横线之间，即 row4..row5）
            if 0 < c < 8:
                p.drawLine(QPointF(x, oy), QPointF(x, oy + 4 * cell))
                p.drawLine(QPointF(x, oy + 5 * cell), QPointF(x, oy + 9 * cell))
            else:
                p.drawLine(QPointF(x, oy), QPointF(x, oy + 9 * cell))

        # 九宫斜线
        self._draw_palace(p, cell, ox, oy, 3, 0)   # 黑方九宫
        self._draw_palace(p, cell, ox, oy, 3, 7)   # 红方九宫

        # 楚河汉界文字
        p.setPen(self.RIVER_COLOR)
        font = p.font()
        font.setPointSize(max(10, int(cell * 0.32)))
        font.setBold(True)
        font.setFamily("KaiTi, STKaiti, serif")
        p.setFont(font)
        mid_y = oy + 4.5 * cell
        p.drawText(QPointF(ox + cell * 1.5, mid_y + cell * 0.12), "楚 河")
        p.drawText(QPointF(ox + cell * 5.5, mid_y + cell * 0.12), "漢 界")

        # 兵卒/炮位标记（小角标）
        self._draw_position_marks(p, cell, ox, oy)

        # 上一步高亮
        if self._last_move:
            for pos in self._last_move:
                self._draw_square_highlight(p, pos, cell, self.LAST_COLOR)

        # 建议走法高亮（绿色半透明方框）
        if self._suggest_move:
            for pos in self._suggest_move:
                self._draw_square_highlight(p, pos, cell, self._suggest_color, thick=True)

        # 选中高亮 + 合法落点
        if self._selected:
            self._draw_square_highlight(p, self._selected, cell, self.SELECT_COLOR)
            for t in self._legal_targets:
                self._draw_dot(p, t, cell)

        # 被将红框
        if self._check_pos:
            self._draw_square_highlight(p, self._check_pos, cell, self.CHECK_COLOR, thick=True)

        # 棋子（动画中的棋子最后画，置于顶层）
        anim_piece = self._anim["piece"] if self._anim else None
        anim_from = self._anim["frm"] if self._anim else None
        for r in range(10):
            for c in range(9):
                pos = Pos(r, c)
                piece = self.board.get(pos)
                if piece is None:
                    continue
                if anim_piece and pos == anim_from:
                    continue  # 动画中跳过原位
                self._draw_piece(p, pos, piece, cell)

        # 动画棋子
        if self._anim:
            self._draw_anim_piece(p, cell)

        # 上一步箭头 + 终点加粗边框（棋子之上，醒目标注最近一步）
        # 对方走（红）与自己走（橙）用不同颜色区分，方便定位对手刚落的子
        if self._last_move and not self._anim:
            frm, to = self._last_move
            is_foe = (self._last_move_by is not None
                      and self._last_move_by is not self.player_color)
            arrow_color = self.FOE_COLOR if is_foe else self.ARROW_COLOR
            self._draw_move_arrow(p, frm, to, cell, arrow_color)
            self._draw_thick_ring(p, to, cell, arrow_color, width=3)

        # 拖拽中的棋子跟随鼠标
        if self._dragging and self._drag_from is not None:
            piece = self.board.get(self._drag_from)
            if piece and self._drag_pos is not None:
                self._draw_piece_at(p, self._drag_pos, piece, cell)

    def _draw_palace(self, p, cell, ox, oy, col_start, row_start):
        x1 = ox + col_start * cell
        y1 = oy + row_start * cell
        x2 = ox + (col_start + 2) * cell
        y2 = oy + (row_start + 2) * cell
        p.drawLine(QPointF(x1, y1), QPointF(x2, y2))
        p.drawLine(QPointF(x2, y1), QPointF(x1, y2))

    def _draw_position_marks(self, p, cell, ox, oy):
        """在兵卒与炮的初始位置画小角标。"""
        marks = [(2, 1), (2, 7), (3, 0), (3, 2), (3, 4), (3, 6), (3, 8),
                 (7, 1), (7, 7), (6, 0), (6, 2), (6, 4), (6, 6), (6, 8)]
        p.setPen(QPen(self.LINE_COLOR, 1.2))
        d = cell * 0.12
        g = cell * 0.06
        for r, c in marks:
            if self.flipped:
                r, c = 9 - r, 8 - c
            x = ox + c * cell
            y = oy + r * cell
            # 四角小角标（边缘位置只画内侧）
            corners = [(-1, -1), (-1, 1), (1, -1), (1, 1)]
            for dx, dy in corners:
                # 棋盘边缘不画外侧
                if c == 0 and dx == -1:
                    continue
                if c == 8 and dx == 1:
                    continue
                cx = x + dx * g
                cy = y + dy * g
                p.drawLine(QPointF(cx, cy), QPointF(cx + dx * d, cy))
                p.drawLine(QPointF(cx, cy), QPointF(cx, cy + dy * d))

    def _draw_square_highlight(self, p, pos, cell, color, thick=False):
        center = self._to_pixel(pos)
        s = cell * 0.46
        rect = QRectF(center.x() - s, center.y() - s, s * 2, s * 2)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(color))
        p.drawRoundedRect(rect, 6, 6)
        if thick:
            p.setPen(QPen(QColor(231, 76, 60), 3))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(rect, 6, 6)

    def _draw_dot(self, p, pos, cell):
        center = self._to_pixel(pos)
        occupant = self.board.get(pos)
        r = cell * 0.14 if occupant is None else cell * 0.44
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(self.HINT_DOT))
        if occupant is None:
            p.drawEllipse(center, r, r)
        else:
            # 吃子目标：环形
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(self.HINT_DOT, 3))
            p.drawEllipse(center, r, r)

    def _draw_move_arrow(self, p, frm: Pos, to: Pos, cell: float, color: QColor):
        """画一条从 frm 指向 to 的箭头，醒目标注最近一步。
        两端缩短避免压住棋子；箭头头落在 to 棋子边缘外，刚好指向棋子。"""
        f = self._to_pixel(frm)
        t = self._to_pixel(to)
        dx, dy = t.x() - f.x(), t.y() - f.y()
        length = (dx * dx + dy * dy) ** 0.5
        if length < cell * 0.3:  # 距离太短不画箭头
            return
        ux, uy = dx / length, dy / length
        start = QPointF(f.x() + ux * cell * 0.38, f.y() + uy * cell * 0.38)
        end = QPointF(t.x() - ux * cell * 0.58, t.y() - uy * cell * 0.58)
        # 线段
        p.setPen(QPen(color, 2.8))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawLine(start, end)
        # 箭头头（三角形）
        head_len = cell * 0.20
        head_w = cell * 0.13
        base = QPointF(end.x() - ux * head_len, end.y() - uy * head_len)
        px, py = -uy, ux  # 垂直方向
        left = QPointF(base.x() + px * head_w, base.y() + py * head_w)
        right = QPointF(base.x() - px * head_w, base.y() - py * head_w)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(color))
        p.drawPolygon(QPolygonF([end, left, right]))

    def _draw_thick_ring(self, p, pos: Pos, cell: float, color: QColor, width: float = 3):
        """在格子周围画加粗圆角边框（醒目标注，不遮挡棋子文字）。"""
        center = self._to_pixel(pos)
        s = cell * 0.46
        rect = QRectF(center.x() - s, center.y() - s, s * 2, s * 2)
        p.setPen(QPen(color, width))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(rect, 6, 6)

    def _draw_piece(self, p, pos, piece, cell):
        center = self._to_pixel(pos)
        self._draw_piece_at(p, center, piece, cell)

    def _draw_piece_at(self, p, center, piece, cell):
        radius = cell * 0.44
        # 阴影
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(0, 0, 0, 60)))
        p.drawEllipse(center + QPointF(2, 3), radius, radius)

        # 棋子底（径向渐变营造立体感）
        grad = QRadialGradient(center.x() - radius * 0.3, center.y() - radius * 0.3, radius * 1.4)
        grad.setColorAt(0, QColor(255, 250, 235))
        grad.setColorAt(1, self.DISC_FILL.darker(112))
        p.setBrush(QBrush(grad))
        p.setPen(QPen(QColor(180, 150, 100), 1.2))
        p.drawEllipse(center, radius, radius)

        # 己方棋子金环（玩家视角提示：这枚是你可以动的棋）
        if piece.color is self.player_color:
            p.setPen(QPen(self.MINE_RING, 3.6))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(center, radius * 1.02, radius * 1.02)

        # 内圈色环
        color = self.RED_PIECE if piece.color is Color.RED else self.BLACK_PIECE
        p.setPen(QPen(color, 2.2))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(center, radius * 0.82, radius * 0.82)

        # 文字
        p.setPen(color)
        font = QFont("KaiTi, STKaiti, SimSun, serif", int(cell * 0.42))
        font.setBold(True)
        p.setFont(font)
        p.drawText(QRectF(center.x() - radius, center.y() - radius, radius * 2, radius * 2),
                   Qt.AlignmentFlag.AlignCenter, piece.name_cn)

    # ---------------- 动画 ----------------
    def _start_anim(self, frm: Pos, to: Pos):
        piece = self.board.get(to)  # 走子后棋子已位于 to
        if piece is None:
            self.update()
            return
        self._anim = {"piece": piece, "frm": frm, "to": to, "t0": 0.0, "dur": 180}
        self._timer.start(16)

    def _tick_anim(self):
        if not self._anim:
            self._timer.stop()
            return
        self._anim["t0"] += 16 / 1000.0
        if self._anim["t0"] >= self._anim["dur"] / 1000.0:
            self._anim = None
            self._timer.stop()
            # 动画确实结束 → 发信号。外部有 QTimer 兜底，所以不会造成"卡死"。
            self.animFinished.emit()
        self.update()

    def _draw_anim_piece(self, p, cell):
        a = self._anim
        t = min(1.0, a["t0"] / (a["dur"] / 1000.0))
        # ease-out
        t = 1 - (1 - t) ** 3
        f = self._to_pixel(a["frm"])
        to = self._to_pixel(a["to"])
        cx = f.x() + (to.x() - f.x()) * t
        cy = f.y() + (to.y() - f.y()) * t
        self._draw_piece_at(p, QPointF(cx, cy), a["piece"], cell)

    # ---------------- 内部刷新 ----------------
    def _refresh_check(self):
        self._check_pos = None
        if rules.is_in_check(self.board, self.board.side):
            kp = self.board.king_pos(self.board.side)
            if kp:
                self._check_pos = kp

    def _recompute_legal(self):
        if self._selected is None:
            self._legal_targets = []
            return
        # 仅展示该子合法走法
        targets = []
        for t in rules.pseudo_targets(self.board, self._selected):
            captured = self.board.make_move(self._selected, t)
            ok = not rules.is_in_check(self.board, self.board.side.opponent)
            self.board.undo_move()
            if ok:
                targets.append(t)
        self._legal_targets = targets

    # ---------------- 鼠标交互 ----------------
    def mousePressEvent(self, event):
        # --- 自我救赎 override（永久保留）：不依赖外部 interactive/_thinking 标志 ---
        # 根因教训：Pikafish QThread 卡死或竞争丢事件时，interactive/_thinking 会失真，
        # 但 board.side（"现在该谁下"）永远是唯一真相→直接用它判定。
        side = self.board.side
        real_turn = (side is self.player_color)

        if self._anim:
            self.hintMessage.emit("⏳ 棋子动画中，稍等一下再操作（最多 0.2 秒）")
            return
        if not real_turn:
            you_txt = self.player_color.name_cn
            side_txt = side.name_cn
            engine_txt = "引擎走棋中…" if side is not self.player_color else ""
            self.hintMessage.emit(
                f"💡 现在轮到{side_txt}方走 {engine_txt}（你执{you_txt}），等对方走完再点你的棋")
            return
        if not self.interactive:
            self.interactive = True

        pos = self._from_pixel(event.position())
        if pos is None:
            return
        piece = self.board.get(pos)

        if self._selected and pos in self._legal_targets:
            self._emit_move(self._selected, pos)
            return
        if piece is not None:
            if piece.color is not self.player_color:
                self.hintMessage.emit(
                    f"❌ 你点的这枚「{piece.name_cn}」是「{piece.color.name_cn}方」的（对方棋）！"
                    f"请只点带金黄色外环的己方{self.player_color.name_cn}子。")
                self._selected = None
                self._legal_targets = []
                self.update()
                return
            if piece.color is not self.board.side:
                self.hintMessage.emit(
                    f"⚠ 现在轮到{self.board.side.name_cn}方走，还没轮到你走你的{self.player_color.name_cn}子")
                self._selected = None
                self._legal_targets = []
                self.update()
                return
            self._selected = pos
            self._recompute_legal()
            self._dragging = True
            self._drag_from = pos
            self._drag_pos = event.position()
            self.update()
            return
        self._selected = None
        self._legal_targets = []
        self.update()


    def mouseMoveEvent(self, event):
        if self._dragging:
            self._drag_pos = event.position()
            self.update()

    def mouseReleaseEvent(self, event):
        if not self._dragging:
            return
        self._dragging = False
        self._drag_pos = None
        target = self._from_pixel(event.position())
        if target and self._drag_from and target != self._drag_from and target in self._legal_targets:
            self._emit_move(self._drag_from, target)
        else:
            self.update()

    def _emit_move(self, frm: Pos, to: Pos):
        f = frm
        t = to
        self._selected = None
        self._legal_targets = []
        self.moveAttempted.emit(f.row, f.col, t.row, t.col)
