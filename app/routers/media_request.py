from fastapi import APIRouter, Request, Depends
from pydantic import BaseModel
import requests
import sqlite3
import io
import json
from app.core.config import cfg, REPORT_COVER_URL
from app.core.database import DB_PATH
from app.schemas.models import MediaRequestSubmitModel, MediaRequestActionModel
from app.services.bot_service import bot

router = APIRouter()

# ================= 数据库工具函数 =================
def execute_sql(query, params=()):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        c.execute(query, params)
        conn.commit()
        return True, ""
    except Exception as e:
        conn.rollback()
        return False, str(e)
    finally:
        conn.close()

class RequestLoginModel(BaseModel):
    username: str
    password: str

# ================= 用户登录认证 =================
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

# ================= 搜索功能 (TMDB + Emby 穿透查重) =================
@router.get("/api/requests/search")
def search_tmdb(query: str, request: Request):
    if not request.session.get("req_user"): return {"status": "error", "message": "未登录"}
    tmdb_key = cfg.get("tmdb_api_key")
    if not tmdb_key: return {"status": "error", "message": "服主暂未配置 TMDB API Key"}
    proxy = cfg.get("proxy_url"); proxies = {"http": proxy, "https": proxy} if proxy else None

    try:
        url = f"https://api.themoviedb.org/3/search/multi?api_key={tmdb_key}&language=zh-CN&query={query}&page=1"
        res = requests.get(url, proxies=proxies, timeout=10)
        if res.status_code == 200:
            data = res.json()
            results = []
            tmdb_ids = [str(item['id']) for item in data.get("results", []) if item.get("media_type") in ["movie", "tv"]]
            local_status_map = {}
            emby_exists_set = set()

            if tmdb_ids:
                # 1. 查本地库状态
                conn = sqlite3.connect(DB_PATH); c = conn.cursor()
                placeholders = ','.join('?' * len(tmdb_ids))
                c.execute(f"SELECT tmdb_id, status FROM media_requests WHERE tmdb_id IN ({placeholders})", tuple(tmdb_ids))
                for row in c.fetchall(): local_status_map[str(row[0])] = row[1]
                conn.close()

                # 2. 🔥 穿透查询 Emby 媒体库
                emby_host = cfg.get("emby_host"); emby_key = cfg.get("emby_api_key")
                if emby_host and emby_key:
                    provider_query = ",".join([f"tmdb.{tid}" for tid in tmdb_ids])
                    emby_search_url = f"{emby_host}/emby/Items?AnyProviderIdEquals={provider_query}&Recursive=true&IncludeItemTypes=Movie,Series&Fields=ProviderIds&api_key={emby_key}"
                    try:
                        emby_res = requests.get(emby_search_url, timeout=5)
                        if emby_res.status_code == 200:
                            for e_item in emby_res.json().get("Items", []):
                                tid = e_item.get("ProviderIds", {}).get("Tmdb")
                                if tid: emby_exists_set.add(str(tid))
                    except: pass

            for item in data.get("results", []):
                if item.get("media_type") not in ["movie", "tv"]: continue
                tid_str = str(item.get("id"))
                title = item.get("title") or item.get("name")
                year_str = item.get("release_date") or item.get("first_air_date") or ""
                poster = f"https://image.tmdb.org/t/p/w500{item.get('poster_path')}" if item.get("poster_path") else ""
                final_status = 2 if tid_str in emby_exists_set else local_status_map.get(tid_str, -1)

                results.append({
                    "tmdb_id": item.get("id"), "media_type": item.get("media_type"),
                    "title": title, "year": year_str[:4] if year_str else "未知",
                    "poster_path": poster, "overview": item.get("overview", ""),
                    "vote_average": round(item.get("vote_average", 0), 1),
                    "local_status": final_status 
                })
            return {"status": "success", "data": results}
        return {"status": "error", "message": "TMDB API 响应异常"}
    except Exception as e: return {"status": "error", "message": f"网络或代理错误: {str(e)}"}

# ================= 🔥 提交求片 (带代理拉取图，企微100%有封面) =================
@router.post("/api/requests/submit")
def submit_media_request(data: MediaRequestSubmitModel, request: Request):
    user = request.session.get("req_user")
    if not user: return {"status": "error", "message": "登录已过期"}

    # 查重逻辑
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("SELECT status FROM media_requests WHERE tmdb_id = ?", (data.tmdb_id,))
    existing = c.fetchone()
    if not existing:
        execute_sql("INSERT INTO media_requests (tmdb_id, media_type, title, year, poster_path, status) VALUES (?, ?, ?, ?, ?, 0)",
                    (data.tmdb_id, data.media_type, data.title, data.year, data.poster_path))
    elif existing[0] == 2:
        conn.close(); return {"status": "error", "message": "这部片子已经入库啦！"}

    success, err_msg = execute_sql("INSERT INTO request_users (tmdb_id, user_id, username) VALUES (?, ?, ?)",
                                   (data.tmdb_id, user.get("Id"), user.get("Name")))
    conn.close()
    if not success: return {"status": "error", "message": "你已经提交过啦，不用重复点 +1"}

    # --- 准备精美通知 ---
    type_cn = "🎬 电影" if data.media_type == "movie" else "📺 剧集"
    overview_text = data.overview if data.overview else "暂无剧情简介"
    if len(overview_text) > 120: overview_text = overview_text[:115] + "..."

    bot_msg = (f"🔔 <b>新求片订单提醒</b>\n\n"
               f"👤 <b>求片人</b>：{user.get('Name')}\n"
               f"📌 <b>片名</b>：{data.title} ({data.year})\n"
               f"🏷️ <b>类型</b>：{type_cn}\n\n"
               f"📝 <b>剧情简介：</b>\n{overview_text}")
    
    admin_url = cfg.get("pulse_url") or str(request.base_url).rstrip('/')
    keyboard = {"inline_keyboard": [[{"text": "🍿 前往后台一键审批", "url": f"{admin_url}/requests_admin"}]]}
    
    # 🔥 修复企微无图的关键：挂载代理拉取 TMDB 封面转成字节流
    photo_data = REPORT_COVER_URL
    if data.poster_path:
        try:
            proxy = cfg.get("proxy_url"); proxies = {"http": proxy, "https": proxy} if proxy else None
            img_res = requests.get(data.poster_path, proxies=proxies, timeout=15)
            if img_res.status_code == 200: photo_data = io.BytesIO(img_res.content)
        except: pass

    # 传给机器人的 wecom_photo_io 确保企微能收到图
    bot.send_photo("sys_notify", photo_data, bot_msg, reply_markup=keyboard, platform="all", wecom_photo_io=photo_data)
    return {"status": "success", "message": "心愿提交成功！已通知服主处理。"}

