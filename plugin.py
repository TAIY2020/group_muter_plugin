"""群聊静音插件 — MaiBot SDK v2

允许管理员通过聊天命令，让麦麦在指定群聊中临时进入“静音状态”。
静音期间，所有群消息将被拦截，不会触发麦麦的思考和回复。
管理员可通过关键词指令或 @麦麦 解除静音。

实现方案：
    1. 入站拦截：使用 @HookHandler 订阅 chat.receive.after_process Hook。
       此 Hook 在消息预处理完成后、Command 匹配之前触发，
       可以通过返回 {"action": "abort"} 拦截消息，阻止其进入后续流程。
    2. 出站拦截：使用 @HookHandler 订阅 send_service.before_send Hook。
       此 Hook 在消息即将发送前触发，用于拦截静音前已进入思考流程、
       但在静音后才生成回复的"漏网"消息。
"""

from maibot_sdk import Field, HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

import asyncio
import logging
import re
import time
from typing import Dict, List, Optional


logger = logging.getLogger("plugin.group_muter")

# 预编译正则：匹配 CQ at 码或 @前缀
_AT_PREFIX_PATTERN = re.compile(r"\[CQ:at,[^\]]+\]|@\S+")


# --- 配置模型 ---

class PluginSection(PluginConfigBase):
    """插件基本配置。"""

    __ui_label__ = "插件设置"

    name: str = Field(
        default="group_muter_plugin",
        description="插件名称",
        json_schema_extra={"disabled": True}
    )
    version: str = Field(
        default="2.1.0",
        description="插件版本",
        json_schema_extra={"disabled": True}
    )
    config_version: str = Field(
        default="2.1.0",
        description="配置文件版本",
        json_schema_extra={"disabled": True}
    )
    enabled: bool = Field(
        default=True,
        description="是否启用插件",
        json_schema_extra={"label": "启用插件"}
    )


class MuteSection(PluginConfigBase):
    """静音功能配置。"""

    __ui_label__ = "静音设置"

    duration_seconds: int = Field(
        default=1200,
        description="静音持续时间（秒）",
        ge=60,
        le=86400,
        json_schema_extra={
            "label": "静音时长（秒）",
            "hint": "60 ~ 86400 秒",
            "x-widget": "slider",
            "min": 60,
            "max": 86400,
            "step": 60,
        },
    )
    mute_keywords: List[str] = Field(
        default=["Mute True", "安安你去看书去"],
        description="触发静音的关键词列表",
        json_schema_extra={"label": "静音关键词", "hint": "管理员发送这些词时开启静音"},
    )
    unmute_keywords: List[str] = Field(
        default=["Mute False", "安安别看了"],
        description="解除静音的关键词列表",
        json_schema_extra={"label": "解除关键词", "hint": "管理员发送这些词时解除静音"},
    )
    enable_unmute: bool = Field(
        default=True,
        description="是否启用解除静音关键词",
        json_schema_extra={"label": "启用关键词解除"}
    )
    at_mention_break: bool = Field(
        default=True,
        description="管理员 @麦麦 时是否自动解除静音",
        json_schema_extra={"label": "允许@解除静音"}
    )
    mute_reply: str = Field(
        default="好吧，那我去看会书📘，你们先聊...",
        description="管理员开启静音时麦麦的回复语",
        json_schema_extra={"label": "开启静音回复语"},
    )
    unmute_reply: str = Field(
        default="我回来啦，你们聊啥呢🤔",
        description="管理员解除静音时麦麦的回复语（@解除与关键词解除共用）",
        json_schema_extra={"label": "解除静音回复语"},
    )
    no_permission_reply: str = Field(
        default="？？？你在教我做事🤡",
        description="非管理员尝试触发静音时麦麦的拒绝回复语",
        json_schema_extra={"label": "拒绝权限回复语"},
    )


