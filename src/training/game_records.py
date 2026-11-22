"""对局棋谱存取：对弈分出胜负后自动保存，供复盘模式回放。

存储格式 data/games/YYYYMMDD-HHMMSS-胜负.json：
    {"ts": ISO时间, "player": "red"/"black", "result": "win"/"loss",
     "moves": ["h2e2", ...], "cn": ["炮二平五", ...]}
"""
import json
import os
from datetime import datetime
from typing import List, Optional, Tuple


def games_dir() -> str:
    try:
        from ..resources import data_dir
        base = data_dir()
    except ImportError:
        base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        base = os.path.join(base, "data")
    return os.path.join(base, "games")


def save_game(player: str, result: str, moves: List[str], cn: List[str]) -> str:
    """保存一局，返回文件路径。moves/cn 为半步序列（等长）。"""
    d = games_dir()
    os.makedirs(d, exist_ok=True)
    ts = datetime.now()
    name = f"{ts:%Y%m%d-%H%M%S}-{'胜' if result == 'win' else '负'}.json"
    path = os.path.join(d, name)
    data = {
        "ts": ts.isoformat(timespec="seconds"),
        "player": player,
        "result": result,
        "moves": list(moves),
        "cn": list(cn),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return path


def list_games(limit: int = 20) -> List[Tuple[str, dict]]:
    """最近 limit 局（新在前），返回 [(路径, data), ...]；坏文件跳过。"""
    d = games_dir()
    if not os.path.isdir(d):
        return []
    out = []
    for name in sorted(os.listdir(d), reverse=True):
        if not name.endswith(".json"):
            continue
        p = os.path.join(d, name)
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data.get("moves"), list):
                out.append((p, data))
        except Exception:
            continue
        if len(out) >= limit:
            break
    return out


def load_game(path: str) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None
