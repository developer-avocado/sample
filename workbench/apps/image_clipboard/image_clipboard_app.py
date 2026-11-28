from datetime import datetime
import hashlib
import io
import zipfile
from typing import Dict, List

from PIL import Image, ImageGrab
import streamlit as st

try:
    import win32clipboard

    HAS_WIN32CLIPBOARD = True
except ImportError:
    HAS_WIN32CLIPBOARD = False

MAX_IMAGE_COUNT = 50

st.set_page_config(page_title="画像クリップボード履歴（50枚）", layout="wide")
st.title("🖼️ 画像クリップボード履歴アプリ（最大50枚）")


def init_session_state() -> None:
    """セッション状態を初期化する"""
    if "image_history" not in st.session_state:
        st.session_state.image_history: List[Dict[str, object]] = []
    if "last_image_hash" not in st.session_state:
        st.session_state.last_image_hash = ""


def image_to_png_bytes(pil_image: Image.Image) -> bytes:
    """PIL画像をPNGのバイト列に変換する"""
    buffer = io.BytesIO()
    pil_image.convert("RGBA").save(buffer, format="PNG")
    return buffer.getvalue()


def copy_image_to_windows_clipboard(png_bytes: bytes) -> None:
    """Windowsのクリップボードに画像をDIB形式で書き込む"""
    if not HAS_WIN32CLIPBOARD:
        st.warning("再コピー機能には pywin32 のインストールが必要です。")
        return

    pil_image = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    bmp_buffer = io.BytesIO()
    pil_image.save(bmp_buffer, format="BMP")
    # BMPヘッダの先頭14バイトを除いてCF_DIB形式にする
    dib_data = bmp_buffer.getvalue()[14:]

    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32clipboard.CF_DIB, dib_data)
    finally:
        win32clipboard.CloseClipboard()


def record_clipboard_image(pil_image: Image.Image) -> None:
    """画像を履歴の先頭に追加し、最大50枚に保つ"""
    png_bytes = image_to_png_bytes(pil_image)
    image_hash = hashlib.sha256(png_bytes).hexdigest()

    if image_hash == st.session_state.last_image_hash:
        return

    cleaned_history = [
        item for item in st.session_state.image_history if item["hash"] != image_hash
    ]
    new_entry = {
        "hash": image_hash,
        "png_bytes": png_bytes,
        "width": pil_image.width,
        "height": pil_image.height,
        "copied_at": datetime.now().strftime("%H:%M:%S"),
    }
    cleaned_history.insert(0, new_entry)
    st.session_state.image_history = cleaned_history[:MAX_IMAGE_COUNT]
    st.session_state.last_image_hash = image_hash


