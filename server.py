import csv
import http.server
import io
import json
import os
import re
import socketserver
import threading
import time
from urllib.parse import parse_qs, urlparse
from curl_cffi import requests

VERSION = "v4.8"
PORT = int(os.environ.get("PORT", 8000))
CACHE_FILE = "player_extra_cache.json"
CACHE_TTL = 60 * 60 * 24  # 快取有效期：24 小時

cache_lock = threading.Lock()
extra_cache = {}

if os.path.exists(CACHE_FILE):
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            loaded = json.load(f)
            if isinstance(loaded, dict):
                extra_cache = loaded
        print(f"[{VERSION} 系統初始化] 已載入現有快取 ({len(extra_cache)} 筆)")
    except Exception as e:
        print(f"[{VERSION} 系統警告] 快取載入失敗，將重新建立：{e}")
        extra_cache = {}

# Baseball Savant 官方球種名稱與代表色
PITCH_META = {
    "FF": {"name": "4-Seam", "color": "#D22D49", "dark_text": False},
    "SI": {"name": "Sinker", "color": "#FE9D00", "dark_text": True},
    "FC": {"name": "Cutter", "color": "#933F2C", "dark_text": False},
    "SL": {"name": "Slider", "color": "#EEE716", "dark_text": True},
    "ST": {"name": "Sweeper", "color": "#DDB33A", "dark_text": True},
    "CU": {"name": "Curve", "color": "#00D1ED", "dark_text": True},
    "KC": {"name": "Knuckle Curve", "color": "#6236CD", "dark_text": False},
    "SV": {"name": "Slurve", "color": "#93AFD4", "dark_text": True},
    "CH": {"name": "Change", "color": "#1DBE3A", "dark_text": False},
    "FS": {"name": "Splitter", "color": "#3BACAC", "dark_text": False},
    "FO": {"name": "Forkball", "color": "#55CCAB", "dark_text": True},
    "KN": {"name": "Knuckle", "color": "#3C5488", "dark_text": False},
    "SC": {"name": "Screwball", "color": "#60DB33", "dark_text": True},
    "CS": {"name": "Slow Curve", "color": "#0068FF", "dark_text": False},
    "EP": {"name": "Eephus", "color": "#888888", "dark_text": False},
}

FALLBACK_LG_AVG_MPH = {
    "R": {"FF": 95.3, "SI": 94.6, "FC": 90.3, "SL": 87.0, "ST": 82.8, "CU": 81.1, "KC": 82.0, "SV": 83.0, "CH": 86.8, "FS": 87.2, "FO": 86.0, "KN": 75.0},
    "L": {"FF": 93.5, "SI": 93.4, "FC": 89.0, "SL": 85.1, "ST": 81.5, "CU": 79.5, "KC": 80.5, "SV": 81.5, "CH": 84.7, "FS": 85.5, "FO": 84.5, "KN": 74.0},
}


def cache_get(key: str):
    with cache_lock:
        entry = extra_cache.get(key)
        if not isinstance(entry, dict) or "ts" not in entry or "data" not in entry:
            return None, False
        if (time.time() - entry["ts"]) > CACHE_TTL:
            extra_cache.pop(key, None)
            return None, False
        return entry["data"], True


def cache_set(key: str, value):
    with cache_lock:
        extra_cache[key] = {"ts": time.time(), "data": value}
        try:
            tmp_file = f"{CACHE_FILE}.tmp"
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(extra_cache, f, ensure_ascii=False)
            os.replace(tmp_file, CACHE_FILE)
        except Exception as e:
            print(f"[{VERSION} 快取寫入警告] {e}")


def norm_key(k: str) -> str:
    return re.sub(r"[\s_\-]+", "", str(k).lower())


def pick_first(d: dict, keys: list):
    for k in keys:
        val = d.get(k)
        if val is not None and str(val).strip() not in ("", "None", "null", "N/A", "-"):
            return str(val).strip()
    norm_map = {norm_key(k): v for k, v in d.items()}
    for k in keys:
        val = norm_map.get(norm_key(k))
        if val is not None and str(val).strip() not in ("", "None", "null", "N/A", "-"):
            return str(val).strip()
    return None


def is_valid_val(v):
    return v is not None and str(v).strip() not in ("", "None", "null", "N/A")


def _safe_float(v):
    if not is_valid_val(v):
        return None
    try:
        return float(str(v).replace("%", "").replace("$", "").replace(",", "").strip())
    except Exception:
        return None


def _fmt_money_clean(amount) -> str:
    v = _safe_float(amount)
    if v is None or v <= 0:
        return ""
    if v >= 1_000_000:
        m = v / 1_000_000
        if abs(m - round(m)) < 0.05:
            return f"${int(round(m))}M"
        return f"${m:.2f}".rstrip("0").rstrip(".") + "M"
    if v >= 1_000:
        return f"${int(round(v / 1_000))}K"
    return f"${int(round(v))}"


