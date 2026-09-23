"""本地元数据目录与血缘服务入口。

运行:
    uv run fastapi dev main.py
或:
    uv run uvicorn main:app --reload
默认数据文件为进程工作目录下的 catalog_data.json。
"""

from metadata_catalog.api import create_app

app = create_app()
