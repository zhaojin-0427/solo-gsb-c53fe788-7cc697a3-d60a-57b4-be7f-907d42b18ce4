"""多麦克风敲击定位 Web 台 —— FastAPI 后端。"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .locator import LocatorError, solve

app = FastAPI(title="多麦克风敲击定位台", version="1.0")


@app.post("/api/locate")
async def locate(request: Request):
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(status_code=200,
                            content={"status": "INVALID_INPUT",
                                     "message": "请求体不是合法的 JSON"})
    if not isinstance(payload, dict):
        return JSONResponse(status_code=200,
                            content={"status": "INVALID_INPUT",
                                     "message": "请求体必须是 JSON 对象"})
    # CPU 密集求解放到线程池，避免阻塞事件循环
    try:
        result = await run_in_threadpool(solve, payload)
    except LocatorError as exc:
        return JSONResponse(status_code=200,
                            content={"status": exc.code, "message": exc.message})
    except (TypeError, ValueError, KeyError) as exc:
        return JSONResponse(status_code=200,
                            content={"status": "INVALID_INPUT",
                                     "message": f"输入格式无效：{exc}"})
    return JSONResponse(result)


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/")
async def index():
    return FileResponse(__file__.rsplit("/", 1)[0] + "/static/index.html")


app.mount("/static", StaticFiles(directory=__file__.rsplit("/", 1)[0] + "/static"),
          name="static")
