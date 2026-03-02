from fastapi import APIRouter, Request, Depends
from pydantic import BaseModel
import requests
import sqlite3
import io
import json
from app.core.config import cfg, REPORT_COVER_URL
from app.core.database import DB_PATH
from app.schemas.models import MediaRequestSubmitModel as BaseSubmitModel
from app.services.bot_service import bot

router = APIRouter()

# 🔥 数据库自动升维：支持多季并存
def ensure_db_schema():
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    # 1. 确保求片主表有 season 和 reject_reason
    try: c.execute("ALTER TABLE media_requests ADD COLUMN season INTEGER DEFAULT 0")
    except: pass
    try: c.execute("ALTER TABLE media_requests ADD COLUMN reject_reason TEXT")
    except: pass
    # 2. 确保投票关联表也有 season 字段，防止多季投票冲突
    try: c.execute("ALTER TABLE request_users ADD COLUMN season INTEGER DEFAULT 0")
    except: pass
    conn.commit(); conn.close()
ensure_db_schema()

def execute_sql(query, params=()):
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    try:
        c.execute(query, params); conn.commit()
        return True, ""
    except Exception as e:
        conn.rollback(); return False, str(e)
    finally: conn.close()

def get_emby_admin(host, key):
    if not host or not key: return None
    try:
        users = requests.get(f"{host}/emby/Users?api_key={key}", timeout=5).json()
        for u in users:
            if u.get("Policy", {}).get("IsAdministrator"): return u['Id']
        return users[0]['Id'] if users else None
    except: return None

class MediaRequestSubmitModel(BaseSubmitModel):
    season: int = 0

# 🔥 管理员 Action 模型同步升级，携带 season 参数
class AdminActionModel(BaseModel):
    tmdb_id: int
    season: int = 0
    action: str
    reject_reason: str = None

class RequestLoginModel(BaseModel):
    username: str
    password: str

@router.post("/api/requests/auth")
def request_system_login(data: RequestLoginModel, request: Request):
    host = cfg.get("emby_host")
    if not host: return {"status": "error", "message": "系统未配置 Emby 地址"}
    headers = {"X-Emby-Authorization": 'MediaBrowser Client="EmbyPulse", Device="Web", DeviceId="PulseReqSys", Version="1.0"'}
    try:
        res = requests.post(f"{host}/emby/Users/AuthenticateByName", json={"Username": data.username, "Pw": data.password}, headers=headers, timeout=8)
        if res.status_code == 200:
            user_info = res.json().get("User", {})
            request.session["req_user"] = {"Id": user_info.get("Id"), "Name": user_info.get("Name")}
            return {"status": "success", "message": "登录成功"}
        return {"status": "error", "message": "账号或密码错误"}
    except Exception as e: return {"status": "error", "message": f"连接 Emby 失败: {str(e)}"}

@router.get("/api/requests/check")
def check_auth(request: Request):
    user = request.session.get("req_user")
    if user: return {"status": "success", "user": user}
    return {"status": "error", "message": "未登录"}

@router.post("/api/requests/logout")
def request_system_logout(request: Request):
    request.session.clear()
    return {"status": "success"}

