"""中局战术训练流程控制器（教练级判定）。

训练方式：每题预设若干条正解线（UCCI 双方合走序列，可分叉于解题方着法）。
解题方提交走法：
    命中任一活跃线的当前步 -> MATCH，走子推进并给该步讲解；
                              随后对手按谱自动应对（取仍活跃的主变线应手）。
    未命中                 -> WRONG，局面不动；若该错着在 mistakes 中有针对性
                              讲解则给出，否则通用提示。
    不合规                 -> ILLEGAL。
线走完 -> 本题完成，进度持久化。

与开局训练一致，采用局面快照机制支持偏着惩罚演示（知其所以然）。
"""
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional, Tuple

from ..board.board import Board
from ..board.pieces import Color
from ..board.position import Pos, move_to_ucci, parse_ucci_move
from ..board import rules
from ..board.notation import move_to_chinese
from .midgame_puzzles import Tactic
from .opening_book import Pitfall
from .trainer import ProgressStore


class TacticVerdict(Enum):
    ILLEGAL = "illegal"   # 不合规走法
    WRONG = "wrong"       # 合规但未命中正解线
    MATCH = "match"       # 命中正解线


@dataclass
class TacticMoveResult:
    verdict: TacticVerdict
    message: str                       # 该步讲解（MATCH）或错着讲解（WRONG）
    user_move_cn: str = ""             # 解题方走法的中文记法
    reply: Optional[Tuple[Pos, Pos]] = None  # 对手按谱应对的走法
    reply_cn: str = ""                 # 对手应对的中文记法
    reply_comment: str = ""            # 对手应对的讲解
    finished: bool = False             # 本题是否完成


