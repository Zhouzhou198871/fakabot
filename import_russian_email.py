
#!/usr/bin/env python3
"""Import Russian mailbox credentials: one credential per line; never prints credentials."""
import argparse
import sys
from pathlib import Path
from products.local_inventory import import_cards, stock_count, INVENTORY_PATH

DEFAULT_INPUT = Path("/root/cloud_emails.txt")

def main():
    parser = argparse.ArgumentParser(description="导入俄罗斯邮箱本地卡密（一行一个）")
    parser.add_argument("--file", default=str(DEFAULT_INPUT), help="卡密文本文件路径")
    args = parser.parse_args()
    source = Path(args.file)
    if not source.is_file():
        print(f"找不到卡密文件：{source}")
        print("请把一行一个卡密的 TXT 上传到该路径后重试。")
        return 2
    try:
        with source.open("r", encoding="utf-8-sig") as f:
            result = import_cards(f.readlines())
        source.chmod(0o600)
    except UnicodeDecodeError:
        print("文件编码不是 UTF-8；请另存为 UTF-8 后重试。")
        return 2
    print("导入完成（不会输出卡密内容）")
    print(f"本次有效行数：{result['input']}")
    print(f"新增库存：{result['added']}")
    print(f"重复跳过：{result['duplicates']}")
    print(f"当前可售库存：{result['available']}")
    print(f"库存文件：{INVENTORY_PATH}")
    print("固定售价：¥10.00 / 个")
    print("注意：源文件为邮箱地址清单，不含邮箱密码。")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
