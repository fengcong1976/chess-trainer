# -*- coding: utf-8 -*-
"""轻量音效系统（零第三方依赖）：标准库合成 wav + winsound 异步播放。

合成采用「模态合成 + 噪声瞬态」敲击物理模型：
  move    落子 = 2ms 白噪碰撞瞬态 + 木板共振峰(880/1450/2350Hz) + 低频体腔(260Hz) → 真实木「嗒」
  capture 吃子 = 两记敲击快速连击（第二记更低沉更重）→「啪嗒」
  check   将军 = E5→A5 柔和钟音上行（带非谐泛音的钟味）
  win     胜利 = 五声音阶（宫商角徵羽）C5-D5-E5-G5-A5-C6 拨弦琶音，中国风
  wrong   走错 = 180Hz 柔和闷鼓「咚」，不刺耳

播放用 winsound.PlaySound(SND_ASYNC)，立即返回不阻塞 UI；同一时刻
只保留最新一个声音（将军音会覆盖落子音，反而更醒目）。
"""
import math
import os
import random
import struct
import wave

try:
    import winsound
except ImportError:  # 非 Windows 平台静默降级为无声
    winsound = None

try:
    from ..resources import bundle_root, data_dir as _res_data_dir
    _SND_DIR = os.path.join(bundle_root(), "sounds")   # 打包后从解包资源读
except ImportError:
    _res_data_dir = None
    _SND_DIR = os.path.join(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))), "sounds")
# 打包后解包目录只读且 wav 已随包分发；若缺失（异常情况）落到可写 data/sounds 再合成
if not os.path.isdir(_SND_DIR):
    try:
        os.makedirs(_SND_DIR, exist_ok=True)
    except OSError:
        _SND_DIR = os.path.join(_res_data_dir() if _res_data_dir else os.getcwd(),
                                "sounds")
_RATE = 22050

_ALL = ("move", "capture", "check", "win", "wrong", "lose", "draw")


# ---------------------------------------------------------------- 合成基元
def _mode(freq: float, dur: float, vol: float, tau: float,
          phase: float = 0.0, harm2: float = 0.0) -> list[float]:
    """单共振模态：正弦(可选二次泛音) * 指数衰减。tau 越小越干脆。"""
    n = int(_RATE * dur)
    out = []
    for i in range(n):
        t = i / _RATE
        v = math.sin(2 * math.pi * freq * t + phase)
        if harm2 > 0.0:
            v += harm2 * math.sin(2 * math.pi * freq * 2.76 * t + phase)  # 非谐泛音（钟味）
        out.append(vol * v * math.exp(-t / tau))
    return out


def _noise(dur: float, vol: float, tau: float = 0.0012, seed: int = 42) -> list[float]:
    """碰撞瞬态：白噪 * 快指数衰减（固定种子保证每次合成结果一致）。"""
    rnd = random.Random(seed)
    n = int(_RATE * dur)
    return [vol * (rnd.random() * 2.0 - 1.0) * math.exp(-(i / _RATE) / tau)
            for i in range(n)]


def _place(buf: list[float], seg: list[float], offset_sec: float) -> None:
    """把 seg 叠加进 buf 的 offset 位置（原位混合）。"""
    off = int(_RATE * offset_sec)
    for i, v in enumerate(seg):
        j = off + i
        if 0 <= j < len(buf):
            buf[j] += v


def _blank(dur: float) -> list[float]:
    return [0.0] * int(_RATE * dur)


def _knock(vol: float = 1.0) -> list[float]:
    """一记木质敲击：噪声瞬态 + 三共振峰 + 低频体腔。"""
    return _mix(
        _noise(0.005, 0.85 * vol),
        _mode(880.0, 0.055, 0.50 * vol, 0.017),
        _mode(1450.0, 0.038, 0.28 * vol, 0.009),
        _mode(2350.0, 0.022, 0.13 * vol, 0.004),
        _mode(260.0, 0.13, 0.42 * vol, 0.042),
    )


