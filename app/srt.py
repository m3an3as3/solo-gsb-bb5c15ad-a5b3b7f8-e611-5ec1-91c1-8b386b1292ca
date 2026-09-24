"""SRT 解析、锚点校验与分段线性时间校准。"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 一条时间轴行：HH:MM:SS,mmm --> HH:MM:SS,mmm（毫秒位允许 1-3 位，逗号或点）
TIMING_RE = re.compile(
    r"(\d{1,2}):(\d{1,2}):(\d{1,2})[,.](\d{1,3})"
    r"\s*-->\s*"
    r"(\d{1,2}):(\d{1,2}):(\d{1,2})[,.](\d{1,3})"
)

_FULL_TIME_RE = re.compile(r"^(\d{1,2}):(\d{1,2}):(\d{1,2})[,.](\d{1,3})$")
_SHORT_TIME_RE = re.compile(r"^(\d{1,2}):(\d{1,2})[,.](\d{1,3})$")


class SrtError(ValueError):
    """SRT 内容解析失败。"""


class AnchorError(ValueError):
    """锚点不合法。"""


@dataclass
class Cue:
    index: str          # 保留原文件中的序号
    start: int          # 起始毫秒
    end: int            # 结束毫秒
    text: str           # 正文（可能含多行）


def parse_time(value: str) -> int:
    """把 HH:MM:SS,mmm / HH:MM:SS.mmm / MM:SS,mmm 解析成毫秒。"""
    s = value.strip()
    m = _FULL_TIME_RE.fullmatch(s)
    if m:
        h, minute, sec, ms = m.groups()
    else:
        m = _SHORT_TIME_RE.fullmatch(s)
        if not m:
            raise AnchorError(f"无法解析时间“{value}”，格式应为 HH:MM:SS,mmm")
        h, minute, sec, ms = "0", *m.groups()
    return (
        int(h) * 3_600_000
        + int(minute) * 60_000
        + int(sec) * 1_000
        + int(ms.ljust(3, "0"))
    )


def format_time(ms: int) -> str:
    """毫秒格式化为 SRT 时间；负数带前导负号（仅用于预览提示）。"""
    sign = "-" if ms < 0 else ""
    ms = abs(ms)
    h, ms = divmod(ms, 3_600_000)
    minute, ms = divmod(ms, 60_000)
    sec, ms = divmod(ms, 1_000)
    return f"{sign}{h:02d}:{minute:02d}:{sec:02d},{ms:03d}"


def parse_srt(content: str) -> list[Cue]:
    """解析 SRT 文本，按文件顺序返回字幕条目。"""
    if content is None or not content.strip():
        raise SrtError("SRT 内容为空")

    content = content.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n[ \t]*\n", content.strip("\n"))

    cues: list[Cue] = []
    for pos, block in enumerate(blocks, start=1):
        lines = block.split("\n")
        timing_i = next(
            (i for i, line in enumerate(lines) if TIMING_RE.search(line)), None
        )
        if timing_i is None:
            raise SrtError(f"第 {pos} 段字幕缺少时间轴行，无法解析")

        m = TIMING_RE.search(lines[timing_i])
        assert m is not None

        if timing_i > 0 and re.fullmatch(r"\d+", lines[0].strip()):
            index = lines[0].strip()
        else:
            index = str(pos)

        start = (
            int(m.group(1)) * 3_600_000
            + int(m.group(2)) * 60_000
            + int(m.group(3)) * 1_000
            + int(m.group(4).ljust(3, "0"))
        )
        end = (
            int(m.group(5)) * 3_600_000
            + int(m.group(6)) * 60_000
            + int(m.group(7)) * 1_000
            + int(m.group(8).ljust(3, "0"))
        )
        text = "\n".join(lines[timing_i + 1 :]).strip()

        cues.append(Cue(index=index, start=start, end=end, text=text))

    if not cues:
        raise SrtError("未解析到任何字幕条目")
    return cues


def prepare_anchors(raw: list[dict]) -> list[tuple[int, int]]:
    """校验锚点并按“原时刻”升序返回 (original_ms, mapped_ms) 列表。"""
    if len(raw) < 2:
        raise AnchorError("至少需要设置 2 个锚点")

    anchors: list[tuple[int, int]] = []
    seen: set[int] = set()
    for i, item in enumerate(raw, start=1):
        try:
            original = parse_time(item.get("original", ""))
            mapped = parse_time(item.get("mapped", ""))
        except AttributeError:
            raise AnchorError(f"第 {i} 个锚点格式不正确")
        if original in seen:
            raise AnchorError(
                f"原时刻重复：{format_time(original)} 出现了不止一次"
            )
        seen.add(original)
        anchors.append((original, mapped))

    anchors.sort(key=lambda pair: pair[0])

    for (_, m1), (_, m2) in zip(anchors, anchors[1:]):
        if m2 <= m1:
            raise AnchorError(
                f"映射时刻必须随原时刻严格递增：{format_time(m1)} 之后不能是 "
                f"{format_time(m2)}"
            )
    return anchors


def _map_time(t: int, anchors: list[tuple[int, int]]) -> int:
    """分段线性映射；两端用最近一段的斜率外推，结果四舍五入到毫秒。"""
    first_o, first_m = anchors[0]
    last_o, last_m = anchors[-1]

    if t <= first_o:
        (o1, m1), (o2, m2) = anchors[0], anchors[1]
    elif t >= last_o:
        (o1, m1), (o2, m2) = anchors[-2], anchors[-1]
    else:
        for (o1, m1), (o2, m2) in zip(anchors, anchors[1:]):
            if o1 <= t <= o2:
                break
        else:  # pragma: no cover - 理论上不可达
            raise AssertionError("插值区间查找失败")

    slope = (m2 - m1) / (o2 - o1)
    mapped = m1 + (t - o1) * slope
    return int(round(mapped))


# 问题类型标签
ISSUE_NEGATIVE = "negative"
ISSUE_INVERTED = "inverted"
ISSUE_OVERLAP = "overlap"
ISSUE_LABELS = {
    ISSUE_NEGATIVE: "负时间",
    ISSUE_INVERTED: "起止倒置",
    ISSUE_OVERLAP: "与下一条重叠",
}


def calibrate(
    cues: list[Cue], anchors: list[tuple[int, int]]
) -> list[dict]:
    """对每条字幕做时间换算，并标注负时间 / 起止倒置 / 与下一条重叠。"""
    results: list[dict] = []
    for cue in cues:
        new_start = _map_time(cue.start, anchors)
        new_end = _map_time(cue.end, anchors)
        issues: list[str] = []
        if new_start < 0 or new_end < 0:
            issues.append(ISSUE_NEGATIVE)
        if new_start > new_end:
            issues.append(ISSUE_INVERTED)
        results.append(
            {
                "index": cue.index,
                "orig_start": cue.start,
                "orig_end": cue.end,
                "new_start": new_start,
                "new_end": new_end,
                "text": cue.text,
                "issues": issues,
            }
        )

    for cur, nxt in zip(results, results[1:]):
        if cur["new_end"] > nxt["new_start"]:
            cur["issues"].append(ISSUE_OVERLAP)

    return results


def build_srt(results: list[dict]) -> str:
    """用校准后的时间重建 SRT，保留原序号与正文。"""
    blocks = []
    for item in results:
        timing = (
            f"{format_time(item['new_start'])} --> {format_time(item['new_end'])}"
        )
        blocks.append(f"{item['index']}\n{timing}\n{item['text']}")
    return "\n\n".join(blocks) + "\n"
