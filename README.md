# astrbot_plugin_adguard

AstrBot 插件：**QQ 群广告检测**。自动检测群消息中的**文字广告 / 图片广告 / 视频广告**，并按违规次数**撤回、禁言、踢人**多级处置。

## 功能特性

| 类型 | 检测方式 |
| --- | --- |
| 文字广告 | 关键词（普通词 +2 分、高危词 +4 分）+ 正则特征（QQ群号、微信号、手机号、网址、裸数字），总分达到阈值即判定 |
| 图片广告 | 本地 OCR（RapidOCR）识别图中文字 + 二维码解码；或使用 AI 视觉模型识别（二选一或自动切换） |
| 视频广告 | OpenCV 抽帧后对每一帧进行 OCR / AI 识别 |
| 处置动作 | 撤回消息 + 禁言（可配置多档时长）+ 踢人；默认“逐级升级”：第 1 次禁言 5 分钟、第 2 次禁言 30 分钟、第 3 次移出群聊 |
| 二次审核 | 疑似广告（未达阈值但命中特征 / AI 无法判定）自动发给群管理员人工确认；确认结果记录进学习库，后续相同内容直接复用结论 |

## 环境要求

- AstrBot >= 4.5.7
- 消息平台为 **aiocqhttp**（OneBot v11，如 NapCat / Lagrange / go-cqhttp）

## 安装

将整个 `astrbot_plugin_adguard` 文件夹放入 AstrBot 的 `data/plugins/` 目录，然后在 WebUI 插件管理中启用（或重载）该插件。

插件依赖 `rapidocr_onnxruntime` 与 `opencv-python-headless`（见 `requirements.txt`，WebUI 安装会自动安装）。

> 若不想安装较重的 OCR/OpenCV 依赖，可以手动删除 `requirements.txt` 中的对应行，插件会**自动降级**：
> - 图片/视频检测在 auto 模式下会改用 **AI 视觉**（需要配置一个支持图片输入的模型，或通过 `ai_provider_id` 指定）；
> - 没有 AI 也没有 OCR 时，图片/视频检测跳过，**文字广告检测始终可用**。

## 配置

在 WebUI 插件配置页中可修改以下关键项（详见 `_conf_schema.json`）：

| 配置项 | 说明 | 默认值 |
| --- | --- | --- |
| `enable` | 总开关 | `true` |
| `groups_only` / `groups_whitelist` | 只检测的群 / 跳过检测的群 | `[]` |
| `users_whitelist` | 白名单用户 QQ | `[]` |
| `admin_qq` | 插件管理权限 QQ（群主/群管理自动拥有） | `[]` |
| `score_threshold` | 文字广告判定阈值 | `4` |
| `media_score_threshold` | 图片/视频广告判定阈值 | `3` |
| `keywords` / `strong_keywords` | 普通 / 高危广告关键词 | 内置常见广告词 |
| `regex_patterns` | 自定义广告特征正则 | `[]` |
| `image_check` / `video_check` | `off` / `auto` / `ocr` / `ai`（`auto` 为 OCR 优先、AI/智谱兜底） | `auto` |
| `ai_provider_id` | AI 视觉检测的模型提供商 ID（留空自动使用当前会话模型） | `""` |
| `action` | `escalate` / `mute` / `kick` / `recall_only` | `escalate` |
| `mute_durations` | 禁言时长列表（秒），按违规次数依次选择 | `[300, 1800]` |
| `kick_after` | 第几次违规后移出群聊 | `3` |
| `recall_message` / `notify_group` / `stop_event` | 撤回 / 群内通知 / 终止事件传播 | `true` |
| `forgive_hours` | 违规记录清零周期（小时） | `24` |
| `audit_enable` | 是否开启二次审核 | `true` |
| `audit_threshold` | 疑似广告进入二次审核的最低得分 | `2` |
| `audit_mention_admin` | 审核通知是否 @ 群管理员 | `true` |
| `audit_attach_image` | 审核通知是否附带图片预览 | `true` |
| `audit_ttl_minutes` | 待审核记录有效期（分钟），超时自动清除 | `120` |
| `audit_max_pending` | 最多同时保留的待审核记录数 | `20` |
| `audit_stop_event` | 疑似内容进入审核后终止事件传播 | `true` |
| `learn_max_entries` | 学习记录库上限（超出自动淘汰最旧） | `500` |
| `zhipu_api_key_id` | 智谱 API Key 的 ID 部分（也可直接填完整 Key） | `""` |
| `zhipu_api_key_secret` | 智谱 API Key 的 Secret 部分 | `""` |
| `zhipu_model` | 智谱视觉模型名称 | `glm-4v-flash` |
| `zhipu_base_url` | 智谱 API 地址（一般无需修改） | `https://open.bigmodel.cn/api/paas/v4/chat/completions` |

