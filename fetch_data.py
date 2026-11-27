import io
import json
import math
import os
import random
import urllib.parse
from curl_cffi import requests
import pandas as pd
import pybaseball as pyb

pyb.cache.enable()

YEARS = list(range(2015, 2027))
DEFAULT_YEAR = 2026
CACHE_DIR = "cache"
CF_PROFILE_DIR = os.path.join(CACHE_DIR, "fg_chrome_profile")

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.fangraphs.com/leaders/major-league",
}


def clean_for_json(obj):
    """遞迴清除 NaN / Inf 並轉為原生 Python 型別，確保快取與輸出 JSON 合法"""
    if isinstance(obj, dict):
        return {str(k): clean_for_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [clean_for_json(v) for v in obj]
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if hasattr(obj, "item"):
        val = obj.item()
        if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
            return None
        return val
    return obj


def restore_int_keys(yd: dict) -> dict:
    """從快取讀取後，將各子字典的 player_id 鍵值轉回 int"""
    restored = {}
    for k, sub in yd.items():
        if isinstance(sub, dict):
            restored[k] = {int(pid): val for pid, val in sub.items()}
        else:
            restored[k] = sub
    return restored


def _create_fg_session():
    """建立帶有 Cloudflare Cookie 暖身的 curl_cffi Session，依序嘗試多組瀏覽器指紋以繞過 403"""
    profiles = ["chrome131", "chrome124", "chrome120", "chrome", "safari15_5", "edge101"]
    for prof in profiles:
        try:
            sess = requests.Session(impersonate=prof)
            # 先訪問排行榜主頁暖身取得 Cloudflare Cookie
            warm = sess.get(
                "https://www.fangraphs.com/leaders/major-league",
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                },
                timeout=20,
            )
            if warm.status_code == 200:
                return sess, prof
        except Exception:
            continue
    return requests.Session(impersonate="chrome"), "chrome"


def _pass_cloudflare_turnstile(page, timeout_sec: int = 35) -> bool:
    """自動偵測並模擬人類滑鼠點擊 Cloudflare Turnstile 驗證框，直到進入 FanGraphs 主頁"""
    last_click_sec = -10
    for sec in range(timeout_sec):
        title = (page.title() or "").lower()
        if "fangraphs" in title:
            page.wait_for_timeout(1200)
            return True

        if sec >= 2 and (sec - last_click_sec) >= 4:
            clicked = False
            # 1) 優先尋找 Cloudflare Turnstile iframe
            for sel in (
                'iframe[src*="challenges.cloudflare.com"]',
                'iframe[src*="turnstile"]',
                'div.cf-turnstile',
                '#turnstile-wrapper',
            ):
                try:
                    loc = page.locator(sel).first
                    if loc.count() > 0:
                        box = loc.bounding_box()
                        if box and box["width"] > 20 and box["height"] > 20:
                            tx = box["x"] + min(32.0, box["width"] * 0.15) + random.uniform(-3.0, 3.0)
                            ty = box["y"] + (box["height"] / 2.0) + random.uniform(-3.0, 3.0)
                            page.mouse.move(tx, ty, steps=random.randint(8, 15))
                            page.wait_for_timeout(random.randint(120, 280))
                            page.mouse.click(tx, ty)
                            last_click_sec = sec
                            clicked = True
                            print("  [Patchright] 已自動點擊 Cloudflare 驗證框，等待放行...")
                            break
                except Exception:
                    continue

            # 2) 若在 Shadow DOM 內，改由 frame 列表定位
            if not clicked:
                for fr in page.frames:
                    if "challenges.cloudflare.com" in (fr.url or ""):
                        try:
                            el = fr.frame_element()
                            box = el.bounding_box()
                            if box and box["width"] > 20 and box["height"] > 20:
                                tx = box["x"] + min(32.0, box["width"] * 0.15) + random.uniform(-3.0, 3.0)
                                ty = box["y"] + (box["height"] / 2.0) + random.uniform(-3.0, 3.0)
                                page.mouse.move(tx, ty, steps=random.randint(8, 15))
                                page.wait_for_timeout(random.randint(120, 280))
                                page.mouse.click(tx, ty)
                                last_click_sec = sec
                                print("  [Patchright] 已透過 Frame 定位自動點擊 Cloudflare 驗證框...")
                                break
                        except Exception:
                            continue

        page.wait_for_timeout(1000)

    return "fangraphs" in (page.title() or "").lower()


def _fetch_fangraphs_both_via_patchright(year: int, need_bat: bool = True, need_pit: bool = True):
    """單一持久化 Chrome 視窗同時抓取打者與投手 FanGraphs API（自動點擊 Turnstile 並保留 Cookie）"""
    try:
        from patchright.sync_api import sync_playwright
    except ImportError:
        print("  [提示] 尚未安裝 Patchright，請執行：pip install patchright && patchright install chrome")
        return {}, {}

    os.makedirs(CF_PROFILE_DIR, exist_ok=True)
    url = "https://www.fangraphs.com/api/leaders/major-league/data"
    fg_bat, fg_pit = {}, {}

    with sync_playwright() as p:
        try:
            context = p.chromium.launch_persistent_context(
                CF_PROFILE_DIR,
                channel="chrome",
                headless=False,
                no_viewport=True,
            )
        except Exception:
            context = p.chromium.launch_persistent_context(
                CF_PROFILE_DIR,
                headless=False,
                no_viewport=True,
            )

        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto("https://www.fangraphs.com/leaders/major-league", wait_until="domcontentloaded", timeout=45000)
            passed = _pass_cloudflare_turnstile(page, timeout_sec=35)
            if not passed:
                print(f"  [警告] Patchright 無法於 35 秒內通過 Cloudflare 驗證 (title={page.title()!r})")

            def fetch_json_in_browser(params_dict: dict):
                qs = urllib.parse.urlencode(params_dict)
                full_url = f"{url}?{qs}"
                return page.evaluate(
                    """async (u) => {
                        const r = await fetch(u, { credentials: 'include' });
                        if (!r.ok) return { ok: false, status: r.status };
                        return { ok: true, payload: await r.json() };
                    }""",
                    full_url,
                )

            extra_keys = [
                "BsR", "BaseRunning", "Def", "Defense", "Fielding", "Positional",
                "ERA-", "Stuff+", "sp_stuff", "HR/9",
                "GB%", "Pull%", "Z-Swing%", "Zone%", "G", "GS", "IP",
            ]

            targets = []
            if need_bat:
                targets.append(("bat", ["6", "2", "5"], fg_bat))
            if need_pit:
                targets.append(("pit", ["0", "36", "5"], fg_pit))

            for stats_type, extra_types, out_map in targets:
                base_params = {
                    "age": "", "pos": "all", "stats": stats_type, "lg": "all", "qual": "1",
                    "season": year, "season1": year,
                    "startdate": f"{year}-03-01", "enddate": f"{year}-11-01",
                    "month": "0", "hand": "", "team": "0", "pageitems": "2000", "pagenum": "1",
                    "ind": "0", "rost": "0", "players": "", "type": "8", "postseason": "",
                    "sortdir": "default", "sortstat": "WAR",
                }
                main_res = fetch_json_in_browser(base_params)
                if not main_res or not main_res.get("ok"):
                    st = main_res.get("status") if isinstance(main_res, dict) else "unknown"
                    print(f"  [警告] Patchright 抓取 {year} FanGraphs ({stats_type}) 主表失敗 (status={st})")
                    continue

                for row in main_res.get("payload", {}).get("data", []):
                    if row.get("playerid"):
                        pid = int(row["playerid"])
                        if row.get("sp_stuff") is not None and row.get("Stuff+") is None:
                            row["Stuff+"] = row["sp_stuff"]
                        out_map[pid] = row

                for t in extra_types:
                    try:
                        ex_res = fetch_json_in_browser({**base_params, "type": t})
                        if ex_res and ex_res.get("ok"):
                            for row in ex_res.get("payload", {}).get("data", []):
                                pid = row.get("playerid")
                                if pid and int(pid) in out_map:
                                    for k in extra_keys:
                                        if row.get(k) is not None:
                                            out_map[int(pid)][k] = row[k]
                                    if row.get("sp_stuff") is not None and out_map[int(pid)].get("Stuff+") is None:
                                        out_map[int(pid)]["Stuff+"] = row["sp_stuff"]
                    except Exception:
                        pass
        except Exception as e:
            print(f"  [警告] Patchright 抓取 {year} FanGraphs 例外: {e}")
        finally:
            context.close()

    return fg_bat, fg_pit


