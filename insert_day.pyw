import datetime
import keyboard


def insert_now():
    now_str = datetime.datetime.now().strftime("%Y%m%d_%H%M")
    keyboard.write(now_str)

def insert_comment():
    comment_str = datetime.datetime.now().strftime("[MYC_%Y%m%d_%H%M]")
    keyboard.write(comment_str)

# パターンA: 3キー重ね（ほぼ確実に重複なし）
keyboard.add_hotkey("ctrl+shift+alt+n", insert_now)
keyboard.add_hotkey("ctrl+shift+alt+c", insert_comment)

# パターンB: Ctrl + Win（仮想デスクトップと被らない文字）
# keyboard.add_hotkey("ctrl+windows+m", insert_now)

keyboard.wait()