class MidgameSession:
    """一道中局战术题的训练会话。"""

    def __init__(self, tactic: Tactic, progress: ProgressStore):
        self.tactic = tactic
        self.progress = progress
        self.board = Board.from_fen(tactic.fen)
        self.ply = 0                                   # 已走的半步数
        self.finished = False
        self.active: List[int] = list(range(len(tactic.lines)))  # 活跃线索引
        # 每个半步走前的局面快照（键=半步序号），供偏着演示回放
        self.snapshots: Dict[int, Board] = {0: self.board.clone()}
        self.hint_level = 0

    # ---- 状态查询 ----
    def user_side(self) -> Color:
        return self.tactic.side

    def is_user_turn(self) -> bool:
        return self.board.side is self.user_side()

    def expected_ucci(self) -> Optional[str]:
        """主变线当前应走的一步（用于提示/演示正着）。"""
        if self.finished:
            return None
        main = self.active[0] if self.active else 0
        line = self.tactic.lines[main]
        if self.ply >= len(line):
            return None
        return line[self.ply]

    def suggest_chinese(self) -> str:
        exp = self.expected_ucci()
        if exp is None:
            return ""
        frm, to = parse_ucci_move(exp)
        return move_to_chinese(self.board, frm, to)

    def pitfall_for(self, ucci: str) -> Optional[Pitfall]:
        """查询某正着是否配有偏着惩罚演示。"""
        return self.tactic.pitfalls.get(ucci)

    def pitfall_start_board(self, ucci: str) -> Optional[Board]:
        """偏着演示的起点局面：该正着走之前的主线局面。"""
        pit = self.pitfall_for(ucci)
        if pit is None:
            return None
        main = self.tactic.lines[0]
        if ucci not in main:
            return None
        idx = main.index(ucci)
        snap = self.snapshots.get(idx)
        return snap.clone() if snap else None

    # ---- 走法提交 ----
    def submit_move(self, frm: Pos, to: Pos) -> TacticMoveResult:
        if self.finished:
            return TacticMoveResult(TacticVerdict.WRONG, "本题已完成，可切换下一题。")

        # 1. 合规性
        if not rules.is_legal(self.board, frm, to):
            return TacticMoveResult(TacticVerdict.ILLEGAL, "这步不合规，请再想想。")

        ucci = move_to_ucci(frm, to)
        user_move_cn = move_to_chinese(self.board, frm, to)

        # 2. 命中判定：解题方轮次才判定；未命中则局面不动
        if self.is_user_turn():
            matches = [li for li in self.active
                       if self.ply < len(self.tactic.lines[li])
                       and self.tactic.lines[li][self.ply] == ucci]
            if not matches:
                wrong_note = self.tactic.mistakes.get(ucci)
                if wrong_note:
                    msg = f"这步不行：{wrong_note}"
                else:
                    msg = (f"这步没有战术效果。此局面的正着是「{self.suggest_chinese()}」。\n"
                           f"正着讲解：{self.tactic.comments.get(self.expected_ucci(), '')}")
                return TacticMoveResult(TacticVerdict.WRONG, msg,
                                        user_move_cn=user_move_cn)

            # 3. 命中：走子推进，收窄活跃线
            self._play_half(frm, to)
            comment = self.tactic.comments.get(ucci, "好棋！")
            if self._advance_finished():
                return TacticMoveResult(TacticVerdict.MATCH, comment,
                                        user_move_cn=user_move_cn, finished=True)

            # 4. 对手按谱应对（取活跃主变线的应手）
            reply_res = self._opponent_reply()
            return TacticMoveResult(TacticVerdict.MATCH, comment,
                                    user_move_cn=user_move_cn,
                                    reply=reply_res.reply,
                                    reply_cn=reply_res.reply_cn,
                                    reply_comment=reply_res.reply_comment,
                                    finished=reply_res.finished)

        # 非解题方轮次（理论上不会发生：对手由会话自动应对）
        return TacticMoveResult(TacticVerdict.WRONG, "现在轮到对方走棋。",
                                user_move_cn=user_move_cn)

    # ---- 内部 ----
    def _play_half(self, frm: Pos, to: Pos):
        self.snapshots[self.ply] = self.board.clone()
        self.board.make_move(frm, to)
        self.ply += 1

    def _advance_finished(self) -> bool:
        """检查活跃线是否已走完（含将死兜底校验）。"""
        main = self.active[0]
        if self.ply >= len(self.tactic.lines[main]):
            self.finished = True
            self.progress.mark_solved(self.tactic.id)
            return True
        # 兜底：对手已无棋可走（构造的杀局提前达成）
        if not rules.has_legal_move(self.board, self.board.side):
            self.finished = True
            self.progress.mark_solved(self.tactic.id)
            return True
        return False

    def _opponent_reply(self) -> TacticMoveResult:
        """对手按谱应对：取活跃主变线的当前步；走后按实际应手收窄活跃线。"""
        main = self.active[0]
        line = self.tactic.lines[main]
        reply_ucci = line[self.ply]
        frm, to = parse_ucci_move(reply_ucci)
        reply_cn = move_to_chinese(self.board, frm, to)
        self._play_half(frm, to)
        # 按实际应手收窄活跃线（不同线的黑方应手若不同则剔除）
        self.active = [li for li in self.active
                       if self.tactic.lines[li][self.ply - 1] == reply_ucci]
        comment = self.tactic.comments.get(reply_ucci, "")
        finished = self._advance_finished()
        return TacticMoveResult(TacticVerdict.MATCH, "",
                                reply=(frm, to), reply_cn=reply_cn,
                                reply_comment=comment, finished=finished)

    # ---- 提示 / 解法 / 重置 ----
    def hint(self) -> str:
        self.hint_level += 1
        if self.hint_level == 1:
            return f"💡 提示：{self.tactic.hint}"
        first = self.tactic.lines[0][0]
        frm, to = parse_ucci_move(first)
        return (f"💡 解法首招：{move_to_chinese(Board.from_fen(self.tactic.fen), frm, to)}"
                f"（{first}）。可参考思路后继续推演。")

    def solution_first_move(self) -> str:
        return self.tactic.lines[0][0]

    def reset(self):
        self.board = Board.from_fen(self.tactic.fen)
        self.ply = 0
        self.finished = False
        self.active = list(range(len(self.tactic.lines)))
        self.snapshots = {0: self.board.clone()}
        self.hint_level = 0
