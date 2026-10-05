"""FastAPI 入口：/api/locate 求解，根路径提供房间定位图页面。"""

from __future__ import annotations

import os
import time

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from .locate import LocateError, solve
from .models import LocateRequest

app = FastAPI(title="多麦克风敲击定位台", version="1.0.0")

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                          "..", "frontend")
STATIC_DIR = os.path.abspath(STATIC_DIR)


@app.exception_handler(LocateError)
async def locate_error_handler(request: Request, exc: LocateError):
    return JSONResponse(
        {"success": False, "status": exc.code, "message": exc.message},
        status_code=400,
    )


@app.exception_handler(ValidationError)
async def validation_error_handler(request: Request, exc: ValidationError):
    return JSONResponse(
        {"success": False, "status": "INVALID_REQUEST",
         "message": "请求参数不合法",
         "details": exc.errors()},
        status_code=422,
    )


@app.post("/api/locate")
async def api_locate(payload: dict):
    # 先走 pydantic 结构校验（422 由 FastAPI 默认处理器返回），再走语义校验
    req = LocateRequest.model_validate(payload)
    req_dict = req.model_dump()
    # 5 秒交互预算，留约 0.5 秒给序列化/网络
    result = solve(req_dict, deadline=time.perf_counter() + 4.5)
    status = result.get("status", "OK")
    result["success"] = status in ("OK", "AMBIGUOUS")
    return result


@app.get("/api/health")
async def health():
    return {"ok": True}


@app.get("/")
async def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