def build_zip(items: List[Dict[str, object]]) -> bytes:
    """画像をPNGのままZIPにまとめる（PNGは圧縮済みなので無圧縮で格納）"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        for number, item in enumerate(items, start=1):
            stamp = str(item["copied_at"]).replace(":", "")
            archive.writestr(f"clip_{number:02d}_{stamp}.png", item["png_bytes"])
    return buffer.getvalue()


def select_all_images() -> None:
    """すべての画像にチェックを入れる"""
    for item in st.session_state.image_history:
        st.session_state[f"sel_{item['hash']}"] = True


def clear_image_selection() -> None:
    """すべてのチェックを外す"""
    for item in st.session_state.image_history:
        st.session_state[f"sel_{item['hash']}"] = False


def render_zip_saver(label: str, zip_key: str, items: List[Dict[str, object]], file_prefix: str) -> None:
    """ZIPの作成ボタンと、作成後のダウンロードボタンを表示する（毎秒の再描画で重くならないよう2段階にする）"""
    hashes = tuple(str(item["hash"]) for item in items)
    if st.button(label, key=f"make_{zip_key}", disabled=not items, use_container_width=True):
        st.session_state[zip_key] = (hashes, build_zip(items), datetime.now().strftime("%Y%m%d_%H%M%S"))
    ready = st.session_state.get(zip_key)
    if ready and ready[0] == hashes and items:
        st.download_button(
            label=f"⬇️ ZIPをダウンロード（{len(items)} 枚）",
            data=ready[1],
            file_name=f"{file_prefix}_{ready[2]}.zip",
            mime="application/zip",
            key=f"dl_{zip_key}",
            use_container_width=True,
        )
    elif ready and items:
        st.caption("画像や選択が変わりました。もう一度ZIPを作成してください。")


init_session_state()

# サイドバー：監視設定と操作
st.sidebar.header("設定")
is_monitoring = st.sidebar.toggle("画像クリップボード自動監視", value=True)

if st.sidebar.button("履歴をすべて消去", use_container_width=True):
    st.session_state.image_history = []
    st.session_state.last_image_hash = ""
    st.rerun()

st.sidebar.markdown("---")
st.sidebar.caption("※ 最大50枚まで保持し、古い画像から自動で押し出されます。")


@st.fragment(run_every="1s")
def monitor_and_render_images() -> None:
    """1秒ごとにクリップボード画像を監視し、一覧を描画する"""
    if is_monitoring:
        try:
            clipboard_content = ImageGrab.grabclipboard()
        except Exception as err:
            st.error(f"クリップボードの読み取りに失敗しました: {err}")
            return

        if isinstance(clipboard_content, Image.Image):
            record_clipboard_image(clipboard_content)

    history = st.session_state.image_history
    st.caption(f"記録枚数: {len(history)} / {MAX_IMAGE_COUNT} 枚")

    if not history:
        st.info("スクリーンショット等をコピーすると、ここに最大50枚まで自動記録されます。")
        return

    # 選択保存・一括保存
    with st.container(border=True):
        selected_items = [
            item for item in history if st.session_state.get(f"sel_{item['hash']}", False)
        ]
        st.markdown(f"**💾 保存**　選択中: {len(selected_items)} / {len(history)} 枚")
        sel_col1, sel_col2 = st.columns(2)
        sel_col1.button("☑️ すべて選択", key="select_all", on_click=select_all_images, use_container_width=True)
        sel_col2.button("☐ 選択解除", key="clear_sel", on_click=clear_image_selection, use_container_width=True)
        save_col1, save_col2 = st.columns(2)
        with save_col1:
            render_zip_saver("📦 選択した画像をZIPにまとめる（選択保存）", "zip_selected", selected_items, "clip_selected")
        with save_col2:
            render_zip_saver("📦 すべての画像をZIPにまとめる（一括保存）", "zip_all", list(history), "clip_all")

    cols = st.columns(2)
    for idx, item in enumerate(history):
        with cols[idx % 2]:
            with st.container(border=True):
                st.checkbox(
                    f"**#{idx + 1}** `{item['copied_at']}` "
                    f"（{item['width']} × {item['height']} px）",
                    key=f"sel_{item['hash']}",
                )
                st.image(item["png_bytes"], use_container_width=True)

                btn_col1, btn_col2, btn_col3 = st.columns(3)

                if btn_col1.button(
                    "📋 再コピー",
                    key=f"recopy_{item['hash']}",
                    use_container_width=True,
                ):
                    copy_image_to_windows_clipboard(item["png_bytes"])
                    st.session_state.last_image_hash = item["hash"]
                    st.toast("画像をクリップボードに再コピーしました。")

                btn_col2.download_button(
                    label="💾 PNG保存",
                    data=item["png_bytes"],
                    file_name=f"clip_{idx + 1}.png",
                    mime="image/png",
                    key=f"save_{item['hash']}",
                    use_container_width=True,
                )

                if btn_col3.button(
                    "🗑️ 削除",
                    key=f"del_{item['hash']}",
                    use_container_width=True,
                ):
                    st.session_state.image_history.pop(idx)
                    st.rerun()


monitor_and_render_images()