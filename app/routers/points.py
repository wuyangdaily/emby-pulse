import sqlite3
import datetime
import random
from fastapi import APIRouter, Request
from pydantic import BaseModel
from typing import List, Optional
from app.core.config import cfg, templates
from app.core.database import DB_PATH, query_db
from app.core.media_adapter import media_api
import requests

router = APIRouter()

# --- 🚀 数据库基建：自动扩展积分相关表 ---
def ensure_points_schema():
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        
        c.execute("PRAGMA table_info(users_meta)")
        cols = [col[1] for col in c.fetchall()]
        if 'points' not in cols:
            c.execute("ALTER TABLE users_meta ADD COLUMN points INTEGER DEFAULT 0")
            
        c.execute('''CREATE TABLE IF NOT EXISTS point_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            username TEXT,
            action TEXT,
            amount INTEGER,
            balance INTEGER,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS point_config (
            key TEXT PRIMARY KEY,
            value TEXT
        )''')
        
        c.execute("SELECT count(*) FROM point_config")
        if c.fetchone()[0] == 0:
            defaults = [
                ("enable_points", "1"),         
                ("checkin_min", "10"),          
                ("checkin_max", "30"),          
                ("enable_req_cost", "0"),       
                ("req_cost", "50"),             
                ("enable_renew", "0"),          
                ("renew_cost", "500"),          
                ("renew_days", "30")            
            ]
            c.executemany("INSERT INTO point_config (key, value) VALUES (?, ?)", defaults)
            
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"初始化积分系统数据库失败: {e}")

ensure_points_schema()

class PointConfigModel(BaseModel):
    configs: dict

class BatchPointsModel(BaseModel):
    user_ids: List[str]
    amount: int 
    reason: str

# ==========================================
# B端 (管理员) 积分大盘控制台
# ==========================================
@router.get("/points")
async def points_page(request: Request):
    if not request.session.get("user"):
        return templates.TemplateResponse("login.html", {"request": request})
    return templates.TemplateResponse("points.html", {
        "request": request, 
        "user": request.session.get("user"), 
        "active_page": "points"
    })

@router.get("/api/points/config")
def get_points_config(request: Request):
    if not request.session.get("user"): return {"status": "error"}
    rows = query_db("SELECT key, value FROM point_config")
    config = {r['key']: r['value'] for r in rows} if rows else {}
    return {"status": "success", "data": config}

@router.post("/api/points/config")
def save_points_config(data: PointConfigModel, request: Request):
    if not request.session.get("user"): return {"status": "error"}
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    for k, v in data.configs.items():
        c.execute("INSERT OR REPLACE INTO point_config (key, value) VALUES (?, ?)", (k, str(v)))
    conn.commit()
    conn.close()
    return {"status": "success", "message": "积分经济学参数已更新"}

@router.get("/api/points/users")
def get_users_points(request: Request):
    if not request.session.get("user"): return {"status": "error"}
    try:
        res = media_api.get("/Users", timeout=5)
        if res.status_code != 200: return {"status": "error", "message": "获取 Emby 用户失败"}
        emby_users = res.json()
        
        meta_rows = query_db("SELECT user_id, points FROM users_meta")
        points_map = {r['user_id']: (r['points'] or 0) for r in meta_rows} if meta_rows else {}
        
        results = []
        for u in emby_users:
            uid = u['Id']
            results.append({
                "id": uid,
                "name": u['Name'],
                "points": points_map.get(uid, 0),
                "last_active": u.get("LastActivityDate", "从未活跃")
            })
            
        results.sort(key=lambda x: x['points'], reverse=True)
        return {"status": "success", "data": results}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@router.post("/api/points/batch_update")
