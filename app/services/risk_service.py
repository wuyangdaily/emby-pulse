import sqlite3
import requests
import logging
import time
import threading
from app.core.config import cfg, DB_PATH
from app.core.event_bus import bus

logger = logging.getLogger("uvicorn")

# ==========================================
# 🗡️ 屠龙刀：强力执法接口
# ==========================================
def kick_session(session_id: str, reason: str = "管理员强制中止播放"):
    host = cfg.get("emby_host", "").rstrip('/')
    api_key = cfg.get("emby_api_key", "")
    if not host or not api_key: return False
    
    url = f"{host}/emby/Sessions/{session_id}/Playing/Stop"
    try:
        res = requests.post(url, headers={"X-Emby-Token": api_key}, timeout=5)
        return res.status_code in [200, 204]
    except Exception as e:
        logger.error(f"[风控] 踢出设备失败: {e}")
        return False

def ban_user(user_id: str):
    host = cfg.get("emby_host", "").rstrip('/')
    api_key = cfg.get("emby_api_key", "")
    if not host or not api_key: return False
    
    policy_url = f"{host}/emby/Users/{user_id}"
    try:
        res = requests.get(policy_url, headers={"X-Emby-Token": api_key}, timeout=5)
        if res.status_code == 200:
            user_data = res.json()
            policy = user_data.get("Policy", {})
            policy["IsDisabled"] = True
            
            update_url = f"{host}/emby/Users/{user_id}/Policy"
            update_res = requests.post(update_url, headers={"X-Emby-Token": api_key}, json=policy, timeout=5)
            return update_res.status_code in [200, 204]
    except Exception as e:
        logger.error(f"[风控] 封禁用户失败: {e}")
    return False

def log_risk_action(user_id: str, username: str, action: str, reason: str):
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("INSERT INTO risk_logs (user_id, username, action, reason) VALUES (?, ?, ?, ?)", (user_id, username, action, reason))
        if action == "ban":
            cur.execute("UPDATE users_meta SET risk_level = 'banned' WHERE user_id = ?", (user_id,))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"[风控] 记录日志失败: {e}")

# ==========================================
# 👁️ 天眼：零延迟实时扫描与智能防抖
# ==========================================
def get_user_concurrent_limit(user_id: str) -> int:
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT max_concurrent FROM users_meta WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        conn.close()
        if row and row[0] is not None: return int(row[0]) 
    except: pass
    return int(cfg.get("default_max_concurrent", 2))

# 缓存池：只记录【当前正在违规】的设备组合，恢复正常后自动释放！
_alerted_sessions = set()

def scan_playbacks_and_alert():
    if not cfg.get("enable_risk_control", True): return

    host = cfg.get("emby_host", "").rstrip('/')
    api_key = cfg.get("emby_api_key", "")
    if not host or not api_key: return

    try:
        res = requests.get(f"{host}/emby/Sessions", headers={"X-Emby-Token": api_key}, timeout=10)
        if res.status_code != 200: return
        sessions = res.json()
        
        active_playbacks = {}
        for s in sessions:
            if s.get("NowPlayingItem") and s["NowPlayingItem"].get("MediaType") == "Video":
                uid = s.get("UserId")
                if not uid: continue
                if uid not in active_playbacks:
                    active_playbacks[uid] = []
                active_playbacks[uid].append(s)
                
        global _alerted_sessions
        current_alert_fingerprints = set()
        
        print(f"📡 [天眼雷达] 正在扫网... 发现 {len(active_playbacks)} 名用户正在看视频。")

        for uid, user_sessions in active_playbacks.items():
            limit = get_user_concurrent_limit(uid)
            current_count = len(user_sessions)
            username = user_sessions[0].get("UserName", "未知用户")
            
            print(f"   ▶️ 锁定用户: {username} | 当前并发: {current_count} | 专属限额: {limit}")
            
            if current_count > limit:
                devices_info = []
                alert_trigger_ids = []
                for s in user_sessions:
                    dev_name = s.get("DeviceName", "未知设备")
                    client = s.get("Client", "未知客户端")
                    sid = s.get("Id", "")
                    devices_info.append(f"{dev_name} ({client})")
                    alert_trigger_ids.append(sid)
                
                fingerprint = f"{uid}-" + "-".join(sorted(alert_trigger_ids))
                current_alert_fingerprints.add(fingerprint)
                
                if fingerprint not in _alerted_sessions:
                    # 这是一个全新的越界动作！
                    log_risk_action(uid, username, "warn", f"并发超限: 当前 {current_count} / 限额 {limit}")
                    devices_text = "\n".join([f"  🔸 {d}" for d in devices_info])
                    print(f"🚨 [风控执行] 发现越界！立即通过总线呼叫机器人发送警报！")
                    
                    bus.publish("notify.risk.alert", {
                        "username": username,
                        "current": current_count,
                        "limit": limit,
                        "devices_info": devices_text
                    })
                else:
                    print(f"⚠️ [风控防抖] {username} 的这批设备已经报过警了，正在等待处理...")
                    
        # 🔥 核心修复：更新缓存池，只保留当前还在违规的记录！
        _alerted_sessions.clear()
        _alerted_sessions.update(current_alert_fingerprints)
                    
    except Exception as e:
        logger.error(f"[风控天眼] 扫描异常: {e}")

def _on_playback_start(data):
    print("🔔 [事件总线] 捕获到视频播放动作，雷达将在 3 秒后启动...")
    def delay_scan():
        # 必须让子弹飞3秒！给足时间让 Emby 底层把新设备的 Session 登记到数据库里
        time.sleep(3)
        scan_playbacks_and_alert()
    threading.Thread(target=delay_scan, daemon=True).start()

def _risk_monitor_loop():
    # 虽然有了事件驱动，但为了防意外（比如别人拔网线没触发 stop 事件），我们保留 60 秒一次的静默兜底巡逻
    while True:
        try: scan_playbacks_and_alert()
        except: pass
        time.sleep(60) 

def start_risk_monitor():
    # 🔥 订阅事件总线
    bus.subscribe("notify.playback.start", _on_playback_start)
    threading.Thread(target=_risk_monitor_loop, daemon=True, name="RiskMonitorThread").start()
    logger.info("👁️ [风险管控] 零延迟天眼系统已启动 (事件驱动 + 60s兜底)")