def extract_fg_roster_info(page_props: dict, html_text: str) -> dict:
    """從 FanGraphs __NEXT_DATA__ 與頁面文字解析 RosterResource 合約、逐年薪資、AAV、選擇權、條款備註與 MiLB Options"""
    info = {
        "contract": None,
        "status": None,
        "aav": None,
        "options": None,
        "fa_year": None,
        "service_time": None,
        "notes": [],
        "yearly": [],
    }
    yearly_map = {}

    def add_note(text):
        if isinstance(text, str):
            clean = text.strip()
            if clean and clean not in info["notes"]:
                info["notes"].append(clean)

    def parse_year_row(row: dict):
        if not isinstance(row, dict):
            return
        yr_raw = row.get("Season") or row.get("season") or row.get("Year") or row.get("year")
        if yr_raw is None:
            return
        yr_str = str(yr_raw).strip()
        if not re.match(r"^20\d{2}$", yr_str):
            return

        sal_keys = ("TotalSalary", "totalSalary", "Payroll", "payroll", "Cash", "cash", "Salary", "salary", "BaseSalary", "baseSalary", "CBT", "cbt", "AAV", "aav")
        opt_keys = ("OptionType", "optionType", "Type", "type", "ContractType", "contractType", "Status", "status", "PayrollType", "payrollType")
        has_sal = any(row.get(k) is not None for k in sal_keys)
        has_opt = any(isinstance(row.get(k), str) and any(w in row.get(k).lower() for w in ("opt", "club", "player", "mutual", "vest", "arb", "free", "fa", "guar", "signed")) for k in opt_keys)
        if not has_sal and not has_opt:
            return

        sal_val = None
        for k in ("TotalSalary", "totalSalary", "Payroll", "payroll", "Cash", "cash", "Salary", "salary", "BaseSalary", "baseSalary"):
            fv = _safe_float(row.get(k))
            if fv is not None and fv > 0:
                sal_val = fv
                break

        row_bonus = _safe_float(row.get("SigningBonus") or row.get("signingBonus") or row.get("Bonus") or row.get("bonus"))
        if row_bonus and row_bonus > 0:
            base_only = _safe_float(row.get("BaseSalary") or row.get("baseSalary"))
            if sal_val is not None and base_only is not None and abs(sal_val - base_only) < 1:
                sal_val += row_bonus

        cbt_val = None
        for k in ("CBT", "cbt", "AAV", "aav", "LuxuryTax", "luxuryTax"):
            fv = _safe_float(row.get(k))
            if fv is not None and fv > 0:
                cbt_val = fv
                break

        buyout_val = None
        for k in ("Buyout", "buyout", "OptionBuyout", "optionBuyout", "PotentialBuyout", "potentialBuyout"):
            fv = _safe_float(row.get(k))
            if fv is not None and fv > 0:
                buyout_val = fv
                break

        tag = None
        for k in ("OptionType", "optionType", "Type", "type", "Status", "status", "PayrollType", "payrollType"):
            val = row.get(k)
            if isinstance(val, str) and val.strip():
                vl = val.strip().lower()
                if "club" in vl:
                    tag = "Club Option"
                elif "player" in vl:
                    tag = "Player Option"
                elif "mutual" in vl:
                    tag = "Mutual Option"
                elif "vest" in vl:
                    tag = "Vesting Option"
                elif "pre-arb" in vl or "prearb" in vl:
                    tag = "Pre-Arb"
                elif "arb" in vl:
                    tag = val.strip() if len(val.strip()) <= 12 else "Arbitration"
                elif "free" in vl or vl == "fa":
                    tag = "Free Agent"
                if tag:
                    break

        opt_out = False
        for k in ("OptOut", "optOut", "hasOptOut", "HasOptOut"):
            v = row.get(k)
            if v in (True, 1, "1", "true", "True", "Yes", "yes"):
                opt_out = True
                break

        note_str = None
        for k in ("Notes", "notes", "Note", "note", "OptionNotes", "optionNotes"):
            v = row.get(k)
            if isinstance(v, str) and v.strip():
                note_str = v.strip()
                if "opt-out" in note_str.lower() or "opt out" in note_str.lower():
                    opt_out = True
                break

        existing = yearly_map.get(yr_str)
        new_entry = {
            "year": yr_str,
            "salary": _fmt_money_clean(sal_val) if sal_val else "-",
            "salary_raw": int(round(sal_val)) if sal_val else None,
            "aav": _fmt_money_clean(cbt_val) if cbt_val else None,
            "tag": tag,
            "buyout": _fmt_money_clean(buyout_val) if buyout_val else None,
            "opt_out": opt_out,
            "note": note_str,
        }
        if not existing or (new_entry["salary_raw"] and not existing.get("salary_raw")) or (new_entry["tag"] and not existing.get("tag")):
            yearly_map[yr_str] = new_entry

    def walk(obj):
        if isinstance(obj, dict):
            if info["contract"] is None and "YearsTotal" in obj and "ContractTotal" in obj:
                yrs = obj.get("YearsTotal")
                tot = obj.get("ContractTotal")
                s_yr = obj.get("startSeason")
                e_yr_base = obj.get("endSeason")
                e_yr_all = obj.get("endSeasonAll")
                e_yr = e_yr_base or e_yr_all
                if yrs is not None and s_yr is not None:
                    try:
                        yrs_i = int(yrs)
                        s_i = int(s_yr)
                        if e_yr_base is not None and (int(e_yr_base) - s_i + 1) == yrs_i:
                            e_yr = e_yr_base
                        elif e_yr_all is not None and (int(e_yr_all) - s_i + 1) == yrs_i:
                            e_yr = e_yr_all
                    except Exception:
                        pass

                if yrs is not None and tot is not None:
                    yr_str = f"{int(yrs)} yr" if int(yrs) == 1 else f"{int(yrs)} yrs"
                    money_str = _fmt_money_clean(tot)
                    if s_yr and e_yr:
                        span = f"({s_yr})" if str(s_yr) == str(e_yr) else f"({s_yr}-{str(e_yr)[-2:]})"
                    else:
                        span = ""
                    info["contract"] = f"{yr_str}, {money_str} {span}".strip()

                ctype = obj.get("ContractType")
                if info["status"] is None and isinstance(ctype, str) and ctype.strip():
                    info["status"] = ctype.strip()

                aav_v = _safe_float(obj.get("AAV"))
                if aav_v is None and yrs and tot:
                    try:
                        aav_v = float(tot) / float(yrs)
                    except Exception:
                        aav_v = None
                if info["aav"] is None and aav_v and aav_v > 0:
                    info["aav"] = _fmt_money_clean(aav_v)

                st_mls = obj.get("servicetime")
                if info["service_time"] is None and isinstance(st_mls, str) and st_mls.strip():
                    info["service_time"] = st_mls.strip()

                sb_tot = _safe_float(obj.get("SigningBonus"))
                if sb_tot and sb_tot > 0:
                    add_note(f"Signing Bonus: {_fmt_money_clean(sb_tot)}")

                add_note(obj.get("LongContractSummaryPayrollNote"))
                if not info["notes"]:
                    add_note(obj.get("ContractSummaryPayrollNote"))
                add_note(obj.get("NoTradeNotes"))

            if info["contract"] is None:
                for k in ("ContractSummary", "contractSummary", "ContractText"):
                    val = obj.get(k)
                    if isinstance(val, str) and ("$" in val or "yr" in val.lower()) and "{" not in val:
                        info["contract"] = re.sub(r"\s*\(RR\)\s*", "", val).strip()
                        break

            if info["options"] is None:
                for k in ("OptionYears", "optionYears", "MiLBOptions", "milbOptions", "OptionsRemaining", "optionsRemaining"):
                    val = obj.get(k)
                    if isinstance(val, (int, str)) and re.match(r"^\d+$", str(val).strip()):
                        info["options"] = int(val)
                        break

            if info["fa_year"] is None:
                for k in ("FreeAgent", "freeAgent", "FAYear", "faYear"):
                    val = obj.get(k)
                    if isinstance(val, (int, str)) and re.match(r"^20\d{2}$", str(val).strip()):
                        info["fa_year"] = str(val).strip()
                        break

            parse_year_row(obj)

            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    if isinstance(page_props, dict):
        walk(page_props)

    if html_text:
        plain = re.sub(r"<[^>]+>", " ", html_text)
        plain = re.sub(r"\s+", " ", plain)

        if info["contract"] is None:
            m_con = re.search(r"Contract\s+([0-9]+\s*yr[s]?,\s*\$[0-9.,]+[MK]?(?:\s*\([0-9\-]+\))?)", plain, re.I)
            if m_con:
                info["contract"] = m_con.group(1).strip()

        if info["aav"] is None:
            m_aav = re.search(r"AAV:\s*(\$[0-9.,]+[MK]?)", plain, re.I)
            if m_aav:
                info["aav"] = m_aav.group(1).strip()

        if info["status"] is None:
            m_rr = re.search(r"RosterResource.{0,800}", plain)
            scope = m_rr.group(0) if m_rr else plain
            if re.search(r"Pre-Arbitration", scope, re.I):
                info["status"] = "Pre-Arbitration"
            elif re.search(r"\bArbitration\b", scope, re.I):
                info["status"] = "Arbitration"

        if info["options"] is None:
            m_opt = re.search(r"MiLB Options\s+(?:Free Agent\s+)?(?:Arb Eligible\s+)?(?:\d+\.\d{3}\s+)?(\d+)", plain, re.I)
            if not m_opt:
                m_opt = re.search(r"MiLB Options.{0,40}?(\d+)", plain, re.I)
            if m_opt:
                val = int(m_opt.group(1))
                if 0 <= val <= 4:
                    info["options"] = val

    if yearly_map:
        info["yearly"] = sorted(yearly_map.values(), key=lambda x: int(x["year"]))
        if info["aav"]:
            for y in info["yearly"]:
                if not y.get("aav") and y.get("salary_raw"):
                    y["aav"] = info["aav"]

        fa_row = next((y for y in info["yearly"] if y.get("tag") == "Free Agent"), None)
        if fa_row:
            info["fa_year"] = fa_row["year"]
        else:
            arb3_row = next((y for y in reversed(info["yearly"]) if (y.get("tag") or "").upper() == "ARB 3"), None)
            if arb3_row:
                info["fa_year"] = str(int(arb3_row["year"]) + 1)
            elif info["fa_year"] is None and info["yearly"]:
                last_yr = info["yearly"][-1]
                if not (last_yr.get("tag") or "").lower().startswith(("pre-arb", "arb")):
                    info["fa_year"] = str(int(last_yr["year"]) + 1)

    has_any = any(v for k, v in info.items() if k not in ("notes", "yearly")) or bool(info["yearly"]) or bool(info["notes"])
    return info if has_any else None


