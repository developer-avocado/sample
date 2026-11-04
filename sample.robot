*** Settings ***
Library    table_diff.py
Library    Collections

*** Test Cases ***
CSV比較テスト
    ${exclude_cols} =    Create List    updated_at    created_at    log_id

    Compare Csv Excluding Columns
    ...    actual_csv_path=results/actual_data.csv
    ...    expected_csv_path=testdata/expected_data.csv
    ...    exclude_columns=${exclude_cols}