## 管理指令

在群里发送（需群主/群管理/`admin_qq` 中配置的 QQ）：

```
/adguard help                查看帮助
/adguard status              查看插件与依赖状态
/adguard on / off            开启 / 关闭检测
/adguard addkw <关键词>      添加普通关键词
/adguard delkw <关键词>      删除普通关键词
/adguard kw                  查看当前关键词列表
/adguard scan <文本>         测试文本广告得分（用于调阈值）
/adguard forgive <QQ号>      清零某用户在本群的违规次数
/adguard audit <编号> yes|no 二次审核：确认广告 / 误判（记录学习）
/adguard pending             查看本群待审核列表
/adguard forget <编号>       移除待审核记录（不学习）
/adguard selftest             插件自检（依赖/配置/文本检测）
```

## 处置策略说明（escalate 模式）

违规次数按“群 × 用户”记录，超过 `forgive_hours` 小时未再违规会自动清零：

1. 第 1 次：撤回消息 + 禁言 `mute_durations[0]`（默认 5 分钟）+ 群内通知
2. 第 2 次：撤回消息 + 禁言 `mute_durations[1]`（默认 30 分钟）
3. 第 ≥3 次：撤回消息 + 移出群聊

群主、群管理以及 `users_whitelist` 中的用户不会被检测处置（`skip_admins = true`）。

## 二次审核流程

检测分为三级：

1. **直接处罚**：总分达到 `score_threshold`（文字）或 `media_score_threshold`（图片/视频），或 AI 明确识别为广告 → 立即撤回 + 禁言/踢人
2. **人工审核**：总分进入疑似区间（`audit_threshold` ≤ 分数 < 处罚阈值），或 OCR 命中特征但不足、AI 无法判定 → 群里 @ 群管理员，附审核编号、内容摘要与图片预览，管理员用指令裁决
3. **放行**：未命中任何特征

管理员裁决后会自动**学习**：

- `/adguard audit <编号> yes` → 确认广告：撤回并处罚发送者，同时记录**内容指纹**与**发送者**。之后相同内容再出现会直接处罚；该用户再发疑似内容也会直接升级处罚
- `/adguard audit <编号> no` → 误判：记录内容指纹为“放行”，之后相同内容自动跳过检测

学习记录按群存储、持久化（KV 存储），超过 `learn_max_entries` 自动淘汰最旧记录。

## 常见问题

**Q1：图片/视频检测不生效？**
运行 `/adguard status` 查看 OCR 与 OpenCV 是否可用。若均不可用且 `image_check=auto`，请确认已配置支持图片的 AI 模型（或在配置中填写 `ai_provider_id`），或按下面 Q4 接入智谱 GLM-4V-Flash。

**Q4：如何接入智谱 GLM-4V-Flash（免费视觉模型）？**
智谱 GLM-4V-Flash 是智谱 AI 提供的免费多模态视觉模型，无需在 AstrBot 中配置模型提供商，只需在插件配置中填写智谱 API Key：
1. 登录 https://open.bigmodel.cn ，在「API Keys」页面创建一个 Key（格式为 `{ID}.{SECRET}`）；
2. 在插件配置中把 Key 的 **ID 部分**填入 `zhipu_api_key_id`、**Secret 部分**填入 `zhipu_api_key_secret`（也可直接把完整 Key 填在 `zhipu_api_key_id` 中）；
3. 将 `image_check` / `video_check` 设为 `ai` 或 `auto`；
4. 重启/重载插件即可。配置了智谱后，AI 视觉检测会**优先使用智谱**，未配置时自动回退到 AstrBot 已配置的 LLM。

