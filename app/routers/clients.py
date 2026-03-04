import sqlite3
import requests
import datetime
from fastapi import APIRouter, Request
from pydantic import BaseModel
from app.core.config import cfg
from app.core.database import DB_PATH, query_db

router = APIRouter()

# ==========================================
# 🔥 数据库热升级：确保黑名单表存在
# ==========================================
def ensure_blacklist_schema():
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS client_blacklist (
                        app_name TEXT PRIMARY KEY,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )''')
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Upgrade blacklist table error: {e}")

ensure_blacklist_schema()

class BlacklistModel(BaseModel):
    app_name: str

# 1. 获取黑名单列表
@router.get("/api/clients/blacklist")
async def get_blacklist():
    rows = query_db("SELECT * FROM client_blacklist ORDER BY created_at DESC")
    return {"status": "success", "data": [dict(r) for r in rows] if rows else []}

# 2. 添加黑名单
@router.post("/api/clients/blacklist")
async def add_blacklist(data: BlacklistModel):
    app_name = data.app_name.strip()
    if not app_name: 
        return {"status": "error", "message": "软件名不能为空"}
    try:
        query_db("INSERT INTO client_blacklist (app_name) VALUES (?)", (app_name,))
        return {"status": "success"}
    except:
        return {"status": "error", "message": f"[{app_name}] 已存在于黑名单中"}

# 3. 移除黑名单
@router.delete("/api/clients/blacklist/{app_name}")
async def delete_blacklist(app_name: str):
    query_db("DELETE FROM client_blacklist WHERE app_name = ?", (app_name,))
    return {"status": "success"}

# 4. 获取图表分析与全服设备数据
@router.get("/api/clients/data")
async def get_clients_data(request: Request):
    if not request.session.get("user"): return {"status": "error", "message": "鉴权失败"}
    
    host = cfg.get("emby_host")
    key = cfg.get("emby_api_key")
    if not host or not key:
        return {"status": "error", "message": "Emby 配置未完成，请检查 config.yaml"}

    try:
        # 获取所有设备
        res = requests.get(f"{host}/emby/Devices?api_key={key}", timeout=5)
        devices = res.json().get("Items", [])
        
        # 获取当前活跃会话 (判断是否在线)
        sess_res = requests.get(f"{host}/emby/Sessions?api_key={key}", timeout=5)
        active_device_ids = [s.get("DeviceId") for s in sess_res.json()]
    except Exception as e:
        return {"status": "error", "message": f"连接 Emby 失败: {str(e)}"}

    # 分析图表数据：优先从本地播放记录获取，如果为空则使用设备列表数据保底
    app_counts = {}
    top_devices = {}
    
    try:
        pie_rows = query_db("SELECT Client, COUNT(*) as cnt FROM PlaybackActivity GROUP BY Client")
        if pie_rows:
            app_counts = {r['Client']: r['cnt'] for r in pie_rows}
            
        bar_rows = query_db("SELECT DeviceName, COUNT(*) as cnt FROM PlaybackActivity GROUP BY DeviceName ORDER BY cnt DESC LIMIT 5")
        if bar_rows:
            top_devices = {r['DeviceName']: r['cnt'] for r in bar_rows}
    except: pass

    # 保底机制
    if not app_counts:
        for d in devices:
            an = d.get("AppName", "未知客户端")
            app_counts[an] = app_counts.get(an, 0) + 1
            
    if not top_devices:
        sorted_devs = sorted(devices, key=lambda x: x.get("DateLastActivity", ""), reverse=True)[:5]
        top_devices = {d.get("Name", "未知设备"): 1 for d in sorted_devs}

    # 读取黑名单比对
    blacklist_rows = query_db("SELECT app_name FROM client_blacklist")
    blacklist = [r['app_name'].lower() for r in blacklist_rows] if blacklist_rows else []

    table_data = []
    for d in devices:
        app_name = d.get("AppName", "未知")
        is_blocked = app_name.lower() in blacklist
        date_str = d.get("DateLastActivity", "")
        last_active = date_str.replace("T", " ").split(".")[0] if date_str else "从未连接"
        
        table_data.append({
            "id": d.get("Id"),
            "name": d.get("Name", "未知设备"),
            "app_name": app_name,
            "last_activity": last_active,
            "is_active": d.get("Id") in active_device_ids,
            "is_blocked": is_blocked
        })

    # 按活动时间倒序
    table_data.sort(key=lambda x: x["last_activity"], reverse=True)

    return {
        "status": "success",
        "charts": {
            "pie": {"labels": list(app_counts.keys()), "data": list(app_counts.values())},
            "bar": {"labels": list(top_devices.keys()), "data": list(top_devices.values())}
        },
        "devices": table_data
    }

# 5. 🔥 核心：执行黑名单拦截 (删除 Emby 原生设备授权)
@router.post("/api/clients/execute_block")
async def execute_block():
    host = cfg.get("emby_host")
    key = cfg.get("emby_api_key")
    
    blacklist_rows = query_db("SELECT app_name FROM client_blacklist")
    if not blacklist_rows: 
        return {"status": "success", "message": "当前黑名单为空，无设备被阻断"}
    blacklist = [r['app_name'].lower() for r in blacklist_rows]
    
    blocked_count = 0
    try:
        res = requests.get(f"{host}/emby/Devices?api_key={key}", timeout=5)
        devices = res.json().get("Items", [])
        
        for d in devices:
            app_name = d.get("AppName", "").lower()
            if app_name in blacklist:
                # 吊销授权并删除设备记录，下次连接需重新登录
                requests.delete(f"{host}/emby/Devices?Id={d['Id']}&api_key={key}", timeout=2)
                blocked_count += 1
                
        return {"status": "success", "message": f"扫描完成！成功强制注销了 {blocked_count} 个违规设备。"}
    except Exception as e:
        return {"status": "error", "message": f"执行阻断失败: {str(e)}"}