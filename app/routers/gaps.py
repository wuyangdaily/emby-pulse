from fastapi import APIRouter, BackgroundTasks
import requests
import threading
import concurrent.futures
from datetime import datetime
from typing import List, Dict, Any, Optional
import json
import urllib.parse
import re
import time

from app.core.config import cfg
from app.core.database import query_db
from app.routers.search import get_emby_sys_info, is_new_emby_router

router = APIRouter(prefix="/api/gaps", tags=["gaps"])

scan_state = {"is_scanning": False, "progress": 0, "total": 0, "current_item": "系统准备中...", "results": [], "error": None}
state_lock = threading.Lock()

def update_progress(item_name=None):
    with state_lock:
        scan_state["progress"] += 1
        if item_name: scan_state["current_item"] = f"分析剧集: {item_name[:20]}"

def _get_proxies():
    proxy = cfg.get("proxy_url")
    return {"http": proxy, "https": proxy} if proxy else None

def get_admin_user_id():
    host = cfg.get("emby_host"); key = cfg.get("emby_api_key")
    if not host or not key: return None
    try:
        users = requests.get(f"{host}/emby/Users?api_key={key}", timeout=5).json()
        for u in users:
            if u.get("Policy", {}).get("IsAdministrator"): return u['Id']
        return users[0]['Id'] if users else None
    except: return None

def process_single_series(series, lock_map, host, key, tmdb_key, proxies, today, global_inventory, server_id, use_new_route):
    series_id = series.get("Id"); series_name = series.get("Name", "未知剧集")
    tmdb_id = series.get("ProviderIds", {}).get("Tmdb")
    if not tmdb_id or lock_map.get(f"{series_id}_-1_-1", 0) == 1:
        update_progress(series_name)
        return None

    local_inventory = global_inventory.get(series_id, {})
    try:
        tmdb_series_data = requests.get(f"https://api.themoviedb.org/3/tv/{tmdb_id}?language=zh-CN&api_key={tmdb_key}", proxies=proxies, timeout=10).json()
        tmdb_seasons = tmdb_series_data.get("seasons", []); tmdb_status = tmdb_series_data.get("status", "") 
    except: 
        update_progress(series_name)
        return None

    series_gaps = []
    for season in tmdb_seasons:
        s_num = season.get("season_number")
        if not s_num or season.get("episode_count", 0) == 0: continue
        local_season_inventory = local_inventory.get(s_num, set())
        if len(local_season_inventory) >= season.get("episode_count", 0): continue
        try: tmdb_episodes = requests.get(f"https://api.themoviedb.org/3/tv/{tmdb_id}/season/{s_num}?language=zh-CN&api_key={tmdb_key}", proxies=proxies, timeout=10).json().get("episodes", [])
        except: continue
        for tmdb_ep in tmdb_episodes:
            e_num = tmdb_ep.get("episode_number"); air_date = tmdb_ep.get("air_date")
            if not air_date or air_date > today: continue
            if e_num not in local_season_inventory and lock_map.get(f"{series_id}_{s_num}_{e_num}", 0) != 1:
                series_gaps.append({"season": s_num, "episode": e_num, "title": tmdb_ep.get("name", f"第 {e_num} 集"), "status": lock_map.get(f"{series_id}_{s_num}_{e_num}", 0)})
    
    update_progress(series_name) 
    if series_gaps:
        public_host = (cfg.get("emby_public_url") or cfg.get("emby_external_url") or cfg.get("emby_public_host") or host).rstrip('/')
        emby_url = f"{public_host}/web/index.html#!/item?id={series_id}&serverId={server_id}" if use_new_route else f"{public_host}/web/index.html#!/item/details.html?id={series_id}&serverId={server_id}"
        return {"series_id": series_id, "series_name": series_name, "tmdb_id": tmdb_id, "poster": f"/api/library/image/{series_id}?type=Primary&width=300", "emby_url": emby_url, "gaps": series_gaps}
    else:
        if tmdb_status in ["Ended", "Canceled"]:
            try: query_db("INSERT OR IGNORE INTO gap_perfect_series (series_id, tmdb_id, series_name) VALUES (?, ?, ?)", (series_id, tmdb_id, series_name))
            except: pass
        return None

