#!/usr/bin/env python3
"""ウィンドウマネージャー - ウィンドウの整列・最前面・透明度などをまとめて操作するWebアプリ (Windows用)

標準ライブラリのみで動作します（Win32 APIは ctypes で呼び出します。追加インストール不要）。

    python3 window_manager.py                # http://127.0.0.1:8407
    python3 window_manager.py --port 9000
    python3 window_manager.py --selftest     # ウィンドウ一覧などを表示して動作確認（何も変更しません）
    python3 window_manager.py --backend mock # Windows以外で画面を試す（模擬ウィンドウ）

主な機能
    ・ウィンドウ一覧から、最前面(ピン留め)・透明度・前面へ・最小化/最大化・閉じる
    ・選択したウィンドウの整列: グリッド / 横並び / 縦積み / メイン＋サブ / カスケード（余白・間隔・モニター指定、プレビュー付き）
    ・位置とサイズの数値指定、スナップ(1/2・1/4・1/3・2/3)、別モニターへ移動、中央寄せ
    ・レイアウトの保存と復元（プロセス名とタイトルでウィンドウを探して配置を戻す）
    ・ルール: 条件に合うウィンドウが開いたら自動で 最前面 / 透明度 / 配置 を適用
    ・集中モード（選択・前面以外を最小化）と元に戻す、画面外に出たウィンドウの回収、最前面/透明度の一括解除
    ・グローバルホットキー（任意。画面のスイッチでON/OFF）

データは同じフォルダの window_manager.db (SQLite) に保存されます（環境変数 WINDOW_MANAGER_DB で変更可）。
セキュリティ: 自分のPC (127.0.0.1) からのアクセスだけを受け付けます。
管理者権限で動いているウィンドウを操作するには、このツールも管理者として起動してください。
"""
import argparse
import ctypes
import json
import math
import ntpath
import os
import sqlite3
import sys
import threading
import time
import traceback
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("WINDOW_MANAGER_DB") or os.path.join(HERE, "window_manager.db")
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}


class BackendError(Exception):
    pass


# ================================================================ 幾何計算（OSに依存しない）
def split(total, n, gap):
    """total を n 個に gap 間隔で分割し [(開始位置, 長さ)] を返す（端数は前の方に配る）"""
    n = max(1, n)
    avail = max(n, total - gap * (n - 1))
    base, rem = divmod(avail, n)
    out, pos = [], 0
    for i in range(n):
        size = base + (1 if i < rem else 0)
        out.append((pos, size))
        pos += size + gap
    return out


def shrink(area, m):
    l, t, r, b = area
    return (l + m, t + m, r - m, b - m) if m > 0 and (r - l) > 2 * m + 100 and (b - t) > 2 * m + 100 else tuple(area)


def auto_cols(n, W, H):
    """セルの縦横比が 16:10 に近く、空きセルが少ない列数を選ぶ"""
    best, best_score = 1, None
    for c in range(1, n + 1):
        rows = math.ceil(n / c)
        aspect = (W / c) / max(H / rows, 1)
        score = abs(math.log(max(aspect, 0.01) / 1.6)) + 0.05 * (c * rows - n)
        if best_score is None or score < best_score:
            best, best_score = c, score
    return best


def layout_rects(mode, n, area, gap=8, cols=0, ratio=0.6):
    l, t, r, b = area
    W, H = r - l, b - t
    if n <= 0:
        return []
    if mode == "columns":
        return [(l + x, t, l + x + w, b) for x, w in split(W, n, gap)]
    if mode == "rows":
        return [(l, t + y, r, t + y + h) for y, h in split(H, n, gap)]
    if mode == "main":
        if n == 1:
            return [(l, t, r, b)]
        mw = int((W - gap) * min(max(ratio, 0.2), 0.8))
        return [(l, t, l + mw, b)] + [(l + mw + gap, t + y, r, t + y + h) for y, h in split(H, n - 1, gap)]
    if mode == "cascade":
        w, h = int(W * 0.7), int(H * 0.7)
        span = min(W - w, H - h)
        out = []
        for i in range(n):
            off = (i * 40) % (span + 1) if span > 0 else 0
            out.append((l + off, t + off, l + off + w, t + off + h))
        return out
    cols = min(max(1, int(cols) if cols else auto_cols(n, W, H)), n)  # grid
    rows = math.ceil(n / cols)
    out = []
    for ri, (y, h) in enumerate(split(H, rows, gap)):
        for x, w in split(W, min(cols, n - ri * cols), gap):  # 最後の行は幅いっぱいに広げる
            out.append((l + x, t + y, l + x + w, t + y + h))
    return out


SNAP_PRESETS = ["topleft", "top", "topright", "left", "center", "right", "bottomleft", "bottom", "bottomright",
                "full", "left3", "center3", "right3", "left23", "right23"]


def snap_rect(preset, area):
    l, t, r, b = area
    W, H = r - l, b - t
    hw, hh, tw = W // 2, H // 2, W // 3
    table = {
        "left": (l, t, l + hw, b), "right": (l + hw, t, r, b), "top": (l, t, r, t + hh), "bottom": (l, t + hh, r, b),
        "topleft": (l, t, l + hw, t + hh), "topright": (l + hw, t, r, t + hh),
        "bottomleft": (l, t + hh, l + hw, b), "bottomright": (l + hw, t + hh, r, b),
        "full": (l, t, r, b),
        "center": (l + int(W * 0.2), t + int(H * 0.15), r - int(W * 0.2), b - int(H * 0.15)),
        "left3": (l, t, l + tw, b), "center3": (l + tw, t, l + 2 * tw, b), "right3": (l + 2 * tw, t, r, b),
        "left23": (l, t, l + 2 * tw, b), "right23": (l + tw, t, r, b),
    }
    if preset not in table:
        raise BackendError(f"不明な配置です: {preset}")
    return table[preset]


def inter_area(a, b):
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return max(0, w) * max(0, h)


def monitor_for_rect(mons, rect):
    best = max(mons, key=lambda m: inter_area(m["rect"], rect))
    if inter_area(best["rect"], rect) > 0:
        return best
    cx, cy = (rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2
    return min(mons, key=lambda m: (((m["rect"][0] + m["rect"][2]) / 2 - cx) ** 2 + ((m["rect"][1] + m["rect"][3]) / 2 - cy) ** 2))


def norm_proc(p):
    p = (p or "").lower()
    return p[:-4] if p.endswith(".exe") else p


# ================================================================ Win32 構造体（サイズ検証のためOS非依存の型で定義）
class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_int32), ("top", ctypes.c_int32), ("right", ctypes.c_int32), ("bottom", ctypes.c_int32)]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_int32), ("y", ctypes.c_int32)]


class WINDOWPLACEMENT(ctypes.Structure):
    _fields_ = [("length", ctypes.c_uint32), ("flags", ctypes.c_uint32), ("showCmd", ctypes.c_uint32),
                ("ptMinPosition", POINT), ("ptMaxPosition", POINT), ("rcNormalPosition", RECT)]


class MONITORINFOEX(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint32), ("rcMonitor", RECT), ("rcWork", RECT),
                ("dwFlags", ctypes.c_uint32), ("szDevice", ctypes.c_uint16 * 32)]


# ================================================================ 模擬ウィンドウ（Windows以外・テスト用）
class MockBackend:
    name = "mock"

    def __init__(self):
        self.lock = threading.RLock()
        self._mons = [
            {"id": 0, "name": "DISPLAY1（デモ）", "primary": True, "rect": (0, 0, 1920, 1080), "work": (0, 0, 1920, 1040)},
            {"id": 1, "name": "DISPLAY2（デモ）", "primary": False, "rect": (1920, 0, 3200, 1024), "work": (1920, 0, 3200, 1024)},
        ]
        self._wins, self._next, self._z, self._fg = {}, 1000, 0, None
        self.insets = (7, 0, 7, 7)  # Windows10/11の見えない枠を再現
        for title, proc, rect in [
            ("Claude - Google Chrome", "chrome.exe", (100, 80, 1100, 800)),
            ("window_manager.py - Visual Studio Code", "Code.exe", (200, 120, 1300, 900)),
            ("無題 - メモ帳", "notepad.exe", (300, 160, 900, 600)),
            ("請求書.xlsx - Excel", "EXCEL.EXE", (350, 200, 1250, 850)),
            ("Slack | #general", "slack.exe", (400, 240, 1000, 700)),
        ]:
            self.spawn(title, proc, rect)

    def spawn(self, title, process, rect=(500, 300, 1100, 800)):
        with self.lock:
            self._next += 1
            self._z += 1
            h = self._next
            self._wins[h] = {"title": title, "process": process, "pid": 4000 + h, "rect": tuple(rect), "state": "normal",
                             "prev": "normal", "topmost": False, "opacity": 100, "z": self._z}
            self._fg = h
            return h

    def _get(self, hwnd):
        w = self._wins.get(hwnd)
        if not w:
            raise BackendError("ウィンドウが見つかりません（閉じられた可能性があります）")
        return w

    def exists(self, hwnd):
        return hwnd in self._wins

    def monitors(self):
        return [dict(m) for m in self._mons]

    def windows(self):
        il, it, ir, ib = self.insets
        with self.lock:
            items = sorted(self._wins.items(), key=lambda kv: (kv[1]["topmost"], kv[1]["z"]), reverse=True)
            out = []
            for h, w in items:
                l, t, r, b = w["rect"]
                out.append({"hwnd": h, "title": w["title"], "process": w["process"], "pid": w["pid"],
                            "rect": [l + il, t + it, r - ir, b - ib], "state": w["state"],
                            "topmost": w["topmost"], "opacity": w["opacity"]})
            return out

    def foreground(self):
        return self._fg if self._fg in self._wins else None

    def set_rect(self, hwnd, l, t, r, b):
        with self.lock:
            w = self._get(hwnd)
            il, it, ir, ib = self.insets
            w["state"] = "normal"
            w["rect"] = (l - il, t - it, r + ir, b + ib)

    def set_topmost(self, hwnd, on):
        with self.lock:
            self._get(hwnd)["topmost"] = bool(on)

    def set_opacity(self, hwnd, pct):
        with self.lock:
            self._get(hwnd)["opacity"] = int(min(100, max(10, pct)))

    def activate(self, hwnd):
        with self.lock:
            w = self._get(hwnd)
            if w["state"] == "min":
                w["state"] = w["prev"]
            self._z += 1
            w["z"] = self._z
            self._fg = hwnd

    def show(self, hwnd, mode):
        with self.lock:
            w = self._get(hwnd)
            if mode == "min":
                if w["state"] != "min":
                    w["prev"] = w["state"]
                w["state"] = "min"
            elif mode == "max":
                w["state"] = "max"
            elif mode == "restore":
                w["state"] = w["prev"] if w["state"] == "min" else "normal"
                if w["state"] == "min":
                    w["state"] = "normal"

    def close(self, hwnd):
        with self.lock:
            self._get(hwnd)
            del self._wins[hwnd]
            if self._fg == hwnd:
                self._fg = None


