"""
CANデータ（Protobuf / candata.proto）を解凍・抽出し、
CSVファイルとして出力する処理を1つの関数で完結させたスクリプト
"""

from pathlib import Path
from typing import Any, Dict, List
import pandas as pd
import zstandard as zstd
from google.protobuf.json_format import MessageToDict

# 事前に `protoc --python_out=. candata.proto` で生成したモジュール
try:
    import candata_pb2
except ImportError:
    candata_pb2 = None


def export_candata_zst_to_csv(input_path: str, output_csv_path: str = "candata_output.csv") -> str:
    """
    .zst 圧縮された candata バイナリを解凍し、Protobuf メッセージをパースして
    フラットな表形式の CSV ファイルとして出力する関数
    """
    src_path = Path(input_path)
    if not src_path.is_file():
        raise FileNotFoundError(f"ファイルが見つかりません: {input_path}")

    # 1. .zst ファイルを読み込んでインメモリ解凍
    dctx = zstd.ZstdDecompressor()
    with open(src_path, "rb") as f:
        decompressed_bytes = dctx.decompress(f.read())

    # 2. Protobuf インスタンスを生成してデシリアライズ
    # （※ candata.proto 内のメッセージ型名に合わせて調整）
    message = candata_pb2.CanData()
    message.ParseFromString(decompressed_bytes)

    # 3. 辞書型へ変換
    data_dict = MessageToDict(message, preserving_proto_field_name=True)

    # 4. ネストされた構造を展開（フラット化）して DataFrame 化
    # records 形式または単一オブジェクト形式の両方に対応
    if isinstance(data_dict, list):
        df = pd.json_normalize(data_dict)
    else:
        df = pd.json_normalize([data_dict])

    # 5. CSV ファイルとして書き出し（UTF-8 with BOM でExcelでも文字化け防止）
    df.to_csv(output_csv_path, index=False, encoding="utf-8-sig")
    print(f"CSV出力完了: {output_csv_path} ({len(df)} 行)")

    return output_csv_path


if __name__ == "__main__":
    # 使用例
    export_candata_zst_to_csv("candata_sample.bin.zst", "candata_extracted.csv")