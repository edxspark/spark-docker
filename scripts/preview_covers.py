"""生成封面预览图：改完版式后用脚本几秒出一组样张，一眼看效果。

用法：
    ./backend/.venv/bin/python scripts/preview_covers.py [输出目录]

只调用封面生成逻辑，不读写数据库、不影响线上任务。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.services import cover  # noqa: E402

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/cover-preview")
OUT.mkdir(parents=True, exist_ok=True)
CASES = [
    ("AI 原生公司搭建指南：YC 合伙人亲述从 0 到 1", ["AI创业", "公司运营", "创业公司", "人工智能", "涨知识"]),
    ("如何用 AI 把工作效率提升 10 倍", ["效率工具", "AI", "职场"]),
    ("短标题", ["测试"]),
    ("这是一个特别长的标题用来测试自动缩小与折行的边界情况", ["长标题", "测试"]),
]
tiles = []
for index, (title, tags) in enumerate(CASES):
    for label, size in (("竖", cover.PORTRAIT_COVER_SIZE), ("横", cover.LANDSCAPE_COVER_SIZE)):
        path = OUT / f"{index}_{label}.jpg"
        cover.generate_cover(
            video=None, title=title, tags=tags, out_path=path,
            style=cover.CoverStyle(width=size[0], height=size[1], brand="AI 译制", max_tags=4),
        )
        tiles.append(path)
        print("已生成", path)

# 再拼一张对比图，便于一次看完全部样张
try:
    from PIL import Image

    images = [Image.open(p).convert("RGB") for p in tiles]
    thumb_w = 360
    thumb_h = int(images[0].height * thumb_w / images[0].width)
    cols = 4
    rows = (len(images) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * thumb_w, rows * thumb_h), (16, 18, 24))
    for i, image in enumerate(images):
        sheet.paste(image.resize((thumb_w, thumb_h), Image.LANCZOS),
                    ((i % cols) * thumb_w, (i // cols) * thumb_h))
    sheet_path = OUT / "contact_sheet.jpg"
    sheet.save(sheet_path, quality=90)
    print("对比图", sheet_path)
except Exception as exc:  # noqa: BLE001
    print("对比图生成失败（不影响单张样张）：", exc)
