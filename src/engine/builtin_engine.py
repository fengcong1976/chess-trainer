"""内置回退引擎：极小化极大 + Alpha-Beta 剪枝（迭代加深 + 时间预算）。

当未检测到 Pikafish 可执行文件时使用。棋力较弱（业余初级），
但保证软件开箱即用、无外部依赖。采用迭代加深并在时间预算内返回当前最佳走法，
避免在满子局面下长时间卡顿。建议放置 Pikafish 以获得最佳体验。
"""
import time
import random
from typing import Optional, Tuple

from ..board.board import Board
from ..board.pieces import Color, PieceType
from ..board.position import Pos
from ..board import rules

# 棋子基础价值（兵=100 为基准）
PIECE_VALUE = {
    PieceType.KING: 100000,
    PieceType.ROOK: 900,
    PieceType.CANNON: 450,
    PieceType.HORSE: 400,
    PieceType.ADVISOR: 120,
    PieceType.ELEPHANT: 100,
    PieceType.PAWN: 100,
}

MATE = 100000  # 将杀分


def _evaluate(board: Board) -> int:
    """静态评估：从红方视角（红正、黑负）。"""
    score = 0
    for r in range(board.ROWS):
        for c in range(board.COLS):
            p = board.get(Pos(r, c))
            if p is None:
                continue
            val = PIECE_VALUE[p.type]
            if p.type is PieceType.PAWN and Pos(r, c).crossed_river(p.color):
                val += 50
            if p.type is PieceType.HORSE and 3 <= c <= 5:
                val += 10
            score += val if p.color is Color.RED else -val
    return score


def _order_moves(board: Board, moves):
    """走法排序：吃子优先（MVV-LVA），提升剪枝效率。"""

    def cap_val(m):
        frm, to = m
        victim = board.get(to)
        attacker = board.get(frm)
        if victim is None:
            return 0
        return PIECE_VALUE[victim.type] * 10 - PIECE_VALUE[attacker.type]

    return sorted(moves, key=cap_val, reverse=True)


class _Searcher:
    """带时间预算的负极大值搜索。超时后通过 TimeUp 异常 unwind。"""

    class TimeUp(Exception):
        pass

    def __init__(self, deadline: float):
        self.deadline = deadline
        self.nodes = 0

    def negamax(self, board: Board, depth: int, alpha: int, beta: int, ply: int) -> int:
        self.nodes += 1
        if (self.nodes & 1023) == 0 and time.time() > self.deadline:
            raise self.TimeUp
        color = board.side
        moves = rules.generate_legal_moves(board, color)
        if not moves:
            return -MATE + ply  # 无棋可走即负
        if depth == 0:
            ev = _evaluate(board)
            return ev if color is Color.RED else -ev

        best = -MATE * 2
        for frm, to in _order_moves(board, moves):
            board.make_move(frm, to)
            try:
                score = -self.negamax(board, depth - 1, -beta, -alpha, ply + 1)
            finally:
                board.undo_move()
            if score > best:
                best = score
            if best > alpha:
                alpha = best
            if alpha >= beta:
                break
        return best


class BuiltinEngine:
    """内置极简引擎（迭代加深 + 时间预算）"""

    name = "内置引擎（Python Minimax）"
    is_ready = True

    def __init__(self):
        self._max_depth = 4   # 最高搜索深度上限
        self._time_ms = 1200  # 单步时间预算（毫秒）

    def set_strength(self, level: int):
        """对手棋力 1-20 级：调节深度上限与时间预算。低级更弱更快。"""
        level = max(1, min(20, int(level)))
        self._max_depth = 1 + round((level - 1) * 5 / 19)   # 1→1层 ... 20→6层
        self._time_ms = min(200 + level * 140, 3000)        # 1≈340ms ... 20=3000ms

    def set_difficulty(self, level: int):
        """旧接口（1-5 档），线性映射到 20 级制。"""
        self.set_strength(int(level) * 4)

    def best_move(self, board: Board) -> Optional[Tuple[Pos, Pos]]:
        color = board.side
        moves = rules.generate_legal_moves(board, color)
        if not moves:
            return None
        if len(moves) == 1:
            return moves[0]

        deadline = time.time() + self._time_ms / 1000.0
        ordered = _order_moves(board, moves)
        best_move = ordered[0]
        best_score = -MATE * 2

        # 迭代加深：从深度1开始，逐层加深，超时则返回上一深度最佳
        for depth in range(1, self._max_depth + 1):
            searcher = _Searcher(deadline)
            alpha, beta = -MATE * 2, MATE * 2
            current_best, current_score = None, -MATE * 2
            try:
                # 把上一轮最佳走法排在最前，提升剪枝
                ordered = ([best_move] + [m for m in ordered if m != best_move]) if best_move else ordered
                for frm, to in ordered:
                    board.make_move(frm, to)
                    try:
                        score = -searcher.negamax(board, depth - 1, -beta, -alpha, 1)
                    finally:
                        board.undo_move()
                    if score > current_score:
                        current_score = score
                        current_best = (frm, to)
                    if current_score > alpha:
                        alpha = current_score
            except _Searcher.TimeUp:
                # 超时：若本层已得到改进则采用，否则沿用上一深度结果
                if current_best is not None and current_score > best_score:
                    best_move, best_score = current_best, current_score
                break
            else:
                if current_best is not None:
                    best_move, best_score = current_best, current_score
            if time.time() > deadline:
                break

        return best_move

    def analyze(self, board: Board, movetime_ms: int = 1500) -> dict:
        """返回 {move, score_cp, bestmove, pv}。score_cp 为红方视角厘兵分。"""
        # 临时用 movetime 调整本次搜索预算
        old_time = self._time_ms
        self._time_ms = max(200, movetime_ms)
        try:
            move = self.best_move(board)
        finally:
            self._time_ms = old_time
        score_cp = _evaluate(board)
        bestmove_ucci = None
        if move:
            bestmove_ucci = f"{move[0].to_ucci()}{move[1].to_ucci()}"
        return {"move": move, "score_cp": score_cp, "bestmove": bestmove_ucci, "pv": []}

    def shutdown(self):
        pass

