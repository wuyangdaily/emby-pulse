import os
import requests
import json
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from app.core.config import cfg
from app.core.database import query_db
import logging
import random

logger = logging.getLogger("uvicorn")
templates = Jinja2Templates(directory="templates")
router = APIRouter()

APP_VERSION = os.environ.get("APP_VERSION", "1.2.0.Dev")

def check_login(request: Request):
    user = request.session.get("user")
    if user and user.get("is_admin"): return True
    return False

def get_common_vars(request: Request, active_page: str, extra_vars: dict = None):
    raw_url = cfg.get("emby_public_url") or cfg.get("emby_public_host") or cfg.get("emby_host") or ""
    emby_url = raw_url
    try:
        routes = json.loads(raw_url)
        if isinstance(routes, list) and len(routes) > 0:
            emby_url = routes[0].get("url", "")
            for r in routes:
                if r.get("is_main"): 
                    emby_url = r.get("url", "")
                    break
    except Exception:
        pass
        
    emby_url = emby_url.strip().rstrip('/')
    
    server_id = ""
    try:
        sys_res = requests.get(f"{cfg.get('emby_host')}/emby/System/Info?api_key={cfg.get('emby_api_key')}", timeout=2)
        if sys_res.status_code == 200: 
            raw_id = sys_res.json().get("Id", "")
            # 🔥 核心防线：强制剥离所有换行、回车及首尾空白字符
            if raw_id:
                server_id = str(raw_id).replace('\r', '').replace('\n', '').strip()
    except: pass

    vars_dict = {
        "request": request,
        "version": APP_VERSION,
        "active_page": active_page,
        "emby_url": emby_url,
        "server_id": server_id
    }
    if extra_vars: vars_dict.update(extra_vars)
    return vars_dict

@router.get("/apple-touch-icon.png")
@router.get("/apple-touch-icon-precomposed.png")
async def get_apple_touch_icon():
    icon_path = os.path.join("static", "img", "logo-app.png")
    if os.path.exists(icon_path): return FileResponse(icon_path)
    return RedirectResponse("/static/img/logo-light.png")

@router.get("/favicon.ico")
async def get_favicon():
    icon_path = os.path.join("static", "img", "logo-app.png")
    return FileResponse(icon_path)

@router.get("/manifest.json")
async def get_manifest():
    return JSONResponse({
        "name": "EmbyPulse 映迹",
        "short_name": "EmbyPulse",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#ffffff",
        "theme_color": "#4f46e5",
        "icons": [{"src": "/static/img/logo-app.png", "sizes": "180x180", "type": "image/png"}, {"src": "/static/img/logo-app.png", "sizes": "512x512", "type": "image/png"}]
    })

from fastapi.responses import PlainTextResponse

@router.get("/request_manifest.json")
async def get_request_manifest():
    return JSONResponse({
        "name": "用户中心 - EmbyPulse",
        "short_name": "用户中心",
        "start_url": "/request",
        "display": "standalone",
        "background_color": "#f8fafc",
        "theme_color": "#4f46e5",
        "icons": [{"src": "/static/img/logo-app.png", "sizes": "192x192", "type": "image/png"}, {"src": "/static/img/logo-app.png", "sizes": "512x512", "type": "image/png"}]
    })

@router.get("/sw.js")
async def get_service_worker():
    sw_content = "const CACHE_NAME='pulse-user-v1'; self.addEventListener('install', (e)=>{self.skipWaiting();}); self.addEventListener('activate', (e)=>{e.waitUntil(clients.claim());}); self.addEventListener('fetch', (e)=>{e.respondWith(fetch(e.request));});"
    return PlainTextResponse(content=sw_content, media_type="application/javascript")

@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    if not check_login(request): return RedirectResponse("/login")
    return templates.TemplateResponse("index.html", get_common_vars(request, "dashboard"))

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if check_login(request): return RedirectResponse("/")
    return templates.TemplateResponse("login.html", {"request": request, "version": APP_VERSION})

@router.get("/invite/{code}", response_class=HTMLResponse)
async def invite_page(code: str, request: Request):
    invite = query_db("SELECT * FROM invitations WHERE code = ?", (code,), one=True)
    valid = False; days = 0
    if invite and invite['used_count'] < invite['max_uses']: valid = True; days = invite['days']
    client_url = cfg.get("client_download_url") or "https://emby.media/download.html"
    return templates.TemplateResponse("register.html", {"request": request, "code": code, "valid": valid, "days": days, "client_download_url": client_url, "version": APP_VERSION})

@router.get("/content", response_class=HTMLResponse)
async def content_page(request: Request):
    if not check_login(request): return RedirectResponse("/login")
    return templates.TemplateResponse("content.html", get_common_vars(request, "content"))

@router.get("/details", response_class=HTMLResponse)
async def details_page(request: Request):
    if not check_login(request): return RedirectResponse("/login")
    return templates.TemplateResponse("details.html", get_common_vars(request, "details"))

