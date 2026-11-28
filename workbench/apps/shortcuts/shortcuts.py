#!/usr/bin/env python3
"""ショートカット - グローバルなショートカットキーで、日時入力・定型文入力・アプリ起動などをまとめて管理する (Windows用)

標準ライブラリのみで動作します（Win32 APIは ctypes）。

    python3 shortcuts.py                 # http://127.0.0.1:8408 を開いて設定
    python3 shortcuts.py --port 9000
    python3 shortcuts.py --selftest      # 書式の確認（入力は行いません）
    python3 shortcuts.py --backend mock  # Windows以外で画面を試す（実行内容はログに記録するだけ）

登録できるショートカットの種類
    日時入力             今日の日付・時刻などを指定書式で入力        例) yyyy/MM/dd → 2026/11/28
    定型文入力           決まった文章を入力（{date:書式} で日付も差し込み）
    開く                 ファイル・フォルダ・URLを開く
    プログラム起動       プログラムを引数つきで起動
    クリップボード加工   コピーした文字を、書式なし・1行化・半角化などして貼り付け

使い方
    1. 画面でショートカットを登録します。初期設定で有効になっています（全体のON/OFFは画面上部のスイッチ）。
    2. 入力したい場所（メモ帳・Excel・ブラウザなど）をクリックしてショートカットを押します。
    ※ このプログラムは起動したままにしてください（画面のブラウザは閉じてかまいません）。
    ※ 管理者権限で動いているアプリに入力するには、このツールも管理者として起動してください。

日時の書式（例）
    yyyy/MM/dd → 2026/11/28    yyyy年M月d日(ddd) → 2026年11月28日(土)    ggee年M月d日 → 令和8年11月28日
    HH:mm → 14:30    '' で囲むとそのまま出力    \\n は改行、\\t はタブ
"""
import argparse
import calendar
import ctypes
import datetime as dt
import json
import os
import re
import subprocess
import threading
import unicodedata
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import webbrowser
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("SHORTCUTS_CONFIG") or os.path.join(HERE, "shortcuts.json")
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}
WD_JA = "月火水木金土日"
WD_JA_LONG = [c + "曜日" for c in WD_JA]
WD_EN = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
WD_EN_LONG = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
MON_EN = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
MON_EN_LONG = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
BASES = {"today": "今日", "month_start": "月初", "month_end": "月末", "week_start": "今週の月曜", "week_end": "今週の金曜",
         "next_month_start": "翌月初", "next_month_end": "翌月末", "year_start": "年初", "year_end": "年末"}


class ConfigError(Exception):
    pass


# ================================================================ 日時の書式（OSに依存しない）
ERAS = [(dt.date(2019, 5, 1), "令和", "R"), (dt.date(1989, 1, 8), "平成", "H"), (dt.date(1926, 12, 25), "昭和", "S"),
        (dt.date(1912, 7, 30), "大正", "T"), (dt.date(1868, 1, 25), "明治", "M")]


def era_of(d):
    for start, name, ab in ERAS:
        if d >= start:
            year = d.year - start.year + 1
            return name, ab, year
    return "西暦", "", d.year


TOKEN = re.compile(r"'((?:[^']|'')*)'|\\(.)|(yyyy|yy|MMMM|MMM|MM|M|dddd|ddd|dd|d|EEEE|EEE|HH|H|hh|h|mm|m|ss|s|tt|aa|gg|g|ee|e|SSS)")


def format_dt(fmt, now):
    """.NET/Excel風の書式で日時を文字列にする。'...' は文字そのまま、\\n \\t \\\\ はエスケープ"""
    era, era_ab, era_year = era_of(now.date())

    def rep(m):
        if m.group(1) is not None:
            return m.group(1).replace("''", "'") or "'"
        if m.group(2) is not None:
            return {"n": "\n", "t": "\t"}.get(m.group(2), m.group(2))
        t, wd = m.group(3), now.weekday()
        h12 = now.hour % 12 or 12
        return {
            "yyyy": f"{now.year:04d}", "yy": f"{now.year % 100:02d}", "MMMM": MON_EN_LONG[now.month - 1], "MMM": MON_EN[now.month - 1],
            "MM": f"{now.month:02d}", "M": str(now.month), "dddd": WD_JA_LONG[wd], "ddd": WD_JA[wd], "dd": f"{now.day:02d}", "d": str(now.day),
            "EEEE": WD_EN_LONG[wd], "EEE": WD_EN[wd], "HH": f"{now.hour:02d}", "H": str(now.hour), "hh": f"{h12:02d}", "h": str(h12),
            "mm": f"{now.minute:02d}", "m": str(now.minute), "ss": f"{now.second:02d}", "s": str(now.second),
            "tt": "AM" if now.hour < 12 else "PM", "aa": "午前" if now.hour < 12 else "午後", "SSS": f"{now.microsecond // 1000:03d}",
            "gg": era, "g": era_ab, "ee": "元" if era_year == 1 else str(era_year), "e": str(era_year),
        }[t]

    return TOKEN.sub(rep, fmt)


def is_bizday(d):
    return d.weekday() < 5  # 祝日は考慮しません（土日のみ）


def add_biz(d, n):
    step = 1 if n >= 0 else -1
    while n:
        d += dt.timedelta(days=step)
        if is_bizday(d):
            n -= step
    return d


def month_end(y, m):
    return dt.date(y, m, calendar.monthrange(y, m)[1])


def base_date(base, today):
    y, m = today.year, today.month
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
    return {
        "today": today, "month_start": today.replace(day=1), "month_end": month_end(y, m),
        "week_start": today - dt.timedelta(days=today.weekday()), "week_end": today - dt.timedelta(days=today.weekday()) + dt.timedelta(days=4),
        "next_month_start": dt.date(ny, nm, 1), "next_month_end": month_end(ny, nm),
        "year_start": dt.date(y, 1, 1), "year_end": dt.date(y, 12, 31),
    }[base]


