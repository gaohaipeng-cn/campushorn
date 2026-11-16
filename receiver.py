# -*- coding: utf-8 -*-
from __future__ import annotations
"""
校园网小喇叭 - 接收端 (receiver.py)
功能：
  · UDP 响应发现广播
  · TCP 接收通知
  · 全屏显示通知（含倒计时）
  · 历史通知列表（SQLite 持久化）
  · 系统托盘（pystray）
  · 开机自启（Windows 注册表）
"""

import ctypes
import json
import os
import socket
import sqlite3
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from tkinter import messagebox, ttk

# ─── 单实例锁 ────────────────────────────────────────
_SINGLE_PORT = 19999
_instance_sock = None

def ensure_single_instance(port: int) -> bool:
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


# ─── 处理 pystray 导入 ───────────────────────────────────────────
try:
    import pystray
    from PIL import Image as PILImage, ImageDraw as PILDraw
    HAS_TRAY = True
except ImportError:
    pystray    = None
    PILImage   = None
    PILDraw    = None
    HAS_TRAY   = False

from common import (
    MAGIC,
    MSG_ANNOUNCE,
    MSG_BROADCAST,
    MSG_DISCOVER,
    RELAY_POLL_INTERVAL,
    TCP_MSG_PORT,
    UDP_BROADCAST_PORT,
    UDP_REPLY_PORT,
    get_data_dir,
    get_local_ips,
    load_config,
    recv_msg,
    relay_ack,
    relay_poll,
    relay_register,
    resource_path,
    save_config,
)

# ─── 颜色方案（蓝色主题）─────────────────────────────────────────
C_BG        = "#0D1B2A"
C_PANEL     = "#0A1520"
C_CARD      = "#1A3A5C"
C_ACCENT    = "#1A7BFF"
C_TEXT      = "#E0E0E0"
C_TEXT_DIM  = "#7788AA"
C_BTN       = "#1A7BFF"
C_BTN_HOV   = "#4DA3FF"
C_BTN_RED   = "#C0392B"
C_ENTRY_BG  = "#0A1525"
C_BORDER    = "#1E3050"
C_ROW_ODD   = "#111E2E"
C_ROW_EVEN  = "#0D1825"

FONT_TITLE  = ("微软雅黑", 14, "bold")
FONT_BODY   = ("微软雅黑", 11)
FONT_SMALL  = ("微软雅黑", 9)
FONT_BTN    = ("微软雅黑", 11, "bold")
FONT_BIG    = ("微软雅黑", 16, "bold")

DB_PATH = os.path.join(get_data_dir(), "notices.db")


# ─── 工具：窗口居中 ───────────────────────────────────────
def center_window(win, w: int = None, h: int = None):
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

APP_NAME    = "校园网小喇叭接收端"
REG_KEY     = r"Software\Microsoft\Windows\CurrentVersion\Run"
REG_VAL     = "CampusBroadcastReceiver"


# ═══════════════════════════════════════════════════════════════════
# 数据库
# ═══════════════════════════════════════════════════════════════════
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS notices (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            teacher   TEXT    NOT NULL,
            content   TEXT    NOT NULL,
            recv_time TEXT    NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def db_add_notice(teacher: str, content: str) -> int:
    recv_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO notices (teacher,content,recv_time) VALUES (?,?,?)",
              (teacher, content, recv_time))
    conn.commit()
    rowid = c.lastrowid
    conn.close()
    db_cleanup()
    return rowid


def db_cleanup(max_records: int = 100):
    """
    当记录数超过 max_records 时，自动删除最旧的记录，保持数据库整洁。
    """
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM notices")
    count = c.fetchone()[0]
    if count > max_records:
        excess = count - max_records
        c.execute("""
            DELETE FROM notices WHERE id IN (
                SELECT id FROM notices ORDER BY id ASC LIMIT ?
            )
        """, (excess,))
        conn.commit()
    conn.close()


def db_get_all() -> list[tuple]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id,teacher,content,recv_time FROM notices ORDER BY id DESC")
    rows = c.fetchall()
    conn.close()
    return rows


def db_delete(ids: list[int]):
    if not ids:
        return
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(f"DELETE FROM notices WHERE id IN ({','.join('?'*len(ids))})", ids)
    conn.commit()
    conn.close()


# ═══════════════════════════════════════════════════════════════════
# 开机自启
# ═══════════════════════════════════════════════════════════════════
def is_autostart() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_KEY, 0, winreg.KEY_READ)
        winreg.QueryValueEx(key, REG_VAL)
        winreg.CloseKey(key)
        return True
    except Exception:
        return False


def set_autostart(enable: bool):
    if sys.platform != "win32":
        return
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_KEY, 0, winreg.KEY_SET_VALUE)
        if enable:
            exe_path = sys.executable if getattr(sys, "frozen", False) else sys.argv[0]
            winreg.SetValueEx(key, REG_VAL, 0, winreg.REG_SZ, f'"{exe_path}"')
        else:
            try:
                winreg.DeleteValue(key, REG_VAL)
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
    except Exception as e:
        print(f"开机启动设置失败: {e}")


