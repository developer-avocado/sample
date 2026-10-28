import io
import pandas as pd
from robot.api import logger


def attach_csv_text_to_report(csv_text: str, title: str = "CSVプレビュー"):
    """CSVの生テキスト文字列を受け取り、HTMLテーブルとしてlog.html内に埋め込む関数。

    Parameters:
    - csv_text (str): CSV形式の生データ文字列
    - title (str): レポート上に表示する見出しタイトル
    """
    # 文字列をファイルのように扱って DataFrame に変換
    df = pd.read_csv(io.StringIO(csv_text))

    table_html = df.to_html(
        classes="table table-bordered", index=False, border=1
    )

    content_html = f"""
    <details open style="margin: 8px 0; border: 1px solid #ddd; padding: 8px; border-radius: 4px; background-color: #fafafa;">
        <summary style="font-weight: bold; cursor: pointer; color: #333;">
            📄 {title}
        </summary>
        <div style="overflow-x: auto; margin-top: 6px; background-color: #fff;">
            {table_html}
        </div>
    </details>
    """

    logger.info(content_html, html=True)