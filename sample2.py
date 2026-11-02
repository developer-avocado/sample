*** Settings ***
Library    OperatingSystem
Library    BuiltIn

*** Keywords ***
ファイルから検索文字列の前後行を取得する
    [Arguments]    ${file_path}    ${search_word}    ${context_lines}=${10}

    # 1. ファイルを読み込み、行リストへ変換
    File Should Exist    ${file_path}    msg=指定されたファイルが存在しません: ${file_path}
    ${content} =    Get File    ${file_path}    encoding=UTF-8
    ${lines} =      Evaluate    $content.splitlines()

    # 2. 検索文字列が含まれる最初の行インデックス（0始まり）を探索
    ${match_index} =    Evaluate
    ...    next((i for i, line in enumerate($lines) if $search_word in line), None)
    Should Not Be Equal    ${match_index}    ${None}    msg=ファイル内に [${search_word}] が見つかりませんでした

    # 3. 前後N行のスライス範囲（開始・終了）を安全に計算
    ${total_lines} =    Evaluate    len($lines)
    ${start_idx} =      Evaluate    max(0, $match_index - $context_lines)
    ${end_idx} =        Evaluate    min($total_lines, $match_index + $context_lines + 1)

    # 4. 前後行を切り出して返却
    ${result_lines} =   Evaluate    $lines[$start_idx:$end_idx]
    
    # ログ確認用に出力（改行で結合）
    ${preview} =        Evaluate    "\\n".join($result_lines)
    Log    取得したコンテキスト範囲（${start_idx + 1}行目〜${end_idx}行目）:\n${preview}

    RETURN    ${result_lines}