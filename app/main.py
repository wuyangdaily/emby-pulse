import os
import asyncio
import threading
import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware

from app.routers import insight
from app.core.config import PORT, SECRET_KEY, CONFIG_DIR, FONT_DIR
from app.core.database import init_db
from app.services.bot_service import bot
from app.routers import media_request
# 🔥 引入所有路由
from app.routers import views, auth, users, stats, bot as bot_router, system, proxy, report, webhook, insight, tasks, history, calendar, search, clients, gaps

# 初始化目录和数据库
if not os.path.exists("static"): os.makedirs("static")
if not os.path.exists("templates"): os.makedirs("templates")
if not os.path.exists(CONFIG_DIR): os.makedirs(CONFIG_DIR)
if not os.path.exists(FONT_DIR): os.makedirs(FONT_DIR)
init_db()

# ==============================================================================
# 🔥 黑客级双开引擎：在子线程强行拉起 10308 (无视 Docker 限制与多进程互殴)
# ==============================================================================
def start_user_portal():
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        # log_level="critical" 屏蔽多余日志，防止刷屏
        config = uvicorn.Config(app, host="0.0.0.0", port=10308, log_level="critical")
        server = uvicorn.Server(config)
        
        # 核心防崩溃：禁止子线程劫持系统信号，否则会引发主进程连环爆炸
        server.install_signal_handlers = lambda: None 
        
        print("🎈 [User Portal] 求片中心专属端口 10308 已就绪！")
        loop.run_until_complete(server.serve())
    except OSError as e:
        # 如果是多个 Worker 抢端口导致的 [Errno 98] 或 [Errno 48]，静默忽略
        pass
    except Exception:
        pass

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("🚀 Starting EmbyPulse...")
    bot.start()
    
    # 🌟 在应用启动时，偷偷开个线程把 10308 端口跑起来
    threading.Thread(target=start_user_portal, daemon=True).start()
    
    yield
    print("🛑 Stopping EmbyPulse...")
    bot.stop()

app = FastAPI(lifespan=lifespan)

# ==============================================================================
# 🔥 核心防御：10308 全环境穿透分流器 (完美支持 Host / 桥接 / Nginx 反代)
# ==============================================================================
@app.middleware("http")
async def port_10308_dispatcher(request: Request, call_next):
    # 1. 物理端口：针对 Host 模式，直接读取服务器底层 Socket 接收的实际物理端口
    server_tuple = request.scope.get("server")
    physical_port = server_tuple[1] if server_tuple else 0
    
    # 2. 逻辑端口：针对 Bridge 模式映射 (-p 10308:10307) 或反代，解析请求环境
    logical_port = request.url.port
    
    # 铁律：只要物理端口或请求头里的逻辑端口是 10308，全部打入普通用户通道！
    if physical_port == 10308 or logical_port == 10308:
        path = request.url.path
        
        # 隐形重写：访问根目录直接送去求片中心
        if path == "/":
            request.scope["path"] = "/request"
            
        # 物理隔绝：只放行普通用户必须的路径，其余全部拉黑
        allowed_prefixes = (
            "/request", "/request_login", 
            "/api/v1/request", "/api/proxy/smart_image", 
            "/static", "/favicon.ico"
        )
        if not request.scope["path"].startswith(allowed_prefixes):
            return HTMLResponse("<h1>404 Not Found</h1><p>Access Denied.</p>", status_code=404)
            
    return await call_next(request)
# ==============================================================================

# 中间件
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, max_age=86400*7)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

# 静态文件
app.mount("/static", StaticFiles(directory="static"), name="static")

# 注册路由
app.include_router(views.router)
app.include_router(auth.router)
app.include_router(users.router)
app.include_router(stats.router)
app.include_router(bot_router.router)
app.include_router(system.router)
app.include_router(proxy.router)
app.include_router(report.router)
app.include_router(insight.router)
app.include_router(webhook.router)
app.include_router(tasks.router)
app.include_router(history.router)
app.include_router(calendar.router)
app.include_router(media_request.router)
app.include_router(search.router)
app.include_router(clients.router)
app.include_router(gaps.router)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)