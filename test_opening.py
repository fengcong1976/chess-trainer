"""开局训练模块冒烟测试（headless，不依赖 GUI）。

覆盖：
1. 中文着法记法（红方中文数字 / 黑方阿拉伯数字 / 平进退）
2. 5 门课程主线逐半步合法性
3. OpeningSession 判定：正着 MATCH+教练应招 / 偏离 WRONG+建议 / 教练先手 / 完成持久化
"""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.board.board import Board
from src.board.position import Pos, parse_ucci_move
from src.board.notation import move_to_chinese
from src.board import rules
from src.training.opening_book import all_lessons
from src.training.opening_trainer import OpeningSession, OpeningVerdict
from src.training.trainer import ProgressStore


def test_notation():
    b = Board.start_position()
    cases = [
        ("h2e2", "炮二平五"),   # 红右炮平中
        ("h0g2", "马二进三"),   # 红马二进三（斜行子：目标纵线）
        ("i0h0", "车一平二"),   # 红车一平二
        ("c0e2", "相七进五"),   # 红左相（七路相）飞中
        ("g0e2", "相三进五"),   # 红右相（三路相）飞中
        ("c3c4", "兵七进一"),   # 红兵七进一
        ("i0i1", "车一进一"),   # 红车一进一（直行子：步数）
        ("h9g7", "马8进7"),     # 黑马8进7（黑方阿拉伯数字）
        ("b7e7", "炮2平5"),     # 黑炮2平5（黑炮在 row2）
        ("i9h9", "车9平8"),     # 黑车9平8
        ("b9c7", "马2进3"),     # 黑马2进3
        ("c6c5", "卒3进1"),     # 黑卒3进1
    ]
    for ucci, expected in cases:
        frm, to = parse_ucci_move(ucci)
        got = move_to_chinese(b, frm, to)
        assert got == expected, f"{ucci}: 期望「{expected}」实际「{got}」"
    print(f"  中文记法 {len(cases)} 例 ✓")


def test_lessons_legal():
    for lesson in all_lessons():
        b = Board.start_position()
        for i, ucci in enumerate(lesson.moves):
            frm, to = parse_ucci_move(ucci)
            assert rules.is_legal(b, frm, to), \
                f"{lesson.id} 第{i+1}半步 {ucci} 不合法"
            b.make_move(frm, to)
        assert lesson.moves[-1] and not b.side is None
    print(f"  {len(all_lessons())} 门课程主线全部合法 ✓")


def test_session_red(tmp_prog):
    """op1（红视角）：走错 -> WRONG；走对全线 -> 完成并持久化。"""
    lesson = {l.id: l for l in all_lessons()}["op1"]
    s = OpeningSession(lesson, tmp_prog)

    # 第一步走偏：马二进三不是主线（主线炮二平五）
    r = s.submit_move(parse_ucci_move("h0g2")[0], parse_ucci_move("h0g2")[1])
    assert r.verdict is OpeningVerdict.WRONG, r
    assert "炮二平五" in r.message, r.message
    assert s.ply == 0, "走偏后主线不应推进"

    # 沿主线走完（教练应招由 submit_move 内部自动完成）
    while not s.finished:
        exp = s.expected_ucci()
        if s.is_user_turn():
            r = s.submit_move(*parse_ucci_move(exp))
            assert r.verdict is OpeningVerdict.MATCH, f"{exp}: {r.message}"
        else:
            res = s.coach_play_next()
            assert res is not None
    assert s.finished, "主线走完应完成"
    assert "op1" in tmp_prog.solved, "完成应持久化"
    print("  op1 红视角判定闭环 ✓")


def test_session_black(tmp_prog):
    """op2（黑视角）：教练先走，用户应招。"""
    lesson = {l.id: l for l in all_lessons()}["op2"]
    s = OpeningSession(lesson, tmp_prog)
    assert not s.is_user_turn(), "黑视角课应由教练先走"

    res = s.coach_play_next()
    assert res is not None and res.coach_reply_cn == "炮二平五"
    assert s.is_user_turn(), "教练走后轮到用户"

    # 用户应招走偏（马8进9 跳边马，未护中卒）
    r = s.submit_move(*parse_ucci_move("h9i7"))
    assert r.verdict is OpeningVerdict.WRONG and "马8进7" in r.message

    # 正确应招走完全线
    while not s.finished:
        exp = s.expected_ucci()
        if s.is_user_turn():
            r = s.submit_move(*parse_ucci_move(exp))
            assert r.verdict is OpeningVerdict.MATCH, f"{exp}: {r.message}"
        else:
            assert s.coach_play_next() is not None
    assert s.finished and "op2" in tmp_prog.solved
    print("  op2 黑视角教练先手+判定闭环 ✓")


def test_pitfalls():
    """所有偏着演示线：偏着合法、惩罚线逐半步合法、起点=正着前局面。"""
    n = 0
    for lesson in all_lessons():
        for correct_ucci, pit in lesson.pitfalls.items():
            assert pit.line[0] == pit.wrong, f"{lesson.id}/{correct_ucci}: line[0] 应为偏着"
            idx = lesson.moves.index(correct_ucci)
            b = Board.start_position()
            for u in lesson.moves[:idx]:
                b.make_move(*parse_ucci_move(u))
            for i, u in enumerate(pit.line):
                frm, to = parse_ucci_move(u)
                assert rules.is_legal(b, frm, to), \
                    f"{lesson.id}/{correct_ucci} 演示第{i+1}半步 {u} 不合法"
                b.make_move(frm, to)
            n += 1
    print(f"  {n} 条偏着演示线全部合法 ✓")


def main():
    prog_path = os.path.join(tempfile.gettempdir(), "test_opening_progress.json")
    if os.path.exists(prog_path):
        os.remove(prog_path)
    prog = ProgressStore(prog_path)

    print("--- 中文着法记法 ---")
    test_notation()
    print("--- 课程主线合法性 ---")
    test_lessons_legal()
    print("--- 偏着演示线合法性 ---")
    test_pitfalls()
    print("--- 训练会话判定 ---")
    test_session_red(prog)
    test_session_black(prog)
    print("\n开局训练模块全部测试通过 ✓✓✓")


if __name__ == "__main__":
    main()
