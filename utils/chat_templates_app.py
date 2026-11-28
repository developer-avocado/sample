import json
from pathlib import Path
import re
from typing import Dict, List
import uuid

import pyperclip
import streamlit as st

DATA_FILE = Path("chat_templates.json")
PLACEHOLDER_PATTERN = re.compile(r"\{([^{}]+)\}")

DEFAULT_TEMPLATES: List[Dict[str, str]] = [
    {
        "id": "default-1",
        "category": "確認依頼",
        "title": "ファイル確認のお願い",
        "body": "{相手名}さん\nお疲れ様です。\n{対象物}を共有いたします。\nお手数ですが、{期限}までにご確認をお願いいたします。",
    },
    {
        "id": "default-2",
        "category": "進捗報告",
        "title": "作業完了の連絡",
        "body": "{相手名}さん\nお疲れ様です。\n{作業名}が完了いたしました。\n問題があればお知らせください。",
    },
    {
        "id": "default-3",
        "category": "日程調整",
        "title": "打ち合わせの相談",
        "body": "{相手名}さん\nお疲れ様です。\n{議題}について、少しご相談させてください。\n{候補日時}あたりでご都合いかがでしょうか。",
    },
]

st.set_page_config(page_title="チャット定型文アプリ", layout="wide")
st.title("💬 チャット定型文管理アプリ")


def load_templates() -> List[Dict[str, str]]:
    """JSONファイルから定型文一覧を読み込む"""
    if not DATA_FILE.exists():
        save_templates(DEFAULT_TEMPLATES)
        return list(DEFAULT_TEMPLATES)
    with DATA_FILE.open("r", encoding="utf-8") as file:
        return json.load(file)


def save_templates(templates: List[Dict[str, str]]) -> None:
    """定型文一覧をJSONファイルへ保存する"""
    with DATA_FILE.open("w", encoding="utf-8") as file:
        json.dump(templates, file, ensure_ascii=False, indent=2)


def extract_placeholders(template_body: str) -> List[str]:
    """本文中の {変数名} を重複なしで抽出する"""
    return list(dict.fromkeys(PLACEHOLDER_PATTERN.findall(template_body)))


def render_template_body(template_body: str, values: Dict[str, str]) -> str:
    """プレースホルダを入力値で置換した本文を返す"""
    rendered = template_body
    for key, val in values.items():
        if val:
            rendered = rendered.replace(f"{{{key}}}", val)
    return rendered


templates = load_templates()

# サイドバー：新規登録フォーム
st.sidebar.header("➕ 定型文を新規登録")
with st.sidebar.form("add_template_form", clear_on_submit=True):
    new_category = st.text_input("カテゴリ", placeholder="例：確認依頼、挨拶、障害連絡")
    new_title = st.text_input("タイトル", placeholder="例：レビュー依頼")
    new_body = st.text_area(
        "本文（{相手名} のように変数を入れられます）",
        height=150,
        placeholder="{相手名}さん\nお疲れ様です。\n...",
    )
    submitted = st.form_submit_button("登録する", use_container_width=True)

    if submitted:
        if not new_title.strip() or not new_body.strip():
            st.sidebar.error("タイトルと本文は必須です。")
        else:
            templates.insert(
                0,
                {
                    "id": str(uuid.uuid4()),
                    "category": new_category.strip() or "未分類",
                    "title": new_title.strip(),
                    "body": new_body.strip(),
                },
            )
            save_templates(templates)