# 微博登录态与 API 认证

## 问题概述

微博 AJAX API 分为两类：

| 类型 | 示例 | 要求 |
|------|------|------|
| 公开 | `hot_band`、`side/search` | curl_cffi TLS 伪装即可 |
| 需登录 | `statuses/search`、`m.weibo.cn API` | 浏览器访客验证 + 完整 Cookie |

纯 HTTP（curl_cffi）QR 码登录拿到的 `SUB` Cookie **无法访问需登录的 API**，返回 `ok=-100` 或重定向到 `retcode=6102`（新浪通行证验证失败）。

## 根因：访客验证系统

微博在 `passport.weibo.com/visitor/visitor` 部署了 **浏览器指纹验证系统**（`fp/1.2.1.umd.js`）：

```
浏览器访问 → JS 采集 20+ 维度指纹 → AES+RSA 加密 → POST /sso/bd → RID → TID → session 被标记为"已验证"
```

纯 HTTP 请求（即使 TLS 指纹伪装到 chrome131）**不执行 JavaScript**，永远无法通过这步验证。服务端看到该 `SUB` 对应的 session 未经验证，直接拒绝所有需登录的 API。

## 解决方案：nodriver headless 浏览器

使用 [nodriver](https://github.com/ultrafunkamsterdam/nodriver) 启动 headless Chromium 完成登录：

```bash
pip install nodriver
python scripts/init/weibo.py
```

**流程**：

1. 浏览器导航到 `weibo.com/newlogin?tabtype=weibo&openLoginLayer=1`（直达 QR 码页面，无需手动点击）
2. 访客验证自动通过（nodriver 有反检测能力，`navigator.webdriver` 被隐藏）
3. QR 图片下载到 `/tmp/weibo_login_qr.png`
4. 用户扫码后浏览器自动跳转，提取所有 Cookie

**对比**：

| 方式 | `navigator.webdriver` | 访客验证 | Cookie 可用 |
|------|----------------------|----------|------------|
| curl_cffi HTTP | N/A（无 JS） | ❌ 未触发 | ❌ |
| Playwright headless | `true` | ❌ 被检测 | ❌ |
| rebrowser-playwright headless | `true` | ❌ 补丁未生效 | ❌ |
| **nodriver headless** | `false` | ✅ 通过 | ✅ |

## 关键 Cookie

浏览器登录后 .weibo.env 保存 13 个 Cookie：

| Cookie | 用途 |
|--------|------|
| `SUB` | 用户身份令牌 |
| `SUBP` | 辅助身份校验 |
| `SCF` / `XSRF-TOKEN` / `X-CSRF-TOKEN` | CSRF 防护令牌（HTTP 模式缺失） |
| `WBPSESS` | 会话标识 |
| `SSOLoginState` | SSO 登录状态 |
| `ALC` / `ALF` | 登录生命周期 |
| `SRF` / `SRT` | 访客验证令牌（HTTP 模式缺失） |
| `SVB` / `tid` | 访客标识 |

纯 HTTP 模式拿到的 `SUB` 单独存在时**无法通过 API 认证**——缺少 `SCF`、`SRF`、`SRT` 等访客验证令牌。

## 死掉的 API

以下 PC 端 AJAX API 已被微博移除或封锁，不要使用：

- `weibo.com/ajax/statuses/hot_band_detail` — 返回 404 "你访问的地址不存在"
- `weibo.com/ajax/statuses/search` — 返回 `ok=-100`
- `weibo.com/ajax/profile/info` — 返回 `ok=-100`
- `weibo.com/ajax/search/suggest` — 返回 `ok=-100`

## 替代：m.weibo.cn 移动端 API

```
GET https://m.weibo.cn/api/container/getIndex?containerid=100103type%3D1%26q%3D{url_encode(话题名)}&page_type=searchall
```

需要携带完整浏览器 Cookie 字符串。返回 `data.cards[]`，其中 `card_type=9` 为微博正文。

**注意**：m.weibo.cn 也有反爬机制，不带 Cookie 或 Cookie 无效时会返回「Sina Visitor System」HTML 页面（非 JSON）。
