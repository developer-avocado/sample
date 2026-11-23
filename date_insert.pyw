import datetime
import subprocess

# 【パターン1】 コンパクトな形式：[MYC 261123_2004] や [MYC 261123 20:04]
formatted_text = datetime.datetime.now().strftime("[MYC_%y%m%d_%H%M]")

# 【パターン2】 標準的なISO形式：[MYC 2026-11-23 20:04]
# formatted_text = datetime.datetime.now().strftime("[MYC %Y-%m-%d %H:%M]")

# 【パターン3】 スラッシュ形式：[MYC 11/23 20:04]
# formatted_text = datetime.datetime.now().strftime("[MYC %m/%d %H:%M]")

# Windows標準のclipコマンドでクリップボードへ送る
process = subprocess.Popen(
    "clip", stdin=subprocess.PIPE, shell=True, text=True, encoding="cp932"
)
process.communicate(input=formatted_text)