# Vocabulary Builder 扩展隐私政策 / Privacy Policy

**本扩展不向开发者或任何第三方发送任何数据。**

Vocabulary Builder 是一个自托管系统的客户端：所有数据只发送到**你自己在扩展选项页里
填写的服务器地址**，那台服务器由你自己部署和控制（见本仓库 README）。

## 收集哪些数据

当你双击一个词并点击「加入」时，扩展会把以下内容发送到你配置的服务器：

- 选中的词及其所在句子（含前后少量上下文）
- 当前页面的 URL 和标题（用于在 Anki 卡片上标注出处）
- 时间戳和一个随机生成的客户端标识

未点击「加入」时，不发送任何内容。扩展本地（`chrome.storage`）只保存你的服务器
地址、API token 和离线待重试队列。

## 不做的事

- 不使用任何分析、统计或广告服务
- 不向扩展开发者回传任何信息
- 不读取或发送与取词无关的页面内容

## English summary

This extension sends data **only to the server you configure yourself** in the
options page (a self-hosted instance of this project). When you save a word, it
sends the word, its sentence with nearby context, the page URL/title, and a
timestamp. Nothing is sent to the developer or any third party; no analytics of
any kind.

如有问题请在本仓库提 issue。
