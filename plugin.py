"""群聊静音插件 — MaiBot SDK v2

允许管理员通过聊天命令，让麦麦在指定群聊中临时进入“静音状态”。
静音期间，所有群消息将被拦截，不会触发麦麦的思考和回复。
管理员可通过关键词指令或 @麦麦 解除静音。

实现方案：
    1. 入站拦截：使用 @HookHandler 订阅 chat.receive.after_process Hook。
       此 Hook 在消息预处理完成后、Command 匹配之前触发，
       可以通过返回 {"action": "abort"} 拦截消息，阻止其进入后续流程。
    2. 主动沉默：注册 silence / sleep Tool，允许麦麦自主决定进入指定时长的沉默。
    3. 出站拦截：使用 @HookHandler 订阅 send_service.before_send Hook。
       此 Hook 在消息即将发送前触发，用于拦截静音前已进入思考流程、
       但在静音后才生成回复的"漏网"消息。
"""

from maibot_sdk import Field, HookHandler, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder, ToolParameterInfo, ToolParamType
from pydantic import field_validator

import asyncio
import logging
import re
import time
import json
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Optional


logger = logging.getLogger(__name__)

def _load_manifest_version() -> str:
    """从 _manifest.json 读取版本号，保持插件元数据单一来源。"""
    try:
        manifest_path = Path(__file__).parent / "_manifest.json"
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        version = data.get("version")
        if isinstance(version, str) and version.strip():
            return version.strip()
        logger.warning(
            "_manifest.json 中 version 字段缺失或非法 (%r)，回落到 0.0.0", version,
        )
    except Exception:
        logger.warning("读取 _manifest.json 失败，回落到 0.0.0", exc_info=True)
    return "0.0.0"


PLUGIN_VERSION = _load_manifest_version()

CONFIG_SCHEMA_VERSION = "2.2.0"

# 预编译正则：匹配 CQ at 码或 @前缀
_AT_PREFIX_PATTERN = re.compile(r"\[CQ:at,[^\]]+\]|@\S+")
# 仅匹配 CQ at 码（用于含空格群名片的兜底匹配）
_CQ_AT_PATTERN = re.compile(r"\[CQ:at,[^\]]+\]")

# 非管理员触发静音关键词时，拒绝回复的同群冷却（秒），防刷屏
_REFUSE_REPLY_COOLDOWN_SECONDS = 30.0

# 静音时长的合法区间（秒）：Field 校验、WebUI slider、clamp 兜底共用同一边界
_MUTE_DURATION_MIN_SECONDS = 60
_MUTE_DURATION_MAX_SECONDS = 86400
_TOOL_MUTE_MAX_DEFAULT_SECONDS = 10800


# --- 配置模型 ---

