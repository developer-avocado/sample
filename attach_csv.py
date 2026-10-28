import pandas as pd
from robot.api import logger


def attach_csv_to_report(csv_path: str, title: str = "CSVプレビュー"):
    """CSVを読み込み、HTMLテーブルとしてlog.html内に埋め込む関数。

    Parameters:
    - csv_path (str): 表示したいCSVファイルのパス
    - title (str): レポート上に表示する見出しタイトル
    """
    df = pd.read_csv(csv_path)

    # pandas の DataFrame を HTML の <table> タグ形式に変換
    table_html = df.to_html(
        classes="table table-bordered", index=False, border=1
    )

    # クリックで開閉できる <details> タグでラップ
    content_html = f"""
    <details open style="margin: 8px 0; border: 1px solid #ddd; padding: 8px; border-radius: 4px; background-color: #fafafa;">
        <summary style="font-weight: bold; cursor: pointer; color: #333;">
            📄 {title} : {csv_path}
        </summary>
        <div style="overflow-x: auto; margin-top: 6px; background-color: #fff;">
            {table_html}
        </div>
    </details>
    """

    # html=True で出力することで、Robot Framework の log.html 上で表としてレンダリングされる
    logger.info(content_html, html=True)