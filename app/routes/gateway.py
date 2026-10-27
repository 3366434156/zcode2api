"""核心网关：/v1/messages（Anthropic 风格）与 /v1/chat/completions（OpenAI 风格）。

共用多账号轮询 + 额度用完自动换号 + 阿里无痕验证自动续期；OpenAI 端点由
openai_compat 做双向格式转换，调度与错误处理策略完全一致。
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .. import affinity, constants, logs, reqlog, settings
from ..agent import build_request
from ..auth_admin import verify_gateway_key
from ..captcha import captcha_manager
from ..models import Account, Status
from ..openai_compat import (
    StreamConverter,
    UsageSniffer,
    anthropic_to_openai,
    openai_to_anthropic,
)
from ..quota import fetch_quota
from ..store import store

_sleep = asyncio.sleep  # 模块级引用：测试可 patch 此名而免污染全局 asyncio

router = APIRouter()

MAX_CAPTCHA_RETRIES = 3
MAX_ACCOUNT_ATTEMPTS = 5

# 常量收口：模型表与被拒信号关键字统一在 app/constants.py
MODEL_NAME_MAP = constants.MODEL_NAME_MAP
AVAILABLE_MODELS = constants.AVAILABLE_MODELS
_EXHAUST_KEYWORDS = constants.EXHAUST_KEYWORDS


def _detect_provider(body: dict, headers) -> str:
    model = body.get("model") or ""
    if model.startswith("bigmodel/") or headers.get("x-provider") == "bigmodel":
        return "bigmodel"
    return "zai"


def _normalize_body(body: dict) -> dict:
    model = body.get("model")
    if isinstance(model, str) and "/" in model:
        model = "/".join(model.split("/")[1:])
    if isinstance(model, str):
        model = MODEL_NAME_MAP.get(model.lower(), model)
        body["model"] = model

    # 上游对 max_tokens 有硬校验（400 code 1210），钳制到合法区间并记录钳制动作
    raw = body.get("max_tokens")
    if raw is not None and not isinstance(raw, bool):
        try:
            mt = int(float(raw))
        except (TypeError, ValueError):
            mt = None
        if mt is not None:
            clamped = max(1, min(mt, constants.MAX_TOKENS_LIMIT))
            if clamped != mt:
                logs.warn("gateway", f"max_tokens {mt} 超出上游范围 [1,{constants.MAX_TOKENS_LIMIT}]，钳制为 {clamped}")
            body["max_tokens"] = clamped

    messages = body.get("messages")
    if isinstance(messages, list):
        bridged = []
        for msg in messages:
            if isinstance(msg, dict) and isinstance(msg.get("content"), str):
                bridged.append({**msg, "content": [{"type": "text", "text": msg["content"]}]})
            else:
                bridged.append(msg)
        body["messages"] = bridged
    return body


def _is_captcha_error(text: str) -> bool:
    low = text.lower()
    return "captcha" in low or "verify token" in low or "verify failed" in low


def _detect_captcha_challenge(resp: httpx.Response, text: str | None = None) -> str | None:
    """验证码挑战双检测（对齐 zapi handler.ts）。

    三种形态：
      1. 响应头 x-aliyun-captcha-verify-param 存在（官方挑战信号）
      2. HTTP 400/403 + body {"code":3007}（2026-08 观测的 body 内挑战）
      3. HTTP 403 + 文案 captcha/verify（老检测，保留兼容）
    返回挑战标记（非 None 即挑战），否则 None。
    """
    # 1) challenge 响应头
    header_val = resp.headers.get(constants.CAPTCHA_HEADER)
    if header_val and header_val.strip():
        return "header"

    if text is None:
        return None
    low = text.lower()

    # 2) body code 3007（400/403 任意状态）
    if resp.status_code in (400, 403) and any(m in text for m in constants.CAPTCHA_BODY_MARKERS):
        return "in-body-3007"

    # 3) 403 + 挑战文案
    if resp.status_code == 403 and _is_captcha_error(low):
        return "text"

    return None


def _is_exhausted(status_code: int, text: str) -> bool:
    # 429 是频控信号，优先于一切 body 关键词：429 body 带额度文案时
    # （api.z.ai 实测形态）必须走频控重试，不得判成额度耗尽踢号。
    if status_code == 429:
        return False
    if status_code in constants.EXHAUST_HTTP_STATUSES:
        return True
    low = text.lower()
    return any(k in low for k in _EXHAUST_KEYWORDS)


def _is_risk_control(status_code: int, text: str) -> bool:
    """风控信号判定（3012「unusual activity」/ messages 端点 405）。

    与验证码挑战互斥：调用点已先排除 challenge 形态。命中即账号级风控，
    需指数退避冷却，而非直接回传客户端错误（会导致下次立刻重打、加剧风控）。
    """
    if status_code in constants.RISK_CONTROL_HTTP_STATUSES:
        return True
    low = text.lower()
    return any(m.lower() in low for m in constants.RISK_CONTROL_MARKERS)


def _parse_retry_after(value: str | None) -> int | None:
    """解析 Retry-After（仅秒数形态；HTTP-date 形态少见，放弃即用默认重试等待）。

    非正数不采信；超长值封顶采信 —— 尊重上游意图的同时防止把客户端吊死。
    """
    if not value:
        return None
    try:
        secs = int(float(value.strip()))
    except (ValueError, AttributeError):
        return None
    return min(secs, settings.RETRY_429_WAIT_MAX) if secs > 0 else None


def _mark(account: Account, status_value: str, error: str | None = None) -> None:
    account.status = status_value
    account.last_error = error
    if status_value == Status.COOLING:
        account.cooling_until = time.time() + settings.COOLING_SECONDS
    store.update_account(account)


def _last_user_text(body: dict) -> str:
    for msg in reversed(body.get("messages") or []):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    return part.get("text", "")
    return ""


@router.get("/v1/models", dependencies=[Depends(verify_gateway_key)])
async def list_models():
    """列出可用模型（Anthropic /v1/models 风格）。"""
    return {
        "object": "list",
        "data": [
            {"id": i, "type": "model", "display_name": i, "created_at": "2025-01-01T00:00:00Z"}
            for i in AVAILABLE_MODELS
        ],
    }


@router.post("/v1/messages", dependencies=[Depends(verify_gateway_key)])
async def messages(request: Request):
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse({"error": {"message": "请求体不是合法 JSON", "type": "invalid_request"}}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse(
            {"error": {"message": "请求体必须是 JSON 对象", "type": "invalid_request_error"}},
            status_code=400,
        )

    incoming_headers = dict(request.headers)
    provider = _detect_provider(body, request.headers)
    requested_model = str(body.get("model") or "")   # 归一化前客户端点的模型
    body = _normalize_body(body)
    # 验证码页面由本服务托管，端口取实际请求端口（兼容任意启动端口）
    port = request.url.port or settings.PORT

    req_id = secrets.token_hex(8)
    logs.req(req_id, str(body.get("model") or "-"), bool(body.get("stream")), _last_user_text(body))
    reqlog.begin(req_id, "messages", str(body.get("model") or "-"),
                 bool(body.get("stream")), _last_user_text(body),
                 provider=provider, requested_model=requested_model,
                 client=request.headers.get("user-agent", ""))

    try:
        result = await _dispatch(req_id, body, incoming_headers, port, provider,
                                 affinity.key_of(body))
    except asyncio.CancelledError:
        # 客户端在调度期间断开（429 重试/验证码等待可达数分钟）——CancelError
        # 是 BaseException，不兜底会让监控条目永久滞留「进行中」
        reqlog.finish_error(req_id, "客户端断开", status=499,
                            outcome=reqlog.OUTCOME_CLIENT_ABORTED)
        raise
    except Exception as err:  # noqa: BLE001 - 调度层意外异常也要收口监控条目
        reqlog.finish_error(req_id, f"网关内部错误: {err}", status=500)
        return JSONResponse(
            {"error": {"message": "网关内部错误", "type": "internal_error"}},
            status_code=500,
        )
    if isinstance(result, _Upstream):
        # dispatch 返回与流式生成器启动之间的取消窗口：兜底关闭释放并发槽位
        try:
            return result.to_streaming(req_id)
        except asyncio.CancelledError:
            await result.close()
            raise
    return result


@router.post("/v1/chat/completions", dependencies=[Depends(verify_gateway_key)])
async def chat_completions(request: Request):
    try:
        payload = await request.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse({"error": {"message": "请求体不是合法 JSON", "type": "invalid_request_error"}}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"error": {"message": "请求体必须是 JSON 对象", "type": "invalid_request_error"}}, status_code=400)

    body, err = openai_to_anthropic(payload)
    if err or body is None:
        return JSONResponse({"error": {"message": err or "请求体不合法", "type": "invalid_request_error"}}, status_code=400)

    incoming_headers = dict(request.headers)
    provider = _detect_provider(body, request.headers)
    body = _normalize_body(body)
    port = request.url.port or settings.PORT

    req_id = secrets.token_hex(8)
    logs.req(req_id, str(body.get("model") or "-"), bool(payload.get("stream")), _last_user_text(body))
    reqlog.begin(req_id, "chat", str(body.get("model") or "-"),
                 bool(payload.get("stream")), _last_user_text(body),
                 provider=provider, requested_model=str(payload.get("model") or ""),
                 client=request.headers.get("user-agent", ""))

    try:
        result = await _dispatch(req_id, body, incoming_headers, port, provider,
                                 affinity.key_of(body))
    except asyncio.CancelledError:
        reqlog.finish_error(req_id, "客户端断开", status=499,
                            outcome=reqlog.OUTCOME_CLIENT_ABORTED)
        raise
    except Exception as err:  # noqa: BLE001 - 调度层意外异常也要收口监控条目
        reqlog.finish_error(req_id, f"网关内部错误: {err}", status=500)
        return JSONResponse(
            {"error": {"message": "网关内部错误", "type": "internal_error"}},
            status_code=500,
        )
    if not isinstance(result, _Upstream):
        return result

    model = str(body.get("model") or "")
    if payload.get("stream"):
        try:
            return _openai_stream_response(result, model, req_id)
        except asyncio.CancelledError:
            await result.close()
            raise

    try:
        raw = await result.read()
        logs.req_ok(req_id)
    except asyncio.CancelledError:
        reqlog.finish_error(req_id, "客户端断开", status=499, t_first=result.t_first,
                            outcome=reqlog.OUTCOME_CLIENT_ABORTED)
        raise
    except Exception as err:  # noqa: BLE001
        logs.req_err(req_id, f"读取上游响应失败: {err}")
        reqlog.finish_error(req_id, f"读取上游响应失败: {err}", status=502,
                            outcome=reqlog.OUTCOME_UPSTREAM_ABORTED)
        return JSONResponse({"error": {"message": f"读取上游响应失败: {err}", "type": "upstream_error"}}, status_code=502)
    finally:
        await result.close()
    data = _safe_json(raw.decode("utf-8", "ignore"))
    if not isinstance(data, dict) or data.get("type") != "message":
        reqlog.finish_error(req_id, "上游响应格式异常", status=502, t_first=result.t_first,
                            outcome=reqlog.OUTCOME_UPSTREAM_ABORTED)
        return JSONResponse({"error": {"message": "上游响应格式异常", "type": "upstream_error"}}, status_code=502)
    usage = data.get("usage") or {}
    reqlog.mark_first(req_id)
    reqlog.mark_last(req_id)
    reqlog.note_usage(req_id, **_usage_kwargs(usage))
    reqlog.finish_ok(req_id, t_first=result.t_first, status=result.resp.status_code)
    return JSONResponse(anthropic_to_openai(data, model))


def _usage_kwargs(usage: dict) -> dict:
    """上游 usage 块 → reqlog.note_usage 的关键字参数（缺字段就不传）。"""
    return {
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "cached_tokens": usage.get("cache_read_input_tokens", usage.get("cached_tokens")),
        "credit": usage.get("credit") if isinstance(usage.get("credit"), (int, float)) else None,
    }


def _openai_stream_response(up: _Upstream, model: str, req_id: str) -> StreamingResponse:
    """把上游 Anthropic SSE 事件流转换为 OpenAI chunk 流。"""
    conv = StreamConverter(model)

    async def _iter():
        try:
            yield conv.start()
            async for line in _paced(up.lines()):
                if line is _KEEPALIVE:
                    yield line  # 上游静默期的心跳，原样透给客户端
                    continue
                if not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if not data_str:
                    continue
                evt = _safe_json(data_str)
                if isinstance(evt, dict):
                    out = conv.feed(evt)
                    if out:
                        reqlog.mark_first(req_id)
                        reqlog.mark_last(req_id)
                    for chunk in out:
                        yield chunk
            yield conv.done()
            logs.req_ok(req_id)
            reqlog.note_usage(req_id, input_tokens=conv.usage.get("prompt_tokens"),
                              output_tokens=conv.usage.get("completion_tokens"),
                              cached_tokens=conv.usage.get("cached_tokens"))
            reqlog.finish_ok(req_id, t_first=up.t_first, status=up.resp.status_code)
        except asyncio.CancelledError:
            reqlog.finish_error(req_id, "客户端断开", status=499, t_first=up.t_first,
                                outcome=reqlog.OUTCOME_CLIENT_ABORTED)
            raise
        except Exception as err:  # noqa: BLE001
            logs.req_err(req_id, f"流传输中断: {err}")
            reqlog.note_usage(req_id, input_tokens=conv.usage.get("prompt_tokens"),
                              output_tokens=conv.usage.get("completion_tokens"),
                              cached_tokens=conv.usage.get("cached_tokens"))
            reqlog.finish_error(req_id, f"流传输中断: {err}", t_first=up.t_first,
                                outcome=reqlog.OUTCOME_UPSTREAM_ABORTED)
        finally:
            await up.close()

    return StreamingResponse(_iter(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})


async def _dispatch(req_id, body, incoming_headers, port, provider, conv_key: str = ""):
    """多账号轮询调度：_Upstream（成功）或 JSONResponse（错误）。

    选号顺序：会话粘性记住的账号优先（同一段对话回到同一账号，上游前缀缓存
    才能继续命中），否则 round-robin。单账号并发限制：选号后若该账号在飞请求
    已达上限（store.account_concurrency，0 = 不限），跳过换下一个账号——不排队
    （流式请求可占槽位数分钟，排队会放大延迟甚至吊死客户端）。满号跳过不计入
    MAX_ACCOUNT_ATTEMPTS（只计真正进入 _try_account 的次数）。全满/无号 → 503。
    """
    tried: set[str] = set()
    limit = _limit()
    attempts = 0
    prefer = affinity.prefer(provider, conv_key)
    # 换号原因的出口：最后一个账号为什么不可用（模型额度用尽等），503 时如实回给客户端
    reason: list[str] = [""]

    while attempts < MAX_ACCOUNT_ATTEMPTS:
        account = store.select(provider, skip_ids=tried, prefer=prefer)
        if account is None:
            break
        tried.add(account.id)
        if limit > 0 and _inflight.get(account.id, 0) >= limit:
            logs.warn(req_id, f"账号 {account.name} 并发已满（{_inflight.get(account.id, 0)}/{limit}），切换下一个")
            continue
        attempts += 1
        needs_captcha = provider == "zai" and account.uses_plan_channel()

        slot_box: list[str | None] = [None]
        if limit > 0:
            _inflight[account.id] = _inflight.get(account.id, 0) + 1
            slot_box[0] = account.id
        try:
            result = await _try_account(
                req_id, account, body, incoming_headers, port, needs_captcha, slot_box,
                reason,
            )
        except BaseException:
            if slot_box[0] is not None:
                _release_slot(slot_box[0])
                slot_box[0] = None
            raise
        if result is _NEXT_ACCOUNT:
            if slot_box[0] is not None:
                _release_slot(slot_box[0])
                slot_box[0] = None
            continue
        if isinstance(result, _Upstream):
            affinity.remember(provider, conv_key, account.id)
            held = slot_box[0]
            slot_box[0] = None
            if held is not None:
                result.on_close = _make_slot_releaser(held)
            return result
        if slot_box[0] is not None:
            _release_slot(slot_box[0])
            slot_box[0] = None
        return result

    detail = reason[0] or "无可用账号 / 额度均已耗尽 / 并发已满"
    logs.req_err(req_id, detail)
    reqlog.finish_error(req_id, detail, status=503)
    message = reason[0] or "所有账号均不可用、额度已用完或并发已满，请在后台检查账号状态"
    return JSONResponse(
        {"error": {"message": message, "type": "no_available_account"}},
        status_code=503,
    )


def _release_slot(account_id: str) -> None:
    n = _inflight.get(account_id, 0) - 1
    if n <= 0:
        _inflight.pop(account_id, None)
    else:
        _inflight[account_id] = n


def _park_slot(slot_box: list[str | None] | None) -> None:
    """等待（429/验证码/5xx）前释放并发槽，避免把账号冻住数分钟。"""
    if slot_box and slot_box[0] is not None:
        _release_slot(slot_box[0])
        slot_box[0] = None


def _reacquire_slot(account: Account, slot_box: list[str | None] | None) -> bool:
    """等待结束后重新占槽；占不到则让调用方换号。"""
    if slot_box is None:
        return True
    limit = _limit()
    if limit <= 0:
        return True
    if _inflight.get(account.id, 0) >= limit:
        return False
    _inflight[account.id] = _inflight.get(account.id, 0) + 1
    slot_box[0] = account.id
    return True


def _make_slot_releaser(account_id: str):
    def _release() -> None:
        _release_slot(account_id)
    return _release


_NEXT_ACCOUNT = object()


# fire-and-forget 后台任务强引用：事件循环对 task 只持弱引用，裸 create_task
# 会被 GC 中途静默丢弃（同 main.py 启动安装序已修过的缺陷，2026-09 review）。
_bg_tasks: set[asyncio.Task] = set()


def _spawn_bg(coro) -> None:
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


# 账号并发限制：account_id → 在飞请求数。asyncio 单线程事件循环下
# check+inc 原子；释放走 _Upstream.close / 失败路径，泄漏面在测试钉住。
_inflight: dict[str, int] = {}


def _limit() -> int:
    """当前并发上限（0 = 不限），实时读取设置（后台改后即生效）。"""
    return store.account_concurrency()


# SSE 注释帧：所有 SSE 客户端都会跳过，它只表示「连接还活着」。
# 结尾不留空行——有些客户端（openai-go v2）把空行当事件解析，会报 JSON 错
# （magpie 的 sseWriter.comment 同样只写 ": keepalive\n"）。
_KEEPALIVE = ": keep-alive\n"


async def _paced(source, idle: float | None = None, stall: float | None = None):
    """把上游分块流转发给客户端，同时做两件事。

    1. 客户端空闲满 idle 秒就补一条 SSE 注释帧。模型思考阶段上游可能只有
       thinking 事件（甚至什么都没有），没有注释帧，客户端与中间代理都无从
       判断连接是否还活着，界面只能显示「未响应」。
    2. 上游彻底静默满 stall 秒即判定连接已死并抛错。httpx 的 read 超时是
       None（长回复不能设总时长上限），缺了这层看门狗，半死的 TCP 连接会把
       请求永久挂住——客户端也就永久「未响应」。

    上游读取放在独立 task 里当生产者，消费侧只等队列：空闲超时因此不会取消
    上游的读操作（取消读会破坏 httpx 的流状态）。
    """
    idle = idle if idle is not None else settings.SSE_KEEPALIVE_IDLE
    stall = stall if stall is not None else settings.UPSTREAM_STALL_SECONDS
    queue: asyncio.Queue = asyncio.Queue()
    eof = object()

    async def pump() -> None:
        try:
            async for item in source:
                await queue.put(item)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 - 读上游失败交给消费侧统一收口
            await queue.put(err)
        finally:
            await queue.put(eof)

    task = asyncio.create_task(pump())
    silent = 0.0
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), idle)
            except TimeoutError as err:
                silent += idle
                if silent >= stall:
                    raise TimeoutError(
                        f"上游 {stall:.0f}s 没有任何数据，判定连接已死"
                    ) from err
                yield _KEEPALIVE
                continue
            silent = 0.0
            if item is eof:
                return
            if isinstance(item, BaseException):
                raise item
            yield item
    finally:
        task.cancel()


class _Upstream:
    """已建立的上游成功流：由调用方消费并负责关闭。

    `preloaded` 是调度阶段就已经读进内存的响应体（非 SSE 的响应）：上游对业务
    错误也回 200（body 形如 `{"code":1005,"msg":"exceed quota limit"}`），要判断
    是错误还是整体回复，只能先读一次——读完就把内容挂在这里，消费方别再读流。
    """

    __slots__ = ("resp", "cm", "client", "t_first", "account_name", "mode", "on_close",
                 "preloaded", "_closed")

    def __init__(self, resp: httpx.Response, cm, client: httpx.AsyncClient,
                 t_first: float | None = None, account_name: str = "", mode: str = "",
                 on_close=None, preloaded: bytes | None = None) -> None:
        self.resp = resp
        self.cm = cm
        self.client = client
        self.t_first = t_first
        self.account_name = account_name
        self.mode = mode
        self.on_close = on_close
        self.preloaded = preloaded
        self._closed = False

    async def read(self) -> bytes:
        """整体读取响应体（调度阶段已读过的直接返回，避免重复消费）。"""
        if self.preloaded is not None:
            return self.preloaded
        return await self.resp.aread()

    async def chunks(self):
        """响应体的字节流（透传路径用）。"""
        if self.preloaded is not None:
            if self.preloaded:
                yield self.preloaded
            return
        async for chunk in self.resp.aiter_bytes():
            yield chunk

    async def lines(self):
        """响应体的文本行流（OpenAI 转换路径用）。"""
        if self.preloaded is not None:
            for line in self.preloaded.decode("utf-8", "ignore").splitlines():
                yield line
            return
        async for line in self.resp.aiter_lines():
            yield line

    async def close(self) -> None:
        """幂等关闭：释放上游流与并发槽位（on_close），重复调用安全。"""
        if self._closed:
            return
        self._closed = True
        await self.cm.__aexit__(None, None, None)
        await self.client.aclose()
        if self.on_close is not None:
            try:
                self.on_close()
            except Exception:  # noqa: BLE001 - 槽位释放失败不掩盖主流程
                pass

    def to_streaming(self, req_id: str) -> StreamingResponse:
        """原样透传（/v1/messages 直通路径）。

        透传不解析 body，于是 token 用量只能靠旁路嗅探：`UsageSniffer` 把流过的
        字节攒起来（上限 1 MB），结束时解析出 usage 落台账——否则这类请求在
        「请求监控」里的输入/输出永远是空（旧实现在这里直接丢掉了用量）。
        """
        up = self
        sniffer = UsageSniffer()

        async def _body_iter():
            first = True
            try:
                async for chunk in _paced(up.chunks()):
                    if chunk is _KEEPALIVE:
                        yield chunk  # 静默期心跳：不算内容，不影响首字计时
                        continue
                    sniffer.feed(chunk if isinstance(chunk, bytes) else chunk.encode("utf-8"))
                    if first:
                        first = False
                        reqlog.mark_first(req_id)
                    reqlog.mark_last(req_id)
                    yield chunk
                reqlog.note_usage(req_id, **_usage_kwargs(sniffer.result()))
                logs.req_ok(req_id)
                reqlog.finish_ok(req_id, t_first=up.t_first, status=up.resp.status_code)
            except asyncio.CancelledError:
                reqlog.note_usage(req_id, **_usage_kwargs(sniffer.result()))
                reqlog.finish_error(req_id, "客户端断开", status=499, t_first=up.t_first,
                                    outcome=reqlog.OUTCOME_CLIENT_ABORTED)
                raise
            except Exception as err:  # noqa: BLE001
                logs.req_err(req_id, f"流传输中断: {err}")
                reqlog.note_usage(req_id, **_usage_kwargs(sniffer.result()))
                reqlog.finish_error(req_id, f"流传输中断: {err}", t_first=up.t_first,
                                    outcome=reqlog.OUTCOME_UPSTREAM_ABORTED)
            finally:
                await up.close()

        return StreamingResponse(_body_iter(), status_code=up.resp.status_code,
                                 media_type=up.resp.headers.get("content-type", "application/json"),
                                 headers={"Cache-Control": "no-cache"})


async def _try_account(req_id, account, body, incoming_headers, port, needs_captcha,
                       slot_box: list | None = None, reason: list | None = None):
    """尝试用单个账号转发，含验证码续期与可配置重试。

    错误处理策略（参数见 settings，均可用环境变量调整）：
      - 验证码挑战：清池换码重建请求，最多 MAX_CAPTCHA_RETRIES 次
      - 429 频控：**不冷却账号**，按上游 Retry-After（封顶 RETRY_429_WAIT_MAX）
        或 RETRY_429_WAIT 等待后原地重试，最多 RETRY_429_TIMES 次；
        耗尽后换下一个账号，账号保持可用。Plan 通道耗尽且有 API Key 时切
        回退通道重试（force_fallback 显式路由——429 不改账号状态，不能靠
        status 推导通道；回退通道自己的 429 重试预算独立计满后再换号）
      - 5xx 等一般错误：重试最多 RETRY_5XX_TIMES 次；耗尽后账号冷却
        COOLING_SECONDS 并换下一个账号
      - 风控（3012/405「unusual activity」）：指数退避冷却（base 900s 起，封顶
        24h），累计 RISK_BAN_STRIKES 次升级禁用（UI 展示），人工确认恢复
    """
    captcha_retries = 0
    retries_429 = 0
    retries_5xx = 0
    force_fallback = False  # 本请求瞬态走 Key 回退（不改账号持久化状态）
    model_name = str(body.get("model") or "-")
    while True:
        attempt_t0 = time.time()
        reqlog.mark_account(req_id, account.name, account.mode, account.id)
        verify_param = verify_region = None
        # CLI 形态（默认）实测不需要无痕验证码：省掉 10–40s 的 Node+浏览器求解。
        # desktop 形态保留原求解流程。
        if needs_captcha and settings.MESSAGES_STYLE != "cli":
            _park_slot(slot_box)
            try:
                verify_param, verify_region = await captcha_manager.get_verify_param(port)
            except Exception as err:  # noqa: BLE001
                logs.req_err(req_id, f"人机校验失败: {err}")
                reqlog.finish_error(req_id, f"人机校验失败: {err}", status=500)
                return JSONResponse(
                    {"error": {"message": f"无法完成人机校验: {err}", "type": "captcha_error"}},
                    status_code=500,
                )
            if not _reacquire_slot(account, slot_box):
                logs.warn(req_id, f"账号 {account.name} 验证码等待后并发已满，切换下一个")
                return _NEXT_ACCOUNT

        try:
            url, headers, payload = build_request(account, body, verify_param,
                                                  incoming_headers, verify_region,
                                                  force_fallback=force_fallback)
        except RuntimeError as err:
            account.record_result(False, f"凭证无效: {err}")
            _mark(account, Status.INVALID, str(err))
            logs.warn(req_id, f"账号 {account.name} 凭证无效，切换下一个")
            return _NEXT_ACCOUNT

        client = httpx.AsyncClient(timeout=httpx.Timeout(connect=30.0, read=None, write=120.0, pool=30.0))
        cm = client.stream("POST", url, headers=headers, content=payload)
        try:
            resp = await cm.__aenter__()
        except httpx.HTTPError as err:
            await client.aclose()
            account.record_result(False, f"连接失败: {err}")
            # 废 JWT / 风控禁用走 Key 回退失败时不得洗成 cooling，否则冷却结束会重开 Plan
            if account.status in (Status.INVALID, Status.DISABLED):
                store.update_account(account)
            else:
                _mark(account, Status.COOLING, f"连接失败: {err}")
            logs.warn(req_id, f"账号 {account.name} 连接失败，切换下一个")
            return _NEXT_ACCOUNT

        status_code = resp.status_code
        preloaded: bytes | None = None
        business_code: int | None = None

        if status_code < 400:
            # 上游对业务错误也回 200：body 是 {"code":1005,"msg":"exceed quota limit"}。
            # 只看 content-type 就能分辨——SSE 是 text/event-stream，整体回复是
            # application/json（可能正常，也可能是这种错误体）。不读一次就分不清，
            # 于是这类请求会被当成成功透传出去、台账里也没有任何用量。
            content_type = (resp.headers.get("content-type") or "").lower()
            if "text/event-stream" not in content_type:
                preloaded = await resp.aread()
                business_code = _business_code(preloaded)
                if business_code not in (None, 0):
                    # 归一成 HTTP 语义走同一套错误分类（验证码/风控/额度/鉴权/其它）
                    status_code = 400

        if status_code >= 400:
            text = (preloaded.decode("utf-8", "ignore") if preloaded is not None
                    else (await resp.aread()).decode("utf-8", "ignore"))
            await cm.__aexit__(None, None, None)
            await client.aclose()

            # 模型级额度用尽（上游 200 + code 1005「exceed quota limit」）/ 模型并发
            # 已满（429 + code 3009「model concurrency limit exceeded」）：都是「这个
            # 账号对这个模型」的局部状态，账号本身仍然可用。实测同一账号 GLM-5.3 还有
            # 300 万、GLM-5.3-Flash 已用完——按 _is_exhausted 判成账号耗尽会把整号
            # 踢出轮询，其他模型跟着挂；而对 3009 原地等 60s 会让客户端几分钟收不到
            # 一个字节（2026-10-27 实测非流式请求因此吊死）。两种都只换号，不动状态。
            if (business_code == 1005 or "exceed quota limit" in text.lower()
                    or business_code == 3009 or "concurrency limit" in text.lower()):
                busy = business_code == 3009 or "concurrency limit" in text.lower()
                label = "并发已满" if busy else "额度用尽"
                account.record_result(False, f"模型{label}: {model_name}")
                logs.warn(
                    req_id,
                    f"账号 {account.name} 的 {model_name} {label}"
                    f"（上游 {business_code or status_code}），换下一个账号",
                )
                if reason is not None:
                    reason[0] = (f"{model_name} {label}"
                                 f"（上游 code {business_code or status_code} · 换号后仍无可用）")
                if not busy:
                    _spawn_bg(_safe_refresh(account))   # 额度变了，后台刷新余额
                return _NEXT_ACCOUNT

            # 验证码挑战：三形态任一命中即清池重试（不改账号状态）
            challenge = _detect_captcha_challenge(resp, text) if needs_captcha else None
            if challenge:
                captcha_manager.invalidate()
                captcha_retries += 1
                if captcha_retries >= MAX_CAPTCHA_RETRIES:
                    account.record_result(False, "验证码挑战连续失败")
                    logs.warn(req_id, f"账号 {account.name} 验证码连续失败，切换下一个")
                    return _NEXT_ACCOUNT
                logs.warn(req_id, f"账号 {account.name} 验证码挑战（{challenge}），刷新重试")
                continue  # 同账号重建请求重试

            # 风控（3012「unusual activity」/ 405）：指数退避冷却自动恢复，累计
            # RISK_BAN_STRIKES 次升级禁用（人工恢复）。不立即重试（避免对风控
            # 账号持续施压）；冷却期零上游流量（is_cooling 全通道门禁）。
            if _is_risk_control(status_code, text):
                account.record_result(False, f"风控 HTTP {status_code}（3012/unusual activity）")
                account.risk_penalty(
                    settings.RISK_COOLDOWN_BASE, settings.RISK_COOLDOWN_MAX,
                    settings.RISK_BAN_STRIKES, settings.RISK_STRIKE_DECAY_SECONDS,
                )
                if account.status == Status.DISABLED:
                    account.last_error = (
                        f"风控封禁 (3012/unusual activity) HTTP {status_code}，"
                        f"确认恢复后请在后台手动启用（第 {account.risk_strikes} 次）"
                    )
                    penalty_note = f"已升级禁用（第 {account.risk_strikes} 次）"
                    if settings.RISK_AUTO_ROTATE:
                        # 「风控后换设备重生」自动化：升级禁用即换发全新设备
                        # （新 SKU + 新 device_mid），后台补跑安装序；账号重新
                        # 启用时已是全新身份（billing/claim 头同源跟随档案）
                        from ..fingerprint import profile_for, rotate
                        from ..install import schedule_install

                        old_mid = profile_for(account).device_mid[:8]
                        rotate(account)
                        account.installed_at = None
                        schedule_install(account)
                        logs.warn(
                            req_id,
                            f"账号 {account.name} 风控禁用，已自动换发设备指纹"
                            f"（旧 mid {old_mid}）",
                        )
                else:
                    cool_until = time.strftime(
                        "%H:%M", time.localtime(account.cooling_until)
                    ) if account.cooling_until else "?"
                    account.last_error = (
                        f"命中风控 (3012/unusual activity) HTTP {status_code}，"
                        f"指数退避冷却至 {cool_until}（第 {account.risk_strikes} 次）"
                    )
                    penalty_note = f"冷却至 {cool_until}（第 {account.risk_strikes} 次）"
                store.update_account(account)
                if needs_captcha and account.has_apikey_fallback():
                    logs.warn(
                        req_id,
                        f"账号 {account.name} 命中风控 HTTP {status_code}，{penalty_note}，切 API Key 回退",
                    )
                    needs_captcha = False
                    force_fallback = True
                    continue
                logs.warn(
                    req_id,
                    f"账号 {account.name} 命中风控 HTTP {status_code}，{penalty_note}，切换下一个",
                )
                return _NEXT_ACCOUNT

            if _is_exhausted(status_code, text):
                account.record_result(False, f"额度用完 HTTP {status_code}")
                if account.status in (Status.INVALID, Status.DISABLED):
                    store.update_account(account)
                else:
                    _mark(account, Status.EXHAUSTED, "额度已用完")
                    _spawn_bg(_safe_refresh(account))
                logs.warn(req_id, f"账号 {account.name} 额度用完，切换下一个")
                return _NEXT_ACCOUNT

            if status_code == 401:
                account.record_result(False, "鉴权失败 HTTP 401")
                _mark(account, Status.INVALID, "鉴权失败 HTTP 401")
                if needs_captcha and account.has_apikey_fallback():
                    logs.warn(req_id, f"账号 {account.name} 鉴权失败 401，切 API Key 回退")
                    needs_captcha = False
                    force_fallback = True
                    continue
                logs.warn(req_id, f"账号 {account.name} 鉴权失败 401，切换下一个")
                return _NEXT_ACCOUNT

            if status_code == 403:
                # 403 已排除挑战形态（上方 challenge 分支），此处为真实鉴权拒绝
                account.record_result(False, "鉴权失败 HTTP 403")
                _mark(account, Status.INVALID, "鉴权失败 HTTP 403")
                if needs_captcha and account.has_apikey_fallback():
                    logs.warn(req_id, f"账号 {account.name} 鉴权失败 403，切 API Key 回退")
                    needs_captcha = False
                    force_fallback = True
                    continue
                logs.warn(req_id, f"账号 {account.name} 鉴权失败 403，切换下一个")
                return _NEXT_ACCOUNT

            if status_code == 429:
                # 1113「无余额/无资源包」是 Key 的永久状态（api.z.ai 实测秒回），
                # 原地等待重试毫无意义，只会把客户端吊死在 RETRY_429_TIMES×WAIT 里
                # ——快速换号/失败。
                if "1113" in text or "insufficient balance" in text.lower():
                    account.record_result(False, "429 1113 无余额（Key 无资源包，永久态）")
                    logs.warn(req_id, f"账号 {account.name} 429 为 1113 无余额，跳过等待重试")
                    if needs_captcha and account.has_apikey_fallback():
                        needs_captcha = False
                        force_fallback = True
                        retries_429 = 0
                        continue
                    return _NEXT_ACCOUNT
                # 频控不是账号故障：不冷却，原地等一等再试，耗尽后换号且账号保持可用。
                # Plan 通道耗尽 ≠ Key 回退也耗尽：同账号切回退并归还该通道的重试预算
                #（与上方 3012/401/403 切回退同一语义）
                if retries_429 < settings.RETRY_429_TIMES:
                    retries_429 += 1
                    wait = _parse_retry_after(resp.headers.get("retry-after")) or settings.RETRY_429_WAIT
                    logs.warn(
                        req_id,
                        f"账号 {account.name} 被限流 429，{wait}s 后重试"
                        f"（{retries_429}/{settings.RETRY_429_TIMES}）",
                    )
                    _park_slot(slot_box)
                    await _sleep(wait)
                    if not _reacquire_slot(account, slot_box):
                        logs.warn(req_id, f"账号 {account.name} 429 等待后并发已满，切换下一个")
                        return _NEXT_ACCOUNT
                    continue
                if needs_captcha and account.has_apikey_fallback():
                    account.record_result(False, "Plan 通道 429 耗尽，切 API Key 回退")
                    logs.warn(req_id, f"账号 {account.name} Plan 通道 429 耗尽，切 API Key 回退")
                    needs_captcha = False
                    force_fallback = True
                    retries_429 = 0
                    continue
                account.record_result(False, f"429 重试 {settings.RETRY_429_TIMES} 次耗尽")
                store.update_account(account)
                logs.warn(
                    req_id,
                    f"账号 {account.name} 429 重试 {settings.RETRY_429_TIMES} 次耗尽，"
                    f"切换下一个（账号保持可用）",
                )
                return _NEXT_ACCOUNT

            if status_code >= 500:
                # 一般性上游错误：重试，耗尽才冷却账号并换号
                if retries_5xx < settings.RETRY_5XX_TIMES:
                    retries_5xx += 1
                    logs.warn(
                        req_id,
                        f"账号 {account.name} 上游 HTTP {status_code}，"
                        f"{settings.RETRY_5XX_WAIT}s 后重试（{retries_5xx}/{settings.RETRY_5XX_TIMES}）",
                    )
                    _park_slot(slot_box)
                    await _sleep(settings.RETRY_5XX_WAIT)
                    if not _reacquire_slot(account, slot_box):
                        logs.warn(req_id, f"账号 {account.name} 5xx 等待后并发已满，切换下一个")
                        return _NEXT_ACCOUNT
                    continue
                account.record_result(False, f"HTTP {status_code} 重试 {settings.RETRY_5XX_TIMES} 次耗尽，冷却")
                if account.status in (Status.INVALID, Status.DISABLED) or account.is_cooling():
                    # Key 回退 5xx 不得覆盖废 JWT / 风控禁用，否则冷却结束会重开 Plan；
                    # 风控冷却同理（冷却期不覆盖，避免缩短风控退避）
                    store.update_account(account)
                    logs.warn(req_id, f"账号 {account.name} 上游 {status_code} 重试耗尽，Plan 已停用，切换下一个")
                else:
                    cool = settings.COOLING_SECONDS
                    account.status = Status.COOLING
                    account.cooling_until = time.time() + cool
                    account.last_error = f"上游 HTTP {status_code} 重试 {settings.RETRY_5XX_TIMES} 次耗尽，冷却"
                    store.update_account(account)
                    logs.warn(req_id, f"账号 {account.name} 上游 {status_code} 重试耗尽，冷却 {cool}s，切换下一个")
                return _NEXT_ACCOUNT

            # 其它 4xx：直接回传客户端；响应体全量落日志供排查
            # （错误 JSON 通常很小；防御性上限 4KB，超长按 HTML 类 WAF 页处理只留头部）
            account.fail_count += 1
            account.record_result(False, f"HTTP {status_code}: {text[:120]}".replace("\n", " "))
            store.update_account(account)
            logs.req_err(req_id, f"上游错误 HTTP {status_code}（账号 {account.name}）")
            body_log = text if len(text) <= 4000 else text[:4000] + f"...(共 {len(text)} 字节，疑似 WAF 页)"
            logs.warn(req_id, f"上游 {status_code} 完整响应体: {body_log}")
            reqlog.finish_error(req_id, f"HTTP {status_code}: {text[:120]}".replace("\n", " "),
                                status=status_code, t_first=time.time() - attempt_t0)
            return JSONResponse(
                _safe_json(text) or {"error": {"message": text[:500], "type": "upstream_error"}},
                status_code=status_code,
            )

        # 成功：记录用量并把打开的上游流交给调用方
        account.use_count += 1
        account.last_used_at = time.time()
        account.record_result(True, f"HTTP 200 · {model_name} · {time.time() - attempt_t0:.1f}s")
        # API Key 回退成功不得把废 JWT / 风控禁用洗成 active，也不得清风控计数；
        # 风控冷却未到期同样不洗（上游风控标记为小时级粘性，回退通道成功不代表
        # Plan 通道已解除，洗掉会立刻重打 Plan 并累加计数）。冷却已过期后的
        # Plan 通道成功即证明恢复，全量清理（清风控计数）。
        if account.status not in (Status.INVALID, Status.DISABLED) and not account.is_cooling():
            account.risk_strikes = 0
            account.last_risk_at = None
            account.last_error = None
            account.cooling_until = None
            if account.status in (Status.COOLING, Status.EXHAUSTED):
                account.status = Status.ACTIVE
        store.update_account(account)
        _spawn_bg(_safe_refresh(account))

        return _Upstream(resp, cm, client, t_first=time.time() - attempt_t0,
                         account_name=account.name, mode=account.mode,
                         preloaded=preloaded)


def _safe_json(text: str):
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


def _business_code(raw: bytes) -> int | None:
    """从整体响应体里取上游业务码（`{"code":1005,...}`），取不到返回 None。

    zcode-plan 的业务错误走 HTTP 200 + body 里的 code：1005=额度用尽、
    3007=验证码、1001=参数……不认这个 code，失败就会被当成成功。
    """
    data = _safe_json(raw.decode("utf-8", "ignore"))
    if not isinstance(data, dict):
        return None
    code = data.get("code")
    return code if isinstance(code, int) else None


async def _safe_refresh(account: Account) -> None:
    try:
        live = store.find(account.provider, account.id)
        if live is None:
            return
        if live.provider == "zai" and live.allows_billing():
            # 去抖：每条消息都刷 billing 是流量放大器（会加剧风控），与 monitor 共享
            # last_checked_at，最小间隔内的刷新直接跳过
            last = live.last_checked_at
            if last and time.time() - last < settings.BILLING_REFRESH_MIN_INTERVAL:
                return
            await fetch_quota(live)
    except Exception:  # noqa: BLE001
        pass
