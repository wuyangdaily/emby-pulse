from fastapi import APIRouter, Request
import requests
from app.core.config import cfg

router = APIRouter()

def get_emby_admin(host, key):
    try:
        users = requests.get(f"{host}/emby/Users?api_key={key}", timeout=5).json()
        for u in users:
            if u.get("Policy", {}).get("IsAdministrator"):
                return u['Id']
        return users[0]['Id'] if users else None
    except:
        return None

@router.get("/api/library/search")
def global_library_search(query: str, request: Request):
    # 鉴权：只有登录了主后台的人才能用全局搜索
    if not request.session.get("user"):
        return {"status": "error", "message": "未登录"}

    host = cfg.get("emby_host")
    key = cfg.get("emby_api_key")
    if not host or not key:
        return {"status": "error", "message": "未配置 Emby 服务器"}

    admin_id = get_emby_admin(host, key)
    if not admin_id:
        return {"status": "error", "message": "找不到管理员账号"}

    try:
        # 1. 穿透搜索 Emby 库
        # 🔥 注意这里增加了 ProviderIds，以便在图片缺失时去 TMDB 借图
        search_url = f"{host}/emby/Users/{admin_id}/Items"
        params = {
            "api_key": key,
            "SearchTerm": query,
            "IncludeItemTypes": "Movie,Series",
            "Recursive": "true",
            "Fields": "Overview,MediaSources,ProviderIds", 
            "Limit": 8 
        }
        res = requests.get(search_url, params=params, timeout=10).json()
        items = res.get("Items", [])

        results = []
        for item in items:
            media_type = "movie" if item["Type"] == "Movie" else "tv"
            
            # ================== 强化版图片获取策略 ==================
            # 1. 优先尝试获取 Primary（主海报）
            poster_url = ""
            if item.get("ImageTags", {}).get("Primary"):
                poster_url = f"{host}/emby/Items/{item['Id']}/Images/Primary?api_key={key}&MaxWidth=400"
            else:
                # 2. 如果没有 Primary，尝试用 Backdrop（背景图）
                if item.get("ImageTags", {}).get("Backdrop"):
                    poster_url = f"{host}/emby/Items/{item['Id']}/Images/Backdrop?api_key={key}&MaxWidth=400"
                else:
                    # 3. 如果还是没有，去 TMDB 提供商里挖图
                    tmdb_id = item.get("ProviderIds", {}).get("Tmdb")
                    if tmdb_id:
                        poster_url = f"https://image.tmdb.org/t/p/w500/{tmdb_id}.jpg"
                    else:
                        # 4. 终极兜底：使用 EmbyPulse 的默认占位图
                        poster_url = "/static/img/logo-dark.png" 

            # 背景图获取策略
            backdrop_url = ""
            if item.get("ImageTags", {}).get("Backdrop"):
                backdrop_url = f"{host}/emby/Items/{item['Id']}/Images/Backdrop?api_key={key}&MaxWidth=1280"
            elif item.get("ImageTags", {}).get("Primary"):
                backdrop_url = f"{host}/emby/Items/{item['Id']}/Images/Primary?api_key={key}&MaxWidth=1280"
            # ========================================================
            
            # 基础信息组装
            info = {
                "id": item["Id"],
                "name": item["Name"],
                "year": item.get("ProductionYear", "未知"),
                "overview": item.get("Overview", "暂无简介"),
                "type": media_type,
                "poster": poster_url,
                "backdrop": backdrop_url,
                "badges": [] 
            }

            # 2. 电影深度解析：画质、特效、音轨
            if media_type == "movie" and "MediaSources" in item and item["MediaSources"]:
                source = item["MediaSources"][0]
                media_streams = source.get("MediaStreams", [])
                
                video_stream = next((s for s in media_streams if s["Type"] == "Video"), None)
                audio_stream = next((s for s in media_streams if s["Type"] == "Audio"), None)

                # 解析视频规格
                if video_stream:
                    width = video_stream.get("Width", 0)
                    if width >= 3800:
                        info["badges"].append({"type": "res", "text": "4K", "color": "bg-yellow-500 text-yellow-900 border-yellow-400"})
                    elif width >= 1900:
                        info["badges"].append({"type": "res", "text": "1080P", "color": "bg-blue-500 text-blue-100 border-blue-400"})
                    
                    video_range = video_stream.get("VideoRange", "")
                    if video_range == "HDR":
                        info["badges"].append({"type": "fx", "text": "HDR", "color": "bg-purple-600 text-white border-purple-500"})
                    elif video_range == "DOVI":
                        info["badges"].append({"type": "fx", "text": "Dolby Vision", "color": "bg-gradient-to-r from-indigo-600 to-purple-600 text-white border-indigo-400"})
                        
                # 解析音频规格
                if audio_stream:
                    codec = audio_stream.get("Codec", "").upper()
                    channels = audio_stream.get("Channels", 2)
                    channel_str = "5.1" if channels == 6 else ("7.1" if channels == 8 else f"{channels}.0")
                    info["badges"].append({"type": "audio", "text": f"{codec} {channel_str}", "color": "bg-slate-700 text-slate-200 border-slate-600"})

            # 3. 剧集深度解析：穿透查询收录季数
            elif media_type == "tv":
                try:
                    seasons_res = requests.get(f"{host}/emby/Shows/{item['Id']}/Seasons?UserId={admin_id}&api_key={key}", timeout=3).json()
                    valid_seasons = [s["IndexNumber"] for s in seasons_res.get("Items", []) if s.get("IndexNumber", 0) > 0]
                    if valid_seasons:
                        info["badges"].append({
                            "type": "season", 
                            "text": f"已入库 {len(valid_seasons)} 季", 
                            "color": "bg-emerald-500 text-white border-emerald-400"
                        })
                except:
                    pass
            
            results.append(info)

        return {"status": "success", "data": results}
    except Exception as e:
        return {"status": "error", "message": f"全局搜索请求失败: {str(e)}"}