# ═══════════════════════════════════════════════════════════════════
# 简单扁平按钮
# ═══════════════════════════════════════════════════════════════════
class FlatButton(tk.Label):
    def __init__(self, parent, text, command, bg=C_BTN, fg="white",
                 font=FONT_BTN, padx=14, pady=6, width=None, height=None, **kw):
        super().__init__(parent, text=text, bg=bg, fg=fg, font=font,
                         padx=padx, pady=pady, cursor="hand2",
                         relief="flat", width=width, height=height, **kw)
        self._bg  = bg
        self._hov = C_BTN_HOV if bg == C_BTN else bg
        self._cmd = command
        self.bind("<Enter>",    lambda e: self.config(bg=self._hov))
        self.bind("<Leave>",    lambda e: self.config(bg=self._bg))
        self.bind("<Button-1>", lambda e: self._cmd())


# ═══════════════════════════════════════════════════════════════════
# 全屏通知窗口
# ═══════════════════════════════════════════════════════════════════

# 全屏通知配色
C_NTBG   = "#0D1B2A"   # 全屏背景
C_NTCARD = "#142840"   # 圆角矩形填充（比背景稍浅）
C_NTLINE = "#1A4A80"   # 圆角矩形描边
C_NTACC  = "#4DA3FF"   # 浅蓝色（箭头 / 副标题）
C_NTTOP  = "#80BFFF"   # 顶部主标题
C_NTTXT  = "#FFFFFF"   # 通知正文白色
C_NTTIME = "#FFD700"   # 倒计时金色

FONT_TITLE   = ("微软雅黑", 24, "bold")   # 窗口标题
FONT_NTTOP   = ("微软雅黑", 18, "bold")   # 顶部来自某某某老师
FONT_NTTXT   = ("微软雅黑", 36, "bold")   # 通知正文
FONT_NTCOUNT = ("微软雅黑", 18)            # 倒计时文字

