# 群聊静音插件 (GroupMuter Plugin)

🤫 **一个允许管理员通过聊天命令，让麦麦在指定群聊中临时进入"静音状态"的群组管理插件。**

这个插件为群组管理员提供了一个强大的工具来让麦麦“静音”（闭嘴）。当群聊需要专注讨论或减少麦麦干扰时，管理员可以一键“静音”麦麦。默认情况下，静音期间麦麦会进入纯窥屏状态：继续读取群聊上下文，但通过发言频率 0 和出站守卫避免发言；关闭 `mute.learn_while_muted` 后才会直接拦截普通群消息，直到被管理员唤醒或到达静音时间自动解除静音。

> 本版本基于 **MaiBot SDK v2** 重写，使用 `@HookHandler` 装饰器实现入站/出站双重拦截，并注册 `silence` Tool 供麦麦主动进入沉默，配合 `PluginConfigBase` 强类型配置模型，支持配置热重载和 Web UI 配置。

## ✨ 功能特性

- **动态控制**: 无需重启，通过简单的聊天命令即可实时开启或关闭麦麦的静音模式。
- **双重拦截**:
  - **入站拦截**: 通过 `chat.receive.after_process` Hook 在消息预处理后拦截，阻止消息进入后续流程。
  - **出站拦截**: 通过 `send_service.before_send` Hook 拦截静音前已进入思考流程、但在静音后才生成回复的“漏网”消息。
- **主动沉默工具**: 注册 `silence` LLM Tool，麦麦可在判断自己不适合继续发言时主动让当前群聊进入沉默。
- **多种唤醒方式**:
  - **命令唤醒**: 管理员使用特定关键词即可主动唤醒。
  - **@提及唤醒**: 管理员在群里 `@` 麦麦，即可立即唤醒麦麦。
  - **自动解除**: 静音时间到达后，麦麦将自动解除静音，恢复正常聊天。
- **高度可配置**: 静音时长、触发关键词、麦麦的回复语均可在配置文件或 Web UI 中轻松修改。
- **配置热重载**: 通过 Web UI 修改配置后无需重启，插件会自动应用新配置。

---

## 🚀 快速开始

### 1. 安装

- 手动安装：下载 `group_muter_plugin` 文件夹放入麦麦主程序的 `plugins` 目录下，然后重启主程序即可完成插件的注册和加载。
- 自动安装：通过 Web UI 在插件市场下载安装

### 2. 环境要求

- **MaiBot 主程序**: v1.3.0+（本次以本地主程序源码为兼容基线，不再声明旧 Host 支持）
- **MaiBot SDK**: v2.8.1+，使用明确的 `task_name` 模型任务路由及发送详情接口。
- **NapCat / SnowLuma 适配器**：仅可选的 QQ 群角色授权需要（两者都提供 `adapter.napcat.group.get_group_member_info`，返回形态差异已在插件内兼容）；默认名单授权不依赖适配器 API。

### 3. 配置

首次启动麦麦后，插件会在其目录下自动生成 `config.toml` 文件。配置管理员权限后重启麦麦主程序即可。你也可以通过 **Web UI** 在线修改配置，修改后会自动热重载生效。

**⚠️ 重要安全提示**:

- **`user_control.list`**: 这是一个**核心安全设置**。
  - 默认值为一个**空列表 `[]`**，这意味着**默认情况下，没有任何人是管理员**。
  - **您必须手动编辑此文件**（或通过 Web UI），将管理员的 QQ 号（字符串格式）添加到这个列表中，才能使用本插件的命令。例如：`list = ["12345", "67890"]`。
- **`user_control.list_type`**: 定义了权限模式。`"whitelist"` 表示只有 `list` 中的人是管理员；`"blacklist"` 表示除了 `list` 中的人，其他人都是管理员。**⚠️ 黑名单模式下如果 `list` 为空，则所有人都可操作静音（含主动沉默关键词）；插件会在加载时打 warning 提醒。想要“默认无人可控”请使用 `whitelist`。**

---

## 📖 使用指南

### 开启静音

管理员在群聊中发送配置的静音关键词即可开启静音模式。

- **默认关键词**: `Mute True` 或 `安安你去看书去`
- **麦麦回复**: 由 `mute.mute_reply` 配置（默认 `好吧，那我去看会书📘，你们先聊...`）
- **非管理员尝试**: 麦麦会回复 `mute.no_permission_reply`（默认 `？？？你在教我做事🤡`）并拒绝操作。同一群内拒绝回复有 30 秒冷却（防止刷屏），冷却期间消息仍会被拦截，只是不再重复回复。

### 续期静音

静音期间，管理员再次发送静音关键词即可**重置静音计时**（重新计满 `duration_seconds`）。

