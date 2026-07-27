#!/usr/bin/env python3
"""Compress delivery images to a target size ratio and read-only check TXT code."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from dataclasses import asdict, dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps, ImageSequence, UnidentifiedImageError


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
FULL_WIDTH_CODE_PUNCTUATION = set("，。：；“”‘’（）【】")


@dataclass
class ImageReport:
    source: str
    output: str
    original_bytes: int
    output_bytes: int
    actual_percent: float
    status: str
    detail: str = ""


@dataclass
class TextReport:
    source: str
    ok: bool
    errors: list[str]
    compact: str


def collect(paths: list[Path]) -> tuple[list[tuple[Path, Path]], list[Path], bool]:
    images: list[tuple[Path, Path]] = []
    texts: list[Path] = []
    folder_input = any(path.is_dir() for path in paths)
    for path in paths:
        path = path.resolve()
        if not path.exists():
            raise FileNotFoundError(f"输入不存在: {path}")
        if path.is_file():
            if path.suffix.lower() in IMAGE_SUFFIXES:
                images.append((path, Path(path.name)))
            elif path.suffix.lower() == ".txt":
                texts.append(path)
            continue
        for item in sorted(path.rglob("*"), key=lambda value: str(value).lower()):
            if not item.is_file():
                continue
            relative = item.relative_to(path)
            if item.suffix.lower() in IMAGE_SUFFIXES:
                images.append((item, relative))
            elif item.suffix.lower() == ".txt":
                texts.append(item)
    return images, texts, folder_input


def default_output(paths: list[Path], folder_input: bool) -> Path:
    first = paths[0].resolve()
    if len(paths) == 1:
        if folder_input:
            return first.parent / f"{first.name}-小压子"
        suffix = ".png" if first.suffix.lower() == ".bmp" else first.suffix
        return first.with_name(f"{first.stem}-小压子{suffix}")
    return Path.cwd() / "小压子-处理结果"


def encode_static(
    image: Image.Image,
    suffix: str,
    quality: int | None = None,
    colors: int | None = None,
    compress_level: int = 9,
    optimize: bool = True,
) -> bytes:
    output = BytesIO()
    prepared = image
    try:
        if suffix in {".jpg", ".jpeg"}:
            prepared = image if image.mode in {"RGB", "L"} else image.convert("RGB")
            prepared.save(output, "JPEG", quality=80 if quality is None else quality, optimize=True, progressive=True)
        elif suffix == ".webp":
            image.save(output, "WEBP", quality=80 if quality is None else quality, method=4, exact=True)
        else:
            if colors:
                method = Image.Quantize.FASTOCTREE if "A" in image.getbands() else Image.Quantize.MEDIANCUT
                prepared = image.quantize(colors=colors, method=method)
            prepared.save(output, "PNG", optimize=optimize, compress_level=compress_level)
    finally:
        if prepared is not image:
            prepared.close()
    return output.getvalue()


def encode_gif(image: Image.Image, colors: int) -> bytes:
    frames: list[Image.Image] = []
    durations: list[int] = []
    disposals: list[int] = []
    for frame in ImageSequence.Iterator(image):
        rgba = frame.convert("RGBA")
        try:
            frames.append(rgba.quantize(colors=colors, method=Image.Quantize.FASTOCTREE))
        finally:
            rgba.close()
        durations.append(int(frame.info.get("duration", image.info.get("duration", 0))))
        disposals.append(int(getattr(frame, "disposal_method", 0)))
    output = BytesIO()
    try:
        frames[0].save(
            output,
            "GIF",
            save_all=len(frames) > 1,
            append_images=frames[1:],
            optimize=True,
            loop=int(image.info.get("loop", 0)),
            duration=durations,
            disposal=disposals,
        )
    finally:
        for frame in frames:
            frame.close()
    return output.getvalue()


class CandidatePicker:
    def __init__(self, target: int, maximum: int) -> None:
        self.target = target
        self.maximum = maximum
        self.best: bytes | None = None
        self.best_score: tuple[int, bool, int] | None = None

    def consider(self, data: bytes) -> int:
        size = len(data)
        if size <= self.maximum:
            score = (abs(size - self.target), size > self.target, size)
            if self.best_score is None or score < self.best_score:
                self.best = data
                self.best_score = score
        return size


def choose_quality(image: Image.Image, suffix: str, target: int, picker: CandidatePicker) -> None:
    low, high = 0, 96
    for _ in range(8):
        if low > high:
            break
        quality = (low + high) // 2
        data = encode_static(image, suffix, quality=quality)
        size = picker.consider(data)
        if size <= target:
            low = quality + 1
        else:
            high = quality - 1
    picker.consider(encode_static(image, suffix, quality=0))
    picker.consider(encode_static(image, suffix, quality=96))


def choose_palette(image: Image.Image, suffix: str, picker: CandidatePicker) -> None:
    colors = (256, 192, 128, 96, 64, 48, 32, 24, 16)
    for count in colors:
        data = encode_gif(image, count) if suffix == ".gif" else encode_static(image, ".png", colors=count)
        picker.consider(data)


def posterize_png(image: Image.Image, bits: int, compress_level: int) -> bytes:
    has_alpha = "A" in image.getbands() or "transparency" in image.info
    rgb = image.convert("RGB")
    prepared: Image.Image | None = None
    alpha: Image.Image | None = None
    if has_alpha:
        prepared = ImageOps.posterize(rgb, bits)
        if "A" in image.getbands():
            alpha = image.getchannel("A")
        else:
            rgba = image.convert("RGBA")
            try:
                alpha = rgba.getchannel("A")
            finally:
                rgba.close()
        prepared.putalpha(alpha)
    else:
        prepared = ImageOps.posterize(rgb, bits)
    try:
        return encode_static(prepared, ".png", compress_level=compress_level, optimize=False)
    finally:
        rgb.close()
        prepared.close()
        if alpha is not None:
            alpha.close()


def choose_png(image: Image.Image, target: int, picker: CandidatePicker) -> None:
    large_image = image.width * image.height >= 16_000_000
    if large_image:
        # For large PNGs, the source itself is the lossless baseline. Re-encoding it
        # several times has a high memory/CPU cost and rarely helps below 85%.
        lossless_levels = (9,) if target >= picker.maximum * 0.85 else ()
    else:
        lossless_levels = (1, 6, 9)
    for level in lossless_levels:
        picker.consider(encode_static(image, ".png", compress_level=level, optimize=False))
    posterized: dict[tuple[int, int], int] = {}

    def add_posterized(bits: int, compress_level: int) -> int:
        key = (bits, compress_level)
        if key not in posterized:
            posterized[key] = picker.consider(posterize_png(image, bits, compress_level))
        return posterized[key]

    def refine_posterized(bits: int) -> None:
        low, high = 1, 9
        while low <= high:
            level = (low + high) // 2
            size = add_posterized(bits, level)
            if size > target:
                low = level + 1
            else:
                high = level - 1
        levels = (1, 6, 9) if large_image else range(max(1, high - 1), min(9, low + 1) + 1)
        for level in levels:
            add_posterized(bits, level)

    posterized_under_target = False
    for bits in (7, 6, 5, 4):
        size = add_posterized(bits, 1)
        if size <= target:
            posterized_under_target = True
            break

    if not posterized_under_target:
        previous_bits: int | None = None
        for bits in (7, 6, 5, 4, 3, 2, 1):
            size = add_posterized(bits, 6)
            if size <= target:
                posterized_under_target = True
                refine_posterized(bits)
                if previous_bits is not None:
                    refine_posterized(previous_bits)
                break
            previous_bits = bits

    if not posterized_under_target and (picker.best is None or len(picker.best) > target):
        previous_colors = 0
        for colors in (256, 128, 64, 32, 16):
            data = encode_static(image, ".png", colors=colors)
            size = picker.consider(data)
            if size <= target:
                if previous_colors:
                    midpoint = (previous_colors + colors) // 2
                    picker.consider(encode_static(image, ".png", colors=midpoint))
                break
            previous_colors = colors


def validate(data: bytes, expected_size: tuple[int, int], expected_frames: int, require_transparency: bool) -> None:
    with Image.open(BytesIO(data)) as check:
        check.load()
        if check.size != expected_size:
            raise ValueError("像素尺寸发生变化")
        if getattr(check, "n_frames", 1) != expected_frames:
            raise ValueError("GIF 动画帧数发生变化")
        if require_transparency and check.format != "GIF":
            if "A" in check.getbands():
                alpha_min = check.getchannel("A").getextrema()[0]
            elif "transparency" in check.info:
                alpha_min = 0
            else:
                alpha_min = 255
            if alpha_min == 255:
                raise ValueError("透明像素丢失")


def unique_output(base: Path, relative: Path, suffix: str) -> Path:
    destination = base / relative
    if suffix == ".bmp":
        destination = destination.with_suffix(".png")
    if not destination.exists():
        return destination
    counter = 2
    while True:
        candidate = destination.with_name(f"{destination.stem}-{counter}{destination.suffix}")
        if not candidate.exists():
            return candidate
        counter += 1


def process_image(
    source: Path,
    relative: Path,
    output_root: Path,
    percent: int,
    exact_output: bool = False,
) -> ImageReport:
    original_bytes = source.stat().st_size
    destination = output_root if exact_output else unique_output(output_root, relative, source.suffix.lower())
    if source.suffix.lower() == ".bmp":
        destination = destination.with_suffix(".png")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with Image.open(source) as image:
            image.load()
            size = image.size
            frames = getattr(image, "n_frames", 1)
            if "A" in image.getbands():
                require_transparency = image.getchannel("A").getextrema()[0] < 255
            else:
                require_transparency = "transparency" in image.info
            target = max(512, round(original_bytes * percent / 100))
            suffix = source.suffix.lower()
            picker = CandidatePicker(target, original_bytes)
            if suffix in {".jpg", ".jpeg", ".webp"}:
                choose_quality(image, suffix, target, picker)
            elif suffix == ".png":
                choose_png(image, target, picker)
            elif suffix == ".bmp":
                choose_png(image, target, picker)
            else:
                choose_palette(image, suffix, picker)
            candidate = picker.best
            original_score = (abs(original_bytes - target), original_bytes > target, original_bytes)
            keep_original = suffix != ".bmp" and (picker.best_score is None or original_score <= picker.best_score)
            if not keep_original:
                if candidate is None:
                    raise ValueError("没有符合要求的压缩候选")
                validate(candidate, size, frames, require_transparency)
    except (OSError, ValueError, UnidentifiedImageError) as error:
        return ImageReport(str(source), "", original_bytes, original_bytes, 100.0, "failed", str(error))

    if keep_original:
        if source.suffix.lower() == ".bmp":
            destination = destination.with_suffix(".bmp")
        status = "kept-original"
        detail = "压缩候选未变小，输出原图副本"
        shutil.copyfile(source, destination)
        output_bytes = original_bytes
    else:
        status = "compressed"
        detail = ""
        destination.write_bytes(candidate)
        output_bytes = len(candidate)
    return ImageReport(
        str(source),
        str(destination),
        original_bytes,
        output_bytes,
        round(output_bytes / original_bytes * 100, 1) if original_bytes else 0.0,
        status,
        detail,
    )


def scan_balanced(text: str) -> list[str]:
    errors: list[str] = []
    opening = "([{"
    pairs = {")": "(", "]": "[", "}": "{"}
    stack: list[tuple[str, int]] = []
    quote = ""
    escaped = False
    for index, char in enumerate(text):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in "\"'`":
            quote = char
        elif char in opening:
            stack.append((char, index))
        elif char in pairs:
            if not stack or stack[-1][0] != pairs[char]:
                errors.append(f"位置 {index + 1} 的 {char} 没有正确配对")
            else:
                stack.pop()
    if quote:
        errors.append(f"存在未闭合的 {quote} 引号")
    if stack:
        errors.append("存在未闭合的 " + " ".join(char for char, _ in stack))
    return errors


def minify_loose(text: str) -> str:
    output: list[str] = []
    quote = ""
    escaped = False
    pending_space = False
    opening = set("{[(,:;=&")
    closing = set("}]),:;=&")
    for char in text.strip():
        if quote:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in "\"'`":
            if pending_space and output and output[-1] not in opening:
                output.append(" ")
            quote = char
            pending_space = False
            output.append(char)
        elif char.isspace():
            pending_space = True
        else:
            if pending_space and output and output[-1] not in opening and char not in closing:
                output.append(" ")
            pending_space = False
            output.append(char)
    return "".join(output)


def process_text(path: Path) -> TextReport:
    raw = path.read_bytes()
    text = ""
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            text = raw.decode(encoding).strip()
            break
        except UnicodeDecodeError:
            continue
    errors: list[str] = []
    if not text:
        errors.append("文件内容为空")
    full_width = sorted(FULL_WIDTH_CODE_PUNCTUATION.intersection(text))
    if full_width:
        errors.append("包含全角代码标点: " + " ".join(full_width))
    errors.extend(scan_balanced(text))
    compact = ""
    if not errors and text[:1] in "{[":
        try:
            compact = json.dumps(json.loads(text), ensure_ascii=False, separators=(",", ":"))
        except json.JSONDecodeError as error:
            errors.append(f"JSON 格式错误: 第 {error.lineno} 行第 {error.colno} 列")
    elif not errors and "&" in text:
        for part in text.split("&"):
            if part.strip() and "=" not in part:
                errors.append(f"参数缺少等号: {part.strip()}")
                break
        if not errors:
            compact = re.sub(r"\s*=\s*", "=", re.sub(r"\s*&\s*", "&", text))
    elif not errors:
        compact = minify_loose(text)
    return TextReport(str(path), not errors, errors, compact if not errors else "")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--target-percent", type=int, default=70)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--scan-only", action="store_true")
    parser.add_argument("--images-only", action="store_true")
    args = parser.parse_args()
    if not 0 <= args.target_percent <= 100:
        parser.error("--target-percent 必须是 0 到 100 的整数")
    if args.scan_only and args.images_only:
        parser.error("--scan-only 与 --images-only 不能同时使用")
    return args


def main() -> int:
    args = parse_args()
    try:
        images, texts, folder_input = collect(args.paths)
    except (FileNotFoundError, OSError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False))
        return 2

    output_root = (args.output or default_output(args.paths, folder_input)).resolve()
    single_image = len(args.paths) == 1 and args.paths[0].is_file() and len(images) == 1
    image_reports: list[ImageReport] = []
    if not args.scan_only and images:
        if output_root.exists():
            print(json.dumps({"error": f"输出位置已存在，请换一个位置: {output_root}"}, ensure_ascii=False))
            return 2
        if single_image:
            output_root.parent.mkdir(parents=True, exist_ok=True)
            image_reports = [process_image(images[0][0], images[0][1], output_root, args.target_percent, True)]
        else:
            output_root.mkdir(parents=True)
            image_reports = [process_image(src, rel, output_root, args.target_percent) for src, rel in images]

    text_reports = [] if args.images_only else [process_text(path) for path in texts]
    payload: dict[str, Any] = {
        "version": "1.0.18",
        "target_percent": args.target_percent,
        "output": str(output_root) if image_reports else "",
        "images": [asdict(report) for report in image_reports],
        "texts": [asdict(report) for report in text_reports],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    failed = any(report.status == "failed" for report in image_reports) or any(not report.ok for report in text_reports)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
