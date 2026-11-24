"""每日训练计划：任务模板 + 打卡持久化 + 连续达标天数统计。

设计对应软件内 6 大训练入口，自动跟踪 4 类训练通关（杀法/残局/中局/开局），
实战与复盘为手动打卡。数据存 data/training_plan.json，按天记 JSON。
"""
import json
import os
from datetime import date, timedelta
from typing import Dict, List, Tuple

# 每日任务模板：(key, 显示名, 每日目标, 是否自动跟踪)
# auto=True 的任务在对应训练模式通关/对弈结束局时自动 +1（GUI 通关钩子调用 record）
DAILY_TASKS: List[Tuple[str, str, int, bool]] = [
    ("tactics", "🎯 杀法训练（一步杀→多步杀）", 5, True),
    ("endgame", "🎓 残局定式", 3, True),
    ("midgame", "⚔ 中局战术", 2, True),
    ("opening", "📖 开局课程", 1, True),
    ("play",    "♟ 实战对弈（下完一整盘自动计）", 2, True),
    ("review",  "📋 复盘总结（找败着，看引擎评估曲线）", 1, False),
]
DAILY_MIN_CATEGORIES = 3  # 每日达标线：完成 ≥3 类任务

TASK_KEY_INDEX = 0
TASK_TITLE_INDEX = 1
TASK_TARGET_INDEX = 2
TASK_AUTO_INDEX = 3


def default_plan_path() -> str:
    try:
        from ..resources import data_dir
        return os.path.join(data_dir(), "training_plan.json")
    except ImportError:
        base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        return os.path.join(base, "data", "training_plan.json")


class TrainingPlanStore:
    """按天记录各类任务完成次数；支持连续达标天数（streak）统计。"""

    def __init__(self, path: str = None):
        self.path = path or default_plan_path()
        self.days: Dict[str, Dict[str, int]] = {}  # {"2026-08-31": {"tactics": 3, ...}}
        self.load()

    # ---------- 持久化 ----------
    def load(self):
        if os.path.isfile(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                days = data.get("days", {}) if isinstance(data, dict) else {}
                self.days = days if isinstance(days, dict) else {}
            except Exception:
                self.days = {}

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"days": self.days}, f, ensure_ascii=False, indent=2)

    # ---------- 记录 ----------
    @staticmethod
    def _day_key(d: date = None) -> str:
        return (d or date.today()).isoformat()

    def record(self, key: str, n: int = 1, day: date = None):
        """今日（或指定日）某类任务完成数 +n。"""
        k = self._day_key(day)
        bucket = self.days.get(k)
        if not isinstance(bucket, dict):
            bucket = {}
            self.days[k] = bucket
        bucket[key] = bucket.get(key, 0) + n
        self.save()

    def manual_check(self, key: str):
        """手动打卡（实战/复盘等非自动跟踪任务）。"""
        self.record(key, 1)

    # ---------- 查询 ----------
    def today_counts(self) -> Dict[str, int]:
        return dict(self.days.get(self._day_key(), {}))

    def day_categories_done(self, d: date) -> int:
        """某日完成的任务种类数（有任意进度即算该类完成）。"""
        bucket = self.days.get(self._day_key(d), {})
        return sum(1 for v in bucket.values() if v > 0)

    def day_all_done(self, d: date) -> bool:
        return self.day_categories_done(d) >= DAILY_MIN_CATEGORIES

    def streak(self) -> int:
        """连续达标天数：今天已达标则从今天倒数；否则从昨天倒数（今天进行中不打断）。"""
        n = 0
        d = date.today()
        if self.day_all_done(d):
            n += 1
        d -= timedelta(days=1)
        while self.day_all_done(d):
            n += 1
            d -= timedelta(days=1)
        return n

    def total_records(self) -> int:
        """历史累计完成总次数（全任务求和）。"""
        return sum(sum(b.values()) for b in self.days.values())


class PlayStatsStore:
    """对弈战绩持久化：总盘数 / 胜 / 负 / 和 + 胜率。存 data/play_stats.json。"""

    def __init__(self, path: str = None):
        self.path = path or default_plan_path().replace(
            "training_plan.json", "play_stats.json")
        self.wins = 0
        self.losses = 0
        self.draws = 0
        self.level = 10   # 对手棋力 1-20 级（跨启动记忆）
        self.load()

    def load(self):
        if os.path.isfile(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.wins = int(data.get("wins", 0))
                self.losses = int(data.get("losses", 0))
                self.draws = int(data.get("draws", 0))
                self.level = max(1, min(20, int(data.get("level", 10))))
            except Exception:
                self.wins = self.losses = self.draws = 0
                self.level = 10

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"wins": self.wins, "losses": self.losses,
                       "draws": self.draws, "level": self.level},
                      f, ensure_ascii=False, indent=2)

    def set_level(self, level: int):
        """记录对手棋力等级（1-20），下次启动自动恢复。"""
        self.level = max(1, min(20, int(level)))
        self.save()

    def record(self, result: str):
        """result: 'win' / 'loss' / 'draw'。"""
        if result == "win":
            self.wins += 1
        elif result == "loss":
            self.losses += 1
        elif result == "draw":
            self.draws += 1
        else:
            return
        self.save()

    @property
    def total(self) -> int:
        return self.wins + self.losses + self.draws

    def win_rate(self) -> float:
        """胜率（0~1）；无对局时返回 0.0。"""
        return self.wins / self.total if self.total else 0.0

    def reset(self):
        self.wins = self.losses = self.draws = 0
        self.save()
