# Claude Code COT Guard

A dependency-free Stop hook that catches zero or unusually thin `thinking`
before Claude Code finishes a turn.

> 非 Anthropic 官方项目。COT 字符数只是工程启发式指标，不等于回答质量。

## 为什么需要它

Claude Code 的 Stop hook 能阻止本轮结束，并把原因作为下一条指令交还给 Claude；
但 Stop 输入只直接提供最终文本，不提供本轮 `thinking` 或实际 serving model。
而且官方文档明确说明：Stop 触发时，`transcript_path` 不保证已经包含最终消息。

因此本项目提供两种模式：

| 模式 | 安装难度 | 能力 |
| --- | --- | --- |
| Transcript fallback | 低 | 补查已经落盘的完整轮次，可能晚一轮 |
| Live-state mode | 中 | 宿主从 `stream-json` 实时计数，Stop 当轮立即判断 |

相关官方文档：

- [Hooks reference: Stop input and decision control](https://code.claude.com/docs/en/hooks#stop)
- [CLI reference: `--include-partial-messages`](https://code.claude.com/docs/en/cli-reference)

## 特性

- 低于阈值时返回 `{"decision":"block","reason":"..."}`
- 模型前缀白名单，由使用者显式配置
- 未知模型默认 fail-closed
- 同一轮最多拦截两次，避免无限循环
- 实时接口只接受 `127.0.0.1` 或 `::1`
- 不记录 prompt、回答正文或 thinking 内容，只记录模型名、字数和判定结果
- Python 标准库实现，无第三方依赖

## 快速安装：Transcript fallback

要求：Python 3.10+，macOS 或 Linux。

```bash
git clone https://github.com/hanasakimishio/claude-code-cot-guard.git
cd claude-code-cot-guard
mkdir -p ~/.claude/hooks
install -m 0755 cot_guard_hook.py ~/.claude/hooks/cot_guard_hook.py
```

把 `settings.example.json` 中的 `env` 和 `hooks.Stop` 合并进
`~/.claude/settings.json`。不要直接覆盖已有设置。

最小配置：

```json
{
  "env": {
    "COT_GUARD_MIN_CHARS": "200",
    "COT_GUARD_ALLOWLIST": "claude-sonnet-,claude-haiku-"
  },
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"$HOME/.claude/hooks/cot_guard_hook.py\"",
            "timeout": 5
          }
        ]
      }
    ]
  }
}
```

重启 Claude Code 后生效。Transcript fallback 会读取本机
`transcript_path`，但不会把内容发送到网络。

## 配置项

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `COT_GUARD_MIN_CHARS` | `200` | 最低 thinking 字符数 |
| `COT_GUARD_ALLOWLIST` | 空 | 逗号分隔的模型前缀；命中即放行 |
| `COT_GUARD_MAX_BLOCKS` | `2` | 同一轮最大拦截次数 |
| `COT_GUARD_CACHE_DIR` | `~/.claude/cache` | ledger 与本地日志目录 |
| `COT_GUARD_STATE_URL` | 空 | 实时状态 URL 模板，必须是 loopback HTTP |

白名单按前缀匹配。例如：

```bash
export COT_GUARD_ALLOWLIST="claude-sonnet-,claude-haiku-"
```

留空表示所有模型都接受阈值检测。

## 当轮实时模式

如果你通过 SDK、bridge 或自己的常驻进程运行 Claude Code，可以启用：

```text
--print
--output-format stream-json
--input-format stream-json
--verbose
--include-partial-messages
```

宿主需要完成四件事：

1. 每次写入真实用户 prompt 前调用 `LiveCotState.start()`。
2. 从 `message_start` 记录实际 serving model。
3. 从 `thinking_delta` 累计字符数；没有 partial 时用完整 assistant block 兜底。
4. 在 loopback GET 接口返回 `snapshot(session_id)`。

返回协议：

```json
{
  "session_id": "current-session-id",
  "turn_id": "host-generated-turn-id",
  "serving_model": "actual-serving-model",
  "thinking_chars": 237,
  "active": true
}
```

然后让 Claude Code 子进程继承：

```bash
export COT_GUARD_STATE_URL="http://127.0.0.1:8800/cot-state/{session_id}"
```

参考实现见 `live_state.py` 和 `examples/host_integration.py`。

`turn_id` 只在真实用户 prompt 到来时更新。Stop hook 触发的续写必须沿用同一个
`turn_id`，这样第二次 Stop 才会重新检查累计后的 thinking。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

覆盖：阈值、白名单、未知模型、重试封顶、partial 去重、完整 block 兜底和
transcript fallback。

## 隐私与安全

- hook 代码会以你的本机权限执行，请在安装前审阅源码。
- 实时状态 URL 拒绝非 loopback 地址，避免把 session 元数据发送到远端。
- 日志位于 `~/.claude/cache/cot-guard.log`，不包含 prompt、回答或 thinking 正文。
- `last_assistant_message` 不会被保存或传输。

## License

[MIT](LICENSE)
