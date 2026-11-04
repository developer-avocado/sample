import csv
from pathlib import Path
from typing import List


def compare_csv_excluding_columns(
    actual_csv_path: str,
    expected_csv_path: str,
    exclude_columns: List[str],
    encoding: str = "utf-8-sig",
) -> bool:
    """指定したカラムを除外して2つのCSVファイルを比較する。"""
    actual_path = Path(actual_csv_path)
    expected_path = Path(expected_csv_path)

    # 1. ファイルの存在確認
    if not actual_path.is_file():
        raise FileNotFoundError(f"ファイルが存在しません: {actual_csv_path}")
    if not expected_path.is_file():
        raise FileNotFoundError(
            f"ファイルが存在しません: {expected_csv_path}"
        )

    exclude_set = set(exclude_columns)

    # 2. 実際のCSVを読み込み、除外カラムを除去
    with actual_path.open("r", encoding=encoding, newline="") as f_act:
        actual_rows = [
            {k: v for k, v in row.items() if k not in exclude_set}
            for row in csv.DictReader(f_act)
        ]

    # 3. 期待値のCSVを読み込み、除外カラムを除去
    with expected_path.open("r", encoding=encoding, newline="") as f_exp:
        expected_rows = [
            {k: v for k, v in row.items() if k not in exclude_set}
            for row in csv.DictReader(f_exp)
        ]

    # 4. データ行数の検証
    if len(actual_rows) != len(expected_rows):
        raise AssertionError(
            f"行数が一致しません。実際: {len(actual_rows)}行, 期待値: {len(expected_rows)}行"
        )

    # 5. 1行ずつ突き合わせ検証
    for idx, (act_row, exp_row) in enumerate(
        zip(actual_rows, expected_rows), start=1
    ):
        if act_row != exp_row:
            diff_keys = set(act_row) | set(exp_row)
            diff_details = {
                k: {"actual": act_row.get(k), "expected": exp_row.get(k)}
                for k in diff_keys
                if act_row.get(k) != exp_row.get(k)
            }
            raise AssertionError(
                f"CSVの {idx} 行目で不一致が発生しました。\n差分詳細: {diff_details}"
            )

    return True