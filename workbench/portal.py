#!/usr/bin/env python3
"""業務ツールボックス (ポータル)

複数のWebアプリを「1つのアプリ」として操作するためのランチャーです。
各アプリは apps/<名前>/ に独立して置かれたまま、別プロセスで動きます（ファイルは一切統合・改変しません）。
このポータルは、起動・停止・切り替え・状態表示だけを担当します。標準ライブラリのみで動作します。

    python3 portal.py                    # http://127.0.0.1:8080 を開く
    python3 portal.py --port 9000
    python3 portal.py --start todo,notes   # 起動するアプリを限定（既定は all=全部、none=起動しない）
    python3 portal.py --no-browser       # ブラウザを自動で開かない

フォルダ構成:
    portal.py            このファイル
    apps.json            ポータルに載せるアプリの一覧（追加・削除はここを編集して再起動）
    apps/<名前>/...      各アプリ。それぞれ独立（データもそれぞれのフォルダに保存されます）
    portal_logs/         各アプリの標準出力・エラーのログ

apps.json の項目:
    id / name / icon / kind / description   表示用
    cwd        アプリのフォルダ（portal.py からの相対パス）。この場所で起動します
    command    起動コマンド。{python}=このポータルのPython、{port}=割り当てるポート
    port       希望ポート（使用中なら次の空きポートを自動で使います）
    requires   必要なPythonパッケージ {"pip名": "import名"}。不足していれば画面で案内します
    env        追加の環境変数（任意）

セキュリティ: 自分のPCからのアクセス (127.0.0.1) だけを受け付けます。
クリップボード監視アプリはこのPCのクリップボードを読むため、ローカル専用で使ってください。
"""
import argparse
import atexit
import importlib
import importlib.util
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("PORTAL_CONFIG") or os.path.join(BASE, "apps.json")
LOG_DIR = os.environ.get("PORTAL_LOGS") or os.path.join(BASE, "portal_logs")
START_TIMEOUT = 90          # 秒。これを過ぎてもポートが開かなければ「エラー」扱い
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}
SERVER = None


class AppError(Exception):
    pass


# ---------------------------------------------------------------- プロセス管理
def port_open(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.25):
            return True
    except OSError:
        return False


def port_free(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name != "nt":  # TIME_WAIT中でも使えるように（Windowsでは逆に危険なので付けない）
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def kill_tree(p):
    """子プロセスごと終了する（Streamlit等が孫プロセスを作っても残らないように）"""
    if p.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True)
        else:
            os.killpg(p.pid, signal.SIGTERM)
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL)
        p.wait(5)
    except (ProcessLookupError, PermissionError, OSError, subprocess.TimeoutExpired):
        pass


def tail_text(path, max_bytes=65536, max_lines=None):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return ""
    lines = text.splitlines()
    return "\n".join(lines[-max_lines:] if max_lines else lines)


