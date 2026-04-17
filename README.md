# 群聊静音插件 (GroupMuter Plugin)

🤫 **一个允许管理员通过聊天命令，让麦麦在指定群聊中临时进入"静音状态"的群组管理插件。**

这个插件为群组管理员提供了一个强大的工具来让麦麦“静音”。当群聊需要专注讨论或减少麦麦干扰时，管理员可以一键“静音”麦麦。在静音模式下，麦麦将忽略所有消息，直到被管理员唤醒或到达静音时间自动解除静音。

> 本版本 (v2.0.0) 基于 **MaiBot SDK v2** 重写，使用 `@HookHandler` 装饰器实现入站/出站双重拦截，配合 `PluginConfigBase` 强类型配置模型，支持配置热重载和 Web UI 配置。

## ✨ 功能特性

- **动态控制**: 无需重启，通过简单的聊天命令即可实时开启或关闭麦麦的静音模式。
- **双重拦截**:
  - **入站拦截**: 通过 `chat.receive.after_process` Hook 在消息预处理后拦截，阻止消息进入后续流程。
  - **出站拦截**: 通过 `send_service.before_send` Hook 拦截静音前已进入思考流程、但在静音后才生成回复的“漏网”消息。
- **多种唤醒方式**:
  - **命令唤醒**: 管理员使用特定关键词即可主动唤醒。
  - **@提及唤醒**: 管理员在群里 `@` 麦麦，即可立即唤醒麦麦。
  - **自动解除**: 静音时间到达后，麦麦将自动解除静音，恢复正常聊天。
- **高度可配置**: 静音时长、触发关键词均可在配置文件或 Web UI 中轻松修改。
- **配置热重载**: 通过 Web UI 修改配置后无需重启，插件会自动应用新配置。

---

## 🚀 快速开始

### 1. 安装

- 手动安装：下载 `group_muter_plugin` 文件夹放入麦麦主程序的 `plugins` 目录下，然后重启主程序即可完成插件的注册和加载。
- 自动安装：通过 Web UI 在插件市场下载安装

### 2. 环境要求

- **MaiBot 主程序**: v1.0.0+
- **MaiBot SDK**: v2.0.0+

### 3. 配置

首次启动麦麦后，插件会在其目录下自动生成 `config.toml` 文件。配置管理员权限后重启麦麦主程序即可。你也可以通过 **Web UI** 在线修改配置，修改后会自动热重载生效。

**默认配置示例**:

```toml
[plugin]
name = "group_muter_plugin"
version = "2.0.0"
config_version = "2.0.0"
enabled = true

[mute]
duration_seconds = 1200
mute_keywords = ["Mute True", "安安你去看书去"]
unmute_keywords = ["Mute False", "安安别看了"]
enable_unmute = true
at_mention_break = true

[user_control]
list_type = "whitelist"
list = []
```

**⚠️ 重要安全提示**:

- **`user_control.list`**: 这是一个**核心安全设置**。
  - 默认值为一个**空列表 `[]`**，这意味着**默认情况下，没有任何人是管理员**。
  - **您必须手动编辑此文件**（或通过 Web UI），将管理员的 QQ 号（字符串格式）添加到这个列表中，才能使用本插件的命令。例如：`list = ["12345", "67890"]`。
- **`user_control.list_type`**: 定义了权限模式。`"whitelist"` 表示只有 `list` 中的人是管理员；`"blacklist"` 表示除了 `list` 中的人，其他人都是管理员。

---

## 📖 使用指南

### 开启静音

管理员在群聊中发送配置的静音关键词即可开启静音模式。

- **默认关键词**: `Mute True` 或 `安安你去看书去`
- **麦麦回复**: `好吧，那我去看会书📘，你们先聊...`
- **非管理员尝试**: 麦麦会回复 `？？？你在教我做事🤡` 并拒绝操作。

### 解除静音

有三种方式可以解除静音：

#### 方式一：关键词解除

管理员发送配置的解除关键词。

- **默认关键词**: `Mute False` 或 `安安别看了`
- **麦麦回复**: `我回来啦，你们聊啥呢🤔`

#### 方式二：@提及解除

管理员在群里 `@` 麦麦，即可立即解除静音。

- **麦麦回复**: `我回来啦，你们聊啥呢🤔`

#### 方式三：自动超时解除

静音时间到达后（默认 1200 秒 = 20 分钟），麦麦将自动解除静音，恢复正常聊天。

### 配置项说明

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `mute.duration_seconds` | int | `1200` | 静音持续时间（秒），范围 60 ~ 86400 |
| `mute.mute_keywords` | list | `["Mute True", "安安你去看书去"]` | 触发静音的关键词列表 |
| `mute.unmute_keywords` | list | `["Mute False", "安安别看了"]` | 解除静音的关键词列表 |
| `mute.enable_unmute` | bool | `true` | 是否启用关键词解除功能 |
| `mute.at_mention_break` | bool | `true` | 管理员 @麦麦 时是否自动解除静音 |

#### 命令示例

>![命令示例](https://s21.ax1x.com/2025/11/05/pZS7YcQ.jpg)

---

## 🔄 从 v1.x 升级

v2.0.0 是基于 MaiBot SDK v2 的完全重写版本，主要变化如下：

| 项目 | v1.x (旧版) | v2.0.0 (新版) |
|------|-------------|---------------|
| SDK 依赖 | `src.plugin_system` (内置插件系统) | `maibot_sdk` v2 |
| 消息拦截 | `BaseEventHandler` (ON_MESSAGE) + `BaseCommand` | `@HookHandler` 装饰器 (入站 + 出站双重拦截) |
| 配置管理 | `config_schema` 字典 + `ConfigField` | `PluginConfigBase` 强类型模型 (Pydantic) |
| 配置热重载 | 不支持 | 支持 `on_config_update` 回调 |
| 日志过滤 | 自定义 `GroupMuterLogFilter` 过滤控制台日志 | 由 SDK 统一管理 |
| 出站拦截 | 无（静音前进入思考的消息可能"漏网"） | `send_service.before_send` Hook 拦截漏网消息 |
| 发送豁免 | 无 | 内置豁免机制，确保插件自身的控制消息不被拦截 |
| 清单文件 | `manifest_version: 1` | `manifest_version: 2`，新增 `capabilities`、`i18n` 等字段 |

**配置兼容性**: `config.toml` 配置结构基本保持一致，新增 `config_version` 字段，可直接迁移使用。

---

## 🙏 致谢

本插件基于 [khiqwq](https://github.com/khiqwq) 的 [silent_mode_plugin](https://github.com/khiqwq/silent_mode_plugin) 插件进行二次开发和优化，我们对原作者的杰出工作表示衷心的感谢和崇高的敬意。

根据原项目的开源协议，本插件同样采用 **[GNU Affero General Public License v3.0](https://www.gnu.org/licenses/agpl-3.0.html)** 协议进行开源。

详情请参阅仓库根目录下的 `LICENSE` 文件。

Enjoy! 🎉