def fetch_fangraphs_leaders(year: int, stats_type: str = "bat"):
    """透過 FanGraphs Leaderboard API 取得真實 fWAR、BsR/Def、進階指標與風格特徵 (GB%/Pull%/Z-Swing%/Zone%)"""
    url = "https://www.fangraphs.com/api/leaders/major-league/data"
    base_params = {
        "age": "", "pos": "all", "stats": stats_type, "lg": "all", "qual": "1",
        "season": year, "season1": year,
        "startdate": f"{year}-03-01", "enddate": f"{year}-11-01",
        "month": "0", "hand": "", "team": "0", "pageitems": "2000", "pagenum": "1",
        "ind": "0", "rost": "0", "players": "", "type": "8", "postseason": "",
        "sortdir": "default", "sortstat": "WAR",
    }
    api_headers = {
        **HEADERS,
        "Origin": "https://www.fangraphs.com",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }

    leaders_map = {}
    active_sess = None
    profiles = ["chrome131", "chrome124", "chrome120", "safari15_5", "edge101", "chrome"]

    # 先試暖身 Session，若仍 403 則輪詢其他瀏覽器指紋
    sess_warm, warm_prof = _create_fg_session()
    try_list = [(sess_warm, warm_prof)] + [(None, p) for p in profiles if p != warm_prof]

    last_err = None
    for sess_obj, prof in try_list:
        try:
            s = sess_obj if sess_obj is not None else requests.Session(impersonate=prof)
            res = s.get(url, params=base_params, headers=api_headers, timeout=20)
            res.raise_for_status()
            data_rows = res.json().get("data", [])
            if data_rows:
                for row in data_rows:
                    if row.get("playerid"):
                        pid = int(row["playerid"])
                        if row.get("sp_stuff") is not None and row.get("Stuff+") is None:
                            row["Stuff+"] = row["sp_stuff"]
                        leaders_map[pid] = row
                active_sess = s
                break
        except Exception as e:
            last_err = e
            continue

    # 打者補抓: 6=Value(BsR/Def), 2=Batted Ball(GB%/Pull%), 5=Plate Discipline(Z-Swing%)
    # 投手補抓: 0=Standard(HR/9, G, GS, IP), 36=Stuff+, 5=Plate Discipline(Zone% 備援)
    extra_types = ["6", "2", "5"] if stats_type == "bat" else ["0", "36", "5"]
    extra_keys = [
        "BsR", "BaseRunning", "Def", "Defense", "Fielding", "Positional",
        "ERA-", "Stuff+", "sp_stuff", "HR/9",
        "GB%", "Pull%", "Z-Swing%", "Zone%", "G", "GS", "IP",
    ]

    if not leaders_map:
        print(f"  [警告] {year} FanGraphs ({stats_type}) curl_cffi 失敗 ({last_err})")
        return leaders_map
    for t in extra_types:
        try:
            ex_res = active_sess.get(url, params={**base_params, "type": t}, headers=api_headers, timeout=20)
            if ex_res.status_code == 200:
                for row in ex_res.json().get("data", []):
                    pid = row.get("playerid")
                    if pid and int(pid) in leaders_map:
                        for k in extra_keys:
                            if row.get(k) is not None:
                                leaders_map[int(pid)][k] = row[k]
                        if row.get("sp_stuff") is not None and leaders_map[int(pid)].get("Stuff+") is None:
                            leaders_map[int(pid)]["Stuff+"] = row["sp_stuff"]
        except Exception:
            pass

    return leaders_map


def enrich_ops_plus_and_era_plus(fg_bat: dict, fg_pit: dict):
    """計算標準聯盟校正 OPS+ 與 ERA+"""
    if fg_bat:
        df_b = pd.DataFrame.from_dict(fg_bat, orient="index")
        pa = pd.to_numeric(df_b.get("PA"), errors="coerce").fillna(0)
        obp = pd.to_numeric(df_b.get("OBP"), errors="coerce").fillna(0)
        slg = pd.to_numeric(df_b.get("SLG"), errors="coerce").fillna(0)
        total_pa = pa.sum()
        if total_pa > 0:
            lg_obp = (obp * pa).sum() / total_pa
            lg_slg = (slg * pa).sum() / total_pa
            for pid, r in fg_bat.items():
                p_obp = pd.to_numeric(r.get("OBP"), errors="coerce")
                p_slg = pd.to_numeric(r.get("SLG"), errors="coerce")
                if pd.notna(p_obp) and pd.notna(p_slg) and lg_obp > 0 and lg_slg > 0:
                    r["OPS+"] = int(round(100.0 * ((p_obp / lg_obp) + (p_slg / lg_slg) - 1.0)))

    if fg_pit:
        df_p = pd.DataFrame.from_dict(fg_pit, orient="index")
        ip = pd.to_numeric(df_p.get("IP"), errors="coerce").fillna(0)
        era = pd.to_numeric(df_p.get("ERA"), errors="coerce").fillna(0)
        total_ip = ip.sum()
        if total_ip > 0:
            lg_era = (era * ip).sum() / total_ip
            for pid, r in fg_pit.items():
                p_era = pd.to_numeric(r.get("ERA"), errors="coerce")
                if pd.notna(p_era) and p_era > 0:
                    r["ERA+"] = int(round(100.0 * (lg_era / p_era)))


