import csv
import zstandard as zstd
import candata_pb2


def export_candata_zst_to_csv(input_path: str, output_csv_path: str = "candata_output.csv") -> str:
    # 1. candata_pb2 からメッセージクラスを取得
    msg_names = list(candata_pb2.DESCRIPTOR.message_types_by_name.keys())
    if not msg_names:
        raise AttributeError("candata_pb2 に定義されているメッセージが見つかりません。")
    TargetClass = getattr(candata_pb2, msg_names[0])

    # 2. .zst ファイルを解凍
    dctx = zstd.ZstdDecompressor()
    with open(input_path, "rb") as f:
        decompressed_bytes = dctx.decompress(f.read())

    # 3. パース
    message = TargetClass()
    message.ParseFromString(decompressed_bytes)

    # 4. Protobuf オブジェクトの各フィールド定義から直接ヘッダーと値を取得
    headers = [field.name for field in message.DESCRIPTOR.fields]
    row = [getattr(message, field.name) for field in message.DESCRIPTOR.fields]

    # 5. CSV へ書き出し（utf-8-sig でExcel文字化け防止）
    with open(output_csv_path, mode="w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        writer.writerow(row)

    return output_csv_path