# ================================================================ Win32 実装
class Win32Backend:
    name = "win32"
    _EX = -20
    TOOL, APPWIN, LAYERED, TOPMOST = 0x80, 0x40000, 0x80000, 0x8

    def __init__(self):
        from ctypes import wintypes as wt
        self.wt = wt
        u = self.u = ctypes.WinDLL("user32", use_last_error=True)
        k = self.k = ctypes.WinDLL("kernel32", use_last_error=True)
        d = self.d = ctypes.WinDLL("dwmapi", use_last_error=True)
        self._dpi_aware()
        H, I, U, B, L, PR = wt.HWND, ctypes.c_int, wt.UINT, wt.BOOL, wt.LPARAM, ctypes.POINTER(RECT)
        self.WNDENUMPROC = ctypes.WINFUNCTYPE(B, H, L)
        self.MONENUMPROC = ctypes.WINFUNCTYPE(B, wt.HANDLE, wt.HDC, PR, L)

        def sig(fn, res, *args):
            fn.restype, fn.argtypes = res, list(args)

        sig(u.EnumWindows, B, self.WNDENUMPROC, L)
        for f in (u.IsWindowVisible, u.IsWindow, u.IsIconic, u.IsZoomed, u.SetForegroundWindow, u.BringWindowToTop):
            sig(f, B, H)
        sig(u.GetWindowTextLengthW, I, H)
        sig(u.GetWindowTextW, I, H, wt.LPWSTR, I)
        sig(u.GetWindowThreadProcessId, wt.DWORD, H, ctypes.POINTER(wt.DWORD))
        sig(u.GetWindowLongW, ctypes.c_long, H, I)
        sig(u.SetWindowLongW, ctypes.c_long, H, I, ctypes.c_long)
        sig(u.GetWindow, H, H, U)
        sig(u.GetWindowRect, B, H, PR)
        sig(u.GetWindowPlacement, B, H, ctypes.POINTER(WINDOWPLACEMENT))
        sig(u.SetWindowPos, B, H, H, I, I, I, I, U)
        sig(u.ShowWindow, B, H, I)
        sig(u.PostMessageW, B, H, U, wt.WPARAM, L)
        sig(u.GetForegroundWindow, H)
        sig(u.AttachThreadInput, B, wt.DWORD, wt.DWORD, B)
        sig(u.SetLayeredWindowAttributes, B, H, wt.COLORREF, wt.BYTE, wt.DWORD)
        sig(u.GetLayeredWindowAttributes, B, H, ctypes.POINTER(wt.COLORREF), ctypes.POINTER(wt.BYTE), ctypes.POINTER(wt.DWORD))
        sig(u.EnumDisplayMonitors, B, wt.HDC, ctypes.c_void_p, self.MONENUMPROC, L)
        sig(u.GetMonitorInfoW, B, wt.HANDLE, ctypes.POINTER(MONITORINFOEX))
        sig(d.DwmGetWindowAttribute, ctypes.c_long, H, wt.DWORD, ctypes.c_void_p, wt.DWORD)
        sig(k.GetCurrentThreadId, wt.DWORD)
        sig(k.OpenProcess, wt.HANDLE, wt.DWORD, B, wt.DWORD)
        sig(k.QueryFullProcessImageNameW, B, wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD))
        sig(k.CloseHandle, B, wt.HANDLE)
        self._ins, self._default_ins, self._layered_by_us = {}, (0, 0, 0, 0), set()

    @staticmethod
    def _dpi_aware():
        # 物理ピクセルで座標を扱う（モニターごとのDPI差でずれないように）
        for call in (lambda: ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)),
                     lambda: ctypes.windll.shcore.SetProcessDpiAwareness(2),
                     lambda: ctypes.windll.user32.SetProcessDPIAware()):
            try:
                call()
                return
            except Exception:
                continue

    def _check(self, ok, what):
        if not ok:
            e = ctypes.get_last_error()
            if e == 5:
                raise BackendError("アクセスが拒否されました（管理者権限のウィンドウは、このツールも管理者として起動すると操作できます）")
            raise BackendError(f"{what}に失敗しました（エラー {e}）")

    def exists(self, hwnd):
        return bool(self.u.IsWindow(hwnd))

    def _title(self, hwnd):
        n = self.u.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        self.u.GetWindowTextW(hwnd, buf, n + 1)
        return buf.value

    def _listable(self, hwnd):
        u, wt = self.u, self.wt
        if not u.IsWindowVisible(hwnd):
            return False
        ex = u.GetWindowLongW(hwnd, self._EX) & 0xFFFFFFFF
        if ex & self.TOOL and not ex & self.APPWIN:
            return False
        if u.GetWindow(hwnd, 4) and not ex & self.APPWIN:  # GW_OWNER: ダイアログ等は除く
            return False
        cloaked = wt.DWORD(0)
        if self.d.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked)) == 0 and cloaked.value:
            return False  # 別の仮想デスクトップや停止中のUWPウィンドウ
        title = self._title(hwnd)
        return bool(title) and title != "Program Manager"

    def _visible(self, hwnd):
        """見た目の枠 (DWM) と、見えない枠の大きさ(insets)を返す"""
        wr = RECT()
        self._check(self.u.GetWindowRect(hwnd, ctypes.byref(wr)), "位置の取得")
        ext = RECT()
        hr = self.d.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(ext), ctypes.sizeof(ext))  # DWMWA_EXTENDED_FRAME_BOUNDS
        if hr == 0 and ext.right > ext.left:
            vis = (ext.left, ext.top, ext.right, ext.bottom)
        else:
            vis = (wr.left, wr.top, wr.right, wr.bottom)
        return vis, (vis[0] - wr.left, vis[1] - wr.top, wr.right - vis[2], wr.bottom - vis[3])

    def _process(self, pid, cache):
        if pid not in cache:
            name = ""
            h = self.k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if h:
                buf = ctypes.create_unicode_buffer(1024)
                size = self.wt.DWORD(1024)
                if self.k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                    name = ntpath.basename(buf.value)
                self.k.CloseHandle(h)
            cache[pid] = name or "?"
        return cache[pid]

    def _info(self, hwnd, pcache):
        u, wt = self.u, self.wt
        pid = wt.DWORD(0)
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        ex = u.GetWindowLongW(hwnd, self._EX) & 0xFFFFFFFF
        iconic, zoomed = bool(u.IsIconic(hwnd)), bool(u.IsZoomed(hwnd))
        if iconic or zoomed:
            wp = WINDOWPLACEMENT()
            wp.length = ctypes.sizeof(wp)
            u.GetWindowPlacement(hwnd, ctypes.byref(wp))
            r = wp.rcNormalPosition
            il, it, ir, ib = self._ins.get(hwnd, self._default_ins)
            rect = [r.left + il, r.top + it, r.right - ir, r.bottom - ib]
        else:
            vis, ins = self._visible(hwnd)
            self._ins[hwnd], self._default_ins = ins, ins
            rect = list(vis)
        opacity = 100
        if ex & self.LAYERED:
            key, alpha, flags = wt.COLORREF(0), wt.BYTE(255), wt.DWORD(0)
            if u.GetLayeredWindowAttributes(hwnd, ctypes.byref(key), ctypes.byref(alpha), ctypes.byref(flags)) and flags.value & 2:
                opacity = round(alpha.value * 100 / 255)
        return {"hwnd": int(hwnd), "title": self._title(hwnd), "process": self._process(pid.value, pcache), "pid": pid.value,
                "rect": rect, "state": "min" if iconic else "max" if zoomed else "normal",
                "topmost": bool(ex & self.TOPMOST), "opacity": opacity}

    def windows(self):
        found = []

        def cb(hwnd, lp):
            try:
                if self._listable(hwnd):
                    found.append(hwnd)
            except Exception:
                pass
            return True

        proc = self.WNDENUMPROC(cb)
        self.u.EnumWindows(proc, 0)
        out, pcache = [], {}
        for h in found:
            try:
                out.append(self._info(h, pcache))
            except Exception:
                continue
        return out

    def monitors(self):
        mons = []

        def cb(hmon, hdc, lprc, lp):
            mi = MONITORINFOEX()
            mi.cbSize = ctypes.sizeof(mi)
            if self.u.GetMonitorInfoW(hmon, ctypes.byref(mi)):
                name = "".join(chr(c) for c in mi.szDevice).split("\0")[0]
                rc, wk = mi.rcMonitor, mi.rcWork
                mons.append({"name": name.lstrip("\\.") or "Display", "primary": bool(mi.dwFlags & 1),
                             "rect": (rc.left, rc.top, rc.right, rc.bottom), "work": (wk.left, wk.top, wk.right, wk.bottom)})
            return True

        proc = self.MONENUMPROC(cb)
        self.u.EnumDisplayMonitors(None, None, proc, 0)
        mons.sort(key=lambda m: (not m["primary"], m["rect"][0], m["rect"][1]))
        for i, m in enumerate(mons):
            m["id"] = i
        return mons

    def foreground(self):
        h = self.u.GetForegroundWindow()
        return int(h) if h else None

    def set_rect(self, hwnd, l, t, r, b):
        u = self.u
        if u.IsIconic(hwnd) or u.IsZoomed(hwnd):
            u.ShowWindow(hwnd, 9)  # SW_RESTORE
        _, ins = self._visible(hwnd)
        self._ins[hwnd], self._default_ins = ins, ins
        x, y = l - ins[0], t - ins[1]
        w, h = (r - l) + ins[0] + ins[2], (b - t) + ins[1] + ins[3]
        self._check(u.SetWindowPos(hwnd, None, x, y, w, h, 0x4 | 0x10), "移動・サイズ変更")  # NOZORDER|NOACTIVATE

    def set_topmost(self, hwnd, on):
        after = ctypes.c_void_p(-1 if on else -2)  # HWND_TOPMOST / HWND_NOTOPMOST
        self._check(self.u.SetWindowPos(hwnd, after, 0, 0, 0, 0, 0x2 | 0x1 | 0x10), "最前面の設定")

    def set_opacity(self, hwnd, pct):
        u = self.u
        pct = int(min(100, max(10, pct)))
        ex = u.GetWindowLongW(hwnd, self._EX) & 0xFFFFFFFF
        if pct >= 100:
            if ex & self.LAYERED:
                u.SetLayeredWindowAttributes(hwnd, 0, 255, 2)
                if hwnd in self._layered_by_us:  # 自分で付けた属性だけ外す
                    u.SetWindowLongW(hwnd, self._EX, ex & ~self.LAYERED)
                    self._layered_by_us.discard(hwnd)
            return
        if not ex & self.LAYERED:
            u.SetWindowLongW(hwnd, self._EX, ex | self.LAYERED)
            self._layered_by_us.add(hwnd)
        self._check(u.SetLayeredWindowAttributes(hwnd, 0, int(pct * 255 / 100), 2), "透明度の設定")

    def activate(self, hwnd):
        u = self.u
        if u.IsIconic(hwnd):
            u.ShowWindow(hwnd, 9)
        fg = u.GetForegroundWindow()
        cur = self.k.GetCurrentThreadId()
        fg_tid = u.GetWindowThreadProcessId(fg, None) if fg else 0
        attached = bool(fg_tid and fg_tid != cur and u.AttachThreadInput(cur, fg_tid, True))
        try:
            u.BringWindowToTop(hwnd)
            u.SetForegroundWindow(hwnd)
        finally:
            if attached:
                u.AttachThreadInput(cur, fg_tid, False)

    def show(self, hwnd, mode):
        self.u.ShowWindow(hwnd, {"min": 6, "max": 3, "restore": 9}[mode])

    def close(self, hwnd):
        self._check(self.u.PostMessageW(hwnd, 0x10, 0, 0), "閉じる")  # WM_CLOSE