def compute_league_percentiles(fg_map: dict, stats_type: str = "bat") -> dict:
    if not fg_map:
        return {}
    df = pd.DataFrame.from_dict(fg_map, orient="index")
    if stats_type == "bat" and "PA" in df.columns:
        pool = df[pd.to_numeric(df["PA"], errors="coerce") >= 50].copy()
        higher_better = [
            "WAR", "OPS+", "wRC+", "wOBA", "ISO", "BABIP", "REAL_BSR", "REAL_DEF",
            "GB%", "Pull%", "Z-Swing%",
        ]
        lower_better = []
    else:
        pool = df[pd.to_numeric(df["IP"], errors="coerce") >= 15].copy()
        higher_better = ["WAR", "ERA+", "LOB%", "Stuff+", "Zone%"]
        lower_better = ["FIP", "xFIP", "SIERA", "ERA-", "HR/9"]

    if pool.empty:
        pool = df.copy()

    pr_lookup = {int(pid): {} for pid in fg_map.keys()}
    for col in higher_better:
        if col not in pool.columns:
            continue
        series = pd.to_numeric(pool[col], errors="coerce").dropna()
        if series.empty:
            continue
        for pid, row in fg_map.items():
            val = pd.to_numeric(row.get(col), errors="coerce")
            if pd.notna(val):
                pct = int(round((series < val).mean() * 98 + 1))
                pr_lookup[int(pid)][col] = max(1, min(99, pct))

    for col in lower_better:
        if col not in pool.columns:
            continue
        series = pd.to_numeric(pool[col], errors="coerce").dropna()
        if series.empty:
            continue
        for pid, row in fg_map.items():
            val = pd.to_numeric(row.get(col), errors="coerce")
            if pd.notna(val):
                pct = int(round((series > val).mean() * 98 + 1))
                pr_lookup[int(pid)][col] = max(1, min(99, pct))

    return pr_lookup


def fetch_savant_sprint_speed(year: int) -> dict:
    """抓取指定球季 Baseball Savant Sprint Speed (ft/s) 實測數值"""
    sp_map = {}
    try:
        sp_res = requests.get(
            "https://baseballsavant.mlb.com/leaderboard/sprint_speed",
            params={"min_season": year, "max_season": year, "position": "", "team": "", "min": "1", "csv": "true"},
            impersonate="chrome", timeout=20,
        )
        if sp_res.status_code == 200:
            sp_df = pd.read_csv(io.StringIO(sp_res.text))
            id_col = "player_id" if "player_id" in sp_df.columns else ("id" if "id" in sp_df.columns else None)
            if id_col and "sprint_speed" in sp_df.columns:
                for _, r in sp_df.iterrows():
                    if pd.notna(r.get(id_col)) and pd.notna(r.get("sprint_speed")):
                        sp_map[int(r[id_col])] = float(r["sprint_speed"])
    except Exception:
        pass
    return sp_map