@router.get("/api/requests/trending")
def get_trending(request: Request):
    tmdb_key = cfg.get("tmdb_api_key")
    if not tmdb_key: return {"status": "error", "message": "未配置 TMDB Key"}
    proxy = cfg.get("proxy_url"); proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        url_m = f"https://api.themoviedb.org/3/trending/movie/week?api_key={tmdb_key}&language=zh-CN"
        res_m = requests.get(url_m, proxies=proxies, timeout=10).json()
        url_t = f"https://api.themoviedb.org/3/trending/tv/week?api_key={tmdb_key}&language=zh-CN"
        res_t = requests.get(url_t, proxies=proxies, timeout=10).json()
        
        movies, tvs = [], []
        for item in res_m.get("results", [])[:20]:
            movies.append({
                "tmdb_id": item.get("id"), "media_type": "movie",
                "title": item.get("title") or item.get("name"), 
                "year": (item.get("release_date") or item.get("first_air_date") or "")[:4] or "未知",
                "poster_path": f"https://image.tmdb.org/t/p/w500{item.get('poster_path')}" if item.get("poster_path") else "",
                "backdrop_path": f"https://image.tmdb.org/t/p/w1280{item.get('backdrop_path')}" if item.get("backdrop_path") else "",
                "overview": item.get("overview", ""), "vote_average": round(item.get("vote_average", 0), 1)
            })
        for item in res_t.get("results", [])[:20]:
            tvs.append({
                "tmdb_id": item.get("id"), "media_type": "tv",
                "title": item.get("title") or item.get("name"), 
                "year": (item.get("release_date") or item.get("first_air_date") or "")[:4] or "未知",
                "poster_path": f"https://image.tmdb.org/t/p/w500{item.get('poster_path')}" if item.get("poster_path") else "",
                "backdrop_path": f"https://image.tmdb.org/t/p/w1280{item.get('backdrop_path')}" if item.get("backdrop_path") else "",
                "overview": item.get("overview", ""), "vote_average": round(item.get("vote_average", 0), 1)
            })
        return {"status": "success", "data": {"movies": movies, "tv": tvs}}
    except Exception as e: return {"status": "error", "message": str(e)}

@router.get("/api/requests/tv/{tmdb_id}")
def get_tv_details(tmdb_id: int, request: Request):
    tmdb_key = cfg.get("tmdb_api_key"); proxy = cfg.get("proxy_url"); proxies = {"http": proxy, "https": proxy} if proxy else None
    local_seasons = []
    try:
        emby_host = cfg.get("emby_host"); emby_key = cfg.get("emby_api_key")
        admin_id = get_emby_admin(emby_host, emby_key)
        if admin_id:
            search_url = f"{emby_host}/emby/Users/{admin_id}/Items?AnyProviderIdEquals=tmdb.{tmdb_id}&IncludeItemTypes=Series&Recursive=true&api_key={emby_key}"
            res = requests.get(search_url, timeout=5).json()
            if res.get("Items"):
                series_id = res["Items"][0]["Id"]
                season_url = f"{emby_host}/emby/Shows/{series_id}/Seasons?UserId={admin_id}&api_key={emby_key}"
                season_res = requests.get(season_url, timeout=5).json()
                local_seasons = [s.get("IndexNumber") for s in season_res.get("Items", []) if s.get("IndexNumber") is not None]

        url = f"https://api.themoviedb.org/3/tv/{tmdb_id}?api_key={tmdb_key}&language=zh-CN"
        tmdb_res = requests.get(url, proxies=proxies, timeout=10).json()
        seasons = [{"season_number": s["season_number"], "name": s["name"], "episode_count": s["episode_count"], "exists_locally": s["season_number"] in local_seasons} 
                   for s in tmdb_res.get("seasons", []) if s["season_number"] > 0]
        return {"status": "success", "seasons": seasons}
    except Exception as e: return {"status": "error", "message": str(e)}

@router.get("/api/requests/search")
def search_tmdb(query: str, request: Request):
    if not request.session.get("req_user"): return {"status": "error", "message": "未登录"}
    tmdb_key = cfg.get("tmdb_api_key"); proxy = cfg.get("proxy_url"); proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        url = f"https://api.themoviedb.org/3/search/multi?api_key={tmdb_key}&language=zh-CN&query={query}&page=1"
        res = requests.get(url, proxies=proxies, timeout=10).json()
        results = []; tmdb_ids = [str(item['id']) for item in res.get("results", []) if item.get("media_type") in ["movie", "tv"]]
        
        # 查重逻辑（简易版，只查整剧是否在库）
        emby_exists_set = set()
        emby_host = cfg.get("emby_host"); emby_key = cfg.get("emby_api_key")
        admin_id = get_emby_admin(emby_host, emby_key)
        if admin_id and tmdb_ids:
            provider_query = ",".join([f"tmdb.{tid}" for tid in tmdb_ids])
            emby_search_url = f"{emby_host}/emby/Users/{admin_id}/Items?AnyProviderIdEquals={provider_query}&Recursive=true&IncludeItemTypes=Movie,Series&Fields=ProviderIds&api_key={emby_key}"
            try:
                emby_res = requests.get(emby_search_url, timeout=5)
                if emby_res.status_code == 200:
                    for e_item in emby_res.json().get("Items", []):
                        tid = e_item.get("ProviderIds", {}).get("Tmdb")
                        if tid: emby_exists_set.add(str(tid))
            except: pass

        for item in res.get("results", []):
            if item.get("media_type") not in ["movie", "tv"]: continue
            tid_str = str(item.get("id"))
            poster = f"https://image.tmdb.org/t/p/w500{item.get('poster_path')}" if item.get("poster_path") else ""
            results.append({
                "tmdb_id": item.get("id"), "media_type": item.get("media_type"),
                "title": item.get("title") or item.get("name"), "year": (item.get("release_date") or item.get("first_air_date") or "")[:4],
                "poster_path": poster, "backdrop_path": f"https://image.tmdb.org/t/p/w1280{item.get('backdrop_path')}",
                "overview": item.get("overview", ""), "vote_average": round(item.get("vote_average", 0), 1),
                "local_status": 2 if tid_str in emby_exists_set else -1
            })
        return {"status": "success", "data": results}
    except Exception as e: return {"status": "error", "message": f"网络错误: {str(e)}"}