- **麦麦回复**: 由 `mute.renew_reply` 配置（默认 `好哦，那我再多看一会书📘`）

### 主动沉默（silence 工具）

插件会向 MaiBot 注册 `silence` Tool。麦麦可以在以下场景自主调用：

- 觉得自己话太多，应该暂时少参与讨论。
- 群聊气氛不适合继续插话。
- 用户礼貌要求麦麦安静一段时间。

工具参数：

- `stream_id`：当前聊天流 ID，通常由系统上下文自动注入、一般无需填写；必须是群聊聊天流。
- `duration_seconds`：沉默时长（秒），可选；不填使用 `tool.default_duration_seconds`，超过 `tool.max_duration_seconds` 会自动收敛。
- `reason`：主动沉默原因，可选，仅用于日志，不会发送到群里。

**权限控制**：默认任何群成员都可以通过对话让麦麦自行判断是否主动沉默（符合“可被礼貌请求安静”的设计）。本次没有修改 Host；当前 Host 未向工具提供不可被模型参数覆盖的调用者身份。因此 `tool.require_admin=true` 时，插件会保守拒绝所有主动沉默工具调用，并提示使用管理员关键词，而不是信任模型传入的 QQ 号。关键词入口仍基于实际入站消息检查权限，不受影响。

**目标校验**：工具只信任 Host 注入的 `stream_id`，并按它反查 Host 的真实群聊流来确定目标群；流不存在、不是群聊、或调用参数里的 `group_id` 与反查结果不一致时一律拒绝执行。这保证目标字段相互一致，但不等于证明模型指定的目标就是本轮对话；彻底绑定当前会话仍需 Host 提供可信执行上下文。需要严格控制时，关闭工具或开启 `require_admin`，通过关键词操作。

工具成功时返回 `stop_after_execution=true`，Host 会在本批工具执行完后结束本轮 Planner 推理，避免模型继续生成一条注定被出站守卫拦下的回复；该字段不会立即跳过同批后续工具。

主动沉默只修改静音状态，不额外发送提示消息；之后该群会按本插件静音守卫逻辑处理（默认纯窥屏：继续读取上下文但不发言，出站回复一律拦截），直到超时或管理员解除。

### 解除静音

有三种方式可以解除静音：

#### 方式一：关键词解除

管理员发送配置的解除关键词。

- **默认关键词**: `Mute False` 或 `安安别看了`
- **麦麦回复**: 由 `mute.unmute_reply` 配置（默认 `我回来啦，你们聊啥呢🤔`）

#### 方式二：@提及解除

管理员在群里 `@` 麦麦，即可立即解除静音。

- **麦麦回复**: 同样使用 `mute.unmute_reply`
- **注意**: 消息中同时含关键词时按关键词处理——例如 `@麦麦 Mute True` 是续期而非解除，只有"@ 了麦麦且不含任何关键词"才走 @ 解除。

#### 方式三：自动超时解除

静音时间到达后（默认 1200 秒 = 20 分钟），麦麦将自动解除静音，恢复正常聊天。

### ⚠️ 注意事项

- **默认启用 `mute.learn_while_muted`，静音期间普通群消息会交给上游纯窥屏模式处理**。插件会把当前聊天流的发言频率调整值临时设为 `0`，让麦麦继续读取上下文但不主动回复；解除静音、自动超时或插件卸载时会恢复原调整值。
- **关闭 `mute.learn_while_muted` 后，静音期间的群消息不会写入聊天历史**。此时插件会在消息入库前直接入站拦截，因此解除静音后，麦麦对静音期间的群聊内容是"失忆"的（没有这段上下文），触发静音的指令消息本身也不留痕。这是强拦截模式的已接受权衡，不是 bug。
- **静音拦截的是该群的所有出站消息**。出站守卫工作在 `send_service.before_send`，静音期间凡经发送服务发往该群的消息都会被拦下——包括同环境其他插件主动发送的消息，不只是麦麦对群聊的回复。
- 默认启用 `mute.persist_sessions`，静音会话写入 `ctx.paths.data_dir` 下的 `mute_sessions.json`，重启/重载后恢复未到期会话；关闭该开关后只保留内存状态。快照通过单写入器串行保存并合并等待中的变化。卸载时会尽量恢复已记录的发言频率调整值，但不会把未到期静音会话清空。
- 极少数情况下，如果插件 Runner 单独崩溃而 MaiBot 主程序仍保持运行，Host 内存中的发言频率调整值可能停留在 `0`。如发现某群在插件异常后长期不主动发言，可重启主程序或手动重置该聊天流的发言频率。

### 🤖 LLM 动态回复

