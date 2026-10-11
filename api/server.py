from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import os

app = FastAPI(title="NextGen API")

@app.get("/map", response_class=HTMLResponse)
async def get_map():
    # FastAPI читает наш index.html и отдает его по ссылке
    file_path = os.path.join(os.path.dirname(__file__), "..", "web", "index.html")
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return "<h1>Файл карты не найден</h1>"