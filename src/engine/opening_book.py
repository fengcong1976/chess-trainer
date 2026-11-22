"""中国象棋专用 .bin 开局书加载 & 查询。

. bin 格式说明（UCCI 兼容标准，每条 16 字节，小端）:
    struct Record:
        key:     uint64  (8B)    = zobrist hash (XiangqiPolyglot key)
        move:    uint16  (2B)    = 编码: (from_file << 12) | (from_rank << 8) | (to_file << 4) | to_rank
                                      file 0..8 对应 a..i; rank 0..9 (0=黑底 9=红底, UCCI rank)
        weight:  uint16  (2B)    = 权重，越大越优先
        games:   uint32  (4B)    = 参考对局数（可选排序）

查询流程:
    board.zobrist() -> 遍历所有 key 相等的 record -> 返回 [(ucci, weight)]
未命中返回 []。"""
import os
import struct
import random
from typing import Optional
from dataclasses import dataclass, field

# Zobrist 表（初始化一次即可）
# 14 种棋子 = 7 类型 × 2 色: 索引 = (type_idx*2 + color_idx)
# type_idx: K=0 A=1 B=2 N=3 R=4 C=5 P=6
# color_idx: RED=0 BLACK=1
# pieces_keys[piece_slot][row][col] -> uint64
_PIECE_SLOT = {
    # (PieceType.value, Color.value) -> slot
    ("K", "r"): 0, ("A", "r"): 2, ("B", "r"): 4,
    ("N", "r"): 6, ("R", "r"): 8, ("C", "r"): 10, ("P", "r"): 12,
    ("K", "b"): 1, ("A", "b"): 3, ("B", "b"): 5,
    ("N", "b"): 7, ("R", "b"): 9, ("C", "b"): 11, ("P", "b"): 13,
}
_TURN_KEY = 0x9D39_2457_6B3C_A29F  # 黑方走子时异或（红先默认不异或）

# 用确定性种子生成 14*10*9 + 1 个伪随机 64-bit 数（保证同一本书 hash 值始终一致）
def _gen_zobrist_table():
    rng = random.Random(0xC0FFEE_5AA5)
    table = [[[rng.getrandbits(64) for _ in range(9)] for _ in range(10)] for _ in range(14)]
    return table
_ZOBRIST = _gen_zobrist_table()
del _gen_zobrist_table


def zobrist_hash(board) -> int:
    """根据棋盘计算 XiangqiPolyglot 风格 zobrist key（64-bit，仅依赖棋子位置+走子方）。"""
    from ..board.position import Pos
    from ..board.pieces import Color
    h = 0
    for r in range(10):
        for c in range(9):
            piece = board.get(Pos(r, c))
            if piece is None:
                continue
            slot = _PIECE_SLOT[(piece.type.value, piece.color.value)]
            h ^= _ZOBRIST[slot][r][c]
    # 黑方走子时异或 turn key（红先手不异或）
    if board.side is Color.BLACK:
        h ^= _TURN_KEY
    return h & 0xFFFFFFFFFFFFFFFF


@dataclass
class BookEntry:
    ucci: str
    weight: int
    games: int = 0


@dataclass
class OpeningBook:
    """二进制开局书。load_bin(path) 加载，query(board) 按权重挑着法。"""
    path: Optional[str] = None
    enabled: bool = True
    _entries: dict[int, list[BookEntry]] = field(default_factory=dict)

    def load_bin(self, path: str) -> bool:
        """加载 Xiangqi-Polyglot 16B/record 二进制开局书。成功返回 True。"""
        if not os.path.isfile(path):
            return False
        try:
            data = open(path, "rb").read()
        except OSError:
            return False
        if len(data) % 16 != 0:
            # 容错：尝试 12B (QHH) 老格式；若仍不匹配认为损坏
            if len(data) % 12 != 0:
                return False
            record_size = 12
            fmt = "<QHH"
        else:
            record_size = 16
            fmt = "<QHHI"
        self._entries.clear()
        for off in range(0, len(data), record_size):
            chunk = data[off:off+record_size]
            try:
                if record_size == 16:
                    key, mv, wt, gm = struct.unpack(fmt, chunk)
                else:
                    key, mv, wt = struct.unpack(fmt, chunk)
                    gm = 0
            except struct.error:
                continue
            ff, fr = (mv >> 12) & 0xF, (mv >> 8) & 0xF
            tf, tr = (mv >> 4) & 0xF, mv & 0xF
            if not (0 <= ff < 9 and 0 <= tf < 9 and 0 <= fr < 10 and 0 <= tr < 10):
                continue
            ucci = f"{chr(ord('a')+ff)}{fr}{chr(ord('a')+tf)}{tr}"
            self._entries.setdefault(key, []).append(BookEntry(ucci, int(wt), int(gm)))
        self.path = os.path.abspath(path)
        return self.loaded

    @property
    def loaded(self) -> bool:
        return bool(self._entries)

    # -------------- 查询接口 --------------
    def query(self, board, *, max_entries: int = 12) -> list[BookEntry]:
        """返回当前局面命中的候选谱招，按 weight 降序最多 max_entries 条。未命中 []。"""
        if not self.enabled or not self._entries:
            return []
        from ..board.rules import is_legal
        from ..board.position import parse_ucci_move
        key = zobrist_hash(board)
        lst = self._entries.get(key, [])
        if not lst:
            return []
        lst = sorted(lst, key=lambda e: (-e.weight, -e.games))
        # 过滤掉在当前 board 上实际不合法的（bin 可能含禁走/偏谱）
        ok = []
        for e in lst:
            try:
                frm, to = parse_ucci_move(e.ucci)
            except ValueError:
                continue
            if is_legal(board, frm, to):
                ok.append(e)
                if len(ok) >= max_entries:
                    break
        return ok

    def pick_weighted(self, board, *, temperature: float = 1.0) -> Optional[str]:
        """按权重采样返回一个 UCCI 着法。温度<1偏最大权重，温度>1更随机。未命中返回 None。"""
        entries = self.query(board)
        if not entries:
            return None
        ws = [e.weight ** (1.0 / max(0.1, temperature)) for e in entries]
        total = sum(ws)
        if total <= 0:
            return entries[0].ucci
        r = random.random() * total
        acc = 0.0
        for e, w in zip(entries, ws):
            acc += w
            if acc >= r:
                return e.ucci
        return entries[-1].ucci
