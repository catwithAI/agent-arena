# A7-5 · 真实 ACP smoke 运行手册

该 smoke 是附加验收，不进入默认 CI，也不会下载或安装 Agent。运行者必须先由管理员安装
registry 中固定版本的 ACP server，并提供本地命令。测试会保留标准结果、owner-only 脱敏
`acp-transcript.jsonl` 和 `agent-manifest.json`。

## 候选与 pin

2026-07-22 获取的 ACP 官方 registry schema 为 `1.0.0`，原始文件 SHA-256：

```text
799d71d53cbf5af4d9a0b38a68a402f8e135f6dd0002f58eaf6a184cbbaa03d1
```

首选两个来源、许可证和 OpenRouter 配置都清晰的候选：

| stable ID | registry distribution | license | source |
|---|---|---|---|
| `acp:opencode@1.18.4` | verified darwin-arm64 binary, `opencode acp` | MIT | `anomalyco/opencode` |
| `acp:kilo@7.4.11` | verified darwin-arm64 binary, `kilo acp` | MIT | `Kilo-Org/kilocode` |

机器上若没有相应账号，可替换为同一 pinned registry 中非 proprietary 且已配置凭据的 Agent；
仍须至少两个，并在记录中保留 stable ID、CLI version、模型与执行位置。

## 显式配置

先把上述固定 registry 原文保存到本地，再创建不含真实 secret 的配置：

```json
{
  "registry_path": "./registry.json",
  "registry_sha256": "799d71d53cbf5af4d9a0b38a68a402f8e135f6dd0002f58eaf6a184cbbaa03d1",
  "output_dir": "/absolute/path/to/acp-smoke-evidence",
  "timeout_seconds": 120,
  "model": "openrouter/google/gemini-2.5-flash",
  "agents": [
    {
      "id": "opencode",
      "version": "1.18.4",
      "command": ["/absolute/path/to/opencode", "acp", "--pure"],
      "env_from": ["OPENROUTER_API_KEY"],
      "config_content_env": "OPENCODE_CONFIG_CONTENT"
    },
    {
      "id": "kilo",
      "version": "7.4.11",
      "command": ["/absolute/path/to/kilo", "acp", "--pure"],
      "env_from": ["OPENROUTER_API_KEY"],
      "config_content_env": "KILO_CONFIG_CONTENT"
    }
  ]
}
```

两个 Agent 统一使用 `OPENROUTER_API_KEY`。凭据只有列入 `env_from` 才从 pytest 环境转交，
inline config 只保存 `{env:OPENROUTER_API_KEY}` 引用、不保存 key；其余用户环境不会继承。ACP
进程使用 Attempt 私有 HOME/XDG，不能读取或污染真实用户配置。执行：

```bash
ARENA_ACP_SMOKE_CONFIG=/absolute/path/to/smoke.json \
  uv run pytest -q tests/smoke/test_acp_real_smoke.py
```

未设置 `ARENA_ACP_SMOKE_CONFIG` 时测试只会 skip。测试校验 registry 内容 hash、精确版本、
非 proprietary 许可证、两个 Agent 的最小回复，以及 transcript/manifest 是否落盘。

## 当前执行记录

- 2026-07-22，macOS arm64：最初发现的 Homebrew Gemini CLI `0.30.1` 和未加入 PATH 的
  `@zed-industries/claude-code-acp@0.12.6` 都不匹配当前 registry pin。
- Gemini 的最小 prompt 在 60 秒内超时；旧 Claude adapter 能完成 ACP initialize，但在
  `session/new` 启动 SDK 时试图访问真实 `~/.claude`，验证出环境隔离缺口。实现现已改为
  最小继承环境与 Attempt 私有 HOME/XDG，并增加回归测试。
- 经用户批准后，已下载 OpenCode `1.18.4` 与 Kilo `7.4.11` darwin-arm64 官方包到隔离的
  `/tmp/agent-arena-acp-tools`；archive SHA-256 分别为
  `04fb881b632b323c712dfda6dcbbc6fce736394f07ba76176e52d6665925d4e6` 与
  `14a030a354f3b51f0241662627702e7b06cddf3fcb6e0f1415279e9d3a3b8998`，均与 registry 一致。
- 使用同一个 `OPENROUTER_API_KEY` 和模型 `openrouter/google/gemini-2.5-flash` 完成双-agent
  smoke；OpenCode 与 Kilo 均返回精确文本 `ACP_SMOKE_OK`，trajectory 为 `verified`、cleanup
  为 `confirmed`，token coverage 按实际 ACP update 标记 `partial`。
- 最终证据位于 `fixtures/acp-real-2026-07-22/`。审计确认 key、`sk-or-` 模式、本机用户路径
  和临时安装路径均未落盘；Attempt 私有 runtime/cache 已清理。A7-5 完成。
- 下一步需要管理员安装上述两个固定版本（或提供两个等价的已安装命令）并确保账号凭据可用。
