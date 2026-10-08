"""时光机接口客户端 —— 请求封装、双 token 模式协商、错误码翻译

主题端（Handsome）暴露两个 action：
  - send_talk   发说说
  - upload_img  传图片，返回 {"status":"1","data":"<图片URL>"}

返回码（来自主题）：1 成功 / -1 参数错误 / -2 信息缺失 / -3 身份验证失败
"""

import asyncio
import hashlib
import json
import logging
from typing import Callable

import aiohttp

logger = logging.getLogger("astrbot")

# ── 协议常量 ──────────────────────────────────────────────

# token=weixin 模式下 time_code 两侧的盐，硬编码自 wechat_for_handsome/cross.php:42
WEIXIN_SALT_PREFIX = "handsome!@#$%^&*()-=+@#$%$"
WEIXIN_SALT_SUFFIX = "handsome!@#$%^&*()-=+@#$%$@#$%^&*"

MODE_CRX = "crx"
MODE_WEIXIN = "weixin"
MODE_AUTO = "auto"
MODES = (MODE_CRX, MODE_WEIXIN)

# 同一个编码在不同客户端会算成不同的哈希，主题端按 token 分流校验：
#   crx    → md5(编码)            （Chrome 插件）
#   weixin → md5(盐 + 编码 + 盐)   （微信服务端；主题里 Utils::md5() 就是这个实现）
HASH_PLAIN = "plain"
HASH_SALTED = "salted"

# 各 token 默认搭配的哈希（照两份参考实现）
MODE_HASH = {MODE_CRX: HASH_PLAIN, MODE_WEIXIN: HASH_SALTED}

# 探测用：token × 哈希 的全部组合
PROBE_COMBINATIONS = tuple(
    (mode, kind) for mode in MODES for kind in (HASH_PLAIN, HASH_SALTED)
)

CODE_OK = "1"
CODE_AUTH_FAILED = "-3"

ERROR_MAP = {
    "-1": "请求参数错误（参数名或格式和主题版本对不上）",
    "-2": "信息缺失（cid / content 等必填项没传到位）",
    "-3": "身份编码错误（验证编码不对，或 token 模式不匹配）",
}

# 探测用的 1×1 透明 PNG。用它试探校验路径，比发一条空说说干净。
PROBE_PNG_DATA_URL = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


class TimeMachineError(Exception):
    """接口调用失败。带上原始响应，方便在聊天里直接看到主题回了什么。"""

    def __init__(self, message: str, raw: str = "") -> None:
        super().__init__(message)
        self.raw = raw

    def detail(self) -> str:
        return f"{self}\n原始响应：{self.raw}" if self.raw else str(self)


# ── 纯函数（可离线单测）────────────────────────────────────


def time_code_crx(code: str) -> str:
    """Chrome 插件算法：裸 md5（time_machine/js/oper.js:283）"""
    return hashlib.md5(code.encode("utf-8")).hexdigest()


def time_code_weixin(code: str) -> str:
    """微信服务端算法：两侧加盐（wechat_for_handsome/cross.php:42）"""
    return hashlib.md5(
        f"{WEIXIN_SALT_PREFIX}{code}{WEIXIN_SALT_SUFFIX}".encode("utf-8")
    ).hexdigest()


def make_time_code(code: str, hash_kind: str = HASH_PLAIN) -> str:
    """按哈希算法算 time_code。hash_kind ∈ {HASH_PLAIN, HASH_SALTED}"""
    return time_code_weixin(code) if hash_kind == HASH_SALTED else time_code_crx(code)


def describe_code(raw: str) -> str:
    """把主题返回码翻译成人话"""
    raw = (raw or "").strip()
    if raw == CODE_OK:
        return "发送成功"
    return ERROR_MAP.get(raw, f"主题返回了没见过的内容：{raw[:200] or '(空)'}")


def parse_upload_response(raw: str) -> tuple[bool, str, str]:
    """解析 upload_img 的响应，返回 (是否成功, 图片地址, 失败原因)"""
    try:
        obj = json.loads(raw)
    except ValueError:
        return False, "", f"上传返回的不是 JSON：{raw[:200] or '(空)'}"
    if not isinstance(obj, dict):
        return False, "", f"上传返回的结构异常：{raw[:200]}"
    status = str(obj.get("status", ""))
    if status == CODE_OK:
        url = str(obj.get("data") or "")
        if not url:
            return False, "", "上传返回成功，但里面没有图片地址"
        return True, url, ""
    return False, "", describe_code(status)