def compute(entry, now=None):
    """登録内容 {format, base, offset, unit} から、入力する文字列を作る"""
    now = now or dt.datetime.now()
    d = base_date(entry.get("base", "today"), now.date())
    off = int(entry.get("offset", 0) or 0)
    if off:
        unit = entry.get("unit", "day")
        if unit == "biz":
            d = add_biz(d, off)
        elif unit == "week":
            d += dt.timedelta(weeks=off)
        elif unit == "month":
            idx = d.year * 12 + d.month - 1 + off
            y, m = divmod(idx, 12)
            d = d.replace(year=y, month=m + 1, day=min(d.day, calendar.monthrange(y, m + 1)[1]))
        else:
            d += dt.timedelta(days=off)
    return format_dt(entry.get("format", ""), dt.datetime.combine(d, now.time()))


TYPES = {"datetime": "日時入力", "text": "定型文入力", "open": "開く", "run": "プログラム起動", "clip": "クリップボード加工"}
CLIP_OPS = {"plain": "書式を外して貼り付け（プレーンテキスト）", "trim": "前後の空白・改行を除いて貼り付け",
            "oneline": "改行を除いて1行にして貼り付け", "nfkc": "全角英数→半角、半角カナ→全角にして貼り付け"}
PLACEHOLDER = re.compile(r"\{\{|\}\}|\{(date|bizdate)([+-]\d+)?:([^{}]*)\}|\{clip\}")


def expand_text(text, now, clip=""):
    """定型文の差し込み: {date:書式} {date+3:書式} {bizdate+1:書式} {clip}（クリップボード）。{{ }} は波括弧そのもの"""
    def rep(m):
        t = m.group(0)
        if t in ("{{", "}}"):
            return t[0]
        if t == "{clip}":
            return clip
        d = now.date()
        off = int(m.group(2) or 0)
        d = add_biz(d, off) if m.group(1) == "bizdate" else d + dt.timedelta(days=off)
        return format_dt(m.group(3), dt.datetime.combine(d, now.time()))
    return PLACEHOLDER.sub(rep, text)


def transform_clip(op, text):
    if op == "trim":
        return text.strip()
    if op == "oneline":
        return re.sub(r"\s*[\r\n]+\s*", " ", text).strip()
    if op == "nfkc":
        return unicodedata.normalize("NFKC", text)
    return text


