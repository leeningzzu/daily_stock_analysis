# DSA 日常操作手册

> 这是一份“以后忘了怎么操作时先看这里”的薄索引。详细参数和完整语义仍以各专题文档为准，本页不复制整套配置手册。

## 1. 先记住当前边界

- **公开仓库仍是代码 / CI / PR 的当前权威入口**。
- 私有仓库目前只作为 **Shadow**：已经做过 Git 镜像与 ref/SHA parity，但 **Actions 仍关闭**，还不是 Production authority。
- 不长期双写两个仓库。只有在材料 checkpoint 或未来私仓 Promotion 验证前，才做受控的 public → private 刷新。
- Cloudflare R2 目前只证明了 **synthetic roundtrip** 能力；Production R2 credential / consumer 尚未晋升，不要把它当作已经启用的日常生产存储。
- 任何新权限、付费服务、Production Secret、仓库 authority 切换，都应走单独的验收 / Promotion。

## 2. 永远不要写进仓库的内容

不要把下面内容写进 README、本文、Issue、PR、日志、截图或普通配置文件：

- API Key、密码、Token、Webhook Secret；
- 私有网关鉴权 Header、账号 ID；
- 私人持仓；
- 原始 Prediction Ledger / 训练原始数据；
- 模型状态或其他仅应存在于受控私有存储中的数据。

