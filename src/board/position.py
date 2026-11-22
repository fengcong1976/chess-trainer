"""坐标系统与 UCCI 转换。

坐标约定（内部使用 (row, col)）：
    row 0 = 最上方 = 黑方底线；row 9 = 最下方 = 红方底线
    col 0..8 从左到右
    楚河汉界位于 row4 与 row5 之间：row0~4 黑方区域，row5~9 红方区域

UCCI 走法记号：每个格点用 "文件字母+数字" 表示，如 "h2e2"。
    文件 a..i 对应 col 0..8
    数字 0..9：0=红方底线(我们的row9)，9=黑方底线(我们的row0)
    故 UCCI rank r <-> 我们的 row (9-r)
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Pos:
    """棋盘上一个格点坐标，也兼作方向/偏移向量（可含负数）。
    合法性由使用方按需通过 in_board() 判定，构造时不强制校验，
    以便表示如 (-1, 0) 的方向向量。
    """
    row: int
    col: int

    # ---- 基本运算 ----
    def __add__(self, other):
        return Pos(self.row + other.row, self.col + other.col)

    def __sub__(self, other):
        return Pos(self.row - other.row, self.col - other.col)

    def in_board(self) -> bool:
        return 0 <= self.row <= 9 and 0 <= self.col <= 8

    # ---- 区域判定 ----
    def in_palace(self, color) -> bool:
        """是否在九宫格内。color 为 Color 枚举"""
        if not (3 <= self.col <= 5):
            return False
        # 红方九宫 row7~9，黑方九宫 row0~2
        if color.value == "r":  # 红方
            return 7 <= self.row <= 9
        return 0 <= self.row <= 2

    def in_own_half(self, color) -> bool:
        """是否在己方半场（不过河）"""
        if color.value == "r":  # 红方
            return 5 <= self.row <= 9
        return 0 <= self.row <= 4

    def crossed_river(self, color) -> bool:
        """是否已过河进入对方半场"""
        if color.value == "r":  # 红方向上进攻，过河 = row<=4
            return self.row <= 4
        return self.row >= 5

    # ---- UCCI 转换 ----
    def to_ucci(self) -> str:
        """转为 UCCI 格点串，如 'e0'"""
        file_char = chr(ord('a') + self.col)
        rank = 9 - self.row  # UCCI rank 0 在红方
        return f"{file_char}{rank}"

    @classmethod
    def from_ucci(cls, s: str) -> "Pos":
        """从 UCCI 格点串解析，如 'e0' -> Pos(9, 4)"""
        s = s.strip()
        file_char, rank_char = s[0].lower(), s[1]
        col = ord(file_char) - ord('a')
        rank = int(rank_char)
        row = 9 - rank
        return cls(row, col)


def move_to_ucci(frm: Pos, to: Pos) -> str:
    """把一步走法转为 UCCI 记号，如 'h2e2'"""
    return frm.to_ucci() + to.to_ucci()


def parse_ucci_move(s: str):
    """解析 UCCI 走法串 'h2e2' -> (Pos, Pos)"""
    s = s.strip().lower()
    if len(s) < 4:
        raise ValueError(f"非法 UCCI 走法: {s}")
    return Pos.from_ucci(s[:2]), Pos.from_ucci(s[2:4])