> 说明：智谱 API Key 属于敏感凭据，仅保存在 AstrBot 本地配置文件中，不会上传第三方。

**Q2：误判 / 漏判怎么办？**
- 误判：用 `/adguard scan <文本>` 查看命中项，删除对应关键词，或提高 `score_threshold`；
- 漏判：用 `/adguard addkw <关键词>` 添加关键词，或自定义 `regex_patterns`，或降低阈值。

**Q3：视频广告检测需要下载视频到本地？**
是的，检测时会将视频落地到 AstrBot 临时目录并抽帧，检测结束后临时文件会被清理。

## 免责声明

**请在使用本插件前仔细阅读以下内容，部署或使用即视为已同意本声明。**

1. **用途声明**
   本插件为开源社区工具，仅用于协助群主/管理员维护群聊秩序，自动识别并处置广告消息。请勿将其用于任何违反法律法规、平台规则或侵犯他人合法权益的用途。

2. **误判与漏判风险**
   广告检测基于关键词、正则特征、OCR 与 AI 视觉模型进行自动判定，**无法保证 100% 准确**，可能存在误判或漏判，批量处置（禁言/踢人）可能对正常成员造成影响。请部署后先在测试环境验证效果，并根据实际情况调整阈值、关键词与处置策略。

3. **处置行为责任**
   插件的撤回、禁言、踢人等操作由部署者（群主/管理员）自行决定是否启用，**由此产生的一切后果由部署者承担**。建议：
   - 保持二次人工审核开启，避免误伤；
   - 对不确定的内容优先人工确认；
   - 妥善使用 `/adguard forgive`、白名单等纠错机制。

4. **平台规则与法律合规**
   请遵守腾讯 QQ 平台的服务条款、社区规范以及所在地法律法规（包括但不限于《网络安全法》《个人信息保护法》）。使用自动化机器人进行群管理时，请确保符合平台使用政策。

5. **数据与隐私**
   插件会在机器人本地记录违规次数、审核记录与学习库等数据（存储于 AstrBot 数据目录），**不会主动向第三方上传**。若启用 AI 视觉检测，图片内容会被发送至所配置的模型提供商处理，请评估相关隐私与合规风险，优先使用本地 OCR 或自建模型。

6. **第三方组件**
   本插件依赖 `rapidocr_onnxruntime`、`opencv-python-headless` 等第三方开源组件，相关组件遵循其各自的许可证与条款。

7. **无担保与责任限制**
   本插件按“现状”提供，作者不提供任何明示或暗示的担保，包括但不限于适销性与特定用途适用性。**在任何情况下，作者均不对因使用或无法使用本插件而产生的任何直接、间接、偶然或后果性损害承担责任。**

8. **风险自负**
   部署、使用本插件即表示您已阅读并同意本声明，并自行承担全部使用风险。

## 更新日志

**v1.1.1（2026-08）**
- 修复：`auto` 模式下 OCR 可用时不会调用 AI（智谱）的问题，现改为「OCR 优先，未命中再交由智谱 GLM-4V-Flash / AI 兜底」，智谱真正参与图片/视频广告检测
- 优化：AI 不可用（未配置智谱且无 AstrBot 模型）时不再进入人工审核，避免打扰

**v1.1.0（2026-08）**
- 新增：接入智谱 GLM-4V-Flash 免费视觉模型（配置 `zhipu_api_key_id` / `zhipu_api_key_secret` 即可使用，优先于 AstrBot 内置 LLM 视觉，未配置自动回退）
- 优化：二次审核「AI 无法判定」时强制进入人工审核

**v1.0.0（2026-08）**
- 初始版本：文字/图片/视频广告检测、撤回/禁言/踢人多级处置、二次人工审核与记录学习

部分功能合zcjui/astrbot_plugin_group_guardian合并
