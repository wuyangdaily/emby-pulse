import sqlite3
import datetime
import random
import json
from fastapi import APIRouter, Request
from pydantic import BaseModel
from typing import List, Optional
from app.core.config import cfg, templates
from app.core.database import DB_PATH, query_db
from app.core.media_adapter import media_api

router = APIRouter()

# --- 🚀 数据库基建 ---
def ensure_points_schema():
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("PRAGMA table_info(users_meta)")
        cols = [col[1] for col in c.fetchall()]
        if 'points' not in cols: c.execute("ALTER TABLE users_meta ADD COLUMN points INTEGER DEFAULT 0")
            
        c.execute('''CREATE TABLE IF NOT EXISTS point_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, username TEXT, action TEXT,
            amount INTEGER, balance INTEGER, created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS point_config (key TEXT PRIMARY KEY, value TEXT)''')
        
        c.execute("SELECT count(*) FROM point_config")
        if c.fetchone()[0] == 0:
            default_store = [
                {"id": "renew_30", "type": "renew", "name": "账号续期 30 天", "cost": 500, "val": 30, "icon": "fa-battery-half", "color": "text-emerald-500", "desc": "延长一个月欢乐时光"},
                {"id": "invite_code", "type": "manual", "name": "购买一枚邀请码", "cost": 2000, "icon": "fa-ticket", "color": "text-amber-500", "desc": "兑换后请凭截图联系服主发放"}
            ]
            defaults = [
                ("enable_points", "1"), ("checkin_min", "10"), ("checkin_max", "30"),          
                ("enable_req_cost", "0"), ("req_cost", "50"), ("store_items", json.dumps(default_store, ensure_ascii=False))            
            ]
            c.executemany("INSERT INTO point_config (key, value) VALUES (?, ?)", defaults)
            
        conn.commit(); conn.close()
    except Exception as e: print(f"初始化积分系统数据库失败: {e}")

ensure_points_schema()

class PointConfigModel(BaseModel): configs: dict
class BatchPointsModel(BaseModel): user_ids: List[str]; amount: int; reason: str

@router.get("/points")
async def points_page(request: Request):
    if not request.session.get("user"): return templates.TemplateResponse("login.html", {"request": request})
    return templates.TemplateResponse("points.html", {"request": request, "user": request.session.get("user"), "active_page": "points"})

@router.get("/api/points/config")
def get_points_config(request: Request):
    if not request.session.get("user"): return {"status": "error"}
    rows = query_db("SELECT key, value FROM point_config")
    config = {r['key']: r['value'] for r in rows} if rows else {}
    return {"status": "success", "data": config}

@router.post("/api/points/config")
async def save_points_config(request: Request):
    if not request.session.get("user"): return {"status": "error"}
    data = await request.json()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    for k, v in data.get('configs', {}).items():
        if isinstance(v, (dict, list)): v = json.dumps(v, ensure_ascii=False)
        c.execute("INSERT OR REPLACE INTO point_config (key, value) VALUES (?, ?)", (k, str(v)))
    conn.commit(); conn.close()
    return {"status": "success", "message": "积分经济学参数已更新"}

@router.get("/api/points/users")
def get_users_points(request: Request):
    if not request.session.get("user"): return {"status": "error"}
    try:
        emby_users = media_api.get("/Users", timeout=5).json()
        meta_rows = query_db("SELECT user_id, points FROM users_meta")
        points_map = {r['user_id']: (r['points'] or 0) for r in meta_rows} if meta_rows else {}
        results = [{"id": u['Id'], "name": u['Name'], "points": points_map.get(u['Id'], 0), "last_active": u.get("LastActivityDate", "从未活跃")} for u in emby_users]
        results.sort(key=lambda x: x['points'], reverse=True)
        return {"status": "success", "data": results}
    except Exception as e: return {"status": "error", "message": str(e)}

@router.post("/api/points/batch_update")
def batch_update_points(data: BatchPointsModel, request: Request):
    if not request.session.get("user"): return {"status": "error"}
    try:
        conn = sqlite3.connect(DB_PATH); c = conn.cursor()
        users = media_api.get("/Users", timeout=5).json()
        name_map = {u['Id']: u['Name'] for u in users}
        count = 0
        for uid in data.user_ids:
            c.execute("SELECT points FROM users_meta WHERE user_id = ?", (uid,))
            row = c.fetchone()
            new_pts = max(0, (row[0] or 0) + data.amount) if row else max(0, data.amount)
            if row: c.execute("UPDATE users_meta SET points = ? WHERE user_id = ?", (new_pts, uid))
            else: c.execute("INSERT INTO users_meta (user_id, points) VALUES (?, ?)", (uid, new_pts))
            c.execute("INSERT INTO point_logs (user_id, username, action, amount, balance) VALUES (?, ?, ?, ?, ?)", (uid, name_map.get(uid, "未知用户"), f"管理员操作: {data.reason}", data.amount, new_pts))
            count += 1
        conn.commit(); conn.close()
        return {"status": "success", "message": f"成功修改了 {count} 名用户的资产"}
    except Exception as e: return {"status": "error", "message": str(e)}

@router.get("/api/points/logs")
def get_point_logs(request: Request, user_id: str = None):
    if not request.session.get("user"): return {"status": "error"}
    try:
        conn = sqlite3.connect(DB_PATH); c = conn.cursor()
        if user_id: c.execute("SELECT * FROM point_logs WHERE user_id = ? ORDER BY created_at DESC LIMIT 100", (user_id,))
        else: c.execute("SELECT * FROM point_logs ORDER BY created_at DESC LIMIT 100")
        
        cols = [desc[0] for desc in c.description]
        logs = [dict(zip(cols, row)) for row in c.fetchall()]
        conn.close()
        return {"status": "success", "data": logs}
    except Exception as e: return {"status": "error", "message": str(e)}