def run_scan_task():
    try:
        host = cfg.get("emby_host"); key = cfg.get("emby_api_key"); tmdb_key = cfg.get("tmdb_api_key"); admin_id = get_admin_user_id()
        proxies = _get_proxies(); today = datetime.now().strftime("%Y-%m-%d")
        try:
            sys_info = requests.get(f"{host}/emby/System/Info/Public", timeout=5).json()
            server_id = sys_info.get("Id", ""); use_new_route = is_new_emby_router(sys_info)
        except: server_id = ""; use_new_route = True

        query_db("CREATE TABLE IF NOT EXISTS gap_perfect_series (series_id TEXT PRIMARY KEY, tmdb_id TEXT, series_name TEXT, marked_at DATETIME DEFAULT CURRENT_TIMESTAMP)")
        query_db("CREATE TABLE IF NOT EXISTS gap_scan_cache (id INTEGER PRIMARY KEY, result_json TEXT, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)")

        records = query_db("SELECT series_id, season_number, episode_number, status FROM gap_records")
        lock_map = {f"{r['series_id']}_{r['season_number']}_{r['episode_number']}": r['status'] for r in records} if records else {}
        perfect_records = query_db("SELECT series_id FROM gap_perfect_series")
        perfect_set = set([r['series_id'] for r in perfect_records]) if perfect_records else set()

        all_series = requests.get(f"{host}/emby/Users/{admin_id}/Items?IncludeItemTypes=Series&Recursive=true&Fields=ProviderIds&api_key={key}", timeout=15).json().get("Items", [])
        pending_series = [s for s in all_series if s.get("Id") not in perfect_set]

        with state_lock:
            scan_state["total"] = len(pending_series)
            scan_state["current_item"] = "正在拉取全库单集缓存..."

        if not pending_series:
            with state_lock: scan_state["results"] = []
            return

        all_eps_data = requests.get(f"{host}/emby/Users/{admin_id}/Items?IncludeItemTypes=Episode&Recursive=true&Fields=IndexNumberEnd&api_key={key}", timeout=45).json().get("Items", [])
        global_inventory = {}
        for ep in all_eps_data:
            ser_id = ep.get("SeriesId"); s_num = ep.get("ParentIndexNumber"); e_num = ep.get("IndexNumber"); e_end = ep.get("IndexNumberEnd")
            if not ser_id or s_num is None or e_num is None: continue
            if ser_id not in global_inventory: global_inventory[ser_id] = {}
            if s_num not in global_inventory[ser_id]: global_inventory[ser_id][s_num] = set()
            for i in range(e_num, (e_end if e_end else e_num) + 1): global_inventory[ser_id][s_num].add(i)

        results = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(process_single_series, s, lock_map, host, key, tmdb_key, proxies, today, global_inventory, server_id, use_new_route) for s in pending_series]
            for f in concurrent.futures.as_completed(futures):
                res = f.result()
                if res: results.append(res)
        
        with state_lock: scan_state["results"] = results
        try: query_db("INSERT OR REPLACE INTO gap_scan_cache (id, result_json, updated_at) VALUES (1, ?, datetime('now', 'localtime'))", (json.dumps(results),))
        except: pass
    except Exception as e:
        with state_lock: scan_state["error"] = str(e)
    finally:
        with state_lock: scan_state["is_scanning"] = False; scan_state["current_item"] = "扫描完成"

@router.post("/scan/start")
def start_scan(bg_tasks: BackgroundTasks):
    with state_lock:
        if scan_state["is_scanning"]: return {"status": "error"}
        scan_state.update({"is_scanning": True, "progress": 0, "total": 0, "results": [], "error": None, "current_item": "系统准备中..."})
    bg_tasks.add_task(run_scan_task)
    return {"status": "success"}

