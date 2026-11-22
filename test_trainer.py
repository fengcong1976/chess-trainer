"""训练控制器冒烟测试。"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.training.endgame_puzzles import all_puzzles
from src.training.trainer import TrainingSession, ProgressStore, MoveVerdict
from src.board.position import parse_ucci_move

# 用临时进度文件
prog = ProgressStore(os.path.join(os.path.dirname(__file__), "data", "_test_progress.json"))

for p in all_puzzles():
    print(f"\n=== {p.title} ({p.goal}) ===")
    s = TrainingSession(p, prog)
    print("提示:", s.hint())

    # 提交正确首招
    first = p.solution[0]
    frm, to = parse_ucci_move(first)
    r = s.submit_move(frm, to)
    print(f"提交解法首招 {first}: {r.verdict.value} - {r.message} 黑应={r.black_reply}")

    if r.verdict == MoveVerdict.SOLVED:
        print("  -> 一步杀题目通关 ✓")
    elif r.verdict == MoveVerdict.CORRECT:
        print("  -> 多步杀继续演算中...")
        # 继续用求解器给红方找保持杀棋的走法，逐手演到将杀
        from src.training.trainer import _Solver
        from src.board import rules
        from src.board.pieces import Color
        from src.board.position import move_to_ucci
        solver = _Solver()
        guard = 0
        while not s.finished and guard < 20:
            guard += 1
            # 找一个保持杀棋的红方走法
            chosen = None
            for f, t in rules.generate_legal_moves(s.board, Color.RED):
                s.board.make_move(f, t)
                ok = solver.red_forces_mate(s.board, s.MATE_BUDGET)
                s.board.undo_move()
                if ok:
                    chosen = (f, t)
                    break
            if not chosen:
                print("  -> 找不到保持杀棋的红方走法，异常")
                break
            r2 = s.submit_move(*chosen)
            print(f"  红走 {move_to_ucci(*chosen)}: {r2.verdict.value} 黑应={r2.black_reply}")
        if s.finished:
            print("  -> 多步杀演算完成，通关 ✓")
        else:
            print("  -> 未在预期步数内完成")

# 清理临时进度
try:
    os.remove(os.path.join(os.path.dirname(__file__), "data", "_test_progress.json"))
except OSError:
    pass

print("\n训练控制器冒烟测试完成 ✓")
