import os
import json
import zstandard as zstd
import boto3
import azure.functions as func
import logging
import psycopg2

# Protobuf 変換用モジュールのインポート
from google.protobuf.json_format import MessageToJson

# ★ 事前に protoc で作成した Python クラスをインポートします
# 例: trip_data.proto から生成された trip_data_pb2.py がプロジェクト内にある場合
import trip_data_pb2  

app = func.FunctionApp()

# --- 各種環境変数の取得 (省略) ---
AWS_ACCESS_KEY = os.environ.get("AWS_ACCESS_KEY_ID")
AWS_SECRET_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY")
AWS_REGION = os.environ.get("AWS_DEFAULT_REGION", "ap-northeast-1")
SQS_QUEUE_URL = os.environ.get("AWS_SQS_QUEUE_URL")
S3_BUCKET_NAME = os.environ.get("AWS_S3_BUCKET_NAME")

AZURE_DB_HOST = os.environ.get("AZURE_DB_HOST")
AZURE_DB_NAME = os.environ.get("AZURE_DB_NAME")
AZURE_DB_USER = os.environ.get("AZURE_DB_USER")
AZURE_DB_PASSWORD = os.environ.get("AZURE_DB_PASSWORD")


def get_aws_clients():
    session = boto3.Session(
        aws_access_key_id=AWS_ACCESS_KEY,
        aws_secret_access_key=AWS_SECRET_KEY,
        region_name=AWS_REGION
    )
    return session.client('sqs'), session.client('s3')


def decompress_zst(compressed_data: bytes) -> bytes:
    """zstd 圧縮データを解凍してバイナリ (bytes) で返す"""
    dctx = zstd.ZstdDecompressor()
    # Protobufのバイナリを取り出すため bytes のまま返却します
    return dctx.decompress(compressed_data)


def parse_protobuf_to_json(pb_bytes: bytes) -> str:
    """Protobuf バイナリを解析し、JSON 文字列に変換する"""
    # 1. 定義済みの Protobuf メッセージオブジェクトを生成
    message = trip_data_pb2.TripMessage()
    
    # 2. バイナリデータをパース
    message.ParseFromString(pb_bytes)
    
    # 3. Protobuf メッセージを JSON 文字列に変換
    # returning_unknown_fields=True 等のオプション指定も可能
    json_str = MessageToJson(
        message,
        preserving_proto_field_name=True, # フィールド名を proto 定義そのまま（スネークケース等）で維持
        including_default_value_fields=True # デフォルト値（0や空文字列）も省略せず出力
    )
    return json_str


def insert_to_azure_db(sqs_data: dict, s3_json_str: str):
    """SQSデータとS3から取得・変換したJSONデータを Azure DB へ登録"""
    conn = psycopg2.connect(
        host=AZURE_DB_HOST,
        dbname=AZURE_DB_NAME,
        user=AZURE_DB_USER,
        password=AZURE_DB_PASSWORD,
        sslmode='require'
    )
    try:
        with conn.cursor() as cursor:
            query = """
                INSERT INTO trip_records (trip_id, sqs_payload, s3_json_payload)
                VALUES (%s, %s, %s);
            """
            trip_id = sqs_data.get("TRIPID")
            cursor.execute(query, (
                trip_id,
                json.dumps(sqs_data),
                s3_json_str
            ))
        conn.commit()
        logging.info(f"DB登録完了: TRIPID = {trip_id}")
    finally:
        conn.close()


@app.timer_trigger(schedule="0 */1 * * * *", arg_name="myTimer", run_on_startup=False)
def sqs_s3_processor(myTimer: func.TimerRequest) -> None:
    logging.info("SQS ポーリング処理を開始します。")

    sqs, s3 = get_aws_clients()

    response = sqs.receive_message(
        QueueUrl=SQS_QUEUE_URL,
        MaxNumberOfMessages=10,
        WaitTimeSeconds=5
    )

    messages = response.get('Messages', [])
    if not messages:
        logging.info("対象の SQS メッセージはありません。")
        return

    for message in messages:
        receipt_handle = message['ReceiptHandle']
        try:
            sqs_body = json.loads(message['Body'])
            trip_id = sqs_body.get("TRIPID")

            if not trip_id:
                logging.error("SQSメッセージ内に TRIPID が存在しません。")
                continue

            # 1. S3から zst ファイルを取得
            s3_key = f"{trip_id}.zst"
            s3_obj = s3.get_object(Bucket=S3_BUCKET_NAME, Key=s3_key)
            compressed_bytes = s3_obj['Body'].read()

            # 2. zst ファイルの解凍 (Protobuf バイナリを取得)
            pb_bytes = decompress_zst(compressed_bytes)

            # 3. Protobuf 形式から JSON 形式へ変換
            s3_json_str = parse_protobuf_to_json(pb_bytes)

            # 4. DB への登録処理
            insert_to_azure_db(sqs_body, s3_json_str)

            # 5. メッセージの削除
            sqs.delete_message(
                QueueUrl=SQS_QUEUE_URL,
                ReceiptHandle=receipt_handle
            )
            logging.info(f"SQSメッセージ削除完了: TRIPID = {trip_id}")

        except Exception as e:
            logging.error(f"メッセージ処理中にエラーが発生しました: {e}", exc_info=True)
