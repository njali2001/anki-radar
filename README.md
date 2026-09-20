# anki-radar

在论坛上找出「有人正卡在 Anki 同步或容量上」的帖子，**只读、不发帖**，跑在自己电脑上。

## 它做什么

1. 用 Reddit 官方 API 读指定版块的新帖和新评论（只读凭据，不登录、不抓网页）。
2. 用关键词筛出相关的，存进本地 `radar.sqlite3`。
3. 生成 `report.html` 并打开：每条带直达链接，点开就能用自己的账号回复。
4. 进过报告的不再重复出现，所以每次看到的都是新的。

**它不会替你发帖。** 回复要你自己发，并在提到 LeeAB 时说明身份（美国 FTC 的背书
指引要求披露利益关系）。这不是谨慎，是前提：自动发的推广回复会让账号被封、
域名被整个拉黑，那之后连真实用户的推荐都发不出去。

## 怎么跑

    run.bat                 扫一轮（读 config.json），生成并打开报告
    run.bat --sample        用离线样例数据跑，不联网（没有凭据时用这个）
    run.bat --limit 10      这次报告里最多放 10 条
    run.bat --again         重新打开上一份报告，不扫描
    run.bat --stats         看看各个关键词分别带来了多少条、有多少是噪音

## 配置

把 `config.example.json` 复制成 `config.json` 再改。**`config.json` 不进 git**，
凭据只留在这台机器上。

要填的两个值这样拿：

1. 打开 https://www.reddit.com/prefs/apps
2. 点「create another app」，类型选 **script**，名字随便（比如 `anki-radar`），
   redirect uri 填 `http://localhost:8080`
3. 创建后页面上：应用名下面那串是 **client_id**，secret 那一栏是 **client_secret**
4. **不需要填账号密码。** 这个工具用的是只读凭据，拿到的令牌没有发帖权限——
   哪天有人给它加了发帖代码，那段代码会当场失败，而不是悄悄发出去一条。

## 关键词怎么选

Reddit 的搜索和这里的匹配都是「整词/原句」，**不支持「同时包含 A 和 B」**。
所以不要写 `anki collection too large` 去限定范围——那只会匹配一字不差写出这一
整串的帖子。要选**本身就只有 Anki 用户才会说**的词：

    AnkiWeb                      提到它的基本都和同步有关
    collection is too large      报错原文的核心片段，这批人当下就需要方案
    self-hosted sync server      已经在考虑自己搭服务器的人，离付费最近
    anki sync                    抱怨时最常见的说法
    media sync                   媒体同步慢；偏泛，跑两周看噪音大不大

## 文件

    radar.py            主程序
    reddit.py           Reddit 只读客户端（标准库，无第三方依赖）
    store.py            本地 SQLite 存储与去重
    sample_posts.json   离线样例数据
    config.json         你的配置与凭据（不进 git）
    radar.sqlite3       本地数据库（不进 git）
    report.html         最近一次报告（不进 git）