@router.get("/report", response_class=HTMLResponse)
async def report_page(request: Request):
    if not check_login(request): return RedirectResponse("/login")
    return templates.TemplateResponse("report.html", get_common_vars(request, "report"))

@router.get("/bot", response_class=HTMLResponse)
async def bot_page(request: Request):
    if not check_login(request): return RedirectResponse("/login")
    return templates.TemplateResponse("bot.html", get_common_vars(request, "bot"))

@router.get("/users_manage", response_class=HTMLResponse)
@router.get("/users", response_class=HTMLResponse)
async def users_page(request: Request):
    if not check_login(request): return RedirectResponse("/login")
    return templates.TemplateResponse("users.html", get_common_vars(request, "users"))

@router.get("/settings", response_class=HTMLResponse)
@router.get("/system", response_class=HTMLResponse)
async def system_page(request: Request):
    if not check_login(request): return RedirectResponse("/login")
    return templates.TemplateResponse("settings.html", get_common_vars(request, "settings"))

@router.get("/insight", response_class=HTMLResponse)
async def insight_page(request: Request):
    if not check_login(request): return RedirectResponse("/login")
    return templates.TemplateResponse("insight.html", get_common_vars(request, "insight"))

@router.get("/tasks", response_class=HTMLResponse)
async def tasks_page(request: Request):
    if not check_login(request): return RedirectResponse("/login")
    return templates.TemplateResponse("tasks.html", get_common_vars(request, "tasks"))

@router.get("/history", response_class=HTMLResponse)
async def history_page(request: Request):
    user = request.session.get("user")
    if not user: return RedirectResponse(url="/login", status_code=303)
    return templates.TemplateResponse("history.html", get_common_vars(request, "history", {"user": user}))

@router.get("/request", response_class=HTMLResponse)
async def request_page(request: Request):
    req_user = request.session.get("req_user")
    return templates.TemplateResponse("request.html", {"request": request, "req_user": req_user, "version": APP_VERSION})

@router.get("/request_login", response_class=HTMLResponse)
async def request_login_page(request: Request):
    if request.session.get("req_user"): return RedirectResponse("/request")
    return templates.TemplateResponse("request_login.html", {"request": request, "version": APP_VERSION})

@router.get("/requests_admin", response_class=HTMLResponse)
async def requests_admin_page(request: Request):
    if not check_login(request): return RedirectResponse("/login")
    return templates.TemplateResponse("requests_admin.html", get_common_vars(request, "requests_admin"))

@router.get("/clients", response_class=HTMLResponse)
async def clients_page(request: Request):
    if not check_login(request): return RedirectResponse("/login")
    return templates.TemplateResponse("clients.html", get_common_vars(request, "clients"))

@router.get("/about", response_class=HTMLResponse)
async def about_page(request: Request):
    if not check_login(request): return RedirectResponse("/login")
    return templates.TemplateResponse("about.html", get_common_vars(request, "about"))

@router.get("/gaps", response_class=HTMLResponse)
async def gaps_page(request: Request):
    if not check_login(request): return RedirectResponse("/login")
    return templates.TemplateResponse("gaps.html", get_common_vars(request, "gaps"))

@router.get("/risk", response_class=HTMLResponse)
async def risk_control_page(request: Request):
    return templates.TemplateResponse("risk.html", get_common_vars(request, "risk", {"title": "风险管控中心"}))

@router.get("/api/wallpaper")
async def get_wallpaper():
    fallback_wallpapers = [
        {"url": "https://images.unsplash.com/photo-1536440136628-849c177e76a1?q=80&w=1925&auto=format&fit=crop", "title": "电影之夜 - Unsplash"},
        {"url": "https://images.unsplash.com/photo-1489599849927-2ee91cede3ba?q=80&w=2070&auto=format&fit=crop", "title": "家庭影院 - Unsplash"}
    ]
    tmdb_key = cfg.get("tmdb_api_key"); proxy = cfg.get("proxy_url"); proxies = {"https": proxy, "http": proxy} if proxy else None
    if tmdb_key:
        try:
            res = requests.get(f"https://api.themoviedb.org/3/trending/all/day?api_key={tmdb_key}&language=zh-CN", proxies=proxies, timeout=3)
            if res.status_code == 200:
                valid_items = [item for item in res.json().get("results", []) if item.get("backdrop_path")]
                if valid_items:
                    item = random.choice(valid_items)
                    title = item.get("title") or item.get("name") or "TMDB 热门"
                    url = f"https://image.tmdb.org/t/p/original{item['backdrop_path']}"
                    return {"status": "success", "url": url, "title": f"今日热门: {title}"}
        except: pass
    item = random.choice(fallback_wallpapers)
    return {"status": "success", "url": item["url"], "title": item["title"]}

@router.get("/dedupe", response_class=HTMLResponse)
async def dedupe_page(request: Request):
    if not check_login(request): return RedirectResponse("/login")
    return templates.TemplateResponse("dedupe.html", get_common_vars(request, "dedupe"))