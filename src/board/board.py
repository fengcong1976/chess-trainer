"""棋盘状态：10x9 格点、FEN 解析/序列化、走子与悔子、回合管理。

FEN 格式（象棋扩展）：
    <盘面> <轮走方> <回合数>
    盘面从黑方底线(row0)到红方底线(row9)逐行书写，数字表示连续空格。
    例：rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR w - - 0 1
"""
from typing import Optional, List, Tuple

from .pieces import Piece, Color, PieceType
from .position import Pos


class Board:
    """棋盘状态机"""

    ROWS = 10
    COLS = 9

    def __init__(self):
        # grid[row][col] = Piece 或 None
        self._grid: List[List[Optional[Piece]]] = [[None] * self.COLS for _ in range(self.ROWS)]
        self.side: Color = Color.RED          # 轮走方
        self.move_history: List[Tuple[Pos, Pos, Optional[Piece]]] = []  # (from, to, captured)
        self.round_number: int = 1            # 回合数（红走一次+黑走一次为一回合）

    # ---- 基本存取 ----
    def get(self, pos: Pos) -> Optional[Piece]:
        if not pos.in_board():
            return None
        return self._grid[pos.row][pos.col]

    def set(self, pos: Pos, piece: Optional[Piece]):
        self._grid[pos.row][pos.col] = piece

    def pieces_of(self, color: Color):
        """枚举某方所有棋子位置"""
        for r in range(self.ROWS):
            for c in range(self.COLS):
                p = self._grid[r][c]
                if p is not None and p.color is color:
                    yield Pos(r, c), p

    def king_pos(self, color: Color) -> Optional[Pos]:
        for pos, p in self.pieces_of(color):
            if p.type is PieceType.KING:
                return pos
        return None

    def clone(self) -> "Board":
        b = Board()
        for r in range(self.ROWS):
            for c in range(self.COLS):
                p = self._grid[r][c]
                b._grid[r][c] = Piece(p.color, p.type) if p else None
        b.side = self.side
        b.round_number = self.round_number
        b.move_history = list(self.move_history)
        return b

    # ---- 走子 ----
    def make_move(self, frm: Pos, to: Pos) -> Optional[Piece]:
        """执行走子，返回被吃棋子（或 None）。不校验合法性。"""
        moving = self.get(frm)
        captured = self.get(to)
        self.set(frm, None)
        self.set(to, moving)
        self.move_history.append((frm, to, captured))
        # 切换轮走方；红方走完进入新回合
        if self.side is Color.BLACK:
            self.round_number += 1
        self.side = self.side.opponent
        return captured

    def undo_move(self):
        """悔一步"""
        if not self.move_history:
            return
        frm, to, captured = self.move_history.pop()
        moving = self.get(to)
        self.set(frm, moving)
        self.set(to, captured)
        # 还原轮走方与回合数
        if self.side is Color.RED:  # 当前是红走，说明悔的是黑方那步
            self.round_number = max(1, self.round_number - 1)
        self.side = self.side.opponent

    # ---- FEN ----
    @classmethod
    def start_position(cls) -> "Board":
        return cls.from_fen("rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR w - - 0 1")

    @classmethod
    def from_fen(cls, fen: str) -> "Board":
        b = cls()
        parts = fen.strip().split()
        board_part = parts[0]
        rows = board_part.split("/")
        if len(rows) != 10:
            raise ValueError(f"FEN 盘面行数错误: {fen}")
        for row_idx, row_str in enumerate(rows):
            col = 0
            for ch in row_str:
                if ch.isdigit():
                    col += int(ch)
                else:
                    b._grid[row_idx][col] = Piece.from_fen(ch)
                    col += 1
            if col != 9:
                raise ValueError(f"FEN 第{row_idx}行列数错误: {row_str}")
        if len(parts) > 1:
            b.side = Color.RED if parts[1] == "w" else Color.BLACK
        # 回合数通常在最后
        if len(parts) >= 6 and parts[5].isdigit():
            b.round_number = int(parts[5])
        return b

    def to_fen(self) -> str:
        rows_str = []
        for r in range(self.ROWS):
            row_str = ""
            empty = 0
            for c in range(self.COLS):
                p = self._grid[r][c]
                if p is None:
                    empty += 1
                else:
                    if empty:
                        row_str += str(empty)
                        empty = 0
                    row_str += p.fen_char
            if empty:
                row_str += str(empty)
            rows_str.append(row_str)
        side = "w" if self.side is Color.RED else "b"
        return f"{'/'.join(rows_str)} {side} - - 0 {self.round_number}"

    def __repr__(self):
        return f"Board(side={self.side.name_cn}, fen={self.to_fen()!r})"
