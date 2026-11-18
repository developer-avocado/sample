from pathlib import Path
import pandas as pd
import zstandard as zstd
from google.protobuf.json_format import MessageToDict
from google.protobuf.message import Message
import candata_pb2


def export_candata_zst_to_csv(input_path: str, output_csv_path: str = "candata_output.csv") -> str:
    # 1. candata_pb2 から Protobuf クラスを自動取得
    msg_classes = [
        cls for cls in candata_pb2.__dict__.values()
        if isinstance(cls, type) and issubclass(cls, Message)
    ]
    if not msg_classes:
        raise AttributeError("candata_pb2 内にメッセージクラスが見つかりません。")

    # 2. .zst ファイルをインメモリ解凍
    dctx = zstd.ZstdDecompressor()
    with open(input_path, "rb") as f:
        decompressed_bytes = dctx.decompress(f.read())

    # 3. デシリアライズ
    message = msg_classes[0]()
    message.ParseFromString(decompressed_bytes)

    # 4. 辞書化してフラットな表形式に変換
    data_dict = MessageToDict(message, preserving_proto_field_name=True)
    df = pd.json_normalize([data_dict] if isinstance(data_dict, dict) else data_dict)

    # 5. CSV 保存（Excel文字化け防止の utf-8-sig）
    df.to_csv(output_csv_path, index=False, encoding="utf-8-sig")
    return output_csv_path