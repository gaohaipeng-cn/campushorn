# -*- coding: utf-8 -*-
from __future__ import annotations
"""
校园网小喇叭 - 发送端 (sender.py)
功能：
  · 自动 UDP 广播发现局域网接收端
  · 班级列表多选
  · 文字通知 + 显示时长 + 定时发送
  · 立即/定时发送
"""

import json
import os
import socket
import sys
import threading
import time
import tkinter as tk
from datetime import datetime, timedelta
from tkinter import messagebox, ttk

from common import (
    BROADCAST_INTERVAL,
    MAGIC,
    MSG_ANNOUNCE,
    MSG_BROADCAST,
    MSG_DISCOVER,
    TCP_MSG_PORT,
    UDP_BROADCAST_PORT,
    UDP_REPLY_PORT,
    encode_msg,
    get_local_ips,
    load_config,
    relay_send,
    relay_get_classes,
    resource_path,
    save_config,
)

# ─── 单实例锁 ──────────────────────────────────────────────
_SINGLE_PORT = 19998   # sender 锁端口
_instance_sock = None   # 保持引用，防止 GC 关闭 socket

def ensure_single_instance(port: int) -> bool:
    """尝试绑定本地端口实现单实例锁；失败则返回 False"""
    global _instance_sock
    _instance_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    _instance_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        _instance_sock.bind(("127.0.0.1", port))
        _instance_sock.listen(1)
        return True
    except OSError:
        _instance_sock.close()
        _instance_sock = None
        return False

# ─── 颜色方案（橙色主题）──────────────────────────────────────────
C_BG        = "#1A1A2E"
C_PANEL     = "#16213E"
C_CARD      = "#0F3460"
C_CARD_SEL  = "#FF6B1A"
C_CARD_OFF  = "#2A2A4A"
C_ACCENT    = "#FF6B1A"
C_ACCENT2   = "#FF8C42"
C_TEXT      = "#E0E0E0"
C_TEXT_DIM  = "#888899"
C_BTN       = "#FF6B1A"
C_BTN_HOV   = "#FF8C42"
C_ENTRY_BG  = "#0F1B35"
C_BORDER    = "#2A3A5A"
C_GREEN     = "#2ECC71"

FONT_TITLE  = ("微软雅黑", 14, "bold")
FONT_BODY   = ("微软雅黑", 11)
FONT_SMALL  = ("微软雅黑", 9)
FONT_BTN    = ("微软雅黑", 12, "bold")
FONT_BIG    = ("微软雅黑", 16, "bold")

DEFAULT_HINT = "1、选择班级，2、输入广播内容，3、点击发送"