def fetch_savant_raw_metrics(year: int, player_type: str = "batter"):
    """抓取 Savant Custom Leaderboard、Bat Tracking、OAA 守備、Arm Strength、投手 Pitch Run Value、球種配比與 Arm Angle"""
    merged_map = {}
    url = "https://baseballsavant.mlb.com/leaderboard/custom"
    if player_type == "batter":
        selections = "xwoba,xba,xslg,exit_velocity_avg,barrel_batted_rate,hard_hit_percent,sweet_spot_percent,avg_swing_speed,squared_up_swing_rate,oz_swing_percent,whiff_percent,k_percent,bb_percent,sprint_speed"
    else:
        selections = "xera,xwoba,xba,exit_velocity_avg,barrel_batted_rate,hard_hit_percent,oz_swing_percent,whiff_percent,k_percent,bb_percent,ff_avg_speed,ff_avg_spin,groundballs_percent,release_extension,in_zone_percent"

    params = {
        "year": year, "type": player_type, "filter": "", "min": "1",
        "selections": selections,
        "chart": "false", "x": "xwoba", "y": "xwoba", "r": "no", "chartType": "beeswarm", "csv": "true",
    }
    try:
        res = requests.get(url, params=params, impersonate="chrome", timeout=20)
        res.raise_for_status()
        df = pd.read_csv(io.StringIO(res.text))
        if "player_id" in df.columns:
            merged_map = df.drop_duplicates(subset=["player_id"]).set_index("player_id").to_dict("index")
    except Exception as e:
        print(f"  [警告] {year} Savant 基礎數值 ({player_type}) 抓取失敗: {e}")

    if player_type == "batter":
        if year >= 2023:
            try:
                bt_res = requests.get(
                    "https://baseballsavant.mlb.com/leaderboard/bat-tracking",
                    params={
                        "dateStart": f"{year}-01-01", "dateEnd": f"{year}-12-31", "gameType": "R",
                        "minSwings": "1", "minGroupSwings": "1", "seasonStart": year, "seasonEnd": year,
                        "type": "batter", "csv": "true",
                    },
                    impersonate="chrome", timeout=20,
                )
                if bt_res.status_code == 200:
                    bt_df = pd.read_csv(io.StringIO(bt_res.text))
                    id_col = "id" if "id" in bt_df.columns else ("player_id" if "player_id" in bt_df.columns else None)
                    if id_col:
                        for _, r in bt_df.iterrows():
                            pid = int(r[id_col])
                            merged_map.setdefault(pid, {})
                            if pd.notna(r.get("avg_bat_speed")):
                                merged_map[pid]["avg_swing_speed"] = r["avg_bat_speed"]
                            if pd.notna(r.get("squared_up_per_swing")):
                                merged_map[pid]["squared_up_swing_rate"] = r["squared_up_per_swing"] * 100
                            elif pd.notna(r.get("squared_up_per_bat_contact")):
                                merged_map[pid]["squared_up_swing_rate"] = r["squared_up_per_bat_contact"] * 100
            except Exception:
                pass

        if year >= 2016:
            try:
                oaa_res = requests.get(
                    "https://baseballsavant.mlb.com/leaderboard/outs_above_average",
                    params={"type": "Fielder", "startYear": year, "endYear": year, "split": "no", "team": "", "range": "year", "min": "1", "pos": "", "roles": "", "viz": "hide", "csv": "true"},
                    impersonate="chrome", timeout=20,
                )
                if oaa_res.status_code == 200:
                    oaa_df = pd.read_csv(io.StringIO(oaa_res.text))
                    id_col = "player_id" if "player_id" in oaa_df.columns else ("id" if "id" in oaa_df.columns else None)
                    if id_col and "outs_above_average" in oaa_df.columns:
                        for _, r in oaa_df.iterrows():
                            if pd.notna(r.get(id_col)) and pd.notna(r.get("outs_above_average")):
                                pid = int(r[id_col])
                                merged_map.setdefault(pid, {})
                                merged_map[pid]["outs_above_average"] = r["outs_above_average"]
            except Exception:
                pass

        if year >= 2020:
            try:
                arm_res = requests.get(
                    "https://baseballsavant.mlb.com/leaderboard/arm-strength",
                    params={"type": "player", "year": year, "minThrows": "1", "pos": "", "team": "", "csv": "true"},
                    impersonate="chrome", timeout=20,
                )
                if arm_res.status_code == 200:
                    arm_df = pd.read_csv(io.StringIO(arm_res.text))
                    id_col = "player_id" if "player_id" in arm_df.columns else ("fielder_id" if "fielder_id" in arm_df.columns else None)
                    val_col = "arm_overall" if "arm_overall" in arm_df.columns else ("max_arm_strength" if "max_arm_strength" in arm_df.columns else None)
                    if id_col and val_col:
                        for _, r in arm_df.iterrows():
                            if pd.notna(r.get(id_col)) and pd.notna(r.get(val_col)):
                                pid = int(r[id_col])
                                merged_map.setdefault(pid, {})
                                merged_map[pid]["arm_overall"] = r[val_col]
            except Exception:
                pass

        for pid, sp_val in fetch_savant_sprint_speed(year).items():
            merged_map.setdefault(pid, {})
            if pd.isna(merged_map[pid].get("sprint_speed")):
                merged_map[pid]["sprint_speed"] = sp_val

    else:
        try:
            rv_res = requests.get(
                "https://baseballsavant.mlb.com/leaderboard/pitch-arsenal-stats",
                params={"type": "pitcher", "pitchType": "", "year": year, "team": "", "min": "1", "gameType": "R", "csv": "true"},
                impersonate="chrome", timeout=20,
            )
            if rv_res.status_code == 200:
                rv_df = pd.read_csv(io.StringIO(rv_res.text))
                id_col = "player_id" if "player_id" in rv_df.columns else ("pitcher" if "pitcher" in rv_df.columns else None)
                if id_col and "pitch_type" in rv_df.columns and "run_value" in rv_df.columns:
                    FASTBALLS = {"FF", "SI", "FC"}
                    BREAKINGS = {"SL", "CU", "KC", "ST", "SV"}
                    OFFSPEEDS = {"CH", "FS", "FO", "SC"}
                    MIX_GROUP = {
                        "FF": "FF", "SI": "SI", "FC": "FC",
                        "SL": "SL", "ST": "SL", "SV": "SL",
                        "CU": "CU", "KC": "CU", "CS": "CU",
                        "CH": "CH", "FS": "CH", "FO": "CH", "SC": "CH",
                    }
                    for _, r in rv_df.iterrows():
                        if pd.isna(r.get(id_col)) or pd.isna(r.get("run_value")):
                            continue
                        pid = int(r[id_col])
                        ptype = str(r.get("pitch_type", "")).strip().upper()
                        pitches = float(r.get("pitches") or 0)

                        if pd.notna(r.get("run_value_per_100")) and pitches > 0:
                            exact_rv = float(r["run_value_per_100"]) * pitches / 100.0
                        else:
                            exact_rv = float(r["run_value"])

                        merged_map.setdefault(pid, {})
                        merged_map[pid]["total_pitches"] = merged_map[pid].get("total_pitches", 0.0) + pitches

                        grp = MIX_GROUP.get(ptype)
                        if grp and pitches > 0:
                            mix_cnt = merged_map[pid].setdefault("_mix_cnt", {})
                            mix_cnt[grp] = mix_cnt.get(grp, 0.0) + pitches

                        if ptype in FASTBALLS:
                            merged_map[pid]["fastball_rv"] = merged_map[pid].get("fastball_rv", 0.0) + exact_rv
                            merged_map[pid]["fb_pitches"] = merged_map[pid].get("fb_pitches", 0.0) + pitches
                        elif ptype in BREAKINGS:
                            merged_map[pid]["breaking_rv"] = merged_map[pid].get("breaking_rv", 0.0) + exact_rv
                            merged_map[pid]["brk_pitches"] = merged_map[pid].get("brk_pitches", 0.0) + pitches
                        elif ptype in OFFSPEEDS:
                            merged_map[pid]["offspeed_rv"] = merged_map[pid].get("offspeed_rv", 0.0) + exact_rv
                            merged_map[pid]["off_pitches"] = merged_map[pid].get("off_pitches", 0.0) + pitches

                    for pid, pdata in merged_map.items():
                        tot_p = pdata.get("total_pitches", 0.0)
                        if tot_p > 0:
                            fb_i = round(pdata.get("fastball_rv", 0.0))
                            brk_i = round(pdata.get("breaking_rv", 0.0))
                            off_i = round(pdata.get("offspeed_rv", 0.0))
                            pdata["pitching_rv"] = float(fb_i + brk_i + off_i)
                            mix_cnt = pdata.pop("_mix_cnt", {})
                            if mix_cnt:
                                pdata["pitch_mix"] = {
                                    g: round((mix_cnt.get(g, 0.0) / tot_p) * 100.0, 1)
                                    for g in ("FF", "SI", "FC", "SL", "CU", "CH")
                                }
        except Exception:
            pass

        try:
            st_res = requests.get(
                "https://baseballsavant.mlb.com/leaderboard/swing-take",
                params={"year": year, "team": "", "leverage": "Neutral", "group": "Pitcher", "type": "All", "sub_type": "null", "min": "1", "csv": "True"},
                impersonate="chrome", timeout=20,
            )
            if st_res.status_code == 200:
                st_df = pd.read_csv(io.StringIO(st_res.text))
                id_col = "player_id" if "player_id" in st_df.columns else ("id" if "id" in st_df.columns else None)
                rv_col = "runs_all" if "runs_all" in st_df.columns else ("runs_prevented" if "runs_prevented" in st_df.columns else None)
                if id_col and rv_col:
                    for _, r in st_df.iterrows():
                        if pd.notna(r.get(id_col)) and pd.notna(r.get(rv_col)):
                            pid = int(r[id_col])
                            merged_map.setdefault(pid, {})
                            merged_map[pid]["pitching_rv"] = float(r[rv_col])
        except Exception:
            pass

        if year >= 2020:
            try:
                aa_res = requests.get(
                    "https://baseballsavant.mlb.com/leaderboard/pitcher-arm-angles",
                    params={"all": "true", "type": "pitcher", "year": year, "min": "1", "csv": "true"},
                    impersonate="chrome", timeout=20,
                )
                if aa_res.status_code == 200:
                    aa_df = pd.read_csv(io.StringIO(aa_res.text))
                    id_col = next((c for c in ("pitcher", "player_id", "id") if c in aa_df.columns), None)
                    val_col = next((c for c in ("ball_angle", "arm_angle", "avg_arm_angle") if c in aa_df.columns), None)
                    if id_col and val_col:
                        for _, r in aa_df.iterrows():
                            if pd.notna(r.get(id_col)) and pd.notna(r.get(val_col)):
                                pid = int(r[id_col])
                                merged_map.setdefault(pid, {})
                                merged_map[pid]["arm_angle"] = float(r[val_col])
            except Exception:
                pass

    return merged_map


