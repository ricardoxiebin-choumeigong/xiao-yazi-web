from __future__ import annotations

import argparse
import base64
import cgi
import hmac
import json
import mimetypes
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote, urlparse


APP_ROOT = Path(__file__).resolve().parent
STATIC_ROOT = APP_ROOT / "static"
RUNTIME_ROOT = APP_ROOT / "runtime"
JOBS_ROOT = RUNTIME_ROOT / "jobs"
SKILL_SCRIPT = Path(os.environ.get("XIAO_YAZI_SCRIPT", APP_ROOT / "scripts" / "xiao_yazi.py"))
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
SUPPORTED_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".txt"}
MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024
JOBS: dict[str, Path] = {}
PROCESSING_SEMAPHORE = threading.BoundedSemaphore(1)


def cleanup_old_jobs(max_age_seconds: int = 24 * 60 * 60) -> None:
    JOBS_ROOT.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - max_age_seconds
    for path in JOBS_ROOT.iterdir():
        try:
            if path.is_dir() and path.stat().st_mtime < cutoff:
                shutil.rmtree(path)
        except OSError:
            continue


def safe_relative_path(raw: str, fallback: str) -> Path:
    normalized = (raw or fallback).replace("\\", "/").lstrip("/")
    parts = [part for part in PurePosixPath(normalized).parts if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        raise ValueError(f"无效的文件路径: {raw or fallback}")
    return Path(*parts)


def relative_to(path_text: str, root: Path) -> str:
    try:
        return Path(path_text).resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return Path(path_text).name


def unique_staging_path(base: Path, relative: Path) -> Path:
    destination = base / relative
    if not destination.exists():
        return destination
    counter = 2
    while True:
        candidate = destination.with_name(f"{destination.stem}-{counter}{destination.suffix}")
        if not candidate.exists():
            return candidate
        counter += 1


class XiaoYaziHandler(BaseHTTPRequestHandler):
    server_version = "XiaoYaziLocal/1.0.18"

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def send_json(self, payload: object, status: int = HTTPStatus.OK) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def require_auth(self) -> bool:
        if not APP_PASSWORD:
            return True
        authorization = self.headers.get("Authorization", "")
        valid = False
        if authorization.startswith("Basic "):
            try:
                decoded = base64.b64decode(authorization[6:]).decode("utf-8")
                _, password = decoded.split(":", 1)
                valid = hmac.compare_digest(password, APP_PASSWORD)
            except (ValueError, UnicodeDecodeError):
                valid = False
        if valid:
            return True
        body = "需要访问密码".encode("utf-8")
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("WWW-Authenticate", 'Basic realm="Xiao Yazi", charset="UTF-8"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return False

    def send_file(self, path: Path, *, download_name: str | None = None) -> None:
        if not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        size = path.stat().st_size
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        if download_name:
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(download_name)}")
        self.end_headers()
        with path.open("rb") as handle:
            shutil.copyfileobj(handle, self.wfile)

    def do_GET(self) -> None:
        if not self.require_auth():
            return
        parsed = urlparse(self.path)
        request_path = unquote(parsed.path)
        if request_path in {"/", "/index.html"}:
            self.send_file(STATIC_ROOT / "index.html")
            return
        if request_path.startswith("/static/"):
            relative = safe_relative_path(request_path.removeprefix("/static/"), "index.html")
            target = (STATIC_ROOT / relative).resolve()
            if STATIC_ROOT.resolve() not in target.parents:
                self.send_error(HTTPStatus.FORBIDDEN)
                return
            self.send_file(target)
            return
        if request_path.startswith("/api/jobs/") and "/files/" in request_path:
            prefix, raw_relative = request_path.split("/files/", 1)
            job_id = prefix.rstrip("/").split("/")[-1]
            output_root = JOBS.get(job_id, JOBS_ROOT / job_id / "output")
            relative = safe_relative_path(raw_relative, "result")
            target = (output_root / relative).resolve()
            if output_root.resolve() not in target.parents:
                self.send_error(HTTPStatus.FORBIDDEN)
                return
            self.send_file(target, download_name=target.name)
            return
        if request_path.startswith("/api/jobs/") and request_path.endswith("/archive"):
            job_id = request_path.rstrip("/").split("/")[-2]
            archive = JOBS_ROOT / job_id / "小压子-处理结果.zip"
            self.send_file(archive, download_name=archive.name)
            return
        if request_path == "/api/health":
            self.send_json({"ok": True, "script": str(SKILL_SCRIPT), "script_exists": SKILL_SCRIPT.is_file()})
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        if not self.require_auth():
            return
        if urlparse(self.path).path != "/api/process":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            self.process_upload()
        except ValueError as error:
            self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        except Exception as error:
            self.send_json({"error": f"处理服务发生错误: {error}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def process_upload(self) -> None:
        if not SKILL_SCRIPT.is_file():
            raise ValueError(f"找不到小压子脚本: {SKILL_SCRIPT}")
        content_length = int(self.headers.get("Content-Length", "0") or 0)
        if content_length <= 0:
            raise ValueError("没有收到文件")
        if content_length > MAX_UPLOAD_BYTES:
            raise ValueError("本次文件总量超过 2 GB")

        form = cgi.FieldStorage(
            fp=self.rfile,
            headers=self.headers,
            environ={
                "REQUEST_METHOD": "POST",
                "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                "CONTENT_LENGTH": str(content_length),
            },
            keep_blank_values=True,
        )
        fields = form.list or []
        uploads = [field for field in fields if field.name == "files" and field.filename]
        raw_paths = [str(field.value) for field in fields if field.name == "paths"]
        if not uploads:
            raise ValueError("没有可处理的文件")
        if len(raw_paths) != len(uploads):
            raw_paths = [field.filename or f"file-{index + 1}" for index, field in enumerate(uploads)]

        try:
            target_percent = int(form.getfirst("target_percent", "70"))
        except ValueError as error:
            raise ValueError("目标百分比必须是 0 到 100 的整数") from error
        if not 0 <= target_percent <= 100:
            raise ValueError("目标百分比必须是 0 到 100 的整数")

        mode = form.getfirst("mode", "all")
        if mode not in {"all", "images", "scan"}:
            raise ValueError("无效的处理范围")

        job_id = uuid.uuid4().hex
        job_root = JOBS_ROOT / job_id
        input_root = job_root / "input"
        output_root = job_root / "output"
        input_root.mkdir(parents=True)

        accepted = 0
        try:
            for upload, raw_path in zip(uploads, raw_paths, strict=True):
                relative = safe_relative_path(raw_path, upload.filename or "file")
                if relative.suffix.lower() not in SUPPORTED_SUFFIXES:
                    continue
                destination = unique_staging_path(input_root, relative)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with destination.open("wb") as target:
                    shutil.copyfileobj(upload.file, target)
                accepted += 1

            if not accepted:
                raise ValueError("没有找到支持的图片或 TXT 文件")

            command = [
                sys.executable,
                str(SKILL_SCRIPT),
                str(input_root),
                "--target-percent",
                str(target_percent),
                "--output",
                str(output_root),
            ]
            if mode == "images":
                command.append("--images-only")
            elif mode == "scan":
                command.append("--scan-only")

            with PROCESSING_SEMAPHORE:
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=60 * 30,
                    check=False,
                )
            stdout = completed.stdout.strip()
            if not stdout:
                detail = completed.stderr.strip()
                if completed.returncode not in {0}:
                    detail = detail or "大图处理进程被服务器中止，通常是临时内存不足，请稍后单独重试这张图片"
                raise ValueError(detail or "处理脚本没有返回结果")
            try:
                report = json.loads(stdout)
            except json.JSONDecodeError as error:
                raise ValueError(f"无法读取处理结果: {stdout[-500:]}") from error
            if report.get("error"):
                raise ValueError(str(report["error"]))

            images = []
            for item in report.get("images", []):
                transformed = dict(item)
                transformed["relative_source"] = relative_to(item.get("source", ""), input_root)
                output_text = item.get("output", "")
                if output_text:
                    relative_output = relative_to(output_text, output_root)
                    transformed["relative_output"] = relative_output
                    transformed["download_url"] = f"/api/jobs/{job_id}/files/{relative_output}"
                else:
                    transformed["relative_output"] = ""
                    transformed["download_url"] = ""
                images.append(transformed)

            texts = []
            for item in report.get("texts", []):
                transformed = dict(item)
                transformed["relative_source"] = relative_to(item.get("source", ""), input_root)
                transformed.pop("source", None)
                texts.append(transformed)

            archive_url = ""
            if output_root.is_dir() and any(item.get("download_url") for item in images):
                archive = job_root / "小压子-处理结果.zip"
                with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
                    for result_file in sorted(output_root.rglob("*")):
                        if result_file.is_file():
                            bundle.write(result_file, result_file.relative_to(output_root).as_posix())
                archive_url = f"/api/jobs/{job_id}/archive"

            JOBS[job_id] = output_root
            response = {
                "version": report.get("version", "1.0.18"),
                "job_id": job_id,
                "target_percent": target_percent,
                "images": images,
                "texts": texts,
                "archive_url": archive_url,
                "failed": completed.returncode not in {0},
            }
            (job_root / "report.json").write_text(
                json.dumps(response, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            self.send_json(response)
        finally:
            shutil.rmtree(input_root, ignore_errors=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="小压子本地网页服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    cleanup_old_jobs()
    server = ThreadingHTTPServer((args.host, args.port), XiaoYaziHandler)
    print(f"小压子已启动: http://{args.host}:{args.port}/")
    print("关闭此窗口即可停止本地服务。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
