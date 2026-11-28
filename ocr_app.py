from typing import List, Tuple

import easyocr
import numpy as np
from PIL import Image
import streamlit as st
from streamlit_paste_button import paste_image_button

st.set_page_config(page_title="スクショ文字抽出アプリ", layout="wide")
st.title("📷 スクリーンキャプチャ文字抽出（OCR）アプリ")


@st.cache_resource
def load_ocr_reader(languages: Tuple[str, ...]) -> easyocr.Reader:
    """EasyOCRリーダーを初期化してキャッシュする"""
    return easyocr.Reader(list(languages), gpu=False)


def extract_text_from_image(
    reader: easyocr.Reader,
    pil_image: Image.Image,
) -> Tuple[str, List[dict]]:
    """PIL画像からテキストと信頼度スコアを抽出する"""
    rgb_image = pil_image.convert("RGB")
    image_array = np.array(rgb_image)
    raw_results = reader.readtext(image_array, detail=1)

    extracted_lines: List[str] = []
    detail_records: List[dict] = []

    for _bbox, text, confidence in raw_results:
        extracted_lines.append(text)
        detail_records.append(
            {
                "抽出テキスト": text,
                "信頼度 (%)": round(float(confidence) * 100, 1),
            }
        )

    joined_text = "\n".join(extracted_lines)
    return joined_text, detail_records


# サイドバー設定
st.sidebar.header("設定")
lang_option = st.sidebar.radio(
    "認識する言語",
    options=["日本語 + 英語", "英語のみ"],
    index=0,
)
selected_languages = ("ja", "en") if lang_option == "日本語 + 英語" else ("en",)

st.sidebar.markdown("---")
st.sidebar.caption("※ 初回実行時はOCRモデルのダウンロードに数分かかります。")

# 入力エリア
col_left, col_right = st.columns([1, 1])

target_image: Image.Image | None = None

with col_left:
    st.subheader("1. スクリーンキャプチャを貼り付け")
    st.write("画面をコピー後、下のボタンを押して貼り付けてください。")

    paste_result = paste_image_button(
        label="📋 クリップボードから画像を貼り付け",
        text_color="#ffffff",
        background_color="#2563eb",
        hover_background_color="#1d4ed8",
        errors="raise",
    )

    st.write("または、画像ファイルをアップロードできます。")
    uploaded_file = st.file_uploader("画像ファイルを選択", type=["png", "jpg", "jpeg"])

    if paste_result.image_data is not None:
        target_image = paste_result.image_data
    elif uploaded_file is not None:
        target_image = Image.open(uploaded_file)

    if target_image is not None:
        st.image(target_image, caption="読み込んだ画像", use_container_width=True)

with col_right:
    st.subheader("2. 抽出結果")

    if target_image is None:
        st.info("左側から画像を貼り付けると、自動で文字を抽出します。")
    else:
        with st.spinner("文字を抽出しています..."):
            ocr_reader = load_ocr_reader(selected_languages)
            extracted_text, confidence_records = extract_text_from_image(
                ocr_reader, target_image
            )

        if not extracted_text.strip():
            st.warning("画像から文字を検出できませんでした。")
        else:
            st.text_area(
                "抽出されたテキスト（編集可能）",
                value=extracted_text,
                height=260,
            )

            st.caption("右上のアイコンからワンクリックでコピーできます。")
            st.code(extracted_text, language="text")

            st.download_button(
                label="テキストファイルで保存 (.txt)",
                data=extracted_text.encode("utf-8"),
                file_name="extracted_text.txt",
                mime="text/plain",
            )

            with st.expander("認識の詳細（信頼度スコア）を見る"):
                st.dataframe(confidence_records, use_container_width=True)