# ================= 🔥 管理员审批 (MoviePilot 终极适配版) =================
@router.post("/api/manage/requests/action")
def manage_request_action(data: MediaRequestActionModel, request: Request):
    if not request.session.get("user"): return {"status": "error", "message": "权限不足"}

    new_status = 0
    if data.action == "approve":
        new_status = 1
        mp_url = cfg.get("moviepilot_url")
        mp_token = cfg.get("moviepilot_token")
        
        if mp_url and mp_token:
            # 采用 Row 模式获取数据，确保字段名引用正确
            conn = sqlite3.connect(DB_PATH); conn.row_factory = sqlite3.Row; c = conn.cursor()
            c.execute("SELECT * FROM media_requests WHERE tmdb_id = ?", (data.tmdb_id,))
            row = c.fetchone(); conn.close()
            
            if row:
                try:
                    clean_token = mp_token.strip().strip("'").strip('"')
                    mp_api = f"{mp_url.rstrip('/')}/api/v1/subscribe/" 
                    
                    # 🔥 源码适配：类型必须是中文，tmdbid 必须是 int
                    mp_type_map = {"movie": "电影", "tv": "电视剧"}
                    payload = {
                        "name": row["title"], 
                        "tmdbid": int(row["tmdb_id"]), 
                        "year": str(row["year"]) if row["year"] else "",
                        "type": mp_type_map.get(row["media_type"], "未知"),
                        "season": 1 if row["media_type"] == "tv" else 0
                    }
                    
                    print(f"[MP DEBUG] 发送 Payload: {json.dumps(payload, ensure_ascii=False)}")
                    
                    # 优先使用 X-API-KEY 认证
                    headers = {"X-API-KEY": clean_token, "Content-Type": "application/json"}
                    res = requests.post(mp_api, json=payload, headers=headers, timeout=15)
                    
                    # 备选方案：apikey URL 参数模式
                    if res.status_code != 200:
                        print(f"[MP DEBUG] 第一轮失败({res.status_code})，尝试 apikey 模式...")
                        res = requests.post(f"{mp_api}?apikey={clean_token}", json=payload, headers={"Content-Type": "application/json"}, timeout=15)

                    if res.status_code != 200:
                        return {"status": "error", "message": f"MoviePilot 拒绝: {res.text}"}
                except Exception as e:
                    return {"status": "error", "message": f"连接 MoviePilot 异常: {str(e)}"}

    elif data.action == "reject": new_status = 3
    elif data.action == "finish": new_status = 2
    elif data.action == "delete":
        execute_sql("DELETE FROM media_requests WHERE tmdb_id = ?", (data.tmdb_id,))
        execute_sql("DELETE FROM request_users WHERE tmdb_id = ?", (data.tmdb_id,))
        return {"status": "success", "message": "记录已彻底删除"}

    success, err_msg = execute_sql("UPDATE media_requests SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE tmdb_id = ?", (new_status, data.tmdb_id))
    return {"status": "success", "message": "审批操作成功"} if success else {"status": "error", "message": err_msg}

@router.get("/api/requests/my")
def get_my_requests(request: Request):
    user = request.session.get("req_user")
    if not user: return {"status": "error", "message": "未登录"}
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    query = "SELECT m.tmdb_id, m.title, m.year, m.poster_path, m.status, r.requested_at FROM request_users r JOIN media_requests m ON r.tmdb_id = m.tmdb_id WHERE r.user_id = ? ORDER BY r.requested_at DESC"
    c.execute(query, (user.get("Id"),)); rows = c.fetchall(); conn.close()
    return {"status": "success", "data": [{"tmdb_id": r[0], "title": r[1], "year": r[2], "poster_path": r[3], "status": r[4], "requested_at": r[5]} for r in rows]}

@router.get("/api/manage/requests")
def get_all_requests(request: Request):
    if not request.session.get("user"): return {"status": "error", "message": "未登录"}
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    query = "SELECT m.tmdb_id, m.media_type, m.title, m.year, m.poster_path, m.status, m.created_at, COUNT(r.user_id) as request_count, GROUP_CONCAT(r.username, ', ') as requested_by FROM media_requests m LEFT JOIN request_users r ON m.tmdb_id = r.tmdb_id GROUP BY m.tmdb_id ORDER BY m.status ASC, request_count DESC, m.created_at DESC"
    c.execute(query); rows = c.fetchall(); conn.close()
    return {"status": "success", "data": [{"tmdb_id": r[0], "media_type": r[1], "title": r[2], "year": r[3], "poster_path": r[4], "status": r[5], "created_at": r[6], "request_count": r[7], "requested_by": r[8] or "未知"} for r in rows]}