# Canvas 圆角矩形 monkey-patch（polygon smooth 模拟圆角）
def _cv_rrect(self, x1, y1, x2, y2, radius, **kw):
    r = min(radius, (x2 - x1) // 2, (y2 - y1) // 2)
    if r <= 0:
        return self.create_rectangle(x1, y1, x2, y2, **kw)
    pts = []
    pts += [x1 + r, y1,  x2 - r, y1,  x2, y1 + r]
    pts += [x2, y1 + r,  x2, y2 - r,  x2 - r, y2]
    pts += [x2 - r, y2,  x1 + r, y2,  x1, y2 - r]
    pts += [x1, y2 - r,  x1, y1 + r,  x1 + r, y1]
    return self.create_polygon(pts, smooth=True, **kw)

tk.Canvas.create_rounded_rect = _cv_rrect


class FullScreenNotice(tk.Toplevel):
    """全屏通知窗口"""

    def __init__(self, master, teacher: str, content: str, duration: int):
        super().__init__(master)
        self._teacher   = teacher
        self._content   = content
        self._duration  = duration
        self._remaining = duration
        self._running   = True
        self._destroyed = False
        self._tick_id   = None
        self._arrows_vis = False

        self.attributes("-fullscreen", True)
        self.attributes("-topmost", True)
        self.configure(bg=C_NTBG)
        self.title("通知")

        _ico = resource_path(os.path.join("icons", "receiver.ico"))
        if os.path.exists(_ico):
            self._set_taskbar_icon(_ico)

        self._build_ui()
        if duration > 0:
            self._lbl_time.config(text=self._fmt_remaining())
            self._tick_id = self.after(1000, self._tick)

    # ── UI 布局 ─────────────────────────────────────────────────
    def _build_ui(self):
        root = tk.Frame(self, bg=C_NTBG)
        root.pack(fill="both", expand=True)

        # ── 顶部 100px ──────────────────────────────────────────
        top = tk.Frame(root, bg=C_NTBG, height=100)
        top.pack(fill="x")
        top.pack_propagate(False)

        tk.Label(top, text="📣  校园网小喇叭",
                 bg=C_NTBG, fg=C_NTTOP, font=FONT_TITLE
        ).pack(side="left", padx=(32,0), pady=(40,0))

        tk.Label(top, text=f"来自：{self._teacher} 老师",
                 bg=C_NTBG, fg=C_NTACC, font=FONT_NTTOP
        ).pack(side="left", padx=(0,200), pady=(40,0), expand=True)

        self._lbl_time = tk.Label(top, text="",
                 bg=C_NTBG, fg=C_NTTIME, font=FONT_NTCOUNT)
        self._lbl_time.pack(side="right", padx=(0,32), pady=(40,0))

        # 分隔线（横跨全宽）
        tk.Frame(root, bg=C_NTLINE, height=2).pack(fill="x", pady=20)

        # ── 中部（expand）── 圆角矩形 ──────────────
        mid = tk.Frame(root, bg=C_NTBG)
        mid.pack(fill="both", expand=True, padx=100, pady=20)

        # 圆角矩形 Canvas（填满 mid）
        card_cv = tk.Canvas(mid, bg=C_NTCARD,
                            highlightthickness=0, confine=False)
        card_cv.pack(fill="both", expand=True)

        # 浮动箭头容器
        arrow_frm = tk.Frame(root, bg=C_NTBG)
        self._arrow_frm = arrow_frm
        self._arrows_vis = False

        # 上箭头
        btn_up = tk.Label(arrow_frm, text="▲",
                bg=C_NTBG, fg=C_NTACC,
                font=("微软雅黑", 24), cursor="hand2", padx=10)
        btn_up.pack(side="top", pady=(0, 10))
        btn_up.bind("<Button-1>", lambda _: self._scroll(-1))
        self._btn_up = btn_up

        # 下箭头
        btn_dn = tk.Label(arrow_frm, text="▼",
                bg=C_NTBG, fg=C_NTACC,
                font=("微软雅黑", 24), cursor="hand2", padx=10)
        btn_dn.pack(side="top", pady=(10, 0))
        btn_dn.bind("<Button-1>", lambda _: self._scroll(1))
        self._btn_dn = btn_dn

        # Canvas 双重绑定：① 画圆角矩形 ② 触发重新居中等逻辑
        card_cv.bind("<Configure>", self._draw_card)
        card_cv.bind("<Configure>", self._on_card_config, add=True)
        self._card_cv = card_cv

        # Text widget（嵌入 Canvas）
        self._text = txt = tk.Text(
            card_cv, bg=C_NTCARD, fg=C_NTTXT,
            font=FONT_NTTXT,
            relief="flat", bd=0,
            padx=40, pady=30, # 基础的上下边界 padding
            wrap="word", state="disabled",
        )
        # 始终水平居中
        txt.tag_configure("center", justify="center")

        self._text_win = card_cv.create_window(
            (0, 0), window=txt, anchor="nw",
            width=0, height=0)

        # 写入内容
        txt.configure(state="normal")
        txt.insert("1.0", self._content)
        txt.tag_add("center", "1.0", "end")
        txt.configure(state="disabled")
        txt.edit_modified(False)

        # 文字修改时触发重新居中
        txt.bind("<<Modified>>", self._on_text_modified)

        # ── 底部 80px ──────────────────────────────────────────
        bot = tk.Frame(root, bg=C_NTBG, height=80)
        bot.pack(fill="x")
        bot.pack_propagate(False)

        FlatButton(bot, "关闭", self._close,
                   bg=C_BTN_RED, fg="white", font=FONT_BTN,
                   padx=16, pady=6, width=8, height=1
        ).pack(side="right", padx=(0, 40), pady=20)

        FlatButton(bot, "最小化", self._minimize,
                   bg=C_NTCARD, font=FONT_BTN, padx=16, pady=6, width=8, height=1
        ).pack(side="right", padx=12, pady=20)

    # ── Canvas 圆角矩形 ────────────────────────────────────────
    def _draw_card(self, _e=None):
        self._card_cv.delete("card_border")
        w = self._card_cv.winfo_width()
        h = self._card_cv.winfo_height()
        if w < 4 or h < 4:
            return
        self._card_cv.create_rounded_rect(
            2, 2, w - 2, h - 2, 50,
            outline=C_NTLINE, width=2, tags="card_border")

    def _on_card_config(self, _e=None):
        """Canvas 尺寸变化：重设 Text 尺寸 → 重算文字居中 → 更新箭头。"""
        cw = self._card_cv.winfo_width()
        ch = self._card_cv.winfo_height()
        if cw < 4 or ch < 4:
            return
        self._draw_card()
        self._card_cv.itemconfigure(self._text_win, width=cw, height=ch)
        
        # 为了防止刷新阻塞，推迟执行重新居中计算
        self.after_idle(self._recenter)

    def _recenter(self):
        """动态计算文字实际高度，撑不满显示区域则垂直居中，过长则恢复默认靠顶并显示滚动条。"""
        if self._destroyed:
            return
        
        self._text.config(state="normal")
        self._text.yview_moveto(0) # 确保计算时从顶端开始
        
        # 基础内边距
        base_pady = 30
        self._text.config(pady=base_pady)
        self._text.update_idletasks() # 强制刷新以便计算高度
        
        # 获取最后一行文字的位置 (x, y, width, height)
        last_box = self._text.bbox("end-1c")
        if not last_box:
            # bbox返回None说明最后一行在可视范围外，说明文本超长，直接保留基础边距并交由滚屏
            self._text.config(state="disabled")
            self._update_arrows()
            return
            
        _, y, _, h = last_box
        bottom = y + h # 文本实际总像素高度（包含顶部 base_pady）
        th = self._card_cv.winfo_height() # 圆角矩形可视高度
        
        # 判断文本高度与可用空间
        if bottom + base_pady < th:
            # 文本较少撑不满可视区域，动态追加额外的 pady，将其垂直居中
            extra_pad = th - (bottom + base_pady)
            self._text.config(pady=base_pady + extra_pad // 2)
        else:
            # 文本超长，恢复基础边距，交由滚屏处理
            self._text.config(pady=base_pady)
            
        self._text.config(state="disabled")
        # 居中或重置排版后，检查是否需要弹出滚动箭头
        self._update_arrows()

    def _on_text_modified(self, _e=None):
        if self._destroyed:
            return
        self._text.edit_modified(False)
        self.after_idle(self._recenter)

    # ── 箭头显示逻辑 ────────────────────────────────────────────
    def _update_arrows(self):
        """需要滚屏时才显示，不需要时完全隐藏。动态判断是否抵达顶部/底部并更新颜色。"""
        if self._destroyed:
            return
        
        # ▼▼▼ 修复：强制刷新一下文本布局，确保能拿到最准确的 yview 比例 ▼▼▼
        self._text.update_idletasks()
        
        try:
            first, last = self._text.yview()
        except Exception:
            return
        
        # 判断是否需要滚动（首尾没有同时完全曝光）
        needs_scroll = not (first <= 0.001 and last >= 0.999)

        if needs_scroll:
            if not self._arrows_vis:
                # ▼▼▼ 修复：相对于 root(全屏) 的右边缘进行定位。
                # relx=1.0 靠右，x=-30 向左偏移 30px，anchor="e" 以右边缘为锚点
                # 这样正好落在距离屏幕右边缘 30px 的留白中心，不挤占任何文字区域 ▼▼▼
                self._arrow_frm.place(relx=1.0, x=-30, rely=0.5, anchor="e")
                self._arrows_vis = True
            
            # 更新箭头激活状态（到顶则变暗，到底则变暗）
            self._btn_up.config(fg=C_NTACC if first > 0.001 else C_NTACC + "55")
            self._btn_dn.config(fg=C_NTACC if last  < 0.999 else C_NTACC + "55")
        else:
            if self._arrows_vis:
                self._arrow_frm.place_forget()
                self._arrows_vis = False

    def _scroll(self, direction: int):
        # 滚屏：1 代表 1 line (1行)
        self._text.yview_scroll(direction, "units")
        self._update_arrows()

    # ── 倒计时 ─────────────────────────────────────────────────
    def _fmt_remaining(self) -> str:
        if self._duration <= 0:
            return ""
        m = self._remaining // 60
        s = self._remaining % 60
        return f"剩余 {m:02d}:{s:02d}"

    def _tick(self):
        if self._destroyed or not self._running:
            return
        self._remaining -= 1
        if self._remaining <= 0:
            self._auto_close()
            return
        try:
            self._lbl_time.config(text=self._fmt_remaining())
        except Exception:
            return
        self._tick_id = self.after(1000, self._tick)

    def _auto_close(self):
        if self._destroyed:
            return
        self._running = False
        self._close()

    def _minimize(self):
        self.attributes("-fullscreen", False)
        self.state("iconic")

    def _close(self):
        if self._destroyed:
            return
        self._destroyed = True
        self._running   = False
        if self._tick_id:
            try:
                self.after_cancel(self._tick_id)
            except Exception:
                pass
        try:
            self.destroy()
        except Exception:
            pass

    def _set_taskbar_icon(self, icon_path: str):
        try:
            user32 = ctypes.windll.user32
            hwnd   = self.winfo_id()
            LR_LOADFROMFILE = 0x00000010
            IMAGE_ICON      = 1
            user32.LoadImageW.argtypes = [
                ctypes.c_void_p, ctypes.c_wchar_p,
                ctypes.c_uint,   ctypes.c_int,
                ctypes.c_int,    ctypes.c_uint]
            user32.LoadImageW.restype = ctypes.c_void_p
            for flag, sz in [(1, 0), (0, 16)]:
                hicon = user32.LoadImageW(None, icon_path, IMAGE_ICON,
                                          sz, sz, LR_LOADFROMFILE)
                user32.SendMessageW(hwnd, 0x0080, flag, hicon)
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════
# 历史通知列表（主窗口内容区）
# ═══════════════════════════════════════════════════════════════════
class NoticeListFrame(tk.Frame):
    """历史通知表格，支持复选框多选、批量删除、单条删除"""

    def __init__(self, parent, **kw):
        super().__init__(parent, bg=C_BG, **kw)
        self._rows = []
        self._build()

    def _build(self):
        toolbar = tk.Frame(self, bg=C_BG)
        toolbar.pack(fill="x", padx=12, pady=(8, 4))
        tk.Label(toolbar, text="历史通知", bg=C_BG, fg=C_TEXT,
                 font=FONT_TITLE).pack(side="left")
        FlatButton(toolbar, "批量删除", self._batch_delete,
                   bg=C_BTN_RED, padx=10, pady=4,
                   font=FONT_SMALL).pack(side="right", padx=4)
        FlatButton(toolbar, "全选", self._select_all,
                   bg=C_CARD, padx=10, pady=4,
                   font=FONT_SMALL).pack(side="right", padx=4)

        header = tk.Frame(self, bg=C_CARD)
        header.pack(fill="x", padx=12)
        for txt, w in [(" ", 3), ("教师", 8), ("通知内容", 50),
                         ("接收时间", 16), ("操作", 6)]:
            tk.Label(header, text=txt, bg=C_CARD, fg=C_TEXT,
                     font=FONT_SMALL, width=w).pack(side="left")

        list_outer = tk.Frame(self, bg=C_BG)
        list_outer.pack(fill="both", expand=True, padx=12, pady=4)

        self._canvas = tk.Canvas(list_outer, bg=C_BG, highlightthickness=0)
        scrollbar = ttk.Scrollbar(list_outer, orient="vertical",
                                   command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self._canvas.pack(side="left", fill="both", expand=True)

        self._list_frame = tk.Frame(self._canvas, bg=C_BG)
        self._list_win = self._canvas.create_window(
            (0, 0), window=self._list_frame, anchor="nw"
        )
        self._list_frame.bind("<Configure>", self._on_resize)
        self._canvas.bind("<Configure>", self._on_canvas_resize)
        self._canvas.bind("<MouseWheel>",
                          lambda e: self._canvas.yview_scroll(
                              -1 * (e.delta // 120), "units"))

        self._empty_lbl = tk.Label(
            self._list_frame, text="暂无历史通知",
            bg=C_BG, fg=C_TEXT_DIM, font=FONT_BODY
        )
        self.reload()

    def _on_resize(self, _e=None):
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))

    def _on_canvas_resize(self, e):
        self._canvas.itemconfig(self._list_win, width=e.width)

    def reload(self):
        for row in self._rows:
            row["frame"].destroy()
        self._rows.clear()
        self._empty_lbl.pack_forget()

        data = db_get_all()
        if not data:
            self._empty_lbl.pack(pady=40)
            return

        for i, (nid, teacher, content, recv_time) in enumerate(data):
            bg = C_ROW_ODD if i % 2 == 0 else C_ROW_EVEN
            frm = tk.Frame(self._list_frame, bg=bg)
            frm.pack(fill="x", pady=1)

            var = tk.BooleanVar(value=False)
            cb = tk.Checkbutton(frm, variable=var, bg=bg,
                                 selectcolor="#3A5A8C",
                                 activebackground=bg,
                                 fg="white")
            cb.pack(side="left", padx=4)

            tk.Label(frm, text=teacher, bg=bg, fg=C_TEXT,
                     font=FONT_BODY, width=8, anchor="w").pack(side="left")

            display = content if len(content) <= 40 else content[:37] + "…"
            content_lbl = tk.Label(frm, text=display, bg=bg, fg=C_TEXT,
                                    font=FONT_BODY, anchor="w")
            content_lbl.pack(side="left", expand=True, fill="x", padx=4)
            if len(content) > 40:
                self._bind_tooltip(content_lbl, content)

            tk.Label(frm, text=recv_time, bg=bg, fg=C_TEXT_DIM,
                     font=FONT_SMALL, width=18).pack(side="left")

            row_id = nid
            del_btn = tk.Label(frm, text="删除", bg=bg, fg=C_BTN_RED,
                                font=FONT_SMALL, cursor="hand2", padx=6)
            del_btn.pack(side="right", padx=4)
            del_btn.bind("<Button-1>",
                          lambda _, rid=row_id: self._delete_one(rid))

            self._rows.append({
                "id": nid, "teacher": teacher, "content": content,
                "recv_time": recv_time, "var": var, "frame": frm,
            })

    def _bind_tooltip(self, widget, text):
        tip = None
        def show(e):
            nonlocal tip
            tip = tk.Toplevel()
            tip.wm_overrideredirect(True)
            tip.wm_geometry(f"+{e.x_root+10}+{e.y_root+10}")
            tk.Label(tip, text=text, bg="#2A3A5A", fg=C_TEXT,
                     font=FONT_SMALL, wraplength=300, padx=6, pady=4).pack()
        def hide(_e):
            nonlocal tip
            if tip:
                tip.destroy()
                tip = None
        widget.bind("<Enter>", show)
        widget.bind("<Leave>", hide)

    def _select_all(self):
        for row in self._rows:
            row["var"].set(True)

    def _batch_delete(self):
        ids = [r["id"] for r in self._rows if r["var"].get()]
        if not ids:
            messagebox.showinfo("提示", "请先勾选要删除的通知！")
            return
        if messagebox.askyesno("确认删除", f"确定删除选中的 {len(ids)} 条通知？"):
            db_delete(ids)
            self.reload()

    def _delete_one(self, rid: int):
        if messagebox.askyesno("确认删除", "确定删除这条通知？"):
            db_delete([rid])
            self.reload()


# ═══════════════════════════════════════════════════════════════════
# 主窗口
# ═══════════════════════════════════════════════════════════════════
class ReceiverApp(tk.Tk):
    def __init__(self):
        if not ensure_single_instance(_SINGLE_PORT):
            messagebox.showerror("错误", "接收端已经在运行中！")
            sys.exit(1)

        super().__init__()
        self.withdraw()
        init_db()

        cfg = load_config()
        self._class_name = cfg.get("class_name", "")
        if not self._class_name:
            self._ask_class_name()

        # 读取/获取服务器 IP
        cfg = load_config()
        self._relay_ip = cfg.get("relay_server_ip", "")
        if not self._relay_ip:
            self._ask_relay_server()

        self.title(f"校园网小喇叭 · 接收端  [{self._class_name}]")
        self.configure(bg=C_BG)
        self.minsize(700, 500)
        center_window(self, 860, 620)

        try:
            icon_path = resource_path(os.path.join("icons", "receiver.ico"))
            if os.path.exists(icon_path):
                self.iconbitmap(icon_path)
        except Exception:
            pass

        self._build_ui()

        # 记录启动时间，用于过滤离线期间的幽灵通知
        self._startup_time = time.time()

        if not is_autostart():
            set_autostart(True)

        self._start_network()

        if HAS_TRAY:
            threading.Thread(target=self._run_tray, daemon=True).start()
        self.protocol("WM_DELETE_WINDOW", self._hide_to_tray)
        self.after(500, self._hide_to_tray)

    def _build_ui(self):
        title_bar = tk.Frame(self, bg=C_PANEL, height=50)
        title_bar.pack(fill="x")
        title_bar.pack_propagate(False)
        tk.Label(title_bar, text="📣  校园网小喇叭  ·  接收端",
                 bg=C_PANEL, fg=C_ACCENT, font=FONT_BIG).pack(side="left", padx=16)

        self._class_name_lbl = tk.Label(
            title_bar, text=f"[{self._class_name}]",
            bg=C_PANEL, fg=C_TEXT_DIM, font=FONT_SMALL, cursor="hand2"
        )
        self._class_name_lbl.pack(side="left", padx=(0, 4))
        self._class_name_lbl.bind("<Button-1>", lambda _: self._change_class_name())

        self._class_edit_lbl = tk.Label(
            title_bar, text="（编辑）",
            bg=C_PANEL, fg=C_ACCENT, font=FONT_SMALL, cursor="hand2"
        )
        self._class_edit_lbl.pack(side="left", padx=(0, 16))
        self._class_edit_lbl.bind("<Button-1>", lambda _: self._change_class_name())

        # 服务器 IP（右侧显示）
        self._relay_edit_lbl = tk.Label(
            title_bar, text="（编辑）",
            bg=C_PANEL, fg=C_ACCENT, font=FONT_SMALL, cursor="hand2"
        )
        self._relay_edit_lbl.pack(side="right", padx=(0, 16))
        self._relay_edit_lbl.bind("<Button-1>", lambda _: self._change_relay_server())

        self._relay_ip_lbl = tk.Label(
            title_bar, text=f"服务器：{self._relay_ip}",
            bg=C_PANEL, fg=C_TEXT_DIM, font=FONT_SMALL, cursor="hand2"
        )
        self._relay_ip_lbl.pack(side="right", padx=(0, 4))
        self._relay_ip_lbl.bind("<Button-1>", lambda _: self._change_relay_server())

        tk.Frame(self, bg=C_BORDER, height=1).pack(fill="x")

        self._notice_list = NoticeListFrame(self)
        self._notice_list.pack(fill="both", expand=True, padx=8, pady=8)

    def _ask_class_name(self):
        dlg = tk.Toplevel(self)
        dlg.title("首次设置")
        dlg.configure(bg=C_BG)
        dlg.resizable(False, False)

        tk.Label(dlg, text="欢迎使用 校园网小喇叭", bg=C_BG, fg=C_ACCENT,
                 font=("微软雅黑", 16, "bold")).pack(pady=(20, 4))
        tk.Label(dlg, text="请设置本教室的班级信息", bg=C_BG,
                 fg=C_TEXT, font=FONT_BODY).pack()

        row = tk.Frame(dlg, bg=C_BG)
        row.pack(padx=28, pady=(14, 0))

        tk.Label(row, text="年级", bg=C_BG, fg=C_TEXT_DIM,
                 font=FONT_SMALL).grid(row=0, column=0, padx=(0, 6), pady=(0, 4))
        tk.Label(row, text="班级", bg=C_BG, fg=C_TEXT_DIM,
                 font=FONT_SMALL).grid(row=0, column=1, pady=(0, 4))

        grade_var = tk.StringVar()
        class_var = tk.StringVar()

        GRADE_LIST = ["一年级", "二年级", "三年级", "四年级", "五年级", "六年级",
                      "七年级", "八年级", "九年级", "高一", "高二", "高三", "手动输入"]
        CLASS_LIST = ["1班", "2班", "3班", "4班", "5班", "6班", "7班", "8班", "9班", "10班", "手动输入"]

        grade_combo = ttk.Combobox(
            row, textvariable=grade_var,
            values=GRADE_LIST, state="normal",
            font=("微软雅黑", 14), width=8
        )
        grade_combo.grid(row=1, column=0, padx=(0, 6), ipady=4)
        grade_combo.set("")

        class_combo = ttk.Combobox(
            row, textvariable=class_var,
            values=CLASS_LIST, state="normal",
            font=("微软雅黑", 14), width=6
        )
        class_combo.grid(row=1, column=1, ipady=4)
        class_combo.set("")

        tk.Label(dlg, text='下拉选择或直接输入，例如："五年级" + "1班"', bg=C_BG,
                 fg=C_TEXT_DIM, font=FONT_SMALL).pack(pady=(6, 2))

        preview_var = tk.StringVar(value="")
        preview_lbl = tk.Label(dlg, textvariable=preview_var,
                                bg=C_BG, fg=C_ACCENT,
                                font=("微软雅黑", 13, "bold"))
        preview_lbl.pack(pady=2)

        def _update_preview(*_):
            g = grade_var.get().strip()
            c = class_var.get().strip()
            preview_var.set(f"预览：{g}{c}" if (g or c) else "")
        grade_var.trace_add("write", _update_preview)
        class_var.trace_add("write", _update_preview)

        def _ok():
            grade = grade_var.get().strip()
            cls   = class_var.get().strip()
            if not grade and not cls:
                messagebox.showwarning("提示", "年级和班级不能都为空！", parent=dlg)
                return
            name = f"{grade}{cls}"
            self._class_name = name
            save_config({"class_name": name})
            dlg.destroy()

        FlatButton(dlg, "确定", _ok).pack(pady=14)
        grade_combo.bind("<Return>", lambda _: class_combo.focus_set())
        class_combo.bind("<Return>", lambda _: _ok())
        grade_combo.focus_set()

        center_window(dlg, 320, 290)
        dlg.grab_set()
        self.wait_window(dlg)

    def _change_class_name(self):
        dlg = tk.Toplevel(self)
        dlg.title("修改班级")
        dlg.configure(bg=C_BG)
        dlg.resizable(False, False)

        tk.Label(dlg, text="修改班级信息", bg=C_BG, fg=C_ACCENT,
                 font=("微软雅黑", 16, "bold")).pack(pady=(20, 4))
        tk.Label(dlg, text="请重新设置本教室的班级信息", bg=C_BG,
                 fg=C_TEXT, font=FONT_BODY).pack()

        row = tk.Frame(dlg, bg=C_BG)
        row.pack(padx=28, pady=(14, 0))

        tk.Label(row, text="年级", bg=C_BG, fg=C_TEXT_DIM,
                 font=FONT_SMALL).grid(row=0, column=0, padx=(0, 6), pady=(0, 4))
        tk.Label(row, text="班级", bg=C_BG, fg=C_TEXT_DIM,
                 font=FONT_SMALL).grid(row=0, column=1, pady=(0, 4))

        grade_var = tk.StringVar()
        class_var = tk.StringVar()

        GRADE_LIST = ["一年级", "二年级", "三年级", "四年级", "五年级", "六年级",
                      "七年级", "八年级", "九年级", "高一", "高二", "高三", "手动输入"]
        CLASS_LIST = ["1班", "2班", "3班", "10班", "手动输入"]

        grade_combo = ttk.Combobox(
            row, textvariable=grade_var,
            values=GRADE_LIST, state="normal",
            font=("微软雅黑", 14), width=8
        )
        grade_combo.grid(row=1, column=0, padx=(0, 6), ipady=4)
        grade_combo.set("")

        class_combo = ttk.Combobox(
            row, textvariable=class_var,
            values=CLASS_LIST, state="normal",
            font=("微软雅黑", 14), width=6
        )
        class_combo.grid(row=1, column=1, ipady=4)
        class_combo.set("")

        tk.Label(dlg, text='下拉选择或直接输入，例如："五年级" + "1班"', bg=C_BG,
                 fg=C_TEXT_DIM, font=FONT_SMALL).pack(pady=(6, 2))

        preview_var = tk.StringVar(value="")
        preview_lbl = tk.Label(dlg, textvariable=preview_var,
                                bg=C_BG, fg=C_ACCENT,
                                font=("微软雅黑", 13, "bold"))
        preview_lbl.pack(pady=2)

        def _update_preview(*_):
            g = grade_var.get().strip()
            c = class_var.get().strip()
            preview_var.set(f"预览：{g}{c}" if (g or c) else "")
        grade_var.trace_add("write", _update_preview)
        class_var.trace_add("write", _update_preview)

        def _ok():
            grade = grade_var.get().strip()
            cls   = class_var.get().strip()
            if not grade and not cls:
                messagebox.showwarning("提示", "年级和班级不能都为空！", parent=dlg)
                return
            name = f"{grade}{cls}"
            self._class_name = name
            save_config({"class_name": name})
            self.title(f"校园网小喇叭 · 接收端  [{name}]")
            self._class_name_lbl.config(text=f"[{name}]")
            dlg.destroy()

        FlatButton(dlg, "保存", _ok).pack(pady=14)
        grade_combo.bind("<Return>", lambda _: class_combo.focus_set())
        class_combo.bind("<Return>", lambda _: _ok())
        grade_combo.focus_set()

        center_window(dlg, 320, 290)
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

        tk.Label(dlg, text="修改中转服务器地址", bg=C_BG, fg=C_ACCENT,
                 font=("微软雅黑", 16, "bold")).pack(pady=(16, 4))
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

    def _start_network(self):
        threading.Thread(target=self._udp_listen,  daemon=True).start()
        threading.Thread(target=self._tcp_listen,  daemon=True).start()
        threading.Thread(target=self._relay_poll, daemon=True).start()

    def _relay_poll(self):
        """HTTP 轮询中转服务器，获取新消息 + 发送班级心跳"""
        local_ips = get_local_ips()
        my_ip = local_ips[0] if local_ips else "127.0.0.1"
        last_id = 0

        while True:
            try:
                # 心跳注册（告知服务器本机 IP 和班级名）
                relay_register(self._class_name, my_ip)
                messages, max_id = relay_poll(my_ip, last_id)
                for msg in messages:
                    # 跳过启动前发送的幽灵通知（离线期间积压的消息）
                    sent_at = msg.get("sent_at", 0)
                    if sent_at < self._startup_time:
                        # 确认已读，避免下次轮询重复处理
                        msg_id = msg.get("id", 0)
                        if msg_id > 0:
                            threading.Thread(
                                target=relay_ack,
                                args=(msg_id,),
                                daemon=True
                            ).start()
                        continue

                    msg_id = msg.get("id", 0)
                    teacher = msg.get("teacher", "未知教师")
                    content = msg.get("content", "")
                    duration = msg.get("duration", 60)

                    # 保存到数据库 + 显示通知
                    db_add_notice(teacher, content)
                    self.after(0, self._show_notice, teacher, content, duration)
                    self.after(100, self._notice_list.reload)

                    # 确认已读
                    if msg_id > 0:
                        threading.Thread(
                            target=relay_ack,
                            args=(msg_id,),
                            daemon=True
                        ).start()

                if max_id > last_id:
                    last_id = max_id
            except Exception:
                pass

            time.sleep(RELAY_POLL_INTERVAL)

    def _udp_listen(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(1)
        try:
            sock.bind(("", UDP_BROADCAST_PORT))
        except Exception:
            sock.close()
            return

        while True:
            try:
                data, addr = sock.recvfrom(1024)
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

            if msg.get("type") == MSG_DISCOVER:
                local_ips = get_local_ips()
                receiver_ip = local_ips[0] if local_ips else "127.0.0.1"
                reply = MAGIC + json.dumps({
                    "type": MSG_ANNOUNCE,
                    "class_name": self._class_name,
                    "receiver_ip": receiver_ip,
                }, ensure_ascii=False).encode("utf-8")
                try:
                    sock.sendto(reply, (addr[0], UDP_REPLY_PORT))
                except Exception:
                    pass

    def _tcp_listen(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind(("", TCP_MSG_PORT))
        except Exception as e:
            print(f"TCP 绑定失败: {e}")
            return
        srv.listen(5)
        while True:
            try:
                conn, addr = srv.accept()
                threading.Thread(
                    target=self._handle_tcp,
                    args=(conn, addr),
                    daemon=True
                ).start()
            except Exception:
                break

    def _handle_tcp(self, conn: socket.socket, addr):
        msg = recv_msg(conn)
        conn.close()
        if not msg:
            return
        if msg.get("type") == MSG_BROADCAST:
            teacher  = msg.get("teacher", "未知教师")
            content  = msg.get("content", "")
            duration = int(msg.get("duration", 60))
            db_add_notice(teacher, content)
            self.after(0, self._show_notice, teacher, content, duration)
            self.after(100, self._notice_list.reload)

    def _show_notice(self, teacher: str, content: str, duration: int):
        FullScreenNotice(self, teacher, content, duration)

    def _run_tray(self):
        try:
            icon_path = resource_path(os.path.join("icons", "receiver.ico"))
            if os.path.exists(icon_path):
                image = PILImage.open(icon_path)
            else:
                image = self._make_tray_icon()
        except Exception:
            image = self._make_tray_icon()

        def _show(_icon, _item):
            self.after(0, self._show_main)

        def _quit(_icon, _item):
            _icon.stop()
            self.after(0, self.destroy)

        icon = pystray.Icon(
            "campus_broadcast_receiver",
            image,
            f"校园网小喇叭 [{self._class_name}]",
        )
        icon.menu = self._build_tray_menu(icon)
        self._tray_icon = icon
        icon.run()

    def _build_tray_menu(self, icon=None):
        auto = is_autostart()
        return pystray.Menu(
            pystray.MenuItem("显示主窗口", lambda _: self.after(0, self._show_main)),
            pystray.MenuItem(
                "取消开机启动" if auto else "设置开机启动",
                lambda _: set_autostart(not auto) or (icon and setattr(icon, 'menu', self._build_tray_menu(icon)))
            ),
            pystray.MenuItem("退出程序", lambda _: (icon.stop() if icon else None) or self.after(0, self.destroy)),
        )

    def _make_tray_icon(self):
        img = PILImage.new("RGBA", (64, 64), (13, 27, 42, 255))
        d = PILDraw.Draw(img)
        d.ellipse([2, 2, 62, 62], fill=(26, 123, 255, 255))
        return img

    def _hide_to_tray(self):
        self.withdraw()

    def _show_main(self):
        self._notice_list.reload()
        self.deiconify()
        self.lift()
        self.focus_force()


# ═══════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    app = ReceiverApp()
    app.mainloop()