from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from contextlib import asynccontextmanager
import os
from app.routers import insight

from app.core.config import PORT, SECRET_KEY, CONFIG_DIR, FONT_DIR
from app.core.database import init_db
from app.services.bot_service import bot
from app.routers import media_request
# 🔥 引入新路由 webhook
from app.routers import views, auth, users, stats, bot as bot_router, system, proxy, report, webhook,insight,tasks,history,calendar,search,clients

# 初始化目录和数据库
if not os.path.exists("static"): os.makedirs("static")
if not os.path.exists("templates"): os.makedirs("templates")
if not os.path.exists(CONFIG_DIR): os.makedirs(CONFIG_DIR)
if not os.path.exists(FONT_DIR): os.makedirs(FONT_DIR)
init_db()

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("🚀 Starting EmbyPulse...")
    bot.start()
    yield
    print("🛑 Stopping EmbyPulse...")
    bot.stop()

app = FastAPI(lifespan=lifespan)

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
# 🔥 注册 webhook 路由
app.include_router(webhook.router)
# 🔥 注册 tasks 路由
app.include_router(tasks.router)
app.include_router(history.router)
# 注册 calendar 路由
app.include_router(calendar.router)
app.include_router(media_request.router)
app.include_router(search.router)
app.include_router(clients.router)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)