在 GitHub Actions 中，**密钥放 Secrets**；非敏感配置优先放 Variables。更细规则见 [LLM 服务商配置指南](llm-providers.md#github-actions-配置) 和 [通知能力基线](notifications.md#github-actions-映射)。

## 3. 我要更换 LLM / API 服务商

优先走 Web 设置页，而不是手工改代码：

1. 打开 **设置 → AI 模型配置**。
2. 在“快速添加渠道”选择已有 provider；如果是 OpenAI-compatible 网关/中转，则创建自定义 channel。
3. 填 API Key、Base URL、模型列表。
4. 选择主模型、Agent 主模型、fallback、Vision 模型（只配置实际需要的）。
5. 保存后先做**配置 / 状态检查**。
6. 需要确认真实 JSON / tools / stream / vision 能力时，再显式运行真实 smoke test。

详细步骤：
- [LLM 配置指南](LLM_CONFIG_GUIDE.md)
- [LLM 服务商配置指南](llm-providers.md)

### 当前支持的接入形态

当前仓库已提供这些配置形态：

- 官方 provider 预设；
- LLM_CHANNELS 多渠道；
- 自定义 OpenAI-compatible gateway / proxy；
- 自定义 Base URL + API Key + model list；
- Ollama；
- LiteLLM YAML；
- generation-only 的 Codex CLI / Claude Code CLI / OpenCode CLI。

第三方中转站、公益站或其他厂商端点**不能只根据品牌名判断兼容**。只要不是当前文档已验证的 preset，都应把它当作候选 endpoint，用当前账号、模型和 endpoint 做一次实际能力验证。

### 自定义 OpenAI-compatible channel：只记这个形状

下面只有占位符，不要把真实 Key 写进文档：

~~~text
LLM_CHANNELS=my_proxy
LLM_MY_PROXY_PROTOCOL=openai
LLM_MY_PROXY_BASE_URL=https://example.invalid/v1
LLM_MY_PROXY_API_KEY=<SECRET>
LLM_MY_PROXY_MODELS=<MODEL_A>,<MODEL_B>
~~~

运行时模型通常走 openai/<model>。Base URL 填到服务商兼容入口，不要自行再拼 /chat/completions。完整规则见 [OpenAI-compatible 与 LiteLLM 规则](llm-providers.md#openai-compatible-与-litellm-规则)。

**GitHub Actions 额外注意：** 默认 workflow 只映射明确列出的 channel 名。自定义 channel（或默认未映射的 channel）如果以后要在 GitHub-hosted Actions 使用，需要先补 workflow env mapping；本地 .env、Docker、自托管脚本不受这个固定映射限制。

## 4. API Key 怎么安全轮换

1. 先在服务商控制台创建新 Key；不要把 Key 发到 Chat / Issue / PR。
2. 本地运行时放到受控配置；GitHub Actions 放 **Repository Secrets**。
3. 若服务商允许新旧 Key 短暂并存，先切到新 Key 并测试，再撤销旧 Key。
4. 先做轻量状态检查；必要时再做真实 smoke。
5. 新 Key 验证成功后再撤销旧 Key。
6. 失败则恢复上一个已知可用 channel / model / Secret 配置。

常用字段放置：
- LLM_<CHANNEL>_API_KEY / API_KEYS → **Secrets**；
- LLM_<CHANNEL>_PROTOCOL、公开 Base URL、公开 model list → 通常 **Variables**；
- 私有 Base URL、鉴权 Header、租户/组织信息 → **Secrets**。

## 5. “状态正常”不等于“真实模型已经跑通”

要区分两类检查：

**轻量状态 / 配置检查**
- 读取已保存配置、草稿和本地 capability；
- 通常不发送真实模型请求；
- 适合先检查配置形状、模型名、Base URL。

**真实 smoke / 运行时能力检测**
- 会真的请求模型；
- 可能产生 token / 图片输入费用；
- 可能触发 RPM / TPM 限流、余额不足、超时；
- 结果只代表“当前账号 + 当前模型 + 当前 endpoint”的一次观测。

因此默认顺序是：**先状态检查，只有需要时才做真实 smoke**。详见 [运行时能力检测边界](llm-providers.md#运行时能力检测边界)。

## 6. 模型或 provider 配坏了，怎么回滚

优先恢复“最后一个已知可用配置”，不要同时改多项：

- Web 设置页：禁用/删除新 channel，重新选择旧主模型 / Agent 模型 / fallback；
- Channels → legacy：清空 LLM_CHANNELS，保留原 legacy provider key 与 LITELLM_MODEL；
- YAML → Channels / legacy：移除 LITELLM_CONFIG / LITELLM_CONFIG_YAML；
- WebUI / Desktop：使用之前导出的系统配置备份恢复。

完整回滚语义见 [LLM 服务商配置指南 → 回滚方式](llm-providers.md#回滚方式)。

## 7. 我要改通知 / 邮件 / Telegram

不要在本页复制所有渠道变量；直接使用 [通知能力基线](notifications.md)。

最小操作顺序：

1. 只配置目标渠道的 **Minimal key**；
2. Secret 类字段放 Secrets；
3. 在 Web 中使用对应的单渠道测试能力确认真实发送；
4. GitHub Actions 场景先核对 [Actions 映射表](notifications.md#github-actions-映射)；
5. 再按需要增加 quiet hours、dedup、cooldown、routing 等 Advanced 配置。

如果只是换收件人、频道或通知 token，不要顺手改模型、数据源或调度。

## 8. R2 现在怎么理解

当前项目已经证明了三层**代码能力**：synthetic R2 roundtrip、selective immutable research-state package chain、以及 boto3 低层 S3 transport。每日 workflow 也包含默认关闭的 restore-before-analysis / success-only-publish 绑定入口。

但这**不等于 Production R2 已启用**。当前默认仍是：
- `RESEARCH_STATE_DURABILITY_ENABLED=false`；
- 未配置长期 Production R2 credential / GitHub Secrets；
- 未执行真实 research-state cross-run trial；
- provider-derived / rights-conditional 字段仍需单独准入；
- R2 仍只是 immutable byte substrate，不是 Prediction Ledger / PIT / model 的逻辑数据库 owner。

未来正式启用时，非敏感 `RESEARCH_STATE_DURABILITY_ENABLED`、`R2_ENDPOINT_URL`、`R2_BUCKET_NAME` 应按当时合同放 Variables；`R2_ACCESS_KEY_ID`、`R2_SECRET_ACCESS_KEY` 放 Secrets。**在项目 State 明确进入 live-effect gate 前，不要自行创建长期 Key、填写这些值或把 enable flag 打开。**

default-off 的意义是：没有显式启用变量时，现有 daily analysis 行为保持不变且不会发起 R2 请求。

代码同时提供一个**仅人工、仅空状态**的 `research-state-smoke` workflow mode，用于未来 live-effect gate 的两次连续性验收：先选 `publish-empty`，再在另一 fresh runner 选 `restore-empty`。这个 mode 使用独立 `research_state_smoke.db`，不执行股票分析、报告或通知；只允许三张 research-state 表均为 0 行的 generation-1 checkpoint，并要求两次运行使用同一 exact `GITHUB_SHA`，把 code SHA、generation、package/manifest key+SHA、package bytes 和表计数输出为 machine-readable JSON receipt，同时仅为该 smoke 上传一个 1 天保留的 receipt artifact，便于后续机器验收；该 artifact 不是 canonical research state。它不会替代 Production durability 开关，正常/定时分析的 `RESEARCH_STATE_DURABILITY_ENABLED` 仍默认 `false`。

在 Project State 明确授权真实 effect 之前，**不要运行该 smoke mode**；代码入口存在不代表 bucket、credential、GitHub Variables/Secrets 或 R2 写入已经获准。

## 9. 私有 GitHub 现在怎么理解

当前状态：

- public repo：**authority**；
- private repo：**Shadow**；
- private Git ref/SHA parity：已验证；
- private fetch：已验证；
- private Actions：**Disabled**；
- private Secrets / Variables / Environments：未作为 Production 配置建立；
- authority Promotion：**未发生**。

不要为了“看起来更安全”就同时维护两套写入。默认只在材料 checkpoint 或未来 Promotion 验证前做一次受控 public → private refresh。

## 10. 出问题时先做这 6 件事

1. **停止继续改配置**，不要一口气换 Key、Base URL、模型和 fallback。
2. 保存当前错误类别 / details.reason；不要复制 Secret。
3. 检查最近一次配置备份和最后一个已知可用 channel/model。
4. 先做轻量状态检查，再决定是否值得跑真实 smoke。
5. 按 [错误分类](llm-providers.md#常见错误与处理建议) 判断是 Key、额度、限流、网络、URL 还是模型权限。
6. 恢复已知可用配置；仍失败再进入项目的 exact evidence / STOP 流程，不要靠反复重试掩盖根因。

## 11. 常用入口

| 我要做什么 | 入口 |
| --- | --- |
| 第一次配置 / 看完整 LLM 说明 | [LLM 配置指南](LLM_CONFIG_GUIDE.md) |
| provider、Channels、Base URL、Actions 映射、错误分类 | [LLM 服务商配置指南](llm-providers.md) |
| 邮件 / Telegram / 飞书 / Slack / Webhook 等 | [通知能力基线](notifications.md) |
| 第一次安装客户端 | [小白客户端安装与配置](beginner-client-setup.md) |
| 完整部署与环境变量 | [完整配置与部署指南](full-guide.md) |
| 常见运行问题 | [FAQ](FAQ.md) |
| 文档总入口 | [文档中心](INDEX.md) |

---

维护原则：本页只保留**稳定的日常操作顺序与安全边界**。Provider 型号、价格、模型列表、完整变量表等容易变化的内容继续由专题文档维护，避免两处同时更新造成漂移。
