# -*- coding: utf-8 -*-
"""
校园网小喇叭 - 公共模块
包含：协议常量、消息序列化/反序列化、工具函数
"""
from __future__ import annotations

import json
import os
import socket
import struct
import sys
import urllib.request

# ─── 网络协议常量 ───────────────────────────────────────────────────
UDP_BROADCAST_PORT = 5555   # UDP 广播（接收端监听，等待 DISCOVER）
UDP_REPLY_PORT     = 5557   # UDP 回包（发送端监听，等待 ANNOUNCE）
TCP_MSG_PORT       = 5556   # TCP 消息传输（接收端监听，发送端连接用）
BROADCAST_INTERVAL = 5      # 发送端广播心跳间隔（秒）
DISCOVER_TIMEOUT   = 2      # 单次扫描等待时间（秒）

# UDP 广播包魔数（防止与其他程序冲突）
MAGIC = b"XLTHORN"          # 校园网小喇叭 magic

# ─── 消息类型 ──────────────────────────────────────────────────────
MSG_DISCOVER   = "DISCOVER"   # 发送端 → 广播，寻找接收端
MSG_ANNOUNCE   = "ANNOUNCE"   # 接收端 → 回应，告知班级名称
MSG_BROADCAST  = "BROADCAST"  # 发送端 → 接收端，正式通知
MSG_ACK        = "ACK"        # 接收端 → 发送端，确认收到


def encode_msg(msg_type: str, payload: dict) -> bytes:
    """将消息编码为带4字节长度前缀的 JSON bytes"""
    body = json.dumps({"type": msg_type, **payload}, ensure_ascii=False).encode("utf-8")
    return struct.pack(">I", len(body)) + body


def decode_msg(data: bytes) -> dict:
    """将 JSON bytes 解码为字典（不含长度前缀，纯 body）"""
    return json.loads(data.decode("utf-8"))


def recv_msg(sock: socket.socket) -> dict | None:
    """从 TCP socket 读取一条完整消息，失败返回 None"""
    try:
        header = _recv_exactly(sock, 4)
        if not header:
            return None
        length = struct.unpack(">I", header)[0]
        if length > 4 * 1024 * 1024:   # 防止超大包
            return None
        body = _recv_exactly(sock, length)
        if not body:
            return None
        return decode_msg(body)
    except Exception:
        return None


def _recv_exactly(sock: socket.socket, n: int) -> bytes | None:
    buf = b""
    while len(buf) < n:
        try:
            chunk = sock.recv(n - len(buf))
        except Exception:
            return None
        if not chunk:
            return None
        buf += chunk
    return buf


def get_local_ips() -> list[str]:
    """获取本机所有非回环 IPv4 地址"""
    ips = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                ips.append(ip)
    except Exception:
        pass
    # 兜底：connect 法
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip not in ips:
            ips.append(ip)
    except Exception:
        pass
    return list(set(ips)) or ["127.0.0.1"]


def resource_path(relative_path: str) -> str:
    """兼容 PyInstaller 打包后的资源路径"""
    if hasattr(sys, "_MEIPASS"):
        base = sys._MEIPASS
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, relative_path)


def get_data_dir() -> str:
    """获取用户数据目录（AppData/Local/CampusBroadcast）"""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))
    else:
        base = os.path.expanduser("~/.local/share")
    data_dir = os.path.join(base, "CampusBroadcast")
    os.makedirs(data_dir, exist_ok=True)
    return data_dir


def get_config_path() -> str:
    return os.path.join(get_data_dir(), "config.json")


def load_config() -> dict:
    path = get_config_path()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_config(data: dict):
    path = get_config_path()
    existing = load_config()
    existing.update(data)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)


# ─── HTTP 中转服务器配置 ─────────────────────────────────────────────
# 用于跨 VLAN/子网通信（Sender 和 Receiver 无法直接 TCP 时启用）
RELAY_AUTH_KEY = "campus_broadcast_2024"  # 通信密钥（与 PHP 一致）
RELAY_POLL_INTERVAL = 3                   # Receiver 轮询间隔（秒）


def get_relay_server() -> str:
    """从配置文件读取中转服务器地址，未配置时返回空字符串"""
    cfg = load_config()
    server_ip = cfg.get("relay_server_ip", "")
    if not server_ip:
        return ""
    return f"http://{server_ip}/api/broadcast.php"


# ─── HTTP 中转发送函数 ──────────────────────────────────────────────
def relay_send(class_name: str, target_ip: str, teacher: str, content: str, duration: int) -> bool:
    """
    通过 HTTP 中转服务器发送通知
    返回: True 成功, False 失败
    """
    relay_url = get_relay_server()
    if not relay_url:
        return False

    payload = json.dumps({
        "class_name": class_name,
        "target_ip": target_ip,
        "teacher": teacher,
        "content": content,
        "duration": duration
    }).encode("utf-8")

    try:
        req = urllib.request.Request(
            relay_url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "X-Auth-Key": RELAY_AUTH_KEY
            },
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            return result.get("success", False)
    except Exception as e:
        print(f"[中转发送失败] {e}")
        return False


def relay_poll(my_ip: str, last_id: int) -> tuple[list, int]:
    """
    从中转服务器轮询新消息
    返回: (messages列表, max_id)
    """
    relay_url = get_relay_server()
    if not relay_url:
        return [], last_id

    try:
        url = f"{relay_url}?ip={my_ip}&last_id={last_id}"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("messages", []), data.get("max_id", last_id)
    except Exception:
        return [], last_id


def relay_ack(msg_id: int) -> bool:
    """
    通过 HTTP 中转服务器确认消息已读
    """
    relay_url = get_relay_server()
    if not relay_url:
        return False

    payload = json.dumps({
        "action": "ack",
        "id": msg_id
    }).encode("utf-8")

    try:
        req = urllib.request.Request(
            relay_url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "X-Auth-Key": RELAY_AUTH_KEY
            },
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            return result.get("success", False)
    except Exception:
        return False


# ─── HTTP 中转班级注册/查询函数 ────────────────────────────────────
def relay_register(class_name: str, ip: str) -> bool:
    """
    通过 HTTP 中转服务器注册班级（含心跳）
    每调用一次即更新心跳时间戳
    """
    relay_url = get_relay_server()
    if not relay_url:
        return False

    payload = json.dumps({
        "action": "register",
        "class_name": class_name,
        "ip": ip
    }).encode("utf-8")

    try:
        req = urllib.request.Request(
            relay_url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "X-Auth-Key": RELAY_AUTH_KEY
            },
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            return result.get("success", False)
    except Exception:
        return False


def relay_get_classes() -> list:
    """
    从 HTTP 中转服务器获取在线班级列表
    返回: [{"ip": "x.x.x.x", "class_name": "五年级1班"}, ...]
    """
    relay_url = get_relay_server()
    if not relay_url:
        return []

    try:
        url = f"{relay_url}?action=classes"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("classes", [])
    except Exception as e:
        print(f"[获取班级列表失败] {e}")
        return None
