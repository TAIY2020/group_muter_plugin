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
from pydantic import field_validator

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Optional


logger = logging.getLogger(__name__)

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
        default="2.2.0",
        description="插件版本",
        json_schema_extra={"disabled": True}
    )
    config_version: str = Field(
        default="2.2.0",
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

    list_type: Literal["whitelist", "blacklist"] = Field(
        default="whitelist",
        description="权限列表类型：whitelist 或 blacklist",
        json_schema_extra={
            "label": "名单类型",
            "hint": "白名单模式只允许列表内用户操作，黑名单模式则禁止列表内用户操作。",
        },
    )
    list: List[str] = Field(
        default=[],
        description="拥有权限的用户 QQ 号列表",
        json_schema_extra={"label": "用户列表", "hint": "填写 QQ 号，如 [\"123456\"]"},
    )

    @field_validator("list_type", mode="before")
    @classmethod
    def _normalize_list_type(cls, value: Any) -> Literal["whitelist", "blacklist"]:
        """规范化名单类型字段，对非法值兜底回退到默认值。"""
        normalized = "" if value is None else str(value).strip().lower()
        if normalized == "whitelist":
            return "whitelist"
        if normalized == "blacklist":
            return "blacklist"
        return "whitelist"


class GroupMuterConfig(PluginConfigBase):
    """群聊静音插件完整配置。"""

    plugin: PluginSection = Field(default_factory=PluginSection)
    mute: MuteSection = Field(default_factory=MuteSection)
    user_control: UserControlSection = Field(default_factory=UserControlSection)


# --- 核心状态管理器 ---

class MuteSessionTracker:
    """群聊静音的多群会话跟踪器（实例字段，禁止做类变量）。

    每个 GroupMuterPlugin 实例持有一个 tracker 实例。曾经的类变量
    在 SDK"预热新实例 → 原子切换 → unload 旧实例"热重载时序下
    会被旧实例的 on_unload clear() 抹掉新实例状态。改成实例字段后自然隔离。

    持有的状态：
        * 每群剩余静音时长（group_id → expire_at）
        * 群名缓存（日志友好显示）
        * 短期发送豁免窗口（插件自身控制消息的"放行令牌"）
        * 后台摘要任务（周期性输出仍静音群的剩余时间）
        * 每群消息驱动日志的节流时间戳

    热重载 → 状态丢失是接受的行为，见 docs/adr/0001-mute-tracker-no-persistence.md。
    """

    def __init__(self) -> None:
        self._mute_until: Dict[str, float] = {}
        self._group_names: Dict[str, str] = {}
        self._send_exempt_until: Dict[str, float] = {}  # group_id → 豁免截止时间戳
        self._summary_task: Optional[asyncio.Task] = None  # 后台摘要日志定时任务
        self._last_msg_log_time: Dict[str, float] = {}  # 消息驱动日志的节流时间戳

    def display_name(self, group_id: str) -> str:
        """返回日志显示用的群名；缺失时回退为 group_id。"""
        return self._group_names.get(group_id, group_id)

    def set_send_exempt(self, group_id: str, seconds: float = 1.5) -> None:
        """记录一个单次性发送豁免令牌。

        令牌被 ``consume_exempt`` 命中后立即消耗；``seconds`` 内一直没人来取
        则按时间过期被丢弃，避免长期残留。默认 1.5 秒覆盖"set_send_exempt 到
        紧邻的 send.text 进入 before_send hook"的合理上限。
        """
        self._send_exempt_until[group_id] = time.time() + seconds

    def consume_exempt(self, group_id: str) -> bool:
        """检查并**消耗**豁免令牌：未过期则返 True 同时立即清掉；否则 False。

        单次性的设计避免基于"时间窗口"被其它插件的并发出站消息无意中也享受到豁免——
        多插件并发场景下严格来说存在"别的插件抢先 consume 让本插件的控制消息反被拦"
        的理论顺序竞争，但在实际链路中"set_send_exempt → await ctx.send.text"是
        紧邻同步操作，其它插件极少能比 inline send 更快进入 before_send hook。
        """
        exempt_until = self._send_exempt_until.pop(group_id, None)
        return exempt_until is not None and time.time() < exempt_until

    def set_mute(self, group_id: str, seconds: int, group_name: Optional[str] = None) -> None:
        """开启指定群的静音状态，并启动后台摘要日志任务。"""
        self._mute_until[group_id] = time.time() + seconds
        if group_name:
            self._group_names[group_id] = group_name
        logger.info(f"[{group_name or group_id}] 进入静音模式，持续 {seconds} 秒。")
        # 确保后台摘要日志任务正在运行
        self._ensure_summary_task()

    def clear_mute(self, group_id: str) -> None:
        """解除指定群的静音状态。"""
        if self._mute_until.pop(group_id, None):
            group_name = self._group_names.pop(group_id, None)
            self._last_msg_log_time.pop(group_id, None)
            logger.info(f"[{group_name or group_id}] 已解除静音模式。")

    def is_muted(self, group_id: str) -> bool:
        """检查指定群是否处于静音状态，超时则自动解除。"""
        mute_end_time = self._mute_until.get(group_id)
        if mute_end_time and time.time() >= mute_end_time:
            logger.info(f"[{self.display_name(group_id)}] 静音时间已到，自动解除。")
            self.clear_mute(group_id)
            return False
        return bool(mute_end_time)

    def remaining_seconds(self, group_id: str) -> int:
        """返回指定群的剩余静音秒数。"""
        mute_end_time = self._mute_until.get(group_id)
        if mute_end_time:
            return max(int(mute_end_time - time.time()), 0)
        return 0

    def _ensure_summary_task(self) -> None:
        """确保后台摘要日志任务正在运行。如果已停止或未启动，则创建新任务。"""
        if self._summary_task is None or self._summary_task.done():
            self._summary_task = asyncio.create_task(self._summary_loop())

    async def _summary_loop(self) -> None:
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
                for group_id, mute_end_time in list(self._mute_until.items()):
                    if now >= mute_end_time:
                        expired_groups.append(group_id)
                    else:
                        active_groups.append(group_id)

                # 清理已过期的静音
                for group_id in expired_groups:
                    display_name = self.display_name(group_id)
                    logger.info(f"[{display_name}] 静音时间已到，自动解除。")
                    self._mute_until.pop(group_id, None)
                    self._group_names.pop(group_id, None)
                    self._last_msg_log_time.pop(group_id, None)

                # 打印仍在静音中的群的摘要
                for group_id in active_groups:
                    mute_end_time = self._mute_until.get(group_id)
                    if mute_end_time:
                        remaining = int(mute_end_time - now)
                        end_str = time.strftime("%H:%M:%S", time.localtime(mute_end_time))
                        display_name = self.display_name(group_id)
                        logger.info(
                            f"[{display_name}] 处于静音模式，剩余 {remaining} 秒，"
                            f"将在 {end_str} 结束。"
                        )

                # 没有任何群在静音中，退出循环
                if not self._mute_until:
                    logger.debug("所有群已解除静音，摘要日志任务退出。")
                    break
        except asyncio.CancelledError:
            pass

    async def cancel_summary_task(self) -> None:
        """取消并等待后台摘要日志任务真正退出。

        必须在 on_unload 调用：_summary_loop 的 bound method 持 self 强引用，
        不取消会让旧实例无法被 GC。

        async + await：cancel() 仅设置取消标志后立即返回，task 实际仍在事件循环中
        排队执行 CancelledError。若 on_unload 同步返回 → Runner 立即清理插件实例 →
        task 仍可能调用已被释放的 self.* 字段。必须 await 让 CancelledError 真正
        传播完成后再退出。
        """
        task = self._summary_task
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._summary_task = None

    def log_on_message(self, group_id: str) -> None:
        """收到被拦截的消息时打印一次剩余时间（每 30 秒最多一次）。"""
        now = time.time()
        if now - self._last_msg_log_time.get(group_id, 0) < 30:
            return
        mute_end_time = self._mute_until.get(group_id)
        if mute_end_time:
            remaining = int(mute_end_time - now)
            end_str = time.strftime("%H:%M:%S", time.localtime(mute_end_time))
            display_name = self.display_name(group_id)
            logger.info(
                f"[{display_name}] 静音中拦截消息，剩余 {remaining} 秒，"
                f"将在 {end_str} 结束。"
            )
            self._last_msg_log_time[group_id] = now


# --- 辅助函数 ---

def _strip_at_prefix(text: str) -> str:
    """去除文本中的 CQ at 码和 @前缀。"""
    return _AT_PREFIX_PATTERN.sub("", text).strip()


def _is_keyword_in_text(text: str, keywords: List[str]) -> bool:
    """检查文本（去除 CQ 码后）是否精确匹配关键词列表中的某个词。"""
    if not text or not keywords:
        return False
    return _strip_at_prefix(text) in keywords


def _is_bot_mentioned(message: dict) -> bool:
    """检测消息中是否 @ 了麦麦。

    依赖 adapter 在入站时正确设置 ``is_at`` / ``is_mentioned``，语义为"@ 到 bot 自己"。
    napcat adapter 已实现该契约（仅当 target_user_id == self_id 时设 True）；
    其它 adapter 若未实现，本插件的 @ 解除静音功能会降级——但关键词解除仍可用。

    历史上还有"扫描 message_segments 含 at 段"和"raw_content 含 [CQ:at,]"两条
    fallback 策略，但它们把"@任何人"误判为"@ bot"。详情见
    docs/adr/0002-trust-adapter-is-at-fields.md。
    """
    return bool(message.get("is_at") or message.get("is_mentioned"))


def _extract_from_message(message: Optional[dict]) -> tuple:
    """从序列化的 SessionMessage 字典中提取关键字段。

    Returns:
        (plain_text, stream_id, group_id, group_name, user_id)
    """
    if not message or not isinstance(message, dict):
        return "", "", "", "", ""

    # processed_plain_text 用于关键词匹配（已去除 CQ 码）
    plain_text = message.get("processed_plain_text") or message.get("plain_text") or ""
    stream_id = message.get("session_id") or ""

    msg_info = message.get("message_info") or {}
    user_info = msg_info.get("user_info") or {}
    group_info = msg_info.get("group_info") or {}

    user_id = user_info.get("user_id") or ""
    group_id = group_info.get("group_id") or ""
    group_name = group_info.get("group_name") or ""

    return (
        str(plain_text), str(stream_id),
        str(group_id), str(group_name), str(user_id),
    )


# --- 主插件类 ---

# --- 决策树解析 ---


@dataclass(frozen=True)
class MuteIntent:
    """决策树解析后的离散意图。dispatch 据此 1:1 派发副作用。

    并非所有字段对所有 kind 都有意义——例如 ``pass_through`` 不需要 group_id。
    保持单 dataclass + kind 字段的写法是为了构造与派发都简洁；若未来意图维度
    变多再切换到 sum type。
    """

    kind: Literal[
        "pass_through",
        "start_mute",
        "refuse_start",
        "end_mute",
        "intercept_while_muted",
    ]
    group_id: str = ""
    group_name: str = ""
    stream_id: str = ""


class MuteIntentResolver:
    """纯函数式：(message, mute_status, config, user_set) → MuteIntent。

    不修改任何状态（仅读 mute_status；is_muted 自带的过期清理是幂等的），
    不调任何 ``ctx.*`` RPC。所有副作用（开/解除静音、发消息、日志）由
    ``GroupMuterPlugin._dispatch_intent`` 执行。

    这样决策树可独立单测（喂 dict + state + config，断言 intent）；
    新增规则只在 ``resolve`` 里加一条 if 返回新 kind，副作用在 dispatch 加一个 elif
    即可——主路径（handler）不必再碰。
    """

    @staticmethod
    def resolve(
        *,
        message: dict,
        mute_status: "MuteSessionTracker",
        mute_config: "MuteSection",
        user_control: "UserControlSection",
        user_set: set[str],
    ) -> MuteIntent:
        plain_text, stream_id, group_id, group_name, user_id = (
            _extract_from_message(message)
        )

        # 非群聊 → 透传
        if not group_id:
            return MuteIntent(kind="pass_through")

        is_admin = MuteIntentResolver._is_admin(user_id, user_control, user_set)
        muted = mute_status.is_muted(group_id)

        # --- 未静音 ---
        if not muted:
            if _is_keyword_in_text(plain_text, mute_config.mute_keywords):
                if is_admin:
                    return MuteIntent(
                        kind="start_mute",
                        group_id=group_id,
                        group_name=group_name,
                        stream_id=stream_id,
                    )
                return MuteIntent(
                    kind="refuse_start",
                    group_id=group_id,
                    stream_id=stream_id,
                )
            return MuteIntent(kind="pass_through")

        # --- 已静音 ---
        if is_admin and mute_config.at_mention_break and _is_bot_mentioned(message):
            return MuteIntent(
                kind="end_mute",
                group_id=group_id,
                stream_id=stream_id,
            )
        if (
            is_admin
            and mute_config.enable_unmute
            and _is_keyword_in_text(plain_text, mute_config.unmute_keywords)
        ):
            return MuteIntent(
                kind="end_mute",
                group_id=group_id,
                stream_id=stream_id,
            )

        return MuteIntent(kind="intercept_while_muted", group_id=group_id)

    @staticmethod
    def _is_admin(
        user_id: str, user_control: "UserControlSection", user_set: set[str],
    ) -> bool:
        if not user_id:
            return False
        uid = str(user_id)
        if user_control.list_type == "whitelist":
            return uid in user_set
        if user_control.list_type == "blacklist":
            return uid not in user_set
        return False  # 不可达：field_validator 已把非法 list_type 归一化


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
        # 实例字段而非类变量：避免热重载时旧实例 on_unload 抹掉新实例状态
        self._mute_status = MuteSessionTracker()

    async def on_load(self) -> None:
        self._user_set = {str(u) for u in self.config.user_control.list}
        logger.info("群聊静音插件(v2.2.0)初始化完成。")

    async def on_unload(self) -> None:
        """插件卸载时取消后台任务。

        实例字段随插件 GC 自动回收，无需手动 clear；但 cancel_summary_task()
        必须 await——_summary_loop 的 bound method 持 self 强引用，
        不取消会让旧实例无法被 GC；cancel() 是异步信号，必须 await 等 task
        真正退出，否则 Runner 释放实例后 task 仍可能触达已失效字段。
        """
        await self._mute_status.cancel_summary_task()

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        """配置热重载回调。"""
        if scope == "self":
            self._user_set = {str(u) for u in self.config.user_control.list}

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

        if self._mute_status.is_muted(group_id):
            # 检查并消耗豁免令牌：单次性，避免 N 秒窗口期内别的插件也搭便车
            if self._mute_status.consume_exempt(group_id):
                return None
            display_name = self._mute_status.display_name(group_id)
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

        薄入口：把决策完全委托给 MuteIntentResolver（纯解析），handler 自己只做
        intent → 副作用的 1:1 派发。新增静音规则的扩展点在 resolver 而非这里。
        """
        if message is None or not isinstance(message, dict):
            return None

        intent = MuteIntentResolver.resolve(
            message=message,
            mute_status=self._mute_status,
            mute_config=self.config.mute,
            user_control=self.config.user_control,
            user_set=self._user_set,
        )
        return await self._dispatch_intent(intent)

    async def _dispatch_intent(self, intent: MuteIntent) -> Optional[Dict[str, Any]]:
        """根据 intent 执行副作用（修改 mute 状态 + 发回复 + 日志）。"""
        if intent.kind == "pass_through":
            return None

        logger.debug(
            "[mute_guard] intent=%s group=%s", intent.kind, intent.group_id or "<n/a>",
        )

        if intent.kind == "start_mute":
            duration = self.config.mute.duration_seconds
            self._mute_status.set_mute(
                intent.group_id, duration, intent.group_name or None,
            )
            self._mute_status.set_send_exempt(intent.group_id)
            await self.ctx.send.text(self.config.mute.mute_reply, intent.stream_id)
            return {"action": "abort"}

        if intent.kind == "refuse_start":
            await self.ctx.send.text(
                self.config.mute.no_permission_reply, intent.stream_id,
            )
            return {"action": "abort"}

        if intent.kind == "end_mute":
            self._mute_status.set_send_exempt(intent.group_id)
            self._mute_status.clear_mute(intent.group_id)
            await self.ctx.send.text(self.config.mute.unmute_reply, intent.stream_id)
            return {"action": "abort"}

        if intent.kind == "intercept_while_muted":
            self._mute_status.log_on_message(intent.group_id)
            return {"action": "abort"}

        return None


def create_plugin() -> GroupMuterPlugin:
    """创建群聊静音插件实例。"""
    return GroupMuterPlugin()
