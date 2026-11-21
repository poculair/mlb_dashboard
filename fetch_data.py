import io
import json
import os
from curl_cffi import requests
import pandas as pd
import pybaseball as pyb

pyb.cache.enable()

YEARS = [2024, 2025, 2026]
DEFAULT_YEAR = 2026

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.fangraphs.com/leaders/major-league",
}


def fetch_fangraphs_leaders(year: int, stats_type: str = "bat"):
    """透過 FanGraphs Leaderboard API 取得真實 fWAR、BsR/Def 與進階指標"""
    url = "https://www.fangraphs.com/api/leaders/major-league/data"
    base_params = {
        "age": "", "pos": "all", "stats": stats_type, "lg": "all", "qual": "1",
        "season": year, "season1": year,
        "startdate": f"{year}-03-01", "enddate": f"{year}-11-01",
        "month": "0", "hand": "", "team": "0", "pageitems": "2000", "pagenum": "1",
        "ind": "0", "rost": "0", "players": "", "type": "8", "postseason": "",
        "sortdir": "default", "sortstat": "WAR",
    }
    leaders_map = {}
    try:
        res = requests.get(url, params=base_params, headers=HEADERS, impersonate="chrome", timeout=20)
        res.raise_for_status()
        for row in res.json().get("data", []):
            if row.get("playerid"):
                pid = int(row["playerid"])
                if row.get("sp_stuff") is not None and row.get("Stuff+") is None:
                    row["Stuff+"] = row["sp_stuff"]
                leaders_map[pid] = row
    except Exception as e:
        print(f"  [警告] {year} FanGraphs ({stats_type}) API 抓取失敗: {e}")

    extra_types = ["6"] if stats_type == "bat" else ["0", "36"]
    for t in extra_types:
        try:
            ex_res = requests.get(url, params={**base_params, "type": t}, headers=HEADERS, impersonate="chrome", timeout=20)
            if ex_res.status_code == 200:
                for row in ex_res.json().get("data", []):
                    pid = row.get("playerid")
                    if pid and int(pid) in leaders_map:
                        for k in ["BsR", "BaseRunning", "Def", "Fielding", "ERA-", "Stuff+", "sp_stuff", "HR/9"]:
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
        higher_better = ["TOTAL_WAR", "OPS+", "wRC+", "wOBA", "ISO", "BABIP", "REAL_BSR", "REAL_DEF"]
        lower_better = []
    else:
        pool = df[pd.to_numeric(df["IP"], errors="coerce") >= 15].copy()
        higher_better = ["TOTAL_WAR", "ERA+", "LOB%", "Stuff+"]
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


def fetch_savant_raw_metrics(year: int, player_type: str = "batter"):
    """抓取 Savant Custom Leaderboard、Bat Tracking、OAA 守備、Arm Strength 與投手 Pitch Run Value"""
    merged_map = {}
    url = "https://baseballsavant.mlb.com/leaderboard/custom"
    if player_type == "batter":
        selections = "xwoba,xba,xslg,exit_velocity_avg,barrel_batted_rate,hard_hit_percent,sweet_spot_percent,avg_swing_speed,squared_up_swing_rate,oz_swing_percent,whiff_percent,k_percent,bb_percent"
    else:
        selections = "xera,xwoba,xba,exit_velocity_avg,barrel_batted_rate,hard_hit_percent,oz_swing_percent,whiff_percent,k_percent,bb_percent,ff_avg_speed,ff_avg_spin,groundballs_percent,release_extension"

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
            merged_map = df.set_index("player_id").to_dict("index")
    except Exception as e:
        print(f"  [警告] {year} Savant 基礎數值 ({player_type}) 抓取失敗: {e}")

    if player_type == "batter":
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
                        if "total_pitches" in pdata:
                            fb_i = round(pdata.get("fastball_rv", 0.0))
                            brk_i = round(pdata.get("breaking_rv", 0.0))
                            off_i = round(pdata.get("offspeed_rv", 0.0))
                            pdata["pitching_rv"] = float(fb_i + brk_i + off_i)
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

    return merged_map


def enrich_official_pitcher_prs(raw_pit_map: dict, pr_pit_dict: dict):
    """補齊球種 Run Value、GB % 與 xERA 的官方 PR"""
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


