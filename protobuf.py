import csv
import user_pb2

def convert_csv_to_protobuf_bin(csv_file_path, bin_file_path):
    # 1. 親オブジェクト（UserList）の作成
    user_list = user_pb2.UserList()

    # 2. CSVファイルを1行ずつ読み込み
    with open(csv_file_path, mode="r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # 3. repeated フィールド（users）に新しい User オブジェクトを追加
            user = user_list.users.add()
            user.user_id = row["user_id"]
            user.name = row["name"]
            user.age = int(row["age"])  # 数値型へキャスト
            user.department = row["department"]

    # 4. オブジェクト全体をバイナリデータにシリアライズ
    binary_data = user_list.SerializeToString()

    # 5. バイナリファイルとして書き出し
    with open(bin_file_path, mode="wb") as f:
        f.write(binary_data)

    print(f"変換完了: {len(user_list.users)} 件のデータを {bin_file_path} に保存しました。")
    print(f"ファイルサイズ: {len(binary_data)} bytes")


def read_protobuf_bin(bin_file_path):
    # 【確認用】保存したバイナリファイルを読み込んで復元
    user_list = user_pb2.UserList()
    
    with open(bin_file_path, mode="rb") as f:
        binary_data = f.read()
        user_list.ParseFromString(binary_data)

    print("\n--- バイナリファイルから復元したデータ ---")
    for user in user_list.users:
        print(f"[{user.user_id}] {user.name} ({user.age}歳) - {user.department}")


if __name__ == "__main__":
    convert_csv_to_protobuf_bin("users.csv", "users.bin")
    read_protobuf_bin("users.bin")
