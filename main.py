"""服务入口。

用法::

    uv run fastapi run main.py --host 127.0.0.1 --port 8000
    uv run python main.py

数据默认落在当前目录的 ``./data`` 仓库，服务重启后数据继续可用；
多个服务进程指向同一仓库目录时共享同一份数据并由文件锁互斥。
可用环境变量 ``METADATA_CATALOG_REPO`` 指定其他目录。
"""

from __future__ import annotations

import os

from metadata_catalog.api import create_app

app = create_app(repo_dir=os.environ.get("METADATA_CATALOG_REPO", "data"))

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
