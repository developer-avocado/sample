*** Settings ***
Library    RequestsLibrary

*** Variables ***
${APP_NAME}      my-sample-func-app
${FUNC_NAME}     my_timer_func
${MASTER_KEY}    ここに_masterキーの値を設定

*** Test Cases ***
Trigger Cloud Timer Function Immediately
    # 1. クラウド用の管理エンドポイントURLを組み立てる
    ${url}=        Set Variable    https://${APP_NAME}.azurewebsites.net/admin/functions/${FUNC_NAME}

    # 2. x-functions-key に _master キーを指定し、Content-Type を json にする
    &{headers}=    Create Dictionary
    ...            Content-Type=application/json
    ...            x-functions-key=${MASTER_KEY}

    # 3. 空の input を持つJSONボディを作成する
    &{body}=       Create Dictionary    input=${EMPTY}

    # 4. POSTリクエストを送信（成功時は 202 Accepted が返る）
    ${response}=   POST    ${url}    json=${body}    headers=${headers}    expected_status=202