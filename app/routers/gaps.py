from fastapi import APIRouter, Depends, HTTPException
import requests
from datetime import datetime
from pydantic import BaseModel
import re

from app.core.config import cfg
from app.core.database import query_db

router = APIRouter(prefix="/api/gaps", tags=["gaps"])

# 辅助函数：获取代理
def _get_proxies():
    proxy = cfg.get("proxy_url")
    return {"http": proxy, "https": proxy} if proxy else None

# 辅助函数：获取管理员用户ID
def get_admin_user_id():
    host = cfg.get("emby_host")
    key = cfg.get("emby_api_key")
    if not host or not key: return None
    try:
        res = requests.get(f"{host}/emby/Users?api_key={key}", timeout=5)
        if res.status_code == 200:
            users = res.json()
            for u in users:
                if u.get("Policy", {}).get("IsAdministrator"):
                    return u['Id']
            if users: return users[0]['Id']
    except: pass
    return None

@router.get("/scan")
def scan_library_gaps():
    """
    【深空雷达引擎】
    扫描 Emby 媒体库中的剧集，对比 TMDB 获取缺集情况
    """
    print("\n🚀 [缺集雷达] 启动媒体库深度扫描任务...")
    host = cfg.get("emby_host")
    key = cfg.get("emby_api_key")
    tmdb_key = cfg.get("tmdb_api_key") 
    
    if not host or not key or not tmdb_key:
        print("❌ [缺集雷达] 缺少 Emby 或 TMDB API_KEY 配置！")
        return {"status": "error", "message": "系统未配置 Emby 或 TMDB API_KEY"}
        
    admin_id = get_admin_user_id()
    if not admin_id:
        print("❌ [缺集雷达] 无法获取 Emby 管理员身份")
        return {"status": "error", "message": "无法获取 Emby 管理员身份"}

    records = query_db("SELECT series_id, season_number, episode_number, status FROM gap_records")
    lock_map = {}
    if records:
        for r in records:
            lock_map[f"{r['series_id']}_{r['season_number']}_{r['episode_number']}"] = r['status']
            
    # 1. 抓取本地所有剧集 (Series)
    series_url = f"{host}/emby/Users/{admin_id}/Items?IncludeItemTypes=Series&Recursive=true&Fields=ProviderIds&api_key={key}"
    try:
        series_res = requests.get(series_url, timeout=15).json()
        series_list = series_res.get("Items", [])
        print(f"📡 [缺集雷达] 成功获取本地剧集，共计 {len(series_list)} 部，开始逐一穿透比对...")
    except Exception as e:
        print(f"❌ [缺集雷达] 请求 Emby 剧集失败: {e}")
        return {"status": "error", "message": f"请求 Emby 剧集失败: {str(e)}"}

    gap_results = []
    today = datetime.now().strftime("%Y-%m-%d")
    proxies = _get_proxies() # 获取代理配置

    for idx, series in enumerate(series_list):
        series_id = series.get("Id")
        series_name = series.get("Name")
        tmdb_id = series.get("ProviderIds", {}).get("Tmdb")
        
        print(f"⏳ [{idx+1}/{len(series_list)}] 正在分析: {series_name} (TMDB ID: {tmdb_id})")
        if not tmdb_id: 
            print("   -> ⚠️ 无 TMDB ID，已跳过")
            continue 
        
        # 2. 拉取本地该剧集的所有实际存在的单集
        episodes_url = f"{host}/emby/Users/{admin_id}/Items?ParentId={series_id}&IncludeItemTypes=Episode&Recursive=true&Fields=IndexNumberEnd&api_key={key}"
        try:
            local_eps_data = requests.get(episodes_url, timeout=10).json().get("Items", [])
        except Exception as e: 
            print(f"   -> ❌ 获取本地单集失败: {e}")
            continue

        local_inventory = {} 
        for ep in local_eps_data:
            s_num = ep.get("ParentIndexNumber") 
            e_num = ep.get("IndexNumber")       
            e_end = ep.get("IndexNumberEnd")    
            
            if s_num is None or e_num is None: continue
            if s_num not in local_inventory: local_inventory[s_num] = set()
            
            end_idx = e_end if e_end else e_num
            for i in range(e_num, end_idx + 1):
                local_inventory[s_num].add(i)

        # 3. 穿透查询 TMDB 真实数据
        try:
            tmdb_series_url = f"https://api.themoviedb.org/3/tv/{tmdb_id}?language=zh-CN&api_key={tmdb_key}"
            # 🔥 加上代理，防止超时！
            tmdb_series_data = requests.get(tmdb_series_url, proxies=proxies, timeout=10).json()
            tmdb_seasons = tmdb_series_data.get("seasons", [])
        except Exception as e: 
            print(f"   -> ❌ 请求 TMDB API 失败 (请检查代理或网络): {e}")
            continue

        series_gaps = []
        
        for season in tmdb_seasons:
            s_num = season.get("season_number")
            if s_num == 0 or s_num is None: continue
            
            tmdb_ep_count = season.get("episode_count", 0)
            if tmdb_ep_count == 0: continue
            
            local_season_inventory = local_inventory.get(s_num, set())
            if len(local_season_inventory) >= tmdb_ep_count: continue
            
            try:
                tmdb_season_url = f"https://api.themoviedb.org/3/tv/{tmdb_id}/season/{s_num}?language=zh-CN&api_key={tmdb_key}"
                tmdb_season_data = requests.get(tmdb_season_url, proxies=proxies, timeout=10).json()
                tmdb_episodes = tmdb_season_data.get("episodes", [])
            except Exception as e: 
                print(f"   -> ❌ 请求 TMDB 第 {s_num} 季数据失败: {e}")
                continue
            
            for tmdb_ep in tmdb_episodes:
                e_num = tmdb_ep.get("episode_number")
                air_date = tmdb_ep.get("air_date")
                
                if not air_date or air_date > today: continue
                
                if e_num not in local_season_inventory:
                    lock_key = f"{series_id}_{s_num}_{e_num}"
                    status = lock_map.get(lock_key, 0)
                    
                    if status == 1:
                        continue # 已被用户永久屏蔽
                        
                    series_gaps.append({
                        "season": s_num,
                        "episode": e_num,
                        "title": tmdb_ep.get("name", f"第 {e_num} 集"),
                        "status": status 
                    })
        
        if series_gaps:
            print(f"   -> 🚨 发现断层！共缺失 {len(series_gaps)} 集")
            gap_results.append({
                "series_id": series_id,
                "series_name": series_name,
                "tmdb_id": tmdb_id,
                "poster": f"{host}/emby/Items/{series_id}/Images/Primary?maxHeight=400&maxWidth=300&api_key={key}",
                "gaps": series_gaps
            })
        else:
            print(f"   -> ✅ 拼图完整")

    print(f"🎉 [缺集雷达] 扫描完毕！共揪出 {len(gap_results)} 部残缺剧集。\n")
    return {"status": "success", "data": gap_results}

