"""复现：1.a3a4 后内置引擎的应招是否合法。"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from src.board.board import Board
from src.board.pieces import Color
from src.board.position import move_to_ucci
from src.board import rules
from src.engine.builtin_engine import BuiltinEngine

b = Board.start_position()
# 1. a3a4 = 红兵 (6,0)->(5,0)
from src.board.position import Pos
assert rules.is_legal(b, Pos(6, 0), Pos(5, 0))
b.make_move(Pos(6, 0), Pos(5, 0))
print("轮走方:", b.side.name_cn)

# 列出黑炮(2,1)的伪合法目标
print("黑炮(2,1)伪合法目标:", [move_to_ucci(Pos(2,1), t) for t in rules.pseudo_targets(b, Pos(2,1))])
# (2,1)->(9,1) 是否在黑方合法走法中
legal = rules.generate_legal_moves(b, Color.BLACK)
uci_set = [move_to_ucci(f, t) for f, t in legal]
print("b7b0 在合法走法中?", "b7b0" in ucci_set)

eng = BuiltinEngine()
eng.set_difficulty(3)
t0 = time.time()
mv = eng.best_move(b)
dt = time.time() - t0
print(f"引擎返回: {move_to_ucci(*mv)}  耗时{dt:.2f}s")
# 校验合法性
b2 = Board.start_position(); b2.make_move(Pos(6,0), Pos(5,0))
print("该走法合法?", rules.is_legal(b2, *mv))
