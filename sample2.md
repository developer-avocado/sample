*** Settings ***
Library    DateTime
Library    BuiltIn
Library    Process

*** Keywords ***
次の3分境界に合わせて関数を有効化する
    [Arguments]    ${app_name}    ${rg_name}    ${func_name}

    # 1. 現在の「分」と「秒」を取得
    ${now} =    Get Current Date    result_format=%M:%S
    ${min}    ${sec} =    Split String    ${now}    :
    ${min_int} =    Convert To Integer    ${min}
    ${sec_int} =    Convert To Integer    ${sec}

    # 2. 次の3の倍数分までの残り秒数を算出
    # (例: 50分の場合、次の51分まで 1分 = 60秒 - 経過秒)
    ${remainder} =    Evaluate    ${min_int} % 3
    ${wait_minutes} =    Evaluate    (3 - ${remainder}) % 3
    ${total_wait_sec} =    Evaluate    (${wait_minutes} * 60) - ${sec_int}
    # ちょうど0秒付近の場合は次の周期（3分後）を待つ
    ${total_wait_sec} =    Set Variable If    ${total_wait_sec} <= 5    ${total_wait_sec + 180}    ${total_wait_sec}

    # 3. アプリ起動のオーバーヘッド（約5秒）を見越して手前まで待機
    ${sleep_sec} =    Evaluate    ${total_wait_sec} - 5
    Log    次の3分境界まで ${sleep_sec} 秒待機します
    Sleep    ${sleep_sec}s