class ManagedApp:
    def __init__(self, cfg):
        self.cfg = cfg
        self.id = cfg["id"]
        self.proc = None
        self.port = None
        self.started_at = 0.0
        self.run_id = 0
        self.lock = threading.Lock()
        self.log_path = os.path.join(LOG_DIR, f"{self.id}.log")

    @property
    def cwd(self):
        return os.path.normpath(os.path.join(BASE, self.cfg.get("cwd", ".")))

    def missing(self):
        importlib.invalidate_caches()
        out = []
        for pip_name, module in (self.cfg.get("requires") or {}).items():
            try:
                found = importlib.util.find_spec(module) is not None
            except (ImportError, ValueError):
                found = False
            if not found:
                out.append(pip_name)
        return out

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self):
        with self.lock:
            if self.alive():
                return
            if not os.path.isdir(self.cwd):
                raise AppError(f"アプリのフォルダが見つかりません: {self.cfg.get('cwd')}")
            missing = self.missing()
            if missing:
                raise AppError("必要なパッケージが不足しています: " + ", ".join(missing))
            taken = {a.port for a in APPS.values() if a is not self and a.alive()}
            pref = int(self.cfg.get("port", 8400))
            port = next((p for p in range(pref, pref + 200) if p not in taken and port_free(p)), None)
            if port is None:
                raise AppError("空いているポートが見つかりません")
            fill = lambda s: str(s).replace("{python}", sys.executable).replace("{port}", str(port))
            cmd = [fill(a) for a in self.cfg["command"]]
            env = os.environ.copy()
            env.update({k: fill(v) for k, v in (self.cfg.get("env") or {}).items()})
            env.update({"PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"})
            os.makedirs(LOG_DIR, exist_ok=True)
            mode = "wb" if os.path.exists(self.log_path) and os.path.getsize(self.log_path) > 1_000_000 else "ab"
            flags = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
            with open(self.log_path, mode) as log:
                log.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} 起動: {' '.join(cmd)} ===\n".encode("utf-8"))
                log.flush()
                try:
                    self.proc = subprocess.Popen(cmd, cwd=self.cwd, env=env, stdin=subprocess.DEVNULL,
                                                 stdout=log, stderr=subprocess.STDOUT, **flags)
                except OSError as e:
                    self.proc = None
                    raise AppError(f"起動できませんでした: {e}")
            self.port, self.started_at = port, time.time()
            self.run_id += 1

    def stop(self):
        with self.lock:
            p, self.proc = self.proc, None
            if p is not None:
                kill_tree(p)

    def status(self):
        p, state, msg = self.proc, "stopped", ""
        missing = self.missing() if p is None else []
        if p is None:
            if not os.path.isdir(self.cwd):
                state, msg = "missing", f"アプリのフォルダが見つかりません: {self.cfg.get('cwd')}"
            elif missing:
                state = "missing"
        else:
            rc = p.poll()
            if rc is not None:
                state, msg = "failed", f"プロセスが終了しました（終了コード {rc}）"
            elif port_open(self.port):
                state = "running"
            elif time.time() - self.started_at > START_TIMEOUT:
                state, msg = "failed", f"{START_TIMEOUT}秒以内に起動しませんでした"
            else:
                state = "starting"
        d = {k: self.cfg.get(k, "") for k in ("id", "name", "icon", "kind", "description")}
        d.update(state=state, message=msg, port=self.port if p is not None else None, run_id=self.run_id,
                 missing=missing, tail=tail_text(self.log_path, 8192, 15) if state == "failed" else "")
        if missing:
            d["install"] = f'"{sys.executable}" -m pip install ' + " ".join(missing)
        return d


def load_apps():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        conf = json.load(f)
    apps = {}
    for cfg in conf.get("apps", []):
        if not re.fullmatch(r"[a-z0-9_]+", str(cfg.get("id", ""))) or not isinstance(cfg.get("command"), list):
            raise SystemExit(f"apps.json の設定が正しくありません: {cfg.get('id')!r}（id は半角英数字と _ 、command は配列）")
        apps[cfg["id"]] = ManagedApp(cfg)
    return conf.get("title", "業務ツールボックス"), apps


TITLE, APPS = "業務ツールボックス", {}


def shutdown_all():
    for a in APPS.values():
        a.stop()