插件支持使用 LLM 动态生成回复文案，替代固定的 `mute_reply` / `renew_reply` / `unmute_reply` / `no_permission_reply`。

#### 启用方法

1. 将 `mute.llm_reply_enabled` 设为 `true`。
2. 可选：将 `mute.llm_reply_use_context` 设为 `true`，让 LLM 在生成回复时参考最近 10 条群聊消息。

#### 行为说明

- **启用后**：开启静音、续期、解除、拒绝权限四类回复使用 LLM 生成动态文案。
- **关闭时**：完全沿用现有固定回复，不调用 LLM 或消息历史接口。
- **提示词模板**：默认模板包含 `{event}`（事件说明）、`{fallback}`（原有固定回复）、`{context}`（最近消息，需开启上下文）三个占位符，可通过 `mute.llm_reply_prompt` 自定义。
- **任务路由**：通过 `task_name="replyer"` 使用 Host 的 replyer 任务，而不是指定一个叫 replyer 的具体模型。
- **超时与 fallback**：历史读取和 LLM 生成共享 30 秒总预算，超时、异常或返回空文本时自动回退到对应的固定回复。
- **过时回复**：同群只保留最新动态控制回复，发送前核对会话快照；解除、续期或过期后，旧生成结果不会重新发送。取消插件等待不代表远端模型请求一定停止计费。
- **输出约束**：LLM 输出会做兜底清理——剥去包裹引号、多行只取第一个非空行、超过 100 字符截断；清理后为空则回退固定回复。
- **上下文**：最多读取 10 条群聊消息，每条正文最多 500 字符，上下文总计最多 3000 字符；昵称最多 80 字符。读取失败不阻断生成，仍以无上下文方式继续。
- **主动沉默工具**：`silence` 工具的 `message` 字段是回给模型的工具结果而非群消息，始终使用固定提示 `已进入沉默状态` / `已续期沉默状态`，不受本功能影响。

### 配置项说明

| 配置项 | 类型 | 默认值 | 说明 |
| ------ | ---- | ------ | ---- |
| `mute.duration_seconds` | int | `1200` | 静音持续时间（秒），范围 60 ~ 86400，越界值自动收敛到边界 |
| `mute.mute_keywords` | list | `["Mute True", "安安你去看书去"]` | 触发静音的关键词列表 |
| `mute.unmute_keywords` | list | `["Mute False", "安安别看了"]` | 解除静音的关键词列表 |
| `mute.enable_unmute` | bool | `true` | 是否启用关键词解除功能 |
| `mute.at_mention_break` | bool | `true` | 管理员 @麦麦 时是否自动解除静音 |
| `mute.mute_reply` | str | `好吧，那我去看会书📘，你们先聊...` | 开启静音时麦麦的回复语 |
| `mute.renew_reply` | str | `好哦，那我再多看一会书📘` | 静音期间管理员再次发送静音关键词（续期）时的回复语 |
| `mute.unmute_reply` | str | `我回来啦，你们聊啥呢🤔` | 解除静音时麦麦的回复语（@解除与关键词解除共用） |
| `mute.no_permission_reply` | str | `？？？你在教我做事🤡` | 非管理员尝试触发静音时的拒绝回复 |
| `mute.learn_while_muted` | bool | `true` | 静音期间是否把当前聊天流发言频率调整为 `0`，让麦麦继续读取群聊上下文 |
| `mute.persist_sessions` | bool | `true` | 持久化静音会话，重启/重载恢复未到期会话 |
| `mute.llm_reply_enabled` | bool | `false` | 是否使用 LLM 动态生成回复文案（关闭时使用各固定回复字段） |
| `mute.llm_reply_prompt` | str | `根据以下事件和群聊语气...` | LLM 生成回复的提示词模板，支持 `{event}` `{fallback}` `{context}` 占位符 |
| `mute.llm_reply_use_context` | bool | `false` | LLM 生成回复时是否读取最近 10 条群聊消息作为上下文 |
| `user_control.list_type` | str | `whitelist` | 权限模式：`whitelist` 仅名单内可操作；`blacklist` 名单外可操作（空名单 = 全员可操作） |
| `user_control.list` | list | `[]` | 拥有静音操作权限的 QQ 号列表（字符串，如 `["123456"]`） |
| `user_control.authorization_mode` | str | `list` | `list` 使用名单；`group_admin` 使用 QQ 群角色；`list_or_group_admin` 任一满足即授权 |
| `user_control.role_cache_seconds` | int | `30` | QQ 群角色缓存秒数，范围 0 ~ 300，0 表示每次重新查询 |
| `tool.enabled` | bool | `true` | 是否允许麦麦通过 `silence` 工具主动进入沉默 |
| `tool.require_admin` | bool | `false` | 当前 Host 无可信工具身份契约，开启后拒绝主动沉默工具，请改用管理员关键词 |
| `tool.default_duration_seconds` | int | `600` | 工具未指定时长时的默认沉默时间（秒） |
| `tool.max_duration_seconds` | int | `10800` | 工具可主动沉默的最大时长（秒） |

