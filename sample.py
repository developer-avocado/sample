import pandas as pd

# 1. 2つの対象列を指定
TARGET_COLS = ["作成日時", "更新日時"]

# --- （CSVの読み込みなどは同様） ---
df1 = pd.read_csv("before.csv")
df2 = pd.read_csv("after.csv")

# 2. 差分のある列を特定
diff_cols = [col for col in df1.columns if (df1[col] != df2[col]).any()]

# 3. 判定（set を使って順不同で「この2列のみ」かを検証）
if set(diff_cols) == set(TARGET_COLS):
    # 指定した各列で、全行更新されているか確認
    all_updated = all(
        (df1[col] != df2[col]).all() for col in TARGET_COLS
    )

    if all_updated:
        print("【判定：OK】")
        print(f"・{TARGET_COLS} の2列のみが全行で更新されています。")
        print("・それ以外の列は完全に一致しています。")
    else:
        print("【判定：一部更新】")
        print("・余計な列の変更はありませんが、値が変わっていない行があります。")

elif not diff_cols:
    print("【判定：NG（差分なし）】")
    print("・どの列も更新されていません。")

else:
    print("【判定：NG（意図しない差分、または不足）】")
    print(f"・期待していた更新列: {TARGET_COLS}")
    print(f"・実際に差分が出た列: {diff_cols}")