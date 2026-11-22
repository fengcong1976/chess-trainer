"""中局战术训练模块测试。

验证内容：
1. 题库数据完整性：FEN 可解析、双方王存在、解说键与正解线一致
2. 起始局面合法：轮走方未被将军（否则局面设计错误）
3. 正解线逐半步合法；主线走完时应达成战术目标（终局或将死）
4. 偏着演示线：偏着合法、演示线逐半步合法、起点=正着前局面
5. 训练会话判定闭环：正解推进、错着不动、多解收窄、完成持久化
"""
import os
import tempfile

from src.board.board import Board
from src.board.pieces import Color, PieceType
from src.board.position import parse_ucci_move, move_to_ucci
from src.board import rules
from src.training.midgame_puzzles import all_tactics
from src.training.midgame_trainer import MidgameSession, TacticVerdict
from src.training.trainer import ProgressStore


def test_puzzle_integrity():
    """FEN 解析、双王存在、讲解键覆盖主线。"""
    n = 0
    for t in all_tactics():
        b = Board.from_fen(t.fen)  # 解析失败会抛异常
        assert b.king_pos(Color.RED) is not None, f"{t.id}: 缺红帅"
        assert b.king_pos(Color.BLACK) is not None, f"{t.id}: 缺黑将"
        # 正解线中每个 ucci 都应有讲解（允许少量缺省，但主变首着必须有）
        assert t.lines[0][0] in t.comments, f"{t.id}: 主变首着缺讲解"
        # 演示的 wrong 必须是某条线的解题方着法（挂点正确）
        for ucci, pit in t.pitfalls.items():
            main = t.lines[0]
            assert ucci in main, f"{t.id}: 演示挂点 {ucci} 不在主变线中"
            idx = main.index(ucci)
            # 挂点必须是解题方走的半步（偶数索引，解题方先走）
            assert idx % 2 == 0, f"{t.id}: 演示挂点 {ucci} 不是解题方着法"
        n += 1
    print(f"  {n} 道中局战术题数据完整 ✓")


def test_start_position_not_in_check():
    """起始局面：轮走方（解题方）不能正处于被将军状态。"""
    for t in all_tactics():
        b = Board.from_fen(t.fen)
        # 解题方先走 => 对手不应已将军解题方
        opp = t.side.opponent
        assert not rules.is_in_check(b, t.side), \
            f"{t.id}: 起始局面解题方已被将军，局面非法"
        assert b.side is t.side, f"{t.id}: 轮走方应与解题方一致"
    print("  起始局面均合法（解题方未被将军）✓")


def test_solution_lines_legal():
    """每条正解线逐半步合法，且解题方着法（偶数位）由解题方棋子走。"""
    n = 0
    for t in all_tactics():
        for li, line in enumerate(t.lines):
            b = Board.from_fen(t.fen)
            for i, u in enumerate(line):
                frm, to = parse_ucci_move(u)
                piece = b.get(frm)
                assert piece is not None, f"{t.id} 线{li} 第{i+1}半步 {u} 无子可动"
                expect_side = t.side if i % 2 == 0 else t.side.opponent
                assert piece.color is expect_side, \
                    f"{t.id} 线{li} 第{i+1}半步 {u} 轮走方不符"
                assert rules.is_legal(b, frm, to), \
                    f"{t.id} 线{li} 第{i+1}半步 {u} 不合法"
                b.make_move(frm, to)
            # 线走完：对手应无子可动（杀局）或战术目标已达成（吃子完成）
            final_no_move = not rules.has_legal_move(b, b.side)
            captured_something = any(
                line[i] == t.lines[0][i] and
                Board.from_fen(t.fen).get(parse_ucci_move(line[i])[1]) is not None
                for i in range(len(line)) if i % 2 == 0
            )
            assert final_no_move or captured_something, \
                f"{t.id} 线{li} 走完既非杀局也未得子，战术目标存疑"
            n += 1
    print(f"  {n} 条正解线全部合法且达成战术目标 ✓")