@router.get("/scan/progress")
def get_progress():
    with state_lock:
        if not scan_state["is_scanning"]:
            if not scan_state["results"]:
                try:
                    row = query_db("SELECT result_json FROM gap_scan_cache WHERE id = 1")
                    if row: scan_state["results"] = json.loads(row[0]['result_json'])
                except: pass
            try:
                ignores = query_db("SELECT series_id FROM gap_records WHERE status=1 AND season_number=-1")
                ignore_ids = set([r['series_id'] for r in ignores]) if ignores else set()
                scan_state["results"] = [s for s in scan_state["results"] if s.get('series_id') not in ignore_ids]
            except: pass
        return {"status": "success", "data": scan_state}

@router.post("/scan/auto_toggle")
def toggle_auto_scan(payload: dict):
    enabled = 1 if payload.get("enabled") else 0
    query_db("INSERT INTO gap_records (series_id, series_name, season_number, episode_number, status) VALUES ('SYSTEM', 'AUTO_SCAN', -99, -99, ?) ON CONFLICT(series_id, season_number, episode_number) DO UPDATE SET status = ?", (enabled, enabled))
    return {"status": "success"}

@router.get("/scan/auto_status")
def get_auto_status():
    try: return {"status": "success", "enabled": bool(query_db("SELECT status FROM gap_records WHERE series_id='SYSTEM' AND season_number=-99")[0]['status'])}
    except: return {"status": "success", "enabled": False}

@router.post("/ignore")
def ignore_gap(payload: dict):
    try:
        s_id = payload.get("series_id"); s_num = int(payload.get("season_number", 0)); e_num = int(payload.get("episode_number", 0))
        query_db("INSERT INTO gap_records (series_id, series_name, season_number, episode_number, status) VALUES (?, ?, ?, ?, 1) ON CONFLICT(series_id, season_number, episode_number) DO UPDATE SET status = 1", (s_id, payload.get("series_name", ""), s_num, e_num))
        with state_lock:
            for s in scan_state["results"]:
                if s.get("series_id") == s_id: s["gaps"] = [ep for ep in s.get("gaps", []) if not (ep["season"] == s_num and ep["episode"] == e_num)]
            scan_state["results"] = [s for s in scan_state["results"] if len(s.get("gaps", [])) > 0]
        return {"status": "success"}
    except Exception as e: return {"status": "error"}

@router.post("/ignore/series")
def ignore_entire_series(payload: dict):
    try:
        s_id = payload.get("series_id")
        query_db("INSERT INTO gap_records (series_id, series_name, season_number, episode_number, status) VALUES (?, ?, -1, -1, 1) ON CONFLICT(series_id, season_number, episode_number) DO UPDATE SET status = 1", (s_id, payload.get("series_name", "")))
        with state_lock: scan_state["results"] = [s for s in scan_state["results"] if s.get("series_id") != s_id]
        return {"status": "success"}
    except Exception as e: return {"status": "error"}

@router.get("/ignores")
def get_ignored_list():
    try:
        records = query_db("SELECT id, series_id, series_name, season_number, episode_number, created_at FROM gap_records WHERE status = 1 AND series_id != 'SYSTEM'")
        perfects = query_db("SELECT series_id, series_name, marked_at FROM gap_perfect_series")
        data = []
        
        if records:
            for r in records: 
                data.append({
                    "type": "record", 
                    "id": r['id'], 
                    "series_name": r['series_name'], 
                    "target": "全剧集" if r['season_number'] == -1 else f"S{str(r['season_number']).zfill(2)}E{str(r['episode_number']).zfill(2)}", 
                    "time": r['created_at']
                })
                
        if perfects:
            for r in perfects: 
                data.append({
                    "type": "perfect", 
                    "id": r['series_id'], 
                    "series_name": r['series_name'], 
                    "target": "完结免检金牌", 
                    "time": r['marked_at']
                })
        
        # 🔥 核心防御加固：按时间倒序排列，即使有历史脏数据时间为空(None)，也会被 '0000-00-00' 兜底，绝不报错
        data.sort(key=lambda x: str(x['time'] or '0000-00-00'), reverse=True)
        
        return {"status": "success", "data": data}
    except Exception as e: 
        import logging
        logging.getLogger("uvicorn").error(f"回收站读取失败: {e}")
        return {"status": "error"}

