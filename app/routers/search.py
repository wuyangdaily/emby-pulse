from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
import requests
import io
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

# ==========================================
# 🌟 核心：图片代理器 (绕过内网与HTTPS限制)
# ==========================================
@router.get("/api/library/image/{item_id}")
def proxy_emby_image(item_id: str, type: str = "Primary", width: int = 400):
    host = cfg.get("emby_host")
    key = cfg.get("emby_api_key")
    if not host or not key:
        return {"status": "error"}
    
    emby_img_url = f"{host}/emby/Items/{item_id}/Images/{type}?api_key={key}&MaxWidth={width}"
    try:
        res = requests.get(emby_img_url, stream=True, timeout=5)
        if res.status_code == 200:
            return StreamingResponse(io.BytesIO(res.content), media_type=res.headers.get("content-type", "image/jpeg"))
    except:
        pass
    # 代理失败时，前端的 onerror 会接管兜底
    return {"status": "error"}

# 通用媒体规格提取器
def extract_media_badges(item):
    badges = []
    if "MediaSources" in item and item["MediaSources"]:
        source = item["MediaSources"][0]
        media_streams = source.get("MediaStreams", [])
        
        video_stream = next((s for s in media_streams if s["Type"] == "Video"), None)
        audio_stream = next((s for s in media_streams if s["Type"] == "Audio"), None)

        if video_stream:
            width = video_stream.get("Width", 0)
            if width >= 3800:
                badges.append({"type": "res", "text": "4K", "color": "bg-yellow-500 text-yellow-900 border-yellow-400"})
            elif width >= 1900:
                badges.append({"type": "res", "text": "1080P", "color": "bg-blue-500 text-blue-100 border-blue-400"})
            
            video_range = video_stream.get("VideoRange", "")
            if video_range == "HDR":
                badges.append({"type": "fx", "text": "HDR", "color": "bg-purple-600 text-white border-purple-500"})
            elif video_range == "DOVI":
                badges.append({"type": "fx", "text": "Dolby Vision", "color": "bg-gradient-to-r from-indigo-600 to-purple-600 text-white border-indigo-400"})
                
        if audio_stream:
            codec = audio_stream.get("Codec", "").upper()
            channels = audio_stream.get("Channels", 2)
            channel_str = "5.1" if channels == 6 else ("7.1" if channels == 8 else f"{channels}.0")
            badges.append({"type": "audio", "text": f"{codec} {channel_str}", "color": "bg-slate-700 text-slate-200 border-slate-600"})
    return badges

@router.get("/api/library/search")
def global_library_search(query: str, request: Request):
    if not request.session.get("user"):
        return {"status": "error", "message": "未登录"}

    # 这是用于后端极速通信的内网 host
    host = cfg.get("emby_host")
    key = cfg.get("emby_api_key")
    if not host or not key:
        return {"status": "error", "message": "未配置 Emby 服务器"}

    # 🔥 获取公网链接：如果配置了公网URL就用公网的，如果没配置则自动回退到内网 host
    public_host = cfg.get("emby_public_url") or cfg.get("emby_external_url") or cfg.get("emby_public_host") or host
    public_host = public_host.rstrip('/')

    admin_id = get_emby_admin(host, key)
    if not admin_id:
        return {"status": "error", "message": "找不到管理员账号"}

    try:
        # API 穿透查询依然使用内网 host
        search_url = f"{host}/emby/Users/{admin_id}/Items"
        params = {
            "api_key": key,
            "SearchTerm": query,
            "IncludeItemTypes": "Movie,Series",
            "Recursive": "true",
            "Fields": "Overview,MediaSources,ProviderIds,ImageTags,ProductionYear", 
            "Limit": 8 
        }
        res = requests.get(search_url, params=params, timeout=10).json()
        items = res.get("Items", [])

        results = []
        for item in items:
            media_type = "movie" if item["Type"] == "Movie" else "tv"
            
            # 图片代理依然走映迹的后端路由
            poster_url = ""
            if item.get("ImageTags", {}).get("Primary"):
                poster_url = f"/api/library/image/{item['Id']}?type=Primary&width=400"
            elif item.get("ImageTags", {}).get("Backdrop"):
                poster_url = f"/api/library/image/{item['Id']}?type=Backdrop&width=400"
            else:
                tmdb_id = item.get("ProviderIds", {}).get("Tmdb")
                if tmdb_id:
                    poster_url = f"https://image.tmdb.org/t/p/w500/{tmdb_id}.jpg"
                else:
                    poster_url = "/static/img/logo-dark.png" 

            # 🔥 构造直达 Emby 的跳转链接 (使用刚刚获取的 public_host)
            emby_url = f"{public_host}/web/index.html#!/item/details.html?id={item['Id']}&serverId={item.get('ServerId', '')}"

            info = {
                "id": item["Id"],
                "name": item["Name"],
                "year": item.get("ProductionYear", "未知"),
                "overview": item.get("Overview", "暂无简介"),
                "type": media_type,
                "poster": poster_url,
                "emby_url": emby_url,  
                "badges": [] 
            }

            if media_type == "movie":
                info["badges"].extend(extract_media_badges(item))

            elif media_type == "tv":
                try:
                    eps_res = requests.get(
                        f"{host}/emby/Shows/{item['Id']}/Episodes?UserId={admin_id}&api_key={key}&Fields=ParentIndexNumber", 
                        timeout=5
                    ).json()
                    
                    season_counts = {}
                    for ep in eps_res.get("Items", []):
                        s_idx = ep.get("ParentIndexNumber")
                        if s_idx and s_idx > 0: 
                            season_counts[s_idx] = season_counts.get(s_idx, 0) + 1
                    
                    for s_idx in sorted(season_counts.keys()):
                        info["badges"].append({
                            "type": "season",
                            "text": f"第{s_idx}季: {season_counts[s_idx]}集",
                            "color": "bg-emerald-500 text-white border-emerald-400"
                        })

                    first_ep_res = requests.get(
                        f"{host}/emby/Shows/{item['Id']}/Episodes?UserId={admin_id}&api_key={key}&Limit=1&Fields=MediaSources", 
                        timeout=3
                    ).json()
                    if first_ep_res.get("Items"):
                        info["badges"].extend(extract_media_badges(first_ep_res["Items"][0]))
                except:
                    pass
            
            results.append(info)

        return {"status": "success", "data": results}
    except Exception as e:
        return {"status": "error", "message": f"全局搜索请求失败: {str(e)}"}