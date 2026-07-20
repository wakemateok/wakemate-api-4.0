import numpy as np
import psycopg2
from datetime import timedelta
from typing import Optional, Tuple, List
from statistics import mean
from .baseline_rt import compute_baseline_rt

# Config / 超參數（可調）
ALPHA_TRAIT = 0.12        # trait EMA learning rate (kss<=4)
ALPHA_FALLBACK = 0.04     # fallback (kss>=5) learning rate, 較保守
MIN_PVT_7D = 14           # 至少 14 次 PVT 才嘗試更新 kc
MIN_DISTINCT_DAYS_7D = 7  # 過去 7 天至少 7 個不同日
KC_GRID = np.arange(0.09, 0.33 + 1e-9, 0.01)
KC_UPDATE_TOLERANCE = 0.01  # new kc 與 old kc 差異超過此值才更新

# Defaults if no users_params row exists
DEFAULTS = {
    "m_c": 1.0,
    "k_a": 1.25,
    "k_c": 0.20,
    "trait_alertness": 0.0,
    "p0_value": 270.0
}

# --- 數學函式 ---
def _sigmoid(hour: int, L: float = 100.0, x0: float = 14.0, k: float = 0.2) -> float:
    return L / (1.0 + np.exp(-k * (hour - x0)))

def _predict_rt_single(t_obs, sleep_intervals, intakes,
                       m_c: float, k_a: float, k_c: float,
                       p0_value: float, trait: float = 0.0) -> float:
    """
    對單一時間點 t_obs 預測 mean RT (ms)。
    baseline 改為：p0_value + circadian + sleep debt（超睡不加分）。
    """
    base = compute_baseline_rt(t_obs, sleep_intervals, p0_value, trait=trait)

    g = 1.0
    for (take_time, dose) in intakes:
        if take_time > t_obs:
            continue
        dt_h = (t_obs - take_time).total_seconds() / 3600.0
        if dt_h <= 0:
            continue
        if abs(k_a - k_c) < 1e-9:
            continue
        phi = np.exp(-k_c * dt_h) - np.exp(-k_a * dt_h)
        eff = 1.0 / (1.0 + (m_c * float(dose) / 200.0) *
                     (k_a / (k_a - k_c)) * phi)
        g *= eff

    return base * g


# --- DB helper functions ---
def fetch_user_params(cur, user_id):
    cur.execute("""
        SELECT m_c, k_a, k_c, trait_alertness, last_trait_update,
               last_kc_update, pvt_count_7d, pvt_avg_7d, p0_value
        FROM users_params
        WHERE user_id = %s
        ORDER BY updated_at DESC
        LIMIT 1
    """, (user_id,))
    r = cur.fetchone()
    if not r:
        return dict(DEFAULTS)
    m_c, k_a, k_c, trait_alertness, last_trait_update, last_kc_update, \
        pvt_count_7d, pvt_avg_7d, p0_value = r
    return {
        "m_c": float(m_c) if m_c is not None else DEFAULTS["m_c"],
        "k_a": float(k_a) if k_a is not None else DEFAULTS["k_a"],
        "k_c": float(k_c) if k_c is not None else DEFAULTS["k_c"],
        "trait_alertness": float(trait_alertness) if trait_alertness is not None else DEFAULTS["trait_alertness"],
        "last_trait_update": last_trait_update,
        "last_kc_update": last_kc_update,
        "pvt_count_7d": int(pvt_count_7d) if pvt_count_7d is not None else 0,
        "pvt_avg_7d": float(pvt_avg_7d) if pvt_avg_7d is not None else None,
        "p0_value": float(p0_value) if p0_value is not None else DEFAULTS["p0_value"]
    }

def upsert_users_params(cur, user_id, params: dict):
    cur.execute("""
        INSERT INTO users_params
        (user_id, m_c, k_a, k_c, trait_alertness, p0_value,
         last_trait_update, last_kc_update, pvt_count_7d, pvt_avg_7d, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
        ON CONFLICT (user_id) DO UPDATE
          SET m_c = EXCLUDED.m_c,
              k_a = EXCLUDED.k_a,
              k_c = EXCLUDED.k_c,
              trait_alertness = EXCLUDED.trait_alertness,
              p0_value = EXCLUDED.p0_value,
              last_trait_update = EXCLUDED.last_trait_update,
              last_kc_update = EXCLUDED.last_kc_update,
              pvt_count_7d = EXCLUDED.pvt_count_7d,
              pvt_avg_7d = EXCLUDED.pvt_avg_7d,
              updated_at = NOW()
    """, (
        user_id,
        params.get("m_c"),
        params.get("k_a"),
        params.get("k_c"),
        params.get("trait_alertness"),
        params.get("p0_value"),
        params.get("last_trait_update"),
        params.get("last_kc_update"),
        params.get("pvt_count_7d"),
        params.get("pvt_avg_7d")
    ))