def enrich_official_pitcher_prs(raw_pit_map: dict, pr_pit_dict: dict):
    """補齊球種 Run Value、GB %、xERA 的官方 PR 以及 Arm Angle / Statcast Zone% 聯盟 PR"""
    if not raw_pit_map or not pr_pit_dict:
        return

    qualified_pids = [pid for pid in pr_pit_dict.keys() if pid in raw_pit_map]
    if not qualified_pids:
        qualified_pids = list(raw_pit_map.keys())

    rv_specs = [
        ("pitching_rv", "total_pitches", 100),
        ("fastball_rv", "fb_pitches", 20),
        ("breaking_rv", "brk_pitches", 20),
        ("offspeed_rv", "off_pitches", 20),
    ]
    for rv_col, pitch_col, min_p in rv_specs:
        pool_vals = pd.Series([
            round(float(raw_pit_map[pid][rv_col]))
            for pid in raw_pit_map.keys()
            if rv_col in raw_pit_map[pid] and float(raw_pit_map[pid].get(pitch_col, 0) or 0) >= min_p
        ])
        if pool_vals.empty:
            continue
        for pid in qualified_pids:
            if rv_col not in raw_pit_map[pid] or float(raw_pit_map[pid].get(pitch_col, 0) or 0) < min_p:
                continue
            val = round(float(raw_pit_map[pid][rv_col]))
            pct = int(round((pool_vals < val).mean() * 99 + 1))
            pr_pit_dict.setdefault(pid, {})[rv_col] = max(1, min(100, pct))

    gb_vals = pd.Series([
        float(raw_pit_map[pid]["groundballs_percent"])
        for pid in qualified_pids
        if pd.notna(raw_pit_map[pid].get("groundballs_percent"))
    ])
    xera_vals = pd.Series([
        float(raw_pit_map[pid]["xera"])
        for pid in qualified_pids
        if pd.notna(raw_pit_map[pid].get("xera"))
    ])

    for col, pr_col in (("arm_angle", "arm_angle_pr"), ("in_zone_percent", "in_zone_percent_pr")):
        pool_s = pd.Series([
            float(raw_pit_map[pid][col])
            for pid in qualified_pids
            if pd.notna(raw_pit_map[pid].get(col))
        ])
        if pool_s.empty:
            pool_s = pd.Series([
                float(pdata[col])
                for pdata in raw_pit_map.values()
                if pd.notna(pdata.get(col))
            ])
        if not pool_s.empty:
            for pid, pdata in raw_pit_map.items():
                val = pdata.get(col)
                if pd.notna(val):
                    v = float(val)
                    pct = int(round((pool_s < v).mean() * 98 + 1))
                    pdata[pr_col] = max(1, min(99, pct))

    for pid in qualified_pids:
        row_pr = pr_pit_dict.setdefault(pid, {})
        if pd.isna(row_pr.get("gb")) and pd.isna(row_pr.get("gb_percent")):
            g_val = raw_pit_map[pid].get("groundballs_percent")
            if pd.notna(g_val) and not gb_vals.empty:
                v = float(g_val)
                pct = int(round((((gb_vals < v).sum() + 0.5 * (gb_vals == v).sum()) / len(gb_vals)) * 99 + 1))
                row_pr["gb_percent"] = max(1, min(100, pct))
        if pd.isna(row_pr.get("xera")):
            x_val = raw_pit_map[pid].get("xera")
            if pd.notna(x_val) and not xera_vals.empty:
                v = float(x_val)
                pct = int(round((((xera_vals > v).sum() + 0.5 * (xera_vals == v).sum()) / len(xera_vals)) * 99 + 1))
                row_pr["xera"] = max(1, min(100, pct))


def build_raw_percentile_fallback(raw_map: dict, metrics_def: list) -> dict:
    if not raw_map:
        return {}
    df = pd.DataFrame.from_dict(raw_map, orient="index")
    LOWER_IS_BETTER_BAT = {"oz_swing_percent", "whiff_percent", "k_percent"}
    HIGHER_IS_BETTER_PIT = {"ff_avg_speed", "ff_avg_spin", "oz_swing_percent", "whiff_percent", "k_percent", "groundballs_percent", "release_extension", "pitching_rv", "fastball_rv", "breaking_rv", "offspeed_rv"}

    fallback_pr = {int(pid): {} for pid in raw_map.keys()}
    is_pitcher = "ff_avg_speed" in df.columns

    for _, _, raw_col, _ in metrics_def:
        if not raw_col or raw_col not in df.columns:
            continue
        series = pd.to_numeric(df[raw_col], errors="coerce").dropna()
        if series.empty:
            continue

        higher_better = (raw_col in HIGHER_IS_BETTER_PIT) if is_pitcher else (raw_col not in LOWER_IS_BETTER_BAT)

        for pid, r in raw_map.items():
            val = pd.to_numeric(r.get(raw_col), errors="coerce")
            if pd.notna(val):
                pct = (series < val).mean() * 98 + 1 if higher_better else (series > val).mean() * 98 + 1
                fallback_pr[int(pid)][raw_col] = max(2, min(98, int(round(pct))))

    return fallback_pr


def format_pct_str(val):
    if val is None or pd.isna(val):
        return None
    f = float(val)
    if abs(f) <= 1.0:
        f *= 100.0
    return f"{f:.1f}%"


