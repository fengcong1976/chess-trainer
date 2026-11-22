"""残局训练流程控制器。

核心思路（教练级判定）：
    用精确杀棋求解器（穷举 minimax）判定用户走法是否仍保持红方强制杀棋，
    与引擎强弱无关——只要用户走法后红方仍有强制杀，即判为"正确"。
    多步杀中，黑方由求解器选择"最顽强防守"（拖延最久才被将杀的走法）自动应对。

这样即便未安装 Pikafish、仅用内置弱引擎，训练判定依然精准。
"""
import json
import os
from dataclasses import dataclass
from datetime import date, timedelta
from enum import Enum
from typing import List, Optional, Tuple

from ..board.board import Board
from ..board.pieces import Color
from ..board.position import Pos, move_to_ucci, parse_ucci_move
from ..board.notation import move_to_chinese
from ..board import rules
from .endgame_puzzles import Puzzle, all_puzzles, puzzle_by_id


# ---------------- 精确杀棋求解器 ----------------

class _Solver:
    """红方强制杀棋求解器，带节点预算防止组合爆炸。"""

    def __init__(self, node_budget: int = 1_500_000):
        self.node_budget = node_budget
        self.nodes = 0

    def red_forces_mate(self, b: Board, plies: int) -> bool:
        """红方是否能在 plies 半步内强制将杀黑方（与轮走方无关）。"""
        self.nodes += 1
        if self.nodes > self.node_budget:
            return False
        if b.side is Color.RED:
            moves = rules.generate_legal_moves(b, Color.RED)
            if not moves:
                return False  # 红无棋可走=红负，不构成红方杀棋
            if plies <= 0:
                return False
            for frm, to in moves:
                b.make_move(frm, to)
                ok = self.red_forces_mate(b, plies - 1)
                b.undo_move()
                if ok:
                    return True
            return False
        else:  # 黑方轮走（防守方）
            moves = rules.generate_legal_moves(b, Color.BLACK)
            if not moves:
                return True  # 黑无棋可走=黑被将杀
            if plies <= 0:
                return False
            for frm, to in moves:
                b.make_move(frm, to)
                ok = self.red_forces_mate(b, plies - 1)
                b.undo_move()
                if not ok:
                    return False  # 黑存在逃逸走法
            return True

    def min_mate_plies(self, b: Board, max_plies: int) -> Optional[int]:
        """红方最快几半步将杀黑方（轮走方任意）。超预算返回 None。"""
        for p in range(0, max_plies + 1):
            self.nodes = 0
            if self.red_forces_mate(b, p):
                return p
        return None


# ---------------- 走法判定结果 ----------------

class MoveVerdict(Enum):
    ILLEGAL = "illegal"      # 不合法走法
    WRONG = "wrong"          # 合法但未保持杀棋
    CORRECT = "correct"      # 正确，但尚未将杀（需继续）
    SOLVED = "solved"        # 正确且已将杀，本题完成


@dataclass
class MoveResult:
    verdict: MoveVerdict
    message: str
    user_move_cn: str = ""             # 用户走法的中文记法（如 "车二进六"）
    black_reply: Optional[str] = None  # 黑方应对走法（UCCI），仅 CORRECT 时可能有
    black_reply_cn: str = ""           # 黑方应对的中文记法（如 "车9平8"）


# ---------------- 进度存储 ----------------

