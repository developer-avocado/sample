#!/usr/bin/env python3
"""Robot Framework テストケースビルダー

画面上でテストケース・キーワード・変数を組み立てて、.robot ファイルを生成するWebアプリです。
標準ライブラリのみで動作します（追加インストール不要）。

    python3 robot_builder.py                # http://127.0.0.1:8300
    python3 robot_builder.py --port 9000

- プロジェクト（編集内容）は同じフォルダの robot_projects.db (SQLite) に保存できます
  （環境変数 ROBOT_BUILDER_DB で変更可）。
- 生成する構文は Robot Framework 6.0 以降向けです（Test Tags / RETURN を使用）。
- 入力値は文字どおりに出力されます。バックスラッシュ・先頭の # ・連続する空白・空文字は
  自動でエスケープされるので、Windowsのパスもそのまま入力できます。
"""
import argparse
import json
import os
import re
import sqlite3
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DB_PATH = os.environ.get(
    "ROBOT_BUILDER_DB",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "robot_projects.db"),
)
SEP = "    "

# キーワード辞書: ライブラリ -> [(キーワード名, [引数名...])]  ("=" 付きは省略可能な引数)
CATALOG = {
    "BuiltIn": [
        ("Log", ["message", "level=INFO"]), ("Log To Console", ["message"]),
        ("Sleep", ["time"]), ("Set Variable", ["value"]),
        ("Set Test Variable", ["name", "value"]), ("Set Suite Variable", ["name", "value"]),
        ("Should Be Equal", ["first", "second"]),
        ("Should Be Equal As Strings", ["first", "second"]),
        ("Should Be Equal As Integers", ["first", "second"]),
        ("Should Contain", ["container", "item"]), ("Should Not Contain", ["container", "item"]),
        ("Should Not Be Empty", ["item"]), ("Should Be True", ["condition"]),
        ("Should Be Empty", ["item"]), ("Get Length", ["item"]),
        ("Convert To Integer", ["item"]), ("Create List", ["items"]),
        ("Evaluate", ["expression"]), ("Run Keyword If", ["condition", "name", "args"]),
        ("Wait Until Keyword Succeeds", ["retry", "retry_interval", "name", "args"]),
        ("Fail", ["msg=None"]), ("No Operation", []),
    ],
    "SeleniumLibrary": [
        ("Open Browser", ["url", "browser=firefox"]), ("Close Browser", []),
        ("Close All Browsers", []), ("Maximize Browser Window", []), ("Go To", ["url"]),
        ("Click Element", ["locator"]), ("Click Button", ["locator"]), ("Click Link", ["locator"]),
        ("Input Text", ["locator", "text"]), ("Input Password", ["locator", "password"]),
        ("Select From List By Label", ["locator", "labels"]),
        ("Page Should Contain", ["text"]), ("Page Should Not Contain", ["text"]),
        ("Page Should Contain Element", ["locator"]),
        ("Element Should Be Visible", ["locator"]),
        ("Element Text Should Be", ["locator", "expected"]),
        ("Wait Until Element Is Visible", ["locator", "timeout=None"]),
        ("Wait Until Page Contains", ["text", "timeout=None"]),
        ("Get Text", ["locator"]), ("Get Location", []), ("Title Should Be", ["title"]),
        ("Capture Page Screenshot", []),
    ],
    "Browser": [
        ("New Browser", ["browser=chromium", "headless=False"]), ("New Context", []),
        ("New Page", ["url=None"]), ("Click", ["selector"]), ("Fill Text", ["selector", "txt"]),
        ("Wait For Elements State", ["selector", "state=visible"]),
        ("Take Screenshot", []), ("Get Element Count", ["selector"]),
    ],
    "RequestsLibrary": [
        ("Create Session", ["alias", "url"]), ("GET On Session", ["alias", "url"]),
        ("POST On Session", ["alias", "url"]), ("PUT On Session", ["alias", "url"]),
        ("DELETE On Session", ["alias", "url"]),
        ("Status Should Be", ["expected_status"]),
    ],
    "Collections": [
        ("Create Dictionary", ["items"]), ("Get From Dictionary", ["dictionary", "key"]),
        ("Dictionary Should Contain Key", ["dictionary", "key"]),
        ("Append To List", ["list_", "values"]), ("List Should Contain Value", ["list_", "value"]),
        ("Get From List", ["list_", "index"]),
    ],
    "String": [
        ("Convert To Upper Case", ["string"]), ("Convert To Lower Case", ["string"]),
        ("Get Substring", ["string", "start", "end=None"]),
        ("Replace String", ["string", "search_for", "replace_with"]),
        ("Split String", ["string"]), ("Should Match Regexp", ["string", "pattern"]),
    ],
    "OperatingSystem": [
        ("File Should Exist", ["path"]), ("Create File", ["path", "content"]),
        ("Remove File", ["path"]), ("Get File", ["path"]),
        ("Directory Should Exist", ["path"]),
    ],
    "DateTime": [("Get Current Date", []), ("Add Time To Date", ["date", "time"])],
}