class UserControlSection(PluginConfigBase):
    """权限控制配置。"""

    __ui_label__ = "权限控制"

    list_type: str = Field(
        default="whitelist",
        description="权限列表类型：whitelist 或 blacklist",
        json_schema_extra={"label": "名单类型", "x-widget": "select", "options": [{"label": "白名单", "value": "whitelist"}, {"label": "黑名单", "value": "blacklist"}]},
    )
    list: List[str] = Field(
        default=[],
        description="拥有权限的用户 QQ 号列表",
        json_schema_extra={"label": "用户列表", "hint": "填写 QQ 号，如 [\"123456\"]"},
    )


class GroupMuterConfig(PluginConfigBase):
    """群聊静音插件完整配置。"""

    plugin: PluginSection = Field(default_factory=PluginSection)
    mute: MuteSection = Field(default_factory=MuteSection)
    user_control: UserControlSection = Field(default_factory=UserControlSection)


# --- 核心状态管理器 ---

class MuteStatus:
    """群聊静音状态管理器（类级别单例，跨实例共享状态）。

    注意：使用类变量作为共享状态，在插件重载时需要手动清理。
    """

    _mute_until: Dict[str, float] = {}
    _group_names: Dict[str, str] = {}
    _send_exempt_until: Dict[str, float] = {}  # group_id → 豁免截止时间戳
    _summary_task: Optional[asyncio.Task] = None  # 后台摘要日志定时任务
    _last_msg_log_time: Dict[str, float] = {}  # 消息驱动日志的节流时间戳

    @classmethod
    def set_send_exempt(cls, group_id: str, seconds: float = 5.0):
        """设置短暂的发送豁免期，允许插件自身的控制消息通过。"""
        cls._send_exempt_until[group_id] = time.time() + seconds

    @classmethod
    def is_send_exempt(cls, group_id: str) -> bool:
        """检查指定群是否处于发送豁免期。"""
        exempt_until = cls._send_exempt_until.get(group_id)
        if exempt_until and time.time() < exempt_until:
            return True
        cls._send_exempt_until.pop(group_id, None)
        return False

    @classmethod
    def set_mute(cls, group_id: str, seconds: int, group_name: Optional[str] = None):
        """开启指定群的静音状态，并启动后台摘要日志任务。"""
        cls._mute_until[group_id] = time.time() + seconds
        if group_name:
            cls._group_names[group_id] = group_name
        logger.info(f"[{group_name or group_id}] 进入静音模式，持续 {seconds} 秒。")
        # 确保后台摘要日志任务正在运行
        cls._ensure_summary_task()

    @classmethod
    def clear_mute(cls, group_id: str):
        """解除指定群的静音状态。"""
        if cls._mute_until.pop(group_id, None):
            group_name = cls._group_names.pop(group_id, None)
            logger.info(f"[{group_name or group_id}] 已解除静音模式。")

    @classmethod
    def is_muted(cls, group_id: str) -> bool:
        """检查指定群是否处于静音状态，超时则自动解除。"""
        mute_end_time = cls._mute_until.get(group_id)
        if mute_end_time and time.time() >= mute_end_time:
            logger.info(f"[{cls._group_names.get(group_id, group_id)}] 静音时间已到，自动解除。")
            cls.clear_mute(group_id)
            return False
        return bool(mute_end_time)

    @classmethod
    def remaining_seconds(cls, group_id: str) -> int:
        """返回指定群的剩余静音秒数。"""
        mute_end_time = cls._mute_until.get(group_id)
        if mute_end_time:
            return max(int(mute_end_time - time.time()), 0)
        return 0

    @classmethod
    def _ensure_summary_task(cls):
        """确保后台摘要日志任务正在运行。如果已停止或未启动，则创建新任务。"""
        if cls._summary_task is None or cls._summary_task.done():
            cls._summary_task = asyncio.create_task(cls._summary_loop())

    @classmethod
    async def _summary_loop(cls):
        """后台定时任务：每 30 秒打印一次所有静音群的状态摘要。

        当没有任何群处于静音状态时，任务自动退出。
        """
        try:
            while True:
                await asyncio.sleep(30)
                now = time.time()
                # 收集仍在静音中的群
                active_groups = []
                expired_groups = []
                for group_id, mute_end_time in list(cls._mute_until.items()):
                    if now >= mute_end_time:
                        expired_groups.append(group_id)
                    else:
                        active_groups.append(group_id)

                # 清理已过期的静音
                for group_id in expired_groups:
                    display_name = cls._group_names.get(group_id, group_id)
                    logger.info(f"[{display_name}] 静音时间已到，自动解除。")
                    cls._mute_until.pop(group_id, None)
                    cls._group_names.pop(group_id, None)

                # 打印仍在静音中的群的摘要
                for group_id in active_groups:
                    mute_end_time = cls._mute_until.get(group_id)
                    if mute_end_time:
                        remaining = int(mute_end_time - now)
                        end_str = time.strftime("%H:%M:%S", time.localtime(mute_end_time))
                        display_name = cls._group_names.get(group_id, group_id)
                        logger.info(
                            f"[{display_name}] 处于静音模式，剩余 {remaining} 秒，"
                            f"将在 {end_str} 结束。"
                        )

                # 没有任何群在静音中，退出循环
                if not cls._mute_until:
                    logger.debug("所有群已解除静音，摘要日志任务退出。")
                    break
        except asyncio.CancelledError:
            pass

    @classmethod
    def cancel_summary_task(cls):
        """取消后台摘要日志任务。"""
        if cls._summary_task and not cls._summary_task.done():
            cls._summary_task.cancel()
            cls._summary_task = None

    @classmethod
    def log_on_message(cls, group_id: str):
        """收到被拦截的消息时打印一次剩余时间（每 30 秒最多一次）。"""
        now = time.time()
        if now - cls._last_msg_log_time.get(group_id, 0) < 30:
            return
        mute_end_time = cls._mute_until.get(group_id)
        if mute_end_time:
            remaining = int(mute_end_time - now)
            end_str = time.strftime("%H:%M:%S", time.localtime(mute_end_time))
            display_name = cls._group_names.get(group_id, group_id)
            logger.info(
                f"[{display_name}] 静音中拦截消息，剩余 {remaining} 秒，"
                f"将在 {end_str} 结束。"
            )
            cls._last_msg_log_time[group_id] = now


