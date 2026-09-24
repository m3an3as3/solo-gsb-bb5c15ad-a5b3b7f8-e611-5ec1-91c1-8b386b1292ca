"""SRT 字幕时间校准工具 — FastAPI 后端。"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import srt

app = FastAPI(title="SRT 字幕时间校准", version="1.0.0")


class AnchorIn(BaseModel):
    original: str
    mapped: str


class CalibrateIn(BaseModel):
    content: str
    anchors: list[AnchorIn]


def _process(payload: CalibrateIn) -> tuple[list[dict] | None, JSONResponse | None]:
    """解析 + 校验 + 校准；失败时返回错误 JSONResponse。"""
    anchors_raw = [a.model_dump() for a in payload.anchors]
    try:
        anchors = srt.prepare_anchors(anchors_raw)
        cues = srt.parse_srt(payload.content)
    except (srt.AnchorError, srt.SrtError) as exc:
        kind = "anchor" if isinstance(exc, srt.AnchorError) else "srt"
        return None, JSONResponse(
            status_code=400,
            content={"ok": False, "error_kind": kind, "error": str(exc)},
        )

    results = srt.calibrate(cues, anchors)
    for item in results:
        item["orig_start_text"] = srt.format_time(item["orig_start"])
        item["orig_end_text"] = srt.format_time(item["orig_end"])
        item["new_start_text"] = srt.format_time(item["new_start"])
        item["new_end_text"] = srt.format_time(item["new_end"])
        item["issue_labels"] = [
            srt.ISSUE_LABELS[code] for code in item["issues"]
        ]
    return results, None


@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


@app.post("/api/preview")
def preview(payload: CalibrateIn) -> JSONResponse:
    results, err = _process(payload)
    if err is not None:
        return err
    has_issues = any(item["issues"] for item in results)  # type: ignore[union-attr]
    return JSONResponse(
        content={"ok": True, "results": results, "has_issues": has_issues}
    )


@app.post("/api/export")
def export(payload: CalibrateIn) -> Response | JSONResponse:
    results, err = _process(payload)
    if err is not None:
        return err
    assert results is not None

    bad = [item for item in results if item["issues"]]
    if bad:
        labels = sorted({lbl for item in bad for lbl in item["issue_labels"]})
        return JSONResponse(
            status_code=422,
            content={
                "ok": False,
                "error_kind": "issues",
                "error": (
                    f"存在 {len(bad)} 条问题字幕（{'、'.join(labels)}），"
                    "请先修正锚点再导出"
                ),
            },
        )

    data = srt.build_srt(results).encode("utf-8")
    return Response(
        content=data,
        media_type="application/x-subrip; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="calibrated.srt"'
        },
    )


app.mount("/", StaticFiles(directory="app/static", html=True), name="static")