def norm(s):
    return re.sub(r"[ _]", "", str(s or "")).lower()


CAT_INDEX = {}
for _lib, _kws in CATALOG.items():
    for _name, _args in _kws:
        CAT_INDEX.setdefault(norm(_name), (_lib, _name, _args))


# ---------------------------------------------------------------- .robot 生成
def clean(s):
    """名前など1セルに収める文字列（空白を1つに）"""
    return re.sub(r"\s+", " ", str(s if s is not None else "")).strip()


def esc(a, empty_marker=True):
    """引数1つを、見たままの値になるようにRobotの1セルへエスケープする"""
    a = str(a if a is not None else "")
    a = a.replace("\r", "").replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n")
    if a == "":
        return "${EMPTY}" if empty_marker else ""
    if a.strip(" ") == "":
        return "\\ " * len(a)
    a = re.sub(r" {2,}", lambda m: " " + "\\ " * (len(m.group()) - 1), a)
    if a.startswith(" "):
        a = "\\" + a
    if a.endswith(" "):
        a = a[:-1] + "\\ "
    if a.startswith("#"):
        a = "\\" + a
    if a == "...":
        a = "\\..."
    return a


def var(n):
    n = clean(n).rstrip("=").strip()
    if not n:
        return ""
    if n[0] in "$@&" and n[1:2] == "{":
        return n
    return "${" + n + "}"


def doc_lines(prefix, text, indent=""):
    parts = str(text or "").replace("\r", "").split("\n")
    out = [indent + prefix + SEP + esc(parts[0], False)]
    out += [indent + "..." + ((SEP + esc(p, False)) if p.strip() else "") for p in parts[1:]]
    return out


def csv_items(s):
    if isinstance(s, list):
        return [clean(x) for x in s if clean(x)]
    return [clean(x) for x in str(s or "").replace("、", ",").split(",") if clean(x)]


def call_cells(step, allow_assign=True):
    kw = clean(step.get("kw"))
    if not kw:
        return None
    cells = []
    assign = str(step.get("assign") or "").strip()
    if allow_assign and assign:
        vs = [var(v) for v in re.split(r"[,\s]+", assign) if v.strip()]
        if vs:
            vs[-1] += "="
            cells += vs
    cells.append(kw)
    args = [str(a) for a in (step.get("args") or [])]
    while args and args[-1] == "":
        args.pop()  # 末尾の空欄は無視
    return cells + [esc(a) for a in args]


def step_line(step, indent=SEP, allow_assign=True):
    c = call_cells(step, allow_assign)
    return indent + SEP.join(c) if c else None


