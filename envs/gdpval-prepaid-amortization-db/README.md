# GDPval 预付费用摊销数据库

本环境改编自 GDPval 官方任务 `7d7fc9a7-21a7-4b83-906f-416dea5ad04f`。原任务包含
6 个源文件和 66 张发票。Agent 编辑 `amortization.db`，scorer 以确定性方式比较
发票级事实和官方月度总账余额，不使用 LLM judge 或 MCP。