# ==========================================
# C端 API
# ==========================================
@router.get("/api/user/points/info")
def get_user_points_info(request: Request):
    user = request.session.get("req_user")
    if not user: return {"status": "error", "message": "未登录"}
    try:
        conn = sqlite3.connect(DB_PATH); c = conn.cursor()
        row = c.execute("SELECT points FROM users_meta WHERE user_id = ?", (user['Id'],)).fetchone()
        points = row[0] if row else 0
        has_checked_in = bool(c.execute("SELECT 1 FROM point_logs WHERE user_id = ? AND action = '每日签到' AND date(created_at, 'localtime') = date('now', 'localtime')", (user['Id'],)).fetchone())
        config = {r[0]: r[1] for r in c.execute("SELECT key, value FROM point_config").fetchall()}
        
        try: store_items = json.loads(config.get('store_items', '[]'))
        except: store_items = []
        config['store_items'] = store_items

        conn.close()
        return {"status": "success", "data": {"points": points, "has_checked_in": has_checked_in, "config": config}}
    except Exception as e: return {"status": "error", "message": str(e)}

@router.post("/api/user/points/checkin")
def user_checkin(request: Request):
    user = request.session.get("req_user")
    if not user: return {"status": "error", "message": "未登录"}
    try:
        conn = sqlite3.connect(DB_PATH); c = conn.cursor()
        if c.execute("SELECT 1 FROM point_logs WHERE user_id = ? AND action = '每日签到' AND date(created_at, 'localtime') = date('now', 'localtime')", (user['Id'],)).fetchone():
            conn.close(); return {"status": "error", "message": "今天已经签到过了，明天再来吧！"}

        config = {r[0]: int(r[1]) for r in c.execute("SELECT key, value FROM point_config WHERE key IN ('checkin_min', 'checkin_max')").fetchall()}
        reward = random.randint(config.get('checkin_min', 10), config.get('checkin_max', 30))

        row = c.execute("SELECT points FROM users_meta WHERE user_id = ?", (user['Id'],)).fetchone()
        new_points = (row[0] or 0) + reward if row else reward
        if row: c.execute("UPDATE users_meta SET points = ? WHERE user_id = ?", (new_points, user['Id']))
        else: c.execute("INSERT INTO users_meta (user_id, points) VALUES (?, ?)", (user['Id'], new_points))

        c.execute("INSERT INTO point_logs (user_id, username, action, amount, balance) VALUES (?, ?, ?, ?, ?)", (user['Id'], user['Name'], "每日签到", reward, new_points))
        conn.commit(); conn.close()
        return {"status": "success", "message": f"签到成功！抽中 {reward} 积分", "reward": reward, "balance": new_points}
    except Exception as e: return {"status": "error", "message": str(e)}

class RedeemModel(BaseModel): item_id: str

@router.post("/api/user/points/redeem")
def user_redeem(data: RedeemModel, request: Request):
    user = request.session.get("req_user")
    if not user: return {"status": "error"}
    try:
        conn = sqlite3.connect(DB_PATH); c = conn.cursor()
        config = {r[0]: r[1] for r in c.execute("SELECT key, value FROM point_config").fetchall()}
        try: store_items = json.loads(config.get('store_items', '[]'))
        except: store_items = []
        
        target_item = next((x for x in store_items if x.get("id") == data.item_id), None)
        if not target_item: conn.close(); return {"status": "error", "message": "商品不存在或已下架"}

        cost = int(target_item.get('cost', 0))
        row = c.execute("SELECT points FROM users_meta WHERE user_id = ?", (user['Id'],)).fetchone()
        current_points = row[0] if row else 0

        if current_points < cost: conn.close(); return {"status": "error", "message": f"余额不足！需要 {cost} 积分。"}

        exp_row = c.execute("SELECT expire_date FROM users_meta WHERE user_id = ?", (user['Id'],)).fetchone()
        current_exp = exp_row[0] if exp_row else None
        
        if target_item.get("type") == "renew":
            if not current_exp or current_exp == "" or "2099" in current_exp or "3000" in current_exp or "永久" in current_exp:
                conn.close(); return {"status": "error", "message": "您的账号当前为【永久有效】，无需兑换续期！"}

        new_points = current_points - cost
        c.execute("UPDATE users_meta SET points = ? WHERE user_id = ?", (new_points, user['Id']))

        if target_item.get("type") == "renew":
            days = int(target_item.get("val", 30))
            today = datetime.date.today()
            try:
                exp_date = datetime.datetime.strptime(current_exp, "%Y-%m-%d").date()
                if exp_date < today: exp_date = today
            except: exp_date = today

            new_exp_date = exp_date + datetime.timedelta(days=days)
            new_exp_str = new_exp_date.strftime("%Y-%m-%d")
            c.execute("UPDATE users_meta SET expire_date = ? WHERE user_id = ?", (new_exp_str, user['Id']))
            action_desc = f"商城兑换: {target_item.get('name')} (至 {new_exp_str})"
            try: requests.post(f"{cfg.get('emby_host')}/emby/Users/{user['Id']}/Policy?api_key={cfg.get('emby_api_key')}", json={"IsDisabled": False}, timeout=3)
            except: pass
        else: action_desc = f"商城兑换: {target_item.get('name')}"

        c.execute("INSERT INTO point_logs (user_id, username, action, amount, balance) VALUES (?, ?, ?, ?, ?)", (user['Id'], user['Name'], action_desc, -cost, new_points))
        conn.commit(); conn.close()
        return {"status": "success", "message": f"兑换成功！{target_item.get('name')}已生效或已记录。"}
    except Exception as e: return {"status": "error", "message": str(e)}