def generate(p):
    """プロジェクト(dict) -> {"text": .robot本文, "warnings": [警告...]}"""
    warnings = []
    out = []
    user_kws = {norm(k.get("name")) for k in p.get("keywords", []) if clean(k.get("name"))}

    # ---- Settings
    libs = [clean(x) for x in p.get("libraries", []) if clean(x)]
    extra_lines = [re.split(r"\s{2,}|\t", l.strip()) for l in str(p.get("extraLibraries") or "").splitlines() if l.strip()]
    lib_lines = [SEP.join(["Library", esc(x, False)]) for x in libs]
    lib_lines += ["Library" + SEP + SEP.join(esc(c, False) for c in cols) for cols in extra_lines]
    imported = {norm(x) for x in libs} | {norm(c[0]) for c in extra_lines}
    settings = []
    if str(p.get("documentation") or "").strip():
        settings += doc_lines("Documentation", p["documentation"])
    settings += lib_lines
    settings += [f"Resource{SEP}{esc(r.strip(), False)}" for r in str(p.get("resources") or "").splitlines() if r.strip()]
    for label, key in (("Suite Setup", "suiteSetup"), ("Suite Teardown", "suiteTeardown")):
        c = call_cells(p.get(key) or {}, False)
        if c:
            settings.append(label + SEP + SEP.join(c))
    tags = csv_items(p.get("tags"))
    if tags:
        settings.append("Test Tags" + SEP + SEP.join(esc(t, False) for t in tags))
    if settings:
        out += ["*** Settings ***"] + settings + [""]

    # ---- Variables
    vlines = []
    for v in p.get("variables", []):
        name = var(v.get("name"))
        if name:
            val = esc(v.get("value"), False)
            vlines.append(name + (SEP + val if val else ""))
    if vlines:
        out += ["*** Variables ***"] + vlines + [""]

    # ---- Test Cases
    used = []  # 使われているキーワード（ライブラリ未import警告用）

    def body(owner, label, setup, steps, teardown, doc="", tag_text="", extra=None):
        lines = []
        if str(doc or "").strip():
            lines += doc_lines("[Documentation]", doc, SEP)
        t = csv_items(tag_text)
        if t:
            lines.append(SEP + "[Tags]" + SEP + SEP.join(esc(x, False) for x in t))
        if extra:
            lines += extra
        c = call_cells(setup or {}, False)
        if c:
            lines.append(SEP + "[Setup]" + SEP + SEP.join(c))
            used.append(clean(setup["kw"]))
        n = 0
        for s in steps or []:
            ln = step_line(s)
            if ln:
                lines.append(ln)
                used.append(clean(s["kw"]))
                n += 1
            elif any(str(a).strip() for a in s.get("args", [])) or str(s.get("assign") or "").strip():
                warnings.append(f"{label}「{owner}」に、キーワード名が空のステップがあります（出力されません）。")
        return lines, n

    tests = p.get("tests", [])
    if tests:
        tl = ["*** Test Cases ***"]
        seen = {}
        for i, t in enumerate(tests, 1):
            name = clean(t.get("name"))
            if not name:
                warnings.append(f"{i}番目のテストケースに名前がありません。")
                name = f"(名前未設定 {i})"
            if norm(name) in seen:
                warnings.append(f"テストケース名「{name}」が重複しています。")
            seen[norm(name)] = True
            lines, n = body(name, "テストケース", t.get("setup"), t.get("steps"), t.get("teardown"),
                            t.get("doc"), t.get("tags"))
            if n == 0:
                warnings.append(f"テストケース「{name}」にステップがないため、No Operation を出力しました。")
                lines.append(SEP + "No Operation")
            c = call_cells(t.get("teardown") or {}, False)
            if c:
                lines.append(SEP + "[Teardown]" + SEP + SEP.join(c))
                used.append(clean(t["teardown"]["kw"]))
            tl += [name] + lines + [""]
        out += tl
    else:
        warnings.append("テストケースがありません（このままでは robot で実行できません）。")

    # ---- Keywords
    kws = [k for k in p.get("keywords", []) if clean(k.get("name"))]
    if kws:
        kl = ["*** Keywords ***"]
        for k in kws:
            name = clean(k["name"])
            extra = []
            args = csv_items(k.get("args"))
            if args:
                cells = []
                for a in args:
                    if "=" in a:
                        n, d = a.split("=", 1)
                        cells.append(var(n) + "=" + esc(d.strip(), False))
                    else:
                        cells.append(var(a))
                extra.append(SEP + "[Arguments]" + SEP + SEP.join(cells))
            lines, n = body(name, "ユーザーキーワード", None, k.get("steps"), None, k.get("doc"), "", extra)
            ret = str(k.get("ret") or "").strip()
            if n == 0 and not ret:
                warnings.append(f"ユーザーキーワード「{name}」にステップがないため、No Operation を出力しました。")
                lines.append(SEP + "No Operation")
            if ret:
                lines.append(SEP + "RETURN" + SEP + esc(ret))
            kl += [name] + lines + [""]
        out += kl

    # ---- ライブラリ未importの警告
    warned = set()
    for kw in used:
        n = norm(kw)
        info = CAT_INDEX.get(n)
        if info and n not in user_kws and info[0] != "BuiltIn" and norm(info[0]) not in imported and info[0] not in warned:
            warned.add(info[0])
            warnings.append(f"「{kw}」は {info[0]} のキーワードですが、Library に {info[0]} がありません。")

    text = "\n".join(out).rstrip("\n") + "\n" if out else ""
    return {"text": text, "warnings": warnings}


# ---------------------------------------------------------------- DB
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db() as con:
        con.execute("CREATE TABLE IF NOT EXISTS projects (name TEXT PRIMARY KEY, data TEXT NOT NULL, "
                    "updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")


