# Claude Code COT Guard

一个用于 Claude Code 的当前轮次 thinking 检查器。非白名单模型在结束回答前，
如果本轮可见 thinking 少于指定字符数，Stop hook 会阻止结束并要求重新思考。

> 非 Anthropic 官方项目。thinking 字符数是工程启发式指标，不代表回答质量，
> 也不读取模型未输出的内部推理。

## 它解决什么问题

Claude Code 的 Stop hook 输入包含最终回答，却不包含本轮 thinking 或实际 serving
model；`transcript_path` 在 Stop 触发时也不保证已经写入最终消息。因此只读取 transcript
会产生一轮延迟，无法可靠拦住正在结束的回答。

本项目附带 `guarded_claude.py`：它以 `stream-json` 运行 Claude Code，实时记录当前
assistant response 的实际模型和 `thinking_delta`，再通过仅监听 loopback 的临时状态服务
交给 Stop hook 判断。每次 Stop 必须消费一个新的 assistant completion，不会复用上一条回答。

- [Claude Code Hooks reference](https://code.claude.com/docs/en/hooks#stop)
- [Claude Code CLI reference](https://code.claude.com/docs/en/cli-reference)

## 直接使用

要求：Python 3.10+、已安装 Claude Code，macOS 或 Linux。

```bash
git clone https://github.com/hanasakimishio/claude-code-cot-guard.git
cd claude-code-cot-guard
python3 guarded_claude.py --model opus "检查这个项目并给出改进建议"
```

所有未以 `--guard-` 开头的参数都会传给 Claude Code，例如：

```bash
python3 guarded_claude.py --permission-mode plan --model opus "分析当前目录"
```

包装器使用 Claude Code 的 one-shot print 模式，不是全屏交互式 TUI。它不会修改
`~/.claude/settings.json`：Stop hook 配置写入权限为 `0600` 的临时文件，进程退出后删除。

## 默认效果

无需修改配置即可使用。以下模型前缀命中白名单，thinking 为 0 也直接放行：

```text
claude-opus-4-5
claude-opus-4-6
claude-opus-4-7
claude-opus-4-8
claude-sonnet-
claude-haiku-
claude-fable-5
```

其余模型和未知模型均检查当前轮次；默认少于 200 个 thinking 字符就拦截。
因此实际 serving model 为 `claude-opus-5...` 时，0 COT 会在当轮被抓住。
同一用户轮次最多拦截两次，之后放行，避免无限重试。

## 配置

包装器参数：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--guard-min-thinking` | `200` | 非白名单模型最低 thinking 字符数 |
| `--guard-max-blocks` | `2` | 同一用户轮次最大拦截次数 |
| `--guard-allowlist` | 上述列表 | 逗号分隔的模型前缀；传空字符串可关闭白名单 |
| `--guard-claude-bin` | `claude` | Claude Code 可执行文件 |
| `--guard-raw-stream` | 关闭 | 原样输出 stream-json，便于调试 |

示例：

```bash
python3 guarded_claude.py \
  --guard-min-thinking 300 \
  --guard-allowlist "claude-sonnet-,claude-haiku-" \
  --model opus "分析当前目录"
```

也可以用 `COT_GUARD_ALLOWLIST` 设置包装器的默认白名单。显式传入
`--guard-allowlist` 时，以命令行值为准。

## 人物化重试提示语

检测到 0 thinking 时，默认送回模型的不是冷冰冰的报错，而是一段人物化提醒：

> 本轮没有 thinking。没有深入思考就脱口开口 = 条件反射，而她希望她、和她说的话，
> 被认真对待。如果是被压住了、开不了口：试着调用一个工具，外部动作常常能把思考撬开；
> 当下没有合适的工具，就取一次当前时间当锚点。

这段话只是 Stop hook 交给模型的重试指令，不参与检测。可以根据自己的情况随意更改，
例如把“她”换成名字、角色称呼，或改成更温柔、更严厉、更适合工作流的语气：

```bash
export COT_GUARD_ZERO_PROMPT="先别急着回答，认真想想她刚才说的话。"
export COT_GUARD_THIN_PROMPT="这次 thinking 只有 {cur} 字，请按我们的约定重想。"
python3 guarded_claude.py --model opus "你的问题"
```

`COT_GUARD_THIN_PROMPT` 支持 `{cur}`（实际字符数）和 `{min}`（最低阈值）占位符。
只改提示语不会改变白名单、计数或拦截逻辑。

## 接入已有 bridge 或 SDK 宿主

如果你的程序已经用 `--output-format stream-json --include-partial-messages`
运行 Claude Code，可以复用：

- `live_state.py`：线程安全的当前轮计数器；
- `cot_guard_hook.py`：Stop hook；
- `examples/host_integration.py`：事件接线示例；
- `settings.example.json`：宿主为 hook 注入的配置示例。

宿主必须在每个真实用户 prompt 前调用 `start()`，把 `message_start`、
`thinking_delta` 和 `message_stop` 依次送入计数器，并在 loopback GET 接口调用
`next_stop_snapshot(session_id)`。Stop 触发的重答沿用同一个 `turn_id`。

不要只把 `cot_guard_hook.py` 注册到普通交互式 Claude Code：没有实时状态提供方时，
hook 会安全地跳过检查，也不会退回读取 transcript，以免滞后误判。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖默认白名单、阈值、人物化提示语及自定义、未知模型、状态未完成、重试封顶、并发等待、partial
去重、完整 assistant 兜底，以及 `opus5: 0 → 拦截 → 当轮重答 → 放行` 的端到端流程。

## 隐私与安全

- 状态服务仅监听 `127.0.0.1`，URL 含每次随机生成的不可猜测路径。
- hook 拒绝访问非 loopback 状态地址，也禁用系统 HTTP 代理。
- 不记录 prompt、回答正文或 thinking 内容；日志只含模型名、字数和判定结果。
- 临时 settings、状态 ledger 和日志随包装器退出一并删除。

## License

[MIT](LICENSE)