@router.post("/ignore")
def ignore_gap(payload: dict):
    series_id = payload.get("series_id")
    series_name = payload.get("series_name", "未知剧集")
    season = int(payload.get("season_number", 0))
    episode = int(payload.get("episode_number", 0))
    
    if not series_id:
        return {"status": "error", "message": "参数缺失"}
        
    try:
        query_db("""
            INSERT INTO gap_records (series_id, series_name, season_number, episode_number, status) 
            VALUES (?, ?, ?, ?, 1)
            ON CONFLICT(series_id, season_number, episode_number) DO UPDATE SET status = 1
        """, (series_id, series_name, season, episode))
        return {"status": "success", "message": "✅ 已加入免检白名单，强迫症治愈！"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

# ----------------- 第二阶段：联动枢纽 -----------------
class GapSearchReq(BaseModel):
    series_id: str
    series_name: str
    season: int
    episode: int

class GapDownloadReq(BaseModel):
    series_id: str
    series_name: str
    season: int
    episode: int
    torrent_info: dict

@router.post("/search_mp")
def search_mp_for_gap(req: GapSearchReq):
    host = cfg.get("emby_host")
    key = cfg.get("emby_api_key")
    mp_url = cfg.get("moviepilot_url")
    mp_token = cfg.get("moviepilot_token")
    
    if not mp_url or not mp_token:
        return {"status": "error", "message": "系统未配置 MoviePilot 连接信息"}

    admin_id = get_admin_user_id()
    genes = []
    if admin_id:
        try:
            sample_url = f"{host}/emby/Users/{admin_id}/Items?ParentId={req.series_id}&IncludeItemTypes=Episode&Recursive=true&Limit=1&Fields=MediaSources&api_key={key}"
            sample_res = requests.get(sample_url, timeout=5).json()
            items = sample_res.get("Items", [])
            if items:
                sources = items[0].get("MediaSources", [])
                if sources:
                    video = next((s for s in sources[0].get("MediaStreams", []) if s.get("Type") == "Video"), None)
                    if video:
                        w = video.get("Width", 0)
                        if w >= 3800: genes.append("4K")
                        elif w >= 1900: genes.append("1080P")
                        
                        v_range = video.get("VideoRange", "")
                        d_title = video.get("DisplayTitle", "").upper()
                        if "HDR" in v_range or "HDR" in d_title: genes.append("HDR")
                        if "DOVI" in d_title or "DOLBY VISION" in d_title: genes.append("DoVi")
        except: pass
    
    if not genes: genes = ["未提取到特殊基因(默认)"]

    keyword = f"{req.series_name} S{str(req.season).zfill(2)}E{str(req.episode).zfill(2)}"
    clean_token = mp_token.strip().strip("'\"")
    headers = {
        "X-API-KEY": clean_token,
        "Authorization": f"Bearer {clean_token}"
    }
    
    try:
        mp_search_url = f"{mp_url.rstrip('/')}/api/v1/search/title?keyword={keyword}"
        # MP 一般在内网，不走代理
        mp_res = requests.get(mp_search_url, headers=headers, timeout=20)
        if mp_res.status_code != 200:
            return {"status": "error", "message": f"MP搜索失败 (HTTP {mp_res.status_code})"}
            
        results = mp_res.json()
        if not results:
            return {"status": "success", "data": {"genes": genes, "results": []}, "message": "MP 未搜索到该单集"}
            
        for r in results:
            score = 0
            title = r.get("title", "").upper()
            desc = r.get("description", "").upper()
            combined_text = title + " " + desc
            
            if "4K" in genes:
                if "2160P" in combined_text or "4K" in combined_text: score += 50
                else: score -= 20
            if "1080P" in genes:
                if "1080P" in combined_text: score += 50
            
            if "DoVi" in genes and ("DOVI" in combined_text or "VISION" in combined_text): score += 30
            if "HDR" in genes and "HDR" in combined_text: score += 20
            if "WEB" in combined_text: score += 10
            
            r["match_score"] = score
            
            tags = []
            if "2160P" in combined_text or "4K" in combined_text: tags.append("4K")
            elif "1080P" in combined_text: tags.append("1080P")
            if "DOVI" in combined_text or "VISION" in combined_text: tags.append("DoVi")
            elif "HDR" in combined_text: tags.append("HDR")
            if "WEB" in combined_text: tags.append("WEB-DL")
            r["extracted_tags"] = tags
            
        results.sort(key=lambda x: x["match_score"], reverse=True)
        top_results = results[:10]
        
        return {
            "status": "success", 
            "data": {
                "genes": genes,
                "results": top_results
            }
        }
    except Exception as e:
        return {"status": "error", "message": f"MP搜索异常: {str(e)}"}

@router.post("/download")
def download_gap_item(req: GapDownloadReq):
    mp_url = cfg.get("moviepilot_url")
    mp_token = cfg.get("moviepilot_token")
    if not mp_url or not mp_token:
        return {"status": "error", "message": "系统未配置 MoviePilot 连接信息"}

    clean_token = mp_token.strip().strip("'\"")
    headers = {
        "X-API-KEY": clean_token,
        "Authorization": f"Bearer {clean_token}"
    }

    try:
        mp_dl_url = f"{mp_url.rstrip('/')}/api/v1/download/"
        res = requests.post(mp_dl_url, headers=headers, json=req.torrent_info, timeout=10)
        
        if res.status_code == 200:
            query_db("""
                INSERT INTO gap_records (series_id, series_name, season_number, episode_number, status) 
                VALUES (?, ?, ?, ?, 2)
                ON CONFLICT(series_id, season_number, episode_number) DO UPDATE SET status = 2
            """, (req.series_id, req.series_name, req.season, req.episode))
            
            return {"status": "success", "message": "🚀 已成功下发至 MoviePilot，状态已锁定为处理中。"}
        else:
            return {"status": "error", "message": f"下发下载失败 (HTTP {res.status_code})"}
    except Exception as e:
        return {"status": "error", "message": str(e)}