# ================================================================ ショートカット文字列
MODS = {"ctrl": 0x2, "control": 0x2, "alt": 0x1, "shift": 0x4, "win": 0x8}
NAMED_VK = {"space": 0x20, "enter": 0x0D, "tab": 0x09, "insert": 0x2D, "delete": 0x2E, "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
            "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27, ";": 0xBA, "=": 0xBB, ",": 0xBC, "-": 0xBD, ".": 0xBE, "/": 0xBF, "`": 0xC0,
            "[": 0xDB, "\\": 0xDC, "]": 0xDD, "'": 0xDE}
FORBIDDEN = {("ctrl", "c"), ("ctrl", "v"), ("ctrl", "x"), ("ctrl", "z"), ("ctrl", "a"), ("ctrl", "s"), ("ctrl", "y")}


def parse_hotkey(s):
    """'ctrl+alt+d' -> (修飾キーのビット, 仮想キーコード, 正規化した文字列)"""
    parts = [p.strip().lower() for p in re.split(r"\+", (s or "").replace(" ", "")) if p.strip()] if "++" not in s else None
    if not parts:
        raise ConfigError("ショートカットの形式が正しくありません（例: Ctrl+Alt+D）")
    key, mods = parts[-1], parts[:-1]
    if any(m not in MODS for m in mods) or key in MODS:
        raise ConfigError("ショートカットの形式が正しくありません（例: Ctrl+Alt+D）")
    if not mods:
        raise ConfigError("Ctrl・Alt・Shift・Win のいずれかを組み合わせてください")
    bits = 0
    for m in mods:
        bits |= MODS[m]
    if len(set(mods)) != len(mods):
        raise ConfigError("同じ修飾キーが重複しています")
    if re.fullmatch(r"[a-z0-9]", key):
        vk = ord(key.upper())
    elif re.fullmatch(r"f([1-9]|1[0-9]|2[0-4])", key):
        vk = 0x70 + int(key[1:]) - 1
    elif key in NAMED_VK:
        vk = NAMED_VK[key]
    else:
        raise ConfigError(f"使えないキーです: {key}")
    if len(mods) == 1 and (mods[0], key) in FORBIDDEN:
        raise ConfigError("コピー・貼り付けなどの基本ショートカットとは重なる設定にできません")
    if bits == 0x4 and re.fullmatch(r"[a-z0-9]", key):
        raise ConfigError("Shift だけの組み合わせは通常の文字入力と重なるため使えません")
    order = [n for n, b in (("Ctrl", 2), ("Alt", 1), ("Shift", 4), ("Win", 8)) if bits & b]
    label = key.upper() if len(key) == 1 or re.fullmatch(r"f\d+", key) else key.capitalize()
    return bits, vk, "+".join(order + [label])


# ================================================================ Win32 構造体
class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_uint16), ("wScan", ctypes.c_uint16), ("dwFlags", ctypes.c_uint32),
                ("time", ctypes.c_uint32), ("dwExtraInfo", ctypes.c_size_t)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_int32), ("dy", ctypes.c_int32), ("mouseData", ctypes.c_uint32), ("dwFlags", ctypes.c_uint32),
                ("time", ctypes.c_uint32), ("dwExtraInfo", ctypes.c_size_t)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.c_uint32), ("u", _INPUTUNION)]


# ================================================================ 入力バックエンド
class MockTyper:
    name = "mock"

    def __init__(self):
        self.typed, self.actions, self.clip = [], [], "（デモのクリップボード）  Hello　ＷＯＲＬＤ\n2行目 "

    def type_text(self, text, mode="type"):
        self.typed.append(text)

    def paste_text(self, text):
        self.typed.append(text)

    def get_clip(self):
        return self.clip

    def open_target(self, target):
        self.actions.append(("open", target))

    def run_command(self, command):
        self.actions.append(("run", command))


class Win32Typer:
    name = "win32"
    KEYUP, UNICODE = 0x2, 0x4
    MOD_VKS = [0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5, 0x5B, 0x5C]  # L/R Shift, Ctrl, Alt, Win

    def __init__(self):
        from ctypes import wintypes as wt
        self.u = u = ctypes.WinDLL("user32", use_last_error=True)
        self.k = k = ctypes.WinDLL("kernel32", use_last_error=True)
        u.SendInput.restype, u.SendInput.argtypes = wt.UINT, [wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
        u.GetAsyncKeyState.restype, u.GetAsyncKeyState.argtypes = ctypes.c_short, [ctypes.c_int]
        u.OpenClipboard.restype, u.OpenClipboard.argtypes = wt.BOOL, [wt.HWND]
        u.GetClipboardData.restype, u.GetClipboardData.argtypes = wt.HANDLE, [wt.UINT]
        u.SetClipboardData.restype, u.SetClipboardData.argtypes = wt.HANDLE, [wt.UINT, wt.HANDLE]
        k.GlobalAlloc.restype, k.GlobalAlloc.argtypes = wt.HANDLE, [wt.UINT, ctypes.c_size_t]
        k.GlobalLock.restype, k.GlobalLock.argtypes = ctypes.c_void_p, [wt.HANDLE]
        k.GlobalUnlock.argtypes = [wt.HANDLE]
        self.lock = threading.Lock()

    def _send(self, events):
        arr = (INPUT * len(events))(*events)
        sent = self.u.SendInput(len(events), arr, ctypes.sizeof(INPUT))
        if sent != len(events):
            raise OSError(f"SendInput が一部しか送れませんでした（{sent}/{len(events)}）。管理者権限のウィンドウには入力できません")

    @staticmethod
    def _key(vk=0, scan=0, flags=0):
        i = INPUT(type=1)
        i.ki = KEYBDINPUT(vk, scan, flags, 0, 0)
        return i

    def _wait_modifiers_released(self, timeout=1.5):
        """ショートカットを押している指が離れるまで待つ（押したままだと Ctrl+文字 として届いてしまうため）"""
        end = time.time() + timeout
        while time.time() < end:
            if not any(self.u.GetAsyncKeyState(vk) & 0x8000 for vk in self.MOD_VKS):
                return
            time.sleep(0.02)
        ev = [self._key(vk, 0, self.KEYUP) for vk in self.MOD_VKS if self.u.GetAsyncKeyState(vk) & 0x8000]
        if ev:
            self._send(ev)  # 離されなければ、こちらで離したことにする

    def _type_unicode(self, text):
        events = []
        for ch in text:
            if ch == "\n":
                events += [self._key(0x0D), self._key(0x0D, 0, self.KEYUP)]
            elif ch == "\t":
                events += [self._key(0x09), self._key(0x09, 0, self.KEYUP)]
            else:
                data = ch.encode("utf-16-le")
                for j in range(0, len(data), 2):  # サロゲートペアも1単位ずつ送る
                    unit = int.from_bytes(data[j:j + 2], "little")
                    events += [self._key(0, unit, self.UNICODE), self._key(0, unit, self.UNICODE | self.KEYUP)]
        for i in range(0, len(events), 200):
            self._send(events[i:i + 200])

    def _get_clip(self):
        if not self.u.OpenClipboard(None):
            return None
        try:
            h = self.u.GetClipboardData(13)  # CF_UNICODETEXT
            if not h:
                return None
            p = self.k.GlobalLock(h)
            try:
                return ctypes.wstring_at(p) if p else None
            finally:
                self.k.GlobalUnlock(h)
        finally:
            self.u.CloseClipboard()

    def _set_clip(self, text):
        for _ in range(10):
            if self.u.OpenClipboard(None):
                break
            time.sleep(0.03)
        else:
            raise OSError("クリップボードを開けませんでした")
        try:
            self.u.EmptyClipboard()
            data = (text + "\0").encode("utf-16-le")
            h = self.k.GlobalAlloc(0x2, len(data))  # GMEM_MOVEABLE
            p = self.k.GlobalLock(h)
            ctypes.memmove(p, data, len(data))
            self.k.GlobalUnlock(h)
            if not self.u.SetClipboardData(13, h):
                raise OSError("クリップボードへの書き込みに失敗しました")
        finally:
            self.u.CloseClipboard()

    def get_clip(self):
        return self._get_clip()

    def paste_text(self, text):
        with self.lock:
            self._wait_modifiers_released()
            self._set_clip(text)
            self._send([self._key(0xA2), self._key(0x56), self._key(0x56, 0, self.KEYUP), self._key(0xA2, 0, self.KEYUP)])

    def open_target(self, target):
        target = os.path.expandvars(target.strip().strip('"'))
        if re.match(r"^https?://", target, re.I):
            webbrowser.open(target)
            return
        try:
            os.startfile(target)  # 関連付けられたアプリで開く（フォルダはエクスプローラー）
        except OSError as e:
            raise OSError(f"開けませんでした: {target}（{e.strerror or e}）")

    def run_command(self, command):
        try:  # シェルは使わず、そのまま起動する
            subprocess.Popen(os.path.expandvars(command), shell=False, close_fds=True, creationflags=0x00000008 | 0x00000200)
        except (OSError, ValueError) as e:
            raise OSError(f"起動できませんでした: {command}（{getattr(e, 'strerror', None) or e}）")

    def type_text(self, text, mode="type"):
        with self.lock:
            self._wait_modifiers_released()
            if mode == "paste":
                old = self._get_clip()
                self._set_clip(text)
                self._send([self._key(0xA2), self._key(0x56), self._key(0x56, 0, self.KEYUP), self._key(0xA2, 0, self.KEYUP)])
                time.sleep(0.25)
                if old is not None:  # 文字のクリップボードだけ元に戻す（画像・ファイルは戻せません）
                    self._set_clip(old)
            else:
                self._type_unicode(text)


# ================================================================ 設定の保存と管理
def _dt(name, hotkey, fmt, offset=0):
    return {"name": name, "hotkey": hotkey, "type": "datetime", "format": fmt, "base": "today", "offset": offset, "unit": "day", "enabled": True}


DEFAULT_ENTRIES = [
    _dt("日付", "Ctrl+Alt+D", "yyyy/MM/dd"),
    _dt("日付+曜日", "Ctrl+Alt+J", "yyyy年M月d日(ddd)"),
    _dt("時刻", "Ctrl+Alt+H", "HH:mm"),
    _dt("日時（ファイル名向け）", "Ctrl+Alt+N", "yyyyMMdd_HHmm"),
    _dt("明日の日付", "Ctrl+Alt+M", "yyyy/MM/dd", 1),
    {"name": "プレーンテキストで貼り付け", "hotkey": "Ctrl+Alt+V", "type": "clip", "op": "plain", "enabled": True},
    {"name": "あいさつ定型文", "hotkey": "Ctrl+Alt+G", "type": "text", "text": "お疲れ様です。\n{date:M月d日(ddd)}の件、ご連絡いたします。", "enabled": True},
    {"name": "メモ帳を開く", "hotkey": "Ctrl+Alt+P", "type": "run", "command": "notepad.exe", "enabled": True},
]
TYPING = ("datetime", "text", "clip")


class Manager:
    def __init__(self, typer, path):
        self.typer, self.path, self.lock = typer, path, threading.RLock()
        self.cfg = self._load()
        self.status, self.last, self.hk = {}, None, HotkeyRunner(self)

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                c = json.load(f)
            if isinstance(c.get("entries"), list):
                return {"enabled": bool(c.get("enabled", True)), "mode": c.get("mode") if c.get("mode") in ("type", "paste") else "type",
                        "entries": [self.clean(e, validate=False) for e in c["entries"]][:60]}
        except (OSError, ValueError, AttributeError):
            pass
        return {"enabled": True, "mode": "type", "entries": [dict(e) for e in DEFAULT_ENTRIES]}

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    @staticmethod
    def clean(e, validate=True):
        e = e if isinstance(e, dict) else {}
        typ = e.get("type") if e.get("type") in TYPES else "datetime"
        out = {"name": str(e.get("name") or "")[:60], "hotkey": str(e.get("hotkey") or "")[:40], "type": typ, "enabled": bool(e.get("enabled", True))}
        if typ == "datetime":
            out.update(format=str(e.get("format") or "")[:200], base=e.get("base") if e.get("base") in BASES else "today",
                       unit=e.get("unit") if e.get("unit") in ("day", "biz", "week", "month") else "day")
            try:
                out["offset"] = max(-3660, min(3660, int(e.get("offset") or 0)))
            except (TypeError, ValueError):
                raise ConfigError("ずらす数は整数で入力してください")
            content, need = out["format"], "書式を入力してください"
        elif typ == "text":
            out["text"] = str(e.get("text") or "")[:5000]
            content, need = out["text"], "入力する文章を入力してください"
        elif typ == "open":
            out["target"] = str(e.get("target") or "").strip()[:500]
            content, need = out["target"], "開く先（ファイル・フォルダ・URL）を入力してください"
        elif typ == "run":
            out["command"] = str(e.get("command") or "").strip()[:500]
            content, need = out["command"], "起動するプログラムを入力してください"
        else:
            out["op"] = e.get("op") if e.get("op") in CLIP_OPS else "plain"
            content, need = out["op"], ""
        if validate:
            if not content:
                raise ConfigError(need)
            if out["hotkey"]:
                out["hotkey"] = parse_hotkey(out["hotkey"])[2]
            if not out["name"]:
                out["name"] = (CLIP_OPS[out["op"]][:20] if typ == "clip" else content.strip().replace("\n", " ")[:30])
        return out

    def check_dupes(self, entries):
        seen = {}
        for e in entries:
            if e["enabled"] and e["hotkey"]:
                k = e["hotkey"].lower()
                if k in seen:
                    raise ConfigError(f"ショートカット {e['hotkey']} が「{seen[k]}」と重複しています")
                seen[k] = e["name"]

    @staticmethod
    def preview_of(e, now=None):
        now = now or dt.datetime.now()
        t = e.get("type", "datetime")
        if t == "datetime":
            return compute(e, now)
        if t == "text":
            return expand_text(e.get("text", ""), now, "‹クリップボードの内容›")
        if t == "open":
            return e.get("target", "")
        if t == "run":
            return e.get("command", "")
        return CLIP_OPS.get(e.get("op"), "")

    def state(self):
        now = dt.datetime.now()
        entries = []
        for i, e in enumerate(self.cfg["entries"]):
            try:
                preview = self.preview_of(e, now)
            except Exception:
                preview = "(書式エラー)"
            entries.append(dict(e, index=i, preview=preview, status=self.status.get(i)))
        return {"backend": self.typer.name, "demo": self.typer.name == "mock", "supported": self.hk.supported(), "enabled": self.cfg["enabled"],
                "mode": self.cfg["mode"], "entries": entries, "bases": BASES, "types": TYPES, "clip_ops": CLIP_OPS, "typing": list(TYPING),
                "now": now.strftime("%Y-%m-%d %H:%M"), "last": self.last}

    def preview(self, entry):
        return {"ok": True, "text": self.preview_of(self.clean(entry, validate=False))}

    def upsert(self, index, entry):
        with self.lock:
            e = self.clean(entry)
            entries = list(self.cfg["entries"])
            if index is None or index < 0:
                if len(entries) >= 60:
                    raise ConfigError("登録は60件までです")
                entries.append(e)
            else:
                entries[index] = e
            self.check_dupes(entries)
            self.cfg["entries"] = entries
            self.save()
            self.hk.apply()
        return {"ok": True}

    def delete(self, index):
        with self.lock:
            del self.cfg["entries"][index]
            self.save()
            self.hk.apply()
        return {"ok": True}

    def set_enabled(self, on):
        if on and not self.hk.supported():
            raise ConfigError("ショートカットはWindows（本番モード）でのみ使えます")
        with self.lock:
            self.cfg["enabled"] = bool(on)
            self.save()
            self.hk.apply()
        return {"ok": True}

    def set_mode(self, mode):
        if mode not in ("type", "paste"):
            raise ConfigError("入力方式が正しくありません")
        self.cfg["mode"] = mode
        self.save()
        return {"ok": True}

    def fire(self, index, delay=0.0):
        """登録 index のショートカットを実行する（入力系は、今アクティブなウィンドウへ入力）"""
        e = self.cfg["entries"][index]
        typ = e.get("type", "datetime")
        if delay:
            time.sleep(delay)
        if typ == "datetime":
            text = compute(e)
            self.typer.type_text(text, self.cfg["mode"])
        elif typ == "text":
            clip = (self.typer.get_clip() or "") if "{clip}" in e["text"] else ""
            text = expand_text(e["text"], dt.datetime.now(), clip)
            self.typer.type_text(text, self.cfg["mode"])
        elif typ == "clip":
            src = self.typer.get_clip()
            if not src or not src.strip():
                raise OSError("クリップボードに文字がありません")
            text = transform_clip(e.get("op", "plain"), src)
            self.typer.paste_text(text)
        elif typ == "open":
            self.typer.open_target(e["target"])
            text = "開く: " + e["target"]
        else:
            self.typer.run_command(e["command"])
            text = "起動: " + e["command"]
        self.last = {"text": text, "at": dt.datetime.now().strftime("%H:%M"), "name": e["name"]}
        return text

    def test(self, index, delay):
        typ = self.cfg["entries"][index].get("type", "datetime")
        delay = max(1.0, min(10.0, float(delay))) if typ in TYPING else 0.0

        def run():
            try:
                self.fire(index, delay)
            except Exception as ex:
                self.last = {"text": "", "at": dt.datetime.now().strftime("%H:%M"), "name": "エラー", "error": str(ex)}

        threading.Thread(target=run, daemon=True).start()
        return {"ok": True, "delay": delay}


class HotkeyRunner:
    def __init__(self, m):
        self.m, self.thread, self.tid = m, None, None

    def supported(self):
        return os.name == "nt" and self.m.typer.name == "win32"

    def apply(self):
        self.stop()
        if self.m.cfg["enabled"] and self.supported():
            ready = threading.Event()
            self.thread = threading.Thread(target=self._run, args=(ready,), daemon=True)
            self.thread.start()
            ready.wait(3)

    def stop(self):
        if self.thread and self.thread.is_alive() and self.tid:
            ctypes.windll.user32.PostThreadMessageW(self.tid, 0x12, 0, 0)  # WM_QUIT
            self.thread.join(2)
        self.thread, self.tid = None, None
        self.m.status = {}

    def _run(self, ready):
        from ctypes import wintypes as wt
        u = ctypes.windll.user32
        self.tid = ctypes.windll.kernel32.GetCurrentThreadId()
        msg = wt.MSG()
        u.PeekMessageW(ctypes.byref(msg), None, 0x400, 0x400, 0)
        ids = {}
        status = {}
        for i, e in enumerate(self.m.cfg["entries"]):
            if not (e["enabled"] and e["hotkey"]):
                continue
            try:
                bits, vk, _ = parse_hotkey(e["hotkey"])
            except ConfigError:
                status[i] = "invalid"
                continue
            hid = i + 1
            ok = bool(u.RegisterHotKey(None, hid, bits | 0x4000, vk))  # MOD_NOREPEAT
            status[i] = "ok" if ok else "in_use"
            if ok:
                ids[hid] = i
        self.m.status = status
        ready.set()
        while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == 0x312 and msg.wParam in ids:  # WM_HOTKEY
                idx = ids[msg.wParam]
                threading.Thread(target=self._fire, args=(idx,), daemon=True).start()  # メッセージループを止めない
        for hid in ids:
            u.UnregisterHotKey(None, hid)

    def _fire(self, idx):
        try:
            self.m.fire(idx)
        except Exception as ex:
            traceback.print_exc()
            self.m.last = {"text": "", "at": dt.datetime.now().strftime("%H:%M"), "name": "エラー", "error": str(ex)}


def dispatch(m, path, b):
    if path == "/api/preview":
        return m.preview(b)
    if path == "/api/entry":
        return m.upsert(b.get("index"), b.get("entry"))
    if path == "/api/entry/delete":
        return m.delete(int(b["index"]))
    if path == "/api/enabled":
        return m.set_enabled(bool(b.get("on")))
    if path == "/api/mode":
        return m.set_mode(b.get("mode"))
    if path == "/api/test":
        return m.test(int(b["index"]), b.get("delay", 3))
    if path == "/api/_mock/fire" and m.typer.name == "mock":
        return {"ok": True, "text": m.fire(int(b["index"]))}
    raise KeyError(path)


PAGE = r"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ショートカット</title>
<style>
:root{--bg:#f3f5fa;--card:#fff;--ink:#1f2933;--sub:#6b7785;--line:#dde3ec;--accent:#4f46e5;--hi:#eef0ff;--err:#c0392b;--ok:#16a34a}
@media (prefers-color-scheme:dark){:root{--bg:#12141d;--card:#1c1f2b;--ink:#e8ecf4;--sub:#a4adc2;--line:#363b4e;--accent:#8b8bff;--hi:#25274a}}
*{box-sizing:border-box}[hidden]{display:none!important}
body{margin:0;font-family:-apple-system,"Hiragino Sans","Yu Gothic",Meiryo,sans-serif;background:var(--bg);color:var(--ink);font-size:14px}
header{display:flex;gap:12px;align-items:center;flex-wrap:wrap;padding:12px 18px;background:var(--card);border-bottom:1px solid var(--line)}
header h1{font-size:16px;margin:0}
input,select,button,textarea{font:inherit;padding:6px 10px;border:1px solid var(--line);border-radius:7px;background:var(--card);color:var(--ink)}
button{cursor:pointer}button.primary{background:var(--accent);color:#fff;border-color:var(--accent)}button.mini{padding:3px 9px;font-size:12px}button.danger{color:var(--err)}
.demo{background:#fef3c7;color:#92400e;padding:6px 18px;font-size:13px}
.switch{display:flex;align-items:center;gap:8px;font-weight:600}
main{max-width:1000px;margin:0 auto;padding:16px;display:grid;gap:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px}
.entry{display:grid;grid-template-columns:auto minmax(120px,170px) 1fr auto;gap:10px;align-items:center;padding:10px 0;border-top:1px solid var(--line)}
.entry:first-of-type{border-top:none}
.key{font-family:Consolas,monospace;background:var(--hi);border:1px solid var(--line);border-radius:6px;padding:3px 8px;font-weight:600;white-space:nowrap}
.prev{font-size:14px;font-weight:600;word-break:break-all;white-space:pre-wrap;max-height:3.4em;overflow:hidden}.sub{color:var(--sub);font-size:12px}
.badge{font-size:11px;padding:1px 7px;border-radius:99px}.b-ok{background:#dcfce7;color:#166534}.b-ng{background:#fee2e2;color:#991b1b}
.type{font-size:11px;padding:1px 8px;border-radius:99px;background:var(--hi);color:var(--accent);border:1px solid var(--line);margin-left:4px}
.hint{color:var(--sub);font-size:12px;margin:4px 0}
.filters{display:flex;gap:6px;flex-wrap:wrap;margin:8px 0}.filters button.on{background:var(--accent);color:#fff;border-color:var(--accent)}
dialog{border:none;border-radius:12px;padding:18px;width:min(640px,95vw);background:var(--card);color:var(--ink)}
dialog::backdrop{background:rgba(10,14,25,.5)}
.fields{display:grid;gap:10px}label.f{display:grid;gap:3px;font-size:12px;color:var(--sub)}label.f input,label.f select,label.f textarea{color:var(--ink)}
.row{display:flex;gap:6px;flex-wrap:wrap;align-items:center}.g2{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.chips button{font-family:Consolas,monospace;font-size:12px;padding:2px 7px}
.live{background:var(--hi);border-radius:8px;padding:10px;font-size:16px;font-weight:700;min-height:44px;word-break:break-all;white-space:pre-wrap}
#toast{position:fixed;left:50%;bottom:22px;transform:translateX(-50%);background:#1f2933;color:#fff;padding:9px 16px;border-radius:99px;font-size:13px;opacity:0;transition:.2s;pointer-events:none}
#toast.show{opacity:.95}
@media(max-width:640px){.entry{grid-template-columns:1fr}.g2{grid-template-columns:1fr}}
</style></head><body>
<header><h1>⌨️ ショートカット</h1>
  <label class="switch"><input type="checkbox" id="enabled"> ショートカットを有効にする</label>
  <span class="sub" title="日時・定型文を入力するときの方式">入力方式:</span>
  <select id="mode"><option value="type">キー入力 (おすすめ)</option><option value="paste">貼り付け (入力できない場合用)</option></select>
  <span style="flex:1"></span><span class="sub" id="now"></span></header>
<div class="demo" id="demo" hidden>デモモードです。ショートカットはWindowsで起動したときに動作します（「試す」ボタンは実行内容をログに記録するだけです）。</div>
<main>
  <section class="card">
    <div class="row"><b>登録済みのショートカット</b><span style="flex:1"></span><button class="primary" id="bNew">＋ 追加</button></div>
    <div class="filters" id="filters"></div>
    <div class="hint" id="lastbox"></div>
    <div id="list"></div>
  </section>
  <section class="card hint">使い方: 入力したい場所（メモ帳・Excelのセル・ブラウザの入力欄など）をクリックして、ショートカットを押します。このツールは起動したままにしてください。<br>
    種類: 日時入力 / 定型文入力 / 開く（ファイル・フォルダ・URL） / プログラム起動 / クリップボード加工（書式なし・1行化・半角化して貼り付け）。<br>
    管理者として動いているアプリには入力できません（このツールも管理者として起動してください）。祝日は「営業日」に含みません（土日のみ除外）。</section>
</main>
<dialog id="dlg"></dialog><div id="toast"></div>
<script>
const $ = s => document.querySelector(s);
function h(tag, props = {}, ...kids){
  const e = document.createElement(tag);
  for(const [k,v] of Object.entries(props)){ if(v == null || v === false) continue;
    if(k === 'class') e.className = v; else if(['checked','value','disabled','selected'].includes(k)) e[k] = v;
    else if(k.startsWith('on')) e[k] = v; else e.setAttribute(k, v); }
  for(const c of kids.flat()) if(c != null) e.append(c.nodeType ? c : document.createTextNode(c));
  return e;
}
function toast(m){ const t = $('#toast'); t.textContent = m; t.classList.add('show'); clearTimeout(toast.t); toast.t = setTimeout(() => t.classList.remove('show'), 2600); }
async function api(path, body){ try{ return await (await fetch(path, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body || {})})).json(); }catch(e){ return {ok:false, error:String(e)}; } }
let S = {entries:[], bases:{}, types:{}, clip_ops:{}, typing:[]}, lastKey = '', filter = 'all';
const UNITS = {day:'日', biz:'営業日', week:'週間', month:'か月'};
const FORMATS = [['yyyy/MM/dd','2026/11/28'],['yyyy-MM-dd','2026-11-28'],['yyyyMMdd','20261128'],['yyyy年M月d日','2026年11月28日'],['yyyy年M月d日(ddd)','…(土)'],['ggee年M月d日','令和8年11月28日'],['M/d','11/28'],['HH:mm','14:30'],['yyyy/MM/dd HH:mm','2026/11/28 14:30'],['yyyyMMdd_HHmm','20261128_1430'],["'【'yyyy/MM/dd'】'",'【2026/11/28】']];
const TOKENS = ['yyyy','yy','MM','M','dd','d','ddd','dddd','EEE','HH','H','hh','mm','aa','tt','gg','ee'];
const PH = [['{date:yyyy/MM/dd}','今日の日付'],['{date+1:M/d}','明日'],['{bizdate+1:M/d}','翌営業日'],['{clip}','クリップボードの文字'],['{date:HH:mm}','今の時刻']];

async function load(force){
  let d; try{ d = await (await fetch('/api/state')).json(); }catch(e){ return; }
  S = d; $('#now').textContent = '現在 ' + S.now; $('#demo').hidden = !S.demo;
  if(document.activeElement !== $('#enabled')) $('#enabled').checked = S.enabled; $('#enabled').disabled = !S.supported;
  if(document.activeElement !== $('#mode')) $('#mode').value = S.mode;
  const k = JSON.stringify([S.entries, S.enabled, S.last, filter]); if(k === lastKey && !force) return; lastKey = k; render();
}
function render(){
  const F = $('#filters'); F.innerHTML = '';
  const counts = {}; S.entries.forEach(e => counts[e.type] = (counts[e.type] || 0) + 1);
  F.append(h('button', {class:'mini' + (filter === 'all' ? ' on' : ''), onclick:() => { filter = 'all'; render(); }}, `すべて ${S.entries.length}`));
  for(const [k, v] of Object.entries(S.types)) if(counts[k]) F.append(h('button', {class:'mini' + (filter === k ? ' on' : ''), 'data-type':k, onclick:() => { filter = k; render(); }}, `${v} ${counts[k]}`));
  const L = $('#list'); L.innerHTML = '';
  $('#lastbox').textContent = S.last ? (S.last.error ? '直近のエラー: ' + S.last.error : `直近の実行 (${S.last.at}): ${S.last.text}`) : '';
  S.entries.filter(e => filter === 'all' || e.type === filter).forEach(e => {
    const st = e.status, badge = !S.enabled || !e.enabled || !e.hotkey ? null : st === 'ok' ? h('span', {class:'badge b-ok'}, '登録済み') : st ? h('span', {class:'badge b-ng'}, st === 'in_use' ? '他で使用中' : '無効な設定') : null;
    L.append(h('div', {class:'entry', 'data-i':e.index, 'data-type':e.type},
      h('input', {type:'checkbox', class:'en', checked:e.enabled, title:'このショートカットを使う', onchange:async ev => { const r = await api('/api/entry', {index:e.index, entry:Object.assign({}, e, {enabled:ev.target.checked})}); if(r.ok === false) toast(r.error); load(true); }}),
      h('div', {}, h('span', {class:'key'}, e.hotkey || '(未設定)'), ' ', badge),
      h('div', {}, h('div', {class:'sub'}, e.name, h('span', {class:'type'}, S.types[e.type])), h('div', {class:'prev'}, e.preview)),
      h('div', {class:'row'}, h('button', {class:'mini try', title:S.typing.includes(e.type) && !S.demo ? '3秒後に、そのとき選択している場所へ入力します' : '実行します', onclick:() => tryIt(e)}, '試す'), h('button', {class:'mini edit', onclick:() => edit(e)}, '編集'),
        h('button', {class:'mini danger', onclick:async () => { if(confirm(`「${e.name}」を削除しますか？`)){ await api('/api/entry/delete', {index:e.index}); load(true); } }}, '削除'))));
  });
  if(!L.children.length) L.append(h('div', {class:'hint'}, '該当する登録がありません。「追加」から作成してください。'));
}
async function tryIt(e){
  if(S.demo){ const r = await api('/api/_mock/fire', {index:e.index}); toast(r.ok ? '実行しました（デモ）: ' + r.text.slice(0, 60) : r.error); return load(true); }
  const r = await api('/api/test', {index:e.index, delay:3}); toast(r.ok ? (r.delay ? '3秒以内に、入力したい場所をクリックしてください' : '実行しました') : r.error);
  setTimeout(() => load(true), 4500);
}
$('#enabled').onchange = async e => { const r = await api('/api/enabled', {on:e.target.checked}); if(r.ok === false){ toast(r.error); e.target.checked = !e.target.checked; } else toast(e.target.checked ? 'ショートカットを有効にしました' : 'ショートカットを無効にしました'); load(true); };
$('#mode').onchange = async e => { await api('/api/mode', {mode:e.target.value}); load(true); };
$('#bNew').onclick = () => edit(null);

const KEYMAP = {Space:'Space',Enter:'Enter',Tab:'Tab',Insert:'Insert',Delete:'Delete',Home:'Home',End:'End',PageUp:'PageUp',PageDown:'PageDown',ArrowUp:'Up',ArrowDown:'Down',ArrowLeft:'Left',ArrowRight:'Right',Semicolon:';',Equal:'=',Comma:',',Minus:'-',Period:'.',Slash:'/',Backquote:'`',BracketLeft:'[',Backslash:'\\',BracketRight:']',Quote:"'"};
function edit(e){
  const cur = e ? Object.assign({}, e) : {name:'', hotkey:'', type:'datetime', format:'yyyy/MM/dd', base:'today', offset:0, unit:'day', text:'', target:'', command:'', op:'plain', enabled:true};
  const dlg = $('#dlg'); dlg.innerHTML = '';
  const name = h('input', {id:'fName', value:cur.name, placeholder:'例: 今日の日付'});
  const type = h('select', {id:'fType', onchange:() => { showType(); upd(); }}, Object.entries(S.types).map(([k, v]) => h('option', {value:k, selected:cur.type === k}, v)));
  const hk = h('input', {id:'fKey', value:cur.hotkey, readonly:'readonly', placeholder:'ここをクリックして、ショートカットを押す', style:'font-family:Consolas,monospace;font-weight:600'});
  hk.onkeydown = ev => { ev.preventDefault(); if(['ControlLeft','ControlRight','AltLeft','AltRight','ShiftLeft','ShiftRight','MetaLeft','MetaRight'].includes(ev.code)) return;
    if(ev.key === 'Backspace' || ev.key === 'Escape'){ hk.value = ''; return; }
    let key = /^Key[A-Z]$/.test(ev.code) ? ev.code.slice(3) : /^Digit\d$/.test(ev.code) ? ev.code.slice(5) : /^F\d+$/.test(ev.code) ? ev.code : KEYMAP[ev.code]; if(!key) return;
    hk.value = [ev.ctrlKey && 'Ctrl', ev.altKey && 'Alt', ev.shiftKey && 'Shift', ev.metaKey && 'Win'].filter(Boolean).concat(key).join('+'); };
  // 日時
  const fmt = h('input', {id:'fFmt', value:cur.format || '', style:'font-family:Consolas,monospace', oninput:upd});
  const base = h('select', {id:'fBase', onchange:upd}, Object.entries(S.bases).map(([k, v]) => h('option', {value:k, selected:cur.base === k}, v)));
  const off = h('input', {id:'fOff', type:'number', value:cur.offset || 0, oninput:upd, style:'width:90px'});
  const unit = h('select', {id:'fUnit', onchange:upd}, Object.entries(UNITS).map(([k, v]) => h('option', {value:k, selected:cur.unit === k}, v)));
  const chips = h('div', {class:'row chips'}, TOKENS.map(k => h('button', {type:'button', onclick:() => { const s = fmt.selectionStart ?? fmt.value.length; fmt.setRangeText(k, s, fmt.selectionEnd ?? s, 'end'); fmt.focus(); upd(); }}, k)));
  const presets = h('div', {class:'row chips'}, FORMATS.map(([f, ex]) => h('button', {type:'button', title:ex, onclick:() => { fmt.value = f; upd(); }}, ex)));
  const secDt = h('div', {class:'fields', 'data-sec':'datetime'}, h('label', {class:'f'}, '書式', fmt), h('div', {class:'hint'}, '入力例から選ぶ'), presets,
    h('div', {class:'hint'}, "クリックで書式に追加（'…' で囲むとそのまま出力、\\n は改行）"), chips,
    h('div', {class:'g2'}, h('label', {class:'f'}, '基準日', base), h('label', {class:'f'}, 'ずらす（マイナスで過去）', h('div', {class:'row'}, off, unit))));
  // 定型文
  const text = h('textarea', {id:'fText', rows:5, oninput:upd, placeholder:'例: お疲れ様です。\n{date:M月d日(ddd)}の件、ご連絡いたします。'}); text.value = cur.text || '';
  const phs = h('div', {class:'row chips'}, PH.map(([p, d]) => h('button', {type:'button', title:d, onclick:() => { const s = text.selectionStart ?? text.value.length; text.setRangeText(p, s, text.selectionEnd ?? s, 'end'); text.focus(); upd(); }}, p)));
  const secText = h('div', {class:'fields', 'data-sec':'text'}, h('label', {class:'f'}, '入力する文章（改行もそのまま入力されます）', text), h('div', {class:'hint'}, '差し込み: クリックで追加（{date:書式} は日付書式、{{ }} は波括弧そのもの）'), phs);
  // 開く / 起動 / クリップボード
  const target = h('input', {id:'fTarget', value:cur.target || '', placeholder:'例: C:\\Users\\me\\Documents\\日報.xlsx / D:\\案件 / https://example.com', oninput:upd});
  const command = h('input', {id:'fCmd', value:cur.command || '', placeholder:'例: notepad.exe / "C:\\Program Files\\App\\app.exe" --option', oninput:upd});
  const op = h('select', {id:'fOp', onchange:upd}, Object.entries(S.clip_ops).map(([k, v]) => h('option', {value:k, selected:cur.op === k}, v)));
  const secOpen = h('div', {class:'fields', 'data-sec':'open'}, h('label', {class:'f'}, '開く先（ファイル・フォルダ・URL）', target), h('div', {class:'hint'}, '関連付けられたアプリで開きます。%USERPROFILE% などの環境変数も使えます。'));
  const secRun = h('div', {class:'fields', 'data-sec':'run'}, h('label', {class:'f'}, 'プログラムと引数', command), h('div', {class:'hint'}, 'スペースを含むパスは "…" で囲みます。シェルは使わず、そのまま起動します。'));
  const secClip = h('div', {class:'fields', 'data-sec':'clip'}, h('label', {class:'f'}, 'クリップボードの文字をどう貼り付けるか', op), h('div', {class:'hint'}, 'コピーしてあるテキストを加工して、今の入力場所へ貼り付けます。'));
  const live = h('div', {class:'live', id:'live'});
  const secs = [secDt, secText, secOpen, secRun, secClip];
  const val = () => ({name:name.value, hotkey:hk.value, type:type.value, enabled:cur.enabled, format:fmt.value, base:base.value, offset:+off.value || 0, unit:unit.value, text:text.value, target:target.value, command:command.value, op:op.value});
  function showType(){ secs.forEach(s => s.hidden = s.dataset.sec !== type.value); }
  let t; function upd(){ clearTimeout(t); t = setTimeout(async () => { const r = await api('/api/preview', val()); live.textContent = r.ok ? r.text : r.error; }, 80); }
  dlg.append(h('h3', {style:'margin:0 0 10px'}, e ? 'ショートカットを編集' : 'ショートカットを追加'),
    h('div', {class:'fields'}, h('div', {class:'g2'}, h('label', {class:'f'}, '種類', type), h('label', {class:'f'}, '名前', name)),
      h('label', {class:'f'}, 'ショートカット（Ctrl / Alt / Shift / Win と組み合わせ。Backspaceで消去）', hk), ...secs,
      h('div', {class:'hint'}, 'プレビュー（今の内容で実行される内容）'), live,
      h('div', {class:'row', style:'justify-content:flex-end'}, h('button', {onclick:() => dlg.close()}, 'キャンセル'),
        h('button', {class:'primary', id:'fSave', onclick:async () => { const r = await api('/api/entry', {index:e ? e.index : -1, entry:val()}); if(r.ok === false) return toast(r.error); dlg.close(); toast('保存しました'); load(true); }}, '保存'))));
  showType(); dlg.showModal(); upd();
}
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
            self._send(403, "Forbidden", "text/plain; charset=utf-8")
            return False
        return True

    def do_GET(self):
        if not self._guard():
            return
        path = urlparse(self.path).path
        if path == "/":
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if path == "/api/state":
            return self._send(200, json.dumps(self.manager.state(), ensure_ascii=False))
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
        except (ConfigError, ValueError, TypeError, IndexError) as e:
            result = {"ok": False, "error": str(e) if isinstance(e, ConfigError) else "入力が正しくありません"}
        except OSError as e:
            result = {"ok": False, "error": str(e)}
        self._send(200, json.dumps(result, ensure_ascii=False))

    def log_message(self, fmt, *args):
        pass


def selftest():
    now = dt.datetime.now()
    print(f"現在: {now:%Y-%m-%d %H:%M}")
    for e in DEFAULT_ENTRIES:
        print(f"  {e['hotkey']:<12} {TYPES[e['type']]:<10} {e['name']:<18} → {Manager.preview_of(e, now)!r}")
    print("→ 表示が期待どおりなら書式は正常です（入力は行っていません）。")


def main():
    ap = argparse.ArgumentParser(description="ショートカット")
    ap.add_argument("--port", type=int, default=8408)
    ap.add_argument("--backend", choices=["auto", "win32", "mock"], default="auto")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if a.backend == "win32" and os.name != "nt":
        raise SystemExit("win32 バックエンドはWindowsでのみ使えます（--backend mock で画面を試せます）")
    typer = Win32Typer() if (os.name == "nt" and a.backend != "mock") else MockTyper()
    mgr = Manager(typer, CONFIG_PATH)
    Handler.manager = mgr
    mgr.hk.apply()
    try:
        server = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    except OSError as e:
        raise SystemExit(f"ポート {a.port} を使えません（{e}）")
    print(f"ショートカットを起動しました [{typer.name}]: http://127.0.0.1:{a.port}  (終了は Ctrl+C)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n終了します")
    finally:
        mgr.hk.stop()


if __name__ == "__main__":
    main()