# --- data fetchers ---
def get_all_sleep_intervals(cur, user_id):
    cur.execute("""
        SELECT sleep_start_time, sleep_end_time
        FROM users_real_sleep_data
        WHERE user_id = %s
          AND is_active = TRUE
          AND deleted_at IS NULL
          AND invalidated_at IS NULL
        ORDER BY sleep_start_time
    """, (user_id,))
    return cur.fetchall()

def get_all_intakes(cur, user_id):
    cur.execute("""
        SELECT taking_timestamp, caffeine_amount
        FROM users_real_time_intake
        WHERE user_id = %s
          AND is_active = TRUE
          AND deleted_at IS NULL
          AND invalidated_at IS NULL
          AND caffeine_amount BETWEEN 1 AND 500
        ORDER BY taking_timestamp
    """, (user_id,))
    return cur.fetchall()

def get_pvts_last_7days(cur, user_id):
    cur.execute("""
        SELECT test_at, mean_rt, kss_level
        FROM users_pvt_results
        WHERE user_id = %s
          AND test_at >= NOW() - INTERVAL '7 days'
        ORDER BY test_at
    """, (user_id,))
    return cur.fetchall()

def update_pvt_7d_stats(cur, user_id):
    cur.execute("""
        SELECT COUNT(*), AVG(mean_rt)
        FROM users_pvt_results
        WHERE user_id = %s
          AND test_at >= NOW() - INTERVAL '7 days'
    """, (user_id,))
    cnt, avg = cur.fetchone()
    cnt = int(cnt or 0)
    avg = float(avg) if avg is not None else None

    cur.execute("""
        SELECT COUNT(DISTINCT date_trunc('day', test_at))
        FROM users_pvt_results
        WHERE user_id = %s
          AND test_at >= NOW() - INTERVAL '7 days'
    """, (user_id,))
    distinct_days = cur.fetchone()[0] or 0

    return cnt, avg, int(distinct_days)

# --- P0 (trait) 更新 ---
def update_p0_for_user(conn, user_id,
                       alpha=ALPHA_TRAIT,
                       alpha_fallback=ALPHA_FALLBACK,
                       min_pvt_7d=MIN_PVT_7D,
                       min_days=MIN_DISTINCT_DAYS_7D):
    cur = conn.cursor()
    try:
        params = fetch_user_params(cur, user_id)
        cnt_7d, avg_7d, distinct_days = update_pvt_7d_stats(cur, user_id)

        rows = get_pvts_last_7days(cur, user_id)
        if not rows:
            cur.close()
            return False

        sleep_intervals = get_all_sleep_intervals(cur, user_id)
        filtered_rows = []
        for (test_at, mean_rt, kss) in rows:
            in_sleep = any(start <= test_at < end for (start, end) in sleep_intervals)
            if not in_sleep:
                filtered_rows.append((test_at, float(mean_rt), kss))
        if not filtered_rows:
            cur.close()
            return False

        # --- 補全不足 ---
        if cnt_7d < min_pvt_7d or distinct_days < min_days:
            first_test_at, _, _ = filtered_rows[0]
            while len(filtered_rows) < min_pvt_7d:
                filtered_rows.append((first_test_at, DEFAULTS["p0_value"], 4))

        # 選樣本
        S = [r for r in filtered_rows if (r[2] is not None and r[2] <= 4)]
        used_alpha = alpha
        if not S:
            S = [r for r in filtered_rows if (r[2] is not None and r[2] >= 5)]
            used_alpha = alpha_fallback
            if not S:
                return False

        avg_mean_rt = mean([r[1] for r in S])
        group_baselines = [270.0 + _sigmoid(r[0].hour) for r in S]
        mean_group_baseline = mean(group_baselines)
        observed_trait = float(avg_mean_rt) - float(mean_group_baseline)

        trait_old = params.get("trait_alertness", 0.0)
        trait_new = (1.0 - used_alpha) * float(trait_old) + used_alpha * observed_trait
        trait_new = max(min(trait_new, 120.0), -120.0)

        # baseline 限制範圍
        p0_value = max(min(270.0 + trait_new, 450.0), 200.0)

        params.update({
            "trait_alertness": float(trait_new),
            "p0_value": float(p0_value),
            "last_trait_update": rows[-1][0],
            "pvt_count_7d": cnt_7d,
            "pvt_avg_7d": avg_7d
        })
        upsert_users_params(cur, user_id, params)
        conn.commit()
        return True
    except Exception as e:
        conn.rollback()
        print(f"[update_p0_for_user] user {user_id} 發生錯誤: {e}")
        raise
    finally:
        cur.close()

