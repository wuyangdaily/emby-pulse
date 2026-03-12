from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import sqlite3
import requests
from app.core.config import cfg, DB_PATH
from app.services.risk_service import kick_session, ban_user, log_risk_action, get_user_concurrent_limit

router = APIRouter(prefix="/api/risk", tags=["RiskControl"])

class ActionRequest(BaseModel):
    user_id: str
    username: str
    session_id: str = None
    reason: str = "风控系统强制执行"

@router.get("/online")
def get_online_status():
    """获取所有在线用户的风控大盘数据 (供前端卡片渲染)"""
    host = cfg.get("emby_host", "").rstrip('/')
    api_key = cfg.get("emby_api_key", "")
    if not host or not api_key:
        return {"error": "未配置 Emby 服务器信息"}

    try:
        res = requests.get(f"{host}/emby/Sessions", headers={"X-Emby-Token": api_key}, timeout=10)
        if res.status_code != 200:
            return {"error": "无法连接到 Emby"}
        
        sessions = res.json()
        active_users = {}
        
        # 将正在播放的设备按用户归类
        for s in sessions:
            if s.get("NowPlayingItem") and s["NowPlayingItem"].get("MediaType") == "Video":
                uid = s.get("UserId")
                if not uid: continue
                
                if uid not in active_users:
                    # 查水表：获取此人的专属并发额度
                    limit = get_user_concurrent_limit(uid)
                    active_users[uid] = {
                        "user_id": uid,
                        "username": s.get("UserName", "未知"),
                        "limit": limit,
                        "current_count": 0,
                        "is_warning": False,
                        "devices": []
                    }
                
                active_users[uid]["current_count"] += 1
                active_users[uid]["devices"].append({
                    "session_id": s.get("Id"),
                    "device_name": s.get("DeviceName", "未知设备"),
                    "client": s.get("Client", "未知客户端"),
                    "ip": s.get("RemoteEndPoint", "未知IP"),
                    "item_name": s["NowPlayingItem"].get("Name", "未知影片")
                })
        
        # 标记超限的用户
        result_list = []
        for uid, data in active_users.items():
            if data["current_count"] > data["limit"]:
                data["is_warning"] = True
            result_list.append(data)
            
        # 按是否超限排序，把红牌警告的排在最前面
        result_list.sort(key=lambda x: x["is_warning"], reverse=True)
        
        return {"data": result_list}
        
    except Exception as e:
        return {"error": str(e)}

@router.post("/kick")
def api_kick_session(req: ActionRequest):
    """前端一键踢人接口"""
    if not req.session_id:
        raise HTTPException(status_code=400, detail="缺少 Session ID")
        
    if kick_session(req.session_id, req.reason):
        log_risk_action(req.user_id, req.username, "kick", req.reason)
        return {"message": "已成功向违规设备发送强制断开指令"}
    raise HTTPException(status_code=500, detail="踢出失败，可能设备已离线")

@router.post("/ban")
def api_ban_user(req: ActionRequest):
    """前端一键封号接口"""
    if ban_user(req.user_id):
        log_risk_action(req.user_id, req.username, "ban", req.reason)
        return {"message": f"用户 {req.username} 已被关入小黑屋并冻结"}
    raise HTTPException(status_code=500, detail="封禁失败，请检查 API 权限")

@router.get("/logs")
def get_risk_logs():
    """获取风控历史审计日志"""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT * FROM risk_logs ORDER BY created_at DESC LIMIT 100")
        rows = cur.fetchall()
        conn.close()
        return {"data": [dict(r) for r in rows]}
    except:
        return {"data": []}