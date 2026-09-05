# 项目文档索引

本项目(rslsync-lite-panel —— Resilio Sync (rslsync) 轻量管理面板与服务器端守护)的标准文档索引。

> 项目整体介绍与快速开始见根目录 [README.md](../README.md)。

## 标准文档(docs/)

| 文档 | 内容 | 用途 |
|------|------|------|
| [requirements.md](requirements.md) | 开发需求与验收标准 | 需求基线,功能范围确认 |
| [technical-design.md](technical-design.md) | 技术架构与组件设计 | 实现遵循的架构 |
| [design-standards.md](design-standards.md) | 编码 / 错误处理 / 日志 / UI 规范 | 写代码前对照 |
| [development-plan.md](development-plan.md) | 分阶段开发计划 | 推进节奏 |
| [api-reference.md](api-reference.md) | Resilio Sync GUI API 参考(实测) | API 客户端实现依据 |
| [case-study.md](case-study.md) | 案例研究 | 问题定义、两次生产事故复盘与设计决策 |

## 示例(examples/)

- [probe_api_demo.py](../examples/probe_api_demo.py) — ResilioSyncClient 最小只读用法示例(凭证从环境变量读取)

## 约定

- 所有新增/改动遵守 [design-standards.md](design-standards.md),按 [development-plan.md](development-plan.md) 的阶段推进
