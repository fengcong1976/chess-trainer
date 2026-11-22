"""走法生成与合法性校验（完整中国象棋规则）。

实现要点：
    将/帅：九宫内一步直行 + 飞将照面（同列无子可直接擒王，视作将军）
    士/仕：九宫内一步斜行
    象/相：田字斜行、不过河、塞象眼禁行
    马：日字走法、蹩马腿禁行
    车：直线无阻滑行
    炮：移动同车；吃子须越一子（炮架）
    兵/卒：未过河仅前进；过河后可左右行，永不后退
    合法性：走子后己方不被将军（含飞将照面）方为合法
"""
from typing import List, Optional, Tuple

from .board import Board
from .pieces import Piece, Color, PieceType
from .position import Pos, parse_ucci_move

# 方向向量
ORTHO = [Pos(-1, 0), Pos(1, 0), Pos(0, -1), Pos(0, 1)]
DIAG = [Pos(-1, -1), Pos(-1, 1), Pos(1, -1), Pos(1, 1)]
# 马的8个走法: (目标偏移, 蹩马腿偏移)
HORSE_MOVES = [
    (Pos(-2, -1), Pos(-1, 0)), (Pos(-2, 1), Pos(-1, 0)),
    (Pos(2, -1), Pos(1, 0)), (Pos(2, 1), Pos(1, 0)),
    (Pos(-1, -2), Pos(0, -1)), (Pos(1, -2), Pos(0, -1)),
    (Pos(-1, 2), Pos(0, 1)), (Pos(1, 2), Pos(0, 1)),
]


def _forward(color: Color) -> Pos:
    """兵卒前进方向：红方向上(row-1)，黑方向下(row+1)"""
    return Pos(-1, 0) if color is Color.RED else Pos(1, 0)


def pseudo_targets(board: Board, pos: Pos) -> List[Pos]:
    """生成某棋子的伪合法目标格点（不检查走后是否被将军）。"""
    piece = board.get(pos)
    if piece is None:
        return []
    color, pt = piece.color, piece.type
    targets: List[Pos] = []

    if pt is PieceType.KING:
        # 九宫内一步直行
        for d in ORTHO:
            t = pos + d
            if t.in_board() and t.in_palace(color):
                targets.append(t)
        # 飞将照面：沿列找首个棋子，若为敌方将则可擒
        for dr in (-1, 1):
            r = pos.row + dr
            while 0 <= r <= 9:
                p = board.get(Pos(r, pos.col))
                if p is not None:
                    if p.type is PieceType.KING and p.color is not color:
                        targets.append(Pos(r, pos.col))
                    break
                r += dr

    elif pt is PieceType.ADVISOR:
        for d in DIAG:
            t = pos + d
            if t.in_board() and t.in_palace(color):
                targets.append(t)

    elif pt is PieceType.ELEPHANT:
        for d in DIAG:
            t = pos + Pos(d.row * 2, d.col * 2)
            eye = pos + d  # 象眼
            if t.in_board() and t.in_own_half(color) and board.get(eye) is None:
                targets.append(t)

    elif pt is PieceType.HORSE:
        for t, leg in HORSE_MOVES:
            target = pos + t
            leg_pos = pos + leg
            if target.in_board() and board.get(leg_pos) is None:
                targets.append(target)

    elif pt is PieceType.ROOK:
        for d in ORTHO:
            t = pos + d
            while t.in_board():
                occupant = board.get(t)
                if occupant is None:
                    targets.append(t)
                else:
                    if occupant.color is not color:
                        targets.append(t)
                    break
                t = t + d

    elif pt is PieceType.CANNON:
        for d in ORTHO:
            t = pos + d
            # 阶段1：移动到空格（未越架）
            while t.in_board() and board.get(t) is None:
                targets.append(t)
                t = t + d
            # 阶段2：越过炮架后吃子
            if t.in_board():
                t = t + d  # 跳过炮架
                while t.in_board():
                    occupant = board.get(t)
                    if occupant is not None:
                        if occupant.color is not color:
                            targets.append(t)
                        break
                    t = t + d

    elif pt is PieceType.PAWN:
        fwd = _forward(color)
        # 前进一步
        t = pos + fwd
        if t.in_board():
            targets.append(t)
        # 过河后可横走
        if pos.crossed_river(color):
            for sd in (Pos(0, -1), Pos(0, 1)):
                t = pos + sd
                if t.in_board():
                    targets.append(t)

    # 过滤掉落到己方棋子上的目标（吃子除外已处理，这里统一兜底）
    return [t for t in targets if board.get(t) is None or board.get(t).color is not color]


def is_in_check(board: Board, color: Color) -> bool:
    """color 方的将/帅是否正被将军（含飞将照面）。"""
    king_p = board.king_pos(color)
    if king_p is None:
        return True  # 无将视为已被将死
    enemy = color.opponent
    for pos, _ in board.pieces_of(enemy):
        if king_p in pseudo_targets(board, pos):
            return True
    return False


def is_legal(board: Board, frm: Pos, to: Pos) -> bool:
    """判定一步走法是否合法（含走后不被将军）。"""
    piece = board.get(frm)
    if piece is None or piece.color is not board.side:
        return False
    if to not in pseudo_targets(board, frm):
        return False
    # 试走，检查己方是否被将军
    captured = board.make_move(frm, to)
    in_check = is_in_check(board, piece.color)
    board.undo_move()
    return not in_check


def generate_legal_moves(board: Board, color: Optional[Color] = None) -> List[Tuple[Pos, Pos]]:
    """生成某方全部合法走法列表。"""
    if color is None:
        color = board.side
    moves: List[Tuple[Pos, Pos]] = []
    for pos, piece in list(board.pieces_of(color)):
        for t in pseudo_targets(board, pos):
            captured = board.make_move(pos, t)
            if not is_in_check(board, color):
                moves.append((pos, t))
            board.undo_move()
    return moves


def has_legal_move(board: Board, color: Optional[Color] = None) -> bool:
    """是否还有合法走法（无则该方告负，象棋中无棋可走即败）。"""
    color = color or board.side
    for pos, piece in list(board.pieces_of(color)):
        for t in pseudo_targets(board, pos):
            captured = board.make_move(pos, t)
            ok = not is_in_check(board, color)
            board.undo_move()
            if ok:
                return True
    return False


def is_checkmate(board: Board, color: Optional[Color] = None) -> bool:
    """是否被将死：被将军且无合法走法。"""
    color = color or board.side
    return is_in_check(board, color) and not has_legal_move(board, color)


def is_legal_ucci(board: Board, ucci: str) -> bool:
    """判定 UCCI 走法串是否合法。"""
    try:
        frm, to = parse_ucci_move(ucci)
    except ValueError:
        return False
    return is_legal(board, frm, to)
