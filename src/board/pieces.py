"""棋子定义：颜色、种类、中文显示名、FEN字符映射。

FEN 约定：大写=红方，小写=黑方。
    K/k 将帅  A/a 士仕  B/b 象相  N/n 马  R/r 车  C/c 炮  P/p 兵卒
"""
from enum import Enum


class Color(Enum):
    """棋子所属方"""
    RED = "r"   # 红方（下方，先手）
    BLACK = "b"  # 黑方（上方，后手）

    @property
    def opponent(self):
        return Color.BLACK if self is Color.RED else Color.RED

    @property
    def name_cn(self):
        return "红方" if self is Color.RED else "黑方"


class PieceType(Enum):
    """棋子种类"""
    KING = "K"       # 将/帅
    ADVISOR = "A"    # 士/仕
    ELEPHANT = "B"   # 象/相
    HORSE = "N"      # 马
    ROOK = "R"       # 车
    CANNON = "C"     # 炮
    PAWN = "P"       # 兵/卒

    @property
    def char_red(self):
        return self.value  # 大写

    @property
    def char_black(self):
        return self.value.lower()


# 红方棋子显示名（中国象棋传统写法）
_RED_NAMES = {
    PieceType.KING: "帅",
    PieceType.ADVISOR: "仕",
    PieceType.ELEPHANT: "相",
    PieceType.HORSE: "马",
    PieceType.ROOK: "车",
    PieceType.CANNON: "炮",
    PieceType.PAWN: "兵",
}

# 黑方棋子显示名
_BLACK_NAMES = {
    PieceType.KING: "将",
    PieceType.ADVISOR: "士",
    PieceType.ELEPHANT: "象",
    PieceType.HORSE: "马",
    PieceType.ROOK: "车",
    PieceType.CANNON: "砲",
    PieceType.PAWN: "卒",
}

# FEN 字符 -> PieceType
_FEN_TO_TYPE = {
    "K": PieceType.KING, "k": PieceType.KING,
    "A": PieceType.ADVISOR, "a": PieceType.ADVISOR,
    "B": PieceType.ELEPHANT, "b": PieceType.ELEPHANT,
    "N": PieceType.HORSE, "n": PieceType.HORSE,
    "R": PieceType.ROOK, "r": PieceType.ROOK,
    "C": PieceType.CANNON, "c": PieceType.CANNON,
    "P": PieceType.PAWN, "p": PieceType.PAWN,
}


class Piece:
    """一枚棋子：颜色 + 种类"""

    __slots__ = ("color", "type")

    def __init__(self, color: Color, ptype: PieceType):
        self.color = color
        self.type = ptype

    @property
    def name_cn(self) -> str:
        return (_RED_NAMES if self.color is Color.RED else _BLACK_NAMES)[self.type]

    @property
    def fen_char(self) -> str:
        """FEN 字符：红方大写，黑方小写"""
        return self.type.value if self.color is Color.RED else self.type.value.lower()

    @classmethod
    def from_fen(cls, ch: str) -> "Piece":
        ptype = _FEN_TO_TYPE[ch]
        color = Color.RED if ch.isupper() else Color.BLACK
        return cls(color, ptype)

    def __eq__(self, other):
        return isinstance(other, Piece) and self.color is other.color and self.type is other.type

    def __hash__(self):
        return hash((self.color, self.type))

    def __repr__(self):
        return f"Piece({self.color.name_cn}{self.name_cn})"
