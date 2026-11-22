# -*- coding: utf-8 -*-
"""题库文本 UCCI 坐标中文注解器。

把题库 explanation/hint 等自由文本里的裸 UCCI 坐标转成初学者可读的形式：
  走法对   e1-d1 → 炮五平六（e1-d1）   （用题目 FEN 回放解法线得到精确中文记法）
  格子引用 d9    → d9·六路             （补充红方视角纵线号，与中文记法的纵线号一致）

源数据文本保持原样（坐标保留），只在题库模块加载时对内存对象做一次性注解，
安全可回滚（删掉题库尾部的调用即可还原）。
"""
import re

from ..board.board import Board
from ..board.position import parse_ucci_move, move_to_ucci
from ..board.notation import move_to_chinese

_LANE_CN = "九八七六五四三二一"   # 红方视角纵线：a=九 ... i=一

_PAIR = re.compile(r"(?<![a-z0-9])([a-i][0-9])\s*([-－—–~～])\s*([a-i][0-9])(?![0-9a-z])")
_SINGLE = re.compile(r"(?<![a-z0-9])([a-i][0-9])(?![0-9a-z])")


def build_pair_map(fen: str, lines) -> dict:
    """回放题目的全部解法线，生成 ((from2, to2) -> 中文记法) 映射。"""
    pair_map: dict = {}
    for line in lines or []:
        try:
            b = Board.from_fen(fen)
        except Exception:
            break
        for u in (line or []):
            try:
                frm, to = parse_ucci_move(u)
                u_ = move_to_ucci(frm, to)
                pair_map.setdefault((u_[:2], u_[2:]), move_to_chinese(b, frm, to))
                b.make_move(frm, to)
            except Exception:
                break
    return pair_map


def annotate_text(text: str, pair_map: dict) -> str:
    """把 text 中的裸 UCCI 坐标注解为中文（先走法对、再单格引用）。"""
    if not text:
        return text
    placeholders: dict = {}

    def _pair_repl(m: re.Match) -> str:
        a, sep, b = m.group(1), m.group(2), m.group(3)
        cn = pair_map.get((a, b))
        ph = f"\x00{len(placeholders)}\x00"
        placeholders[ph] = f"{cn}（{a}-{b}）" if cn else m.group(0)
        return ph

    def _single_repl(m: re.Match) -> str:
        c = m.group(1)
        return f"{c}·{_LANE_CN[ord(c[0]) - ord('a')]}路"

    t = _PAIR.sub(_pair_repl, text)
    t = _SINGLE.sub(_single_repl, t)
    for ph, rep in placeholders.items():
        t = t.replace(ph, rep)
    return t


def annotate_puzzle_texts(obj, lines=None,
                          fields=("goal", "hint", "explanation")) -> None:
    """就地注解一道题的文本字段（goal/hint/explanation/comments/mistakes/pitfalls）。"""
    lines_src = lines if lines is not None else getattr(obj, "solution", None)
    if lines_src is None:
        lines_src = getattr(obj, "lines", None)
    if lines_src and isinstance(lines_src, list) and lines_src and isinstance(lines_src[0], str):
        lines_src = [lines_src]
    pair_map = build_pair_map(getattr(obj, "fen", ""), lines_src)

    for f in fields:
        v = getattr(obj, f, None)
        if isinstance(v, str):
            setattr(obj, f, annotate_text(v, pair_map))
    for f in ("comments", "mistakes"):
        d = getattr(obj, f, None)
        if isinstance(d, dict):
            for k in list(d):
                if isinstance(d[k], str):
                    d[k] = annotate_text(d[k], pair_map)
    d = getattr(obj, "pitfalls", None)
    if isinstance(d, dict):
        for v in d.values():
            ex = getattr(v, "explanation", None)
            if isinstance(ex, str):
                v.explanation = annotate_text(ex, pair_map)