def get_fg_leaderboard_data():
    cache_key = f"fg_lb_full_{VERSION}"
    cached, hit = cache_get(cache_key)
    if hit:
        return cached

    name_to_id = {}
    stats_map = {}

    for yr in ("2026", "2025", "2024"):
        stats_map[yr] = {"batter": {}, "pitcher": {}}
        for st, role in (("bat", "batter"), ("pit", "pitcher")):
            url = (
                f"https://www.fangraphs.com/api/leaders/major-league/data"
                f"?pos=all&stats={st}&lg=all&qual=0&type=8&season={yr}&season1={yr}"
                f"&ind=0&team=0&pageitems=2000&pagenum=1"
            )
            try:
                res = requests.get(
                    url,
                    impersonate="chrome",
                    headers={"Referer": "https://www.fangraphs.com/leaders/major-league"},
                    timeout=15,
                )
                if res.status_code != 200:
                    continue
                rows = res.json().get("data", [])
                for r in rows:
                    pname = str(r.get("PlayerName") or "").strip().lower()
                    pid = str(r.get("playerid") or "").strip()
                    if not pname:
                        continue
                    if pid and pname not in name_to_id:
                        name_to_id[pname] = pid

                    entry = {}
                    war_v = _safe_float(r.get("WAR"))
                    if war_v is not None:
                        entry["fWAR"] = round(war_v, 1)

                    if role == "batter":
                        wrc = _safe_float(r.get("wRC+"))
                        if wrc is not None:
                            entry["wRC+"] = int(round(wrc))
                        for k_src, k_dst, digits in (
                            ("wOBA", "wOBA", 3),
                            ("ISO", "ISO", 3),
                            ("BABIP", "BABIP", 3),
                            ("BsR", "BsR", 1),
                            ("Def", "Def", 1),
                        ):
                            fv = _safe_float(r.get(k_src))
                            if fv is not None:
                                entry[k_dst] = round(fv, digits)
                    else:
                        for k_src, k_dst, digits in (
                            ("FIP", "FIP", 2),
                            ("xFIP", "xFIP", 2),
                            ("SIERA", "SIERA", 2),
                            ("HR/9", "HR/9", 2),
                        ):
                            fv = _safe_float(r.get(k_src))
                            if fv is not None:
                                entry[k_dst] = round(fv, digits)
                        lob = _safe_float(r.get("LOB%"))
                        if lob is not None:
                            pct = lob * 100.0 if lob <= 1.5 else lob
                            entry["LOB%"] = f"{pct:.1f}%"
                        stuff = _safe_float(r.get("sp_stuff") or r.get("Stuff+"))
                        if stuff is not None:
                            entry["Stuff+"] = int(round(stuff))

                    if entry:
                        stats_map[yr][role][pname] = entry
            except Exception as e:
                print(f"[{VERSION} FG 排行榜抓取異常] {yr} {st}: {e}")

    print(f"[{VERSION} FG 排行榜建立完成] 共收錄 {len(name_to_id)} 位大聯盟球員正式 ID 與進階數據")
    bundle = {"name_to_id": name_to_id, "stats": stats_map}
    cache_set(cache_key, bundle)
    return bundle


def resolve_fg_url_by_name(player_name: str):
    if not player_name:
        return None
    bundle = get_fg_leaderboard_data()
    pid = bundle["name_to_id"].get(player_name.strip().lower())
    if pid:
        slug = re.sub(r"[^a-z0-9]+", "-", player_name.strip().lower()).strip("-")
        return f"https://www.fangraphs.com/players/{slug}/{pid}/stats"
    return None


def lookup_fg_live_stats(player_name: str, year: str, role: str):
    if not player_name:
        return None
    bundle = get_fg_leaderboard_data()
    return bundle["stats"].get(str(year), {}).get(role, {}).get(player_name.strip().lower())


