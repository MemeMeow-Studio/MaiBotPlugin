# 通过本机 HTTP 文件服务下载真实编码图片，检查响应类型、完整数据和动画附件。

import base64
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from threading import Thread
from uuid import uuid4

import pytest
from PIL import Image

from plugin import Candidate, MemeMeowClient, MemeMeowError, MemeMeowSettings, append_meme


@pytest.fixture
def media_server(tmp_path):
    """提供真实静态文件服务，为没有扩展名的媒体资源设置指定 Content-Type。"""
    servers = []
    threads = []

    def start(content_type):
        """按当前测试的媒体类型启动文件服务，返回服务根地址。"""
        class MediaHandler(SimpleHTTPRequestHandler):
            """使用标准 HTTP 文件处理器，为 UUID 文件名配置媒体类型。"""

            extensions_map = {**SimpleHTTPRequestHandler.extensions_map, "": content_type}

        server = ThreadingHTTPServer(("127.0.0.1", 0), partial(MediaHandler, directory=str(tmp_path)))
        thread = Thread(target=server.serve_forever, daemon=True)
        servers.append(server)
        threads.append(thread)
        thread.start()
        return f"http://127.0.0.1:{server.server_port}"

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()
    for thread in threads:
        thread.join()


@pytest.mark.parametrize(("image_format", "frames", "content_type"), [
    ("PNG", 1, "image/png"),
    ("JPEG", 1, "image/jpeg"),
    ("GIF", 2, "image/gif"),
    ("WEBP", 1, "image/webp"),
    ("WEBP", 2, "image/webp"),
    ("PNG", 2, "image/png"),
    ("PNG", 2, "image/apng"),
])
async def test_http_download_preserves_image_and_animation(tmp_path, media_server, image_format,
                                                         frames, content_type):
    """实际编码、下载并附加图片，逐帧验证动画像素、时长和循环次数保持完整。"""
    encoded = BytesIO()
    first = Image.new("RGB", (32, 24), (30, 80, 150))
    options = {}
    if frames > 1:
        options = {"save_all": True, "append_images": [Image.new("RGB", (32, 24), (200, 70, 20))],
                   "duration": [100, 200], "loop": 0}
    first.save(encoded, format=image_format, **options)
    original = encoded.getvalue()
    meme_id = str(uuid4())
    (tmp_path / meme_id).write_bytes(original)
    config = MemeMeowSettings(api_base=media_server(content_type), api_key="local-file-service", max_edge=0)
    candidate = Candidate(meme_id, f"/{meme_id}", {"name": "动画验证"})
    async with MemeMeowClient(config) as client:
        data, received_type = await client.download(candidate)
    assert received_type == content_type
    assert data == original
    messages = append_meme([
        {"segments": [{"type": "text", "data": "图片验证"}], "quote_previous": False},
    ], candidate, data)
    restored = base64.b64decode(messages[-1]["segments"][0]["binary_data_base64"], validate=True)
    assert restored == original
    with Image.open(BytesIO(original)) as source, Image.open(BytesIO(restored)) as image:
        assert image.format == image_format
        assert image.size == (32, 24)
        assert getattr(image, "n_frames", 1) == frames
        if frames > 1:
            assert image.is_animated
            assert image.info["loop"] == 0
        for index in range(frames):
            source.seek(index)
            image.seek(index)
            assert image.convert("RGBA").tobytes() == source.convert("RGBA").tobytes()
            assert image.info.get("duration") == source.info.get("duration")


async def test_http_download_rejects_non_image(tmp_path, media_server):
    """文件服务返回 HTML 时，应在下载阶段保留明确的媒体类型错误。"""
    meme_id = str(uuid4())
    (tmp_path / meme_id).write_text("<!doctype html><title>文件服务页面</title>", encoding="utf-8")
    config = MemeMeowSettings(api_base=media_server("text/html"), api_key="local-file-service")
    async with MemeMeowClient(config) as client:
        with pytest.raises(MemeMeowError) as error:
            await client.download(Candidate(meme_id, f"/{meme_id}", {}))
    assert error.value.details == {
        "error": "unexpected_media_type", "stage": "download", "content_type": "text/html",
    }
