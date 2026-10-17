"""
派为资讯发布

将群内指令内容通过各平台提供的 HTTP 发布接口发送出去。
支持：模板发布、平台发布、请求预览、动态字段、多平台配置、
Bearer / 自定义 Header / MD5 签名鉴权。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from html import escape
from html.parser import HTMLParser
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import Any

import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

try:
    from astrbot.api.web import error_response, json_response, request

    WEB_API_AVAILABLE = True
except ImportError:  # 兼容不支持插件视图 API 的旧版本
    error_response = json_response = request = None
    WEB_API_AVAILABLE = False

PLUGIN_NAME = "astrbot_plugin_publish_bridge"
PLUGIN_VERSION = "1.4.8"

# 指令前缀（按长度倒序匹配，兼顾 wake_prefix 是否已被剥离）
COMMAND_PREFIXES = (
    "/publish_preview",
    "/publish_list",
    "/publish",
    "/发布预览",
    "/发布列表",
    "/发布帮助",
    "/发布",
    "publish_preview",
    "publish_list",
    "publish",
    "发布预览",
    "发布列表",
    "发布帮助",
    "发布",
)

RESPONSE_PREVIEW_LIMIT = 300
HELP_TOKENS = {"help", "帮助", "?", "？"}

# 动态参数中文别名
FIELD_ALIASES = {
    "价格": "price",
    "单价": "price",
    "数量": "amount",
    "库存": "amount",
    "单位": "unit",
    "品牌": "brand",
    "公司": "company",
    "企业": "company",
    "联系人": "truename",
    "姓名": "truename",
    "手机": "mobile",
    "电话": "mobile",
    "标签": "tag",
    "天数": "days",
    "有效期": "days",
    "分类": "catid",
    "地址": "address",
    "邮箱": "email",
}

# 各 action 的默认必填字段（相对 data）
REQUIRED_BY_ACTION = {
    "article": ("title", "content"),
    "product": ("title", "content", "price"),
    "demand": ("title", "content"),
}

# 内置固定模板：资讯 / 产品 / 需求
# platform 留空表示使用 default_platform，未配置时使用第一个平台
BUILTIN_TEMPLATES = (
    {
        "name": "资讯",
        "platform": "",
        "action": "article",
        "catid": 44730,
        "title_field": "data.title",
        "content_field": "data.content",
        "html_content": True,
        "fixed_fields": "{}",
        "required_fields": "[]",
        "description": "发布一条资讯",
    },
    {
        "name": "产品",
        "platform": "",
        "action": "product",
        "catid": 44632,
        "title_field": "data.title",
        "content_field": "data.content",
        "html_content": True,
        "fixed_fields": '{"unit":"件"}',
        "required_fields": "[]",
        "description": "发布产品信息（需价格）",
    },
    {
        "name": "需求",
        "platform": "",
        "action": "demand",
        "catid": 44508,
        "title_field": "data.title",
        "content_field": "data.content",
        "html_content": True,
        "fixed_fields": "{}",
        "required_fields": "[]",
        "description": "发布需求信息",
    },
)

# 抓取链接正文时使用的浏览器 UA
LINK_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0 Safari/537.36"
)


class _ArticleHTMLParser(HTMLParser):
    """轻量文章提取器：优先收集 <p>/标题文本，跳过脚本和导航。"""

    SKIP_TAGS = {
        "script",
        "style",
        "noscript",
        "svg",
        "iframe",
        "nav",
        "header",
        "footer",
        "aside",
        "form",
    }
    PARA_TAGS = {"p", "h1", "h2", "h3", "h4", "h5", "h6"}
    BLOCK_TAGS = PARA_TAGS | {"div", "br", "li", "tr", "section", "article"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip_depth = 0
        self.para_depth = 0
        self.para_buf: list[str] = []
        self.paragraphs: list[str] = []
        self.all_buf: list[str] = []
        self.title_buf: list[str] = []
        self.in_title = False
        self.meta_description = ""

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == "meta":
            attrs_dict = {key.lower(): (value or "") for key, value in attrs}
            if attrs_dict.get("name", "").lower() == "description":
                self.meta_description = attrs_dict.get("content", "").strip()
            return
        if tag in self.SKIP_TAGS:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag == "title":
            self.in_title = True
        if tag in self.PARA_TAGS:
            self.para_depth += 1
            self.para_buf = []
        if tag in self.BLOCK_TAGS:
            self.all_buf.append("\n")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in self.SKIP_TAGS:
            if self.skip_depth:
                self.skip_depth -= 1
            return
        if self.skip_depth:
            return
        if tag == "title":
            self.in_title = False
        if tag in self.PARA_TAGS:
            self._flush_para()
            self.para_depth = max(0, self.para_depth - 1)
        if tag in self.BLOCK_TAGS:
            self.all_buf.append("\n")

    def handle_data(self, data):
        if self.in_title:
            self.title_buf.append(data)
            return
        if self.skip_depth:
            return
        text = data.strip()
        if not text:
            return
        if self.para_depth:
            self.para_buf.append(text)
        self.all_buf.append(text)

    def _flush_para(self):
        if self.para_buf:
            text = re.sub(r"\s+", " ", " ".join(self.para_buf)).strip()
            if text:
                self.paragraphs.append(text)
            self.para_buf = []

    def close(self):
        super().close()
        self._flush_para()


class PublishBridgePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._register_web_apis(context)

    def _register_web_apis(self, context: Context) -> None:
        if not WEB_API_AVAILABLE:
            logger.warning(
                "[publish-bridge] 当前 AstrBot 不支持插件视图 API，发布面板不可用"
            )
            return
        register = getattr(context, "register_web_api", None)
        if not callable(register):
            logger.warning(
                "[publish-bridge] 当前 AstrBot 缺少 register_web_api，发布面板不可用"
            )
            return
        register(
            f"/{PLUGIN_NAME}/overview",
            self.api_overview,
            ["GET"],
            "发布面板概览",
        )
        register(
            f"/{PLUGIN_NAME}/preview",
            self.api_preview,
            ["POST"],
            "预览发布请求",
        )
        register(
            f"/{PLUGIN_NAME}/publish",
            self.api_publish,
            ["POST"],
            "执行发布",
        )

    # ------------------------------------------------------------------
    # 指令入口
    # ------------------------------------------------------------------
    @filter.command("发布", alias={"publish", "Publish"})
    async def publish_cmd(self, event: AstrMessageEvent):
        """发布内容（模板或平台模式）。"""
        async for item in self._handle_publish(event, preview=False):
            yield item

    @filter.command("发布预览", alias={"publish_preview"})
    async def preview_cmd(self, event: AstrMessageEvent):
        """只渲染发布请求，不真正发送。"""
        async for item in self._handle_publish(event, preview=True):
            yield item

    @filter.command("发布列表", alias={"publish_list"})
    async def list_cmd(self, event: AstrMessageEvent):
        """列出可用模板和平台。"""
        if not self.config.get("enabled", True):
            return
        access = self._access_error(event)
        if access:
            yield event.plain_result(access)
            return
        yield event.plain_result(self._list_text())

    @filter.command("发布帮助", alias={"publish_help", "PublishHelp"})
    async def help_cmd(self, event: AstrMessageEvent):
        """显示发布指令的用法和示例。"""
        if not self.config.get("enabled", True):
            return
        access = self._access_error(event)
        if access:
            yield event.plain_result(access)
            return
        yield event.plain_result(self._help_text())

    @filter.command("诊断", alias={"publish_diag", "发布诊断"})
    async def diag_cmd(self, event: AstrMessageEvent):
        """诊断插件视图与运行环境（不受管理员限制）。"""
        if not self.config.get("enabled", True):
            return
        astrbot_version = "unknown"
        for package_name in ("astrbot", "AstrBot"):
            try:
                astrbot_version = _pkg_version(package_name)
                break
            except Exception:
                continue
        base = Path(__file__).resolve().parent
        view_index = base / "views" / "publish" / "index.html"
        page_index = base / "pages" / "publish" / "index.html"
        sender_id = self._sender_id(event)
        whitelist = self._admin_whitelist()
        raw_whitelist = self.config.get("admin_users", "")
        event_role = str(getattr(event, "role", "") or "")
        diag_sender = getattr(getattr(event, "message_obj", None), "sender", None)
        sender_role = str(getattr(diag_sender, "role", "") or "")
        try:
            is_admin_value = bool(event.is_admin())
        except Exception:
            is_admin_value = False
        lines = [
            "🔧 发布桥诊断",
            f"插件版本: {PLUGIN_VERSION}",
            f"AstrBot 版本: {astrbot_version}",
            f"WEB_API_AVAILABLE: {WEB_API_AVAILABLE}",
            "register_web_api: "
            f"{callable(getattr(self.context, 'register_web_api', None))}",
            f"views/publish/index.html: {'存在' if view_index.exists() else '缺失'}",
            f"pages/publish/index.html: {'存在' if page_index.exists() else '缺失'}",
            f"平台数: {len(self._platforms())}",
            f"仅管理员可用配置: {self.config.get('admin_only', True)!r}",
            f"严格白名单模式: {self.config.get('strict_admin', False)!r}",
            "内置模板: "
            f"{'、'.join(str(t.get('name')) for t in self._templates()) or '无'}",
            f"管理员白名单类型: {type(raw_whitelist).__name__}",
            f"管理员白名单条目数: {len(whitelist)}",
            f"发送者 ID: {sender_id or '未获取到'}",
            f"event.role: {event_role or '空'}",
            f"sender.role: {sender_role or '空'}",
            f"is_admin(): {is_admin_value}",
            f"白名单命中: {sender_id.lower() in whitelist if sender_id else False}",
            f"最终判定为管理员: {self._is_admin(event)}",
        ]
        if not self._is_admin(event):
            lines.append(
                "提示: 你的角色未被识别为管理员，"
                "请把上面的「发送者 ID」加入管理员白名单。"
            )
        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------------
    # WebUI 发布面板 API
    # ------------------------------------------------------------------
    async def api_overview(self):
        return json_response(
            {
                "plugin": PLUGIN_NAME,
                "version": PLUGIN_VERSION,
                "default_platform": str(
                    self.config.get("default_platform", "") or ""
                ),
                "platforms": [
                    {
                        "name": str(p.get("name", "")),
                        "url": str(p.get("url", "")),
                        "method": str(p.get("method", "POST")),
                        "auth_type": str(p.get("auth_type", "none")),
                    }
                    for p in self._platforms()
                ],
                "templates": [
                    {
                        "name": str(t.get("name", "")),
                        "platform": self._template_platform_name(t),
                        "action": str(t.get("action", "")),
                        "catid": t.get("catid"),
                        "required": self._required_fields(t),
                        "description": str(t.get("description", "") or ""),
                    }
                    for t in self._templates()
                ],
            }
        )

    async def api_preview(self):
        return await self._api_publish(preview=True)

    async def api_publish(self):
        return await self._api_publish(preview=False)

    async def _api_publish(self, preview: bool):
        try:
            payload = await request.json(default={})
        except Exception:
            return error_response("请求体必须是 JSON", 400)
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象", 400)

        target = str(payload.get("target", "") or "").strip()
        title = str(payload.get("title", "") or "").strip()
        body = str(payload.get("body", "") or "").strip()
        params_raw = str(payload.get("params", "") or "").strip()

        platform, content, error = await self._build_web_action(
            target, title, body, params_raw
        )
        if error:
            return error_response(error, 400)

        if preview:
            return json_response({"preview": self._preview(platform, content)})
        result = await self._publish(platform, content)
        return json_response({"result": result})

    async def _build_web_action(
        self, target: str, title: str, body: str, params_raw: str
    ) -> tuple[dict[str, Any] | None, str | None, str | None]:
        if not target:
            return None, None, "请选择模板或平台"
        if not title:
            return None, None, "标题不能为空"
        if len(title) < 3:
            return None, None, "标题至少需要 3 个字符"
        if not body:
            return None, None, "正文不能为空"

        template = self._find_template(target)
        if template is not None:
            platform = self._find_platform(self._template_platform_name(template))
            if platform is None:
                return (
                    None,
                    None,
                    f"模板引用的平台「{self._template_platform_name(template)}」不存在",
                )
            try:
                params = self._parse_params(params_raw)
            except ValueError as exc:
                return None, None, f"参数错误：{exc}"
            synthetic, data, error = self._build_template_platform(
                template, platform, params
            )
            if error:
                return None, None, error
            error = self._validate_template(template, title, body, data)
            if error:
                return None, None, error
            use_html = bool(template.get("html_content", True))
            body, error = await self._maybe_fetch_link(body, use_html)
            if error:
                return None, None, error
            synthetic["__title__"] = title
            synthetic["__body__"] = body
            return synthetic, title, None

        platform = self._find_platform(target)
        if platform is None:
            return None, None, f"未找到模板或平台「{target}」"
        use_html = bool(platform.get("html_content", False))
        body, error = await self._maybe_fetch_link(body, use_html)
        if error:
            return None, None, error
        if str(platform.get("title_field", "") or "").strip():
            platform = dict(platform)
            platform["__title__"] = title
            platform["__body__"] = body
            return platform, title, None
        return platform, body, None

    async def _maybe_fetch_link(
        self, body: str, use_html: bool
    ) -> tuple[str | None, str | None]:
        if not (
            self.config.get("auto_fetch_link", True) and self._is_pure_url(body)
        ):
            return body, None
        _page_title, extracted, error = await self._fetch_article_text(body)
        if error:
            return None, f"链接正文抓取失败：{error}"
        return self._text_to_html(extracted) if use_html else extracted, None

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------
    async def _handle_publish(self, event: AstrMessageEvent, preview: bool):
        if not self.config.get("enabled", True):
            return

        access = self._access_error(event)
        if access:
            yield event.plain_result(access)
            return

        raw = self._strip_command(event.get_message_str())
        if not raw or raw.lower() in HELP_TOKENS:
            yield event.plain_result(self._help_text())
            return

        resolved = self._resolve_target(raw)
        if resolved is None:
            yield event.plain_result(
                f"❌ 未找到模板或平台「{self._first_token(raw)}」。\n{self._short_usage()}"
            )
            return

        if resolved["kind"] == "template":
            yield event.plain_result(
                await self._handle_template(
                    resolved["template"], resolved["rest"], preview
                )
            )
            return

        platform: dict[str, Any] = resolved["platform"]
        content: str = resolved["rest"]
        if not content:
            yield event.plain_result("❌ 发布内容不能为空。")
            return

        # 纯链接：抓取链接正文后再发布
        if self.config.get("auto_fetch_link", True):
            title, body = self._split_title(content)
            if self._is_pure_url(body):
                page_title, extracted, error = await self._fetch_article_text(body)
                if error:
                    yield event.plain_result(
                        f"❌ 链接正文抓取失败：{error}\n"
                        "可关闭「纯链接自动抓取正文」后直接发布链接。"
                    )
                    return
                use_html = bool(platform.get("html_content", False))
                extracted_body = (
                    self._text_to_html(extracted) if use_html else extracted
                )
                if not title or self._is_pure_url(title):
                    title = page_title or "链接文章"
                if str(platform.get("title_field", "") or "").strip():
                    platform = dict(platform)
                    platform["__title__"] = title
                    platform["__body__"] = extracted_body
                    content = title
                else:
                    content = extracted_body

        if preview:
            yield event.plain_result(self._preview(platform, content))
        else:
            yield event.plain_result(await self._publish(platform, content))

    async def _handle_template(
        self, template: dict[str, Any], rest: str, preview: bool
    ) -> str:
        platform = self._find_platform(self._template_platform_name(template))
        if platform is None:
            return (
                f"❌ 模板「{template.get('name')}」引用的平台"
                f"「{self._template_platform_name(template)}」不存在。"
            )

        try:
            parsed = self._parse_publish_args(rest)
        except ValueError as exc:
            return f"❌ 参数错误：{exc}"

        # 纯链接：抓取链接正文后再发布
        if self.config.get("auto_fetch_link", True) and self._is_pure_url(
            parsed["body"]
        ):
            _page_title, extracted, error = await self._fetch_article_text(
                parsed["body"]
            )
            if error:
                return (
                    f"❌ 链接正文抓取失败：{error}\n"
                    "可关闭「纯链接自动抓取正文」后直接发布链接。"
                )
            use_html = bool(template.get("html_content", True))
            parsed["body"] = self._text_to_html(extracted) if use_html else extracted

        synthetic, data, error = self._build_template_platform(
            template, platform, parsed["params"]
        )
        if error:
            return error

        error = self._validate_template(
            template, parsed["title"], parsed["body"], data
        )
        if error:
            return error

        synthetic["__title__"] = parsed["title"]
        synthetic["__body__"] = parsed["body"]

        if preview:
            return self._preview(synthetic, parsed["title"])

        return await self._publish(synthetic, parsed["title"])

    # ------------------------------------------------------------------
    # 权限与解析
    # ------------------------------------------------------------------
    def _access_error(self, event: AstrMessageEvent) -> str | None:
        if self.config.get("group_only", True) and not event.get_group_id():
            return "该指令仅支持在群聊中使用。"
        if self.config.get("admin_only", True) and not self._is_admin(event):
            logger.info(
                f"[publish-bridge] 拒绝非管理员: sender={self._sender_id(event)}"
            )
            return (
                "❌ 该指令仅管理员可用。\n"
                "可在插件配置的「管理员白名单」填入你的 QQ 号，"
                "或关闭「仅管理员可用」。"
            )
        return None

    @staticmethod
    def _strip_command(message: str) -> str:
        """去掉消息开头的指令前缀，保留正文（含换行）。"""
        text = (message or "").strip()
        lowered = text.lower()
        for prefix in sorted(COMMAND_PREFIXES, key=len, reverse=True):
            if lowered.startswith(prefix.lower()):
                text = text[len(prefix):].strip()
                break
        if text[:1] in {":", "："}:
            text = text[1:].strip()
        return text

    @staticmethod
    def _first_token(text: str) -> str:
        parts = (text or "").split(maxsplit=1)
        return parts[0] if parts else ""

    def _resolve_target(self, raw: str) -> dict[str, Any] | None:
        """解析首个词：模板优先，其次平台，最后回退默认平台。"""
        parts = re.split(r"\s+", raw.strip(), maxsplit=1)
        first = parts[0] if parts else ""
        if not first:
            return None
        rest = parts[1].strip() if len(parts) > 1 else ""

        template = self._find_template(first)
        if template is not None:
            return {
                "kind": "template",
                "template": template,
                "rest": rest,
                "target": first,
            }

        platform = self._find_platform(first)
        if platform is not None:
            return {
                "kind": "platform",
                "platform": platform,
                "rest": rest,
                "target": first,
            }

        default_name = str(self.config.get("default_platform", "") or "").strip()
        platform = self._find_platform(default_name) if default_name else None
        if platform is not None:
            return {
                "kind": "platform",
                "platform": platform,
                "rest": raw.strip(),
                "target": default_name,
            }
        return None

    # ------------------------------------------------------------------
    # 模板解析
    # ------------------------------------------------------------------
    def _templates(self) -> list[dict[str, Any]]:
        """内置固定模板：资讯 / 产品 / 需求。"""
        return [dict(template) for template in BUILTIN_TEMPLATES]

    def _template_platform_name(self, template: dict[str, Any]) -> str:
        """模板未指定平台时，用 default_platform 或第一个平台。"""
        name = str(template.get("platform") or "").strip()
        if name:
            return name
        default = str(self.config.get("default_platform") or "").strip()
        if default:
            return default
        platforms = self._platforms()
        if platforms:
            return str(platforms[0].get("name", "")).strip()
        return ""

    def _find_template(self, name: str) -> dict[str, Any] | None:
        name = (name or "").strip()
        if not name:
            return None
        for template in self._templates():
            if str(template.get("name") or "").strip() == name:
                return template
        return None

    def _build_template_platform(
        self,
        template: dict[str, Any],
        platform: dict[str, Any],
        params: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str | None]:
        """把模板 + 动态参数渲染成平台请求配置，并返回 data 供校验。"""
        action = str(template.get("action") or "").strip()
        try:
            data = self._parse_json_object(
                template.get("fixed_fields"), "模板固定字段"
            )
        except ValueError as exc:
            return (
                None,
                None,
                f"❌ 模板「{template.get('name')}」配置错误：{exc}",
            )

        catid = template.get("catid")
        if catid not in (None, "", 0, "0"):
            data["catid"] = catid

        for key, value in params.items():
            key = key[5:] if key.startswith("data.") else key
            self._set_dotted(data, key, value)

        if self._get_dotted(data, "catid") in (None, "", 0, "0"):
            return None, None, "❌ 模板缺少 catid，请在模板配置或指令参数中提供。"

        synthetic = dict(platform)
        synthetic["extra_body"] = json.dumps(
            {"action": action, "data": data}, ensure_ascii=False
        )
        synthetic["content_field"] = template.get("content_field") or "data.content"
        synthetic["title_field"] = template.get("title_field") or "data.title"
        synthetic["html_content"] = bool(template.get("html_content", True))
        return synthetic, data, None

    def _validate_template(
        self,
        template: dict[str, Any],
        title: str,
        body: str,
        data: dict[str, Any],
    ) -> str | None:
        if not title:
            return "❌ 标题不能为空，请使用「标题 | 正文」格式。"
        if len(title) < 3:
            return "❌ 标题至少需要 3 个字符，请使用「标题 | 正文」格式。"
        if not body:
            return "❌ 正文不能为空，请使用「标题 | 正文」格式。"

        required = self._required_fields(template)
        missing = []
        for field in required:
            if field == "title":
                value = title
            elif field in {"content", "body"}:
                value = body
            else:
                value = self._get_dotted(data, field)
            if value in (None, ""):
                missing.append(field)
        if missing:
            return "❌ 缺少必填字段：" + "、".join(missing)

        action = str(template.get("action") or "").strip()
        if action == "product":
            price = self._get_dotted(data, "price")
            try:
                price_value = float(price)
            except (TypeError, ValueError):
                return "❌ 产品模板要求提供 price（价格），例如：| price=9.9"
            if price_value < 0.1:
                return "❌ 产品价格 price 必须 ≥ 0.1。"
        return None

    @staticmethod
    def _required_fields(template: dict[str, Any]) -> list[str]:
        required = template.get("required_fields")
        if isinstance(required, str):
            text = required.strip()
            if text:
                try:
                    required = json.loads(text)
                except json.JSONDecodeError:
                    required = None
            else:
                required = None
        if not isinstance(required, list) or not required:
            required = list(
                REQUIRED_BY_ACTION.get(
                    str(template.get("action") or "").strip(),
                    ("title", "content"),
                )
            )
        return [str(field).replace("data.", "", 1) for field in required]

    # ------------------------------------------------------------------
    # 指令参数解析
    # ------------------------------------------------------------------
    @classmethod
    def _parse_publish_args(cls, raw: str) -> dict[str, Any]:
        """解析「标题 | 正文 [| key=value, ...]」。"""
        text = (raw or "").strip()
        parts = re.split(r"[|｜]", text)

        params_raw = ""
        if len(parts) >= 3 and cls._looks_like_params(parts[-1].strip()):
            params_raw = parts[-1].strip()
            parts = parts[:-1]

        if len(parts) >= 2:
            title = parts[0].strip()
            body = "|".join(parts[1:]).strip()
        else:
            only = parts[0].strip() if parts else ""
            lines = only.splitlines()
            if len(lines) >= 2:
                title = lines[0].strip()
                body = "\n".join(lines[1:]).strip()
            else:
                title, body = only, only

        return {
            "title": title,
            "body": body,
            "params": cls._parse_params(params_raw),
        }

    @staticmethod
    def _looks_like_params(text: str) -> bool:
        if "=" not in text:
            return False
        segments = [s for s in re.split(r"[,，;；]", text) if s.strip()]
        return bool(segments) and all("=" in s for s in segments)

    @classmethod
    def _parse_params(cls, raw: str) -> dict[str, Any]:
        result: dict[str, Any] = {}
        if not (raw or "").strip():
            return result
        for segment in re.split(r"[,，;；]", raw):
            segment = segment.strip()
            if not segment:
                continue
            if "=" not in segment:
                raise ValueError(
                    f"参数「{segment}」缺少 =，格式应为 key=value"
                )
            key, value = segment.split("=", 1)
            key = key.strip()
            key = FIELD_ALIASES.get(key, key)
            value = value.strip().strip('"').strip("'")
            result[key] = cls._coerce_scalar(value)
        return result

    @staticmethod
    def _coerce_scalar(value: str) -> Any:
        lowered = value.lower()
        if lowered in {"true", "false"}:
            return lowered == "true"
        if lowered in {"null", "none"}:
            return None
        if re.fullmatch(r"-?\d+", value):
            return int(value)
        if re.fullmatch(r"-?\d+\.\d+", value):
            return float(value)
        return value

    # ------------------------------------------------------------------
    # 平台配置
    # ------------------------------------------------------------------
    def _platforms(self) -> list[dict[str, Any]]:
        items = self.config.get("platforms", []) or []
        result: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "") or "").strip()
            url = str(item.get("url", "") or "").strip()
            if name and url:
                result.append(item)
        return result

    def _find_platform(self, name: str) -> dict[str, Any] | None:
        name = (name or "").strip()
        if not name:
            return None
        for platform in self._platforms():
            if str(platform.get("name", "")).strip() == name:
                return platform
        return None

    @staticmethod
    def _sender_id(event: AstrMessageEvent) -> str:
        getter = getattr(event, "get_sender_id", None)
        if callable(getter):
            try:
                value = str(getter() or "").strip()
                if value:
                    return value
            except Exception:
                pass
        sender = getattr(getattr(event, "message_obj", None), "sender", None)
        return str(getattr(sender, "user_id", "") or "").strip()

    def _admin_whitelist(self) -> set[str]:
        raw = self.config.get("admin_users", "")
        if isinstance(raw, (list, tuple, set)):
            items = [str(item) for item in raw]
        else:
            items = re.split(r"[,，;；\s]+", str(raw or ""))
        cleaned = {item.strip().strip('"').strip("'") for item in items}
        return {item.lower() for item in cleaned if item}

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        sender_id = self._sender_id(event)
        if sender_id and sender_id.lower() in self._admin_whitelist():
            return True
        if self.config.get("strict_admin", False):
            return False
        # 协议端可能把群主标成 owner，而 AstrBot 的 is_admin() 只认 admin
        roles: list[str] = []
        event_role = getattr(event, "role", None)
        if event_role:
            roles.append(str(event_role))
        sender = getattr(getattr(event, "message_obj", None), "sender", None)
        sender_role = getattr(sender, "role", None)
        if sender_role:
            roles.append(str(sender_role))
        if any(role.strip().lower() in {"admin", "owner"} for role in roles):
            return True
        try:
            return bool(event.is_admin())
        except Exception:
            return False

    # ------------------------------------------------------------------
    # 文案
    # ------------------------------------------------------------------
    def _help_text(self) -> str:
        """按已配置模板动态生成使用示例。"""
        templates = self._templates()
        lines = ["📮 发布帮助", "", "【使用示例】"]

        if templates:
            for template in templates:
                name = str(template.get("name") or "").strip()
                description = str(template.get("description") or "").strip()
                if description:
                    lines.append(f"{name}｜{description}：")
                else:
                    lines.append(f"{name}：")
                lines.append(f"  {self._example_command(template)}")
                lines.append("")

            lines.append("预览（不发送）：")
            lines.append(
                f"  {self._example_command(templates[0], preview=True)}"
            )
        else:
            lines.append("  /发布 <模板> <标题> | <正文>")
            lines.append(
                "  /发布 <模板> <标题> | <正文> | key=value, key=value"
            )
            lines.append("  /发布预览 <模板> <标题> | <正文>")

        lines.append("")
        lines.append("查看模板和平台：")
        lines.append("  /发布列表")
        lines.append("")
        lines.append(
            "说明：标题和正文用 | 或 ｜ 分隔；也可以第一行写标题、后续行写正文。"
        )
        return "\n".join(lines)

    @staticmethod
    def _example_command(template: dict[str, Any], preview: bool = False) -> str:
        name = str(template.get("name") or "").strip()
        action = str(template.get("action") or "").strip()
        prefix = "/发布预览" if preview else "/发布"
        if action == "product":
            return (
                f"{prefix} {name} 新款工具 | 高效稳定。 | "
                "price=9.9, amount=10, unit=件"
            )
        if action == "demand":
            return f"{prefix} {name} 寻求合作 | 需要一位前端开发者。 | days=7"
        return f"{prefix} {name} 今日观察 | 今天学习了 AstrBot 插件开发。"

    @staticmethod
    def _short_usage() -> str:
        return (
            "用法：/发布 <模板> <标题> | <正文>\n"
            "发送 /发布帮助 查看使用示例，发送 /发布列表 查看模板和平台。"
        )

    def _list_text(self) -> str:
        templates = self._templates()
        platforms = self._platforms()

        lines = ["📋 可用发布模板："]
        if templates:
            for template in templates:
                action = str(template.get("action") or "")
                required = self._required_fields(template)
                lines.append(
                    f"- {template.get('name')} → 平台 {self._template_platform_name(template)}"
                    f" ｜ action={action} ｜ 分类 {template.get('catid', '-')}"
                )
                lines.append(f"  必填：{'、'.join(required)}")
                description = str(template.get("description") or "").strip()
                if description:
                    lines.append(f"  {description}")
        else:
            lines.append("- （未配置模板）")

        lines.append("")
        lines.append("🖥️ 可用平台：")
        if platforms:
            lines.append("、".join(str(p.get("name")) for p in platforms))
        else:
            lines.append("（未配置平台）")
        lines.append("")
        lines.append("发送 /发布帮助 查看完整用法和使用示例。")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 发布逻辑
    # ------------------------------------------------------------------
    async def _publish(self, platform: dict[str, Any], content: str) -> str:
        request, error = self._prepare_request(platform, content)
        if error:
            return error
        return await self._send_request(request)

    def _preview(self, platform: dict[str, Any], content: str) -> str:
        request, error = self._prepare_request(platform, content)
        if error:
            return error
        body = json.loads(json.dumps(request["body"], ensure_ascii=False))
        if "sign" in body:
            body["sign"] = "***"
        return (
            "🧪 预览（未发送）\n"
            f"{request['method']} {request['url']}\n"
            + json.dumps(body, ensure_ascii=False, indent=2)
        )

    def _prepare_request(
        self, platform: dict[str, Any], content: str
    ) -> tuple[dict[str, Any] | None, str | None]:
        name = str(platform.get("name", "")).strip()
        url = str(platform.get("url", "")).strip()
        if not url.lower().startswith(("http://", "https://")):
            return (
                None,
                f"❌ 平台「{name}」的 API 地址无效（需以 http:// 或 https:// 开头）。",
            )

        method = str(platform.get("method", "POST") or "POST").strip().upper()
        if method not in {"POST", "PUT", "PATCH"}:
            method = "POST"

        try:
            extra_headers = self._parse_json_object(
                platform.get("extra_headers"), "额外请求头"
            )
        except ValueError as exc:
            return None, f"❌ 平台「{name}」配置错误：{exc}"

        headers: dict[str, str] = {
            "User-Agent": "AstrBot-PublishBridge/1.1",
        }
        headers.update({str(k): str(v) for k, v in extra_headers.items()})

        auth_type = str(platform.get("auth_type", "bearer") or "bearer").strip().lower()
        configured_appid = str(platform.get("appid", "") or "").strip()
        if configured_appid and auth_type != "md5_sign":
            logger.warning(
                f"[publish-bridge] 平台「{name}」配置了 appid，"
                f"但鉴权方式为 {auth_type}，不会发送 appid/timestamp/sign"
            )
        token = str(platform.get("token", "") or "")
        if auth_type == "bearer" and token:
            headers["Authorization"] = f"Bearer {token}"
        elif auth_type == "header" and token:
            header_name = str(platform.get("auth_header", "X-API-Key") or "X-API-Key")
            headers[header_name] = token

        try:
            body = self._parse_json_object(platform.get("extra_body"), "额外请求体")
        except ValueError as exc:
            return None, f"❌ 平台「{name}」配置错误：{exc}"

        content_field = str(platform.get("content_field", "content") or "content").strip()
        title_field = str(platform.get("title_field", "") or "").strip()
        html_content = bool(platform.get("html_content", False))

        if title_field:
            preset_title = platform.get("__title__")
            preset_body = platform.get("__body__")
            if preset_title is not None:
                title = str(preset_title)
                content_body = str(preset_body or "")
            else:
                title, content_body = self._split_title(content)
            if not title:
                return None, "❌ 该平台要求提供标题，请使用「标题 | 正文」格式。"
            if len(title) < 3:
                return None, "❌ 标题至少需要 3 个字符，请使用「标题 | 正文」格式。"
            self._set_dotted(body, title_field, title)
            self._set_dotted(
                body,
                content_field,
                self._to_html(content_body) if html_content else content_body,
            )
        else:
            self._set_dotted(
                body,
                content_field,
                self._to_html(content) if html_content else content,
            )

        if auth_type == "md5_sign":
            appid = str(platform.get("appid", "") or "").strip()
            if not appid or not token:
                return None, f"❌ 平台「{name}」缺少 appid 或 secret，无法生成签名。"
            timestamp = int(time.time())
            sign = hashlib.md5(
                f"{appid}{timestamp}{token}".encode("utf-8")
            ).hexdigest()
            body["appid"] = appid
            body["timestamp"] = timestamp
            body["sign"] = sign

        return {
            "name": name,
            "url": url,
            "method": method,
            "headers": headers,
            "body": body,
            "auth_type": auth_type,
            "timeout": self._clamp_int(platform.get("timeout", 15), 1, 120, 15),
            "verify_ssl": bool(platform.get("verify_ssl", True)),
        }, None

    async def _send_request(self, request: dict[str, Any]) -> str:
        name = request["name"]
        url = request["url"]
        method = request["method"]
        timeout_s = request["timeout"]
        verify_ssl = request["verify_ssl"]
        connector = aiohttp.TCPConnector(ssl=False) if not verify_ssl else None

        title_preview = "-"
        data_obj = request["body"].get("data")
        if isinstance(data_obj, dict):
            title_preview = data_obj.get("title", "-")
        logger.info(
            f"[publish-bridge] -> {name} {method} {url} "
            f"(title={title_preview!r})"
        )

        try:
            timeout = aiohttp.ClientTimeout(total=timeout_s)
            async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
                async with session.request(
                    method,
                    url,
                    json=request["body"],
                    headers=request["headers"],
                ) as resp:
                    status = resp.status
                    text = (await resp.text(errors="replace")).strip()
        except asyncio.TimeoutError:
            logger.warning(f"[publish-bridge] timeout -> {name} {url}")
            return f"⏱️ 发布失败：平台「{name}」请求超时（>{timeout_s}s）。"
        except aiohttp.ClientError as exc:
            logger.warning(f"[publish-bridge] client error -> {name}: {exc!r}")
            return f"❌ 发布失败：连接平台「{name}」出错：{exc}"
        except Exception as exc:  # noqa: BLE001 - 不让插件因单次请求崩溃
            logger.exception(f"[publish-bridge] unexpected error -> {name}")
            return f"❌ 发布失败：平台「{name}」发生未知错误：{exc}"

        payload_ok = 200 <= status < 300
        detail = ""
        try:
            resp_json = json.loads(text) if text else None
        except json.JSONDecodeError:
            resp_json = None

        if isinstance(resp_json, dict):
            code = resp_json.get("code")
            if code is not None and code not in (0, "0", 200, "200"):
                payload_ok = False
            msg = str(resp_json.get("msg") or "").strip()
            data = resp_json.get("data")
            url_value = ""
            status_txt = ""
            if isinstance(data, dict):
                url_value = str(data.get("url") or data.get("mobile_url") or "").strip()
                status_txt = str(data.get("status_txt") or "").strip()
            detail = " ".join(part for part in (msg, status_txt, url_value) if part)

        if payload_ok:
            logger.info(f"[publish-bridge] ok -> {name} HTTP {status}")
            message = f"✅ 已发布到「{name}」（HTTP {status}）"
            if detail:
                message += f"\n{detail}"
            if self.config.get("show_response", False) and text:
                message += f"\n{self._preview_text(text)}"
            return message

        logger.warning(
            f"[publish-bridge] failed -> {name} HTTP {status}: {text[:200]}"
        )
        message = f"❌ 发布失败：平台「{name}」返回 HTTP {status}"
        if detail:
            message += f"\n{detail}"
        if text and not detail:
            message += f"\n{self._preview_text(text)}"
        if "appid" in text.lower() and request.get("auth_type") != "md5_sign":
            message += (
                "\n提示：平台需要 appid 参数，请把该平台的「鉴权方式」"
                "改为 md5_sign，并填写 AppID 与 Secret。"
            )
        return message

    # ------------------------------------------------------------------
    # 小工具
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_json_object(raw: Any, label: str) -> dict[str, Any]:
        if raw is None:
            return {}
        if isinstance(raw, dict):
            return dict(raw)
        text = str(raw).strip()
        if not text:
            return {}
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{label}不是合法 JSON（{exc.msg}）") from exc
        if not isinstance(value, dict):
            raise ValueError(f"{label}必须是 JSON 对象，例如 {{\"key\": \"value\"}}")
        return value

    @staticmethod
    def _split_title(content: str) -> tuple[str, str]:
        """把「标题 | 正文」或「第一行标题 + 后续正文」拆成标题和正文。"""
        text = (content or "").strip()
        for separator in ("|", "｜"):
            if separator in text:
                title, body = text.split(separator, 1)
                return title.strip(), body.strip()
        lines = text.splitlines()
        if len(lines) >= 2:
            return lines[0].strip(), "\n".join(lines[1:]).strip()
        return text.strip(), text.strip()

    @staticmethod
    def _to_html(text: str) -> str:
        """把纯文本转成简单 HTML；已经是 HTML 的内容原样保留。"""
        text = (text or "").strip()
        if not text:
            return ""
        if text.lstrip().lower().startswith("<"):
            return text
        paragraphs = [line.strip() for line in text.splitlines() if line.strip()]
        return "".join(f"<p>{paragraph}</p>" for paragraph in paragraphs)

    @staticmethod
    def _set_dotted(data: dict[str, Any], path: str, value: Any) -> None:
        keys = [key for key in path.split(".") if key]
        if not keys:
            data["content"] = value
            return
        cursor = data
        for key in keys[:-1]:
            child = cursor.get(key)
            if not isinstance(child, dict):
                child = {}
                cursor[key] = child
            cursor = child
        cursor[keys[-1]] = value

    @staticmethod
    def _get_dotted(data: dict[str, Any], path: str) -> Any:
        cursor: Any = data
        for key in [key for key in path.split(".") if key]:
            if not isinstance(cursor, dict):
                return None
            cursor = cursor.get(key)
        return cursor

    @staticmethod
    def _is_pure_url(text: str) -> bool:
        return bool(re.fullmatch(r"https?://[^\s]+", (text or "").strip(), re.I))

    async def _fetch_article_text(
        self, url: str
    ) -> tuple[str, str | None, str | None]:
        """抓取链接正文，返回 (页面标题, 正文文本, 错误)。"""
        timeout_s = self._clamp_int(
            self.config.get("link_fetch_timeout", 20), 5, 60, 20
        )
        headers = {
            "User-Agent": LINK_USER_AGENT,
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        try:
            timeout = aiohttp.ClientTimeout(total=timeout_s)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    url, headers=headers, allow_redirects=True
                ) as resp:
                    status = resp.status
                    charset = resp.charset
                    raw = await resp.read()
        except asyncio.TimeoutError:
            return "", None, f"请求超时（>{timeout_s}s）"
        except aiohttp.ClientError as exc:
            return "", None, f"请求失败：{exc}"
        except Exception as exc:  # noqa: BLE001
            logger.exception("[publish-bridge] link fetch error")
            return "", None, f"请求异常：{exc}"

        if status != 200:
            return "", None, f"HTTP {status}"

        html_text = self._decode_html(raw, charset)
        page_title, article_text = self._extract_article(html_text)
        if not article_text or len(article_text) < 50:
            return page_title, None, "未提取到有效正文"

        limit = self._clamp_int(
            self.config.get("link_max_chars", 20000), 1000, 100000, 20000
        )
        if len(article_text) > limit:
            article_text = article_text[:limit].rstrip() + "…"

        logger.info(
            f"[publish-bridge] link ok -> {url} "
            f"title={page_title!r} chars={len(article_text)}"
        )
        return page_title, article_text, None

    @staticmethod
    def _decode_html(raw: bytes, charset: str | None) -> str:
        candidates: list[str] = []
        if charset:
            candidates.append(charset)
        head = raw[:4096].decode("ascii", errors="ignore").lower()
        match = re.search(r'charset=["\']?([\w-]+)', head)
        if match:
            candidates.append(match.group(1))
        candidates += ["utf-8", "gb18030", "big5"]
        for encoding in candidates:
            try:
                return raw.decode(encoding)
            except (LookupError, UnicodeDecodeError):
                continue
        return raw.decode("utf-8", errors="replace")

    @staticmethod
    def _extract_article(html_text: str) -> tuple[str, str]:
        parser = _ArticleHTMLParser()
        try:
            parser.feed(html_text)
            parser.close()
        except Exception:
            pass

        def is_good(paragraph: str) -> bool:
            paragraph = paragraph.strip()
            if len(paragraph) < 12:
                return False
            if paragraph in {"+", "-", "-->"} or "-->" in paragraph:
                return False
            if re.fullmatch(r"[\d\s:：\-—/,\.年月日]+", paragraph):
                return False
            return True

        seen: set[str] = set()
        paragraphs: list[str] = []
        for paragraph in parser.paragraphs:
            paragraph = paragraph.strip()
            if not is_good(paragraph) or paragraph in seen:
                continue
            seen.add(paragraph)
            paragraphs.append(paragraph)

        text = "\n".join(paragraphs)
        if not text:
            fallback = re.sub(r"[ \t]+", " ", "\n".join(parser.all_buf))
            fallback = re.sub(r"\n{2,}", "\n", fallback).strip()
            if len(fallback) >= 20:
                text = fallback
        if not text and parser.meta_description:
            text = parser.meta_description.strip()

        page_title = re.sub(r"\s+", " ", " ".join(parser.title_buf)).strip()
        return page_title, text.strip()

    @staticmethod
    def _text_to_html(text: str) -> str:
        paragraphs = [
            line.strip() for line in (text or "").splitlines() if line.strip()
        ]
        return "".join(f"<p>{escape(paragraph)}</p>" for paragraph in paragraphs)

    @staticmethod
    def _clamp_int(value: Any, minimum: int, maximum: int, default: int) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return default
        return max(minimum, min(maximum, number))

    @staticmethod
    def _preview_text(text: str) -> str:
        text = " ".join(text.split())
        if len(text) <= RESPONSE_PREVIEW_LIMIT:
            return text
        return text[:RESPONSE_PREVIEW_LIMIT] + "…"
