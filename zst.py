import zstandard as zstd
from pathlib import Path

def decompress_zst(input_path: str, output_path: str = None) -> Path:
    """
    .zst ファイルを解凍して出力する関数
    :param input_path: 解凍対象の .zst ファイルパス
    :param output_path: 出力先ファイルパス（省略時は .zst を除いたファイル名）
    """
    src_file = Path(input_path)
    if output_path is None:
        # 例: data.json.zst -> data.json
        dst_file = src_file.with_suffix("")
    else:
        dst_file = Path(output_path)

    dctx = zstd.ZstdDecompressor()

    with open(src_file, "rb") as ifh, open(dst_file, "wb") as ofh:
        # copy_stream を使うことでバッファを効率よくストリーム解凍
        dctx.copy_stream(ifh, ofh)

    print(f"解凍完了: {dst_file}")
    return dst_file

# 使用例
if __name__ == "__main__":
    decompress_zst("sample.json.zst")