# --- 辅助函数 ---

def _strip_at_prefix(text: str) -> str:
    """去除文本中的 CQ at 码和 @前缀。"""
    return _AT_PREFIX_PATTERN.sub("", text).strip()


def _is_keyword_in_text(text: str, keywords: List[str]) -> bool:
    """检查文本（去除 CQ 码后）是否精确匹配关键词列表中的某个词。"""
    if not text or not keywords:
        return False
    return _strip_at_prefix(text) in keywords


def _is_bot_mentioned(message: dict, plain_text: str, raw_content: str,
                      message_segments: list) -> bool:
    """检测消息中是否 @了麦麦。

    检测策略（按优先级）：
    1. message 中的 is_at 字段（SDK 预处理标记，最可靠）
    2. message 中的 is_mentioned 字段（SDK 预处理标记）
    3. message_segments 中是否有 type="at" 的段
    4. raw_content 中是否包含 [CQ:at,...] 码
    """
    # 策略1+2：SDK 预处理标记（最可靠，不依赖名字或格式）
    if message.get("is_at"):
        return True
    if message.get("is_mentioned"):
        return True

    # 策略3：检查消息段中的 at 类型
    if message_segments:
        for seg in message_segments:
            if isinstance(seg, dict) and (seg.get("type") or "") == "at":
                return True

    # 策略4：检查原始内容中的 CQ:at 码
    if raw_content and "[CQ:at," in raw_content:
        return True

    return False


