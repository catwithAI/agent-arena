# 环境前置条件

不同评测环境可能依赖编译器、Office 渲染器、媒体工具或外部 Judge。前置条件声明的
目的是在提交前说明“本机缺什么、缺失后会怎样”，避免运行结束后才发现得分无效。

## 声明格式

在环境的 `meta.yaml` 中使用：

```yaml
prerequisites:
  level: office
  summary: "需要 LibreOffice、PyMuPDF 和多模态 Judge。"
  requires:
    - "LibreOffice/soffice（Office 渲染）"
    - "PyMuPDF/fitz（PDF 转图片）"
    - "ANTHROPIC_API_KEY 环境变量"
  on_missing: "缺少渲染器或 Judge 会使对应评分维度为 0。"
```

字段含义：

- `level`：供界面分组的粗粒度等级，当前约定为 `none`、`skill-source`、
  `office`、`compiler`、`media`、`judge`；
- `summary`：面向使用者的简短说明；
- `requires`：依赖清单，可以写可执行文件、Python 包、环境变量或人工条件；
- `on_missing`：明确缺失后的降级、失败或失分方式。

## 自动检查范围

启动时，加载器只对能够可靠判断的短名称做本地存在性检查：

- 先使用 `PATH` 查找可执行文件；
- 找不到时再尝试判断同名 Python 包是否可导入；
- `A/B` 表示候选项，任一存在即可；括号中的说明不参与匹配；
- 自然语言、环境变量、远程服务可用性和平台兼容性不会被自动推断。

例如 `python3`、`LibreOffice/soffice（Office 渲染）` 可以自动检查，而
`ANTHROPIC_API_KEY 环境变量` 只作为说明展示。保守跳过无法判断的条目，比产生误报
更重要。

## 告警而非启动门禁

前置检查是告警机制：缺失依赖不会阻止后端加载其余环境。告警会进入环境列表 API，
由前端在运行前展示。真正执行时，环境应按 `on_missing` 的约定给出稳定、可解释的
失败或维度得分。

与此不同，以下属于环境结构错误，应由运行时或校验器拒绝：

- 环境名与目录名不一致；
- 任务缺少 ID/Prompt，字段类型错误或时间预算非正整数；
- `core.py`、`scorer.py` 无法导入；
- 声明了 MCP 能力却没有可执行的入口命令。

## 编写建议

1. `summary` 说明完整运行所需条件，不要只写包名；
2. `requires` 中把可自动检查的短名称单独成项；
3. `on_missing` 指明具体影响到哪个阶段或评分维度；
4. 不要把“建议安装”写成硬要求，也不要声称启动时会验证远程 API；
5. 对平台相关二进制说明架构和操作系统限制。

修改后运行：

```bash
uv run python scripts/lint_env.py <环境名>
```

还可在服务启动后查看 `GET /api/envs` 的 `prerequisite_warnings` 和
`GET /api/selfcheck` 的整体结果。