> **提示**：`mute_keywords` 与 `unmute_keywords` 请勿配置相同的词。两个列表出现重叠词时，该词在静音期间会被判为“解除”而非“续期”（取决于判定顺序）；插件会在加载或配置热重载时对重叠词打印 warning 提醒。

### 可选的 QQ 群角色授权

默认 `authorization_mode="list"` 完全沿用已有名单。若改成 `group_admin`，仅允许 NapCat 返回 `owner` 或 `admin` 的 QQ 群成员操作；`list_or_group_admin` 则取名单和群角色的并集，黑名单不是角色授权的额外否决规则。

角色查询只发生在可能触发控制操作的消息上，超时或查询失败时不会授予角色权限。缓存期间群管理员变更可能延迟生效，敏感场景可将 `role_cache_seconds` 设为 `0`。角色模式不适用于非 QQ 平台；这些平台仍可使用名单模式。

### 提供给其他插件的静音查询 API

公开只读 API：`group_muter.get_status`，版本 `1.0.0`，按 `stream_id`（或 `group_id`）查询，不读取或返回管理员名单。调用方需要在自己的 `_manifest.json` 里声明 `api.call` 能力：

```python
status = await self.ctx.api.call(
    "group_muter.get_status",
    version="1.0.0",
    stream_id=current_stream_id,
)
# SDK 对 api.call 的归一化：成功时直接得到本 API 的返回字典；
# 失败（静音插件未加载、参数缺失等）才是 {"success": False, "error": "..."}。
if not isinstance(status, dict) or status.get("success") is False:
    # 查询失败不等于未静音，应按调用方自己的策略处理。
    return
if status["muted"]:
    return
```

结果包含 `muted`、`remaining_seconds`、`learn_while_muted`、`group_id`、`stream_id`。查询是纯读：不会终结已过期会话、不触发频率恢复或持久化写盘；只给 `stream_id` 时经插件已记录的映射反查，从未被静音过的流自然返回未静音。直接调用 NapCat 戳戳等 API 的插件应在实际发送前查询；提供接口不意味着其他插件已自动遵守静音。

#### 命令示例

>![命令示例](https://s21.ax1x.com/2025/11/05/pZS7YcQ.jpg)

---

## 🔄 从 v1.x 升级

v2.x 是基于 MaiBot SDK v2 的完全重写版本，主要变化如下：

| 项目 | v1.x (旧版) | v2.x (新版) |
| ---- | ----------- | ------------- |
| SDK 依赖 | `src.plugin_system` (内置插件系统) | `maibot_sdk` v2 |
| 消息拦截 | `BaseEventHandler` (ON_MESSAGE) + `BaseCommand` | `@HookHandler` 装饰器 (入站 + 出站双重拦截) |
| 配置管理 | `config_schema` 字典 + `ConfigField` | `PluginConfigBase` 强类型模型 (Pydantic) |
| 配置热重载 | 不支持 | 支持 `on_config_update` 回调 |
| 日志过滤 | 自定义 `GroupMuterLogFilter` 过滤控制台日志 | 由 SDK 统一管理 |
| 出站拦截 | 无（静音前进入思考的消息可能"漏网"） | `send_service.before_send` Hook 拦截漏网消息 |
| 主动沉默 | `BaseAction` | `@Tool("silence")` |
| 发送豁免 | 无 | 内置豁免机制，确保插件自身的控制消息不被拦截 |
| 清单文件 | `manifest_version: 1` | `manifest_version: 2`，新增 `capabilities`、`i18n` 等字段 |

**配置兼容性**: `config.toml` 配置结构基本保持一致，新增 `config_version` 字段，可直接迁移使用。新增的 `mute_reply` / `unmute_reply` / `no_permission_reply` 三个字段会使用代码内的默认值，无需手动改动旧配置。

---

## 🙏 致谢

本插件基于 [khiqwq](https://github.com/khiqwq) 的 [silent_mode_plugin](https://github.com/khiqwq/silent_mode_plugin) 插件进行二次开发和优化，我们对原作者的杰出工作表示衷心的感谢和崇高的敬意。

根据原项目的开源协议，本插件同样采用 **[GNU Affero General Public License v3.0](https://www.gnu.org/licenses/agpl-3.0.html)** 协议进行开源。

详情请参阅仓库根目录下的 `LICENSE` 文件。

Enjoy! 🎉