def _extract_from_message(message: Optional[dict]) -> tuple:
    """从序列化的 SessionMessage 字典中提取关键字段。

    Returns:
        (plain_text, raw_content, stream_id, group_id, group_name, user_id, message_segments)
    """
    if not message or not isinstance(message, dict):
        return "", "", "", "", "", "", []

    # processed_plain_text 用于关键词匹配（已去除 CQ 码）
    plain_text = message.get("processed_plain_text") or message.get("plain_text") or ""
    # 原始内容保留 CQ 码，用于 @ 检测
    raw_content = message.get("raw_content") or message.get("plain_text") or ""
    stream_id = message.get("session_id") or ""

    msg_info = message.get("message_info") or {}
    user_info = msg_info.get("user_info") or {}
    group_info = msg_info.get("group_info") or {}

    user_id = user_info.get("user_id") or ""
    group_id = group_info.get("group_id") or ""
    group_name = group_info.get("group_name") or ""

    # 提取消息段列表，用于精确检测 at 类型
    message_segments = msg_info.get("message_segments") or message.get("message_segments") or []

    return (
        str(plain_text), str(raw_content), str(stream_id),
        str(group_id), str(group_name), str(user_id), message_segments,
    )


# --- 主插件类 ---

class GroupMuterPlugin(MaiBotPlugin):
    """群聊静音插件。

    使用 @HookHandler 订阅 chat.receive.after_process Hook，
    在消息预处理完成后、Command 匹配之前拦截静音群的消息。
    不使用任何 @Command，不会影响其他插件的命令匹配。
    """

    config_model = GroupMuterConfig

    def __init__(self) -> None:
        super().__init__()
        self._user_set: set[str] = set()  # 缓存权限用户集合

    async def on_load(self) -> None:
        self._user_set = {str(u) for u in self.config.user_control.list}
        logger.info("群聊静音插件(v2.1.0)初始化完成。")

    async def on_unload(self) -> None:
        """插件卸载时清理所有静音状态和后台任务。"""
        MuteStatus.cancel_summary_task()
        MuteStatus._mute_until.clear()
        MuteStatus._group_names.clear()
        MuteStatus._send_exempt_until.clear()
        MuteStatus._last_msg_log_time.clear()

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        """配置热重载回调。"""
        if scope == "self":
            self._user_set = {str(u) for u in self.config.user_control.list}

    def _check_permission(self, user_id: str) -> bool:
        """检查用户是否有操作权限。"""
        if not user_id:
            return False
        user_id_str = str(user_id)
        list_type = self.config.user_control.list_type
        if list_type == "whitelist":
            return user_id_str in self._user_set
        if list_type == "blacklist":
            return user_id_str not in self._user_set
        logger.warning(f"未知的权限列表类型: '{list_type}'，默认拒绝")
        return False

    # ===== 核心 Hook 处理器 =====

    @HookHandler(
        "send_service.before_send",
        name="mute_send_guard",
        description="静音期间拦截出站消息，防止静音前已进入思考流程的消息在静音后发出",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        timeout_ms=3000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_mute_send_guard(self, message: Optional[dict] = None, **kwargs):
        """出站消息拦截器。

        在 send_service.before_send 阶段检查：
        如果消息目标群处于静音状态，则 abort 阻止发送。
        仅拦截 bot 主动回复（非插件自身发送的控制消息）。
        """
        if message is None or not isinstance(message, dict):
            return None

        msg_info = message.get("message_info") or {}
        group_info = msg_info.get("group_info") or {}
        group_id = str(group_info.get("group_id") or "")

        if not group_id:
            return None

        if MuteStatus.is_muted(group_id):
            # 检查是否在豁免期（插件自身发送的控制消息）
            if MuteStatus.is_send_exempt(group_id):
                return None
            display_name = MuteStatus._group_names.get(group_id, group_id)
            logger.info(f"[{display_name}] 静音中，拦截出站消息")
            return {"action": "abort"}

        return None

    @HookHandler(
        "chat.receive.after_process",
        name="mute_guard",
        description="群聊静音守卫：拦截静音群消息、处理静音/解除指令",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        timeout_ms=5000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_mute_guard(self, message: Optional[dict] = None, **kwargs):
        """群聊静音核心处理器。

        在 chat.receive.after_process 阶段拦截：
        1. 非群聊 → continue
        2. 群未静音 + 管理员 + 静音关键词 → 开启静音，abort
        3. 群未静音 + 非管理员 + 静音关键词 → 拒绝提示，abort
        4. 群未静音 + 普通消息 → continue
        5. 群已静音 + 管理员 + @麦麦 → 解除静音，abort
        6. 群已静音 + 管理员 + 解除关键词 → 解除静音，abort
        7. 群已静音 + 其他消息 → 拦截，abort
        """
        if message is None:
            return None

        # 调试日志：打印 message 完整结构，用于排查字段名
        logger.debug("[mute_guard] message keys: %s", list(message.keys()) if isinstance(message, dict) else type(message))
        if isinstance(message, dict):
            msg_info = message.get("message_info") or {}
            logger.debug(
                "[mute_guard] message_info keys: %s | "
                "raw_content=%r | processed_plain_text=%r | plain_text=%r | "
                "message_segments=%r",
                list(msg_info.keys()) if isinstance(msg_info, dict) else type(msg_info),
                message.get("raw_content"),
                message.get("processed_plain_text"),
                message.get("plain_text"),
                (msg_info.get("message_segments") if isinstance(msg_info, dict) else None),
            )

        plain_text, raw_content, stream_id, group_id, group_name, user_id, message_segments = _extract_from_message(message)

        # 仅处理群消息
        if not group_id:
            return None

        is_admin = self._check_permission(user_id)
        mute_keywords = self.config.mute.mute_keywords

        # --- 群未静音 ---
        if not MuteStatus.is_muted(group_id):
            if _is_keyword_in_text(plain_text, mute_keywords):
                if is_admin:
                    duration = self.config.mute.duration_seconds
                    MuteStatus.set_mute(group_id, duration, group_name or None)
                    MuteStatus.set_send_exempt(group_id, 5.0)
                    await self.ctx.send.text(self.config.mute.mute_reply, stream_id)
                    return {"action": "abort"}
                else:
                    await self.ctx.send.text(self.config.mute.no_permission_reply, stream_id)
                    return {"action": "abort"}
            # 普通消息，放行
            return None

        # --- 群已静音 ---

        # 管理员 @麦麦 → 自动解除
        bot_mentioned = _is_bot_mentioned(message, plain_text, raw_content, message_segments)
        logger.debug(
            "[mute_guard] @检测: is_admin=%s, at_mention_break=%s, bot_mentioned=%s | "
            "is_at=%r, is_mentioned=%r, plain_text=%r, raw_content=%r, segments=%r",
            is_admin, self.config.mute.at_mention_break, bot_mentioned,
            message.get("is_at"), message.get("is_mentioned"),
            plain_text[:200], raw_content[:200], message_segments,
        )
        if is_admin and self.config.mute.at_mention_break and bot_mentioned:
            MuteStatus.set_send_exempt(group_id, 5.0)
            MuteStatus.clear_mute(group_id)
            await self.ctx.send.text(self.config.mute.unmute_reply, stream_id)
            return {"action": "abort"}

        # 管理员 + 解除关键词 → 解除
        if is_admin and self.config.mute.enable_unmute and _is_keyword_in_text(plain_text, self.config.mute.unmute_keywords):
            MuteStatus.set_send_exempt(group_id, 5.0)
            MuteStatus.clear_mute(group_id)
            await self.ctx.send.text(self.config.mute.unmute_reply, stream_id)
            return {"action": "abort"}

        # 其他所有消息一律拦截，打印剩余时间
        MuteStatus.log_on_message(group_id)
        return {"action": "abort"}


def create_plugin() -> GroupMuterPlugin:
    """创建群聊静音插件实例。"""
    return GroupMuterPlugin()