def test_pitfall_lines():
    """偏着演示：偏着合法、演示线逐半步合法、起点=正着前主线局面。"""
    n = 0
    for t in all_tactics():
        for correct_ucci, pit in t.pitfalls.items():
            assert pit.line[0] == pit.wrong, f"{t.id}/{correct_ucci}: line[0] 应为偏着"
            main = t.lines[0]
            idx = main.index(correct_ucci)
            b = Board.from_fen(t.fen)
            for u in main[:idx]:
                b.make_move(*parse_ucci_move(u))
            for i, u in enumerate(pit.line):
                frm, to = parse_ucci_move(u)
                assert rules.is_legal(b, frm, to), \
                    f"{t.id}/{correct_ucci} 演示第{i+1}半步 {u} 不合法"
                b.make_move(frm, to)
            n += 1
    print(f"  {n} 条偏着演示线全部合法 ✓")


def _tmp_progress() -> ProgressStore:
    tmp = tempfile.mkdtemp()
    return ProgressStore(os.path.join(tmp, "progress.json"))


def test_session_verdicts():
    """会话判定闭环：正解推进+对手应对、错着不动、完成持久化。"""
    tactics = {t.id: t for t in all_tactics()}

    # ---- mg001：捉双主线 ----
    s = MidgameSession(tactics["mg001"], _tmp_progress())
    r = s.submit_move(*parse_ucci_move("e5a5"))   # 错着（有针对性讲解）
    assert r.verdict is TacticVerdict.WRONG
    assert "左翼" in r.message and s.ply == 0, "错着后局面不应推进"
    r = s.submit_move(*parse_ucci_move("e5e7"))   # 正解
    assert r.verdict is TacticVerdict.MATCH and r.reply is not None
    assert move_to_ucci(*r.reply) == "h7f8", "对手应按谱逃马"
    r = s.submit_move(*parse_ucci_move("e7b7"))   # 吃炮
    assert r.finished, "主线走完应完成"
    assert s.progress.is_solved("mg001")

    # ---- mg002：闪击 ----
    s = MidgameSession(tactics["mg002"], _tmp_progress())
    r = s.submit_move(*parse_ucci_move("e7e8"))   # 偏着：贪吃卒（合法但错）
    assert r.verdict is TacticVerdict.WRONG and s.ply == 0
    r = s.submit_move(*parse_ucci_move("e7a7"))
    assert r.verdict is TacticVerdict.MATCH
    assert move_to_ucci(*r.reply) == "e9d9"
    r = s.submit_move(*parse_ucci_move("a7a9"))
    assert r.finished and s.progress.is_solved("mg002")

    # ---- mg003：马踩双 ----
    s = MidgameSession(tactics["mg003"], _tmp_progress())
    r = s.submit_move(*parse_ucci_move("h2f3"))   # 错着
    assert r.verdict is TacticVerdict.WRONG and s.ply == 0
    r = s.submit_move(*parse_ucci_move("h2i4"))   # 踩双
    assert r.verdict is TacticVerdict.MATCH
    assert move_to_ucci(*r.reply) == "g3g2", "主变线黑车逃"
    r = s.submit_move(*parse_ucci_move("i4g5"))
    assert r.finished and s.progress.is_solved("mg003")

    # ---- mg004：弃子成杀 ----
    s = MidgameSession(tactics["mg004"], _tmp_progress())
    r = s.submit_move(*parse_ucci_move("e6g6"))   # 错着
    assert r.verdict is TacticVerdict.WRONG and s.ply == 0
    line = tactics["mg004"].lines[0]
    for idx in (0, 2, 4):   # 解题方三步：弃炮吃士 / 车吃士照将 / 卧槽马杀
        r = s.submit_move(*parse_ucci_move(line[idx]))
        assert r.verdict is TacticVerdict.MATCH, f"{line[idx]} 应命中"
    assert r.finished, "弃子杀局线走完应完成"
    assert s.progress.is_solved("mg004")

    # ---- mg005：牵制·拴链得车 ----
    s = MidgameSession(tactics["mg005"], _tmp_progress())
    r = s.submit_move(*parse_ucci_move("a3a4"))   # 错着(挺边兵走闲)
    assert r.verdict is TacticVerdict.WRONG and s.ply == 0, "mg005错着应不推进"
    line = tactics["mg005"].lines[0]
    r = s.submit_move(*parse_ucci_move(line[0]))  # e3e7 正解
    assert r.verdict is TacticVerdict.MATCH and r.reply is not None
    assert move_to_ucci(*r.reply) == line[1], "mg005 黑应手应为 d9e8"
    r = s.submit_move(*parse_ucci_move(line[2]))  # e7e8 吃士
    assert r.finished and s.progress.is_solved("mg005"), "mg005 应完成"

    # ---- mg006：突破·车砍中卒 ----
    s = MidgameSession(tactics["mg006"], _tmp_progress())
    r = s.submit_move(*parse_ucci_move("e3e4"))   # 错着(车只进一格不吃卒)
    assert r.verdict is TacticVerdict.WRONG and s.ply == 0, "mg006错着应不推进"
    line = tactics["mg006"].lines[0]
    r = s.submit_move(*parse_ucci_move(line[0]))  # e3e5 车吃中卒
    assert r.verdict is TacticVerdict.MATCH
    assert move_to_ucci(*r.reply) == line[1], "mg006 黑应手应为 d9e8"
    r = s.submit_move(*parse_ucci_move(line[2]))  # e5e8 吃士
    assert r.finished and s.progress.is_solved("mg006"), "mg006 应完成"

    # ---- mg007：引离·弃炮引车 马得车 ----
    s = MidgameSession(tactics["mg007"], _tmp_progress())
    r = s.submit_move(*parse_ucci_move("d2e2"))   # 错着(逃炮)
    assert r.verdict is TacticVerdict.WRONG and s.ply == 0, "mg007错着应不推进"
    line = tactics["mg007"].lines[0]
    r = s.submit_move(*parse_ucci_move(line[0]))  # d2d9 炮打士
    assert r.verdict is TacticVerdict.MATCH
    assert move_to_ucci(*r.reply) == line[1], "mg007 黑应手应为 d8d9(车吃炮)"
    r = s.submit_move(*parse_ucci_move(line[2]))  # e6d8 马踏车
    assert r.finished and s.progress.is_solved("mg007"), "mg007 应完成"

    # ---- mg008：破卫·双车叠肋夺士 ----
    s = MidgameSession(tactics["mg008"], _tmp_progress())
    r = s.submit_move(*parse_ucci_move("a1a0"))   # 错着(炮走闲)
    assert r.verdict is TacticVerdict.WRONG and s.ply == 0, "mg008错着应不推进"
    line = tactics["mg008"].lines[0]
    r = s.submit_move(*parse_ucci_move(line[0]))  # d8d9 前车吃士
    assert r.verdict is TacticVerdict.MATCH
    assert move_to_ucci(*r.reply) == line[1], "mg008 黑应手应为 e9e8(将逃)"
    r = s.submit_move(*parse_ucci_move(line[2]))  # d9d8 前车追将
    assert r.finished and s.progress.is_solved("mg008"), "mg008 应完成"
    print("  8 题会话判定闭环全部通过 ✓")


def test_session_illegal():
    """不合规走法判定。"""
    t = all_tactics()[0]
    s = MidgameSession(t, _tmp_progress())
    # 黑车 i0 (0,8) 想横移到 d0 (0,3)：越过黑将与士，不合法
    r = s.submit_move(*parse_ucci_move("i0d0"))
    assert r.verdict is TacticVerdict.ILLEGAL, "越过己方棋子的走法应判不合规"
    assert s.ply == 0
    print("  不合规走法判定 ✓")


if __name__ == "__main__":
    from src.board.position import Pos  # noqa: E402
    test_puzzle_integrity()
    test_start_position_not_in_check()
    test_solution_lines_legal()
    test_pitfall_lines()
    test_session_verdicts()
    test_session_illegal()
    print("\n中局战术训练模块全部测试通过 ✓✓✓")
