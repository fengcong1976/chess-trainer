"""UCCI 走法 → 中文纵线记法（如 "h2e2" -> "炮二平五"）。

记谱规则：
    红方用中文数字一~九，从红方视角右起（col 8 -> 一，col 0 -> 九）；
    黑方用阿拉伯数字 1~9，从黑方视角右起（col 0 -> 1，col 8 -> 9）。
    红方进 = row 减小（向上）；黑方进 = row 增大（向下）。
    平：rank 不变，目标用纵线数字。
    车/炮/兵(卒)/将(帅) 直行：进/退 后跟步数数字。
    马/相(象)/仕(士) 斜行：进/退 后跟目标纵线数字。
    同线同型双子加"前/后"（前 = 更靠近敌方）。
"""
from .board import Board
from .pieces import Color, PieceType
from .position import Pos, move_to_ucci

_CN_NUM = "一二三四五六七八九"
_PT_CN = {
    PieceType.KING: ("帅", "将"),
    PieceType.ADVISOR: ("仕", "士"),
    PieceType.ELEPHANT: ("相", "象"),
    PieceType.HORSE: ("马", "马"),
    PieceType.ROOK: ("车", "车"),
    PieceType.CANNON: ("炮", "炮"),
    PieceType.PAWN: ("兵", "卒"),
}
_DIAGONAL_TYPES = (PieceType.HORSE, PieceType.ELEPHANT, PieceType.ADVISOR)


def _num(col: int, red: bool) -> str:
    """纵线数字：红方中文、黑方阿拉伯。"""
    return _CN_NUM[8 - col] if red else str(col + 1)


def _step(n: int, red: bool) -> str:
    """步数数字：红方中文、黑方阿拉伯。"""
    return _CN_NUM[n - 1] if red else str(n)


def _side_prefix(board: Board, frm: Pos, piece) -> str:
    """同线（同列）同型同色双子时的 前/后 消歧前缀。"""
    others = []
    for r in range(10):
        p = board.get(Pos(r, frm.col))
        if Pos(r, frm.col) != frm and p is not None \
                and p.color is piece.color and p.type is piece.type:
            others.append(r)
    if not others:
        return ""
    red = piece.color is Color.RED
    # 前 = 更靠近敌方：红 row 越小越前，黑 row 越大越前
    if red:
        return "前" if frm.row < min(others) else "后"
    return "前" if frm.row > max(others) else "后"


def move_to_chinese(board: Board, frm: Pos, to: Pos) -> str:
    """把一步走法转为中文纵线记法，如 '炮二平五'。"""
    piece = board.get(frm)
    if piece is None:
        return move_to_ucci(frm, to)
    red = piece.color is Color.RED
    pt_cn = _PT_CN[piece.type][0 if red else 1]
    prefix = _side_prefix(board, frm, piece)

    if to.row == frm.row:  # 平
        return f"{prefix}{pt_cn}{_num(frm.col, red)}平{_num(to.col, red)}"

    forward = (to.row < frm.row) if red else (to.row > frm.row)
    verb = "进" if forward else "退"
    if piece.type in _DIAGONAL_TYPES:  # 斜行子：目标纵线
        target = _num(to.col, red)
    else:  # 直行子：步数
        target = _step(abs(to.row - frm.row), red)
    return f"{prefix}{pt_cn}{_num(frm.col, red)}{verb}{target}"