# 🔥 核心升级：提交请求精准匹配 TMDB_ID + SEASON
@router.post("/api/requests/submit")
def submit_media_request(data: MediaRequestSubmitModel, request: Request):
    user = request.session.get("req_user")
    if not user: return {"status": "error", "message": "登录已过期"}

    # 查重：同一部剧的同一季是否已经有人求过了
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("SELECT status FROM media_requests WHERE tmdb_id = ? AND season = ?", (data.tmdb_id, data.season))
    existing = c.fetchone()
    
    if not existing:
        execute_sql("INSERT INTO media_requests (tmdb_id, media_type, title, year, poster_path, status, season) VALUES (?, ?, ?, ?, ?, 0, ?)",
                    (data.tmdb_id, data.media_type, data.title, data.year, data.poster_path, data.season))
    elif existing[0] == 2:
        conn.close(); return {"status": "error", "message": "这一季已经入库啦！"}
    elif existing[0] == 3: # 被拒绝的，允许重启
        execute_sql("UPDATE media_requests SET status = 0, reject_reason = NULL WHERE tmdb_id = ? AND season = ?", (data.tmdb_id, data.season))

    # 记录投票人 (tmdb_id + season + user_id)
    success, err_msg = execute_sql("INSERT INTO request_users (tmdb_id, user_id, username, season) VALUES (?, ?, ?, ?)", 
                                   (data.tmdb_id, user.get("Id"), user.get("Name"), data.season))
    conn.close()
    if not success and "UNIQUE" not in err_msg: return {"status": "error", "message": "你已经支持过这一季啦"}

    type_cn = "🎬 电影" if data.media_type == "movie" else f"📺 剧集 (第 {data.season} 季)"
    bot_msg = (f"🔔 <b>新求片订单提醒</b>\n\n👤 <b>求片人</b>：{user.get('Name')}\n📌 <b>片名</b>：{data.title} ({data.year})\n🏷️ <b>类型</b>：{type_cn}")
    admin_url = cfg.get("pulse_url") or str(request.base_url).rstrip('/')
    keyboard = {"inline_keyboard": [[{"text": "🍿 一键审批", "url": f"{admin_url}/requests_admin"}]]}
    bot.send_photo("sys_notify", data.poster_path or REPORT_COVER_URL, bot_msg, reply_markup=keyboard, platform="all")
    
    return {"status": "success", "message": "求片心愿已精准送达！"}

