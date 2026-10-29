# Nano 多账号签到 · AstrBot 内置视图

GitHub 直接安装 / 更新地址：

```text
https://github.com/LiChenguang6/vpn-signs
```

**v0.3.0 使用 AstrBot 插件详情中的“视图”管理账号。** 登录 AstrBot 后即可进入，不再启动独立网页服务，不需要 `6198` 端口、反向代理或额外管理密码。

## 安装或从旧版升级

1. 使用 **AstrBot 4.29.3+、Python 3.12+**。在「插件 → 通过链接安装」输入上述 GitHub 地址，或更新已安装的 Nano 插件。
2. 本次更新后重载一次以加载新版代码和发现视图。以后配置字段填错，只需在视图里改正保存，不需要删除插件或重新加载。
3. 打开 **插件 → Nano 多账号签到 → 视图 → Nano 签到管理**。部分 AstrBot 版本也可从侧栏的插件视图入口进入。
4. 如果旧版已加载失败，可以用同一仓库链接覆盖安装新版。保留原有 AstrBot `data/config` 和 `data/plugin_data` 数据，不要通过删除账号配置来解决加载问题。

仓库根目录包含 `main.py`、`metadata.yaml`、`_conf_schema.json` 和依赖；视图入口为 `views/nano/index.html`，使用官方 `AstrBotPluginView` bridge 和 Dashboard Web API。

## 配置出错如何恢复

- 错误的时间、时区、API、群会话或账号字段不会阻止插件加载，任务会暂停，视图显示错误提示。
- 直接在视图修正并点击「保存全部配置」。保存成功后同一个插件实例恢复工作。
- 视图提交的新配置若不合规，会明确报错，保留表单和上次已保存配置，不会让插件退出。
- 修复已存在的错误配置前，原配置备份到 `data/plugin_data/nano_checkin/config-recovery-*.json`，权限为 `0600`。格式损坏的字段会在编辑页面中以安全默认值显示，保存前请核对账号和目标群列表。
- 已保存密码不会返回浏览器；同一账号的密码留空时保留原值。更换邮箱或 API 地址时要重新输入密码，避免把旧密码提交给另一个服务。

上述恢复针对插件配置字段及其格式。手工编辑底层 JSON 导致文件无法被 AstrBot 自身解析、磁盘不可写等框架/存储问题，仍需要修复对应文件或权限，不应删除账号数据。

## 多账号、每日签到和群推送

1. 在视图中核对默认 Nano API 根地址（包含 `/api/v1`）。预填地址来自用户上传的 Nano 1.0.10 样本公共配置：`https://account.alibaba.alicdn.men/api/v1`。它是第三方地址，可能变化，请确认仍为自己的 Nano 服务；不是订阅链接，也不代表 Alibaba 官方服务。
2. 点击「添加账号」，填写备注、邮箱/账号和密码，可添加多个账号。每个账号可独立启用、覆盖 API 地址及指定群会话。
3. 保存后先点击「测试登录」，再用「立即签到」核验。无需自己获取登录令牌，也不需要设置账号环境变量。
4. 用 **AstrBot 管理员账号**在目标 QQ 群发送 `/nano绑定群`，即可加入全局推送列表；可绑定多个群。也可用 `/nano会话` 获取完整会话标识后填到视图中，不能只填写 QQ 群号。
5. 账号专属推送会话非空时覆盖全局会话，留空则继承全局列表。
6. 设置每日时间和时区，验证后开启自动签到。默认 `08:00`、`Asia/Singapore`（UTC+8）。手动签到不要求打开全局自动开关，但会跳过单独禁用的账号。

每个账号独立登录、防重和重试；一个账号失败后会继续处理其他账号。每日每账号最多尝试 3 次，自动重试间隔 15 分钟。同一目标的本次结果合并推送；发送失败或超时会持久化重试，不会因推送失败重复签到。任务总开关关闭时自动重试也暂停。

| 管理员指令 | 功能 |
| --- | --- |
| `/nano签到` | 手动执行全部启用账号 |
| `/nano状态` | 查看账号完成情况与待推送数量 |
| `/nano会话` | 获取当前完整会话标识 |
| `/nano绑定群` | 当前群加入全局推送列表 |
| `/nano解绑群` | 从全局列表移除当前会话；账号专属目标请在视图修改 |
| `/nano测试推送` | 向已配置目标发送测试消息，不执行签到 |

常见 OneBot/NapCat QQ 适配器可主动推送。AstrBot QQ 官方 API 适配器不支持这里使用的 `Context.send_message`，不保证适用；机器人须已加入目标群且可以发言。

## Nano APP 登录请求协议适配（v0.3.2）

原版 Nano v1.0.10 样本包含 Dio 网络库；Dio 发送 Map 请求体通常使用 JSON。V2Board 公开接口资料亦使用 JSON 作为 `POST /passport/auth/login` 的请求体，但**不能单凭 APK 字符串证明此 Nano 版本一定使用 JSON**。旧版插件以 `aiohttp(data=字典)` 发送 form 表单，与此不一致。

