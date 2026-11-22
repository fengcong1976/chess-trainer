"""残局题目发现与验证脚本（开发辅助）。

提供精确的杀棋求解器（穷举 minimax，非评估引擎），
对候选 FEN 判定红方是否存在强制杀棋及其步数，作为题库可靠来源。

运行： python -m src.training.discover_puzzles
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.board.board import Board
from src.board.pieces import Color, PieceType
from src.board.position import Pos, move_to_ucci
from src.board import rules


def find_mate_in_one(fen: str):
    board = Board.from_fen(fen)
    if rules.is_in_check(board, Color.BLACK):
        return [], "黑方已被将军（起始位置非法）"
    if board.side is not Color.RED:
        return [], "非红方走"
    mating = []
    for frm, to in rules.generate_legal_moves(board, Color.RED):
        board.make_move(frm, to)
        if rules.is_checkmate(board, Color.BLACK):
            mating.append(move_to_ucci(frm, to))
        board.undo_move()
    return mating, "ok"


def forced_mate_depth(fen: str, max_plies: int = 5):
    """精确判断红方是否存在强制杀棋，返回最小杀棋步数(杀几步)及一招。无则返回 None。"""
    board = Board.from_fen(fen)
    nodes = [0]
    BUDGET = 2_000_000  # 节点预算，超过则放弃（防止组合爆炸）

    def search(b: Board, plies_left: int):
        nodes[0] += 1
        if nodes[0] > BUDGET:
            return False, 0
        # 当前轮走方若无棋可走，则其告负
        if not rules.has_legal_move(b, b.side):
            return True, 0  # 已被将杀/困毙
        if plies_left == 0:
            return False, 0
        mover = b.side
        if mover is Color.RED:
            # 红方任一走法能在剩余plies内杀掉黑即算成功
            for frm, to in rules.generate_legal_moves(b, Color.RED):
                b.make_move(frm, to)
                mated, _ = search(b, plies_left - 1)
                b.undo_move()
                if mated:
                    return True, plies_left
            return False, 0
        else:
            # 黑方需所有走法均被杀（强制杀）
            for frm, to in rules.generate_legal_moves(b, Color.BLACK):
                b.make_move(frm, to)
                mated, _ = search(b, plies_left - 1)
                b.undo_move()
                if not mated:
                    return False, 0
            return True, plies_left

    # 红先，杀N步 = 红走N步，中间黑走N-1步，共 2N-1 plies
    for steps in range(1, max_plies + 1):
        plies = 2 * steps - 1
        for frm, to in rules.generate_legal_moves(board, Color.RED):
            board.make_move(frm, to)
            mated, _ = search(board, plies - 1)
            board.undo_move()
            if mated:
                return steps, move_to_ucci(frm, to)
        if nodes[0] > BUDGET:
            return None, None  # 放弃
    return None, None


# 候选残局（红先）。所有FEN确保红黑两将不在同列（避免飞将起手非法）。
CANDIDATES = {
    # —— 已确认的合法一步杀 ——
    "双车错-左角": "3k5/9/8R/9/9/4R4/9/9/9/4K4 w - - 0 1",
    "双车错-右角": "5k3/9/R8/9/9/4R4/9/9/9/3K5 w - - 0 1",
    # —— 马后炮（炮借马为架，马控两肋）——
    "马后炮": "4k4/9/4N4/9/8C/9/9/9/9/3K5 w - - 0 1",
    # —— 铁门栓（炮镇中线，黑卒为架，车守肋）——
    "铁门栓": "3ak4/4p4/9/8C/9/5R3/9/9/9/3K5 w - - 0 1",
    # —— 车兵两步杀 ——
    "车兵-两步杀": "4k4/9/9/9/9/9/9/4P4/4R4/3K5 w - - 0 1",
    # —— 双车两步杀（将居中需先逼位）——
    "双车错-两步杀": "4k4/9/9/9/9/9/4R4/9/8R/3K5 w - - 0 1",
}


def mate_line(fen: str, max_plies: int = 5):
    """返回强制杀棋的主变走法串列表（UCCI），如 ['e2d2','e9d9','d2d9']。无则空。"""
    board = Board.from_fen(fen)
    nodes = [0]
    BUDGET = 2_000_000

    def search(b: Board, plies_left: int):
        nodes[0] += 1
        if nodes[0] > BUDGET:
            return False
        if not rules.has_legal_move(b, b.side):
            return True
        if plies_left == 0:
            return False
        mover = b.side
        if mover is Color.RED:
            for frm, to in rules.generate_legal_moves(b, Color.RED):
                b.make_move(frm, to)
                ok = search(b, plies_left - 1)
                b.undo_move()
                if ok:
                    return True
            return False
        else:
            for frm, to in rules.generate_legal_moves(b, Color.BLACK):
                b.make_move(frm, to)
                ok = search(b, plies_left - 1)
                b.undo_move()
                if not ok:
                    return False
            return True

    line = []
    cur = board.clone()
    for steps in range(1, max_plies + 1):
        plies = 2 * steps - 1
        # 找首招
        found = None
        for frm, to in rules.generate_legal_moves(cur, Color.RED):
            cur.make_move(frm, to)
            if search(cur, plies - 1):
                found = (frm, to)
                cur.undo_move()
                break
            cur.undo_move()
        if not found:
            break
        line.append(move_to_ucci(*found))
        cur.make_move(*found)  # 红走
        if rules.is_checkmate(cur, Color.BLACK):
            break
        # 黑方最佳防守（选能拖延最久的走法）
        best_def = None
        for frm, to in rules.generate_legal_moves(cur, Color.BLACK):
            cur.make_move(frm, to)
            # 该防守下红最快几步杀
            survivable = None
            for s in range(1, max_plies + 1):
                if search(cur, 2 * s - 1):
                    survivable = s
                    break
            cur.undo_move()
            if best_def is None or (survivable and survivable > best_def[0]):
                best_def = (survivable, frm, to)
        if best_def is None:
            break
        line.append(move_to_ucci(best_def[1], best_def[2]))
        cur.make_move(best_def[1], best_def[2])
        if nodes[0] > BUDGET:
            break
    return line


def main():
    print("=== 一步杀检测 ===")
    for name, fen in CANDIDATES.items():
        moves, status = find_mate_in_one(fen)
        flag = "  一步杀: " + str(moves) if moves else "  无一步杀"
        print(f"[{name}] {status}{flag}")
        print(f"  FEN: {fen}")

    print("\n=== 强制杀棋求解（最多3步）===")
    for name, fen in CANDIDATES.items():
        steps, mv = forced_mate_depth(fen, max_plies=3)
        if steps:
            print(f"[{name}] 杀{steps}步，首招: {mv}")
        else:
            print(f"[{name}] 3步内无强制杀（或超时放弃）")

    print("\n=== 杀棋主变（用于题解提示）===")
    for name, fen in CANDIDATES.items():
        line = mate_line(fen, max_plies=5)
        print(f"[{name}] {' '.join(line) if line else '（未解出）'}")


if __name__ == "__main__":
    main()