# 🔥 核心升级：管理动作精准定位到 TMDB_ID + SEASON
@router.post("/api/manage/requests/action")
def manage_request_action(data: AdminActionModel, request: Request):
    if not request.session.get("user"): return {"status": "error", "message": "权限不足"}
    
    conn = sqlite3.connect(DB_PATH); conn.row_factory = sqlite3.Row; c = conn.cursor()
    c.execute("SELECT * FROM media_requests WHERE tmdb_id = ? AND season = ?", (data.tmdb_id, data.season))
    row = c.fetchone(); conn.close()
    if not row: return {"status": "error", "message": "求片记录已失效"}

    new_status = 0
    if data.action == "approve":
        new_status = 1
        mp_url = cfg.get("moviepilot_url"); mp_token = cfg.get("moviepilot_token")
        if mp_url and mp_token:
            try:
                clean_token = mp_token.strip().strip("'").strip('"')
                mp_api = f"{mp_url.rstrip('/')}/api/v1/subscribe/" 
                payload = {"name": row["title"], "tmdbid": int(row["tmdb_id"]), "year": str(row["year"]), "type": "电影" if row["media_type"]=="movie" else "电视剧"}
                if row["media_type"] == "tv": payload["season"] = row["season"] or 1
                
                headers = {"X-API-KEY": clean_token, "Content-Type": "application/json"}
                res = requests.post(mp_api, json=payload, headers=headers, timeout=15)
                if res.status_code != 200: return {"status": "error", "message": f"MP 拒绝: {res.text}"}
            except Exception as e: return {"status": "error", "message": f"连接 MP 异常: {str(e)}"}

    elif data.action == "reject":
        new_status = 3
        execute_sql("UPDATE media_requests SET status = ?, reject_reason = ?, updated_at = CURRENT_TIMESTAMP WHERE tmdb_id = ? AND season = ?", 
                    (new_status, data.reject_reason, data.tmdb_id, data.season))
        return {"status": "success", "message": "已拒绝"}

    elif data.action == "finish": new_status = 2
    elif data.action == "delete":
        execute_sql("DELETE FROM media_requests WHERE tmdb_id = ? AND season = ?", (data.tmdb_id, data.season))
        execute_sql("DELETE FROM request_users WHERE tmdb_id = ? AND season = ?", (data.tmdb_id, data.season))
        return {"status": "success", "message": "已删除"}

    execute_sql("UPDATE media_requests SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE tmdb_id = ? AND season = ?", 
                (new_status, data.tmdb_id, data.season))
    return {"status": "success", "message": "操作成功"}

@router.get("/api/requests/my")
def get_my_requests(request: Request):
    user = request.session.get("req_user")
    if not user: return {"status": "error", "message": "未登录"}
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    # 核心：按用户、ID、季联合查询
    query = """
        SELECT m.tmdb_id, m.title, m.year, m.poster_path, m.status, m.season, m.media_type, r.requested_at, m.reject_reason 
        FROM request_users r 
        JOIN media_requests m ON r.tmdb_id = m.tmdb_id AND r.season = m.season
        WHERE r.user_id = ? ORDER BY r.requested_at DESC
    """
    c.execute(query, (user.get("Id"),)); rows = c.fetchall(); conn.close()
    results = []
    for r in rows:
        title = r[1] + (f" (第 {r[5]} 季)" if r[6]=='tv' else "")
        results.append({"tmdb_id": r[0], "season": r[5], "title": title, "year": r[2], "poster_path": r[3], "status": r[4], "requested_at": r[7], "reject_reason": r[8]})
    return {"status": "success", "data": results}

@router.get("/api/manage/requests")
def get_all_requests(request: Request):
    if not request.session.get("user"): return {"status": "error", "message": "未登录"}
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    # 核心：按 ID + 季 分组统计
    query = """
        SELECT m.tmdb_id, m.media_type, m.title, m.year, m.poster_path, m.status, m.season, m.created_at, 
               COUNT(r.user_id) as cnt, GROUP_CONCAT(r.username, ', ') as users, m.reject_reason
        FROM media_requests m 
        LEFT JOIN request_users r ON m.tmdb_id = r.tmdb_id AND m.season = r.season
        GROUP BY m.tmdb_id, m.season
        ORDER BY m.status ASC, cnt DESC, m.created_at DESC
    """
    c.execute(query); rows = c.fetchall(); conn.close()
    results = []
    for r in rows:
        title = r[2] + (f" (第 {r[6]} 季)" if r[1]=='tv' else "")
        results.append({
            "tmdb_id": r[0], "media_type": r[1], "season": r[6], "title": title, "year": r[3], 
            "poster_path": r[4], "status": r[5], "created_at": r[7], "request_count": r[8], 
            "requested_by": r[9] or "未知", "reject_reason": r[10]
        })
    return {"status": "success", "data": results}