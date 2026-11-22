"""引擎管理器：自动选择 Pikafish 或内置回退引擎。

搜索顺序：
    1. 环境变量 XIANGQI_ENGINE 指定的引擎路径
    2. 项目 engine_bin/ 目录下的 pikafish 可执行文件
    3. 内置 Python Minimax 引擎（保底）
"""
import os
import random
import sys
import threading
from typing import Optional, Tuple

from ..board.board import Board
from ..board.position import Pos, parse_ucci_move
from ..board import rules as _rules
from .builtin_engine import BuiltinEngine
from .opening_book import OpeningBook

_PLATFORM = sys.platform
# 各平台 Pikafish 可执行文件名候选
_EXE_CANDIDATES = {
    "win32": ["pikafish.exe", "Pikafish.exe"],
    "darwin": ["pikafish", "pikafish_macos"],
}.get(_PLATFORM, ["pikafish"])


def _find_pikafish() -> Optional[str]:
    # 1. 环境变量
    env_path = os.environ.get("XIANGQI_ENGINE")
    if env_path and os.path.isfile(env_path):
        return env_path
    # 2. 打包资源（PyInstaller 解包目录）/ 项目 engine_bin/
    from ..resources import resource, app_root
    for base in (resource("engine_bin"), os.path.join(app_root(), "engine_bin")):
        for name in _EXE_CANDIDATES:
            candidate = os.path.join(base, name)
            if os.path.isfile(candidate):
                return candidate
    return None


def _project_root() -> str:
    """应用根目录（开发=项目根；打包=exe所在目录）。"""
    from ..resources import app_root
    return app_root()


def default_opening_book_path() -> str:
    """内置开局书的标准位置（exe/项目根旁「开局书」目录）。"""
    from ..resources import books_dir
    return os.path.join(books_dir(), "default_opening.bin")


class EngineManager:
    """统一引擎接口（含三级走法选择：开局书命中 → 搜索 → 合法随机兜底）"""

    def __init__(self):
        self._builtin = BuiltinEngine()
        self._pikafish = None
        self._use_pikafish = False
        self._difficulty = 10  # 默认中级（1-20 级制）
        self._opening_book = OpeningBook()
        # Pikafish 子进程的 stdin/stdout 不是线程安全；多个 QThread 同时调用会读写冲突并触发 C++ 层崩溃
        self._lock = threading.Lock()
        # 自动加载项目内置开局书（若存在则默认启用；GUI 菜单可随时覆盖/禁用/卸载）
        try:
            p = default_opening_book_path()
            if os.path.isfile(p) and self._opening_book.load_bin(p):
                self._opening_book.enabled = True
        except Exception:
            pass

    def initialize(self) -> str:
        """初始化引擎，返回实际使用的引擎名称。在主窗口构造后调用（避免阻塞 UI 构造）。"""
        path = _find_pikafish()
        if path:
            try:
                from .pikafish_engine import PikafishEngine
                self._pikafish = PikafishEngine(path)
                if self._pikafish.start():
                    self._use_pikafish = True
                    self._pikafish.set_strength(self._difficulty)
                    return self._pikafish.name
            except Exception as e:
                print(f"[EngineManager] Pikafish 不可用，回退内置引擎: {e}")
                self._pikafish = None
        self._builtin.set_strength(self._difficulty)
        return self._builtin.name

    # ---- 开局书 ----
    @property
    def opening_book(self) -> OpeningBook:
        return self._opening_book

    def load_opening_book(self, path: Optional[str]) -> bool:
        """加载 Xiangqi Polyglot .bin 开局书；传 None 则卸载。返回是否成功。"""
        if path is None:
            self._opening_book = OpeningBook()
            return True
        ok = self._opening_book.load_bin(path)
        self._opening_book.enabled = ok
        return ok

    def set_opening_book_enabled(self, enabled: bool):
        self._opening_book.enabled = bool(enabled)

    @property
    def opening_book_info(self) -> str:
        b = self._opening_book
        if not b.loaded:
            return "未加载"
        status = "启用" if b.enabled else "已禁用"
        return f"{os.path.basename(b.path or '')}（{status}）"

    @property
    def engine_name(self) -> str:
        if self._use_pikafish and self._pikafish:
            return self._pikafish.name
        return self._builtin.name

    @property
    def using_pikafish(self) -> bool:
        return self._use_pikafish

    def set_difficulty(self, level: int):
        """对手棋力 1-20 级（旧 1-5 档调用自动按 ×4 映射兼容）。"""
        self._difficulty = max(1, min(20, int(level)))
        if self._use_pikafish and self._pikafish:
            self._pikafish.set_strength(self._difficulty)
        else:
            self._builtin.set_strength(self._difficulty)

    def best_move(self, board: Board) -> Optional[Tuple[Pos, Pos]]:
        """同步获取最佳走法（会阻塞，应在工作线程调用）。线程安全：Pikafish 子进程读写受锁保护。

        三级兜底（Experience1809012 经验）：
            1. 开局书命中并启用 → 按权重采样谱招返回
            2. Pikafish / 内置引擎 搜索最佳走法
            3. 引擎仍无结果 → 从合法着法集中随机选一个（必走保底，避免状态机卡死）
        """
        # ① 开局书优先（只读，无需锁）
        if self._opening_book.enabled and self._opening_book.loaded:
            ucci = self._opening_book.pick_weighted(board)
            if ucci:
                try:
                    return parse_ucci_move(ucci)
                except ValueError:
                    pass
        # ② 引擎搜索（Pikafish 子进程 stdin/stdout 非线程安全，必须串行）
        with self._lock:
            mv = None
            if self._use_pikafish and self._pikafish:
                mv = self._pikafish.best_move(board)
            if mv is None:
                mv = self._builtin.best_move(board)
            if mv is not None:
                return mv
        # ③ 合法随机兜底（必走）
        try:
            candidates = [e for e in _rules.generate_legal_moves(board)]
            if not candidates:
                return None
            return candidates[random.randrange(len(candidates))]
        except Exception:
            return None

    def analyze(self, board: Board, movetime_ms: int = 1500) -> dict:
        """同步分析当前局面，返回 {move, score_cp, pv, bestmove}。
        支持 Pikafish 和内置引擎；会阻塞，应在工作线程调用。"""
        if self._use_pikafish and self._pikafish:
            with self._lock:
                result = self._pikafish.analyze(board, movetime_ms=movetime_ms)
            if result:
                return result
        # Fallback: 内置引擎 analyze
        return self._builtin.analyze(board, movetime_ms=movetime_ms)

    def shutdown(self):
        if self._pikafish:
            self._pikafish.shutdown()
        self._builtin.shutdown()