def fetch_fg_player_scouting(fg_id: str, player_name: str = "", year: str = "2026", role: str = "batter"):
    clean_id = str(fg_id).replace(".0", "").strip()
    cache_key = f"fg_{VERSION}_{clean_id}_{player_name}_{year}_{role}"
    cached, hit = cache_get(cache_key)
    if hit:
        return cached

    url = f"https://www.fangraphs.com/players/-/{clean_id}/stats"
    resolved_url = url
    fg_stats = None
    roster_info = None

    try:
        res = requests.get(
            url,
            impersonate="chrome",
            headers={"Referer": "https://www.fangraphs.com/"},
            timeout=15,
        )

        if res.status_code >= 500:
            print(f"[{VERSION} FG 5xx 暫時錯誤] name={player_name} id={clean_id} status={res.status_code}，1 秒後重試...")
            time.sleep(1)
            res = requests.get(
                url,
                impersonate="chrome",
                headers={"Referer": "https://www.fangraphs.com/"},
                timeout=15,
            )

        if res.status_code >= 500:
            print(f"[{VERSION} FG 5xx 重試仍失敗] name={player_name} id={clean_id} status={res.status_code}（不寫入快取）")
            return {
                "scouting": None,
                "resolved_url": None,
                "fg_stats": None,
                "roster_info": None,
            }

        is_404 = (res.status_code == 404) or ("Page Not Found" in res.text[:15000])
        print(f"[{VERSION} FG 查詢] name={player_name} id={clean_id} yr={year} role={role} status={res.status_code} is_404={is_404}")

        if is_404 and player_name:
            fg_stats = lookup_fg_live_stats(player_name, year, role)
            alt_url = resolve_fg_url_by_name(player_name)
            if alt_url:
                print(f"[{VERSION} FanGraphs 404 自動救援成功] {player_name}: {alt_url}")
                resolved_url = alt_url
                res = requests.get(
                    alt_url,
                    impersonate="chrome",
                    headers={"Referer": "https://www.fangraphs.com/"},
                    timeout=15,
                )
                if res.status_code >= 500:
                    print(f"[{VERSION} FG 救援頁 5xx 失敗] {player_name} status={res.status_code}（不寫入快取）")
                    return {
                        "scouting": None,
                        "resolved_url": resolved_url,
                        "fg_stats": fg_stats,
                        "roster_info": None,
                    }

        if res.status_code != 200 or ("Page Not Found" in res.text[:15000]):
            payload = {
                "scouting": None,
                "resolved_url": resolved_url if resolved_url != url else None,
                "fg_stats": fg_stats,
                "roster_info": None,
            }
            cache_set(cache_key, payload)
            return payload

        resolved_url = res.url
        match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', res.text)
        scouting_obj = None
        page_props = {}

        if match:
            next_data = json.loads(match.group(1))
            page_props = next_data.get("props", {}).get("pageProps", {})

            def find_report(obj):
                if isinstance(obj, dict):
                    has_grade = any(k in obj for k in ["FV", "fv", "TLDR", "tldr", "Summary", "summary"])
                    has_tool = any(k in obj for k in ["Hit", "hit", "FB", "fb", "OrgRk", "orgRank", "OvrRk", "top100"])
                    if has_grade and has_tool:
                        return obj
                    for v in obj.values():
                        found = find_report(v)
                        if found:
                            return found
                elif isinstance(obj, list):
                    for item in obj:
                        found = find_report(item)
                        if found:
                            return found
                return None

            rep = find_report(page_props)
            if rep:
                tools = {}
                tool_defs = [
                    (["Hit", "hit"], "Hit"),
                    (["GamePwr", "gamePwr", "GamePower", "Game Power", "Game_Power", "Game", "game"], "Game Power"),
                    (["RawPwr", "rawPwr", "RawPower", "Raw Power", "Raw_Power", "Raw", "raw"], "Raw Power"),
                    (["Spd", "spd", "Speed", "speed", "Run"], "Speed"),
                    (["Fld", "fld", "Field", "field", "Fielding"], "Field"),
                    (["FB", "fb", "Fastball", "fastball"], "Fastball"),
                    (["SL", "sl", "Slider", "slider"], "Slider"),
                    (["CB", "cb", "Curveball", "curveball", "CU"], "Curveball"),
                    (["CH", "ch", "Changeup", "changeup"], "Changeup"),
                    (["CT", "ct", "Cutter", "cutter"], "Cutter"),
                    (["SP", "sp", "Splitter", "splitter", "FS"], "Splitter"),
                    (["CMD", "cmd", "Command", "command"], "Command"),
                ]
                for keys, label in tool_defs:
                    val = pick_first(rep, keys)
                    if val:
                        if "/" in val or (val.isdigit() and 20 <= int(val) <= 80 and int(val) % 5 == 0):
                            tools[label] = val

                scouting_obj = {
                    "report_title": pick_first(rep, ["Report", "Type", "Season", "season", "Year", "year"]) or "FanGraphs Scouting",
                    "org_rk": pick_first(rep, ["OrgRk", "orgRk", "orgRank", "Org_Rk", "org_rank", "Org Rk", "Rk_Org", "cOrgRk"]),
                    "ovr_rk": pick_first(rep, ["OvrRk", "ovrRk", "ovrRank", "Top100", "top100", "Ovr_Rk", "ovr_rank", "Ovr Rk", "Top_100", "cOvrRk"]),
                    "eta": pick_first(rep, ["ETA", "eta", "cETA", "Season", "Year"]),
                    "fv": pick_first(rep, ["FV", "fv", "cFV", "Current_FV", "FutureValue", "futureValue"]),
                    "tools": tools,
                    "tldr": pick_first(rep, ["TLDR", "tldr", "Summary", "summary", "Report", "report"]) or "無文字總結。",
                }

        roster_info = extract_fg_roster_info(page_props, res.text)
        yr_cnt = len(roster_info.get("yearly", [])) if roster_info else 0
        print(f"[{VERSION} FG RosterInfo] {player_name}: contract={roster_info.get('contract') if roster_info else None} fa={roster_info.get('fa_year') if roster_info else None} yearly_cnt={yr_cnt}")

        payload = {
            "scouting": scouting_obj,
            "resolved_url": resolved_url,
            "fg_stats": fg_stats,
            "roster_info": roster_info,
        }
        cache_set(cache_key, payload)
        return payload
    except Exception as e:
        print(f"[{VERSION} FanGraphs 球探解析異常] {fg_id}: {e}")
        return {"scouting": None, "resolved_url": None, "fg_stats": fg_stats, "roster_info": None}