def build_database():
    os.makedirs(CACHE_DIR, exist_ok=True)

    print("1/4 正在載入 Chadwick 球員 ID 對照表...")
    chadwick = pyb.chadwick_register()
    min_year = min(YEARS)
    active_players = chadwick[
        (chadwick["mlb_played_last"] >= min_year)
        & (chadwick["key_mlbam"].notna())
    ].copy()

    active_players["key_mlbam"] = active_players["key_mlbam"].astype(int)
    active_players["full_name"] = (
        active_players["name_first"].fillna("")
        + " "
        + active_players["name_last"].fillna("")
    ).str.strip()

    BATTER_METRICS = [
        ("xwOBA", ["xwoba"], "xwoba", ".3f"),
        ("xBA", ["xba"], "xba", ".3f"),
        ("xSLG", ["xslg"], "xslg", ".3f"),
        ("Avg Exit Velo", ["exit_velocity"], "exit_velocity_avg", ".1f"),
        ("Barrel %", ["brl_percent"], "barrel_batted_rate", ".1f"),
        ("Hard-Hit %", ["hard_hit_percent"], "hard_hit_percent", ".1f"),
        ("LA Sweet-Spot %", ["sweet_spot_percent", "la_sweet_spot_percent", "sweet_spot", "la_sweet_spot"], "sweet_spot_percent", ".1f"),
        ("Bat Speed", ["bat_speed", "avg_bat_speed"], "avg_swing_speed", ".1f"),
        ("Squared-Up %", ["squared_up_percent", "squared_up_swing_rate", "squared_up_per_swing", "squared_up"], "squared_up_swing_rate", ".1f"),
        ("Chase %", ["chase_percent"], "oz_swing_percent", ".1f"),
        ("Whiff %", ["whiff_percent"], "whiff_percent", ".1f"),
        ("K %", ["k_percent"], "k_percent", ".1f"),
        ("BB %", ["bb_percent"], "bb_percent", ".1f"),
        ("Sprint Speed", ["sprint_speed"], "sprint_speed", ".1f"),
        ("Range (OAA)", ["oaa"], "outs_above_average", ".0f"),
        ("Arm Strength", ["arm_strength"], "arm_overall", ".1f"),
    ]

    PITCHER_METRICS = [
        ("Pitching Run Value", ["pitching_rv", "pitching_run_value", "rv_pitching"], "pitching_rv", ".0f"),
        ("Fastball Run Value", ["fastball_rv", "fastball_run_value", "rv_fb"], "fastball_rv", ".0f"),
        ("Breaking Run Value", ["breaking_rv", "breaking_run_value", "rv_brk"], "breaking_rv", ".0f"),
        ("Offspeed Run Value", ["offspeed_rv", "offspeed_run_value", "rv_off"], "offspeed_rv", ".0f"),
        ("xERA", ["xera"], "xera", ".2f"),
        ("xwOBA", ["xwoba"], "xwoba", ".3f"),
        ("xBA", ["xba"], "xba", ".3f"),
        ("Fastball Velo", ["fb_velocity"], "ff_avg_speed", ".1f"),
        ("Fastball Spin", ["fb_spin"], "ff_avg_spin", ".0f"),
        ("Avg Exit Velo", ["exit_velocity"], "exit_velocity_avg", ".1f"),
        ("Chase %", ["chase_percent"], "oz_swing_percent", ".1f"),
        ("Whiff %", ["whiff_percent"], "whiff_percent", ".1f"),
        ("K %", ["k_percent"], "k_percent", ".1f"),
        ("BB %", ["bb_percent"], "bb_percent", ".1f"),
        ("Barrel %", ["brl_percent"], "barrel_batted_rate", ".1f"),
        ("Hard-Hit %", ["hard_hit_percent"], "hard_hit_percent", ".1f"),
        ("GB %", ["gb", "gb_percent", "groundballs_percent"], "groundballs_percent", ".1f"),
        ("Extension", ["extension"], "release_extension", ".1f"),
    ]

    yearly_data = {}
    for yr in YEARS:
        cache_path = os.path.join(CACHE_DIR, f"{yr}.json")
        if yr != DEFAULT_YEAR and os.path.exists(cache_path):
            print(f"2/4 [快取命中] 直接讀取 {yr} 年快取並同步單邊 WAR PR...")
            with open(cache_path, "r", encoding="utf-8") as cf:
                yd_cached = restore_int_keys(json.load(cf))
            raw_bat_cached = yd_cached.get("raw_bat", {})
            if raw_bat_cached and not any("sprint_speed" in r for r in raw_bat_cached.values()):
                print(f"    -> 補抓 {yr} 年 Sprint Speed 並更新快取...")
                for pid, sp_val in fetch_savant_sprint_speed(yr).items():
                    raw_bat_cached.setdefault(pid, {})["sprint_speed"] = sp_val
                yd_cached["raw_bat"] = raw_bat_cached
                yd_cached["est_bat"] = build_raw_percentile_fallback(raw_bat_cached, BATTER_METRICS)
                with open(cache_path, "w", encoding="utf-8") as cf:
                    json.dump(clean_for_json(yd_cached), cf, ensure_ascii=False, separators=(",", ":"))
            yd_cached["fg_bat_pr"] = compute_league_percentiles(yd_cached.get("fg_bat", {}), "bat")
            yd_cached["fg_pit_pr"] = compute_league_percentiles(yd_cached.get("fg_pit", {}), "pit")
            yearly_data[yr] = yd_cached
            continue

        print(f"2/4 正在抓取 {yr} 年 Savant 與 FanGraphs 數據...")
        s_bat_pr = pyb.statcast_batter_percentile_ranks(yr)
        s_pit_pr = pyb.statcast_pitcher_percentile_ranks(yr)
        s_bat_raw = fetch_savant_raw_metrics(yr, "batter")
        s_pit_raw = fetch_savant_raw_metrics(yr, "pitcher")

        bat_raw_est_pr = build_raw_percentile_fallback(s_bat_raw, BATTER_METRICS)
        pit_raw_est_pr = build_raw_percentile_fallback(s_pit_raw, PITCHER_METRICS)

        pr_pit_dict = (
            s_pit_pr.drop_duplicates(subset=["player_id"]).set_index("player_id").to_dict("index")
            if not s_pit_pr.empty else {}
        )
        enrich_official_pitcher_prs(s_pit_raw, pr_pit_dict)

        fg_bat = fetch_fangraphs_leaders(yr, "bat")
        fg_pit = fetch_fangraphs_leaders(yr, "pit")
        if not fg_bat or not fg_pit:
            print(f"  [Patchright] 啟動單一持久化視窗自動補抓 {yr} 年 FanGraphs（打者/投手共用一次驗證）...")
            pw_bat, pw_pit = _fetch_fangraphs_both_via_patchright(yr, need_bat=not fg_bat, need_pit=not fg_pit)
            if not fg_bat and pw_bat:
                fg_bat = pw_bat
            if not fg_pit and pw_pit:
                fg_pit = pw_pit

        # 防呆保護：若 FanGraphs 遇到 403 導致回傳 0 筆，自動從舊快取救回該年 FanGraphs 數據，避免清空 mlb_data.json
        if (not fg_bat or not fg_pit) and os.path.exists(cache_path):
            print(f"  [防呆保護] {yr} 年 FanGraphs 抓取失敗，改用本地快取 {cache_path} 保留既有 FanGraphs 數據...")
            with open(cache_path, "r", encoding="utf-8") as cf:
                yd_fallback = restore_int_keys(json.load(cf))
            if not fg_bat:
                fg_bat = yd_fallback.get("fg_bat", {})
            if not fg_pit:
                fg_pit = yd_fallback.get("fg_pit", {})

        if not fg_bat and not fg_pit:
            raise RuntimeError(
                f"{yr} 年 FanGraphs API 遭到 403 封鎖且無本地快取 ({cache_path}) 可回退，"
                "已中止寫入 mlb_data.json 以免覆蓋現有資料。請稍後再試或先保留舊版 mlb_data.json。"
            )

        for pid, r in fg_bat.items():
            bsr_val = r.get("BsR") if r.get("BsR") is not None else r.get("BaseRunning")
            if r.get("Defense") is not None:
                def_val = r.get("Defense")
            elif r.get("Def") is not None:
                def_val = r.get("Def")
            elif r.get("Fielding") is not None and r.get("Positional") is not None:
                def_val = float(r["Fielding"]) + float(r["Positional"])
            else:
                def_val = r.get("Fielding")
            r["REAL_BSR"] = float(bsr_val) if bsr_val is not None else 0.0
            r["REAL_DEF"] = float(def_val) if def_val is not None else 0.0

        enrich_ops_plus_and_era_plus(fg_bat, fg_pit)
        fg_bat_pr = compute_league_percentiles(fg_bat, "bat")
        fg_pit_pr = compute_league_percentiles(fg_pit, "pit")

        yr_payload = {
            "pr_bat": (
                s_bat_pr.drop_duplicates(subset=["player_id"]).set_index("player_id").to_dict("index")
                if not s_bat_pr.empty else {}
            ),
            "pr_pit": pr_pit_dict,
            "raw_bat": s_bat_raw,
            "raw_pit": s_pit_raw,
            "est_bat": bat_raw_est_pr,
            "est_pit": pit_raw_est_pr,
            "fg_bat": fg_bat,
            "fg_pit": fg_pit,
            "fg_bat_pr": fg_bat_pr,
            "fg_pit_pr": fg_pit_pr,
        }
        yearly_data[yr] = yr_payload
        print(f"    {yr} 年完成：打者 {len(fg_bat)} 筆、投手 {len(fg_pit)} 筆。")

        # 僅在 FanGraphs 有實際抓到資料時寫入快取（含當年作為下次 403 防呆備份）
        if fg_bat and fg_pit:
            with open(cache_path, "w", encoding="utf-8") as cf:
                json.dump(clean_for_json(yr_payload), cf, ensure_ascii=False, separators=(",", ":"))

    # 用 FanGraphs API 自帶的 xMLBAMID 建立反查表，補齊 Chadwick 尚未更新的新秀 fg_id
    mlbam_to_fg = {}
    for yr in YEARS:
        for fg_map in (yearly_data[yr]["fg_bat"], yearly_data[yr]["fg_pit"]):
            for pid, r in fg_map.items():
                xm = r.get("xMLBAMID")
                if xm is not None and pd.notna(xm):
                    try:
                        mlbam_to_fg[int(xm)] = int(pid)
                    except (ValueError, TypeError):
                        pass

    print("3/4 正在整合 2015–2026 球員資料庫 (單邊 fWAR、打者 PA、內建 SP/RP)...")
    players_db = {}

    def format_savant_items(metrics_def, pr_row, raw_row, est_row):
        items = []
        for label, pr_cols, raw_col, fmt in metrics_def:
            pr_val = None
            if pr_row:
                for col_candidate in pr_cols:
                    if col_candidate in pr_row and pd.notna(pr_row[col_candidate]):
                        pr_val = pr_row[col_candidate]
                        break

            raw_val = raw_row.get(raw_col) if (raw_row and raw_col) else None
            est_val = est_row.get(raw_col) if (est_row and raw_col) else None

            has_pr = pr_val is not None and pd.notna(pr_val)
            has_raw = pd.notna(raw_val)

            if not has_pr and not has_raw:
                continue

            formatted_raw = "-"
            if has_raw:
                fval = float(raw_val)
                if fmt == ".3f" and abs(fval) < 1:
                    formatted_raw = f"{fval:{fmt}}".replace("0.", ".")
                else:
                    formatted_raw = f"{fval:{fmt}}"
                    if formatted_raw == "-0":
                        formatted_raw = "0"

            items.append({
                "label": label,
                "pr": int(pr_val) if has_pr else None,
                "est_pr": int(est_val) if est_val is not None else 50,
                "val": formatted_raw,
            })
        return items

    for _, row in active_players.iterrows():
        mlbam_id = int(row["key_mlbam"])
        raw_fg = int(row["key_fangraphs"]) if pd.notna(row["key_fangraphs"]) else -1
        fg_id = mlbam_to_fg.get(mlbam_id) or (raw_fg if raw_fg > 0 else None)
        bbref_id = str(row["key_bbref"]) if pd.notna(row["key_bbref"]) else None

        seasons_dict = {}
        default_role = "batter"

        for yr in YEARS:
            yd = yearly_data[yr]

            bat_savant = format_savant_items(
                BATTER_METRICS,
                yd["pr_bat"].get(mlbam_id),
                yd["raw_bat"].get(mlbam_id),
                yd["est_bat"].get(mlbam_id),
            )
            pit_savant = format_savant_items(
                PITCHER_METRICS,
                yd["pr_pit"].get(mlbam_id),
                yd["raw_pit"].get(mlbam_id),
                yd["est_pit"].get(mlbam_id),
            )

            fg_bat_row = yd["fg_bat"].get(fg_id) if fg_id else None
            fg_pit_row = yd["fg_pit"].get(fg_id) if fg_id else None
            bat_pr_map = yd["fg_bat_pr"].get(fg_id, {}) if fg_id else {}
            pit_pr_map = yd["fg_pit_pr"].get(fg_id, {}) if fg_id else {}

            has_bat = bool(bat_savant or fg_bat_row)
            has_pit = bool(pit_savant or fg_pit_row)

            # 跳過完全無出賽紀錄的空球季，大幅縮減 JSON 體積
            if not has_bat and not has_pit:
                continue

            bat_fg, bat_fg_pr, bat_sim_raw = {}, {}, {}
            bat_pa = 0
            if fg_bat_row:
                bat_pa = int(round(float(fg_bat_row.get("PA") or 0)))
                bat_fg = {
                    "fWAR": round(float(fg_bat_row.get("WAR") or 0), 1),
                    "OPS+": fg_bat_row.get("OPS+"),
                    "wRC+": int(round(float(fg_bat_row.get("wRC+", 0)))) if fg_bat_row.get("wRC+") is not None else None,
                    "wOBA": round(float(fg_bat_row.get("wOBA", 0)), 3),
                    "ISO": round(float(fg_bat_row.get("ISO", 0)), 3),
                    "BABIP": round(float(fg_bat_row.get("BABIP", 0)), 3),
                    "BsR": round(fg_bat_row["REAL_BSR"], 1),
                    "Def": round(fg_bat_row["REAL_DEF"], 1),
                }
                bat_fg_pr = {
                    "fWAR": bat_pr_map.get("WAR"),
                    "OPS+": bat_pr_map.get("OPS+"),
                    "wRC+": bat_pr_map.get("wRC+"),
                    "wOBA": bat_pr_map.get("wOBA"),
                    "ISO": bat_pr_map.get("ISO"),
                    "BABIP": bat_pr_map.get("BABIP"),
                    "BsR": bat_pr_map.get("REAL_BSR"),
                    "Def": bat_pr_map.get("REAL_DEF"),
                }
                for k in ("GB%", "Pull%", "Z-Swing%"):
                    p_str = format_pct_str(fg_bat_row.get(k))
                    if p_str is not None:
                        bat_sim_raw[k] = p_str

            bat_sim_pr = {
                k: bat_pr_map.get(k)
                for k in ("GB%", "Pull%", "Z-Swing%")
                if bat_pr_map.get(k) is not None
            }

            pit_fg, pit_fg_pr, pit_sim_raw = {}, {}, {}
            pit_sub_role = None
            pit_ip = 0.0
            if fg_pit_row:
                stuff_val = fg_pit_row.get("Stuff+") or fg_pit_row.get("sp_stuff")
                pit_fg = {
                    "fWAR": round(float(fg_pit_row.get("WAR") or 0), 1),
                    "ERA+": fg_pit_row.get("ERA+"),
                    "FIP": round(float(fg_pit_row.get("FIP", 0)), 2),
                    "xFIP": round(float(fg_pit_row.get("xFIP", 0)), 2),
                    "SIERA": round(float(fg_pit_row.get("SIERA", 0)), 2) if fg_pit_row.get("SIERA") else "-",
                    "HR/9": round(float(fg_pit_row.get("HR/9", 0)), 2) if fg_pit_row.get("HR/9") is not None else "-",
                    "LOB%": f"{round(float(fg_pit_row.get('LOB%', 0)) * 100, 1)}%" if fg_pit_row.get("LOB%") is not None else "-",
                    "Stuff+": int(round(float(stuff_val))) if stuff_val is not None and pd.notna(stuff_val) else "-",
                }
                pit_fg_pr = {
                    "fWAR": pit_pr_map.get("WAR"),
                    "ERA+": pit_pr_map.get("ERA+"),
                    "FIP": pit_pr_map.get("FIP"),
                    "xFIP": pit_pr_map.get("xFIP"),
                    "SIERA": pit_pr_map.get("SIERA"),
                    "HR/9": pit_pr_map.get("HR/9"),
                    "LOB%": pit_pr_map.get("LOB%"),
                    "Stuff+": pit_pr_map.get("Stuff+"),
                }
                g_val = float(fg_pit_row.get("G") or 0)
                gs_val = float(fg_pit_row.get("GS") or 0)
                pit_ip = round(float(fg_pit_row.get("IP") or 0), 1)
                if g_val > 0:
                    pit_sub_role = "SP" if (gs_val / g_val) >= 0.4 else "RP"

            raw_pit_row = yd["raw_pit"].get(mlbam_id) or {}
            pit_sim_pr = {}

            # Zone% 優先採用 Statcast (in_zone_percent，聯盟平均 ~50%)，缺值才退回 FanGraphs
            if raw_pit_row.get("in_zone_percent_pr") is not None:
                pit_sim_pr["Zone%"] = raw_pit_row.get("in_zone_percent_pr")
            elif pit_pr_map.get("Zone%") is not None:
                pit_sim_pr["Zone%"] = pit_pr_map.get("Zone%")

            if raw_pit_row.get("in_zone_percent") is not None and pd.notna(raw_pit_row.get("in_zone_percent")):
                iz_str = format_pct_str(raw_pit_row.get("in_zone_percent"))
                if iz_str is not None:
                    pit_sim_raw["Zone%"] = iz_str
            elif fg_pit_row:
                z_str = format_pct_str(fg_pit_row.get("Zone%"))
                if z_str is not None:
                    pit_sim_raw["Zone%"] = z_str

            if raw_pit_row.get("arm_angle_pr") is not None:
                pit_sim_pr["Arm Angle"] = raw_pit_row.get("arm_angle_pr")
            if raw_pit_row.get("arm_angle") is not None and pd.notna(raw_pit_row.get("arm_angle")):
                pit_sim_raw["Arm Angle"] = f"{int(round(float(raw_pit_row['arm_angle'])))}°"

            yr_role = "batter" if has_bat else "pitcher"
            default_role = yr_role

            season_entry = {
                "role": yr_role,
                "has_bat": has_bat,
                "has_pit": has_pit,
            }
            if has_bat:
                season_entry["batting"] = {
                    "savant_pr": bat_savant,
                    "fangraphs": bat_fg,
                    "fangraphs_pr": bat_fg_pr,
                    "sim_pr": bat_sim_pr,
                    "sim_raw": bat_sim_raw,
                    "pa": bat_pa,
                }
            if has_pit:
                season_entry["pitching"] = {
                    "savant_pr": pit_savant,
                    "fangraphs": pit_fg,
                    "fangraphs_pr": pit_fg_pr,
                    "pitch_mix": raw_pit_row.get("pitch_mix"),
                    "sim_pr": pit_sim_pr,
                    "sim_raw": pit_sim_raw,
                    "sub_role": pit_sub_role,
                    "ip": pit_ip,
                }
            seasons_dict[str(yr)] = season_entry

        if not seasons_dict:
            continue

        players_db[str(mlbam_id)] = {
            "mlbam_id": mlbam_id,
            "fg_id": fg_id,
            "bbref_id": bbref_id,
            "name": row["full_name"],
            "role": default_role,
            "seasons": seasons_dict,
        }

    with open("mlb_data.json", "w", encoding="utf-8") as f:
        json.dump(
            clean_for_json({
                "years": sorted(YEARS, reverse=True),
                "default_year": DEFAULT_YEAR,
                "players": players_db,
            }),
            f,
            ensure_ascii=False,
            separators=(",", ":"),
        )

    size_mb = os.path.getsize("mlb_data.json") / (1024 * 1024)
    print(f"4/4 完成！已輸出 mlb_data.json（{size_mb:.1f} MB），共收錄 {len(players_db)} 位球員（{min(YEARS)}–{max(YEARS)}）。")


if __name__ == "__main__":
    build_database()