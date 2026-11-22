"""生成内置默认开局书 default_opening.bin（Xiangqi Polyglot 16B/条）。

数据来源：项目内置 14 门 OpeningLesson 主线 + 所有 Variation。
主线 moves 权重 60000（优先级高），变例 moves 权重 30000。

直接运行（Windows PowerShell / CMD 均可）：
    python _gen_default_book.py
输出：项目根 / 开局书 / default_opening.bin
"""
import sys, pathlib, struct
ROOT = pathlib.Path(__file__).parent.resolve()
sys.path.insert(0, str(ROOT))

from src.board.board import Board
from src.board.position import parse_ucci_move
from src.engine.opening_book import zobrist_hash
from src.training.opening_book import all_lessons

WEIGHT_MAIN = 60000
WEIGHT_VAR = 30000
GAMES = 1

records: dict[int, dict[str, tuple[int, int]]] = {}  # key -> {ucci: (weight, games)}

def add_line(moves: list[str], weight: int):
    """回放一条走子序列，沿途局面 key → ucci 权重累加。"""
    board = Board.start_position()
    for u in moves:
        frm, to = parse_ucci_move(u)
        key = zobrist_hash(board)
        bucket = records.setdefault(key, {})
        if u in bucket:
            w, g = bucket[u]
            bucket[u] = (max(w, weight), g + GAMES)
        else:
            bucket[u] = (weight, GAMES)
        board.make_move(frm, to)


def main():
    lessons = all_lessons()
    print(f"导入开局课：{len(lessons)} 门")
    n_moves_main = 0
    n_vars = 0
    for l in lessons:
        add_line(l.moves, WEIGHT_MAIN)
        n_moves_main += len(l.moves)
        for v in l.variations:
            if getattr(v, "branch_at", ""):
                # 从主线某一步 branch_at 之后分叉：先回放主线到该步，再续上变例 moves
                try:
                    idx = l.moves.index(v.branch_at)
                except ValueError:
                    continue
                prefix = l.moves[: idx + 1]
                full = list(prefix) + list(v.moves)
            else:
                # branch_at 为空：变例本身是一条从开局起的独立平行主线（直接用 v.moves）
                full = list(v.moves)
            if len(full) >= 4:
                add_line(full, WEIGHT_VAR)
                n_vars += 1
    print(f"  · 主线累计 {n_moves_main} 半步，变例累计 {n_vars} 条分支")
    # 扁平化并写入
    total_records = 0
    out_dir = ROOT / "开局书"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / "default_opening.bin"
    with open(out_path, "wb") as f:
        for key, bucket in records.items():
            for ucci, (w, g) in bucket.items():
                # ucci 4-char: file_a_i + rank_digit(0-9) x2
                ff = ord(ucci[0]) - ord('a')
                fr = int(ucci[1])
                tf = ord(ucci[2]) - ord('a')
                tr = int(ucci[3])
                mv = (ff << 12) | (fr << 8) | (tf << 4) | tr
                if not (0 <= ff < 9 and 0 <= tf < 9 and 0 <= fr < 10 and 0 <= tr < 10):
                    continue
                f.write(struct.pack("<QHHI", key & 0xFFFFFFFFFFFFFFFF, mv, w & 0xFFFF, g & 0xFFFFFFFF))
                total_records += 1
    print(f"写入 {out_path}: {total_records} 条记录, {out_path.stat().st_size/1024:.1f} KB")
    return out_path


if __name__ == "__main__":
    p = main()
    # ---------- 立即自检：OpeningBook 加载后起始局面命中炮二平五 ----------
    from src.engine.opening_book import OpeningBook
    ob = OpeningBook()
    assert ob.load_bin(str(p)), "load default book failed"
    hits = ob.query(Board.start_position())
    print(f"起始局面命中：{[(e.ucci, e.weight) for e in hits[:5]]}")
    assert any(e.ucci == "h2e2" for e in hits), "起始局面未命中炮二平五h2e2"
    print("✅ 自检通过：开局书起始局面命中主招炮二平五")
