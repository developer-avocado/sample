import json
from pathlib import Path
from urllib.parse import parse_qs
import uuid
import azure.functions as func

app = func.FunctionApp()

# 同一ディレクトリのファイルからメッセージリストを事前読み込み
BASE_DIR = Path(__file__).parent
raw_json = (BASE_DIR / "sqs_messages.json").read_text(encoding="utf-8")
PREPARED_MESSAGE_LIST = json.loads(raw_json)

@app.route(
    route="sqsmock/{queue_name}",
    methods=["POST"],
    auth_level=func.AuthLevel.ANONYMOUS,
)
def sqs_mock(req: func.HttpRequest) -> func.HttpResponse:
  params = {k: v[0] for k, v in parse_qs(req.get_body().decode()).items()}
  action = params.get("Action")

  # 1. 受信 (sqs.receive_message)
  if action == "ReceiveMessage":
    # リストの各要素を <Message> タグに展開
    messages_xml = "".join([
        f"""
        <Message>
            <MessageId>{uuid.uuid4()}</MessageId>
            <ReceiptHandle>handle_{idx}</ReceiptHandle>
            <Body>{msg}</Body>
        </Message>"""
        for idx, msg in enumerate(PREPARED_MESSAGE_LIST)
    ])

    xml = f"""<ReceiveMessageResponse>
    <ReceiveMessageResult>
        {messages_xml}
    </ReceiveMessageResult>
</ReceiveMessageResponse>"""
    return func.HttpResponse(body=xml, status_code=200, mimetype="text/xml")

  # 2. 送信・削除など
  xml = f"<{action}Response><ResponseMetadata><RequestId>{uuid.uuid4()}</RequestId></ResponseMetadata></{action}Response>"
  return func.HttpResponse(body=xml, status_code=200, mimetype="text/xml")