class ProgressStore:
    """以 JSON 持久化已通关题目与统计（含错题本/间隔重复 srs 字段）。"""

    # 间隔重复复习间隔（天）：答对一级级推进，走完即"毕业"移出错题本
    SRS_INTERVALS = [1, 2, 4, 7, 15]

    def __init__(self, path: str):
        self.path = path
        self.solved: List[str] = []
        self.attempts: dict = {}   # puzzle_id -> 尝试次数
        self.srs: dict = {}        # puzzle_id -> {"iv","due","wrong","right"}
        self.current: dict = {}    # module -> 当前题目/课程 id（恢复训练进度）
        self.load()

    def load(self):
        if os.path.isfile(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.solved = data.get("solved", [])
                self.attempts = data.get("attempts", {})
                self.srs = data.get("srs", {})
                self.current = data.get("current", {})
            except Exception:
                self.solved, self.attempts, self.srs, self.current = [], {}, {}, {}

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"solved": self.solved, "attempts": self.attempts,
                       "srs": self.srs, "current": self.current},
                      f, ensure_ascii=False, indent=2)

    # ---------- 当前训练进度 ----------
    def set_current(self, module: str, pid: str):
        self.current[module] = pid
        self.save()

    def get_current(self, module: str) -> Optional[str]:
        return self.current.get(module)

    def mark_solved(self, pid: str):
        if pid not in self.solved:
            self.solved.append(pid)
        self.save()

    def record_attempt(self, pid: str):
        self.attempts[pid] = self.attempts.get(pid, 0) + 1
        self.save()

    def is_solved(self, pid: str) -> bool:
        return pid in self.solved

    # ---------- 错题本 / 间隔重复 ----------
    def record_wrong(self, pid: str):
        """答错：进入/重置错题本，今天即到期。"""
        e = self.srs.get(pid) or {"iv": 0, "wrong": 0, "right": 0}
        e["iv"] = 0
        e["wrong"] = e.get("wrong", 0) + 1
        e["due"] = date.today().isoformat()
        self.srs[pid] = e
        self.save()

    def record_right(self, pid: str):
        """错题答对（通关）：复习间隔推进一级；走完间隔表即毕业移出。"""
        e = self.srs.get(pid)
        if not e:
            return  # 未进错题本的题答对不记录
        e["right"] = e.get("right", 0) + 1
        e["iv"] = e.get("iv", 0) + 1
        if e["iv"] >= len(self.SRS_INTERVALS):
            self.srs.pop(pid, None)   # 毕业
        else:
            e["due"] = (date.today()
                        + timedelta(days=self.SRS_INTERVALS[e["iv"]])).isoformat()
        self.save()

    def pick_due_pid(self, pids=None) -> Optional[str]:
        """最早到期的错题 id（可限定 pids 范围）；无到期返回 None。"""
        today = date.today().isoformat()
        best, best_due = None, ""
        for pid, e in self.srs.items():
            if pids is not None and pid not in pids:
                continue
            due = e.get("due", "")
            if due <= today and (best is None or due < best_due):
                best, best_due = pid, due
        return best

    def due_count(self, pids=None) -> int:
        """到期错题数量（可限定 pids 范围）。"""
        today = date.today().isoformat()
        return sum(1 for pid, e in self.srs.items()
                   if (pids is None or pid in pids) and e.get("due", "") <= today)


# ---------------- 训练会话 ----------------