def batch_update_points(data: BatchPointsModel, request: Request):
    if not request.session.get("user"): return {"status": "error"}
    if not data.user_ids or data.amount == 0: return {"status": "error", "message": "参数无效"}
        
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        
        res = media_api.get("/Users", timeout=5)
        users = res.json() if res.status_code == 200 else []
        name_map = {u['Id']: u['Name'] for u in users}
        
        success_count = 0
        for uid in data.user_ids:
            uname = name_map.get(uid, "未知用户")
            c.execute("SELECT points FROM users_meta WHERE user_id = ?", (uid,))
            row = c.fetchone()
            
            if row:
                new_points = max(0, (row[0] or 0) + data.amount)
                c.execute("UPDATE users_meta SET points = ? WHERE user_id = ?", (new_points, uid))
            else:
                new_points = max(0, data.amount)
                c.execute("INSERT INTO users_meta (user_id, points) VALUES (?, ?)", (uid, new_points))
                
            c.execute("INSERT INTO point_logs (user_id, username, action, amount, balance) VALUES (?, ?, ?, ?, ?)",
                      (uid, uname, f"管理员批量操作: {data.reason}", data.amount, new_points))
            success_count += 1
            
        conn.commit()
        conn.close()
        return {"status": "success", "message": f"成功修改了 {success_count} 名用户的资产"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ==========================================
# 🔥 C端 (用户大厅) 互动与兑换 API
# ==========================================

@router.get("/api/user/points/info")
def get_user_points_info(request: Request):
    user = request.session.get("req_user")
    if not user: return {"status": "error", "message": "未登录"}
    uid = user['Id']

    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        # 1. 查余额
        c.execute("SELECT points FROM users_meta WHERE user_id = ?", (uid,))
        row = c.fetchone()
        points = row[0] if row else 0

        # 2. 查今日签到防刷状态 (带上 'localtime' 防时区漂移)
        c.execute("SELECT 1 FROM point_logs WHERE user_id = ? AND action = '每日签到' AND date(created_at, 'localtime') = date('now', 'localtime')", (uid,))
        has_checked_in = bool(c.fetchone())
        
        # 3. 拉取商品物价
        c.execute("SELECT key, value FROM point_config")
        config = {r[0]: r[1] for r in c.fetchall()}

        conn.close()
        return {
            "status": "success", 
            "data": {"points": points, "has_checked_in": has_checked_in, "config": config}
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}

@router.post("/api/user/points/checkin")
def user_checkin(request: Request):
    user = request.session.get("req_user")
    if not user: return {"status": "error", "message": "未登录"}
    uid = user['Id']; uname = user['Name']

    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()

        # 再次严格校验防并发连点
        c.execute("SELECT 1 FROM point_logs WHERE user_id = ? AND action = '每日签到' AND date(created_at, 'localtime') = date('now', 'localtime')", (uid,))
        if c.fetchone():
            conn.close()
            return {"status": "error", "message": "今天已经签到过了，明天再来吧！"}

        c.execute("SELECT key, value FROM point_config WHERE key IN ('checkin_min', 'checkin_max')")
        config = {r[0]: int(r[1]) for r in c.fetchall()}
        min_pts = config.get('checkin_min', 10)
        max_pts = config.get('checkin_max', 30)

        # 核心：生成盲盒随机积分
        reward = random.randint(min_pts, max_pts)

        c.execute("SELECT points FROM users_meta WHERE user_id = ?", (uid,))
        row = c.fetchone()
        if row:
            new_points = (row[0] or 0) + reward
            c.execute("UPDATE users_meta SET points = ? WHERE user_id = ?", (new_points, uid))
        else:
            new_points = reward
            c.execute("INSERT INTO users_meta (user_id, points) VALUES (?, ?)", (uid, new_points))

        c.execute("INSERT INTO point_logs (user_id, username, action, amount, balance) VALUES (?, ?, ?, ?, ?)",
                  (uid, uname, "每日签到", reward, new_points))

        conn.commit()
        conn.close()
        return {"status": "success", "message": f"签到成功！运气爆棚抽中 {reward} 积分盲盒", "reward": reward, "balance": new_points}
    except Exception as e:
        return {"status": "error", "message": str(e)}

class RedeemModel(BaseModel):
    item_id: str

@router.post("/api/user/points/redeem")
def user_redeem(data: RedeemModel, request: Request):
    user = request.session.get("req_user")
    if not user: return {"status": "error", "message": "未登录"}
    uid = user['Id']; uname = user['Name']

    if data.item_id != 'renew':
        return {"status": "error", "message": "无效的商品"}

    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()

        c.execute("SELECT key, value FROM point_config WHERE key IN ('enable_renew', 'renew_cost', 'renew_days')")
        config = {r[0]: r[1] for r in c.fetchall()}
        
        if config.get('enable_renew') != "1":
            conn.close()
            return {"status": "error", "message": "管理员暂未开启自助续期服务"}

        cost = int(config.get('renew_cost', 500))
        days = int(config.get('renew_days', 30))

        c.execute("SELECT points FROM users_meta WHERE user_id = ?", (uid,))
        row = c.fetchone()
        current_points = row[0] if row else 0

        # 🔥 检查钱包够不够
        if current_points < cost:
            conn.close()
            return {"status": "error", "message": f"余额不足！续期需要 {cost} 积分，您当前仅有 {current_points} 积分。"}

        new_points = current_points - cost
        c.execute("UPDATE users_meta SET points = ? WHERE user_id = ?", (new_points, uid))

        # 🔥 核心：增加账号寿命
        c.execute("SELECT expire_date FROM users_meta WHERE user_id = ?", (uid,))
        exp_row = c.fetchone()
        current_exp = exp_row[0] if exp_row and exp_row[0] else None

        today = datetime.date.today()
        if current_exp:
            try:
                exp_date = datetime.datetime.strptime(current_exp, "%Y-%m-%d").date()
                if exp_date < today: exp_date = today  # 如果已经过期，直接从今天起算叠加
            except: exp_date = today
        else:
            exp_date = today

        new_exp_date = exp_date + datetime.timedelta(days=days)
        new_exp_str = new_exp_date.strftime("%Y-%m-%d")

        c.execute("UPDATE users_meta SET expire_date = ? WHERE user_id = ?", (new_exp_str, uid))

        c.execute("INSERT INTO point_logs (user_id, username, action, amount, balance) VALUES (?, ?, ?, ?, ?)",
                  (uid, uname, f"商城兑换: 账号续期 {days} 天", -cost, new_points))

        conn.commit()
        conn.close()
        
        # 兜底：请求 Emby 接口解除账号禁用状态（防万一他已经到期被封号了）
        try:
            host = cfg.get("emby_host"); key = cfg.get("emby_api_key")
            requests.post(f"{host}/emby/Users/{uid}/Policy?api_key={key}", json={"IsDisabled": False}, timeout=3)
        except: pass

        return {"status": "success", "message": f"兑换成功！账号有效期已成功延长至 {new_exp_str}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}