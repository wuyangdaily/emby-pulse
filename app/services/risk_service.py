import sqlite3
import requests
import logging
import time
import threading
from app.core.config import cfg, DB_PATH
from app.services.bot_service import bot

logger = logging.getLogger("uvicorn")

# ==========================================
# 🗡️ 屠龙刀：强力执法接口
# ==========================================
def kick_session(session_id: str, reason: str = "管理员强制中止播放"):
    """强行掐断指定的播放会话"""
    host = cfg.get("emby_host", "").rstrip('/')
    api_key = cfg.get("emby_api_key", "")
    if not host or not api_key: return False
    
    url = f"{host}/emby/Sessions/{session_id}/Playing/Stop"
    try:
        # 发送停止指令
        res = requests.post(url, headers={"X-Emby-Token": api_key}, timeout=5)
        return res.status_code in [200, 204]
    except Exception as e:
        logger.error(f"[风控] 踢出设备失败: {e}")
        return False

def ban_user(user_id: str):
    """终极封号：直接冻结该用户"""
    host = cfg.get("emby_host", "").rstrip('/')
    api_key = cfg.get("emby_api_key", "")
    if not host or not api_key: return False
    
    # 1. 先获取他当前的 Policy (权限配置)
    policy_url = f"{host}/emby/Users/{user_id}"
    try:
        res = requests.get(policy_url, headers={"X-Emby-Token": api_key}, timeout=5)
        if res.status_code == 200:
            user_data = res.json()
            policy = user_data.get("Policy", {})
            
            # 2. 修改权限：设置为禁用
            policy["IsDisabled"] = True
            
            # 3. 提交修改
            update_url = f"{host}/emby/Users/{user_id}/Policy"
            update_res = requests.post(update_url, headers={"X-Emby-Token": api_key}, json=policy, timeout=5)
            return update_res.status_code in [200, 204]
    except Exception as e:
        logger.error(f"[风控] 封禁用户失败: {e}")
    return False

def log_risk_action(user_id: str, username: str, action: str, reason: str):
    """把执法记录写进我们刚才建的数据库表里"""
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO risk_logs (user_id, username, action, reason) VALUES (?, ?, ?, ?)",
            (user_id, username, action, reason)
        )
        # 如果是封号操作，顺便把他的风控等级标记为 banned
        if action == "ban":
            cur.execute("UPDATE users_meta SET risk_level = 'banned' WHERE user_id = ?", (user_id,))
            
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"[风控] 记录日志失败: {e}")

# ==========================================
# 👁️ 天眼：实时并发巡逻警报
# ==========================================
def get_user_concurrent_limit(user_id: str) -> int:
    """查水表：获取该用户的专属并发额度"""
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT max_concurrent FROM users_meta WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        conn.close()
        if row and row[0] is not None:
            return int(row[0])  # 返回专属额度
    except:
        pass
    # 没有专属额度，返回系统全局默认额度 (默认 2)
    return int(cfg.get("default_max_concurrent", 2))

# 内存缓存：记录已经报过警的会话，防止机器人疯狂刷屏发通知
_alerted_sessions = set()

def scan_playbacks_and_alert():
    """雷达扫描：检查所有人并发情况，超限则发通知"""
    # 如果没开风控开关，直接跳过
    if not cfg.get("enable_risk_control", False): return

    host = cfg.get("emby_host", "").rstrip('/')
    api_key = cfg.get("emby_api_key", "")
    if not host or not api_key: return

    try:
        res = requests.get(f"{host}/emby/Sessions", headers={"X-Emby-Token": api_key}, timeout=10)
        if res.status_code != 200: return
        sessions = res.json()
        
        # 1. 把正在看视频的会话按 UserId 分组归类
        active_playbacks = {}
        for s in sessions:
            if s.get("NowPlayingItem") and s["NowPlayingItem"].get("MediaType") == "Video":
                uid = s.get("UserId")
                if not uid: continue
                if uid not in active_playbacks:
                    active_playbacks[uid] = []
                active_playbacks[uid].append(s)
                
        # 2. 挨个查水表，比对限额
        for uid, user_sessions in active_playbacks.items():
            limit = get_user_concurrent_limit(uid)
            current_count = len(user_sessions)
            
            if current_count > limit:
                username = user_sessions[0].get("UserName", "未知用户")
                
                # 提取所有的设备名称和对应的 SessionId
                devices_info = []
                alert_trigger_ids = []
                for s in user_sessions:
                    dev_name = s.get("DeviceName", "未知设备")
                    client = s.get("Client", "未知客户端")
                    sid = s.get("Id")
                    devices_info.append(f"{dev_name} ({client})")
                    alert_trigger_ids.append(sid)
                
                # 生成唯一指纹，防止同一批设备重复报警
                fingerprint = f"{uid}-" + "-".join(sorted(alert_trigger_ids))
                if fingerprint in _alerted_sessions:
                    continue  # 这个违规情况已经报过了，跳过
                
                _alerted_sessions.add(fingerprint)
                
                # 记录数据库审计日志
                log_risk_action(uid, username, "warn", f"并发超限: 当前 {current_count} / 限额 {limit}")
                
                # 触发机器人报警推送 (让服主知道有人搞事情)
                msg = (
                    f"🚨 **[风控警告] 账号并发越界**\n"
                    f"👤 用户：{username}\n"
                    f"📈 状态：并发 {current_count} / 额度 {limit}\n"
                    f"📱 设备：{', '.join(devices_info)}\n"
                    f"⚠️ 请及时前往 EmbyPulse 后台查看处理！"
                )
                if hasattr(bot, 'send_message'):
                    bot.send_message(msg)
                    
    except Exception as e:
        logger.error(f"[风控天眼] 扫描异常: {e}")

def _risk_monitor_loop():
    """天眼死循环：每15秒扫一次"""
    while True:
        try:
            scan_playbacks_and_alert()
        except Exception as e:
            logger.error(f"[风控天眼] 守护线程异常: {e}")
        time.sleep(15)  # 休息15秒再查

def start_risk_monitor():
    """启动天眼守护线程"""
    threading.Thread(target=_risk_monitor_loop, daemon=True, name="RiskMonitorThread").start()
    logger.info("👁️ [风险管控] 天眼巡逻系统已启动，并发监控中...")