class PluginSection(PluginConfigBase):
    """插件基本配置。"""

    __ui_label__ = "插件设置"

    name: str = Field(
        default="group_muter_plugin",
        description="插件名称",
        json_schema_extra={"disabled": True}
    )
    config_version: str = Field(
        default=CONFIG_SCHEMA_VERSION,
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
        ge=_MUTE_DURATION_MIN_SECONDS,
        le=_MUTE_DURATION_MAX_SECONDS,
        json_schema_extra={
            "label": "静音时长（秒）",
            "hint": "60 ~ 86400 秒",
            "x-widget": "slider",
            "min": _MUTE_DURATION_MIN_SECONDS,
            "max": _MUTE_DURATION_MAX_SECONDS,
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
    renew_reply: str = Field(
        default="好哦，那我再多看一会书📘",
        description="静音期间管理员再次发送静音关键词（续期）时麦麦的回复语",
        json_schema_extra={"label": "续期静音回复语"},
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

    @field_validator("mute_keywords", "unmute_keywords", mode="before")
    @classmethod
    def _drop_blank_keywords(cls, value: Any) -> Any:
        """剔除空白关键词项。

        纯 @ 消息经 ``_strip_at_prefix`` 剥掉 CQ 码 / @前缀后是空串，
        关键词列表里混入 ""（WebUI 列表控件误加空项）会让任何纯 @ 消息
        精确匹配命中——管理员纯 @bot 直接开静音、非管理员被吞消息。
        """
        if isinstance(value, list):
            cleaned = (str(item).strip() for item in value)
            return [item for item in cleaned if item]
        return value

    @field_validator("duration_seconds", mode="before")
    @classmethod
    def _clamp_duration(cls, value: Any) -> Any:
        """把越界时长收敛到合法区间，而非让整份配置校验失败。

        SDK 在配置校验失败时只置 ``_plugin_config_instance = None`` 并打
        一行 warning，之后每次访问 ``self.config`` 都抛 RuntimeError，被
        两个 hook 的 ErrorPolicy.SKIP 吞掉——手改 toml 写 30 的代价是整个
        插件静默失效。与 user_control.list 的 ``_coerce_user_ids`` 同一
        容错哲学。非数值输入原样透传，交给 pydantic 正常报错。
        """
        try:
            number = int(value)
        except (TypeError, ValueError):
            return value
        clamped = min(
            max(number, _MUTE_DURATION_MIN_SECONDS), _MUTE_DURATION_MAX_SECONDS
        )
        if clamped != number:
            logger.warning(
                "duration_seconds=%s 超出 [%s, %s]，已收敛为 %s",
                number, _MUTE_DURATION_MIN_SECONDS, _MUTE_DURATION_MAX_SECONDS, clamped,
            )
        return clamped


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

    @field_validator("list", mode="before")
    @classmethod
    def _coerce_user_ids(cls, value: Any) -> Any:
        """容忍 TOML 中漏写引号的数字 QQ 号。

        Pydantic v2 即使在 lax 模式也不做 int → str 自动转换，
        ``list = [12345]`` 会让整份 GroupMuterConfig 校验失败而非降级。
        校验发生在 AdminRoster.refresh 的 str(u) 之前，只能在这层兜。
        """
        if isinstance(value, list):
            return [str(item) for item in value]
        return value

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


class ToolSection(PluginConfigBase):
    """主动沉默工具配置。"""

    __ui_label__ = "主动沉默工具"

    enabled: bool = Field(
        default=True,
        description="是否允许麦麦通过 LLM 工具主动进入沉默",
        json_schema_extra={"label": "启用主动沉默工具"},
    )
    default_duration_seconds: int = Field(
        default=600,
        description="工具未指定时长时的默认沉默时间（秒）",
        ge=_MUTE_DURATION_MIN_SECONDS,
        le=_MUTE_DURATION_MAX_SECONDS,
        json_schema_extra={
            "label": "默认沉默时长（秒）",
            "hint": "60 ~ 86400 秒",
            "x-widget": "slider",
            "min": _MUTE_DURATION_MIN_SECONDS,
            "max": _MUTE_DURATION_MAX_SECONDS,
            "step": 60,
        },
    )
    max_duration_seconds: int = Field(
        default=_TOOL_MUTE_MAX_DEFAULT_SECONDS,
        description="工具可主动沉默的最大时长（秒）",
        ge=_MUTE_DURATION_MIN_SECONDS,
        le=_MUTE_DURATION_MAX_SECONDS,
        json_schema_extra={
            "label": "主动沉默最大时长（秒）",
            "hint": "建议不要过大，避免模型误判后长时间沉默",
            "x-widget": "slider",
            "min": _MUTE_DURATION_MIN_SECONDS,
            "max": _MUTE_DURATION_MAX_SECONDS,
            "step": 60,
        },
    )

    @field_validator("default_duration_seconds", "max_duration_seconds", mode="before")
    @classmethod
    def _clamp_tool_duration(cls, value: Any) -> Any:
        """工具时长配置沿用静音时长的容错收敛策略。"""
        try:
            number = int(value)
        except (TypeError, ValueError):
            return value
        return min(max(number, _MUTE_DURATION_MIN_SECONDS), _MUTE_DURATION_MAX_SECONDS)


class GroupMuterConfig(PluginConfigBase):
    """群聊静音插件完整配置。"""

    plugin: PluginSection = Field(default_factory=PluginSection)
    mute: MuteSection = Field(default_factory=MuteSection)
    user_control: UserControlSection = Field(default_factory=UserControlSection)
    tool: ToolSection = Field(default_factory=ToolSection)


# --- 核心状态管理器 ---

class MuteSessionTracker:
    """群聊静音的多群会话跟踪器（实例字段，禁止做类变量）。

    每个 GroupMuterPlugin 实例持有一个 tracker 实例。曾经的类变量
    在 SDK"预热新实例 → 原子切换 → unload 旧实例"热重载时序下
    会被旧实例的 on_unload clear() 抹掉新实例状态。改成实例字段后自然隔离。

    持有的状态：
        * 每群剩余静音时长（group_id → expire_at）
        * 群名缓存（日志友好显示）
        * 每群 stream_id（出站消息缺 group_info 时按 session_id 回退识别）
        * 单次性发送豁免令牌（group_id → {预期文本: 截止时间}，
          绑定文本防 bot 在途回复抢消费；多键共存防续期顶掉在途令牌）
        * 后台摘要任务（周期性输出仍静音群的剩余时间）
        * 每群消息驱动日志的节流时间戳

    热重载 → 状态丢失是接受的行为，见 docs/adr/0001-mute-tracker-no-persistence.md。
    """

    def __init__(self) -> None:
        self._mute_until: Dict[str, float] = {}
        self._group_names: Dict[str, str] = {}
        self._stream_ids: Dict[str, str] = {}  # group_id → stream_id（出站守卫的回退匹配键）
        self._send_exempt: Dict[str, Dict[str, float]] = {}  # group_id → {预期文本: 豁免截止时间戳}
        self._summary_task: Optional[asyncio.Task] = None  # 后台摘要日志定时任务
        self._last_msg_log_time: Dict[str, float] = {}  # 消息驱动日志的节流时间戳

    def display_name(self, group_id: str) -> str:
        """返回日志显示用的群名；缺失时回退为 group_id。"""
        return self._group_names.get(group_id, group_id)

    def set_send_exempt(self, group_id: str, expected_text: str, seconds: float = 10.0) -> None:
        """记录一个单次性发送豁免令牌，绑定**预期文本**。

        令牌按"群 + 预期文本"多键共存：``consume_exempt`` 只有在出站文本与
        某个预期文本匹配时才消耗该键并放行。仅按群存的旧设计有一个高概率
        竞争——管理员发静音指令时，bot 往往正有一条已进入思考流程的在途回复
        （bot 话多才需要静音），它若先到 before_send 就会抢走令牌被放行，
        而控制消息反被自己的守卫拦下。绑定文本后在途回复抢不走令牌。
        多键共存解决另一个窄窗口：start 的控制消息还在途时管理员立刻续期，
        renew 的令牌不再顶掉 mute_reply 的令牌。同一文本重复入队只刷新
        过期时间（仍只放行一条）——比误拦好，且窗口极窄。

        ``seconds`` 内一直没人来取则按时间过期被丢弃。默认 10 秒：
        从 set_send_exempt 到本插件的 before_send 守卫被调度，中间隔着
        send RPC 往返、after_build_message 上所有插件的 BLOCKING handler
        串行（每个默认超时 5000ms）、以及 before_send 链上排在本插件前面的
        内置 handler——控制消息又是 create_task 后台发送，1.5 秒窗口很容易
        被单个慢 hook 吃掉。

        已知边界：排在本插件前面的 hook 若改写了消息文本，比对会失配 →
        控制消息被拦（fail-closed，与无令牌时的失败模式相同），令牌留到过期。
        """
        tokens = self._send_exempt.setdefault(group_id, {})
        tokens[expected_text.strip()] = time.time() + seconds

    def consume_exempt(self, group_id: str, outbound_text: str) -> bool:
        """检查并**消耗**豁免令牌。

        先剔除该群所有已过期的令牌；``outbound_text`` 与某个未过期的预期
        文本匹配 → 消耗该键并返 True；不匹配（如 bot 自己的在途回复）→
        返 False 且**保留其余令牌**，等真正的控制消息来取。
        """
        tokens = self._send_exempt.get(group_id)
        if not tokens:
            return False
        now = time.time()
        for text in [t for t, expire_at in tokens.items() if now >= expire_at]:
            tokens.pop(text, None)
        key = (outbound_text or "").strip()
        matched = tokens.pop(key, None) is not None
        if not tokens:
            self._send_exempt.pop(group_id, None)
        return matched

    def set_mute(
        self,
        group_id: str,
        seconds: int,
        group_name: Optional[str] = None,
        stream_id: Optional[str] = None,
    ) -> None:
        """开启指定群的静音状态，并启动后台摘要日志任务。

        ``stream_id`` 供出站守卫做回退匹配：Host 构建出站消息时只有解析到
        非空 group_name 才会填 group_info，群名缺失时出站 dict 里没有
        group_id，只能靠 session_id 识别目标群。
        """
        self._mute_until[group_id] = time.time() + seconds
        if group_name:
            self._group_names[group_id] = group_name
        if stream_id:
            self._stream_ids[group_id] = stream_id
        logger.info(f"[{group_name or group_id}] 进入静音模式，持续 {seconds} 秒。")
        # 确保后台摘要日志任务正在运行
        self._ensure_summary_task()

    def clear_mute(self, group_id: str, *, expired: bool = False) -> None:
        """终结指定群的静音会话——**唯一的会话终结实现**。

        手动解除与超时自动解除都必须走这里（``expired`` 仅影响日志措辞），
        给"会话终结"加新行为（通知、清理等）只改本方法。
        对未静音的群调用是无害的空操作。
        """
        if self._mute_until.pop(group_id, None) is not None:
            group_name = self._group_names.pop(group_id, None)
            self._stream_ids.pop(group_id, None)
            self._send_exempt.pop(group_id, None)
            self._last_msg_log_time.pop(group_id, None)
            display_name = group_name or group_id
            if expired:
                logger.info(f"[{display_name}] 静音时间已到，自动解除。")
            else:
                logger.info(f"[{display_name}] 已解除静音模式。")

    def is_muted(self, group_id: str) -> bool:
        """检查指定群是否处于静音状态，超时则自动解除。"""
        mute_end_time = self._mute_until.get(group_id)
        if mute_end_time and time.time() >= mute_end_time:
            self.clear_mute(group_id, expired=True)
            return False
        return bool(mute_end_time)

    def remaining_seconds(self, group_id: str) -> int:
        """返回指定群的剩余静音秒数。"""
        mute_end_time = self._mute_until.get(group_id)
        if mute_end_time:
            return max(int(mute_end_time - time.time()), 0)
        return 0

    def group_for_stream(self, stream_id: str) -> str:
        """按 session_id 反查静音群的 group_id；未命中返回空串。

        出站守卫的回退路径：出站消息缺 group_info 时用 session_id 识别。
        静音中的群通常只有个位数，线性扫描即可。
        """
        if not stream_id:
            return ""
        for group_id, known_stream_id in self._stream_ids.items():
            if known_stream_id == stream_id:
                return group_id
        return ""

    def _ensure_summary_task(self) -> None:
        """确保后台摘要日志任务正在运行。如果已停止或未启动，则创建新任务。"""
        if self._summary_task is None or self._summary_task.done():
            self._summary_task = asyncio.create_task(self._summary_loop())

    async def _summary_loop(self) -> None:
        """后台定时任务：每 30 秒打印一次所有静音群的状态摘要。

        纯观察者——发现过期只调用 ``clear_mute``，不持有任何会话终结知识。
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

                # 终结已过期的会话（唯一实现在 clear_mute）
                for group_id in expired_groups:
                    self.clear_mute(group_id, expired=True)

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
        except Exception:
            # 任务死亡不影响核心功能（is_muted 惰性过期兜底），但不记日志
            # 异常会埋到 GC 时才以 "exception was never retrieved" 浮出
            logger.exception("摘要日志任务异常退出，下次 set_mute 时自动重建")

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
    """检查文本（去除 CQ 码 / @前缀后）是否匹配关键词列表中的某个词。

    主路径是剥掉 ``[CQ:at,...]`` 与 ``@\\S+`` 后精确匹配；剥后为空串
    （纯 @ 消息）不参与匹配——配置层 ``_drop_blank_keywords`` 已剔除空
    关键词，这里是对称防御。但 at 段在
    processed_plain_text 中渲染为 "@群名片"，QQ 群名片可含空格，
    ``@\\S+`` 剥不干净（如 "@张 三 Mute True" 剥后残留 "三 Mute True"）。
    兜底：剥掉 CQ 码后若以 "@" 开头，允许"以关键词结尾"的后缀匹配——
    仅对 @ 开头的消息放宽，普通聊天文本仍要求精确匹配。
    """
    if not text or not keywords:
        return False
    stripped = _strip_at_prefix(text)
    if stripped and stripped in keywords:
        return True
    no_cq = _CQ_AT_PATTERN.sub("", text).strip()
    if no_cq.startswith("@"):
        return any(no_cq.endswith(keyword) for keyword in keywords if keyword)
    return False


@dataclass(frozen=True)
class MessageContext:
    """从序列化的 SessionMessage 字典提取的关键字段集合（入站/出站共用）。

    "如何从 message dict 挖字段"的知识单点集中在 ``from_message``；
    出站消息缺少 plain_text / user_id 等字段时安全回退为空串，调用方按需读取。

    ``is_bot_mentioned`` 依赖 adapter 在入站时正确设置 ``is_at`` / ``is_mentioned``，
    语义为"@ 到 bot 自己"。napcat adapter 已实现该契约（仅当
    target_user_id == self_id 时设 True）；其它 adapter 若未实现，本插件的
    @ 解除静音功能会降级——但关键词解除仍可用。历史上的 segments / raw_content
    fallback 把"@任何人"误判为"@ bot"，已删除，
    详见 docs/adr/0002-trust-adapter-is-at-fields.md。
    """

    plain_text: str = ""
    stream_id: str = ""
    group_id: str = ""
    group_name: str = ""
    user_id: str = ""
    is_bot_mentioned: bool = False

    @classmethod
    def from_message(cls, message: Optional[dict]) -> "MessageContext":
        """从消息字典构造；非法输入返回全空上下文（group_id 为空 → 调用方透传）。"""
        if not message or not isinstance(message, dict):
            return cls()

        # processed_plain_text 用于关键词匹配（已去除 CQ 码）；
        # Host 序列化层只输出 processed_plain_text 这一个纯文本键
        plain_text = message.get("processed_plain_text") or ""

        msg_info = message.get("message_info") or {}
        user_info = msg_info.get("user_info") or {}
        group_info = msg_info.get("group_info") or {}

        return cls(
            plain_text=str(plain_text),
            stream_id=str(message.get("session_id") or ""),
            group_id=str(group_info.get("group_id") or ""),
            group_name=str(group_info.get("group_name") or ""),
            user_id=str(user_info.get("user_id") or ""),
            is_bot_mentioned=bool(message.get("is_at") or message.get("is_mentioned")),
        )


# --- 权限判定 ---


class AdminRoster:
    """管理员名册：单一持有"名单类型 + 用户集合 + 白/黑名单判定语义"。

    ``refresh`` 在 on_load / on_config_update 时从配置重建缓存集合，
    ``is_admin`` 是运行时热路径。"_user_set 必须与 config.user_control.list
    保持同步"这条不变量从插件生命周期回调收回到本类内——调用方只接触
    一个判定入口，不再需要同时传 user_control + user_set 两个耦合参数。
    """

    def __init__(self) -> None:
        self._list_type: Literal["whitelist", "blacklist"] = "whitelist"
        self._user_set: set[str] = set()

    def refresh(self, user_control: "UserControlSection") -> None:
        """从配置节重建判定缓存。"""
        self._list_type = user_control.list_type
        self._user_set = {str(u) for u in user_control.list}

    def is_admin(self, user_id: str) -> bool:
        """判定用户是否拥有静音操作权限；空 user_id 一律视为无权限。"""
        if not user_id:
            return False
        uid = str(user_id)
        if self._list_type == "whitelist":
            return uid in self._user_set
        return uid not in self._user_set


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
        "renew_mute",
        "refuse_start",
        "end_mute",
        "intercept_while_muted",
    ]
    group_id: str = ""
    group_name: str = ""
    stream_id: str = ""
    duration_seconds: Optional[int] = None


class MuteIntentResolver:
    """纯函数式：(context, muted, is_admin, mute_config) → MuteIntent。

    输入全部是值（MessageContext + 两个标量 + 配置节），不读写任何状态、
    不调任何 ``ctx.*`` RPC——tracker 查询与管理员判定由 handler 在调用前完成。
    所有副作用（开/解除静音、发消息、日志）由
    ``GroupMuterPlugin._dispatch_intent`` 执行。

    这样决策树可独立单测（喂 MessageContext + bool + 配置，断言 intent，
    不必构造 tracker）；新增规则只在 ``resolve`` 里加一条 if 返回新 kind，
    副作用在 dispatch 加一个 elif 即可——主路径（handler）不必再碰。
    """

    @staticmethod
    def resolve(
        *,
        context: MessageContext,
        muted: bool,
        is_admin: bool,
        mute_config: "MuteSection",
    ) -> MuteIntent:
        # 非群聊 → 透传
        if not context.group_id:
            return MuteIntent(kind="pass_through")

        # --- 未静音 ---
        if not muted:
            if _is_keyword_in_text(context.plain_text, mute_config.mute_keywords):
                if is_admin:
                    return MuteIntent(
                        kind="start_mute",
                        group_id=context.group_id,
                        group_name=context.group_name,
                        stream_id=context.stream_id,
                    )
                return MuteIntent(
                    kind="refuse_start",
                    group_id=context.group_id,
                    stream_id=context.stream_id,
                )
            return MuteIntent(kind="pass_through")

        # --- 已静音 ---
        # 判定顺序敏感：解除关键词 → 静音关键词（续期）→ 纯 @ 解除。
        # @ 检查必须放在关键词之后——管理员习惯用 "@bot Mute True" 发指令，
        # 若 @ 解除先判，这条消息在未静音时是 start_mute、已静音时却变成
        # end_mute，同一条消息两种状态下语义相反。只有"@ 了 bot 且不含
        # 任何关键词"才走 @ 解除。
        if (
            is_admin
            and mute_config.enable_unmute
            and _is_keyword_in_text(context.plain_text, mute_config.unmute_keywords)
        ):
            return MuteIntent(
                kind="end_mute",
                group_id=context.group_id,
                stream_id=context.stream_id,
            )
        if is_admin and _is_keyword_in_text(context.plain_text, mute_config.mute_keywords):
            # 静音中管理员再发静音关键词 → 续期（重置计时），不再静默吞掉
            return MuteIntent(
                kind="renew_mute",
                group_id=context.group_id,
                group_name=context.group_name,
                stream_id=context.stream_id,
            )
        if is_admin and mute_config.at_mention_break and context.is_bot_mentioned:
            return MuteIntent(
                kind="end_mute",
                group_id=context.group_id,
                stream_id=context.stream_id,
            )

        return MuteIntent(kind="intercept_while_muted", group_id=context.group_id)


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
        self._admin_roster = AdminRoster()  # 权限判定缓存，on_load / on_config_update 时 refresh
        # 实例字段而非类变量：避免热重载时旧实例 on_unload 抹掉新实例状态
        self._mute_status = MuteSessionTracker()
        # 后台控制消息发送任务的强引用：create_task 后不存引用会被 GC 提前回收
        self._send_tasks: set[asyncio.Task] = set()
        # refuse_start 拒绝回复的同群节流时间戳
        self._refuse_reply_last: Dict[str, float] = {}

    async def on_load(self) -> None:
        self._admin_roster.refresh(self.config.user_control)
        logger.info("群聊静音插件(v%s)初始化完成。", PLUGIN_VERSION)

    async def on_unload(self) -> None:
        """插件卸载时取消后台任务。

        实例字段随插件 GC 自动回收，无需手动 clear；但 cancel_summary_task()
        必须 await——_summary_loop 的 bound method 持 self 强引用，
        不取消会让旧实例无法被 GC；cancel() 是异步信号，必须 await 等 task
        真正退出，否则 Runner 释放实例后 task 仍可能触达已失效字段。
        后台控制消息发送任务同理。
        """
        await self._mute_status.cancel_summary_task()
        for task in list(self._send_tasks):
            task.cancel()
        if self._send_tasks:
            await asyncio.gather(*self._send_tasks, return_exceptions=True)
        self._send_tasks.clear()

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        """配置热重载回调。"""
        if scope == "self":
            self._admin_roster.refresh(self.config.user_control)

    @Tool(
        "silence",
        description=(
            "让麦麦在当前群聊主动进入沉默状态，之后该群消息会被静音守卫拦截，"
            "直到超时或管理员解除。适合觉得自己话太多、气氛不适合继续发言、"
            "或用户礼貌要求安静一段时间时调用。"
        ),
        parameters=[
            ToolParameterInfo(
                name="stream_id",
                param_type=ToolParamType.STRING,
                description="当前聊天流 ID。通常由上下文提供，必须是群聊聊天流。",
                required=True,
            ),
            ToolParameterInfo(
                name="duration_seconds",
                param_type=ToolParamType.INTEGER,
                description="沉默时长（秒），可选；不填使用插件默认值，过大会按配置上限收敛。",
                required=False,
                default=None,
            ),
            ToolParameterInfo(
                name="reason",
                param_type=ToolParamType.STRING,
                description="主动沉默原因，便于日志记录；不会发送到群里。",
                required=False,
                default="",
            ),
        ],
    )
    async def handle_silence_tool(
        self,
        stream_id: str,
        duration_seconds: Optional[int] = None,
        reason: str = "",
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """LLM 工具入口：主动进入沉默。"""
        return await self._handle_active_mute_tool(
            stream_id=stream_id,
            duration_seconds=duration_seconds,
            reason=reason,
            tool_name="silence",
            **kwargs,
        )

    @Tool(
        "sleep",
        description=(
            "silence 的别名：让麦麦在当前群聊主动休眠/沉默指定时长。"
            "当你判断自己应暂时不参与聊天时调用。"
        ),
        parameters=[
            ToolParameterInfo(
                name="stream_id",
                param_type=ToolParamType.STRING,
                description="当前聊天流 ID。通常由上下文提供，必须是群聊聊天流。",
                required=True,
            ),
            ToolParameterInfo(
                name="duration_seconds",
                param_type=ToolParamType.INTEGER,
                description="休眠/沉默时长（秒），可选；不填使用插件默认值，过大会按配置上限收敛。",
                required=False,
                default=None,
            ),
            ToolParameterInfo(
                name="reason",
                param_type=ToolParamType.STRING,
                description="主动休眠原因，便于日志记录；不会发送到群里。",
                required=False,
                default="",
            ),
        ],
    )
    async def handle_sleep_tool(
        self,
        stream_id: str,
        duration_seconds: Optional[int] = None,
        reason: str = "",
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """LLM 工具入口：sleep 是 silence 的语义别名。"""
        return await self._handle_active_mute_tool(
            stream_id=stream_id,
            duration_seconds=duration_seconds,
            reason=reason,
            tool_name="sleep",
            **kwargs,
        )

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

        Host 只在解析到非空 group_name 时才给出站消息填 group_info，
        group_id 取不到时按 session_id 回退识别目标群，避免拦截静默失效。

        error_policy=SKIP 意味着本 handler 自身异常/超时是 **fail-open**：
        Host 跳过本 handler 继续发送，静音期间漏发一条。换 ErrorPolicy.ABORT
        可得真 fail-closed（Host 端异常时置 aborted），但代价是本 handler
        出 bug 时会拦掉**所有群**的全部出站消息。当前 handler 全内存操作、
        几乎不可能异常，维持 SKIP 是有意的权衡。
        """
        context = MessageContext.from_message(message)
        group_id = context.group_id or self._mute_status.group_for_stream(context.stream_id)
        if not group_id:
            return None

        if self._mute_status.is_muted(group_id):
            # 检查并消耗豁免令牌：绑定预期文本，bot 的在途回复抢不走
            if self._mute_status.consume_exempt(group_id, context.plain_text):
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

        薄入口：提取上下文、查 tracker、查权限名册，把决策完全委托给
        MuteIntentResolver（纯解析），handler 自己只做 intent → 副作用的
        1:1 派发。新增静音规则的扩展点在 resolver 而非这里。

        is_muted 在此处调用（而非 resolver 内部）——它自带幂等的过期清理
        副作用，收在 handler 层让 resolver 保持纯函数。
        """
        context = MessageContext.from_message(message)

        intent = MuteIntentResolver.resolve(
            context=context,
            muted=self._mute_status.is_muted(context.group_id),
            is_admin=self._admin_roster.is_admin(context.user_id),
            mute_config=self.config.mute,
        )
        return await self._dispatch_intent(intent)

    async def _dispatch_intent(self, intent: MuteIntent) -> Optional[Dict[str, Any]]:
        """根据 intent 执行副作用（修改 mute 状态 + 发回复 + 日志）。

        回复一律 ``_spawn_control_send`` 后台发送、本方法立即返回 abort：
        宿主端 send.text 会同步等完整发送管线（after_build_message /
        before_send 全部 BLOCKING hook + 平台 IO 投递），若在此 await，
        全程计入入站 hook 的 timeout_ms 预算；一旦超时，ErrorPolicy.SKIP
        会丢弃本 handler 的返回值——abort 丢失，触发词消息漏入主链。
        """
        if intent.kind == "pass_through":
            return None

        logger.debug(
            "[mute_guard] intent=%s group=%s", intent.kind, intent.group_id or "<n/a>",
        )

        if intent.kind in ("start_mute", "renew_mute"):
            reply = (
                self.config.mute.mute_reply
                if intent.kind == "start_mute"
                else self.config.mute.renew_reply
            )
            duration_seconds = intent.duration_seconds or self.config.mute.duration_seconds
            self._mute_status.set_mute(
                intent.group_id,
                duration_seconds,
                intent.group_name or None,
                stream_id=intent.stream_id or None,
            )
            self._mute_status.set_send_exempt(intent.group_id, reply)
            self._spawn_control_send(reply, intent.stream_id, label=intent.kind)
            return {"action": "abort"}

        if intent.kind == "refuse_start":
            # 拒绝回复做同群节流防刷屏；拦截本身不节流
            if self._should_send_refusal(intent.group_id):
                self._spawn_control_send(
                    self.config.mute.no_permission_reply,
                    intent.stream_id,
                    label="refuse_start",
                )
            return {"action": "abort"}

        if intent.kind == "end_mute":
            self._mute_status.clear_mute(intent.group_id)
            self._refuse_reply_last.pop(intent.group_id, None)
            self._spawn_control_send(
                self.config.mute.unmute_reply, intent.stream_id, label="end_mute",
            )
            return {"action": "abort"}

        if intent.kind == "intercept_while_muted":
            self._mute_status.log_on_message(intent.group_id)
            return {"action": "abort"}

        return None

    async def _handle_active_mute_tool(
        self,
        *,
        stream_id: str,
        duration_seconds: Optional[int],
        reason: str,
        tool_name: str,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """处理 silence / sleep 工具调用。"""
        if not self.config.tool.enabled:
            return {"success": False, "message": "主动沉默工具已在配置中关闭"}

        context = await self._resolve_tool_context(stream_id=stream_id, **kwargs)
        if not context.stream_id:
            return {"success": False, "message": "缺少 stream_id，无法确定当前聊天流"}
        if not context.group_id:
            return {"success": False, "message": "主动沉默工具只能在群聊聊天流中使用"}

        seconds = self._coerce_tool_duration(duration_seconds)
        was_muted = self._mute_status.is_muted(context.group_id)
        self._mute_status.set_mute(
            context.group_id,
            seconds,
            context.group_name or None,
            stream_id=context.stream_id,
        )

        logger.info(
            "[%s] 麦麦通过 %s 工具主动进入沉默，持续 %s 秒。reason=%s",
            context.group_name or context.group_id,
            tool_name,
            seconds,
            (reason or "").strip() or "<empty>",
        )
        return {
            "success": True,
            "message": "已进入沉默状态" if not was_muted else "已续期沉默状态",
            "stream_id": context.stream_id,
            "group_id": context.group_id,
            "duration_seconds": seconds,
            "remaining_seconds": self._mute_status.remaining_seconds(context.group_id),
        }

    def _coerce_tool_duration(self, duration_seconds: Optional[int]) -> int:
        """将工具传入时长收敛到配置允许范围。"""
        default_seconds = int(self.config.tool.default_duration_seconds)
        max_seconds = int(self.config.tool.max_duration_seconds)
        if duration_seconds in (None, ""):
            seconds = default_seconds
        else:
            try:
                seconds = int(duration_seconds)
            except (TypeError, ValueError):
                seconds = default_seconds
        seconds = min(max(seconds, _MUTE_DURATION_MIN_SECONDS), max_seconds)
        return min(seconds, _MUTE_DURATION_MAX_SECONDS)

    async def _resolve_tool_context(self, stream_id: str, **kwargs: Any) -> MessageContext:
        """从工具上下文与 Chat 能力中解析群聊上下文。"""
        sid = str(stream_id or kwargs.get("stream_id") or kwargs.get("session_id") or "")
        group_id = str(kwargs.get("group_id") or "")
        group_name = str(kwargs.get("group_name") or "")

        if not group_id:
            group_id = self._mute_status.group_for_stream(sid)

        if sid and not group_id:
            try:
                streams = await self.ctx.chat.get_group_streams()
            except Exception:
                logger.warning("[mute_guard] 查询群聊流失败，无法解析主动沉默目标", exc_info=True)
                streams = []
            for stream in streams or []:
                if not isinstance(stream, dict):
                    continue
                stream_sid = str(stream.get("session_id") or stream.get("stream_id") or "")
                if stream_sid != sid:
                    continue
                group_info = stream.get("group_info") or {}
                group_id = str(
                    stream.get("group_id")
                    or group_info.get("group_id")
                    or stream.get("target_id")
                    or ""
                )
                group_name = str(
                    stream.get("group_name")
                    or group_info.get("group_name")
                    or stream.get("name")
                    or group_name
                    or ""
                )
                break

        return MessageContext(stream_id=sid, group_id=group_id, group_name=group_name)

    def _should_send_refusal(self, group_id: str) -> bool:
        """拒绝回复的同群节流：冷却期内返回 False，否则记录时间戳并放行。"""
        now = time.time()
        if now - self._refuse_reply_last.get(group_id, 0.0) < _REFUSE_REPLY_COOLDOWN_SECONDS:
            return False
        self._refuse_reply_last[group_id] = now
        return True

    def _spawn_control_send(self, text: str, stream_id: str, *, label: str) -> None:
        """后台发送控制消息，与入站 hook 的决策返回解耦。

        任务引用存入 ``_send_tasks`` 防 GC，完成后自动移除；
        on_unload 统一取消未完成的任务。
        """
        task = asyncio.create_task(
            self._send_control_message(text, stream_id, label=label)
        )
        self._send_tasks.add(task)
        task.add_done_callback(self._send_tasks.discard)

    async def _send_control_message(self, text: str, stream_id: str, *, label: str) -> None:
        """实际执行控制消息发送，并检查结果。

        send.text 失败（含被 before_send hook 中止）时返回 False 而不抛异常，
        不检查就会静默丢失。
        """
        try:
            ok = await self.ctx.send.text(text, stream_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "[mute_guard] %s 控制消息发送异常 (stream=%s)", label, stream_id,
                exc_info=True,
            )
            return
        if not ok:
            logger.warning(
                "[mute_guard] %s 控制消息发送失败，send.text 返回 False (stream=%s)",
                label, stream_id,
            )


def create_plugin() -> GroupMuterPlugin:
    """创建群聊静音插件实例。"""
    return GroupMuterPlugin()