# ---------------------------------------------------------------- 画面
PAGE = r"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>業務ツールボックス</title>
<style>
:root{--bg:#f1f4f9;--side:#1f2740;--side-ink:#d8def0;--side-sub:#8a94b5;--side-hi:#34406a;--card:#fff;--ink:#1f2933;--sub:#6b7785;--line:#dde3ec;--accent:#3a5bd9;
--ok:#16a34a;--warn:#d97706;--err:#d9304f}
@media (prefers-color-scheme:dark){:root{--bg:#12151c;--card:#1c212c;--ink:#e8ecf4;--sub:#a4adc2;--line:#363d4e;--accent:#6b8cff}}
*{box-sizing:border-box}
html,body{height:100%;margin:0}
body{font-family:-apple-system,"Hiragino Sans","Yu Gothic",Meiryo,sans-serif;background:var(--bg);color:var(--ink)}
button{font:inherit;cursor:pointer;border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:8px;padding:6px 12px}
button.primary{background:var(--accent);color:#fff;border-color:var(--accent)}
button:disabled{opacity:.5;cursor:default}
#shell{display:grid;grid-template-columns:240px minmax(0,1fr);height:100vh}
#side{background:var(--side);color:var(--side-ink);display:flex;flex-direction:column;padding:14px 10px;gap:4px;overflow:auto}
.brand{font-weight:700;font-size:16px;padding:6px 10px 12px;color:#fff}
.nav{display:flex;align-items:center;gap:10px;width:100%;text-align:left;background:none;border:none;color:var(--side-ink);padding:9px 10px;border-radius:8px}
.nav:hover{background:var(--side-hi)}.nav.on{background:var(--accent);color:#fff}
.nav .nm{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:14px}
.dot{width:9px;height:9px;border-radius:50%;background:#6b7490;flex:none}
.dot.running{background:#34d399}.dot.starting{background:#fbbf24;animation:bl 1s infinite}.dot.failed{background:#fb7185}.dot.missing{background:transparent;border:2px solid #fbbf24}
@keyframes bl{50%{opacity:.35}}
.sep{height:1px;background:#ffffff22;margin:8px 4px}
.side-foot{margin-top:auto}.side-foot button{width:100%;background:none;color:var(--side-sub);border-color:#ffffff33}
#main{display:flex;flex-direction:column;min-width:0;height:100vh}
#bar{display:flex;align-items:center;gap:8px;padding:10px 16px;background:var(--card);border-bottom:1px solid var(--line);flex-wrap:wrap}
#bar h1{margin:0;font-size:16px}
.pill{font-size:12px;padding:2px 10px;border-radius:99px;background:var(--line);color:var(--sub);font-weight:700}
.pill.running{background:#d1fae5;color:#065f46}.pill.starting{background:#fef3c7;color:#92400e}.pill.failed{background:#ffe4e6;color:#9f1239}.pill.missing{background:#fef3c7;color:#92400e}
.sp{flex:1}
#stage{position:relative;flex:1;min-height:0}
.view,.frame{position:absolute;inset:0;width:100%;height:100%}
.frame{border:0;background:#fff;display:none}.frame.on{display:block}
.view{overflow:auto;padding:22px;display:none}.view.on{display:block}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(270px,1fr));gap:14px;margin-top:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;display:grid;gap:8px;align-content:start}
.card .hd{display:flex;align-items:center;gap:10px}.card .ic{font-size:26px}.card b{font-size:15px}
.card p{margin:0;color:var(--sub);font-size:13px;min-height:2.6em}
.card .ft{display:flex;align-items:center;gap:8px}.badge{font-size:11px;color:var(--sub);border:1px solid var(--line);border-radius:6px;padding:1px 7px}
.lead{color:var(--sub);font-size:14px;max-width:760px;line-height:1.7}
.center{max-width:640px;margin:8vh auto 0;display:grid;gap:12px;text-align:center}
.center h2{margin:0}.center .ic{font-size:44px}
pre{background:#1e2430;color:#e6edf3;border-radius:8px;padding:12px;font:12px/1.5 ui-monospace,Menlo,Consolas,monospace;overflow:auto;text-align:left;white-space:pre-wrap;word-break:break-all;margin:0}
.spin{width:30px;height:30px;border:4px solid var(--line);border-top-color:var(--accent);border-radius:50%;animation:sp 1s linear infinite;margin:0 auto}
@keyframes sp{to{transform:rotate(360deg)}}
.row{display:flex;gap:8px;justify-content:center;flex-wrap:wrap}
dialog{border:none;border-radius:12px;padding:16px;width:min(820px,94vw);background:var(--card);color:var(--ink)}
dialog::backdrop{background:rgba(10,14,25,.5)}
#toast{position:fixed;left:50%;bottom:22px;transform:translateX(-50%);background:#1f2933;color:#fff;padding:9px 16px;border-radius:99px;font-size:13px;opacity:0;transition:.2s;pointer-events:none;z-index:9}
#toast.show{opacity:.95}
@media(max-width:760px){#shell{grid-template-columns:1fr;grid-template-rows:auto minmax(0,1fr);height:100vh}
 #side{flex-direction:row;overflow-x:auto;padding:8px;align-items:center;gap:4px}.brand,.sep,.side-foot{display:none}
 #navlist{display:flex;gap:4px}.nav{width:auto;white-space:nowrap;flex:none}#main{height:auto}}
</style></head><body>
<div id="shell">
  <nav id="side">
    <div class="brand" id="brand">🧰 業務ツールボックス</div>
    <button class="nav" data-id="home"><span>🏠</span><span class="nm">ホーム</span></button>
    <div class="sep"></div><div id="navlist"></div>
    <div class="side-foot"><button id="quit">ポータルを終了</button></div>
  </nav>
  <section id="main">
    <header id="bar">
      <h1 id="title"></h1><span id="stat" class="pill" hidden></span><span class="sp"></span>
      <span id="tools" hidden>
        <button id="bReload" title="表示中の画面を再読み込み">再読み込み</button>
        <button id="bRestart">再起動</button><button id="bStop">停止</button>
        <button id="bNewTab">別タブで開く</button><button id="bLog">ログ</button>
      </span>
    </header>
    <div id="stage"><div class="view" id="home"></div><div class="view" id="overlay"></div></div>
  </section>
</div>
<dialog id="logDlg"><div style="display:grid;gap:10px"><b id="logTitle"></b><pre id="logPre" style="max-height:60vh"></pre>
  <div class="row"><button id="logClose">閉じる</button></div></div></dialog>
<div id="toast"></div>
<script>
const $ = s => document.querySelector(s);
function h(tag, props = {}, ...kids){
  const e = document.createElement(tag);
  for(const [k,v] of Object.entries(props)){ if(v == null || v === false) continue;
    if(k==='class') e.className=v; else if(k.startsWith('on')) e[k]=v; else e.setAttribute(k,v); }
  for(const c of kids.flat()) if(c != null) e.append(c.nodeType ? c : document.createTextNode(c));
  return e;
}
const LABEL = {stopped:'停止中', starting:'起動中', running:'実行中', failed:'エラー', missing:'要インストール'};
let apps = [], cur = 'home', frames = {}, lastKey = '', loaded = false;
const byId = id => apps.find(a => a.id === id);
const lsGet = k => { try{ return localStorage.getItem(k); }catch(e){ return null; } };
const lsSet = (k,v) => { try{ localStorage.setItem(k,v); }catch(e){} };
function toast(m){ const t=$('#toast'); t.textContent=m; t.classList.add('show'); clearTimeout(toast.t); toast.t=setTimeout(()=>t.classList.remove('show'),1600); }
async function post(url){ try{ const r = await fetch(url, {method:'POST'}); return await r.json(); }catch(e){ return {ok:false, error:String(e)}; } }
const url = a => `http://${location.hostname}:${a.port}/`;

async function poll(){
  let d; try{ d = await (await fetch('/api/apps')).json(); }catch(e){ return; }
  apps = d.apps; $('#brand').textContent = '🧰 ' + d.title; document.title = d.title;
  if(!loaded){ loaded = true; buildNav();
    const want = decodeURIComponent(location.hash.slice(1)) || lsGet('portal.cur') || 'home';
    select(byId(want) || want === 'home' ? want : 'home', true); }
  updateNav(); updateFrames(); render();
}
function buildNav(){
  const box = $('#navlist'); box.innerHTML = '';
  for(const a of apps) box.append(h('button', {class:'nav', 'data-id':a.id, onclick:()=>select(a.id)},
    h('span', {}, a.icon), h('span', {class:'nm'}, a.name), h('span', {class:'dot'})));
  document.querySelector('.nav[data-id=home]').onclick = () => select('home');
}
function updateNav(){
  document.querySelectorAll('.nav').forEach(b => {
    b.classList.toggle('on', b.dataset.id === cur);
    const a = byId(b.dataset.id), dot = b.querySelector('.dot');
    if(a && dot){ dot.className = 'dot ' + a.state; dot.title = LABEL[a.state]; }
  });
}
function select(id, first){
  cur = id; lsSet('portal.cur', id); if(!first) history.replaceState(null, '', '#' + encodeURIComponent(id));
  const a = byId(id);
  if(a && a.state === 'stopped') act(id, 'start', true);
  updateNav(); render();
}
async function act(id, action, quiet){
  const r = await post(`/api/apps/${id}/${action}`);
  if(!r.ok && !quiet) toast(r.error || '失敗しました');
  await poll();
}
function updateFrames(){
  const stage = $('#stage');
  for(const a of apps){
    const f = frames[a.id];
    if(a.state !== 'running'){ if(f){ f.el.remove(); delete frames[a.id]; } continue; }
    if(!f){
      const el = h('iframe', {class:'frame', src:url(a), title:a.name, allow:'clipboard-read; clipboard-write; fullscreen'});
      stage.append(el); frames[a.id] = {el, run_id:a.run_id};
    } else if(f.run_id !== a.run_id){ f.el.src = url(a); f.run_id = a.run_id; }
  }
}
function render(){
  const a = byId(cur), isHome = cur === 'home' || !a;
  $('#title').textContent = isHome ? '🏠 ホーム' : `${a.icon} ${a.name}`;
  const st = $('#stat'); st.hidden = isHome; $('#tools').hidden = isHome || a.state === 'stopped' || a.state === 'missing';
  if(!isHome){ st.className = 'pill ' + a.state; st.textContent = LABEL[a.state]; }
  $('#home').classList.toggle('on', isHome);
  if(isHome) renderHome();
  const running = !isHome && a.state === 'running' && frames[a.id];
  for(const [id,f] of Object.entries(frames)) f.el.classList.toggle('on', running && id === cur);
  $('#overlay').classList.toggle('on', !isHome && !running);
  if(!isHome && !running) renderOverlay(a);
}
function renderHome(){
  const key = 'home:' + apps.map(a => a.id + a.state).join(',');
  if(key === lastKey) return; lastKey = key;
  const box = $('#home'); box.innerHTML = '';
  const startable = apps.filter(a => a.state === 'stopped' || a.state === 'failed');
  box.append(h('div', {class:'lead'}, '使いたいアプリを左のメニューまたは下のカードから選んでください。各アプリは独立したフォルダ・別プロセスで動き、データもそれぞれのフォルダに保存されます。選ぶと自動で起動し、一度開いたアプリは他の画面を見ている間も動き続けます。'),
    h('div', {style:'margin-top:12px'}, h('button', {class:'primary', disabled:startable.length ? null : 'disabled',
      onclick:async()=>{ for(const a of startable) await post(`/api/apps/${a.id}/start`); await poll(); }}, 'すべて起動')));
  const cards = h('div', {class:'cards'});
  for(const a of apps) cards.append(h('div', {class:'card'},
    h('div', {class:'hd'}, h('span', {class:'ic'}, a.icon), h('b', {}, a.name)),
    h('p', {}, a.description),
    h('div', {class:'ft'}, h('span', {class:'pill ' + a.state}, LABEL[a.state]), h('span', {class:'badge'}, a.kind), h('span', {class:'sp'}),
      h('button', {class:'primary', onclick:()=>select(a.id)}, '開く'))));
  box.append(cards);
}
function renderOverlay(a){
  const key = ['o', a.id, a.state, a.message, a.tail, (a.missing || []).join()].join('|');
  if(key === lastKey) return; lastKey = key;
  const v = $('#overlay'); v.innerHTML = '';
  const box = h('div', {class:'center'}); v.append(box);
  if(a.state === 'starting' || (a.state === 'running')){
    box.append(h('div', {class:'spin'}), h('h2', {}, `${a.name} を起動しています…`), h('div', {class:'lead'}, 'Streamlitのアプリは初回の起動に十数秒かかることがあります。'));
  } else if(a.state === 'stopped'){
    box.append(h('div', {class:'ic'}, a.icon), h('h2', {}, `${a.name} は停止中です`), h('div', {class:'row'}, h('button', {class:'primary', onclick:()=>act(a.id,'start')}, '起動する')));
  } else if(a.state === 'missing'){
    box.append(h('div', {class:'ic'}, '📦'), h('h2', {}, '必要なパッケージがインストールされていません'),
      h('div', {class:'lead'}, a.message || `不足: ${(a.missing||[]).join('、')}`),
      a.install ? h('div', {}, h('div', {class:'lead', style:'margin-bottom:6px'}, 'ポータルを動かしているPythonに、次のコマンドでインストールしてください。インストール後、このボタンで起動できます。'),
        h('pre', {}, a.install)) : null,
      h('div', {class:'row'}, a.install ? h('button', {onclick:async()=>{ try{ await navigator.clipboard.writeText(a.install); toast('コマンドをコピーしました'); }catch(e){ toast('コピーできませんでした'); } }}, 'コマンドをコピー') : null,
        h('button', {class:'primary', onclick:()=>act(a.id,'start')}, '起動する')));
  } else {
    box.append(h('div', {class:'ic'}, '⚠️'), h('h2', {}, `${a.name} が起動できませんでした`), h('div', {class:'lead'}, a.message),
      a.tail ? h('pre', {}, a.tail) : null,
      h('div', {class:'row'}, h('button', {class:'primary', onclick:()=>act(a.id,'restart')}, 'もう一度起動する'), h('button', {onclick:()=>showLog(a.id)}, 'ログ全体を見る')));
  }
}
async function showLog(id){
  const a = byId(id); $('#logTitle').textContent = `${a.name} のログ`;
  $('#logPre').textContent = await (await fetch(`/api/apps/${id}/log`)).text() || '（ログはまだありません）';
  $('#logDlg').showModal(); const p = $('#logPre'); p.scrollTop = p.scrollHeight;
}
$('#logClose').onclick = () => $('#logDlg').close();
$('#bLog').onclick = () => showLog(cur);
$('#bRestart').onclick = () => act(cur, 'restart');
$('#bStop').onclick = () => act(cur, 'stop');
$('#bReload').onclick = () => { const f = frames[cur]; if(f) f.el.src = f.el.src; };
$('#bNewTab').onclick = () => { const a = byId(cur); if(a && a.port) window.open(url(a), '_blank'); };
$('#quit').onclick = async () => {
  if(!confirm('ポータルと、起動中のすべてのアプリを終了します。よろしいですか？')) return;
  await post('/api/quit'); document.body.innerHTML = '<div style="font-family:sans-serif;text-align:center;margin-top:20vh"><h2>終了しました</h2><p>このタブは閉じて構いません。</p></div>';
};
document.querySelector('.nav[data-id=home]').onclick = () => select('home');
poll(); setInterval(poll, 1500);
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
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
        if host not in ALLOWED_HOSTS:  # DNS rebinding 対策
            self._send(403, "Forbidden", "text/plain; charset=utf-8")
            return False
        origin = self.headers.get("Origin")
        if post and origin and urlparse(origin).netloc != self.headers.get("Host"):
            self._send(403, "Forbidden", "text/plain; charset=utf-8")
            return False
        return True

    def do_GET(self):
        if not self._guard():
            return
        path = urlparse(self.path).path
        if path == "/":
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if path == "/api/apps":
            return self._send(200, json.dumps({"title": TITLE, "python": sys.executable,
                                               "apps": [a.status() for a in APPS.values()]}, ensure_ascii=False))
        m = re.fullmatch(r"/api/apps/([a-z0-9_]+)/log", path)
        if m and m.group(1) in APPS:
            return self._send(200, tail_text(APPS[m.group(1)].log_path, 65536), "text/plain; charset=utf-8")
        self._send(404, "Not Found", "text/plain; charset=utf-8")

    def do_POST(self):
        if not self._guard(post=True):
            return
        path = urlparse(self.path).path
        if path == "/api/quit":
            self._send(200, '{"ok":true}')
            threading.Thread(target=SERVER.shutdown, daemon=True).start()
            return
        m = re.fullmatch(r"/api/apps/([a-z0-9_]+)/(start|stop|restart)", path)
        if not m or m.group(1) not in APPS:
            return self._send(404, '{"ok":false,"error":"not found"}')
        app, action = APPS[m.group(1)], m.group(2)
        try:
            if action in ("stop", "restart"):
                app.stop()
            if action in ("start", "restart"):
                app.start()
        except AppError as e:
            return self._send(200, json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        self._send(200, '{"ok":true}')

    def log_message(self, fmt, *args):
        pass


def main():
    global SERVER, TITLE, APPS
    p = argparse.ArgumentParser(description="業務ツールボックス（ポータル）")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--start", default="all", help="起動と同時に開始するアプリ（id をカンマ区切り / all=全部（既定） / none=開始しない）")
    p.add_argument("--no-browser", action="store_true", help="ブラウザを自動で開かない")
    a = p.parse_args()
    TITLE, APPS = load_apps()
    atexit.register(shutdown_all)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        SERVER = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    except OSError as e:
        raise SystemExit(f"ポート {a.port} を使えません（{e}）。--port で別のポートを指定してください。")
    ids = list(APPS) if a.start == "all" else [] if a.start == "none" else [x.strip() for x in a.start.split(",") if x.strip()]
    for i in ids:
        if i not in APPS:
            print(f"--start: 不明なアプリ id です: {i}")
            continue
        try:
            APPS[i].start()
        except AppError as e:
            print(f"{i}: {e}")
    url = f"http://127.0.0.1:{a.port}"
    print(f"{TITLE} を起動しました: {url}  (終了は Ctrl+C)")
    if not a.no_browser:
        threading.Timer(0.6, webbrowser.open, args=(url,)).start()
    try:
        SERVER.serve_forever()
    except KeyboardInterrupt:
        print("\n終了します")
    finally:
        shutdown_all()


if __name__ == "__main__":
    main()
