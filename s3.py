from pathlib import Path
import azure.functions as func

app = func.FunctionApp()
BASE_DIR = Path(__file__).parent


@app.route(
    route="s3mock/{bucket}/{*key}",
    methods=["GET", "HEAD"],
    auth_level=func.AuthLevel.ANONYMOUS,
)
def s3_mock(req: func.HttpRequest) -> func.HttpResponse:
  key = req.route_params.get("key")  # 例: "folder/data1.zst"
  filename = Path(key).name  # ファイル名部分（"data1.zst"）だけを取り出す

  # 同一ディレクトリから該当する .zst ファイルをバイナリとして読み込む
  data = (BASE_DIR / filename).read_bytes()

  return func.HttpResponse(
      body=data,
      status_code=200,
      headers={"Content-Length": str(len(data))},
      mimetype="application/zstd",
  )