# ================================================================ 管理ロジック（バックエンドに依存しない）
HOTKEYS = [  # (動作名, 表示, 修飾キー(Ctrl+Alt), 仮想キーコード, 説明)
    ("topmost", "Ctrl+Alt+T", 0x54, "今のウィンドウの最前面を ON/OFF"),
    ("opacity", "Ctrl+Alt+O", 0x4F, "今のウィンドウの透明度を切替（100→80→60→40%）"),
    ("center", "Ctrl+Alt+C", 0x43, "今のウィンドウを画面中央へ"),
    ("left", "Ctrl+Alt+←", 0x25, "今のウィンドウを画面の左半分へ"),
    ("right", "Ctrl+Alt+→", 0x27, "今のウィンドウを画面の右半分へ"),
    ("rescue", "Ctrl+Alt+R", 0x52, "画面外に出たウィンドウをすべて回収"),
    ("focus", "Ctrl+Alt+F", 0x46, "集中モード: 今のウィンドウ以外を最小化 / もう一度押すと元に戻す"),
]


class Manager:
    def __init__(self, backend, db_path):
        self.be, self.db_path = backend, db_path
        self.minimized_stack, self.rule_applied, self.rules_error = [], set(), ""
        self.hotkeys = HotkeyRunner(self)
        self.lock = threading.RLock()
        self._q("CREATE TABLE IF NOT EXISTS layouts(name TEXT PRIMARY KEY, data TEXT NOT NULL)")
        self._q("CREATE TABLE IF NOT EXISTS rules(id INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT NOT NULL)")
        self._q("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL)")

    def _q(self, sql, args=()):
        with closing(sqlite3.connect(self.db_path)) as con:
            with con:
                cur = con.execute(sql, args)
                return cur.fetchall(), cur.lastrowid

    def setting(self, key, default=""):
        rows, _ = self._q("SELECT value FROM settings WHERE key=?", (key,))
        return rows[0][0] if rows else default

    def set_setting(self, key, value):
        self._q("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

    # ---- 状態
    def state(self):
        mons = self.be.monitors()
        wins = self.be.windows()
        for w in wins:
            w["monitor"] = monitor_for_rect(mons, w["rect"])["id"]
        layouts = [{"name": n, "count": len(json.loads(d))} for n, d in self._q("SELECT name,data FROM layouts ORDER BY name")[0]]
        alive = {w["hwnd"] for w in wins}
        return {"backend": self.be.name, "demo": self.be.name == "mock", "monitors": mons, "windows": wins,
                "focus_stack": len([h for h in self.minimized_stack if h in alive]), "layouts": layouts,
                "rules": self.rules(), "rules_enabled": self.setting("rules_enabled", "0") == "1", "rules_error": self.rules_error,
                "snap_presets": SNAP_PRESETS, "hotkeys": self.hotkeys.info()}

    def _find(self, hwnd, wins=None):
        for w in wins if wins is not None else self.be.windows():
            if w["hwnd"] == hwnd:
                return w
        raise BackendError("ウィンドウが見つかりません（閉じられた可能性があります）")

    def _bulk(self, hwnds, fn):
        done, errs = 0, []
        for h in hwnds:
            try:
                fn(h)
                done += 1
            except BackendError as e:
                errs.append(str(e))
        return {"ok": done > 0 or not hwnds, "done": done, "errors": errs[:5]}

    def _monitor(self, mid, rect=None):
        mons = self.be.monitors()
        if mid is not None and mid >= 0:
            for m in mons:
                if m["id"] == mid:
                    return m
        return monitor_for_rect(mons, rect) if rect else next((m for m in mons if m["primary"]), mons[0])

    # ---- 個別操作
    def action(self, hwnds, action, value=None):
        hw = [int(h) for h in hwnds]
        be = self.be
        if action == "topmost":
            wins = {w["hwnd"]: w for w in be.windows()}
            return self._bulk(hw, lambda h: be.set_topmost(h, value if isinstance(value, bool) else not wins[h]["topmost"] if h in wins else True))
        if action == "opacity":
            return self._bulk(hw, lambda h: be.set_opacity(h, int(value)))
        if action in ("min", "max", "restore"):
            return self._bulk(hw, lambda h: be.show(h, action))
        if action == "activate":
            return self._bulk(list(reversed(hw)), be.activate)
        if action == "close":
            return self._bulk(hw, be.close)
        if action == "center":
            return self._bulk(hw, self.center)
        raise BackendError(f"不明な操作です: {action}")

    def set_rect(self, hwnd, l, t, w, h):
        if w < 100 or h < 60:
            raise BackendError("サイズが小さすぎます（幅100・高さ60以上）")
        self.be.set_rect(hwnd, int(l), int(t), int(l) + int(w), int(t) + int(h))
        return {"ok": True}

    def snap(self, hwnd, preset, monitor=-1, margin=0):
        w = self._find(hwnd)
        area = shrink(self._monitor(monitor, w["rect"])["work"], int(margin))
        self.be.set_rect(hwnd, *snap_rect(preset, area))
        return {"ok": True}

    def center(self, hwnd):
        w = self._find(hwnd)
        wl, wt, wr, wb = self._monitor(-1, w["rect"])["work"]
        ww, wh = min(w["rect"][2] - w["rect"][0], wr - wl), min(w["rect"][3] - w["rect"][1], wb - wt)
        l, t = wl + (wr - wl - ww) // 2, wt + (wb - wt - wh) // 2
        self.be.set_rect(hwnd, l, t, l + ww, t + wh)

    def move_to_monitor(self, hwnd, mid):
        w = self._find(hwnd)
        src, dst = self._monitor(-1, w["rect"])["work"], self._monitor(int(mid))["work"]
        sw, sh, dw, dh = src[2] - src[0], src[3] - src[1], dst[2] - dst[0], dst[3] - dst[1]
        l, t, r, b = w["rect"]
        nw, nh = min(r - l, dw), min(b - t, dh)
        nl = dst[0] + min(max(0, round((l - src[0]) * dw / max(sw, 1))), dw - nw)
        nt = dst[1] + min(max(0, round((t - src[1]) * dh / max(sh, 1))), dh - nh)
        self.be.set_rect(hwnd, nl, nt, nl + nw, nt + nh)
        return {"ok": True}

    # ---- 整列
    def arrange(self, hwnds, mode="grid", monitor=-1, gap=8, margin=0, cols=0, ratio=0.6, preview=False):
        wins = self.be.windows()
        chosen = [w for h in hwnds for w in wins if w["hwnd"] == int(h)]
        if not chosen:
            raise BackendError("整列するウィンドウを選んでください")
        mon = self._monitor(monitor, chosen[0]["rect"])
        area = shrink(mon["work"], int(margin))
        rects = layout_rects(mode, len(chosen), area, int(gap), int(cols), float(ratio))
        plan = [{"hwnd": w["hwnd"], "rect": list(r)} for w, r in zip(chosen, rects)]
        if preview:
            return {"ok": True, "monitor": mon["id"], "area": list(area), "plan": plan}
        res = self._bulk([p["hwnd"] for p in plan], lambda h: self.be.set_rect(h, *next(p["rect"] for p in plan if p["hwnd"] == h)))
        if mode == "cascade":  # 先頭のウィンドウが一番手前になるように
            for p in reversed(plan):
                try:
                    self.be.activate(p["hwnd"])
                except BackendError:
                    pass
        return res

    # ---- まとめ操作
    def rescue(self):
        mons, moved = self.be.monitors(), 0
        prim = next((m for m in mons if m["primary"]), mons[0])
        for w in self.be.windows():
            r = w["rect"]
            area = max(1, (r[2] - r[0]) * (r[3] - r[1]))
            if w["state"] != "normal":
                continue
            seen = max(inter_area(r, m["work"]) for m in mons)
            if seen >= 0.25 * area and seen >= 60 * 40:
                continue
            wl, wt, wr, wb = prim["work"]
            ww, wh = min(r[2] - r[0], wr - wl), min(r[3] - r[1], wb - wt)
            l, t = wl + (wr - wl - ww) // 2, wt + (wb - wt - wh) // 2
            try:
                self.be.set_rect(w["hwnd"], l, t, l + ww, t + wh)
                moved += 1
            except BackendError:
                pass
        return {"ok": True, "done": moved}

    def focus_mode(self, keep=()):
        keep = {int(h) for h in keep}
        fg = self.be.foreground()
        if fg:
            keep.add(fg)
        done = 0
        for w in self.be.windows():
            if w["hwnd"] in keep or w["state"] == "min":
                continue
            try:
                self.be.show(w["hwnd"], "min")
                self.minimized_stack.append(w["hwnd"])
                done += 1
            except BackendError:
                pass
        return {"ok": True, "done": done}

    def focus_restore(self):
        done = 0
        for h in reversed(self.minimized_stack):
            try:
                if self.be.exists(h):
                    self.be.show(h, "restore")
                    done += 1
            except BackendError:
                pass
        self.minimized_stack = []
        return {"ok": True, "done": done}

    def reset_all(self):
        done = 0
        for w in self.be.windows():
            try:
                if w["topmost"]:
                    self.be.set_topmost(w["hwnd"], False)
                    done += 1
                if w["opacity"] < 100:
                    self.be.set_opacity(w["hwnd"], 100)
                    done += 1
            except BackendError:
                pass
        return {"ok": True, "done": done}

    # ---- レイアウト
    def save_layout(self, name, hwnds=None):
        name = (name or "").strip()[:60]
        if not name:
            raise BackendError("レイアウト名を入力してください")
        wins = self.be.windows()
        pick = {int(h) for h in hwnds} if hwnds else None
        entries = [{"process": w["process"], "title": w["title"], "rect": w["rect"], "state": w["state"],
                    "topmost": w["topmost"], "opacity": w["opacity"]}
                   for w in wins if (w["hwnd"] in pick if pick else w["state"] != "min")]
        if not entries:
            raise BackendError("保存するウィンドウがありません")
        self._q("INSERT INTO layouts(name,data) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET data=excluded.data",
                (name, json.dumps(entries, ensure_ascii=False)))
        return {"ok": True, "done": len(entries)}

    def apply_layout(self, name):
        rows, _ = self._q("SELECT data FROM layouts WHERE name=?", (name,))
        if not rows:
            raise BackendError("レイアウトが見つかりません")
        entries, wins = json.loads(rows[0][0]), self.be.windows()
        pairs = []
        for i, e in enumerate(entries):
            for j, w in enumerate(wins):
                if norm_proc(w["process"]) != norm_proc(e["process"]):
                    continue
                a, b = e["title"].lower(), w["title"].lower()
                score = 3 if a == b else 2 if (len(a) >= 3 and (a in b or b in a)) else 1
                pairs.append((-score, i, j))
        pairs.sort()
        used_e, used_w, matched = set(), set(), []
        for _, i, j in pairs:
            if i not in used_e and j not in used_w:
                used_e.add(i), used_w.add(j), matched.append((entries[i], wins[j]["hwnd"]))
        errs = []
        for e, h in matched:
            try:
                l, t, r, b = e["rect"]
                if e["state"] != "min" or True:
                    self.be.set_rect(h, l, t, r, b)
                self.be.set_topmost(h, e["topmost"])
                self.be.set_opacity(h, e["opacity"])
                if e["state"] in ("max", "min"):
                    self.be.show(h, e["state"])
            except BackendError as ex:
                errs.append(str(ex))
        missing = [f'{e["process"]}: {e["title"][:30]}' for i, e in enumerate(entries) if i not in used_e]
        return {"ok": True, "done": len(matched) - len(errs), "missing": missing, "errors": errs[:5]}

    def delete_layout(self, name):
        self._q("DELETE FROM layouts WHERE name=?", (name,))
        return {"ok": True}

    # ---- ルール
    def rules(self):
        return [dict(json.loads(d), id=i) for i, d in self._q("SELECT id,data FROM rules ORDER BY id")[0]]

    def add_rule(self, data):
        proc, title = str(data.get("process") or "").strip()[:80], str(data.get("title") or "").strip()[:120]
        top, op, snap = data.get("topmost"), data.get("opacity"), data.get("snap") or None
        if not proc and not title:
            raise BackendError("プロセス名かタイトルのどちらかを入力してください")
        if top is None and not op and not snap:
            raise BackendError("適用する設定（最前面・透明度・配置）を1つ以上選んでください")
        if snap and snap not in SNAP_PRESETS:
            raise BackendError("配置の指定が正しくありません")
        rule = {"process": proc, "title": title, "topmost": top if isinstance(top, bool) else None,
                "opacity": int(op) if op else None, "snap": snap, "enabled": True}
        self._q("INSERT INTO rules(data) VALUES(?)", (json.dumps(rule, ensure_ascii=False),))
        self.rule_applied.clear()
        return {"ok": True}

    def update_rule(self, rid, **kw):
        rows, _ = self._q("SELECT data FROM rules WHERE id=?", (int(rid),))
        if rows:
            d = json.loads(rows[0][0])
            d.update(kw)
            self._q("UPDATE rules SET data=? WHERE id=?", (json.dumps(d, ensure_ascii=False), int(rid)))
        return {"ok": True}

    def delete_rule(self, rid):
        self._q("DELETE FROM rules WHERE id=?", (int(rid),))
        return {"ok": True}

    def apply_rules(self):
        if self.setting("rules_enabled", "0") != "1":
            return 0
        rules = [r for r in self.rules() if r.get("enabled")]
        if not rules:
            return 0
        wins = self.be.windows()
        alive = {w["hwnd"] for w in wins}
        self.rule_applied = {k for k in self.rule_applied if k[0] in alive}
        count = 0
        for r in rules:
            for w in wins:
                key = (w["hwnd"], r["id"])
                if key in self.rule_applied:
                    continue
                if r["process"] and norm_proc(w["process"]) != norm_proc(r["process"]):
                    continue
                if r["title"] and r["title"].lower() not in w["title"].lower():
                    continue
                if r.get("snap") and w["state"] == "min":
                    continue  # 最小化中は配置しない（開かれたら適用）
                try:
                    if r.get("topmost") is not None:
                        self.be.set_topmost(w["hwnd"], r["topmost"])
                    if r.get("opacity"):
                        self.be.set_opacity(w["hwnd"], r["opacity"])
                    if r.get("snap"):
                        self.be.set_rect(w["hwnd"], *snap_rect(r["snap"], self._monitor(-1, w["rect"])["work"]))
                    self.rule_applied.add(key)
                    count += 1
                except BackendError as e:
                    self.rules_error = str(e)
                    self.rule_applied.add(key)  # 失敗を繰り返さない
        return count

    # ---- ホットキーからの操作（今、前面にあるウィンドウが対象）
    def foreground_action(self, name):
        if name == "rescue":
            return self.rescue()
        if name == "focus":
            return self.focus_restore() if self.minimized_stack else self.focus_mode()
        fg = self.be.foreground()
        if not fg:
            return None
        w = self._find(fg)
        if name == "topmost":
            self.be.set_topmost(fg, not w["topmost"])
        elif name == "opacity":
            cycle = [100, 80, 60, 40]
            cur = min(cycle, key=lambda c: abs(c - w["opacity"]))
            self.be.set_opacity(fg, cycle[(cycle.index(cur) + 1) % len(cycle)])
        elif name == "center":
            self.center(fg)
        elif name in ("left", "right"):
            self.snap(fg, name)


class HotkeyRunner:
    """RegisterHotKey を専用スレッドで管理する（Windows + win32 バックエンドのみ）"""

    def __init__(self, manager):
        self.m, self.thread, self.tid, self.status = manager, None, None, []

    def supported(self):
        return os.name == "nt" and self.m.be.name == "win32"

    def enabled(self):
        return self.m.setting("hotkeys", "0") == "1"

    def info(self):
        ok = {s["name"]: s["ok"] for s in self.status}
        return {"supported": self.supported(), "enabled": self.enabled(),
                "bindings": [{"name": n, "keys": k, "label": d, "ok": ok.get(n)} for n, k, vk, d in HOTKEYS]}

    def start(self):
        if not self.supported() or (self.thread and self.thread.is_alive()):
            return
        ready = threading.Event()
        self.thread = threading.Thread(target=self._run, args=(ready,), daemon=True)
        self.thread.start()
        ready.wait(3)

    def stop(self):
        if self.thread and self.thread.is_alive() and self.tid:
            ctypes.windll.user32.PostThreadMessageW(self.tid, 0x12, 0, 0)  # WM_QUIT
            self.thread.join(2)
        self.thread, self.tid, self.status = None, None, []

    def _run(self, ready):
        from ctypes import wintypes as wt
        u = ctypes.windll.user32
        self.tid = ctypes.windll.kernel32.GetCurrentThreadId()
        msg = wt.MSG()
        u.PeekMessageW(ctypes.byref(msg), None, 0x400, 0x400, 0)  # メッセージキューを作る
        status = []
        for i, (name, keys, vk, desc) in enumerate(HOTKEYS, 1):
            status.append({"name": name, "ok": bool(u.RegisterHotKey(None, i, 0x2 | 0x1 | 0x4000, vk))})  # CTRL|ALT|NOREPEAT
        self.status = status
        ready.set()
        while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == 0x312:  # WM_HOTKEY
                try:
                    self.m.foreground_action(HOTKEYS[msg.wParam - 1][0])
                except Exception:
                    traceback.print_exc()
        for i in range(1, len(HOTKEYS) + 1):
            u.UnregisterHotKey(None, i)

    def apply_setting(self):
        self.start() if self.enabled() else self.stop()


class RuleWatcher(threading.Thread):
    def __init__(self, manager, interval=2.0):
        super().__init__(daemon=True)
        self.m, self.interval = manager, interval

    def run(self):
        while True:
            time.sleep(self.interval)
            try:
                self.m.apply_rules()
            except Exception as e:
                self.m.rules_error = f"{type(e).__name__}: {e}"


# ================================================================ API
def dispatch(m, path, b):
    """POSTされたJSON b を処理し、結果(dict)を返す"""
    if path == "/api/windows/action":
        return m.action(b.get("hwnds", []), b.get("action"), b.get("value"))
    if path == "/api/windows/rect":
        return m.set_rect(int(b["hwnd"]), b["l"], b["t"], b["w"], b["h"])
    if path == "/api/windows/snap":
        return m.snap(int(b["hwnd"]), b.get("preset"), int(b.get("monitor", -1)), int(b.get("margin", 0)))
    if path == "/api/windows/move_monitor":
        return m.move_to_monitor(int(b["hwnd"]), int(b["monitor"]))
    if path == "/api/arrange":
        return m.arrange(b.get("hwnds", []), b.get("mode", "grid"), int(b.get("monitor", -1)), int(b.get("gap", 8)),
                         int(b.get("margin", 0)), int(b.get("cols", 0)), float(b.get("ratio", 0.6)), bool(b.get("preview")))
    if path == "/api/rescue":
        return m.rescue()
    if path == "/api/focus":
        return m.focus_mode(b.get("keep", []))
    if path == "/api/focus/restore":
        return m.focus_restore()
    if path == "/api/reset_all":
        return m.reset_all()
    if path == "/api/layouts/save":
        return m.save_layout(b.get("name"), b.get("hwnds"))
    if path == "/api/layouts/apply":
        return m.apply_layout(b.get("name"))
    if path == "/api/layouts/delete":
        return m.delete_layout(b.get("name"))
    if path == "/api/rules/add":
        return m.add_rule(b)
    if path == "/api/rules/delete":
        return m.delete_rule(b["id"])
    if path == "/api/rules/toggle":
        return m.update_rule(b["id"], enabled=bool(b.get("enabled")))
    if path == "/api/rules/enabled":
        m.set_setting("rules_enabled", "1" if b.get("on") else "0")
        return {"ok": True}
    if path == "/api/hotkeys":
        if not m.hotkeys.supported():
            raise BackendError("ホットキーはWindows（本番モード）でのみ使えます")
        m.set_setting("hotkeys", "1" if b.get("on") else "0")
        m.hotkeys.apply_setting()
        return {"ok": True}
    if path == "/api/_mock/spawn" and m.be.name == "mock":
        return {"ok": True, "hwnd": m.be.spawn(b.get("title", "新しいウィンドウ"), b.get("process", "app.exe"))}
    raise KeyError(path)


PAGE = r"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ウィンドウマネージャー</title>
<style>
:root{--bg:#f1f4f9;--card:#fff;--ink:#1f2933;--sub:#6b7785;--line:#dde3ec;--accent:#0b7a75;--hi:#e6f4f3;--warn:#b45309;--err:#c0392b}
@media (prefers-color-scheme:dark){:root{--bg:#12151c;--card:#1c212c;--ink:#e8ecf4;--sub:#a4adc2;--line:#363d4e;--accent:#2dd4bf;--hi:#16302f}}
*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,"Hiragino Sans","Yu Gothic",Meiryo,sans-serif;background:var(--bg);color:var(--ink);font-size:14px}
header{display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding:10px 16px;background:var(--card);border-bottom:1px solid var(--line)}
header h1{font-size:16px;margin:0;margin-right:8px}
input,select,button,textarea{font:inherit;padding:6px 10px;border:1px solid var(--line);border-radius:7px;background:var(--card);color:var(--ink)}
button{cursor:pointer}button:disabled{opacity:.5;cursor:default}
button.primary{background:var(--accent);color:#fff;border-color:var(--accent)}
button.danger{color:var(--err)}button.mini{padding:3px 8px;font-size:12px}
.demo{background:#fef3c7;color:#92400e;padding:6px 16px;font-size:13px}
main{display:grid;grid-template-columns:minmax(0,1.5fr) minmax(320px,1fr);gap:14px;padding:14px 16px;max-width:1500px;margin:0 auto}
@media(max-width:1000px){main{grid-template-columns:1fr}}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px}
.bar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:8px}
.sp{flex:1}.sub{color:var(--sub);font-size:12px}
table{width:100%;border-collapse:collapse}
th{font-size:12px;color:var(--sub);font-weight:500;text-align:left;padding:4px 6px}
td{padding:6px;border-top:1px solid var(--line);vertical-align:middle}
tr.sel td{background:var(--hi)}
.ttl{max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ttl small{display:block;color:var(--sub)}
.tag{font-size:11px;padding:1px 7px;border-radius:99px;background:var(--line);color:var(--sub)}
.pin{background:none;border:1px solid var(--line);font-size:15px;padding:2px 7px;opacity:.55}.pin.on{opacity:1;background:var(--hi);border-color:var(--accent)}
.order{display:inline-block;min-width:18px;height:18px;border-radius:9px;background:var(--accent);color:#fff;font-size:11px;text-align:center;line-height:18px;margin-left:4px}
.tabs{display:flex;gap:4px;margin-bottom:10px;flex-wrap:wrap}
.tabs button{border-radius:99px;padding:5px 12px}.tabs button.on{background:var(--accent);color:#fff;border-color:var(--accent)}
.modes{display:grid;grid-template-columns:repeat(5,1fr);gap:6px;margin-bottom:10px}
.modes button{display:grid;gap:2px;padding:8px 2px;text-align:center;font-size:12px}.modes button b{font-size:20px}
.modes button.on{background:var(--hi);border-color:var(--accent)}
.fields{display:grid;grid-template-columns:repeat(2,1fr);gap:8px;margin-bottom:10px}
label.f{display:grid;gap:3px;font-size:12px;color:var(--sub)}label.f input,label.f select{color:var(--ink)}
svg.pv{width:100%;background:var(--bg);border:1px solid var(--line);border-radius:8px;margin-bottom:10px}
.list>div{display:flex;gap:8px;align-items:center;padding:6px 0;border-top:1px solid var(--line)}
.list>div .grow{flex:1;min-width:0}
dialog{border:none;border-radius:12px;padding:16px;width:min(520px,94vw);background:var(--card);color:var(--ink)}
dialog::backdrop{background:rgba(10,14,25,.5)}
.snap{display:grid;grid-template-columns:repeat(3,1fr);gap:4px;margin:6px 0}
.row{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin:6px 0}
#toast{position:fixed;left:50%;bottom:22px;transform:translateX(-50%);background:#1f2933;color:#fff;padding:9px 16px;border-radius:99px;font-size:13px;opacity:0;transition:.2s;pointer-events:none;z-index:9}
#toast.show{opacity:.95}
.ok{color:#16a34a}.ng{color:var(--err)}
</style></head><body>
<header>
  <h1>🪟 ウィンドウマネージャー</h1>
  <button id="bRefresh">更新</button><button id="bRescue" title="画面外に出てしまったウィンドウを、画面の中央へ戻します">画面外を回収</button>
  <button id="bFocus" title="選択中と前面のウィンドウ以外を最小化します">集中モード</button>
  <button id="bFocusBack" disabled>元に戻す</button>
  <button id="bReset" title="最前面・透明度をすべて解除します">設定を一括解除</button>
</header>
<div class="demo" id="demo" hidden>デモモードです（模擬ウィンドウを操作しています）。実際のウィンドウはWindowsで起動したときに操作できます。</div>
<main>
  <section class="card">
    <div class="bar">
      <input id="q" type="search" placeholder="🔍 タイトル・プロセス名で絞り込み" style="flex:1;min-width:160px">
      <select id="sort"><option value="z">前面順</option><option value="process">プロセス名順</option><option value="title">タイトル順</option></select>
      <label class="sub"><input type="checkbox" id="showMin" checked> 最小化も表示</label>
    </div>
    <div class="bar" id="bulk" hidden>
      <b id="nsel"></b><button class="mini" id="bClear">選択解除</button><span class="sp"></span>
      <button class="mini" id="bTopOn">📌 最前面ON</button><button class="mini" id="bTopOff">最前面OFF</button>
      <select id="bulkOp" title="透明度"><option value="">透明度…</option><option>100</option><option>90</option><option>80</option><option>70</option><option>60</option><option>50</option><option>40</option></select>
      <button class="mini" id="bMin">最小化</button><button class="mini" id="bRestore">元に戻す</button>
    </div>
    <table><thead><tr><th></th><th></th><th>ウィンドウ</th><th>状態</th><th>透明度</th><th></th></tr></thead><tbody id="tbody"></tbody></table>
    <div class="sub" id="empty" hidden style="padding:16px;text-align:center">該当するウィンドウがありません</div>
  </section>
  <section>
    <div class="tabs" id="tabs"></div>
    <div class="card" id="panel"></div>
  </section>
</main>
<dialog id="dlg"></dialog><div id="toast"></div>
<script>
const $ = s => document.querySelector(s);
const NS = 'http://www.w3.org/2000/svg';
function h(tag, props = {}, ...kids){
  const e = document.createElement(tag);
  for(const [k,v] of Object.entries(props)){ if(v == null || v === false) continue;
    if(k === 'class') e.className = v; else if(k === 'checked' || k === 'value' || k === 'disabled' || k === 'selected') e[k] = v;
    else if(k.startsWith('on')) e[k] = v; else e.setAttribute(k, v); }
  for(const c of kids.flat()) if(c != null) e.append(c.nodeType ? c : document.createTextNode(c));
  return e;
}
const svg = (tag, a = {}, txt) => { const e = document.createElementNS(NS, tag); for(const [k,v] of Object.entries(a)) e.setAttribute(k, v); if(txt != null) e.textContent = txt; return e; };
const lsGet = k => { try{ return localStorage.getItem(k); }catch(e){ return null; } };
const lsSet = (k,v) => { try{ localStorage.setItem(k,v); }catch(e){} };
function toast(m){ const t = $('#toast'); t.textContent = m; t.classList.add('show'); clearTimeout(toast.t); toast.t = setTimeout(() => t.classList.remove('show'), 2200); }
async function api(path, body){ try{ const r = await fetch(path, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body || {})}); return await r.json(); }catch(e){ return {ok:false, error:String(e)}; } }
function report(r, msg){ if(r.ok === false) toast(r.error || '失敗しました'); else if(r.errors && r.errors.length) toast(`${r.done}件成功 / ${r.errors[0]}`); else if(msg) toast(msg.replace('{n}', r.done)); }

let S = {windows:[], monitors:[], layouts:[], rules:[], hotkeys:{bindings:[]}, snap_presets:[]}, sel = [], tab = lsGet('wm.tab') || 'arrange', busy = false, lastKey = '', lastSide = '';
let AP = Object.assign({mode:'grid', monitor:-1, gap:8, margin:0, cols:0, ratio:60}, JSON.parse(lsGet('wm.ap') || '{}'));
const MODES = [['grid','⊞','グリッド'],['columns','▥','横に並べる'],['rows','☰','縦に積む'],['main','◧','メイン＋サブ'],['cascade','❐','カスケード']];
const PRESET_LABEL = {topleft:'↖',top:'↑',topright:'↗',left:'←',center:'中央',right:'→',bottomleft:'↙',bottom:'↓',bottomright:'↘',full:'全体',left3:'左1/3',center3:'中1/3',right3:'右1/3',left23:'左2/3',right23:'右2/3'};
const monLabel = m => `モニター${m.id + 1}${m.primary ? '（メイン）' : ''} ${m.rect[2]-m.rect[0]}×${m.rect[3]-m.rect[1]}`;

async function load(force){
  if(busy && !force) return;
  let d; try{ d = await (await fetch('/api/state')).json(); }catch(e){ return; }
  S = d; sel = sel.filter(x => S.windows.some(w => w.hwnd === x));
  $('#demo').hidden = !S.demo; $('#bFocusBack').disabled = !S.focus_stack;
  renderWindows(); renderSide();
}
function toggleSel(hwnd, on){ sel = sel.filter(x => x !== hwnd); if(on) sel.push(hwnd); lastKey = ''; renderWindows(); lastSide = ''; renderSide(); }

function renderWindows(){
  const q = $('#q').value.trim().toLowerCase(), sort = $('#sort').value, showMin = $('#showMin').checked;
  let list = S.windows.filter(w => (showMin || w.state !== 'min') && (!q || (w.title + ' ' + w.process).toLowerCase().includes(q)));
  if(sort === 'process') list.sort((a,b) => a.process.localeCompare(b.process) || a.title.localeCompare(b.title));
  if(sort === 'title') list.sort((a,b) => a.title.localeCompare(b.title));
  const key = JSON.stringify([list, sel]); if(key === lastKey) return; lastKey = key;
  const tb = $('#tbody'); tb.innerHTML = '';
  for(const w of list){
    const i = sel.indexOf(w.hwnd);
    const stateBtn = w.state === 'normal' ? h('button', {class:'mini', onclick:()=>act([w.hwnd],'max')}, '最大化') : h('button', {class:'mini', onclick:()=>act([w.hwnd],'restore')}, w.state === 'min' ? '復元' : '元のサイズ');
    tb.append(h('tr', {'data-hwnd':w.hwnd, class:i >= 0 ? 'sel' : ''},
      h('td', {}, h('input', {type:'checkbox', class:'selbox', checked:i >= 0, onchange:e => toggleSel(w.hwnd, e.target.checked)}), i >= 0 ? h('span', {class:'order'}, i + 1) : null),
      h('td', {}, h('button', {class:'pin' + (w.topmost ? ' on' : ''), title:'最前面に固定', onclick:()=>act([w.hwnd],'topmost', !w.topmost)}, '📌')),
      h('td', {class:'ttl', title:w.title}, w.title, h('small', {}, w.process)),
      h('td', {}, h('span', {class:'tag'}, {normal:'通常', min:'最小化', max:'最大化'}[w.state])),
      h('td', {}, h('input', {type:'range', class:'op', min:20, max:100, step:5, value:w.opacity, style:'width:80px', title:w.opacity + '%',
        onpointerdown:()=>{busy = true}, onchange:e => { busy = false; act([w.hwnd], 'opacity', +e.target.value); }})),
      h('td', {}, h('span', {style:'white-space:nowrap'}, h('button', {class:'mini', onclick:()=>act([w.hwnd],'activate')}, '前面へ'), ' ', stateBtn, ' ',
        h('button', {class:'mini edit', onclick:()=>openDlg(w.hwnd)}, '✎'), ' ', h('button', {class:'mini danger', title:'閉じる', onclick:()=>{ if(confirm(`「${w.title}」を閉じますか？`)) act([w.hwnd],'close'); }}, '✕')))));
  }
  $('#empty').hidden = list.length > 0;
  $('#bulk').hidden = !sel.length; $('#nsel').textContent = `${sel.length}件選択中`;
}
async function act(hwnds, action, value){ const r = await api('/api/windows/action', {hwnds, action, value}); report(r); await load(true); }
document.addEventListener('pointerup', () => { if(busy) setTimeout(() => { busy = false; }, 300); });

// ---- 右側パネル
const TABS = [['arrange','整列'],['layouts','レイアウト'],['rules','ルール'],['hotkeys','ホットキー']];
function renderTabs(){ const t = $('#tabs'); t.innerHTML = ''; for(const [k,l] of TABS) t.append(h('button', {class:tab === k ? 'on' : '', 'data-tab':k, onclick:()=>{ tab = k; lsSet('wm.tab', k); lastSide = ''; renderSide(); }}, l)); }
function renderSide(){
  const key = [tab, JSON.stringify(S.layouts), JSON.stringify(S.rules), S.rules_enabled, JSON.stringify(S.hotkeys), JSON.stringify(S.monitors), sel.join(), S.rules_error].join('|');
  if(key === lastSide) return; lastSide = key; renderTabs();
  const p = $('#panel'); p.innerHTML = '';
  if(tab === 'arrange') arrangePanel(p); else if(tab === 'layouts') layoutsPanel(p); else if(tab === 'rules') rulesPanel(p); else hotkeysPanel(p);
}
function numField(label, key, min, max, unit){ return h('label', {class:'f'}, label, h('input', {type:'number', min, max, value:AP[key], 'data-k':key, onchange:e => { AP[key] = +e.target.value; lsSet('wm.ap', JSON.stringify(AP)); preview(); }})); }
function arrangePanel(p){
  const modes = h('div', {class:'modes'});
  for(const [k, ic, lb] of MODES) modes.append(h('button', {class:AP.mode === k ? 'on' : '', 'data-mode':k, onclick:()=>{ AP.mode = k; lsSet('wm.ap', JSON.stringify(AP)); lastSide = ''; renderSide(); }}, h('b', {}, ic), lb));
  const mon = h('select', {id:'apMon', onchange:e => { AP.monitor = +e.target.value; lsSet('wm.ap', JSON.stringify(AP)); preview(); }},
    h('option', {value:-1, selected:AP.monitor === -1}, '自動（先頭のウィンドウがあるモニター）'), S.monitors.map(m => h('option', {value:m.id, selected:AP.monitor === m.id}, monLabel(m))));
  p.append(h('div', {class:'sub', style:'margin-bottom:8px'}, '左の一覧でウィンドウにチェックを入れた順に配置されます（メイン＋サブでは最初がメイン）。'), modes,
    h('div', {class:'fields'}, h('label', {class:'f', style:'grid-column:1/3'}, '配置するモニター', mon), numField('ウィンドウの間隔 (px)', 'gap', 0, 100), numField('画面端の余白 (px)', 'margin', 0, 300),
      AP.mode === 'grid' ? numField('列数（0=自動）', 'cols', 0, 10) : null, AP.mode === 'main' ? numField('メインの幅 (%)', 'ratio', 20, 80) : null),
    h('div', {id:'pvbox'}), h('button', {class:'primary', id:'bArrange', disabled:sel.length ? null : 'disabled', onclick:doArrange}, sel.length ? `選択した${sel.length}件を整列` : 'ウィンドウを選択してください'));
  preview();
}
const apBody = extra => Object.assign({hwnds:sel, mode:AP.mode, monitor:AP.monitor, gap:AP.gap, margin:AP.margin, cols:AP.cols, ratio:AP.ratio / 100}, extra);
async function doArrange(){ const r = await api('/api/arrange', apBody()); report(r, '{n}件を整列しました'); await load(true); preview(); }
let pvTimer;
function preview(){
  clearTimeout(pvTimer); pvTimer = setTimeout(async () => {
    const box = $('#pvbox'); if(!box) return; box.innerHTML = '';
    const mons = S.monitors; if(!mons.length) return;
    const x0 = Math.min(...mons.map(m => m.rect[0])), y0 = Math.min(...mons.map(m => m.rect[1])), x1 = Math.max(...mons.map(m => m.rect[2])), y1 = Math.max(...mons.map(m => m.rect[3]));
    const W = x1 - x0, H = y1 - y0, s = svg('svg', {class:'pv', viewBox:`0 0 ${W} ${H}`, id:'pv'});
    for(const m of mons){ s.append(svg('rect', {x:m.rect[0]-x0, y:m.rect[1]-y0, width:m.rect[2]-m.rect[0], height:m.rect[3]-m.rect[1], fill:'none', stroke:'#8a94a8', 'stroke-width':W/300}));
      s.append(svg('text', {x:m.rect[0]-x0+W/100, y:m.rect[1]-y0+H/14, 'font-size':H/16, fill:'#8a94a8'}, `モニター${m.id+1}`)); }
    if(sel.length){
      const r = await api('/api/arrange', apBody({preview:true}));
      if(r.ok){ r.plan.forEach((pl, i) => { const [l,t,rr,b] = pl.rect;
        s.append(svg('rect', {class:'pvr', x:l-x0, y:t-y0, width:rr-l, height:b-t, fill:'rgba(11,122,117,.28)', stroke:'#0b7a75', 'stroke-width':W/300}));
        s.append(svg('text', {x:(l+rr)/2-x0, y:(t+b)/2-y0+H/20, 'font-size':H/9, 'text-anchor':'middle', fill:'#0b7a75', 'font-weight':'700'}, i + 1)); }); }
    }
    box.append(s);
  }, 120);
}
function layoutsPanel(p){
  const name = h('input', {id:'lname', placeholder:'レイアウト名（例: 開発セット）', style:'flex:1'});
  p.append(h('div', {class:'sub', style:'margin-bottom:8px'}, '今のウィンドウ配置（位置・サイズ・最前面・透明度）を保存し、あとでワンクリックで元に戻せます。ウィンドウはプロセス名とタイトルで探します。'),
    h('div', {class:'row'}, name, h('button', {class:'primary', id:'bSaveLayout', onclick:async () => { const r = await api('/api/layouts/save', {name:name.value, hwnds:sel}); report(r, `${r.done}件のウィンドウ配置を保存しました`); await load(true); }}, sel.length ? `選択した${sel.length}件を保存` : '全ウィンドウを保存')));
  const list = h('div', {class:'list'});
  for(const l of S.layouts) list.append(h('div', {'data-layout':l.name}, h('div', {class:'grow'}, h('b', {}, l.name), h('div', {class:'sub'}, `${l.count}件`)),
    h('button', {class:'mini applyL', onclick:async () => { const r = await api('/api/layouts/apply', {name:l.name}); if(r.ok === false) return report(r);
      toast(`${r.done}件を配置しました` + (r.missing.length ? ` / 見つからない: ${r.missing.slice(0,2).join('、')}${r.missing.length > 2 ? ' ほか' : ''}` : '')); await load(true); }}, '適用'),
    h('button', {class:'mini danger', onclick:async () => { if(confirm(`レイアウト「${l.name}」を削除しますか？`)){ await api('/api/layouts/delete', {name:l.name}); load(true); } }}, '削除')));
  if(!S.layouts.length) list.append(h('div', {class:'sub'}, '保存したレイアウトはまだありません'));
  p.append(list);
}
function rulesPanel(p){
  p.append(h('div', {class:'sub', style:'margin-bottom:8px'}, '条件に合うウィンドウが開いたら、自動で設定を適用します（ウィンドウごとに1回だけ。あとで手動で変えても上書きしません）。'),
    h('label', {class:'row'}, h('input', {type:'checkbox', id:'rulesOn', checked:S.rules_enabled, onchange:async e => { await api('/api/rules/enabled', {on:e.target.checked}); toast(e.target.checked ? 'ルールを有効にしました' : 'ルールを無効にしました'); load(true); }}), 'ルールを有効にする'));
  if(S.rules_error) p.append(h('div', {class:'ng sub'}, '直近のエラー: ' + S.rules_error));
  const procs = [...new Set(S.windows.map(w => w.process))];
  const f = {proc:h('input', {id:'rProc', list:'procs', placeholder:'プロセス名（例: chrome.exe）'}), title:h('input', {id:'rTitle', placeholder:'タイトルに含む文字（任意）'}),
    top:h('select', {id:'rTop'}, h('option', {value:''}, '最前面: 変更しない'), h('option', {value:'1'}, '最前面にする'), h('option', {value:'0'}, '最前面にしない')),
    op:h('select', {id:'rOp'}, h('option', {value:''}, '透明度: 変更しない'), [90,80,70,60,50].map(v => h('option', {value:v}, `透明度 ${v}%`))),
    snap:h('select', {id:'rSnap'}, h('option', {value:''}, '配置: 変更しない'), S.snap_presets.map(k => h('option', {value:k}, '配置: ' + PRESET_LABEL[k])))};
  p.append(h('datalist', {id:'procs'}, procs.map(x => h('option', {value:x}))), h('div', {class:'fields'}, f.proc, f.title, f.top, f.op, f.snap,
    h('button', {class:'primary', id:'bAddRule', onclick:async () => { const r = await api('/api/rules/add', {process:f.proc.value, title:f.title.value, topmost:f.top.value === '' ? null : f.top.value === '1', opacity:f.op.value ? +f.op.value : null, snap:f.snap.value || null});
      report(r, 'ルールを追加しました'); load(true); }}, 'ルールを追加')));
  const list = h('div', {class:'list'});
  for(const r of S.rules){
    const parts = [r.topmost === true ? '最前面ON' : r.topmost === false ? '最前面OFF' : null, r.opacity ? `透明度${r.opacity}%` : null, r.snap ? `配置:${PRESET_LABEL[r.snap]}` : null].filter(Boolean).join(' / ');
    list.append(h('div', {'data-rule':r.id}, h('input', {type:'checkbox', checked:r.enabled, onchange:async e => { await api('/api/rules/toggle', {id:r.id, enabled:e.target.checked}); load(true); }}),
      h('div', {class:'grow'}, h('b', {}, [r.process, r.title && `「${r.title}」`].filter(Boolean).join(' ')), h('div', {class:'sub'}, parts)),
      h('button', {class:'mini danger', onclick:async () => { await api('/api/rules/delete', {id:r.id}); load(true); }}, '削除')));
  }
  if(!S.rules.length) list.append(h('div', {class:'sub'}, 'ルールはまだありません'));
  p.append(list);
}
function hotkeysPanel(p){
  const hk = S.hotkeys;
  p.append(h('div', {class:'sub', style:'margin-bottom:8px'}, 'どのアプリを使っていても、今前面にあるウィンドウを素早く操作できます。'),
    h('label', {class:'row'}, h('input', {type:'checkbox', id:'hkOn', checked:hk.enabled, disabled:hk.supported ? null : 'disabled', onchange:async e => { const r = await api('/api/hotkeys', {on:e.target.checked}); report(r, e.target.checked ? 'ホットキーを有効にしました' : 'ホットキーを無効にしました'); load(true); }}), 'グローバルホットキーを有効にする'));
  if(!hk.supported) p.append(h('div', {class:'sub'}, 'ホットキーはWindows（本番モード）でのみ使えます。'));
  const t = h('table'); for(const b of hk.bindings) t.append(h('tr', {}, h('td', {}, h('b', {}, b.keys)), h('td', {}, b.label), h('td', {class:b.ok === true ? 'ok' : b.ok === false ? 'ng' : 'sub'}, b.ok === true ? '登録済み' : b.ok === false ? '他で使用中' : '')));
  p.append(t);
}

// ---- 詳細ダイアログ
function openDlg(hwnd){
  const w = S.windows.find(x => x.hwnd === hwnd); if(!w) return;
  const [l,t,r,b] = w.rect, dlg = $('#dlg'); dlg.innerHTML = '';
  const F = {l:h('input',{type:'number',value:l,id:'dL'}), t:h('input',{type:'number',value:t,id:'dT'}), w:h('input',{type:'number',value:r-l,id:'dW'}), h:h('input',{type:'number',value:b-t,id:'dH'})};
  const snapBtns = ['topleft','top','topright','left','center','right','bottomleft','bottom','bottomright'].map(k => h('button', {class:'sn', 'data-p':k, onclick:async () => { const x = await api('/api/windows/snap', {hwnd, preset:k}); report(x); await load(true); dlg.close(); }}, PRESET_LABEL[k]));
  const more = ['full','left3','center3','right3','left23','right23'].map(k => h('button', {class:'mini sn', 'data-p':k, onclick:async () => { const x = await api('/api/windows/snap', {hwnd, preset:k}); report(x); await load(true); dlg.close(); }}, PRESET_LABEL[k]));
  const mon = h('select', {id:'dMon'}, S.monitors.map(m => h('option', {value:m.id, selected:m.id === w.monitor}, monLabel(m))));
  dlg.append(h('h3', {style:'margin:0 0 4px'}, w.title), h('div', {class:'sub'}, w.process),
    h('h4', {style:'margin:12px 0 4px'}, '画面に合わせて配置（モニターの作業領域）'), h('div', {class:'snap'}, snapBtns), h('div', {class:'row'}, more),
    h('h4', {style:'margin:12px 0 4px'}, '位置とサイズ（px）'),
    h('div', {class:'fields'}, h('label',{class:'f'},'左',F.l), h('label',{class:'f'},'上',F.t), h('label',{class:'f'},'幅',F.w), h('label',{class:'f'},'高さ',F.h)),
    h('div', {class:'row'}, [[1280,720],[1600,900],[1920,1080],[800,600]].map(([a,c]) => h('button', {class:'mini', onclick:()=>{ F.w.value = a; F.h.value = c; }}, `${a}×${c}`)),
      h('button', {class:'primary', id:'dApply', onclick:async () => { const x = await api('/api/windows/rect', {hwnd, l:+F.l.value, t:+F.t.value, w:+F.w.value, h:+F.h.value}); report(x, '適用しました'); await load(true); if(x.ok !== false) dlg.close(); }}, '適用')),
    h('h4', {style:'margin:12px 0 4px'}, '別のモニターへ移動'), h('div', {class:'row'}, mon, h('button', {id:'dMove', onclick:async () => { const x = await api('/api/windows/move_monitor', {hwnd, monitor:+mon.value}); report(x, '移動しました'); await load(true); dlg.close(); }}, '移動')),
    h('div', {class:'row', style:'margin-top:14px'}, h('button', {onclick:async () => { await act([hwnd], 'center'); dlg.close(); }}, '中央に寄せる'), h('span', {class:'sp'}), h('button', {onclick:()=>dlg.close()}, '閉じる')));
  dlg.showModal();
}

// ---- 上部ボタン・入力
$('#bRefresh').onclick = () => load(true);
$('#bRescue').onclick = async () => { const r = await api('/api/rescue'); toast(r.done ? `${r.done}件を画面内に戻しました` : '画面外のウィンドウはありません'); load(true); };
$('#bFocus').onclick = async () => { const r = await api('/api/focus', {keep:sel}); toast(`${r.done}件を最小化しました`); load(true); };
$('#bFocusBack').onclick = async () => { const r = await api('/api/focus/restore'); toast(`${r.done}件を元に戻しました`); load(true); };
$('#bReset').onclick = async () => { if(!confirm('すべてのウィンドウの最前面と透明度を解除します。よろしいですか？')) return; const r = await api('/api/reset_all'); toast(`${r.done}件を解除しました`); load(true); };
$('#bClear').onclick = () => { sel = []; lastKey = ''; lastSide = ''; renderWindows(); renderSide(); };
$('#bTopOn').onclick = () => act(sel, 'topmost', true); $('#bTopOff').onclick = () => act(sel, 'topmost', false);
$('#bMin').onclick = () => act(sel, 'min'); $('#bRestore').onclick = () => act(sel, 'restore');
$('#bulkOp').onchange = e => { if(e.target.value){ act(sel, 'opacity', +e.target.value); e.target.value = ''; } };
for(const id of ['q','sort','showMin']) $('#' + id).addEventListener('input', () => { lastKey = ''; renderWindows(); });
load(true); setInterval(() => { if(!document.hidden && !$('#dlg').open) load(); }, 2000);
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    manager = None

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _guard(self, post=False):
        try:
            host = urlparse("//" + (self.headers.get("Host") or "")).hostname
        except ValueError:
            host = None
        origin = self.headers.get("Origin")
        if host not in ALLOWED_HOSTS or (post and origin and urlparse(origin).netloc != self.headers.get("Host")):
            self._send(403, "Forbidden", "text/plain; charset=utf-8")  # DNS rebinding / 他サイトからの操作を拒否
            return False
        return True

    def do_GET(self):
        if not self._guard():
            return
        path = urlparse(self.path).path
        if path == "/":
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if path == "/api/state":
            try:
                return self._send(200, json.dumps(self.manager.state(), ensure_ascii=False))
            except BackendError as e:
                return self._send(200, json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        self._send(404, "Not Found", "text/plain; charset=utf-8")

    def do_POST(self):
        if not self._guard(post=True):
            return
        try:
            size = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(size).decode("utf-8")) if size else {}
            assert isinstance(body, dict)
        except (ValueError, AssertionError):
            return self._send(400, '{"ok":false,"error":"invalid"}')
        try:
            result = dispatch(self.manager, urlparse(self.path).path, body)
        except KeyError:
            return self._send(404, '{"ok":false,"error":"not found"}')
        except (BackendError, ValueError, TypeError) as e:
            result = {"ok": False, "error": str(e) if isinstance(e, BackendError) else "入力が正しくありません"}
        self._send(200, json.dumps(result, ensure_ascii=False))

    def log_message(self, fmt, *args):
        pass


def make_backend(kind):
    if kind == "mock" or (kind == "auto" and os.name != "nt"):
        return MockBackend()
    if os.name != "nt":
        raise SystemExit("win32 バックエンドはWindowsでのみ使えます（--backend mock で画面を試せます）")
    return Win32Backend()


def selftest(be):
    print(f"バックエンド: {be.name}")
    for m in be.monitors():
        print(f"  モニター{m['id'] + 1}{'(メイン)' if m['primary'] else ''}: {m['rect']}  作業領域: {m['work']}")
    wins = be.windows()
    print(f"ウィンドウ: {len(wins)}件（上位10件）")
    for w in wins[:10]:
        print(f"  [{w['hwnd']}] {w['process']:<18} {w['state']:<6} top={int(w['topmost'])} op={w['opacity']:>3}% {w['rect']}  {w['title'][:40]}")
    print("前面:", be.foreground())
    print("→ 一覧が実際の画面と一致していれば、取得部分は正常です（この確認では何も変更していません）。")


def main():
    ap = argparse.ArgumentParser(description="ウィンドウマネージャー")
    ap.add_argument("--port", type=int, default=8407)
    ap.add_argument("--backend", choices=["auto", "win32", "mock"], default="auto")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    be = make_backend(a.backend)
    if a.selftest:
        return selftest(be)
    mgr = Manager(be, DB_PATH)
    Handler.manager = mgr
    RuleWatcher(mgr).start()
    mgr.hotkeys.apply_setting()
    try:
        server = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    except OSError as e:
        raise SystemExit(f"ポート {a.port} を使えません（{e}）")
    print(f"ウィンドウマネージャーを起動しました [{be.name}]: http://127.0.0.1:{a.port}  (終了は Ctrl+C)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n終了します")
    finally:
        mgr.hotkeys.stop()


if __name__ == "__main__":
    main()