def fetch_pitcher_custom_csv_row(mlbam_id: str, year: str) -> dict:
    cache_key = f"pit_csv_{year}"
    cached, hit = cache_get(cache_key)
    if not hit:
        url = "https://baseballsavant.mlb.com/leaderboard/custom"
        selections = "xera,xwoba,xba,exit_velocity_avg,barrel_batted_rate,hard_hit_percent,oz_swing_percent,whiff_percent,k_percent,bb_percent,ff_avg_speed,ff_avg_spin,groundballs_percent,release_extension"
        params = {
            "year": year, "type": "pitcher", "filter": "", "min": "1",
            "selections": selections, "chart": "false", "x": "xwoba", "y": "xwoba",
            "r": "no", "chartType": "beeswarm", "csv": "true",
        }
        row_map = {}
        try:
            res = requests.get(url, params=params, impersonate="chrome", timeout=15)
            if res.status_code == 200:
                reader = csv.DictReader(io.StringIO(res.text))
                for r in reader:
                    pid = r.get("player_id") or r.get("id")
                    if pid:
                        row_map[str(pid).strip()] = r
        except Exception:
            pass
        cache_set(cache_key, row_map)
        cached = row_map

    return (cached or {}).get(str(mlbam_id), {})


def fetch_league_pitch_avg_speed(year: str, hand: str) -> dict:
    hand = "L" if str(hand).upper() == "L" else "R"
    cache_key = f"lg_pitch_avg_{VERSION}_{year}_{hand}"
    cached, hit = cache_get(cache_key)
    if hit:
        return cached

    avg_map = dict(FALLBACK_LG_AVG_MPH.get(hand, {}))
    try:
        hand_url = "https://baseballsavant.mlb.com/leaderboard/pitch-arsenals"
        hand_res = requests.get(
            hand_url,
            params={"year": year, "min": "1", "type": "avg_speed", "hand": hand, "csv": "true"},
            impersonate="chrome",
            timeout=15,
        )
        hand_pids = set()
        if hand_res.status_code == 200 and "pitcher" in hand_res.text[:500].lower():
            for r in csv.DictReader(io.StringIO(hand_res.text.lstrip("\ufeff"))):
                pid = str(r.get("pitcher") or r.get("player_id") or "").strip()
                if pid:
                    hand_pids.add(pid)

        stats_cache_key = f"lg_arsenal_stats_raw_{year}"
        rows_parsed, stats_hit = cache_get(stats_cache_key)
        if not stats_hit:
            st_url = f"https://baseballsavant.mlb.com/leaderboard/pitch-arsenal-stats?type=pitcher&pitchType=&year={year}&team=&min=1&gameType=R&csv=true"
            st_res = requests.get(st_url, impersonate="chrome", timeout=20)
            rows_parsed = []
            if st_res.status_code == 200 and "pitch_type" in st_res.text[:2000]:
                for r in csv.DictReader(io.StringIO(st_res.text.lstrip("\ufeff"))):
                    pid = str(r.get("player_id") or r.get("pitcher") or "").strip()
                    pt = (r.get("pitch_type") or "").strip().upper()
                    pitches = r.get("pitches")
                    spd = r.get("avg_speed")
                    if pid and pt and is_valid_val(pitches) and is_valid_val(spd):
                        try:
                            cnt = int(float(pitches))
                            fv = float(spd)
                            if cnt > 0 and 65.0 <= fv <= 105.0:
                                rows_parsed.append((pid, pt, cnt, fv))
                        except Exception:
                            pass
            cache_set(stats_cache_key, rows_parsed)

        w_sums = {}
        w_cnts = {}
        for pid, pt, cnt, fv in (rows_parsed or []):
            if hand_pids and pid not in hand_pids:
                continue
            w_sums[pt] = w_sums.get(pt, 0.0) + fv * cnt
            w_cnts[pt] = w_cnts.get(pt, 0) + cnt

        for pt, tot_cnt in w_cnts.items():
            if tot_cnt >= 100:
                avg_map[pt] = round(w_sums[pt] / tot_cnt, 1)
    except Exception as e:
        print(f"[{VERSION} 聯盟均速抓取異常] {year} {hand}: {e}")

    cache_set(cache_key, avg_map)
    return avg_map


