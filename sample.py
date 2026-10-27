import pandas as pd


def verify_only_target_columns_updated(
    before_csv: str,
    after_csv: str,
    target_cols: list[str],
    require_all_rows_updated: bool = True,
) -> bool:
    """指定した列のみが更新され、その他の列が完全一致しているかを検証する。

    Parameters:
    - before_csv (str): 変更前のCSVファイルパス
    - after_csv (str): 変更後のCSVファイルパス
    - target_cols (list[str]): 更新を許容・期待する列名のリスト（例: ["作成日時", "更新日時"]）
    - require_all_rows_updated (bool): 全行で値が変化していることまで求める場合は True

    Returns:
    - bool: 検証に合格した場合は True、不一致や意図しない差分がある場合は False
    """
    df1 = pd.read_csv(before_csv)
    df2 = pd.read_csv(after_csv)

    # 1. 行数・列数・列名の完全一致チェック
    if df1.shape != df2.shape or list(df1.columns) != list(df2.columns):
        return False

    # 2. 指定した対象列がそもそもCSV内に存在するかチェック
    if not set(target_cols).issubset(set(df1.columns)):
        return False

    # 3. 差分が存在する列名を抽出
    diff_cols = [col for col in df1.columns if (df1[col] != df2[col]).any()]

    # 4. 差分列が target_cols と完全に一致しているか判定（setで順不同比較）
    if set(diff_cols) != set(target_cols):
        return False

    # 5. 全行で値が変化しているかの検証（オプション）
    if require_all_rows_updated:
        is_all_updated = all(
            (df1[col] != df2[col]).all() for col in target_cols
        )
        if not is_all_updated:
            return False

    return True


# ==========================================
# 呼び出し例
# ==========================================
if __name__ == "__main__":
    targets = ["作成日時", "更新日時"]

    is_valid = verify_only_target_columns_updated(
        before_csv="before.csv",
        after_csv="after.csv",
        target_cols=targets,
        require_all_rows_updated=True,
    )

    print(f"検証結果: {is_valid}")