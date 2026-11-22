"""开局训练流程控制器（教练级判定）。

训练方式：课程主线为一串双方合走的 UCCI 着法。
用户轮到时提交走法：
    与主线一致 -> MATCH，给该步讲解；随后教练按谱自动应招并给对手着法讲解。
    偏离主线   -> WRONG，局面不动，提示该局面的正着（中文记法）及讲解理由。
    不合规     -> ILLEGAL。
主线走完 -> 本课完成，进度持久化。
"""
from dataclasses import dataclass
from enum import Enum
from typing import Dict, Optional, Tuple

from ..board.board import Board
from ..board.pieces import Color
from ..board.position import Pos, move_to_ucci, parse_ucci_move
from ..board import rules
from ..board.notation import move_to_chinese
from .opening_book import OpeningLesson, Pitfall
from .trainer import ProgressStore


class OpeningVerdict(Enum):
    ILLEGAL = "illegal"   # 不合规走法
    WRONG = "wrong"       # 合规但偏离主线
    MATCH = "match"       # 与主线一致


@dataclass
class OpeningMoveResult:
    verdict: OpeningVerdict
    message: str                     # 该步讲解（MATCH）或正着建议（WRONG）
    user_move_cn: str = ""           # 用户走法的中文记法
    coach_reply: Optional[Tuple[Pos, Pos]] = None  # 教练应招（UCCI 已在谱内）
    coach_reply_cn: str = ""         # 教练应招的中文记法
    coach_comment: str = ""          # 教练应招的讲解
    finished: bool = False           # 本课是否完成


class OpeningSession:
    """一门开局课的训练会话。"""

    def __init__(self, lesson: OpeningLesson, progress: ProgressStore):
        self.lesson = lesson
        self.progress = progress
        self.board = Board.start_position()
        self.ply = 0            # 已沿主线走的半步数
        self.finished = False
        # 每个半步走前的局面快照（键=半步序号），供偏着演示回放
        self.snapshots: Dict[int, Board] = {0: Board.start_position()}

    # ---- 状态查询 ----
    def user_side(self) -> Color:
        return self.lesson.side

    def is_user_turn(self) -> bool:
        return self.board.side is self.user_side()

    def expected_ucci(self) -> Optional[str]:
        if self.finished or self.ply >= len(self.lesson.moves):
            return None
        return self.lesson.moves[self.ply]

    def suggest_chinese(self) -> str:
        """当前主线着法的中文记法（用于走偏提示）。"""
        exp = self.expected_ucci()
        if exp is None:
            return ""
        frm, to = parse_ucci_move(exp)
        return move_to_chinese(self.board, frm, to)

    def pitfall_for(self, ucci: str) -> Optional[Pitfall]:
        """查询某正着是否配有偏着惩罚演示。"""
        return self.lesson.pitfalls.get(ucci)

    def pitfall_start_board(self, ucci: str) -> Optional[Board]:
        """偏着演示的起点局面：该正着走之前的主线局面。"""
        pit = self.pitfall_for(ucci)
        if pit is None or ucci not in self.lesson.moves:
            return None
        idx = self.lesson.moves.index(ucci)
        snap = self.snapshots.get(idx)
        return snap.clone() if snap else None

    # ---- 走法提交 ----
    def submit_move(self, frm: Pos, to: Pos) -> OpeningMoveResult:
        if self.finished:
            return OpeningMoveResult(OpeningVerdict.WRONG, "本课已完成，可切换下一课。")

        # 1. 合规性
        if not rules.is_legal(self.board, frm, to):
            return OpeningMoveResult(OpeningVerdict.ILLEGAL, "这步不合规，请再想想。")

        user_move_cn = move_to_chinese(self.board, frm, to)

        # 2. 主线比对
        exp = self.expected_ucci()
        if move_to_ucci(frm, to) != exp:
            return OpeningMoveResult(
                OpeningVerdict.WRONG,
                f"这步偏离了主流着法。此局面的正着是「{self.suggest_chinese()}」。\n"
                f"正着讲解：{self.lesson.comments.get(exp, '')}")

        # 3. 正确：走子并推进
        self.snapshots[self.ply] = self.board.clone()
        self.board.make_move(frm, to)
        self.ply += 1
        comment = self.lesson.comments.get(exp, "好棋！")
        if self.ply >= len(self.lesson.moves):
            self.finished = True
            self.progress.mark_solved(self.lesson.id)
            return OpeningMoveResult(OpeningVerdict.MATCH, comment,
                                     user_move_cn=user_move_cn, finished=True)

        # 4. 教练按谱应招
        coach_res = self.coach_play_next()
        return OpeningMoveResult(OpeningVerdict.MATCH, comment,
                                 user_move_cn=user_move_cn,
                                 coach_reply=coach_res.coach_reply,
                                 coach_reply_cn=coach_res.coach_reply_cn,
                                 coach_comment=coach_res.coach_comment,
                                 finished=coach_res.finished)

    def coach_play_next(self) -> Optional[OpeningMoveResult]:
        """教练按谱走一步（仅当当前轮到教练时）。"""
        if self.finished or self.is_user_turn():
            return None
        reply = self.lesson.moves[self.ply]
        cfrom, cto = parse_ucci_move(reply)
        coach_cn = move_to_chinese(self.board, cfrom, cto)
        self.snapshots[self.ply] = self.board.clone()
        self.board.make_move(cfrom, cto)
        self.ply += 1
        coach_comment = self.lesson.comments.get(reply, "")
        finished = self.ply >= len(self.lesson.moves)
        if finished:
            self.finished = True
            self.progress.mark_solved(self.lesson.id)
        return OpeningMoveResult(OpeningVerdict.MATCH, "",
                                 coach_reply=(cfrom, cto), coach_reply_cn=coach_cn,
                                 coach_comment=coach_comment, finished=finished)

    # ---- 重置 ----
    def reset(self):
        self.board = Board.start_position()
        self.ply = 0
        self.finished = False
        self.snapshots = {0: Board.start_position()}