- **v0.3.2 登录默认改为 JSON**：`Content-Type: application/json` 和 `{"email": "...", "password": "..."}`；只用于登录请求。
- 每个账号增加“登录提交格式”选择：**JSON（默认）** 或 **表单（旧版兼容）**。旧账号在更新后默认使用 JSON，配置的密码会保留。
- 点击“测试登录”只提交一次，不会自动轮流试两种格式，降低错误登录的限流风险。若仍收到 HTTP 200 空正文，优先确认 APP 实时远程配置的 API 地址及服务端特定请求头；不要关闭证书验证。
- 已知 APK 远程配置入口：`https://nanoapi.oss-ap-northeast-1.aliyuncs.com/u.json`。当前插件**不会自动将账号密码发送到远程配置返回的未知域名**；修改地址前必须核实。
- 登录验证通过后再手动签到，最终用 APP 中的积分/流量奖励确认结果，之后才开启自动定时任务。

## “网络、TLS 或 JSON 响应异常”排查（v0.3.1）

旧版把 DNS 解析失败、连接超时、TLS 握手失败、代理不可用，以及服务返回 HTML/空响应全部显示为同一句话。v0.3.1 在“测试登录”和签到结果中区分这些故障；**不会展示密码、令牌或服务端原始响应**。

1. **先核对 API 根地址**：格式为 `https://实际服务域名/api/v1`，不是节点订阅链接，也不是 `/passport/auth/login` 完整接口路径。插件预设的 `account.alibaba.alicdn.men` 来自旧 APK 中的公开远程配置，**不保证现在仍有效**。
2. **用群晖 AstrBot 容器自身测试**（不是 Windows 浏览器）：
   ```sh
   python - <<'PY'
   import socket
   host = "account.alibaba.alicdn.men"  # 改成面板配置的实际域名
   try:
       print("DNS:", [r[4][0] for r in socket.getaddrinfo(host, 443)])
   except OSError as exc:
       print("DNS 检测失败:", type(exc).__name__)
   PY
   curl -Iv --connect-timeout 10 https://account.alibaba.alicdn.men/api/v1
   ```
   这只是连接诊断；API 根路径返回 404/405 或其他状态不必然表示签到接口不可用。检查 TLS 验证、响应头、状态码，不要把登录密码放进命令。
3. **如果报 DNS**：检查 NAS/Docker DNS 和实际服务域名。**如果报 TLS**：检查证书是否匹配域名、系统时间、HTTPS 代理。不要设置 `ssl=False` 绕过验证。
4. **如果报超时或代理失败**：容器内的 `HTTPS_PROXY` / `HTTP_PROXY` 与你的 Windows Clash 设置相互独立；需确认 NAS 容器确实能访问代理服务器。
5. **如果返回空正文或 HTML**：可能是服务端已迁移、反代/验证码页面、HTTP 200 空响应或接口协议变化。APK 字符串只能确认路径存在，不能证实当前服务端 API 可用。
6. 修好地址后，在视图先点**测试登录**，确认成功，再点**立即签到**，最后启用每日自动签到和目标群推送。

**隐私提醒**：不要公开上传 AstrBot 账号配置、抓包中的 Authorization、密码或 Cookie。插件不会自动相信远程发现的 API 地址并向其提交账号密码，改地址前请自行确认服务可信。

## 群晖运行与数据保护

使用现有 AstrBot 容器和 QQ 适配器，持久化挂载 AstrBot 的 `data` 目录，并设置容器重启策略。内置视图直接使用 AstrBot 登录鉴权，无需增加端口映射；旧版独立面板端口设置已不再使用。

账号密码保存在本地 `data/config/nano_checkin_config.json`。密码字段默认遮蔽显示，**遮蔽不等于加密**。加载及视图保存时设置文件权限为 `0600`；保护 NAS 管理权限、AstrBot HTTPS 入口、配置和修复备份，不要上传真实配置或把密码发到聊天。

状态保存在 `data/plugin_data/nano_checkin/state.json`，不保存邮箱、密码或登录令牌。令牌仅在本次登录签到的内存中使用，不回传视图或群消息。同一数据目录只运行一个 AstrBot 实例。

## 支持范围与验证

接口采用 Nano 样本出现的 V2Board 风格路径：JSON（可选表单）`POST /passport/auth/login` 提交 `email`、`password`，读取 `data.auth_data`；查询 `GET /user/checkinStatus` 的布尔字段 `data.can_checkin`，允许时 `POST /user/checkin`。服务可能存在定制或额外验证，不是通用的所有 VPN 服务签到插件。

账号请求保持 HTTPS、证书验证且不跟随重定向，不绕过验证码、2FA、设备签名或服务限制。未知响应不会仅凭 HTTP 200 判定成功。

测试包括配置错误恢复、实际 AstrBot 插件管理器加载和视图发现、官方后端请求路由，以及真实 Chromium 的沙箱 iframe + 官方 bridge SDK 操作。测试使用本地虚拟账号；**尚未用用户真实 Nano 账号和 QQ 群端到端验证**，首次请核对原客户端签到奖励和群消息。

[版本记录](CHANGELOG.md) · [v0.3.0 视图集成测试截图](docs/Nano-view-v0.3.0.png) · [早期 APK 静态分析](docs/Nano-APK-analysis.md)

## 开发检查

```sh
python3 -m pip install -r requirements.txt
python3 -m unittest discover -s tests -v
```

已安装 AstrBot SDK 的环境还可执行：

```sh
python3 tests/sdk_smoke.py
python3 tests/install_smoke.py
python3 tests/install_smoke.py --remote
```

浏览器集成测试需 Playwright、Chromium、AstrBot SDK 和 uvicorn：`python3 tests/browser_smoke.py`。截图为本地测试宿主中的沙箱视图，包含模拟数据。
