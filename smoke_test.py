"""临时冒烟测试：验证规则修复。"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.board.board import Board
from src.board.pieces import Color, PieceType
from src.board.position import Pos, move_to_ucci
from src.board import rules


def legal_ucci(board):
    return [move_to_ucci(f, t) for f, t in rules.generate_legal_moves(board)]


# 1. 车兵残局：红兵(7,4)未过河，不应能横走
b = Board.from_fen("4k4/9/9/9/9/9/9/4P4/4R4/3K5 w - - 0 1")
print("车兵红方走法:", legal_ucci(b))
assert "e2d2" not in legal_ucci(b), "兵仍能横走！bug未修复"
assert "e2f2" not in legal_ucci(b), "兵仍能横走！bug未修复"
assert "e2e3" in legal_ucci(b), "兵应能前进"
print("  -> 兵横走bug已修复 ✓")

# 2. 开局走法数应为44（标准中国象棋开局合法走法数）
bs = Board.start_position()
n = len(rules.generate_legal_moves(bs))
print(f"开局红方合法走法数: {n}")
assert n == 44, f"开局走法数应为44，实际{n}"
print("  -> 开局走法数正确 ✓")

# 3. 红将能移动（九宫内）
kp = bs.king_pos(Color.RED)
print("红将位置:", kp, "UCCI:", kp.to_ucci())
assert kp == Pos(9, 4), "红将应在(9,4)即e0"
# 红将向上一步
up = kp + Pos(-1, 0)
assert up.in_palace(Color.RED), f"红将上方{up}应在红方九宫"
assert rules.is_legal(bs, kp, up), "红将应能上移一步"
print("  -> 红将可在己方九宫移动 ✓")

# 4. 黑将同理
bk = bs.king_pos(Color.BLACK)
assert bk == Pos(0, 4), "黑将应在(0,4)即e9"
print("黑将位置:", bk.to_ucci())

# 5. 飞将：开局两将同列(4)但中间有兵卒阻挡，不应判为将军
assert not rules.is_in_check(bs, Color.RED), "开局红方不应被将军"
assert not rules.is_in_check(bs, Color.BLACK), "开局黑方不应被将军"
print("  -> 开局无飞将误判 ✓")

# 6. 飞将边界：两将同列且无子遮挡（一在底行一在顶行）应判为将军，
#    且走入照面的将步应为非法
b2 = Board.from_fen("4k4/9/9/9/9/9/9/9/9/4K4 w - - 0 1")  # 两将同列col4无遮挡
assert rules.is_in_check(b2, Color.RED), "两将照面红方应被将军"
assert rules.is_in_check(b2, Color.BLACK), "两将照面黑方应被将军"
# 红将横向走出照面应合法
assert rules.is_legal(b2, Pos(9, 4), Pos(9, 3)), "红将横移离开照面应合法"
# 红将走到(9,3)后若仍同列...这里测另一局面：两将在col3照面，红将走到col3应为非法
b3 = Board.from_fen("3k5/9/9/9/9/9/9/9/9/4K4 w - - 0 1")  # 红将(9,4)，黑将(0,3)不同列
assert not rules.is_in_check(b3, Color.RED)
# 红将走到(9,3)会与黑将照面 -> 非法
assert not rules.is_legal(b3, Pos(9, 4), Pos(9, 3)), "红将走入照面应为非法"
print("  -> 飞将边界检测正确 ✓")

print("\n全部冒烟测试通过 ✓")