@router.post("/unignore")
def unignore_item(payload: dict):
    try:
        if payload.get("type") == "record": query_db("DELETE FROM gap_records WHERE id = ?", (payload.get("id"),))
        elif payload.get("type") == "perfect": query_db("DELETE FROM gap_perfect_series WHERE series_id = ?", (payload.get("id"),))
        return {"status": "success"}
    except Exception as e: return {"status": "error"}

@router.get("/config")
def get_gap_config():
    query_db("CREATE TABLE IF NOT EXISTS gap_config (key TEXT PRIMARY KEY, value TEXT)")
    rows = query_db("SELECT key, value FROM gap_config")
    conf = {r['key']: r['value'] for r in rows} if rows else {}
    return {"status": "success", "data": conf}

@router.post("/config")
def save_gap_config(payload: dict):
    query_db("CREATE TABLE IF NOT EXISTS gap_config (key TEXT PRIMARY KEY, value TEXT)")
    for k, v in payload.items():
        query_db("INSERT OR REPLACE INTO gap_config (key, value) VALUES (?, ?)", (k, str(v).strip()))
    return {"status": "success"}


@router.post("/search_mp")
def search_mp_for_gap(payload: dict):
    series_id = payload.get("series_id"); series_name = payload.get("series_name")
    season = payload.get("season"); episodes = payload.get("episodes", [])
    mp_url = cfg.get("moviepilot_url"); mp_token = cfg.get("moviepilot_token")
    if not mp_url or not mp_token: return {"status": "error", "message": "未配置 MP"}
    
    admin_id = get_admin_user_id(); genes = []
    if admin_id:
        try:
            items = requests.get(f"{cfg.get('emby_host')}/emby/Users/{admin_id}/Items?ParentId={series_id}&IncludeItemTypes=Episode&Recursive=true&Limit=1&Fields=MediaSources&api_key={cfg.get('emby_api_key')}", timeout=5).json().get("Items", [])
            if items and items[0].get("MediaSources"):
                v = next((s for s in items[0]["MediaSources"][0].get("MediaStreams", []) if s.get("Type") == "Video"), None)
                if v:
                    if v.get("Width", 0) >= 3800: genes.append("4K")
                    elif v.get("Width", 0) >= 1900: genes.append("1080P")
                    if "HDR" in v.get("VideoRange", "") or "HDR" in v.get("DisplayTitle", "").upper(): genes.append("HDR")
                    if "DOVI" in v.get("DisplayTitle", "").upper() or "DOLBY VISION" in v.get("DisplayTitle", "").upper(): genes.append("DoVi")
        except: pass
    if not genes: genes = ["无明显特效"]
    
    headers = {"X-API-KEY": mp_token.strip().strip("'\""), "User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    
    def deep_extract(d, keys):
        for k in keys:
            if d.get(k) is not None and str(d.get(k)).strip() != "": return d.get(k)
        for n in ["torrent", "torrent_info", "detail", "data", "info"]:
            if isinstance(d.get(n), dict):
                for k in keys:
                    if d[n].get(k) is not None and str(d[n].get(k)).strip() != "": return d[n].get(k)
        return None

    try:
        results = []; is_pack = False
        if len(episodes) == 1:
            kw = f"{series_name} S{str(season).zfill(2)}E{str(episodes[0]).zfill(2)}"
            res_data = requests.get(f"{mp_url.rstrip('/')}/api/v1/search/title?keyword={urllib.parse.quote(kw)}", headers=headers, timeout=20).json()
            if isinstance(res_data, dict): res_data = res_data.get("data") or res_data.get("results") or []
            if isinstance(res_data, list): results = res_data
        
        if len(results) == 0:
            kw2 = f"{series_name} S{str(season).zfill(2)}"
            res_data2 = requests.get(f"{mp_url.rstrip('/')}/api/v1/search/title?keyword={urllib.parse.quote(kw2)}", headers=headers, timeout=20).json()
            if isinstance(res_data2, dict): res_data2 = res_data2.get("data") or res_data2.get("results") or []
            if isinstance(res_data2, list): results = res_data2; is_pack = True

        processed = []
        for r in results:
            score = 0
            title = str(deep_extract(r, ["name", "title", "torrent_name"]) or "未提取到种名")
            desc = str(deep_extract(r, ["description", "desc", "detail", "subtitle"]) or "")
            text = (title + " " + desc).upper()
            size_val = deep_extract(r, ["size", "enclosure_size", "torrent_size"]) or 0
            site_val = deep_extract(r, ["site_name", "site", "indexer"]) or "未知站点"
            seeders_val = deep_extract(r, ["seeders", "seeder"]) or 0
            
            if "4K" in text: score += 50 if ("2160P" in text or "4K" in text) else -20
            if "1080P" in text: score += 50
            if "DoVi" in text or "VISION" in text: score += 30
            if "HDR" in text: score += 20
            if "WEB" in text: score += 10
            
            r["ui_title"] = title; r["ui_site"] = str(site_val)
            try: r["ui_size"] = float(size_val)
            except: r["ui_size"] = 0
            try: r["ui_seeders"] = int(seeders_val)
            except: r["ui_seeders"] = 0
            
            r["match_score"] = score
            r["is_pack"] = is_pack 
            r["org_payload"] = r.get("torrent_info", r) 
            
            tags = []
            if "2160P" in text or "4K" in text: tags.append("4K")
            elif "1080P" in text: tags.append("1080P")
            if "DOVI" in text or "VISION" in text: tags.append("DoVi")
            elif "HDR" in text: tags.append("HDR")
            if "WEB" in text: tags.append("WEB-DL")
            r["extracted_tags"] = tags
            processed.append(r)

        processed.sort(key=lambda x: x["match_score"], reverse=True)
        return {"status": "success", "data": {"genes": genes, "results": processed[:10]}}
    except Exception as e: return {"status": "error", "message": str(e)}

# ==========================================
# 🔥 宗师级：文件名集数提取正则引擎
# ==========================================
def extract_episodes_from_filename(filename: str) -> set:
    eps = set()
    fname = filename.upper()
    
    # 1. 匹配 S01E08, S01E08-E09, S01E08-09
    s_e = re.findall(r'S\d{1,2}E(\d{1,3})(?:-E?(\d{1,3}))?', fname)
    for e1, e2 in s_e:
        eps.add(int(e1))
        if e2: eps.update(range(int(e1), int(e2)+1))
        
    # 2. 匹配 EP08, E08, EPISODE 08, EP08-09 (使用非捕获组 ?:)
    ep = re.findall(r'(?:EPISODE|EP|E)[\s\.\-]*(\d{1,3})(?:-E?(\d{1,3}))?', fname)
    for e1, e2 in ep:
        eps.add(int(e1))
        if e2: eps.update(range(int(e1), int(e2)+1))
        
    # 3. 匹配 中文 第08集, 第8-9集
    zh = re.findall(r'第\s*(\d{1,3})\s*(?:-|至|到)\s*(\d{1,3})\s*集', filename)
    for e1, e2 in zh:
        eps.update(range(int(e1), int(e2)+1))
    zh_single = re.findall(r'第\s*(\d{1,3})\s*集', filename)
    for e in zh_single: eps.add(int(e))
        
    # 4. 保底机制：匹配裸露的数字（比如 [08], .08., - 08）
    if not eps:
        naked = re.findall(r'(?:\[|\s-?\s|\.)(\d{2,4})(?:\]|\s|\.)', fname)
        for e in naked:
            num = int(e)
            if num not in (480, 720, 1080, 2160, 264, 265, 2020, 2021, 2022, 2023, 2024, 2025, 2026, 2027):
                eps.add(num)
                
    return eps

# ==========================================
# 🔥 截胡引擎：qBittorrent (核弹级日志版)
# ==========================================
def hook_qbittorrent(host, user, password, expected_size, target_episodes):
    print(f"\n==============================================")
    print(f"[qB 截胡引擎] 🚀 启动特种兵潜入任务")
    print(f"[qB 截胡引擎] 目标: {host} | 目标大小: {expected_size} Bytes | 需求集数: {target_episodes}")
    try:
        s = requests.Session()
        login = s.post(f"{host.rstrip('/')}/api/v2/auth/login", data={"username": user, "password": password}, timeout=10)
        if login.status_code != 200 or "Ok" not in login.text: 
            print("[qB 截胡引擎] ❌ 登录失败，检查账号密码")
            return False, "qBittorrent 登录失败"
        
        print("[qB 截胡引擎] ✅ 登录成功！开始轮询监控新种子下发...")
        target_hash = None
        
        # 轮询 20 次，每次 3 秒，共等待 60 秒
        for attempt in range(20):
            print(f"[qB 截胡引擎] ⏳ 轮询第 {attempt+1}/20 次...")
            time.sleep(3)
            res = s.get(f"{host.rstrip('/')}/api/v2/torrents/info?filter=all", timeout=10)
            if res.status_code == 200:
                for t in res.json():
                    # 匹配规则：5分钟内添加 且 大小误差小于 10MB，天然的指纹锁定！
                    if time.time() - t.get("added_on", 0) < 300: 
                        if expected_size > 0 and abs(t.get("total_size", 0) - expected_size) < 10 * 1024 * 1024:
                            target_hash = t.get("hash")
                            print(f"[qB 截胡引擎] 🎯 指纹锁定种子 Hash: {target_hash} (大小: {t.get('total_size')})")
                            break
                        elif expected_size == 0:
                            target_hash = t.get("hash")
                            break
            
            # 如果抓到种子，尝试获取文件列表
            if target_hash:
                f_res = s.get(f"{host.rstrip('/')}/api/v2/torrents/files?hash={target_hash}", timeout=10)
                files = f_res.json() if f_res.status_code == 200 else []
                # 只有当 qB 真正把元数据(Metadata)下载完，文件列表才会出来
                if files and len(files) > 0 and files[0].get("size", 0) > 0:
                    print(f"[qB 截胡引擎] 📂 获取到 {len(files)} 个文件，开始逐一验明正身...")
                    wanted, unwanted = [], []
                    for i, f in enumerate(files):
                        fname = f.get("name", "")
                        if not fname.lower().endswith(('.mp4', '.mkv', '.avi', '.ts', '.iso')):
                            unwanted.append(str(i))
                            continue
                        
                        # 调用正则引擎
                        f_eps = extract_episodes_from_filename(fname)
                        is_wanted = any(e in target_episodes for e in f_eps)
                        
                        print(f"  --> 文件: {fname[:30]}... | 识别出集数: {f_eps} | 保留: {is_wanted}")
                        
                        if is_wanted: wanted.append(str(i))
                        else: unwanted.append(str(i))
                        
                    if not wanted:
                        print("[qB 截胡引擎] ⚠️ 警告：正则未匹配到任何需要的集数，取消截胡，放行全包。")
                        return False, "⚠️ 正则未能识别出视频集数，为防误杀已放行"
                    
                    # 绝对优先级控制：先全部踢掉，再把需要的拉回来！
                    print(f"[qB 截胡引擎] 🔪 执行手术：踢除 {len(unwanted)} 个文件，提权 {len(wanted)} 个文件")
                    if unwanted: s.post(f"{host.rstrip('/')}/api/v2/torrents/filePrio", data={"hash": target_hash, "id": "|".join(unwanted), "priority": 0}, timeout=10)
                    if wanted: s.post(f"{host.rstrip('/')}/api/v2/torrents/filePrio", data={"hash": target_hash, "id": "|".join(wanted), "priority": 1}, timeout=10)
                    
                    print("[qB 截胡引擎] 🎉 截胡行动圆满成功！\n==============================================")
                    return True, f"🔪 截胡成功！保留 {len(wanted)} 集，剔除 {len(unwanted)} 个多余文件"
                    
        print("[qB 截胡引擎] ❌ 轮询 60 秒超时，未锁定种子或卡在 Metadata\n==============================================")
        return False, "轮询 60 秒超时：未锁定种子或未获取到文件列表"
    except Exception as e:
        print(f"[qB 截胡引擎] 崩溃: {e}\n==============================================")
        return False, f"qB 交互异常: {str(e)}"

# ==========================================
# 🔥 截胡引擎：Transmission
# ==========================================
def hook_transmission(host, user, password, expected_size, target_episodes):
    print(f"\n==============================================")
    print(f"[TR 截胡引擎] 🚀 启动特种兵潜入任务")
    try:
        rpc_url = f"{host.rstrip('/')}/transmission/rpc"
        auth = (user, password) if user else None
        s = requests.Session()
        
        res = s.post(rpc_url, auth=auth, timeout=10)
        session_id = res.headers.get('X-Transmission-Session-Id')
        if not session_id: return False, "Transmission 认证失败"
        s.headers.update({'X-Transmission-Session-Id': session_id})
        
        target_id = None
        for attempt in range(20):
            print(f"[TR 截胡引擎] ⏳ 轮询第 {attempt+1}/20 次...")
            time.sleep(3)
            payload = {"method": "torrent-get", "arguments": {"fields": ["id", "addedDate", "totalSize", "files"]}}
            r = s.post(rpc_url, json=payload, auth=auth, timeout=10)
            if r.status_code == 200:
                torrents = r.json().get("arguments", {}).get("torrents", [])
                for t in torrents:
                    if time.time() - t.get("addedDate", 0) < 300:
                        if expected_size > 0 and abs(t.get("totalSize", 0) - expected_size) < 10 * 1024 * 1024:
                            target_id = t.get("id"); files = t.get("files", [])
                            print(f"[TR 截胡引擎] 🎯 锁定种子 ID: {target_id}")
                            break
            
            if target_id and files and len(files) > 0 and files[0].get("length", 0) > 0:
                wanted, unwanted = [], []
                for i, f in enumerate(files):
                    fname = f.get("name", "")
                    if not fname.lower().endswith(('.mp4', '.mkv', '.avi', '.ts', '.iso')):
                        unwanted.append(i); continue
                        
                    f_eps = extract_episodes_from_filename(fname)
                    if any(e in target_episodes for e in f_eps): wanted.append(i)
                    else: unwanted.append(i)
                    
                if not wanted: return False, "⚠️ 正则未匹配到视频集数，为防止误杀，已放行全包下载"
                    
                set_payload = {"method": "torrent-set", "arguments": {"id": target_id}}
                if unwanted: set_payload["arguments"]["files-unwanted"] = unwanted
                if wanted: set_payload["arguments"]["files-wanted"] = wanted
                
                s.post(rpc_url, json=set_payload, auth=auth, timeout=10)
                print("[TR 截胡引擎] 🎉 截胡行动圆满成功！\n==============================================")
                return True, f"🔪 TR 截胡成功！保留 {len(wanted)} 集，剔除 {len(unwanted)} 个文件"
                
        return False, "轮询 60 秒超时：未锁定种子"
    except Exception as e:
        return False, f"TR 交互异常: {str(e)}"

# ==========================================
# 🔥 下载分发总控中心
# ==========================================
@router.post("/download")
def download_gap_item(payload: dict):
    series_id = payload.get("series_id")
    series_name = payload.get("series_name")
    season = payload.get("season")
    episodes = payload.get("episodes", [])
    torrent_info = payload.get("torrent_info", {})

    print(f"\n[中央分发] 收到 {series_name} S{season} 提取请求: {episodes}")

    mp_url = cfg.get("moviepilot_url")
    mp_token = cfg.get("moviepilot_token")
    clean_token = mp_token.strip().strip("'\"") if mp_token else ""
    headers = {"X-API-KEY": clean_token, "Content-Type": "application/json"}
    
    query_db("CREATE TABLE IF NOT EXISTS gap_config (key TEXT PRIMARY KEY, value TEXT)")
    ui_conf = {r['key']: r['value'] for r in query_db("SELECT key, value FROM gap_config")} if query_db("SELECT key, value FROM gap_config") else {}
    
    client_type = ui_conf.get("client_type", "")
    client_url = ui_conf.get("client_url", "")
    client_user = ui_conf.get("client_user", "")
    client_pass = ui_conf.get("client_pass", "")
    
    pure_torrent_in = torrent_info.get("org_payload", torrent_info)
    
    # 强制将 size 转换为 int (避免 float 报错)
    try:
        pure_torrent_in["size"] = int(float(pure_torrent_in.get("size", 0)))
    except:
        pure_torrent_in["size"] = 0

    mp_payload = {"torrent_in": pure_torrent_in}

    try:
        print("[中央分发] 正在呼叫 MP /add 接口，等待 MP 响应 (超时限制 90 秒)...")
        # 🔥 将这里的 timeout 彻底放宽到 90 秒，避免被 MP 的找字幕、下种子等阻塞拖死
        res = requests.post(f"{mp_url.rstrip('/')}/api/v1/download/add", headers=headers, json=mp_payload, timeout=90)
        print(f"[中央分发] 收到 MP 响应，状态码: {res.status_code}")
        
        hook_msg = ""
        if res.status_code in [200, 201]:
            # 🔥 只要 MP 接单成功，立刻发动底层下载器截胡引擎！
            if client_type and client_url and len(episodes) > 0 and torrent_info.get("is_pack", False):
                expected_size = pure_torrent_in.get("size", 0)
                if client_type == "qbittorrent":
                    success, hook_msg = hook_qbittorrent(client_url, client_user, client_pass, expected_size, episodes)
                elif client_type == "transmission":
                    success, hook_msg = hook_transmission(client_url, client_user, client_pass, expected_size, episodes)
                hook_msg = f"\n{hook_msg}"

            # 写数据库
            for ep in episodes:
                query_db("INSERT INTO gap_records (series_id, series_name, season_number, episode_number, status) VALUES (?, ?, ?, ?, 2) ON CONFLICT(series_id, season_number, episode_number) DO UPDATE SET status = 2", (series_id, series_name, int(season), int(ep)))
            
            with state_lock:
                for s in scan_state["results"]:
                    if s.get("series_id") == series_id:
                        for ep_obj in s.get("gaps", []):
                            if ep_obj["season"] == int(season) and ep_obj["episode"] in [int(e) for e in episodes]:
                                ep_obj["status"] = 2

            return {"status": "success", "message": f"种子已推给 MP！{hook_msg}"}
            
        return {"status": "error", "message": f"MP 接口拒绝 (HTTP {res.status_code})"}
    except requests.exceptions.ReadTimeout:
        print("[中央分发] ❌ MP 响应超时 (90秒)，可能是网络拥堵或 MP 在找字幕。")
        return {"status": "error", "message": "推送超时，但 MP 可能仍在后台处理，请稍后检查 qB。"}
    except Exception as e: 
        print(f"[中央分发] 请求发生崩溃异常: {e}")
        return {"status": "error", "message": str(e)}