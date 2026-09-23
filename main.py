"""服务入口：``uv run fastapi run main.py`` 或 ``uv run python main.py``。"""

from metadata_catalog.api import create_app

app = create_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