# --- kc 更新 ---
def maybe_update_kc_for_user(conn, user_id,
                             min_pvt_7d=MIN_PVT_7D,
                             min_days=MIN_DISTINCT_DAYS_7D,
                             kc_grid=KC_GRID,
                             tol=KC_UPDATE_TOLERANCE):
    cur = conn.cursor()
    try:
        params = fetch_user_params(cur, user_id)
        cnt_7d, avg_7d, distinct_days = update_pvt_7d_stats(cur, user_id)

        cur.execute("""
            SELECT test_at, mean_rt
            FROM users_pvt_results
            WHERE user_id = %s
              AND test_at >= NOW() - INTERVAL '7 days'
            ORDER BY test_at
        """, (user_id,))
        rows = cur.fetchall()
        if not rows:
            cur.close()
            return False

        sleep_intervals = get_all_sleep_intervals(cur, user_id)
        intakes = get_all_intakes(cur, user_id)
        usable_rows = []
        for (test_at, mean_rt) in rows:
            in_sleep = any(start <= test_at < end for (start, end) in sleep_intervals)
            if not in_sleep:
                usable_rows.append((test_at, float(mean_rt)))

        if not usable_rows:
            cur.close()
            return False

        # --- 補全不足 ---
        if cnt_7d < min_pvt_7d or distinct_days < min_days:
            first_test_at, _ = usable_rows[0]
            while len(usable_rows) < min_pvt_7d:
                usable_rows.append((first_test_at, DEFAULTS["p0_value"]))

        trait = params.get("trait_alertness", 0.0)
        m_c = params.get("m_c", DEFAULTS["m_c"])
        k_a = params.get("k_a", DEFAULTS["k_a"])
        k_c_current = params.get("k_c", DEFAULTS["k_c"])

        best_kc = k_c_current
        best_mse = None
        for kc_cand in kc_grid:
            if abs(k_a - kc_cand) < 1e-6:
                continue
            se = 0.0
            n = 0
            for (test_at, mean_rt) in usable_rows:
                p0_value = params.get("p0_value", DEFAULTS["p0_value"])

                y_hat = _predict_rt_single(
                    test_at, sleep_intervals, intakes,
                    m_c, k_a, kc_cand,
                    p0_value=p0_value, trait=0.0
                )

                # 不做 +trait，因為 p0_value 已經是 270+trait_new
                y_hat_adjusted = y_hat

                err = (float(mean_rt) - y_hat_adjusted) ** 2
                se += err
                n += 1
            if n == 0:
                continue
            mse = se / n
            if best_mse is None or mse < best_mse:
                best_mse = mse
                best_kc = float(kc_cand)

        if abs(best_kc - k_c_current) >= tol:
            cur.execute("""
                UPDATE users_params
                SET k_c = %s, last_kc_update = NOW(),
                    pvt_count_7d = %s, pvt_avg_7d = %s, updated_at = NOW()
                WHERE user_id = %s
            """, (best_kc, cnt_7d, avg_7d, user_id))
            conn.commit()
            return True
        else:
            params.update({"pvt_count_7d": cnt_7d, "pvt_avg_7d": avg_7d})
            upsert_users_params(cur, user_id, params)
            conn.commit()
            return False
    except Exception as e:
        conn.rollback()
        print(f"[maybe_update_kc_for_user] user {user_id} 發生錯誤: {e}")
        raise
    finally:
        cur.close()

# --- API 封裝 ---
def update_user_params(conn, user_id):
    """
    更新指定 user_id 的個人化參數。
    執行兩步驟：
      1. update_p0_for_user → 更新 trait_alertness 與 p0_value
      2. maybe_update_kc_for_user → 嘗試 grid-search 更新 kc
    """
    result = {"trait_updated": False, "kc_updated": False}

    try:
        trait_updated = update_p0_for_user(conn, user_id)
        kc_updated = maybe_update_kc_for_user(conn, user_id)

        result["trait_updated"] = bool(trait_updated)
        result["kc_updated"] = bool(kc_updated)

        # 🔹 讀取最新參數，方便 log 與 API 回傳
        cur = conn.cursor()
        cur.execute("""
            SELECT p0_value, k_c, trait_alertness
            FROM users_params
            WHERE user_id = %s
            ORDER BY updated_at DESC
            LIMIT 1
        """, (user_id,))
        row = cur.fetchone()
        cur.close()

        if row:
            p0_value, k_c, trait = row
            print(f"[update_user_params] user {user_id} → "
                  f"trait_updated={result['trait_updated']}, kc_updated={result['kc_updated']}, "
                  f"p0={p0_value:.2f}, kc={k_c:.3f}, trait={trait:.2f}")
            result.update({
                "p0_value": float(p0_value),
                "k_c": float(k_c),
                "trait_alertness": float(trait)
            })

        return result

    except Exception as e:
        print(f"[update_user_params] user {user_id} 發生錯誤: {e}")
        raise

# --- 批次入口 ---
def get_user_ids_with_pvt(conn):
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT user_id FROM users_pvt_results")
    ids = [r[0] for r in cur.fetchall()]
    cur.close()
    return ids

def update_all_users(conn, user_ids: Optional[List] = None):
    cur = conn.cursor()
    try:
        if user_ids is None:
            user_ids = get_user_ids_with_pvt(conn)
        for uid in user_ids:
            print(f"[personalize] Updating P0/trait for user {uid} ...")
            update_p0_for_user(conn, uid)
            print(f"[personalize] Maybe updating kc for user {uid} ...")
            updated = maybe_update_kc_for_user(conn, uid)
            print(f"[personalize] kc updated: {updated}")
    finally:
        cur.close()
