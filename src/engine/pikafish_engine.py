"""Pikafish 引擎接口：通过 UCI 协议与引擎子进程通信。

Pikafish 是基于 Stockfish 的开源象棋引擎，棋力达人类特级大师水平。
未放置可执行文件时，EngineManager 会自动回退到内置引擎。

通信流程：
    uci            -> uciok
    isready        -> readyok
    setoption ...  -> (无显式回执)
    position fen.. -> (无显式回执)
    go ...         -> bestmove <move>
"""
import os
import queue
import subprocess
import threading
from typing import Optional, Tuple

from ..board.board import Board
from ..board.position import Pos, parse_ucci_move


class PikafishEngine:
    """Pikafish UCI 引擎封装"""

    name = "Pikafish（特级大师级）"
    is_ready = False

    def __init__(self, exe_path: str):
        self._exe_path = exe_path
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._skill_level = 8        # UCI Skill Level 0-20
        self._movetime_ms = 800      # 思考毫秒
        self._depth = 0              # 0 表示用 movetime

    # ---- 生命周期 ----
    def start(self) -> bool:
        try:
            # 工作目录设为 exe 所在目录，便于引擎找到同目录的 pikafish.nnue
            # CREATE_NO_WINDOW：打包成无控制台 exe 后，防止引擎子进程弹出 cmd 黑框
            flags = 0
            if os.name == "nt":
                flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            self._proc = subprocess.Popen(
                [self._exe_path],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
                encoding="utf-8",
                errors="replace",
                cwd=os.path.dirname(os.path.abspath(self._exe_path)),
                creationflags=flags,
            )
        except Exception as e:
            print(f"[Pikafish] 启动失败: {e}")
            return False

        # 后台读线程：引擎输出行进队列，读取端用带超时的 queue.get。
        # 旧实现用 Timer 到点 kill 进程实现读超时——超时一次引擎就死了
        # 但 is_ready 仍为 True，之后 AI 永远不出招。
        self._lines: "queue.Queue[str]" = queue.Queue()
        self._reader = threading.Thread(target=self._pump_output, daemon=True)
        self._reader.start()

        # 握手
        self._send("uci")
        if not self._wait_for("uciok", timeout=10):
            print("[Pikafish] 未收到 uciok，可能不是合法引擎")
            self.shutdown()
            return False
        self._send("isready")
        if not self._wait_for("readyok", timeout=10):
            self.shutdown()
            return False
        self.is_ready = True
        return True

    def shutdown(self):
        if self._proc:
            try:
                self._send("quit")
                self._proc.wait(timeout=3)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None
        self.is_ready = False

    # ---- 难度 ----
    def set_strength(self, level: int):
        """对手棋力 1-20 级：Skill Level 0-20 + 思考时间联动。

        低级别时评估随机噪声大且思考极短，棋力骤降（新手可赢）；
        高级别接近引擎真实棋力。"""
        level = max(1, min(20, int(level)))
        self._skill_level = round((level - 1) * 20 / 19)   # 1→0 ... 20→20
        self._movetime_ms = min(100 + level * 145, 3000)   # 1≈245ms ... 20=3000ms
        if self.is_ready:
            self._send(f"setoption name Skill Level value {self._skill_level}")

    def set_difficulty(self, level: int):
        """旧接口（1-5 档），线性映射到 20 级制。"""
        self.set_strength(int(level) * 4)

    # ---- 走法 ----
    def best_move(self, board: Board) -> Optional[Tuple[Pos, Pos]]:
        info = self.analyze(board, movetime_ms=self._movetime_ms)
        return info.get("move") if info else None

    def analyze(self, board: Board, movetime_ms: int = 1200) -> dict:
        """返回 {move, score_cp, pv}。score_cp 为当前方视角的厘兵分；
        pv 为最佳走法主线（UCCI 字符串列表）。"""
        if not self.is_ready:
            return {}
        fen = board.to_fen()
        with self._lock:
            # 排空上次超时遗留的输出行，避免污染本次解析
            self._drain()
            self._send(f"setoption name Skill Level value {self._skill_level}")
            self._send(f"position fen {fen}")
            self._send(f"go movetime {movetime_ms}")
            raw = self._read_until_bestmove_full(timeout=movetime_ms / 1000 + 5)
        if not raw:
            return {}
        bestmove = raw.get("bestmove")
        info_lines = raw.get("info_lines", [])
        # 取最后一条含 score 的 info
        score_cp = None
        pv: list = []
        for line in reversed(info_lines):
            parts = line.split()
            if "score" in parts:
                idx = parts.index("score")
                if len(parts) > idx + 2 and parts[idx + 1] == "cp":
                    try:
                        score_cp = int(parts[idx + 2])
                    except ValueError:
                        pass
            if "pv" in parts:
                pv = parts[parts.index("pv") + 1:]
            if score_cp is not None and pv:
                break
        move = None
        if bestmove and bestmove != "(none)":
            try:
                move = parse_ucci_move(bestmove)
            except ValueError:
                pass
        return {"move": move, "score_cp": score_cp, "pv": pv, "bestmove": bestmove}

    # ---- 底层 IO ----
    def _send(self, cmd: str):
        if self._proc and self._proc.stdin:
            self._proc.stdin.write(cmd + "\n")
            self._proc.stdin.flush()

    def _wait_for(self, token: str, timeout: float = 10) -> bool:
        end_time = _now() + timeout
        while _now() < end_time:
            line = self._readline(timeout=end_time - _now())
            if line is None:
                return False
            if token in line:
                return True
        return False

    def _read_until_bestmove(self, timeout: float) -> Optional[str]:
        """仅返回 bestmove 的 UCCI 字符串（兼容旧调用）。"""
        full = self._read_until_bestmove_full(timeout)
        return full.get("bestmove") if full else None

    def _read_until_bestmove_full(self, timeout: float) -> dict:
        """返回 {bestmove, info_lines}，保留 info 行用于评分/PV 解析。"""
        end_time = _now() + timeout
        info_lines: list = []
        bestmove = None
        while _now() < end_time:
            line = self._readline(timeout=end_time - _now())
            if line is None:
                break
            line = line.strip()
            if line.startswith("info "):
                info_lines.append(line)
            elif line.startswith("bestmove"):
                parts = line.split()
                if len(parts) >= 2:
                    bestmove = parts[1]
                break
        return {"bestmove": bestmove, "info_lines": info_lines}

    def _pump_output(self):
        """后台线程：持续读取引擎 stdout 放入队列。"""
        try:
            for line in self._proc.stdout:
                self._lines.put(line)
        except Exception:
            pass

    def _drain(self):
        """清空队列中的残留输出行。"""
        try:
            while True:
                self._lines.get_nowait()
        except queue.Empty:
            pass

    def _readline(self, timeout: float) -> Optional[str]:
        """带超时的逐行读取（从队列取，超时不杀引擎进程）。"""
        if not self._proc or self._proc.poll() is not None:
            self.is_ready = False
            return None
        try:
            return self._lines.get(timeout=max(0.05, timeout))
        except queue.Empty:
            return None


def _now() -> float:
    import time
    return time.time()