# ---------------------------------------------------------------- 画面
PAGE = r"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Robot Framework テストビルダー</title>
<style>
:root{--bg:#f4f6f9;--card:#fff;--ink:#1f2933;--sub:#6b7785;--line:#e1e6ec;--accent:#0b6bcb}
*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,"Hiragino Sans","Yu Gothic",Meiryo,sans-serif;background:var(--bg);color:var(--ink)}
header{background:var(--accent);color:#fff;padding:10px 20px;display:flex;gap:10px;flex-wrap:wrap;align-items:center}
header b{font-size:17px;margin-right:6px}
header input,header select{padding:6px 8px;border:none;border-radius:6px;font:inherit;font-size:14px}
header button{background:rgba(255,255,255,.18);color:#fff;border:1px solid rgba(255,255,255,.4)}
input,select,button,textarea{font:inherit;font-size:14px;padding:6px 9px;border:1px solid var(--line);border-radius:6px;background:#fff;color:var(--ink)}
textarea{width:100%;resize:vertical}
button{cursor:pointer}button.primary{background:var(--accent);color:#fff;border:none}
button.mini{padding:4px 8px;font-size:12px}button.danger{color:#c0392b;border-color:#e8c4bf}
main{display:grid;grid-template-columns:minmax(0,1.3fr) minmax(0,1fr);gap:16px;padding:16px;max-width:1500px;margin:0 auto}
@media(max-width:1000px){main{grid-template-columns:1fr}}
.tabs{display:flex;gap:4px;margin-bottom:10px;flex-wrap:wrap}
.tabs button{border-radius:99px;padding:6px 14px}.tabs button.on{background:var(--accent);color:#fff;border-color:var(--accent)}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;margin-bottom:12px;display:grid;gap:8px}
.card h3{margin:0;font-size:13px;color:var(--sub);font-weight:600}
label.f{display:grid;gap:3px;font-size:12px;color:var(--sub)}
.row{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.head{display:flex;gap:6px;align-items:center}.head input{flex:1;font-weight:600}
.step{display:flex;gap:6px;flex-wrap:wrap;align-items:center;background:#f8fafc;border:1px solid var(--line);border-radius:8px;padding:6px}
.step .as{width:120px}.step .kw{flex:1 1 200px;min-width:160px}
.step .args{display:flex;gap:4px;flex-wrap:wrap;align-items:center;flex:2 1 260px}.step .arg{flex:1 1 130px;min-width:110px}
.lbl{font-size:11px;color:var(--sub);width:60px}
.vrow{display:grid;grid-template-columns:1fr 2fr auto;gap:6px}
.check{display:flex;gap:14px;flex-wrap:wrap;font-size:14px}
.check label{display:flex;gap:4px;align-items:center}
.side{position:sticky;top:12px;align-self:start;max-height:calc(100vh - 24px);display:flex;flex-direction:column}
@media(max-width:1000px){.side{position:static;max-height:none}}
#out{margin:0;background:#1e2430;color:#e6edf3;padding:14px;border-radius:8px;overflow:auto;font:13px/1.55 ui-monospace,Menlo,Consolas,monospace;white-space:pre;min-height:200px;flex:1}
#warn{font-size:12px;color:#8a5a00;background:#fff4d6;border-radius:6px;padding:6px 10px;margin-bottom:8px}#warn:empty{display:none}
#warn div+div{margin-top:3px}
#toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%);background:#1f2933;color:#fff;padding:9px 16px;border-radius:99px;font-size:13px;opacity:0;transition:.2s;pointer-events:none;z-index:9}
#toast.show{opacity:.95}
.hint{font-size:12px;color:var(--sub)}
</style></head><body>
<header>
  <b>Robot Framework テストビルダー</b>
  <input id="pname" placeholder="ファイル名 / プロジェクト名" size="20">
  <button id="bNew">新規</button><button id="bSample">サンプル読込</button>
  <select id="saved"><option value="">保存済みを開く…</option></select>
  <button id="bDelSaved">削除</button><button id="bSave">サーバに保存</button>
</header>
<main>
  <section><div class="tabs" id="tabs"></div><div id="panel"></div></section>
  <aside class="side card" style="align-content:start;grid-template-rows:auto auto 1fr">
    <div class="row"><h3 style="flex:1">生成される .robot ファイル</h3>
      <button id="bCopy">コピー</button><button class="primary" id="bDl">ダウンロード</button></div>
    <div id="warn"></div><pre id="out"></pre>
  </aside>
</main>
<datalist id="kwlist"></datalist><div id="toast"></div>

<script>
const $ = s => document.querySelector(s);
function h(tag, props = {}, ...kids){
  const e = document.createElement(tag);
  for(const [k,v] of Object.entries(props)){
    if(k === 'class') e.className = v; else if(k.startsWith('on')) e[k] = v;
    else if(k === 'value' || k === 'checked') e[k] = v; else e.setAttribute(k, v);
  }
  for(const c of kids.flat()) if(c != null) e.append(c.nodeType ? c : document.createTextNode(c));
  return e;
}
const norm = s => (s||'').replace(/[ _]/g,'').toLowerCase();
const LIBS = ['SeleniumLibrary','Browser','RequestsLibrary','Collections','String','OperatingSystem','DateTime'];
const blank = () => ({name:'', documentation:'', libraries:[], extraLibraries:'', resources:'',
  suiteSetup:{kw:'',args:[]}, suiteTeardown:{kw:'',args:[]}, tags:'', variables:[], tests:[], keywords:[]});
const newStep = () => ({assign:'', kw:'', args:[]});
const newTest = () => ({name:'', doc:'', tags:'', setup:{kw:'',args:[]}, teardown:{kw:'',args:[]}, steps:[newStep()]});
const newKw = () => ({name:'', doc:'', args:'', ret:'', steps:[newStep()]});
let state = blank(), tab = 'tests', catIdx = {};

function fix(d){ const s = Object.assign(blank(), d || {});
  for(const k of ['suiteSetup','suiteTeardown']) s[k] = Object.assign({kw:'',args:[]}, s[k]);
  for(const t of s.tests){ t.setup = Object.assign({kw:'',args:[]}, t.setup); t.teardown = Object.assign({kw:'',args:[]}, t.teardown); t.steps = t.steps || []; }
  for(const k of s.keywords) k.steps = k.steps || [];
  return s; }
function toast(m){ const t=$('#toast'); t.textContent=m; t.classList.add('show'); clearTimeout(toast.t); toast.t=setTimeout(()=>t.classList.remove('show'),1600); }
const api = async (method, url, data) => { const r = await fetch(url, {method, headers:{'Content-Type':'application/json'}, body: data ? JSON.stringify(data) : undefined}); return r.json(); };

// ---- 変更通知・プレビュー
let pt;
function changed(){ try{ localStorage.setItem('robotDraft', JSON.stringify(state)); }catch(e){} clearTimeout(pt); pt = setTimeout(preview, 150); }
async function preview(){
  const d = await api('POST', '/api/generate', state);
  $('#out').textContent = d.text || '（左のフォームで入力すると、ここに表示されます）';
  const w = $('#warn'); w.innerHTML = ''; for(const m of d.warnings) w.append(h('div', {}, '⚠ ' + m));
}
function refresh(){ const y = window.scrollY; renderTabs(); renderPanel(); refreshKwList(); window.scrollTo(0, y); changed(); }

// ---- キーワード情報
function kwInfo(name){
  const n = norm(name); if(!n) return null;
  const u = state.keywords.find(k => norm(k.name) === n);
  if(u) return {lib:null, args: u.args.split(',').map(s => s.trim()).filter(Boolean)};
  return catIdx[n] || null;
}
function refreshKwList(){
  const dl = $('#kwlist'); dl.innerHTML = '';
  const names = [...state.keywords.map(k=>k.name).filter(Boolean), ...Object.values(catIdx).map(i=>i.name)];
  for(const n of new Set(names)) dl.append(h('option', {value:n}));
}
function applyKw(step){   // 変更があれば true
  const info = kwInfo(step.kw); if(!info) return false; let ch = false;
  if(!step.args.length){ step.args = info.args.filter(a => !a.includes('=')).map(() => ''); ch = step.args.length > 0; }
  if(info.lib && info.lib !== 'BuiltIn' && !state.libraries.includes(info.lib)){ state.libraries.push(info.lib); toast(`Library に ${info.lib} を追加しました`); }
  return ch;
}

// ---- ステップ編集
function stepRow(step, o = {}){
  const row = h('div', {class:'step'});
  if(o.label) row.append(h('span', {class:'lbl'}, o.label));
  if(!o.noAssign) row.append(h('input', {class:'as', placeholder:'${戻り値}', value:step.assign, oninput:e=>{step.assign=e.target.value;changed();}}));
  const kw = h('input', {class:'kw', list:'kwlist', placeholder:'キーワード（例: Click Element）', value:step.kw, oninput:e=>{step.kw=e.target.value;changed();}});
  kw.onchange = () => {
    if(applyKw(step)){ const nr = stepRow(step, o); row.replaceWith(nr); const f = nr.querySelector('.arg'); if(f) f.focus(); }
    else { const info = kwInfo(step.kw); row.querySelectorAll('.arg').forEach((a,i) => a.placeholder = (info && info.args[i]) || `引数${i+1}`); }
    changed();
  };
  row.append(kw);
  const info = kwInfo(step.kw), args = h('span', {class:'args'});
  step.args.forEach((a,i) => args.append(h('input', {class:'arg', placeholder:(info && info.args[i]) || `引数${i+1}`, value:a, oninput:e=>{step.args[i]=e.target.value;changed();}})));
  args.append(h('button', {class:'mini', title:'引数を追加', onclick:()=>{step.args.push(''); refresh();}}, '＋'));
  if(step.args.length) args.append(h('button', {class:'mini', title:'引数を減らす', onclick:()=>{step.args.pop(); refresh();}}, '－'));
  row.append(args);
  if(o.list){
    const i = o.list.indexOf(step);
    row.append(h('button', {class:'mini', title:'上へ', onclick:()=>move(o.list, i, -1)}, '↑'),
               h('button', {class:'mini', title:'下へ', onclick:()=>move(o.list, i, 1)}, '↓'),
               h('button', {class:'mini danger', title:'削除', onclick:()=>{o.list.splice(i,1); refresh();}}, '✕'));
  }
  return row;
}
function move(list, i, d){ const j = i + d; if(j < 0 || j >= list.length) return; [list[i], list[j]] = [list[j], list[i]]; refresh(); }
function steps(list){
  const box = h('div', {class:'row', style:'display:grid;gap:6px'});
  list.forEach(s => box.append(stepRow(s, {list})));
  box.append(h('div', {}, h('button', {class:'mini', onclick:()=>{list.push(newStep()); refresh();}}, '＋ ステップを追加')));
  return box;
}
const field = (label, input) => h('label', {class:'f'}, label, input);
const text = (obj, key, ph = '') => h('input', {value:obj[key], placeholder:ph, oninput:e=>{obj[key]=e.target.value;changed();}});

// ---- タブ
function renderTabs(){
  const box = $('#tabs'); box.innerHTML = '';
  const defs = [['settings','設定'],['variables',`変数 (${state.variables.length})`],['tests',`テストケース (${state.tests.length})`],['keywords',`ユーザーキーワード (${state.keywords.length})`]];
  for(const [k,l] of defs) box.append(h('button', {class:tab===k?'on':'', onclick:()=>{tab=k; renderTabs(); renderPanel();}}, l));
}
function renderPanel(){
  const p = $('#panel'); p.innerHTML = '';
  if(tab === 'settings') settingsTab(p); else if(tab === 'variables') variablesTab(p);
  else if(tab === 'tests') testsTab(p); else keywordsTab(p);
}
function settingsTab(p){
  const c = h('div', {class:'card'});
  c.append(field('スイートの説明 (Documentation)', h('textarea', {rows:'2', oninput:e=>{state.documentation=e.target.value;changed();}}, state.documentation)));
  const libs = h('div', {class:'check'});
  for(const l of LIBS) libs.append(h('label', {}, h('input', {type:'checkbox', checked:state.libraries.includes(l), onchange:e=>{
    state.libraries = state.libraries.filter(x => x !== l); if(e.target.checked) state.libraries.push(l); changed(); }}), l));
  c.append(field('ライブラリ（BuiltIn は自動で読み込まれます）', libs));
  c.append(field('その他のライブラリ（1行に1つ。引数は半角スペース2つ以上で区切る）', h('textarea', {rows:'2', placeholder:'例: MyLibrary.py', oninput:e=>{state.extraLibraries=e.target.value;changed();}}, state.extraLibraries)));
  c.append(field('リソースファイル（1行に1つ）', h('textarea', {rows:'2', placeholder:'例: resources/common.resource', oninput:e=>{state.resources=e.target.value;changed();}}, state.resources)));
  c.append(field('共通タグ Test Tags（カンマ区切り）', text(state, 'tags', 'smoke, regression')));
  c.append(h('h3', {}, 'スイートの前処理・後処理'));
  c.append(stepRow(state.suiteSetup, {noAssign:true, label:'Setup'}), stepRow(state.suiteTeardown, {noAssign:true, label:'Teardown'}));
  p.append(c);
}
function variablesTab(p){
  const c = h('div', {class:'card'}, h('div', {class:'hint'}, '名前は URL のように書けば ${URL} として出力されます。'));
  state.variables.forEach((v,i) => c.append(h('div', {class:'vrow'}, text(v,'name','名前（例: URL）'), text(v,'value','値'),
    h('button', {class:'mini danger', onclick:()=>{state.variables.splice(i,1); refresh();}}, '✕'))));
  c.append(h('div', {}, h('button', {class:'mini', onclick:()=>{state.variables.push({name:'',value:''}); refresh();}}, '＋ 変数を追加')));
  p.append(c);
}
function testsTab(p){
  state.tests.forEach((t,i) => {
    const c = h('div', {class:'card'});
    c.append(h('div', {class:'head'}, text(t,'name','テストケース名'),
      h('button', {class:'mini', onclick:()=>move(state.tests,i,-1)}, '↑'), h('button', {class:'mini', onclick:()=>move(state.tests,i,1)}, '↓'),
      h('button', {class:'mini', onclick:()=>{const d=JSON.parse(JSON.stringify(t)); d.name+=' (コピー)'; state.tests.splice(i+1,0,d); refresh();}}, '複製'),
      h('button', {class:'mini danger', onclick:()=>{ if(confirm('このテストケースを削除しますか？')){state.tests.splice(i,1); refresh();} }}, '削除')));
    c.append(h('div', {class:'row'}, field('説明', text(t,'doc')), field('タグ（カンマ区切り）', text(t,'tags','login, smoke'))));
    c.append(stepRow(t.setup, {noAssign:true, label:'Setup'}), steps(t.steps), stepRow(t.teardown, {noAssign:true, label:'Teardown'}));
    p.append(c);
  });
  p.append(h('button', {class:'primary', onclick:()=>{state.tests.push(newTest()); refresh();}}, '＋ テストケースを追加'));
}
function keywordsTab(p){
  p.append(h('div', {class:'hint', style:'margin-bottom:8px'}, 'よく使う操作をまとめて、テストケースのステップから呼び出せます。'));
  state.keywords.forEach((k,i) => {
    const c = h('div', {class:'card'});
    c.append(h('div', {class:'head'}, text(k,'name','キーワード名'),
      h('button', {class:'mini danger', onclick:()=>{ if(confirm('このキーワードを削除しますか？')){state.keywords.splice(i,1); refresh();} }}, '削除')));
    c.append(h('div', {class:'row'}, field('引数（カンマ区切り。例: user, password=初期値）', text(k,'args')), field('説明', text(k,'doc'))));
    c.append(steps(k.steps), field('戻り値（任意）', text(k,'ret','例: ${result}')));
    p.append(c);
  });
  p.append(h('button', {class:'primary', onclick:()=>{state.keywords.push(newKw()); refresh();}}, '＋ キーワードを追加'));
}

// ---- ヘッダー操作
const SAMPLE = () => fix({name:'login_tests', documentation:'ログイン機能のテスト', libraries:['SeleniumLibrary'], tags:'web',
  variables:[{name:'URL',value:'https://example.com/login'},{name:'BROWSER',value:'chrome'},{name:'USER',value:'demo'},{name:'PASSWORD',value:'secret'}],
  tests:[
   {name:'正しい認証情報でログインできる', doc:'', tags:'smoke, login', setup:{kw:'Open Browser',args:['${URL}','${BROWSER}']}, teardown:{kw:'Close Browser',args:[]},
    steps:[{assign:'',kw:'ログインする',args:['${USER}','${PASSWORD}']},{assign:'',kw:'Page Should Contain',args:['ようこそ']}]},
   {name:'パスワードを間違えるとエラーが表示される', doc:'', tags:'login', setup:{kw:'Open Browser',args:['${URL}','${BROWSER}']}, teardown:{kw:'Close Browser',args:[]},
    steps:[{assign:'',kw:'ログインする',args:['${USER}','wrong']},{assign:'',kw:'Page Should Contain',args:['パスワードが違います']}]}],
  keywords:[{name:'ログインする', doc:'ユーザー名とパスワードでログインする', args:'user, password', ret:'',
    steps:[{assign:'',kw:'Input Text',args:['id:username','${user}']},{assign:'',kw:'Input Password',args:['id:password','${password}']},{assign:'',kw:'Click Button',args:['id:login']}]}]});
function load(s){ state = fix(s); $('#pname').value = state.name; refresh(); }
$('#pname').oninput = e => { state.name = e.target.value; changed(); };
$('#bNew').onclick = () => { if(confirm('現在の編集内容を破棄して新規作成しますか？')) load(blank()); };
$('#bSample').onclick = () => { if(confirm('サンプルを読み込みます。現在の編集内容は破棄されます。')) load(SAMPLE()); };
$('#bCopy').onclick = async () => { const t = $('#out').textContent;
  try{ await navigator.clipboard.writeText(t); }catch(e){ const a=h('textarea'); a.value=t; document.body.append(a); a.select(); document.execCommand('copy'); a.remove(); } toast('コピーしました'); };
$('#bDl').onclick = () => { const n = (state.name || 'test').replace(/[\\/:*?"<>|\s]+/g,'_').replace(/\.robot$/,'');
  const a = h('a', {href:URL.createObjectURL(new Blob([$('#out').textContent], {type:'text/plain;charset=utf-8'})), download:n+'.robot'}); document.body.append(a); a.click(); a.remove(); };
async function loadSavedList(){ const names = await api('GET','/api/projects'); const s = $('#saved'); s.innerHTML = '<option value="">保存済みを開く…</option>';
  for(const n of names) s.append(h('option', {value:n}, n)); }
$('#bSave').onclick = async () => { const n = state.name.trim(); if(!n){ toast('先にファイル名 / プロジェクト名を入力してください'); return; }
  await api('PUT', '/api/projects/' + encodeURIComponent(n), state); toast('保存しました'); loadSavedList(); };
$('#saved').onchange = async e => { const n = e.target.value; if(!n) return;
  if(confirm(`「${n}」を開きます。現在の編集内容は破棄されます。`)){ const d = await api('GET', '/api/projects/' + encodeURIComponent(n)); d.name = n; load(d); } e.target.value = ''; };
$('#bDelSaved').onclick = async () => { const n = state.name.trim(); if(!n) return;
  if(confirm(`サーバ上の「${n}」を削除しますか？（編集中の内容は残ります）`)){ await api('DELETE', '/api/projects/' + encodeURIComponent(n)); toast('削除しました'); loadSavedList(); } };

(async () => {
  for(const i of await api('GET', '/api/catalog')) catIdx[norm(i.name)] = i;
  let draft = null; try{ draft = JSON.parse(localStorage.getItem('robotDraft')); }catch(e){}
  state = fix(draft); $('#pname').value = state.name;
  renderTabs(); renderPanel(); refreshKwList(); loadSavedList(); preview();
})();
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self):
        size = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(size).decode("utf-8"))

    def _pname(self):
        parts = self.path.split("/")
        if len(parts) == 4 and parts[1:3] == ["api", "projects"]:
            name = urllib.parse.unquote(parts[3]).strip()
            return name[:80] if name else None
        return None

    def do_GET(self):
        if self.path == "/":
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if self.path == "/api/catalog":
            items = [{"lib": lib, "name": n, "args": a} for lib, kws in CATALOG.items() for n, a in kws]
            return self._send(200, json.dumps(items, ensure_ascii=False))
        if self.path == "/api/projects":
            with db() as con:
                names = [r["name"] for r in con.execute("SELECT name FROM projects ORDER BY updated_at DESC")]
            return self._send(200, json.dumps(names, ensure_ascii=False))
        name = self._pname()
        if name:
            with db() as con:
                row = con.execute("SELECT data FROM projects WHERE name=?", (name,)).fetchone()
            return self._send(200, row["data"]) if row else self._send(404, '{"error":"not found"}')
        self._send(404, "Not Found", "text/plain; charset=utf-8")

    def do_POST(self):
        if self.path == "/api/generate":
            try:
                project = self._json()
                assert isinstance(project, dict)
            except (ValueError, AssertionError):
                return self._send(400, '{"error":"invalid"}')
            return self._send(200, json.dumps(generate(project), ensure_ascii=False))
        self._send(404, "Not Found", "text/plain; charset=utf-8")

    def do_PUT(self):
        name = self._pname()
        if not name:
            return self._send(404, "Not Found", "text/plain; charset=utf-8")
        try:
            project = self._json()
            assert isinstance(project, dict)
        except (ValueError, AssertionError):
            return self._send(400, '{"error":"invalid"}')
        with db() as con:
            con.execute("INSERT INTO projects (name, data) VALUES (?,?) ON CONFLICT(name) DO UPDATE SET "
                        "data=excluded.data, updated_at=CURRENT_TIMESTAMP",
                        (name, json.dumps(project, ensure_ascii=False)))
        self._send(200, '{"ok":true}')

    def do_DELETE(self):
        name = self._pname()
        if not name:
            return self._send(404, "Not Found", "text/plain; charset=utf-8")
        with db() as con:
            con.execute("DELETE FROM projects WHERE name=?", (name,))
        self._send(200, '{"ok":true}')

    def log_message(self, fmt, *args):
        pass


def main():
    p = argparse.ArgumentParser(description="Robot Framework テストケースビルダー")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8300)
    a = p.parse_args()
    init_db()
    print(f"起動しました: http://{a.host}:{a.port}  (終了は Ctrl+C)")
    try:
        ThreadingHTTPServer((a.host, a.port), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\n終了します")


if __name__ == "__main__":
    main()
