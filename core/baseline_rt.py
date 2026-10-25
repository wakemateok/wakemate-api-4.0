# baseline_rt.py
import numpy as np
from .time_context import taipei_time

# --- circadian ---
def sigmoid(hour: int, L: float = 100.0, x0: float = 14.0, k: float = 0.2) -> float:
    return L / (1.0 + np.exp(-k * (hour - x0)))

# --- sleep debt config (可調) ---
IDEAL_SLEEP_H = 8.0
ALPHA_EXCESS = 0.15   # 超過 8h 的「有效恢復」折扣（0.1~0.2 都合理）
DEBT_GAIN = 18.0      # 每 1 小時缺睡 → RT 增加多少 ms（建議 10~25 之間調）

def _effective_sleep_hours(sleep_h: float) -> float:
    """RDF: 超過 8 小時後報酬遞減，但不會變成 '睡越多越超清醒'。"""
    if sleep_h <= IDEAL_SLEEP_H:
        return sleep_h
    return IDEAL_SLEEP_H + ALPHA_EXCESS * (sleep_h - IDEAL_SLEEP_H)

def _last_sleep_duration_hours_before(t_obs, sleep_intervals) -> float:
    """
    找到 t_obs 前最近一段「已經結束」的睡眠區間長度（小時）。
    sleep_intervals: list[(start_dt, end_dt)] sorted by start_dt
    """
    prev = None
    for (s, e) in sleep_intervals:
        if s is None or e is None:
            continue
        if e <= t_obs:
            prev = (s, e)
        else:
            break
    if not prev:
        return 0.0
    s, e = prev
    return max(0.0, (e - s).total_seconds() / 3600.0)

def _sleep_debt_term_ms(t_obs, sleep_intervals) -> float:
    """只懲罰缺睡；超睡不加分（避免 12h 比 8h 更 '超清醒'）。"""
    sleep_h = _last_sleep_duration_hours_before(t_obs, sleep_intervals)
    if sleep_h <= 0:
        return 0.0
    eff = _effective_sleep_hours(sleep_h)
    debt_h = max(0.0, IDEAL_SLEEP_H - eff)
    return DEBT_GAIN * debt_h

def compute_baseline_rt(t_obs, sleep_intervals, p0_value: float, trait: float = 0.0) -> float:
    """
    回傳「無咖啡因」baseline mean RT (ms)，含：
      - 個人化基線 p0_value（建議視為 well-rested baseline）
      - circadian sigmoid(hour)
      - sleep debt 懲罰（缺睡才加，超睡不降）
    trait: 如果你已經把 trait 吃進 p0_value（目前你是 p0=270+trait），那就傳 0.0。
    """
    asleep = any(s <= t_obs < e for (s, e) in sleep_intervals if s is not None and e is not None)

    base = float(p0_value) + float(trait)
    if asleep:
        return base  # 睡眠期間你之後會設成 NaN/None，這裡仍回傳 base 方便一致

    return base + sigmoid(taipei_time(t_obs).hour) + _sleep_debt_term_ms(t_obs, sleep_intervals)