def is_auth_failure(raw: str) -> bool:
    """判断响应是不是「身份校验没过」。上传接口返回 JSON，发送接口返回裸字符串。"""
    if (raw or "").strip() == CODE_AUTH_FAILED:
        return True
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        return False
    return isinstance(obj, dict) and str(obj.get("status", "")) == CODE_AUTH_FAILED


# ── 客户端 ────────────────────────────────────────────────


class TimeMachineClient:
    """对主题 send_talk / upload_img 的封装。

    token 模式（crx / weixin）决定 time_code 的算法，主题端按 token 分流校验。
    配置成 auto 时会先试 crx，拿到 -3 再试 weixin —— -3 表示校验没过、内容根本没入库，
    所以这种试探不会往博客里留下垃圾内容。
    """

    def __init__(
        self,
        blog_url: str = "",
        timecode: str = "",
        cid: int | str = 0,
        token_mode: str = MODE_AUTO,
        proxy: str = "",
        verify_ssl: bool = True,
        timeout: int = 60,
        detected_mode: str = "",
        on_mode_detected: Callable[[str], None] | None = None,
    ) -> None:
        self.blog_url = (blog_url or "").strip()
        self.timecode = timecode or ""
        self.cid = cid
        self.mode_config = token_mode if token_mode in MODES else MODE_AUTO
        self.detected_mode = detected_mode if detected_mode in MODES else ""
        self.proxy = (proxy or "").strip() or None
        self.verify_ssl = verify_ssl
        self.timeout = timeout
        self.on_mode_detected = on_mode_detected
        self._session: aiohttp.ClientSession | None = None

    # ── 配置自检 ──────────────────────────────────────────

    @property
    def configured(self) -> bool:
        return bool(self.blog_url and self.timecode and self.cid)

    def missing_fields(self) -> list[str]:
        missing = []
        if not self.blog_url:
            missing.append("博客地址")
        if not self.timecode:
            missing.append("时光机验证编码")
        if not self.cid:
            missing.append("时光机 cid")
        return missing

    def mode_label(self) -> str:
        if self.mode_config in MODES:
            return f"{self.mode_config}（配置锁定）"
        if self.detected_mode:
            return f"{self.detected_mode}（自动协商结果）"
        return "auto（首次发送时自动协商）"

    # ── 会话 ──────────────────────────────────────────────

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                trust_env=False,
                connector=aiohttp.TCPConnector(
                    ssl=False if not self.verify_ssl else None
                ),
                timeout=aiohttp.ClientTimeout(total=self.timeout, connect=15),
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                },
            )
        return self._session

    @property
    def has_session(self) -> bool:
        """会话是懒创建的：没发过请求就没有东西要关"""
        return self._session is not None and not self._session.closed

    async def close(self) -> None:
        if self.has_session:
            await self._session.close()
        self._session = None

    # ── 模式协商 ──────────────────────────────────────────

    def _candidate_modes(self) -> list[str]:
        if self.mode_config in MODES:
            return [self.mode_config]  # 用户锁定了模式
        if self.detected_mode:
            return [self.detected_mode]  # 之前协商成功过，直接用
        return [MODE_CRX, MODE_WEIXIN]  # auto：先试 Chrome 插件那条已验证的路

    def _remember_mode(self, mode: str) -> None:
        if self.mode_config in MODES or self.detected_mode == mode:
            return
        self.detected_mode = mode
        logger.info(f"🔑 时光机 token 模式确定为 {mode}")
        if self.on_mode_detected:
            try:
                self.on_mode_detected(mode)
            except Exception as e:
                logger.warning(f"⚠️ 保存 token 模式失败：{type(e).__name__}: {e}")

    # ── 请求 ──────────────────────────────────────────────

    async def _post(self, params: dict) -> str:
        if not self.configured:
            raise TimeMachineError(
                "插件还没配置好，缺少：" + "、".join(self.missing_fields())
            )
        status = 0
        try:
            async with self._get_session().post(
                self.blog_url, data=params, proxy=self.proxy
            ) as resp:
                status = resp.status
                text = await resp.text()
        except asyncio.TimeoutError as e:
            raise TimeMachineError(f"请求超时（{self.timeout}s），博客没有响应") from e
        except aiohttp.ClientError as e:
            raise TimeMachineError(f"连接博客失败：{type(e).__name__}: {e}") from e
        if status != 200:
            raise TimeMachineError(f"博客返回 HTTP {status}", text[:300])
        return text

    def _talk_params(
        self, mode: str, content: str, msg_type: str, hash_kind: str | None = None
    ) -> dict:
        return {
            "action": "send_talk",
            "time_code": make_time_code(self.timecode, hash_kind or MODE_HASH.get(mode, HASH_PLAIN)),
            "token": mode,
            "cid": str(self.cid),
            "content": content,
            "msg_type": msg_type,
            "mediaId": "1",
        }

    def _upload_params(
        self, mode: str, file_data: str, suffix: str, hash_kind: str | None = None
    ) -> dict:
        return {
            "action": "upload_img",
            "time_code": make_time_code(self.timecode, hash_kind or MODE_HASH.get(mode, HASH_PLAIN)),
            "token": mode,
            "file": file_data,
            "type": suffix,
            "mediaId": "1",
        }

    # ── 对外接口 ──────────────────────────────────────────

    async def send_talk(self, content: str, msg_type: str = "text") -> str:
        """发一条说说。成功返回 "1"，失败抛 TimeMachineError。"""
        modes = self._candidate_modes()
        raw = ""
        for idx, mode in enumerate(modes):
            raw = (await self._post(self._talk_params(mode, content, msg_type))).strip()
            if raw == CODE_OK:
                self._remember_mode(mode)
                return raw
            if raw == CODE_AUTH_FAILED and idx + 1 < len(modes):
                logger.info(f"🔑 token 模式 {mode} 校验没过，换 {modes[idx + 1]} 再试")
                continue
            break
        raise TimeMachineError(describe_code(raw), raw)

    async def upload_image(self, file_data: str, suffix: str = ".jpg") -> str:
        """上传一张图片，返回图片 URL。

        file_data 可以是 base64 dataURL，也可以是图片直链（主题两种都认）。
        """
        modes = self._candidate_modes()
        raw = ""
        last_err = "上传失败"
        for idx, mode in enumerate(modes):
            raw = (await self._post(self._upload_params(mode, file_data, suffix))).strip()
            ok, url, err = parse_upload_response(raw)
            if ok:
                self._remember_mode(mode)
                return url
            last_err = err
            if not is_auth_failure(raw) or idx + 1 >= len(modes):
                break
            logger.info(f"🔑 token 模式 {mode} 校验没过，换 {modes[idx + 1]} 再试")
        raise TimeMachineError(last_err, raw)

    async def probe(self) -> list[tuple[str, str, bool, str]]:
        """把 token × 哈希 的四种组合各试一遍，返回 [(token, 哈希, 是否通过, 说明)]。

        用 upload_img 传一张 1×1 的空白图探路：不会有说说产生，
        最坏只是在博客 uploads 里留一张 1 像素的图片。
        """
        results: list[tuple[str, str, bool, str]] = []
        for mode, hash_kind in PROBE_COMBINATIONS:
            try:
                raw = (
                    await self._post(
                        self._upload_params(mode, PROBE_PNG_DATA_URL, ".png", hash_kind)
                    )
                ).strip()
            except TimeMachineError as e:
                results.append((mode, hash_kind, False, str(e)))
                continue

            ok, url, err = parse_upload_response(raw)
            if ok:
                # 只有和插件支持的某个模式对得上的组合才值得缓存
                if MODE_HASH.get(mode) == hash_kind:
                    self._remember_mode(mode)
                results.append((mode, hash_kind, True, f"通过（探测图：{url}）"))
            elif is_auth_failure(raw):
                results.append((mode, hash_kind, False, "身份校验失败（-3）"))
            else:
                # 不是 -3，说明身份这关过了，问题出在别的参数上
                if MODE_HASH.get(mode) == hash_kind:
                    self._remember_mode(mode)
                results.append(
                    (mode, hash_kind, True, f"身份校验通过，但接口另有问题：{err}")
                )
        return results