def fetch_pitch_arsenal_detail(mlbam_id: str, year: str):
    cache_key = f"pitch_arsenal_{VERSION}_{mlbam_id}_{year}"
    cached, hit = cache_get(cache_key)
    if hit:
        return cached

    url = (
        f"https://baseballsavant.mlb.com/statcast_search/csv"
        f"?all=true&hfGT=R%7C&hfSea={year}%7C&player_type=pitcher"
        f"&game_date_gt={year}-03-01&game_date_lt={year}-11-15"
        f"&pitchers_lookup%5B%5D={mlbam_id}&type=details"
    )

    try:
        res = requests.get(
            url,
            impersonate="chrome",
            headers={"Referer": "https://baseballsavant.mlb.com/statcast_search"},
            timeout=25,
        )
        stats = {}
        p_throws = "R"
        total_pitches = 0
        total_lhh = 0
        total_rhh = 0
        so_deltas = []

        if res.status_code == 200 and "pitch_type" in res.text[:2000]:
            reader = csv.DictReader(io.StringIO(res.text.lstrip("\ufeff")))
            ignore_codes = {"", "PO", "IN", "UN", "AB", "AS", "FA"}

            for r in reader:
                pt = (r.get("pitch_type") or "").strip().upper()
                if pt in ignore_codes:
                    continue

                if is_valid_val(r.get("p_throws")):
                    p_throws = str(r["p_throws"]).strip().upper()

                stand = (r.get("stand") or "").strip().upper()
                total_pitches += 1
                if stand == "L":
                    total_lhh += 1
                elif stand == "R":
                    total_rhh += 1

                st = stats.setdefault(pt, {
                    "code": pt,
                    "raw_name": (r.get("pitch_name") or "").strip(),
                    "count": 0,
                    "velo_sum": 0.0,
                    "velo_cnt": 0,
                    "lhh_cnt": 0,
                    "rhh_cnt": 0,
                    "lhh_delta": 0.0,
                    "rhh_delta": 0.0,
                })
                st["count"] += 1

                spd = r.get("release_speed")
                if is_valid_val(spd):
                    try:
                        st["velo_sum"] += float(spd)
                        st["velo_cnt"] += 1
                    except Exception:
                        pass

                d_raw = r.get("delta_pitcher_run_exp")
                if not is_valid_val(d_raw):
                    d_raw = r.get("delta_run_exp")
                d_val = 0.0
                if is_valid_val(d_raw):
                    try:
                        d_val = float(d_raw)
                    except Exception:
                        d_val = 0.0

                if (r.get("events") or "").strip().lower() == "strikeout" and d_val != 0.0:
                    so_deltas.append(d_val)

                if stand == "L":
                    st["lhh_cnt"] += 1
                    st["lhh_delta"] += d_val
                elif stand == "R":
                    st["rhh_cnt"] += 1
                    st["rhh_delta"] += d_val

        if total_pitches == 0 or not stats:
            fb_url = f"https://baseballsavant.mlb.com/leaderboard/pitch-arsenal-stats?type=pitcher&pitchType=&year={year}&team=&min=1&gameType=R&csv=true"
            fb_res = requests.get(fb_url, impersonate="chrome", timeout=20)
            if fb_res.status_code == 200 and "pitch_type" in fb_res.text[:2000]:
                fb_reader = csv.DictReader(io.StringIO(fb_res.text.lstrip("\ufeff")))
                for r in fb_reader:
                    pid = str(r.get("player_id") or r.get("pitcher") or "").strip()
                    if pid != str(mlbam_id):
                        continue
                    pt = (r.get("pitch_type") or "").strip().upper()
                    if not pt:
                        continue
                    cnt = int(float(r.get("pitches") or 0))
                    if cnt <= 0:
                        continue
                    total_pitches += cnt
                    spd = float(r["avg_speed"]) if is_valid_val(r.get("avg_speed")) else 0.0
                    stats[pt] = {
                        "code": pt,
                        "raw_name": (r.get("pitch_name") or pt).strip(),
                        "count": cnt,
                        "velo_sum": spd * cnt if spd > 0 else 0.0,
                        "velo_cnt": cnt if spd > 0 else 0,
                        "lhh_cnt": 0,
                        "rhh_cnt": 0,
                        "lhh_delta": 0.0,
                        "rhh_delta": 0.0,
                    }

        if total_pitches == 0 or not stats:
            cache_set(cache_key, None)
            return None

        rv_sign = -1.0
        if so_deltas and (sum(so_deltas) / len(so_deltas)) > 0:
            rv_sign = 1.0

        lg_avg_map = fetch_league_pitch_avg_speed(year, p_throws)

        def fmt_pct(cnt, tot):
            if tot <= 0 or cnt <= 0:
                return 0.0, "0%"
            pct = (cnt / tot) * 100.0
            if pct < 1.0:
                return round(pct, 1), "<1%"
            return round(pct, 1), f"{int(round(pct))}%"

        pitches_list = []
        for pt, st in sorted(stats.items(), key=lambda kv: kv[1]["count"], reverse=True):
            meta = PITCH_META.get(pt, {"name": st["raw_name"] or pt, "color": "#64748b", "dark_text": False})
            usage_num, usage_str = fmt_pct(st["count"], total_pitches)
            lhh_num, lhh_str = fmt_pct(st["lhh_cnt"], total_lhh)
            rhh_num, rhh_str = fmt_pct(st["rhh_cnt"], total_rhh)
            mph = round(st["velo_sum"] / st["velo_cnt"], 1) if st["velo_cnt"] > 0 else None

            pitches_list.append({
                "code": pt,
                "name": meta["name"],
                "color": meta["color"],
                "dark_text": meta["dark_text"],
                "count": st["count"],
                "usage": usage_num,
                "usage_str": usage_str,
                "mph": mph,
                "lg_avg_mph": lg_avg_map.get(pt),
                "lhh_usage": lhh_num,
                "lhh_usage_str": lhh_str,
                "lhh_rv": int(round(st["lhh_delta"] * rv_sign)),
                "rhh_usage": rhh_num,
                "rhh_usage_str": rhh_str,
                "rhh_rv": int(round(st["rhh_delta"] * rv_sign)),
            })

        result = {
            "year": str(year),
            "p_throws": p_throws,
            "avg_label": "LHP AVG" if p_throws == "L" else "RHP AVG",
            "total_pitches": total_pitches,
            "pitches": pitches_list,
        }
        cache_set(cache_key, result)
        return result
    except Exception as e:
        print(f"[{VERSION} Pitch Arsenal 抓取異常] {mlbam_id}: {e}")
        return None


def find_metric_pair(record: dict, exact_pr: list, exact_val: list, allow_unrounded_as_pr: bool = False, fuzzy_mode: str = None):
    pr_v = None
    est_pr = None

    for k in exact_pr:
        if k.endswith("_unrounded"):
            continue
        raw_k_val = record.get(k)
        if is_valid_val(raw_k_val):
            try:
                pr_v = int(round(float(raw_k_val)))
                break
            except Exception:
                continue

    unrounded_candidates = [k for k in exact_pr if k.endswith("_unrounded")] + [
        f"{k}_unrounded" for k in exact_pr if not k.endswith("_unrounded")
    ]
    for uk in unrounded_candidates:
        u_val = record.get(uk)
        if is_valid_val(u_val):
            try:
                fv = float(u_val)
                if 0.0 <= fv <= 1.0:
                    est_pr = max(1, min(100, int(round(fv * 100))))
                else:
                    est_pr = max(1, min(100, int(round(fv))))
                break
            except Exception:
                continue

    raw_v = next((record.get(k) for k in exact_val if is_valid_val(record.get(k))), None)

    if fuzzy_mode == "extension":
        if pr_v is None and est_pr is None:
            for k, v in record.items():
                kl = k.lower()
                if "extension" in kl and kl.startswith("percent_rank_") and is_valid_val(v):
                    try:
                        fv = float(v)
                        val_int = max(1, min(100, int(round(fv * 100)))) if (kl.endswith("_unrounded") and 0.0 <= fv <= 1.0) else int(round(fv))
                        if kl.endswith("_unrounded"):
                            est_pr = val_int
                        else:
                            pr_v = val_int
                            break
                    except Exception:
                        continue
        if raw_v is None:
            for k, v in record.items():
                kl = k.lower()
                if "extension" in kl and not kl.startswith("percent_rank_") and is_valid_val(v):
                    raw_v = v
                    break
    elif pr_v is None and est_pr is None and fuzzy_mode in ("all", "fastball", "breaking", "offspeed"):
        for k, v in record.items():
            kl = k.lower()
            if not is_valid_val(v) or not kl.startswith("percent_rank_"):
                continue
            if any(bad in kl for bad in ("velo", "speed", "spin", "pitches", "count")):
                continue
            if fuzzy_mode == "all":
                if ("run_value" in kl or "swing_take" in kl) and not any(t in kl for t in ("fastball", "breaking", "offspeed")):
                    fv = float(v)
                    val_int = max(1, min(100, int(round(fv * 100)))) if (kl.endswith("_unrounded") and 0.0 <= fv <= 1.0) else int(round(fv))
                    if kl.endswith("_unrounded"):
                        est_pr = val_int
                    else:
                        pr_v = val_int
                        break
            elif fuzzy_mode in kl and ("run_value" in kl or "rv" in kl):
                fv = float(v)
                val_int = max(1, min(100, int(round(fv * 100)))) if (kl.endswith("_unrounded") and 0.0 <= fv <= 1.0) else int(round(fv))
                if kl.endswith("_unrounded"):
                    est_pr = val_int
                else:
                    pr_v = val_int
                    break

    if pr_v is None and allow_unrounded_as_pr and est_pr is not None:
        pr_v = est_pr

    return pr_v, est_pr, raw_v