# ─── 工具：窗口居中 ───────────────────────────────────────────────
def center_window(win, w: int = None, h: int = None):
    """将窗口居中显示在屏幕上。w/h 为 None 时自动检测窗口尺寸。"""
    win.update_idletasks()
    if w is None:
        w = win.winfo_reqwidth()
    if h is None:
        h = win.winfo_reqheight()
    sw = win.winfo_screenwidth()
    sh = win.winfo_screenheight()
    x = max(0, (sw - w) // 2)
    y = max(0, (sh - h) // 2)
    win.geometry(f"{w}x{h}+{x}+{y}")


# ═══════════════════════════════════════════════════════════════════
# 数据模型：已知接收端
# ═══════════════════════════════════════════════════════════════════
class ReceiverInfo:
    def __init__(self, ip: str, class_name: str):
        self.ip = ip
        self.class_name = class_name
        self.online = True
        self.last_seen = time.time()


# ═══════════════════════════════════════════════════════════════════
# 后台服务：UDP 广播发现
# ═══════════════════════════════════════════════════════════════════
class DiscoveryService(threading.Thread):
    """周期性向所有广播地址发 DISCOVER 包，收到 ANNOUNCE 后更新列表"""

    def __init__(self, on_update):
        super().__init__(daemon=True)
        self.on_update = on_update          # 回调，参数：dict[ip → ReceiverInfo]
        self.receivers: dict[str, ReceiverInfo] = {}
        self._lock = threading.Lock()
        self._stop_evt = threading.Event()

    def run(self):
        # 监听 ANNOUNCE 回包
        listener = threading.Thread(target=self._listen_loop, daemon=True)
        listener.start()
        while not self._stop_evt.is_set():
            self._send_discover()
            self._mark_offline()
            self._stop_evt.wait(BROADCAST_INTERVAL)

    def _send_discover(self):
        # UDP 数据报自带边界，无需长度前缀，直接发 MAGIC + JSON
        msg = json.dumps({"type": MSG_DISCOVER}).encode("utf-8")
        # 发往本机所有网卡所在子网的广播地址
        broadcast_addrs = set()
        try:
            for ip in get_local_ips():
                # 尝试猜广播地址：把最后一段改成255
                parts = ip.split(".")
                if len(parts) == 4:
                    broadcast_addrs.add(".".join(parts[:3]) + ".255")
        except Exception:
            pass
        broadcast_addrs.add("255.255.255.255")
        # 明确加上 loopback，确保同机运行时也能收到
        broadcast_addrs.add("127.0.0.1")

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(1)
        try:
            for addr in broadcast_addrs:
                try:
                    sock.sendto(MAGIC + msg, (addr, UDP_BROADCAST_PORT))
                except Exception:
                    pass
        finally:
            sock.close()

    def _listen_loop(self):
        """发送端在 UDP_REPLY_PORT 监听，等接收端回复的 ANNOUNCE 包"""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(1)
        try:
            sock.bind(("", UDP_REPLY_PORT))
        except Exception:
            return
        while not self._stop_evt.is_set():
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except Exception:
                break
            if data[:len(MAGIC)] != MAGIC:
                continue
            try:
                msg = json.loads(data[len(MAGIC):].decode("utf-8"))
            except Exception:
                continue
            if msg.get("type") == MSG_ANNOUNCE:
                # 使用 ANNOUNCE 中的 receiver_ip 作为 key（避免多网卡重复）
                ip = msg.get("receiver_ip", addr[0])
                class_name = msg.get("class_name", ip)
                with self._lock:
                    if ip not in self.receivers:
                        self.receivers[ip] = ReceiverInfo(ip, class_name)
                    else:
                        self.receivers[ip].class_name = class_name
                    self.receivers[ip].online = True
                    self.receivers[ip].last_seen = time.time()
                self.on_update(dict(self.receivers))
        sock.close()

    def _mark_offline(self):
        now = time.time()
        changed = False
        with self._lock:
            for r in self.receivers.values():
                if r.online and now - r.last_seen > BROADCAST_INTERVAL * 3:
                    r.online = False
                    changed = True
        if changed:
            with self._lock:
                snap = dict(self.receivers)
            self.on_update(snap)

    def stop(self):
        self._stop_evt.set()


# ═══════════════════════════════════════════════════════════════════
# 发送任务调度器
# ═══════════════════════════════════════════════════════════════════
class BroadcastScheduler(threading.Thread):
    """维护一个待发送任务队列，支持定时发送"""

    def __init__(self):
        super().__init__(daemon=True)
        self._queue: list[dict] = []
        self._lock = threading.Lock()
        self._wake = threading.Event()

    def schedule(self, task: dict):
        """task 包含: targets[{ip,class_name}], content, teacher, duration, send_at(timestamp)"""
        with self._lock:
            self._queue.append(task)
            self._queue.sort(key=lambda x: x["send_at"])
        self._wake.set()

    def run(self):
        while True:
            self._wake.clear()
            now = time.time()
            with self._lock:
                due = [t for t in self._queue if t["send_at"] <= now]
                for t in due:
                    self._queue.remove(t)
            for task in due:
                self._send_task(task)
            # 计算下次唤醒时间
            with self._lock:
                wait = (self._queue[0]["send_at"] - time.time()) if self._queue else 60
            self._wake.wait(timeout=max(0.5, wait))

    def _send_task(self, task: dict):
        for target in task["targets"]:
            threading.Thread(
                target=_send_to_target,
                args=(target["ip"], target["class_name"], task),
                daemon=True,
            ).start()


def _send_to_target(ip: str, class_name: str, task: dict):
    """发送通知到目标：优先 HTTP 中转，失败则 TCP 直连"""
    # 优先尝试 HTTP 中转（跨 VLAN）
    success = relay_send(
        class_name=class_name,
        target_ip=ip,
        teacher=task["teacher"],
        content=task["content"],
        duration=task["duration"]
    )
    if success:
        print(f"[发送成功] {ip} ({class_name}) via HTTP relay")
        return

    # 回退到 TCP 直连（局域网内可用）
    _tcp_send(ip, task)


def _tcp_send(ip: str, task: dict):
    """TCP 单播发送通知"""
    msg = encode_msg(MSG_BROADCAST, {
        "teacher":   task["teacher"],
        "content":   task["content"],
        "duration":  task["duration"],   # 秒数，-1 表示永久
        "sent_at":   task["send_at"],
    })
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(5)
        s.connect((ip, TCP_MSG_PORT))
        s.sendall(msg)
        s.close()
    except Exception as e:
        print(f"[发送失败] {ip}: {e}")


# ═══════════════════════════════════════════════════════════════════
# 班级卡片控件
# ═══════════════════════════════════════════════════════════════════
# 班级卡片常量（790窗宽-32padx-17滚动条=741, 741-18gap=723, /5列=144.6 → 取144）
CARD_W = 144   # 单卡片宽度 px
CARD_H = 76    # 单卡片高度 px（固定，防止长名换行撑高）
CARD_COLS = 5  # 每行卡片数
CARD_GAP  = 6  # 卡片间隔 px


class ClassCard(tk.Frame):
    def __init__(self, parent, info: ReceiverInfo, toggle_cb):
        super().__init__(parent, bg=C_CARD_OFF, relief="flat", bd=0,
                         cursor="hand2", width=CARD_W, height=CARD_H)
        self.grid_propagate(False)
        self.info = info
        self.selected = False
        self.toggle_cb = toggle_cb
        # 显式跟踪当前绘制状态，避免 cget() 颜色格式不一致导致误判
        self._painted_sel = False
        self._painted_online = info.online

        self._status_var = tk.StringVar()
        self._update_status()

        # 班级名称（固定宽度、不换行、超长自动截断由 width 限制）
        self._name_lbl = tk.Label(self, text=self._truncate(info.class_name, 8),
                                  bg=C_CARD_OFF, fg=C_TEXT,
                                  font=("微软雅黑", 11, "bold"),
                                  anchor="center", width=CARD_W // 11)
        self._name_lbl.pack(pady=(8, 0))
        # IP 地址
        self._ip_lbl = tk.Label(self, text=info.ip,
                                 bg=C_CARD_OFF, fg=C_TEXT_DIM,
                                 font=("微软雅黑", 8), anchor="center")
        self._ip_lbl.pack()
        # 在线状态
        self._dot = tk.Label(self, textvariable=self._status_var,
                              bg=C_CARD_OFF, font=FONT_SMALL,
                              fg=C_GREEN if info.online else C_TEXT_DIM)
        self._dot.pack(pady=(2, 0))

        self.bind("<Button-1>", self._on_click)
        for w in self.winfo_children():
            w.bind("<Button-1>", self._on_click)

    @staticmethod
    def _truncate(text: str, max_chars: int) -> str:
        """超过 max_chars 字时末尾显示 …"""
        return text if len(text) <= max_chars else text[:max_chars - 1] + "…"

    def _update_status(self):
        if self.info.online:
            self._status_var.set("● 在线")
        else:
            self._status_var.set("○ 离线")

    def refresh(self, info: ReceiverInfo):
        self.info = info
        # 只有值真正变化时才 config，避免无条件重绘导致抖动
        new_name = self._truncate(info.class_name, 8)
        if self._name_lbl.cget("text") != new_name:
            self._name_lbl.config(text=new_name)
        if self._ip_lbl.cget("text") != info.ip:
            self._ip_lbl.config(text=info.ip)
        new_online = info.online
        if self._painted_online != new_online:
            self._update_status()
        self._repaint()

    def _on_click(self, _event=None):
        self.selected = not self.selected
        self._repaint()
        self.toggle_cb()

    def _repaint(self):
        bg = C_CARD_SEL if self.selected else (C_CARD if self.info.online else C_CARD_OFF)
        fg_dot = C_GREEN if self.info.online else C_TEXT_DIM
        # 用显式状态变量判断，不依赖 cget() 颜色字符串比较
        need_repaint = (self._painted_sel != self.selected) or (self._painted_online != self.info.online)
        if need_repaint:
            self.config(bg=bg)
            for w in self.winfo_children():
                w.config(bg=bg)
            self._dot.config(fg=fg_dot)
            self._painted_sel = self.selected
            self._painted_online = self.info.online


# ═══════════════════════════════════════════════════════════════════
# 自定义圆角按钮
# ═══════════════════════════════════════════════════════════════════
class FlatButton(tk.Label):
    def __init__(self, parent, text, command, bg=C_BTN, fg="white",
                 font=FONT_BTN, padx=20, pady=8, **kw):
        super().__init__(parent, text=text, bg=bg, fg=fg, font=font,
                         padx=padx, pady=pady, cursor="hand2",
                         relief="flat", **kw)
        self._bg = bg
        self._bg_hov = C_BTN_HOV
        self._cmd = command
        self.bind("<Enter>",    lambda e: self.config(bg=self._bg_hov))
        self.bind("<Leave>",    lambda e: self.config(bg=self._bg))
        self.bind("<Button-1>", lambda e: self._cmd())


# ═══════════════════════════════════════════════════════════════════
# 定时发送对话框
# ═══════════════════════════════════════════════════════════════════
class ScheduleDialog(tk.Toplevel):
    """弹窗让用户选择定时发送的日期和时间"""

    def __init__(self, parent):
        super().__init__(parent)
        self.title("设置定时发送")
        self.configure(bg=C_BG)
        self.resizable(False, False)
        self.result: datetime | None = None

        now = datetime.now() + timedelta(minutes=5)
        tk.Label(self, text="定时发送时间", bg=C_BG, fg=C_TEXT,
                 font=FONT_TITLE).pack(pady=(16, 8))

        frm = tk.Frame(self, bg=C_BG)
        frm.pack(padx=20, pady=4)

        # 日期
        tk.Label(frm, text="日期：", bg=C_BG, fg=C_TEXT, font=FONT_BODY).grid(
            row=0, column=0, sticky="e", pady=4)
        self._date_var = tk.StringVar(value=now.strftime("%Y-%m-%d"))
        tk.Entry(frm, textvariable=self._date_var, bg=C_ENTRY_BG, fg=C_TEXT,
                 insertbackground=C_TEXT, font=FONT_BODY, width=12,
                 relief="flat").grid(row=0, column=1, padx=6)

        # 时间
        tk.Label(frm, text="时间：", bg=C_BG, fg=C_TEXT, font=FONT_BODY).grid(
            row=1, column=0, sticky="e", pady=4)
        self._time_var = tk.StringVar(value=now.strftime("%H:%M"))
        tk.Entry(frm, textvariable=self._time_var, bg=C_ENTRY_BG, fg=C_TEXT,
                 insertbackground=C_TEXT, font=FONT_BODY, width=8,
                 relief="flat").grid(row=1, column=1, padx=6, sticky="w")

        tk.Label(self, text="格式：YYYY-MM-DD  HH:MM", bg=C_BG,
                 fg=C_TEXT_DIM, font=FONT_SMALL).pack()

        btn_frm = tk.Frame(self, bg=C_BG)
        btn_frm.pack(pady=12)
        FlatButton(btn_frm, "确定", self._ok, font=FONT_BODY).pack(side="left", padx=8)
        FlatButton(btn_frm, "取消", self.destroy,
                   bg=C_CARD, font=FONT_BODY).pack(side="left", padx=8)
        self.protocol("WM_DELETE_WINDOW", self.destroy)

        center_window(self, 280, 220)
        self.grab_set()

    def _ok(self):
        try:
            dt = datetime.strptime(
                f"{self._date_var.get()} {self._time_var.get()}",
                "%Y-%m-%d %H:%M"
            )
            if dt <= datetime.now():
                messagebox.showwarning("提示", "定时时间必须晚于当前时间！", parent=self)
                return
            self.result = dt
            self.destroy()
        except ValueError:
            messagebox.showerror("格式错误", "请输入正确的日期和时间格式！", parent=self)


# ═══════════════════════════════════════════════════════════════════
# 主窗口
# ═══════════════════════════════════════════════════════════════════
class SenderApp(tk.Tk):
    def __init__(self):
        # 单实例检查
        if not ensure_single_instance(_SINGLE_PORT):
            messagebox.showerror("错误", "发送端已经在运行中！")
            sys.exit(1)

        super().__init__()
        self.title("校园网小喇叭 · 发送端")
        self.configure(bg=C_BG)
        self.minsize(700, 580)
        center_window(self, 790, 640)

        # 设置图标（使用 after 避免任务栏图标不显示）
        try:
            icon_path = resource_path(os.path.join("icons", "sender.ico"))
            if os.path.exists(icon_path):
                self.after(100, self._set_icon, icon_path)
        except Exception:
            pass

        # 读取/获取教师姓名（同步执行，保证窗口有内容）
        cfg = load_config()
        self._teacher = cfg.get("teacher_name", "")
        if not self._teacher:
            self._ask_teacher_name()

        # 读取/获取服务器 IP
        cfg = load_config()
        self._relay_ip = cfg.get("relay_server_ip", "")
        if not self._relay_ip:
            self._ask_relay_server()

        self._receivers: dict[str, ReceiverInfo] = {}
        self._relay_ips: set[str] = set()  # 追踪 HTTP relay 发现的 IP，用于离线检测
        self._cards: dict[str, ClassCard] = {}
        self._scheduled_dt: datetime | None = None
        self._scheduler = BroadcastScheduler()
        self._scheduler.start()

        self._build_ui()

        # 启动发现服务
        self._discovery = DiscoveryService(self._on_receivers_update)
        self._discovery.start()

        # 启动 HTTP 班级列表轮询（跨 VLAN）
        threading.Thread(target=self._relay_class_poll, daemon=True).start()

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _set_icon(self, icon_path):
        """设置窗口图标"""
        try:
            self.iconbitmap(icon_path)
        except Exception:
            pass

    # ── 首次运行：询问教师姓名 ──
    def _ask_teacher_name(self):
        dlg = tk.Toplevel(self)
        dlg.title("首次设置")
        dlg.configure(bg=C_BG)
        dlg.resizable(False, False)

        tk.Label(dlg, text="欢迎使用 校园网小喇叭", bg=C_BG, fg=C_ACCENT,
                 font=("微软雅黑", 16, "bold")).pack(pady=(20, 4))
        tk.Label(dlg, text="请输入教师姓名", bg=C_BG,
                 fg=C_TEXT, font=FONT_BODY).pack(pady=4)

        var = tk.StringVar()
        entry = tk.Entry(dlg, textvariable=var, bg=C_ENTRY_BG, fg=C_TEXT,
                         insertbackground=C_TEXT, font=("微软雅黑", 14),
                         width=16, relief="flat", justify="center")
        entry.pack(pady=8, ipady=6)
        entry.focus_set()

        def _ok():
            name = var.get().strip()
            if not name:
                messagebox.showwarning("提示", "姓名不能为空！", parent=dlg)
                return
            self._teacher = name
            save_config({"teacher_name": name})
            dlg.destroy()

        FlatButton(dlg, "确定", _ok).pack(pady=12)
        entry.bind("<Return>", lambda _: _ok())

        center_window(dlg, 300, 220)
        dlg.grab_set()
        self.wait_window(dlg)

    # ── 构建 UI ──
    def _build_ui(self):
        # 顶部标题栏
        title_bar = tk.Frame(self, bg=C_PANEL, height=50)
        title_bar.pack(fill="x")
        title_bar.pack_propagate(False)
        tk.Label(title_bar, text="📣  校园网小喇叭  ·  发送端",
                 bg=C_PANEL, fg=C_ACCENT, font=FONT_BIG).pack(side="left", padx=16)
        self._teacher_lbl = tk.Label(
            title_bar,
            text=f"教师姓名：{self._teacher}",
            bg=C_PANEL, fg=C_TEXT_DIM, font=FONT_SMALL,
            cursor="hand2"
        )
        self._teacher_lbl.pack(side="left", padx=(0, 4))
        self._teacher_lbl.bind("<Button-1>", lambda _: self._change_teacher())

        self._edit_lbl = tk.Label(
            title_bar,
            text="（编辑）",
            bg=C_PANEL, fg=C_ACCENT2, font=FONT_SMALL,
            cursor="hand2"
        )
        self._edit_lbl.pack(side="left", padx=(0, 16))
        self._edit_lbl.bind("<Button-1>", lambda _: self._change_teacher())

        # 服务器 IP（右侧显示）
        self._relay_edit_lbl = tk.Label(
            title_bar, text="（编辑）",
            bg=C_PANEL, fg=C_ACCENT2, font=FONT_SMALL, cursor="hand2"
        )
        self._relay_edit_lbl.pack(side="right", padx=(0, 16))
        self._relay_edit_lbl.bind("<Button-1>", lambda _: self._change_relay_server())

        self._relay_ip_lbl = tk.Label(
            title_bar, text=f"服务器：{self._relay_ip}",
            bg=C_PANEL, fg=C_TEXT_DIM, font=FONT_SMALL, cursor="hand2"
        )
        self._relay_ip_lbl.pack(side="right", padx=(0, 4))
        self._relay_ip_lbl.bind("<Button-1>", lambda _: self._change_relay_server())

        # 分隔线
        tk.Frame(self, bg=C_BORDER, height=1).pack(fill="x")

        # 班级列表区域
        cls_header = tk.Frame(self, bg=C_BG)
        cls_header.pack(fill="x", padx=16, pady=(12, 0))
        tk.Label(cls_header, text="班级列表", bg=C_BG, fg=C_TEXT,
                 font=FONT_TITLE).pack(side="left")
        self._cls_count_lbl = tk.Label(cls_header, text="（搜索中…）",
                                        bg=C_BG, fg=C_TEXT_DIM, font=FONT_SMALL)
        self._cls_count_lbl.pack(side="left", padx=8)
        FlatButton(cls_header, "全选", self._select_all,
                   bg=C_CARD, font=FONT_SMALL, padx=10, pady=4).pack(side="right", padx=4)
        FlatButton(cls_header, "取消全选", self._deselect_all,
                   bg=C_CARD, font=FONT_SMALL, padx=10, pady=4).pack(side="right", padx=4)

        # 班级卡片容器（可纵向滚动，固定高度刚好显示 2 行）
        # 2行 × CARD_H + 3×CARD_GAP(上下+行间) + 滚动条
        CLS_AREA_H = CARD_H * 2 + CARD_GAP * 3 + 18   # 18 = 滚动条高度估算
        cls_frame_outer = tk.Frame(self, bg=C_BG, height=CLS_AREA_H)
        cls_frame_outer.pack(fill="x", padx=16, pady=6)
        cls_frame_outer.pack_propagate(False)

        self._canvas = tk.Canvas(cls_frame_outer, bg=C_BG, highlightthickness=0)
        self._scrollbar = ttk.Scrollbar(cls_frame_outer, orient="vertical",
                                         command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=self._scrollbar.set)
        self._scrollbar.pack(side="right", fill="y")
        self._canvas.pack(side="left", fill="both", expand=True)

        self._cards_frame = tk.Frame(self._canvas, bg=C_BG)
        self._canvas_window = self._canvas.create_window(
            (0, 0), window=self._cards_frame, anchor="nw"
        )
        self._cards_frame.bind("<Configure>", self._on_cards_resize)
        # 鼠标滚轮支持
        self._canvas.bind("<MouseWheel>",
                          lambda e: self._canvas.yview_scroll(-1 if e.delta > 0 else 1, "units"))

        self._empty_lbl = tk.Label(
            self._cards_frame,
            text="正在搜索局域网内的接收端…\n（请确认接收端已运行）",
            bg=C_BG, fg=C_TEXT_DIM, font=FONT_BODY,
        )
        self._empty_lbl.grid(row=0, column=0, columnspan=CARD_COLS, padx=16, pady=30)

        tk.Frame(self, bg=C_BORDER, height=1).pack(fill="x", padx=16)

        # 广播内容区域
        content_lbl = tk.Frame(self, bg=C_BG)
        content_lbl.pack(fill="x", padx=16, pady=(10, 0))
        tk.Label(content_lbl, text="广播内容", bg=C_BG, fg=C_TEXT,
                 font=FONT_TITLE).pack(side="left")

        # 文本框
        txt_frame = tk.Frame(self, bg=C_ENTRY_BG, bd=0)
        txt_frame.pack(fill="both", padx=16, pady=4, expand=True)
        self._txt = tk.Text(txt_frame, bg=C_ENTRY_BG, fg=C_TEXT_DIM,
                             insertbackground=C_TEXT, font=("微软雅黑", 13),
                             relief="flat", bd=8, wrap="word", height=5)
        self._txt.pack(fill="both", expand=True)
        self._txt.insert("1.0", DEFAULT_HINT)
        self._txt.bind("<FocusIn>",  self._on_txt_focus_in)
        self._txt.bind("<FocusOut>", self._on_txt_focus_out)

        # 选项行：显示时长 + 定时发送
        opt_frame = tk.Frame(self, bg=C_BG)
        opt_frame.pack(fill="x", padx=16, pady=(4, 0))

        # 显示时长
        tk.Label(opt_frame, text="显示时长：", bg=C_BG, fg=C_TEXT,
                 font=FONT_BODY).pack(side="left")
        self._duration_var = tk.StringVar(value="1分钟")
        duration_opts = ["30秒", "1分钟", "5分钟", "永久", "自定义"]
        dur_menu = ttk.Combobox(opt_frame, textvariable=self._duration_var,
                                values=duration_opts, state="readonly",
                                width=7, font=FONT_BODY)
        dur_menu.pack(side="left", padx=4)
        dur_menu.bind("<<ComboboxSelected>>", self._on_duration_change)

        self._custom_dur_var = tk.StringVar(value="")
        self._custom_dur_entry = tk.Entry(
            opt_frame, textvariable=self._custom_dur_var,
            bg=C_ENTRY_BG, fg=C_TEXT, insertbackground=C_TEXT,
            font=FONT_BODY, width=6, relief="flat"
        )
        self._custom_dur_unit = tk.Label(opt_frame, text="秒", bg=C_BG,
                                          fg=C_TEXT, font=FONT_BODY)

        # 分隔
        tk.Label(opt_frame, text="  ", bg=C_BG).pack(side="left")

        # 定时发送
        self._schedule_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            opt_frame, text="定时发送", variable=self._schedule_var,
            bg=C_BG, fg=C_TEXT, selectcolor=C_CARD,
            activebackground=C_BG, activeforeground=C_TEXT,
            font=FONT_BODY, command=self._on_schedule_toggle
        ).pack(side="left", padx=(12, 4))
        self._schedule_lbl = tk.Label(opt_frame, text="", bg=C_BG,
                                       fg=C_ACCENT, font=FONT_BODY,
                                       cursor="hand2")
        self._schedule_lbl.pack(side="left")
        self._schedule_lbl.bind("<Button-1>", lambda _: self._pick_schedule())

        # 发送按钮
        btn_frame = tk.Frame(self, bg=C_BG)
        btn_frame.pack(fill="x", padx=16, pady=12)
        self._send_btn_var = tk.StringVar(value="📢  立即发送")
        self._send_btn = FlatButton(btn_frame, "📢  立即发送",
                                     self._do_send, font=FONT_BTN,
                                     padx=30, pady=10)
        self._send_btn.pack(expand=True)

    # ── 班级卡片更新（接收发现服务回调）──
    def _on_receivers_update(self, udp_receivers: dict):
        """UDP 发现回调：合并到 self._receivers 后刷新，避免覆盖 relay 发现的班级"""
        def _do_merge():
            for ip, info in udp_receivers.items():
                if ip not in self._receivers:
                    self._receivers[ip] = info
                else:
                    self._receivers[ip].class_name = info.class_name
                    self._receivers[ip].online = info.online
                    self._receivers[ip].last_seen = info.last_seen
            # 将 UDP 不再见到的 IP 标记离线（只处理通过 UDP 发现的，relay 的由 _merge_relay_classes 处理）
            for ip in list(self._receivers.keys()):
                if ip not in udp_receivers and ip not in self._relay_ips:
                    self._receivers[ip].online = False
            self._refresh_cards(self._receivers)
        self.after(0, _do_merge)

    def _receivers_snapshot(self, receivers: dict) -> tuple:
        """生成 receivers 的不可变快照，用于判断数据是否真的变化"""
        snap = []
        for ip in sorted(receivers.keys()):
            r = receivers[ip]
            snap.append((ip, r.class_name, r.online))
        return tuple(snap)

    def _refresh_cards(self, receivers: dict):
        # 数据没变就跳过整轮刷新，避免任何残余抖动
        new_snap = self._receivers_snapshot(receivers)
        if getattr(self, "_last_refresh_snap", None) == new_snap:
            return
        self._last_refresh_snap = new_snap

        self._receivers = receivers
        # 移除真正消失的班级（不会重新出现的）
        for ip in list(self._cards.keys()):
            if ip not in receivers:
                self._cards[ip].grid_forget()
                self._cards[ip].destroy()
                del self._cards[ip]
        # 新增不存在的；原地更新已有的（避免 destroy+rebuild 造成闪烁）
        for ip, info in receivers.items():
            if ip not in self._cards:
                card = ClassCard(self._cards_frame, info, self._update_send_btn)
                self._cards[ip] = card

        # 按 IP 排序后重排 Grid（仅位置变化时才 grid，避免反复触发 Configure 事件导致抖动）
        sorted_ips = sorted(self._cards.keys())
        for idx, ip in enumerate(sorted_ips):
            card = self._cards[ip]
            if ip in receivers:
                card.refresh(receivers[ip])
            row, col = divmod(idx, CARD_COLS)
            gi = card.grid_info()
            if not gi or gi.get("row") != row or gi.get("column") != col:
                card.grid(row=row, column=col,
                          padx=CARD_GAP // 2, pady=CARD_GAP // 2,
                          sticky="nsew")

        if receivers:
            # grid_info() 不依赖渲染状态，比 winfo_ismapped() 更可靠
            if self._empty_lbl.grid_info():
                self._empty_lbl.grid_remove()
        else:
            if not self._empty_lbl.grid_info():
                self._empty_lbl.grid(row=0, column=0, columnspan=CARD_COLS, padx=16, pady=30)

        count = len(receivers)
        online = sum(1 for r in receivers.values() if r.online)
        new_text = f"（共 {count} 个班级，{online} 个在线）"
        if self._cls_count_lbl.cget("text") != new_text:
            self._cls_count_lbl.config(text=new_text)

        # 卡片布局变化后强制刷新 scrollregion
        # after_idle 时子 widget 可能尚未完成 grid 布局，bbox("all") 返回空
        # 使用 update_idletasks() 强制完成布局计算再读边界框
        self._cards_frame.update_idletasks()
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))
        cw = self._canvas.winfo_width()
        if cw > 0:
            self._last_canvas_w = cw
            self._canvas.itemconfig(self._canvas_window, width=cw)

    def _on_cards_resize(self, _event=None):
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))
        # 让 cards_frame 宽度跟随 canvas（网格左对齐时不撑满问题）
        cw = self._canvas.winfo_width()
        if getattr(self, "_last_canvas_w", 0) != cw:
            self._last_canvas_w = cw
            self._canvas.itemconfig(self._canvas_window, width=cw)

    # ── HTTP 班级列表轮询（跨 VLAN 发现） ──
    def _relay_class_poll(self):
        """定期从 HTTP 中转服务器获取在线班级列表，与 UDP 发现互补"""
        while True:
            try:
                classes = relay_get_classes()
                if classes is not None:
                    self.after(0, self._merge_relay_classes, classes)
            except Exception:
                pass
            time.sleep(BROADCAST_INTERVAL)

    def _merge_relay_classes(self, classes: list):
        """合并 HTTP 中转返回的班级列表，同时检测离线"""
        current_ips = set()
        for cls in classes:
            ip = cls.get("ip", "")
            class_name = cls.get("class_name", "")
            if not ip or not class_name:
                continue
            current_ips.add(ip)
            if ip not in self._receivers:
                self._receivers[ip] = ReceiverInfo(ip, class_name)
            else:
                self._receivers[ip].class_name = class_name
            self._receivers[ip].online = True
            self._receivers[ip].last_seen = time.time()

        # 标记已不在服务器返回列表中的 relay 班级为离线
        for ip in self._relay_ips - current_ips:
            if ip in self._receivers:
                self._receivers[ip].online = False

        self._relay_ips = current_ips
        self._refresh_cards(self._receivers)

    def _select_all(self):
        for card in self._cards.values():
            if not card.selected:
                card.selected = True
                card._repaint()
        self._update_send_btn()

    def _deselect_all(self):
        for card in self._cards.values():
            if card.selected:
                card.selected = False
                card._repaint()
        self._update_send_btn()

    # ── 文本框占位提示 ──
    def _on_txt_focus_in(self, _event=None):
        if self._txt.get("1.0", "end-1c") == DEFAULT_HINT:
            self._txt.delete("1.0", "end")
            self._txt.config(fg=C_TEXT)

    def _on_txt_focus_out(self, _event=None):
        if not self._txt.get("1.0", "end-1c").strip():
            self._txt.insert("1.0", DEFAULT_HINT)
            self._txt.config(fg=C_TEXT_DIM)

    # ── 显示时长 ──
    def _on_duration_change(self, _event=None):
        if self._duration_var.get() == "自定义":
            self._custom_dur_entry.pack(side="left", padx=(2, 0), ipady=2)
            self._custom_dur_unit.pack(side="left")
        else:
            self._custom_dur_entry.pack_forget()
            self._custom_dur_unit.pack_forget()

    def _get_duration_seconds(self) -> int:
        """返回显示时长（秒），-1 表示永久"""
        val = self._duration_var.get()
        if val == "30秒":    return 30
        if val == "1分钟":   return 60
        if val == "5分钟":   return 300
        if val == "永久":    return -1
        if val == "自定义":
            try:
                s = int(self._custom_dur_var.get())
                return max(5, s)
            except ValueError:
                return 60
        return 60

    # ── 定时发送 ──
    def _on_schedule_toggle(self):
        if self._schedule_var.get():
            self._pick_schedule()
        else:
            self._scheduled_dt = None
            self._schedule_lbl.config(text="")
            self._update_send_btn()

    def _pick_schedule(self):
        dlg = ScheduleDialog(self)
        self.wait_window(dlg)
        if dlg.result:
            self._scheduled_dt = dlg.result
            self._schedule_lbl.config(
                text=dlg.result.strftime("  %Y-%m-%d %H:%M")
            )
            self._schedule_var.set(True)
        else:
            self._schedule_var.set(False)
            self._scheduled_dt = None
            self._schedule_lbl.config(text="")
        self._update_send_btn()

    def _update_send_btn(self):
        if self._scheduled_dt:
            self._send_btn.config(text="⏰  定时发送")
        else:
            self._send_btn.config(text="📢  立即发送")

    # ── 发送 ──
    def _do_send(self):
        # 检查选中班级
        targets = [
            {"ip": ip, "class_name": card.info.class_name}
            for ip, card in self._cards.items()
            if card.selected
        ]
        if not targets:
            messagebox.showwarning("提示", "请先选择至少一个班级！")
            return

        # 检查内容
        content = self._txt.get("1.0", "end-1c").strip()
        if not content or content == DEFAULT_HINT:
            messagebox.showwarning("提示", "请输入广播内容！")
            return

        duration = self._get_duration_seconds()
        send_at = self._scheduled_dt.timestamp() if self._scheduled_dt else time.time()

        task = {
            "targets":  targets,
            "teacher":  self._teacher,
            "content":  content,
            "duration": duration,
            "send_at":  send_at,
        }
        self._scheduler.schedule(task)

        if self._scheduled_dt:
            dt_str = self._scheduled_dt.strftime("%Y-%m-%d %H:%M")
            target_names = "、".join(t["class_name"] for t in targets)
            messagebox.showinfo(
                "定时发送",
                f"已安排定时发送！\n\n接收班级：{target_names}\n发送时间：{dt_str}"
            )
            # 重置定时
            self._scheduled_dt = None
            self._schedule_var.set(False)
            self._schedule_lbl.config(text="")
            self._update_send_btn()
        else:
            target_names = "、".join(t["class_name"] for t in targets)
            messagebox.showinfo("发送成功", f"通知已发送至：{target_names}")

    # ── 修改教师姓名 ──
    def _change_teacher(self):
        dlg = tk.Toplevel(self)
        dlg.title("修改姓名")
        dlg.configure(bg=C_BG)
        dlg.resizable(False, False)
        tk.Label(dlg, text="修改教师姓名", bg=C_BG, fg=C_TEXT,
                 font=FONT_TITLE).pack(pady=(16, 8))
        var = tk.StringVar(value=self._teacher)
        entry = tk.Entry(dlg, textvariable=var, bg=C_ENTRY_BG, fg=C_TEXT,
                          insertbackground=C_TEXT, font=("微软雅黑", 14),
                          width=14, relief="flat", justify="center")
        entry.pack(pady=6, ipady=6)
        entry.focus_set()

        def _ok():
            name = var.get().strip()
            if not name:
                return
            self._teacher = name
            save_config({"teacher_name": name})
            self._teacher_lbl.config(text=f"教师姓名：{name}")
            self._edit_lbl.config(text="（编辑）")
            dlg.destroy()

        FlatButton(dlg, "保存", _ok).pack(pady=10)
        entry.bind("<Return>", lambda _: _ok())

        center_window(dlg, 260, 190)
        dlg.grab_set()

    # ── 首次设置：询问服务器 IP ──
    def _ask_relay_server(self):
        dlg = tk.Toplevel(self)
        dlg.title("首次设置 · 服务器地址")
        dlg.configure(bg=C_BG)
        dlg.resizable(False, False)

        tk.Label(dlg, text="设置中转服务器地址", bg=C_BG, fg=C_ACCENT,
                 font=("微软雅黑", 16, "bold")).pack(pady=(20, 4))
        tk.Label(dlg, text="请输入学校中转服务器的 IP 地址", bg=C_BG,
                 fg=C_TEXT, font=FONT_BODY).pack(pady=(0, 4))
        tk.Label(dlg, text="例如：192.168.1.100", bg=C_BG,
                 fg=C_TEXT_DIM, font=FONT_SMALL).pack()

        var = tk.StringVar()
        entry = tk.Entry(dlg, textvariable=var, bg=C_ENTRY_BG, fg=C_TEXT,
                         insertbackground=C_TEXT, font=("微软雅黑", 14),
                         width=18, relief="flat", justify="center")
        entry.pack(pady=10, ipady=6)
        entry.focus_set()

        def _ok():
            ip = var.get().strip()
            if not ip:
                messagebox.showwarning("提示", "服务器 IP 不能为空！", parent=dlg)
                return
            self._relay_ip = ip
            save_config({"relay_server_ip": ip})
            dlg.destroy()

        FlatButton(dlg, "确定", _ok).pack(pady=12)
        entry.bind("<Return>", lambda _: _ok())

        center_window(dlg, 320, 230)
        dlg.grab_set()
        self.wait_window(dlg)

    # ── 修改服务器 IP ──
    def _change_relay_server(self):
        dlg = tk.Toplevel(self)
        dlg.title("修改服务器地址")
        dlg.configure(bg=C_BG)
        dlg.resizable(False, False)

        tk.Label(dlg, text="修改中转服务器地址", bg=C_BG, fg=C_TEXT,
                 font=FONT_TITLE).pack(pady=(16, 8))
        tk.Label(dlg, text="例如：192.168.1.100", bg=C_BG,
                 fg=C_TEXT_DIM, font=FONT_SMALL).pack()

        var = tk.StringVar(value=self._relay_ip)
        entry = tk.Entry(dlg, textvariable=var, bg=C_ENTRY_BG, fg=C_TEXT,
                         insertbackground=C_TEXT, font=("微软雅黑", 14),
                         width=18, relief="flat", justify="center")
        entry.pack(pady=8, ipady=6)
        entry.focus_set()

        def _ok():
            ip = var.get().strip()
            if not ip:
                messagebox.showwarning("提示", "服务器 IP 不能为空！", parent=dlg)
                return
            self._relay_ip = ip
            save_config({"relay_server_ip": ip})
            self._relay_ip_lbl.config(text=f"服务器：{ip}")
            dlg.destroy()

        FlatButton(dlg, "保存", _ok).pack(pady=10)
        entry.bind("<Return>", lambda _: _ok())

        center_window(dlg, 300, 210)
        dlg.grab_set()

    def _on_close(self):
        self._discovery.stop()
        self.destroy()


# ═══════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    app = SenderApp()
    app.mainloop()
