from pathlib import Path
from typing import List


def get_context_lines_around_pattern(
    file_path: str, search_word: str, context_lines: int = 10
) -> List[str]:
    """ファイル内を検索し、最初に見つかった文字列の前後指定行をリストで返却する。"""
    path = Path(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"ファイルが存在しません: {file_path}")

    # ファイルを行単位で読み込み
    lines = path.read_text(encoding="utf-8").splitlines()

    # 該当行のインデックスを探索
    target_idx = next(
        (i for i, line in enumerate(lines) if search_word in line), None
    )
    if target_idx is None:
        raise ValueError(
            f"ファイル内に指定文字列が見つかりませんでした: '{search_word}'"
        )

    # 前後N行の範囲を安全に計算してスライス
    start_idx = max(0, target_idx - context_lines)
    end_idx = min(len(lines), target_idx + context_lines + 1)

    return lines[start_idx:end_idx]