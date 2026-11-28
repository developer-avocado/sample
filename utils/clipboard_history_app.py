from datetime import datetime
from typing import Dict, List

import pyperclip
import streamlit as st

MAX_HISTORY_COUNT = 10

st.set_page_config(page_title="クリップボード履歴（10件）", layout="centered")
st.title("📋 クリップボード履歴アプリ（最大10件）")


def init_session_state() -> None:
    """セッション状態を初期化する"""
    if "clipboard_history" not in st.session_state:
        st.session_state.clipboard_history: List[Dict[str, str]] = []
    if "last_recorded_text" not in st.session_state:
        st.session_state.last_recorded_text = ""


def record_clipboard_text(text: str) -> None:
    """コピー内容を履歴の先頭に追加し、最大10件に保つ"""
    cleaned_history = [
        item for item in st.session_state.clipboard_history if item["text"] != text
    ]
    new_entry = {
        "text": text,
        "copied_at": datetime.now().strftime("%H:%M:%S"),
    }
    cleaned_history.insert(0, new_entry)
    st.session_state.clipboard_history = cleaned_history[:MAX_HISTORY_COUNT]
    st.session_state.last_recorded_text = text


def copy_back_to_clipboard(text: str) -> None:
    """履歴のテキストをクリップボードへ再コピーする"""
    pyperclip.copy(text)
    st.session_state.last_recorded_text = text
    st.toast("クリップボードに再コピーしました。")


def delete_history_item(target_index: int) -> None:
    """指定した位置の履歴を削除する"""
    if 0 <= target_index < len(st.session_state.clipboard_history):
        st.session_state.clipboard_history.pop(target_index)


init_session_state()

# サイドバー：監視設定と操作
st.sidebar.header("設定")
is_monitoring = st.sidebar.toggle("クリップボード自動監視", value=True)
search_query = st.sidebar.text_input("履歴を検索", value="")

if st.sidebar.button("履歴をすべて消去", use_container_width=True):
    st.session_state.clipboard_history = []
    st.session_state.last_recorded_text = ""
    st.rerun()

st.sidebar.markdown("---")
st.sidebar.caption("※ 最大10件まで保持し、古い履歴から自動で押し出されます。")


@st.fragment(run_every="1s")
def monitor_and_render_history() -> None:
    """1秒ごとにクリップボードを監視し、履歴一覧を描画する"""
    if is_monitoring:
        try:
            current_text = pyperclip.paste()
        except pyperclip.PyperclipException as err:
            st.error(f"クリップボードの読み取りに失敗しました: {err}")
            return

        if (
            isinstance(current_text, str)
            and current_text.strip()
            and current_text != st.session_state.last_recorded_text
        ):
            record_clipboard_text(current_text)

    history_items = st.session_state.clipboard_history
    if search_query:
        history_items = [
            item
            for item in history_items
            if search_query.lower() in item["text"].lower()
        ]

    st.caption(
        f"記録件数: {len(st.session_state.clipboard_history)} / {MAX_HISTORY_COUNT} 件"
    )

    if not history_items:
        st.info("テキストをコピーすると、ここに最大10件まで自動で記録されます。")
        return

    for idx, item in enumerate(history_items):
        with st.container(border=True):
            header_col, copy_col, del_col = st.columns([6, 2, 2])
            header_col.markdown(
                f"**#{idx + 1}** `{item['copied_at']}` （{len(item['text'])} 文字）"
            )

            if copy_col.button("📋 再コピー", key=f"copy_{idx}", use_container_width=True):
                copy_back_to_clipboard(item["text"])

            if del_col.button("🗑️ 削除", key=f"del_{idx}", use_container_width=True):
                original_index = st.session_state.clipboard_history.index(item)
                delete_history_item(original_index)
                st.rerun()

            st.code(item["text"], language="text")


monitor_and_render_history()