def fetch_savant_official_bars(mlbam_id: str, year: str, role: str):
    cache_key = f"savant_bars_{VERSION}_{mlbam_id}_{year}_{role}"
    cached, hit = cache_get(cache_key)
    if hit:
        return cached

    url = f"https://baseballsavant.mlb.com/savant-player/{mlbam_id}"
    try:
        res = requests.get(
            url,
            impersonate="chrome",
            headers={"Referer": "https://baseballsavant.mlb.com/"},
            timeout=15,
        )
        if res.status_code != 200:
            return None

        base_final_url = res.url.split("?")[0]
        target_stats = "statcast-r-pitching-mlb" if role == "pitcher" else "statcast-r-hitting-mlb"
        if target_stats not in res.url:
            res2 = requests.get(
                f"{base_final_url}?stats={target_stats}",
                impersonate="chrome",
                headers={"Referer": "https://baseballsavant.mlb.com/"},
                timeout=15,
            )
            if res2.status_code == 200:
                res = res2

        best_record = {}
        best_score = -1
        role_matched_objs = []

        for m in re.finditer(r'\{[^{}]+\}', res.text):
            chunk = m.group(0)
            if str(year) not in chunk:
                continue
            if "percent_rank_" not in chunk and "run_value" not in chunk and "rv_" not in chunk and "xera" not in chunk and "xwoba" not in chunk:
                continue
            try:
                row = json.loads(chunk)
                if not isinstance(row, dict):
                    continue
                row_yr = str(row.get("year") or row.get("season") or "")
                if row_yr != str(year):
                    continue

                non_null_keys = " ".join(k.lower() for k, v in row.items() if is_valid_val(v))

                has_core_pit = any(
                    sig in non_null_keys
                    for sig in ["xera", "fb_velocity", "ff_avg_speed", "fb_spin", "ff_avg_spin", "extension"]
                )
                has_rv = any(sig in non_null_keys for sig in ["run_value", "rv_", "swing_take"])
                has_bat_vals = any(
                    sig in non_null_keys
                    for sig in ["bat_speed", "swing_speed", "squared_up", "outs_above_average", "arm_overall", "sprint_speed"]
                )

                if role == "pitcher":
                    if has_bat_vals and not has_core_pit:
                        continue
                    if not has_core_pit and not has_rv:
                        continue
                else:
                    if has_core_pit and not has_bat_vals:
                        continue
                    if not has_bat_vals and "xwoba" not in non_null_keys:
                        continue

                role_matched_objs.append(row)

                non_null_prs = sum(1 for k, v in row.items() if k.lower().startswith("percent_rank_") and is_valid_val(v))
                score = len(non_null_keys) + non_null_prs * 5 + (500 if (role == "pitcher" and has_core_pit) else 0)

                if score > best_score:
                    best_record = dict(row)
                    best_score = score
            except Exception:
                continue

        for obj in role_matched_objs:
            for k, v in obj.items():
                if not is_valid_val(best_record.get(k)) and is_valid_val(v):
                    best_record[k] = v

        if role == "pitcher":
            csv_row = fetch_pitcher_custom_csv_row(mlbam_id, year)
            if csv_row:
                for k, v in csv_row.items():
                    if is_valid_val(v) and not is_valid_val(best_record.get(k)):
                        best_record[k] = v
                if is_valid_val(csv_row.get("release_extension")):
                    best_record["release_extension"] = csv_row["release_extension"]
                elif is_valid_val(csv_row.get("extension")):
                    best_record["release_extension"] = csv_row["extension"]

        if not best_record:
            cache_set(cache_key, None)
            return None

        if role == "pitcher":
            is_pa_qualified = is_valid_val(best_record.get("percent_rank_xera"))
            has_pitch_arsenal_pr = any(
                is_valid_val(best_record.get(k))
                for k in ("percent_rank_fastball_velo", "percent_rank_fb_velocity", "percent_rank_ff_avg_speed", "percent_rank_fb_velo")
            )
        else:
            is_pa_qualified = is_valid_val(best_record.get("percent_rank_xwoba"))
            has_pitch_arsenal_pr = False

        def fmt_num(val, fmt):
            if not is_valid_val(val):
                return "-"
            fval = float(val)
            if fmt == ".3f" and abs(fval) < 1:
                return f"{fval:{fmt}}".replace("0.", ".")
            s = f"{fval:{fmt}}"
            return "0" if s == "-0" else s

        if role == "batter":
            specs = [
                ("xwOBA", ["percent_rank_xwoba"], ["xwoba"], False, ".3f", None),
                ("xBA", ["percent_rank_xba"], ["xba"], False, ".3f", None),
                ("xSLG", ["percent_rank_xslg"], ["xslg"], False, ".3f", None),
                ("Avg Exit Velo", ["percent_rank_exit_velocity_avg"], ["exit_velocity_avg"], False, ".1f", None),
                ("Barrel %", ["percent_rank_barrel_batted_rate"], ["barrel_batted_rate"], False, ".1f", None),
                ("Hard-Hit %", ["percent_rank_hard_hit_percent"], ["hard_hit_percent"], False, ".1f", None),
                ("LA Sweet-Spot %", ["percent_rank_sweet_spot_percent"], ["sweet_spot_percent"], False, ".1f", None),
                ("Bat Speed", ["percent_rank_bat_speed", "percent_rank_avg_swing_speed", "percent_rank_swing_speed"], ["avg_swing_speed", "bat_speed", "swing_speed"], True, ".1f", None),
                ("Squared-Up %", ["percent_rank_squared_up_per_swing", "percent_rank_squared_up_swing", "percent_rank_squared_up"], ["squared_up_per_swing", "squared_up_swing", "squared_up"], True, ".1f", None),
                ("Chase %", ["percent_rank_chase_percent", "percent_rank_oz_swing_percent"], ["oz_swing_percent", "chase_percent"], False, ".1f", None),
                ("Whiff %", ["percent_rank_whiff_percent"], ["whiff_percent"], False, ".1f", None),
                ("K %", ["percent_rank_k_percent"], ["k_percent"], False, ".1f", None),
                ("BB %", ["percent_rank_bb_percent"], ["bb_percent"], False, ".1f", None),
                (
                    "Range (OAA)",
                    [
                        "percent_rank_oaa",
                        "percent_rank_outs_above_average",
                        "percent_rank_oaa_unrounded",
                        "percent_rank_outs_above_average_unrounded",
                    ],
                    ["outs_above_average", "oaa"],
                    True,
                    ".0f",
                    None,
                ),
                ("Arm Strength", ["percent_rank_arm_overall"], ["arm_overall"], True, ".1f", None),
            ]
        else:
            specs = [
                ("Pitching Run Value", ["percent_rank_pitch_run_value", "percent_rank_swing_take_all", "percent_rank_pitching_run_value", "percent_rank_pitch_run_value_unrounded"], ["pitch_run_value", "swing_take_all", "pitching_run_value"], is_pa_qualified, ".0f", "all"),
                ("Fastball Run Value", ["percent_rank_pitch_run_value_fastball", "percent_rank_fastball_run_value", "percent_rank_pitch_run_value_fastball_unrounded"], ["pitch_run_value_fastball", "fastball_run_value"], is_pa_qualified, ".0f", "fastball"),
                ("Breaking Run Value", ["percent_rank_pitch_run_value_breaking", "percent_rank_breaking_run_value", "percent_rank_pitch_run_value_breaking_unrounded"], ["pitch_run_value_breaking", "breaking_run_value"], is_pa_qualified, ".0f", "breaking"),
                ("Offspeed Run Value", ["percent_rank_pitch_run_value_offspeed", "percent_rank_offspeed_run_value", "percent_rank_pitch_run_value_offspeed_unrounded"], ["pitch_run_value_offspeed", "offspeed_run_value"], is_pa_qualified, ".0f", "offspeed"),
                ("xERA", ["percent_rank_xera"], ["xera"], False, ".2f", None),
                ("xwOBA", ["percent_rank_xwoba"], ["xwoba"], False, ".3f", None),
                ("xBA", ["percent_rank_xba"], ["xba"], False, ".3f", None),
                ("Fastball Velo", ["percent_rank_fastball_velo", "percent_rank_fastball_avg_speed", "percent_rank_fb_velo", "percent_rank_fb_velocity", "percent_rank_ff_avg_speed"], ["fastball_velo", "fastball_avg_speed", "fb_velo", "fb_velocity", "ff_avg_speed"], True, ".1f", None),
                ("Fastball Spin", ["percent_rank_fb_spin", "percent_rank_ff_avg_spin"], ["fb_spin", "ff_avg_spin"], True, ".0f", None),
                ("Avg Exit Velo", ["percent_rank_exit_velocity_avg"], ["exit_velocity_avg"], False, ".1f", None),
                ("Chase %", ["percent_rank_chase_percent", "percent_rank_oz_swing_percent"], ["oz_swing_percent", "chase_percent"], False, ".1f", None),
                ("Whiff %", ["percent_rank_whiff_percent"], ["whiff_percent"], False, ".1f", None),
                ("K %", ["percent_rank_k_percent"], ["k_percent"], False, ".1f", None),
                ("BB %", ["percent_rank_bb_percent"], ["bb_percent"], False, ".1f", None),
                ("Barrel %", ["percent_rank_barrel_batted_rate"], ["barrel_batted_rate"], False, ".1f", None),
                ("Hard-Hit %", ["percent_rank_hard_hit_percent"], ["hard_hit_percent"], False, ".1f", None),
                ("GB %", ["percent_rank_gb_percent", "percent_rank_groundballs_percent", "percent_rank_gb", "percent_rank_groundballs_percent_unrounded"], ["gb_percent", "groundballs_percent", "gb"], is_pa_qualified, ".1f", None),
                ("Extension", ["percent_rank_release_extension", "percent_rank_extension", "percent_rank_ext", "percent_rank_release_extension_unrounded", "percent_rank_extension_unrounded", "percent_rank_ext_unrounded"], ["release_extension", "extension", "ext"], is_pa_qualified or has_pitch_arsenal_pr, ".1f", "extension"),
            ]

        bars = []
        for label, exact_pr, exact_val, allow_unrounded_pr, fmt, fuzzy_mode in specs:
            pr_v, est_pr, raw_v = find_metric_pair(
                best_record, exact_pr, exact_val, allow_unrounded_as_pr=allow_unrounded_pr, fuzzy_mode=fuzzy_mode
            )
            val_str = fmt_num(raw_v, fmt)

            if pr_v is None and val_str == "-" and not ("Run Value" in label and not is_pa_qualified):
                continue

            bars.append({
                "label": label,
                "pr": pr_v,
                "est_pr": est_pr,
                "val": val_str,
                "pa_qualified": is_pa_qualified,
            })

        result = bars if bars else None
        cache_set(cache_key, result)
        return result
    except Exception as e:
        print(f"[{VERSION} Savant 抓取異常] {mlbam_id}: {e}")
        return None


