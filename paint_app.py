import io
from PIL import Image
import streamlit as st
from streamlit_drawable_canvas import st_canvas

st.set_page_config(page_title="Pythonペイントアプリ", layout="wide")
st.title("🎨 Python ペイントアプリ")

# サイドバー：描画ツールの設定
st.sidebar.header("描画設定")

drawing_mode = st.sidebar.selectbox(
    "描画モード",
    options=["freedraw", "line", "rect", "circle", "polygon", "text"],
    format_func=lambda x: {
        "freedraw": "フリーハンド（ペン）",
        "line": "直線",
        "rect": "四角形",
        "circle": "円",
        "polygon": "多角形",
        "text": "テキスト",
    }[x],
)

stroke_width = st.sidebar.slider("ペンの太さ", min_value=1, max_value=30, value=4)
stroke_color = st.sidebar.color_picker("線の色", value="#000000")
fill_color = st.sidebar.color_picker("図形の塗りつぶし色", value="#ff9900")
bg_color = st.sidebar.color_picker("背景色", value="#ffffff")

st.sidebar.markdown("---")
bg_image_file = st.sidebar.file_uploader(
    "背景画像を読み込む（任意）",
    type=["png", "jpg", "jpeg"],
)

background_image = None
if bg_image_file is not None:
    background_image = Image.open(bg_image_file)

# キャンバスサイズの指定
canvas_width = st.sidebar.number_input("キャンバス幅 (px)", min_value=300, max_value=1200, value=800, step=50)
canvas_height = st.sidebar.number_input("キャンバス高さ (px)", min_value=300, max_value=900, value=500, step=50)

# 描画キャンバスの表示
canvas_result = st_canvas(
    fill_color=fill_color,
    stroke_width=stroke_width,
    stroke_color=stroke_color,
    background_color=bg_color,
    background_image=background_image,
    height=int(canvas_height),
    width=int(canvas_width),
    drawing_mode=drawing_mode,
    return_image_data=True,
    key="paint_canvas",
)

# 描画結果のPNG保存機能
st.markdown("---")
if canvas_result.image_data is not None:
    pil_image = Image.fromarray(canvas_result.image_data.astype("uint8"), mode="RGBA")
    buffer = io.BytesIO()
    pil_image.save(buffer, format="PNG")

    st.download_button(
        label="💾 描いた画像をPNGで保存",
        data=buffer.getvalue(),
        file_name="my_painting.png",
        mime="image/png",
    )