def _mix(*parts: list[float]) -> list[float]:
    """多段同步叠加（取最长长度）。"""
    n = max(len(p) for p in parts)
    out = [0.0] * n
    for p in parts:
        for i, v in enumerate(p):
            out[i] += v
    return out


# ---------------------------------------------------------------- 五种音效
def _samples(name: str) -> list[float]:
    if name == "move":                     # 落子：一记清脆木「嗒」
        buf = _knock(vol=1.0)
        return buf
    if name == "capture":                  # 吃子：轻敲 + 55ms 后一记更重更低的砸
        buf = _blank(0.17)
        _place(buf, _knock(vol=0.75), 0.0)
        _place(buf, _mix(
            _noise(0.006, 1.0, tau=0.0015, seed=7),
            _mode(620.0, 0.09, 0.55, 0.030),
            _mode(210.0, 0.14, 0.50, 0.050),
        ), 0.055)
        return buf
    if name == "check":                    # 将军：E5→A5 柔和钟音上行
        buf = _blank(0.40)
        _place(buf, _mode(659.0, 0.20, 0.42, 0.13, harm2=0.18), 0.0)
        _place(buf, _mode(880.0, 0.26, 0.46, 0.16, harm2=0.18), 0.11)
        return buf
    if name == "win":                      # 胜利：宫商角徵羽上行琶音 C5-D5-E5-G5-A5-C6
        buf = _blank(1.0)
        seq = ((523.0, 0.00), (587.0, 0.105), (659.0, 0.21),
               (784.0, 0.315), (880.0, 0.42), (1046.5, 0.55))
        for f, t0 in seq:
            last = t0 >= 0.5
            _place(buf, _mode(f, 0.45 if last else 0.16,
                              0.40 if last else 0.34,
                              0.30 if last else 0.075, harm2=0.15), t0)
        return buf
    if name == "wrong":                    # 走错：180Hz 柔和闷鼓单「咚」
        return _mix(
            _noise(0.004, 0.35, tau=0.0009, seed=11),
            _mode(180.0, 0.26, 0.95, 0.085),
            _mode(92.0, 0.30, 0.45, 0.110),
        )
    if name == "lose":                     # 失败：C5→A4→F4 低沉下行琶音
        buf = _blank(0.90)
        seq = ((523.0, 0.00), (440.0, 0.14), (349.0, 0.30))
        for f, t0 in seq:
            _place(buf, _mode(f, 0.32, 0.38, 0.25, harm2=0.12), t0)
        return buf
    if name == "draw":                     # 和棋：D5-E5 中性双音
        buf = _blank(0.45)
        _place(buf, _mode(587.0, 0.22, 0.42, 0.18, harm2=0.10), 0.0)
        _place(buf, _mode(659.0, 0.22, 0.38, 0.18, harm2=0.10), 0.18)
        return buf
    raise KeyError(name)


def ensure_sound_files() -> dict[str, str]:
    """合成（若缺失）全部音效 wav，返回 name->path。峰值归一到 0.85 防削波失真。"""
    os.makedirs(_SND_DIR, exist_ok=True)
    paths = {}
    for name in _ALL:
        p = os.path.join(_SND_DIR, f"{name}.wav")
        paths[name] = p
        if os.path.isfile(p) and os.path.getsize(p) > 44:
            continue
        data = _samples(name)
        peak = max((abs(v) for v in data), default=1.0) or 1.0
        scale = 0.85 / peak
        frames = b"".join(
            struct.pack("<h", max(-32767, min(32767, int(v * scale * 32767))))
            for v in data
        )
        with wave.open(p, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(_RATE)
            w.writeframes(frames)
    return paths


_PATHS = ensure_sound_files()


class _Sound:
    """全局音效开关 + 播放入口（GUI 菜单可切换 enabled）。"""

    def __init__(self):
        self.enabled = True

    def play(self, name: str) -> None:
        if not self.enabled or winsound is None:
            return
        p = _PATHS.get(name)
        if p and os.path.isfile(p):
            try:
                winsound.PlaySound(p, winsound.SND_FILENAME | winsound.SND_ASYNC)
            except Exception:
                pass


sound = _Sound()