class DashboardHandler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        super().end_headers()

    def do_GET(self):
        if self.path.startswith("/api/scouting/"):
            parsed = urlparse(self.path)
            fg_id = parsed.path.split("/api/scouting/")[1].strip()
            qs = parse_qs(parsed.query)
            player_name = qs.get("name", [""])[0]
            year = qs.get("year", ["2026"])[0]
            role = qs.get("role", ["batter"])[0]
            res_obj = fetch_fg_player_scouting(fg_id, player_name, year, role) or {}
            self._send_json({
                "fg_id": fg_id,
                "scouting": res_obj.get("scouting"),
                "resolved_url": res_obj.get("resolved_url"),
                "fg_stats": res_obj.get("fg_stats"),
                "roster_info": res_obj.get("roster_info"),
            })
            return

        if self.path.startswith("/api/savant_bars/"):
            parts = self.path.split("/api/savant_bars/")[1].split("?")[0].strip().split("/")
            if len(parts) >= 3:
                mlbam_id, year, role = parts[0], parts[1], parts[2]
                bars_data = fetch_savant_official_bars(mlbam_id, year, role)
                self._send_json({"mlbam_id": mlbam_id, "year": year, "role": role, "bars": bars_data})
                return

        if self.path.startswith("/api/pitch_arsenal/"):
            parts = self.path.split("/api/pitch_arsenal/")[1].split("?")[0].strip().split("/")
            if len(parts) >= 2:
                mlbam_id, year = parts[0], parts[1]
                arsenal_data = fetch_pitch_arsenal_detail(mlbam_id, year)
                self._send_json({"mlbam_id": mlbam_id, "year": year, "arsenal": arsenal_data})
                return

        return super().do_GET()

    def _send_json(self, payload: dict):
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))


class ThreadedTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    with ThreadedTCPServer(("", PORT), DashboardHandler) as httpd:
        print("==================================================")
        print(f" MLB 整合儀表板伺服器已啟動 | 版本：{VERSION}")
        print(f" 監聽埠號：{PORT}")
        print("==================================================")
        httpd.serve_forever()