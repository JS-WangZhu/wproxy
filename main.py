"""
Simple GitHub-like proxy (ghproxy style)
---------------------------------------
Usage:
  /<url>                   →  proxy any HTTP/HTTPS URL
  /raw/<url>               →  convert GitHub blob links to raw.githubusercontent.com

Examples:
  http://127.0.0.1:8000/https://raw.githubusercontent.com/python/cpython/main/README.rst
  http://127.0.0.1:8000/raw/https://github.com/python/cpython/blob/main/README.rst
"""

import os
import re
from urllib.parse import urlparse, unquote
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException, Response
from fastapi.responses import StreamingResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
import httpx
from cachetools import TTLCache

# ===========================
# Configuration
# ===========================
DEFAULT_TIMEOUT = float(os.environ.get("GHPROXY_TIMEOUT", "15"))  # 15秒超时
CACHE_TTL = int(os.environ.get("GHPROXY_CACHE_TTL", "60"))       # 缓存60秒
CACHE_MAXSIZE = int(os.environ.get("GHPROXY_CACHE_MAXSIZE", "256"))
MAX_STREAM_CHUNK = 64 * 1024  # 64KB 流式块大小

# ===========================
# HTTP client + Cache
# ===========================
client = httpx.AsyncClient(follow_redirects=True, timeout=DEFAULT_TIMEOUT)
cache = TTLCache(maxsize=CACHE_MAXSIZE, ttl=CACHE_TTL) if CACHE_TTL > 0 else None

# ===========================
# Lifespan (现代写法)
# ===========================
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动阶段（可以放初始化逻辑）
    yield
    # 关闭阶段（释放资源）
    await client.aclose()

# 定义 FastAPI 应用对象
app = FastAPI(title="Simple GHProxy-like server", lifespan=lifespan)

# CORS 设置：允许所有来源（可按需限制）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ===========================
# Helper Functions
# ===========================
gh_blob_regex = re.compile(r'^https?://(?:www\.)?github\.com/([^/]+)/([^/]+)/blob/(.+)$', re.I)
gh_tree_regex = re.compile(r'^https?://(?:www\.)?github\.com/([^/]+)/([^/]+)/raw/(.+)$', re.I)

def github_to_raw(url: str) -> str:
    """Convert GitHub blob/raw URLs into raw.githubusercontent.com"""
    m = gh_blob_regex.match(url)
    if m:
        owner, repo, rest = m.groups()
        return f"https://raw.githubusercontent.com/{owner}/{repo}/{rest}"
    m2 = gh_tree_regex.match(url)
    if m2:
        owner, repo, rest = m2.groups()
        return f"https://raw.githubusercontent.com/{owner}/{repo}/{rest}"
    return url

def normalize_url_param(raw: str) -> str:
    """Clean and decode URL path"""
    u = unquote(raw.strip())
    if u.startswith("/http://") or u.startswith("/https://"):
        u = u[1:]
    return u

def allowed_scheme(url: str) -> bool:
    p = urlparse(url)
    return p.scheme in ("http", "https")

def pick_response_headers(resp: httpx.Response):
    """Preserve safe response headers"""
    hdrs = {}
    for k in ("content-type", "content-length", "accept-ranges", "etag", "last-modified", "cache-control"):
        v = resp.headers.get(k)
        if v:
            hdrs[k] = v
    return hdrs

async def stream_response(resp: httpx.Response):
    """Stream response chunks"""
    async for chunk in resp.aiter_bytes(MAX_STREAM_CHUNK):
        yield chunk

# ===========================
# Proxy Core Logic
# ===========================
async def do_proxy(request: Request, url: str, convert_github: bool = False):
    u = normalize_url_param(url)
    if not allowed_scheme(u):
        raise HTTPException(400, "Only http/https supported")

    if convert_github:
        u = github_to_raw(u)

    cache_key = f"proxy:{u}"
    if cache is not None and cache_key in cache:
        content, headers, status = cache[cache_key]
        return Response(content=content, status_code=status, headers=headers)

    # 构造请求头
    out_headers = {}
    if "range" in request.headers:
        out_headers["range"] = request.headers["range"]
    out_headers["user-agent"] = os.environ.get(
        "GHPROXY_USER_AGENT",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ghproxy/1.0",
    )

    try:
        resp = await client.get(u, headers=out_headers)
    except httpx.RequestError as e:
        raise HTTPException(502, f"Upstream request failed: {e}") from e

    picked = pick_response_headers(resp)
    status = resp.status_code

    # 缓存小文件（<1MB）
    if cache is not None and resp.headers.get("content-length"):
        try:
            clen = int(resp.headers["content-length"])
        except Exception:
            clen = None
    else:
        clen = None

    if cache is not None and (clen is not None and clen <= 1_048_576):
        content = resp.content
        cache[cache_key] = (content, picked, status)
        return Response(content=content, status_code=status, headers=picked)

    # 流式传输大文件
    return StreamingResponse(stream_response(resp), status_code=status, headers=picked)

# ===========================
# Routes
# ===========================
@app.get("/")
def index():
    return PlainTextResponse(
        "a high performance web proxy website based on python3"
    )

@app.get("/{url:path}")
async def proxy_path(url: str, request: Request):
    """Proxy any HTTP/HTTPS URL directly via path"""
    return await do_proxy(request, url, convert_github=False)

@app.get("/raw/{url:path}")
async def raw_path(url: str, request: Request):
    """Proxy GitHub blob/tree URLs as raw content"""
    return await do_proxy(request, url, convert_github=True)