def build_database():
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
        print(f"2/4 正在抓取 {yr} 年 Savant 與 FanGraphs 數據...")
        s_bat_pr = pyb.statcast_batter_percentile_ranks(yr)
        s_pit_pr = pyb.statcast_pitcher_percentile_ranks(yr)
        s_bat_raw = fetch_savant_raw_metrics(yr, "batter")
        s_pit_raw = fetch_savant_raw_metrics(yr, "pitcher")

        bat_raw_est_pr = build_raw_percentile_fallback(s_bat_raw, BATTER_METRICS)
        pit_raw_est_pr = build_raw_percentile_fallback(s_pit_raw, PITCHER_METRICS)

        pr_pit_dict = s_pit_pr.set_index("player_id").to_dict("index") if not s_pit_pr.empty else {}
        enrich_official_pitcher_prs(s_pit_raw, pr_pit_dict)

        fg_bat = fetch_fangraphs_leaders(yr, "bat")
        fg_pit = fetch_fangraphs_leaders(yr, "pit")

        for pid, r in fg_bat.items():
            tot = float(r.get("WAR") or 0)
            if pid in fg_pit:
                tot += float(fg_pit[pid].get("WAR") or 0)
            r["TOTAL_WAR"] = tot
            bsr_val = r.get("BsR") if r.get("BsR") is not None else r.get("BaseRunning")
            def_val = r.get("Def") if r.get("Def") is not None else r.get("Fielding")
            r["REAL_BSR"] = float(bsr_val) if bsr_val is not None else 0.0
            r["REAL_DEF"] = float(def_val) if def_val is not None else 0.0

        for pid, r in fg_pit.items():
            tot = float(r.get("WAR") or 0)
            if pid in fg_bat:
                tot += float(fg_bat[pid].get("WAR") or 0)
            r["TOTAL_WAR"] = tot

        enrich_ops_plus_and_era_plus(fg_bat, fg_pit)
        fg_bat_pr = compute_league_percentiles(fg_bat, "bat")
        fg_pit_pr = compute_league_percentiles(fg_pit, "pit")

        yearly_data[yr] = {
            "pr_bat": s_bat_pr.set_index("player_id").to_dict("index") if not s_bat_pr.empty else {},
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
        print(f"    {yr} 年完成：打者 {len(fg_bat)} 筆、投手 {len(fg_pit)} 筆。")

    print("3/4 正在整合多年份球員資料庫 (完整支援二刀流雙模式)...")
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
        fg_id = int(row["key_fangraphs"]) if pd.notna(row["key_fangraphs"]) else None
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

            bat_fg, bat_fg_pr = {}, {}
            if fg_bat_row:
                bat_fg = {
                    "fWAR": round(float(fg_bat_row.get("TOTAL_WAR", 0)), 1),
                    "OPS+": fg_bat_row.get("OPS+"),
                    "wRC+": int(round(float(fg_bat_row.get("wRC+", 0)))) if fg_bat_row.get("wRC+") is not None else None,
                    "wOBA": round(float(fg_bat_row.get("wOBA", 0)), 3),
                    "ISO": round(float(fg_bat_row.get("ISO", 0)), 3),
                    "BABIP": round(float(fg_bat_row.get("BABIP", 0)), 3),
                    "BsR": round(fg_bat_row["REAL_BSR"], 1),
                    "Def": round(fg_bat_row["REAL_DEF"], 1),
                }
                bat_fg_pr = {
                    "fWAR": bat_pr_map.get("TOTAL_WAR"),
                    "OPS+": bat_pr_map.get("OPS+"),
                    "wRC+": bat_pr_map.get("wRC+"),
                    "wOBA": bat_pr_map.get("wOBA"),
                    "ISO": bat_pr_map.get("ISO"),
                    "BABIP": bat_pr_map.get("BABIP"),
                    "BsR": bat_pr_map.get("REAL_BSR"),
                    "Def": bat_pr_map.get("REAL_DEF"),
                }

            pit_fg, pit_fg_pr = {}, {}
            if fg_pit_row:
                stuff_val = fg_pit_row.get("Stuff+") or fg_pit_row.get("sp_stuff")
                pit_fg = {
                    "fWAR": round(float(fg_pit_row.get("TOTAL_WAR", 0)), 1),
                    "ERA+": fg_pit_row.get("ERA+"),
                    "FIP": round(float(fg_pit_row.get("FIP", 0)), 2),
                    "xFIP": round(float(fg_pit_row.get("xFIP", 0)), 2),
                    "SIERA": round(float(fg_pit_row.get("SIERA", 0)), 2) if fg_pit_row.get("SIERA") else "-",
                    "HR/9": round(float(fg_pit_row.get("HR/9", 0)), 2) if fg_pit_row.get("HR/9") is not None else "-",
                    "LOB%": f"{round(float(fg_pit_row.get('LOB%', 0)) * 100, 1)}%" if fg_pit_row.get("LOB%") is not None else "-",
                    "Stuff+": int(round(float(stuff_val))) if stuff_val is not None and pd.notna(stuff_val) else "-",
                }
                pit_fg_pr = {
                    "fWAR": pit_pr_map.get("TOTAL_WAR"),
                    "ERA+": pit_pr_map.get("ERA+"),
                    "FIP": pit_pr_map.get("FIP"),
                    "xFIP": pit_pr_map.get("xFIP"),
                    "SIERA": pit_pr_map.get("SIERA"),
                    "HR/9": pit_pr_map.get("HR/9"),
                    "LOB%": pit_pr_map.get("LOB%"),
                    "Stuff+": pit_pr_map.get("Stuff+"),
                }

            has_bat = bool(bat_savant or bat_fg)
            has_pit = bool(pit_savant or pit_fg)

            yr_role = "batter" if has_bat else ("pitcher" if has_pit else "batter")
            if yr == DEFAULT_YEAR:
                default_role = yr_role

            seasons_dict[str(yr)] = {
                "role": yr_role,
                "has_bat": has_bat,
                "has_pit": has_pit,
                "batting": {
                    "savant_pr": bat_savant,
                    "fangraphs": bat_fg,
                    "fangraphs_pr": bat_fg_pr,
                },
                "pitching": {
                    "savant_pr": pit_savant,
                    "fangraphs": pit_fg,
                    "fangraphs_pr": pit_fg_pr,
                },
            }

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
            {
                "years": sorted(YEARS, reverse=True),
                "default_year": DEFAULT_YEAR,
                "players": players_db,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )

    print(f"4/4 完成！已輸出 mlb_data.json，共收錄 {len(players_db)} 位球員。")


if __name__ == "__main__":
    build_database()