class TrainingSession:
    """一次残局训练会话。"""

    # 多步杀求解的半步预算（足以覆盖三步杀=5半步）
    MATE_BUDGET = 7

    def __init__(self, puzzle: Puzzle, progress: ProgressStore):
        self.puzzle = puzzle
        self.progress = progress
        self.board = Board.from_fen(puzzle.fen)
        self._solver = _Solver()
        self.hint_level = 0  # 已提示次数
        self.finished = False

    # ---- 走法提交 ----
    def submit_move(self, frm: Pos, to: Pos) -> MoveResult:
        if self.finished:
            return MoveResult(MoveVerdict.WRONG, "本题已完成，可切换下一题。")
        # 1. 合法性
        if not rules.is_legal(self.board, frm, to):
            return MoveResult(MoveVerdict.ILLEGAL, "这步不合规，请再想想。")

        self.progress.record_attempt(self.puzzle.id)

        # 2. 试走，判定是否仍保持红方强制杀
        user_cn = move_to_chinese(self.board, frm, to)
        self.board.make_move(frm, to)
        black_mated = not rules.has_legal_move(self.board, Color.BLACK)
        if black_mated:
            # 已将杀，本题完成（无需再判定）
            self.finished = True
            self.progress.mark_solved(self.puzzle.id)
            return MoveResult(MoveVerdict.SOLVED, "漂亮！将杀成立，本题通关！🎉",
                              user_move_cn=user_cn)

        # 仍保持强制杀？
        keeps_mate = self._solver.red_forces_mate(self.board, self.MATE_BUDGET)
        if not keeps_mate:
            self.board.undo_move()
            return MoveResult(MoveVerdict.WRONG,
                              "这步虽然合规，但红方将不再有强制杀棋。请寻找更强手的走法。",
                              user_move_cn=user_cn)

        # 3. 正确但未终局：黑方以最顽强走法应对
        reply = self._best_black_defense()
        if reply is not None:
            bfrom, bto = reply
            reply_cn = move_to_chinese(self.board, bfrom, bto)
            self.board.make_move(bfrom, bto)
            reply_ucci = move_to_ucci(bfrom, bto)
        else:
            reply_ucci = None
            reply_cn = ""

        return MoveResult(MoveVerdict.CORRECT,
                          "好棋！红方仍保有强制杀棋，请继续。",
                          user_move_cn=user_cn,
                          black_reply=reply_ucci, black_reply_cn=reply_cn)

    def _best_black_defense(self) -> Optional[Tuple[Pos, Pos]]:
        """选择拖延最久才被将杀的黑方走法（最顽强防守）。"""
        best_move = None
        best_plies = -1
        for frm, to in rules.generate_legal_moves(self.board, Color.BLACK):
            self.board.make_move(frm, to)
            # 红方从此处最快几半步杀
            self._solver.nodes = 0
            plies = None
            for p in range(0, self.MATE_BUDGET + 1):
                if self._solver.red_forces_mate(self.board, p):
                    plies = p
                    break
                self._solver.nodes = 0
            self.board.undo_move()
            if plies is not None and plies > best_plies:
                best_plies = plies
                best_move = (frm, to)
        return best_move

    # ---- 提示 ----
    def hint(self) -> str:
        self.hint_level += 1
        if self.hint_level == 1:
            return f"💡 提示：{self.puzzle.hint}"
        # 第二级提示给出首招（中文记法 + 坐标）
        first = self.puzzle.solution[0]
        try:
            frm, to = parse_ucci_move(first)
            cn = move_to_chinese(Board.from_fen(self.puzzle.fen), frm, to)
        except Exception:
            cn = first
        return (f"💡 解法首招：{cn}（坐标 {first[:2]}→{first[2:]}）。"
                f"可参考思路后继续推演。")

    def solution_first_move(self) -> str:
        return self.puzzle.solution[0]

    def full_mate_line(self, engine=None) -> Tuple[List[dict], bool]:
        """用引擎交替求解完整杀法线（阻塞——请在工作线程调用）。

        红方招来自引擎搜索（保持强制杀），黑方招为引擎防守。
        返回 (steps, verified)：steps=[{"side","ucci","cn"}]，verified=True 表示
        最后一招红棋走完后黑方已无合法着法（将杀确认）。
        """
        b = Board.from_fen(self.puzzle.fen)
        steps: List[dict] = []
        red_turn = True
        verified = False
        try:
            for _ in range(self.MATE_BUDGET + 2):
                mv = engine.best_move(b) if engine is not None else None
                if mv is None:
                    break
                frm, to = mv
                u = move_to_ucci(frm, to)
                steps.append({
                    "side": "红" if red_turn else "黑",
                    "ucci": u,
                    "cn": move_to_chinese(b, frm, to),
                })
                b.make_move(frm, to)
                if red_turn:
                    if not rules.has_legal_move(b, Color.BLACK):
                        verified = True
                        break
                    red_turn = False
                else:
                    red_turn = True
        except Exception:
            pass
        return steps, verified

    def reset(self):
        self.board = Board.from_fen(self.puzzle.fen)
        self.finished = False
        self.hint_level = 0


def default_progress_path() -> str:
    try:
        from ..resources import data_dir
        return os.path.join(data_dir(), "progress.json")
    except ImportError:
        base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        return os.path.join(base, "data", "progress.json")


def list_puzzles() -> List[Puzzle]:
    return all_puzzles()


def get_puzzle(pid: str) -> Puzzle:
    return puzzle_by_id(pid)
