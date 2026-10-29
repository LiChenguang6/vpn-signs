# Nano 每日签到 · AstrBot 插件（待实号验证版）

此插件是根据 `Nano_v8a.apk` 的静态分析独立编写的 Python 适配器，不是 Nano 原始源码，也不是修改版 APK。Nano 签到 API 的路径已在 APK 中找到；HTTP 方法、完整响应格式及令牌格式尚未通过真实账号核实。本版不能宣称已完成真实签到或 QQ 群发送。

## 已实现

- 每日定时执行；时区和时间在 AstrBot 插件配置面板修改。
- 同一账号每日防重；状态保存在 AstrBot 的 `data/plugin_data/nano_checkin/state.json`。
- 自动失败重试，间隔 15 分钟，每日最多 3 次；当天错过计划时间会补跑。
- 指定 AstrBot 会话列表推送。推送失败后单独重试，不重新签到。
- 带访问令牌的独立可视化面板：计划时间、今日状态、次数、待推送数量、最近 30 次记录、立即执行按钮。
- 默认关闭签到和独立面板，避免未配置时自动请求服务。
- API 强制 HTTPS、保持证书验证、不跟随重定向；不写入或输出账号令牌。

## 群晖安装（已有 AstrBot 和 QQ 适配器）

适配接口参考 AstrBot 4.29.3 官方源码，并已在真实 SDK 中通过配置加载、插件生命周期、面板鉴权和消息链构造测试；使用 Python 3.12+。不要通过本插件重新创建或覆盖你现有的 AstrBot / QQ 容器。

1. 下载插件 ZIP，解压后将整个 `astrbot_plugin_nano` 文件夹放到 AstrBot 持久化目录中的 `data/plugins/`。以容器内部路径为准：应能看到 `data/plugins/astrbot_plugin_nano/main.py`，不要多嵌套一层目录。也可使用 AstrBot 版本支持的本地 ZIP 安装功能。
2. 在群晖 Container Manager 中给**现有 AstrBot 容器**添加环境变量：
   - `NANO_AUTH_DATA`：你自己的 Nano 登录令牌。请在 Nano 官方登录流程或你自己浏览器开发者工具中取得 `auth_data` / Authorization 的真实值。不要填订阅链接，不要发到聊天或上传 GitHub。
   - `NANO_PANEL_TOKEN`：随机生成至少 24 字符的独立面板密码，仅在启用面板时需要。不能使用账号令牌代替。
3. 保留现有 AstrBot `data` 目录的持久化挂载；容器重启策略设置为 `unless-stopped` 或 `always`。群晖必须在计划时间保持开机，AstrBot 和 QQ 适配器也须在线。容器重启策略不能保证断电、NAS 休眠或退出 QQ 登录后的运行。
4. 重新创建/启动容器使新环境变量生效，加载插件并确认 `aiohttp` 依赖安装成功。
5. 在 AstrBot 的插件配置中填写：
   - `api_base`：你已确认可信的 Nano HTTPS API 根地址，包含 `/api/v1`。该项刻意留空，不自动信任 APK 远程配置的后续变更。
   - `auth_env`：默认 `NANO_AUTH_DATA`。
   - `bearer_prefix`：默认 false，直接将环境变量内容作为 Authorization；仅服务要求时开启。如果令牌本身已有 `Bearer `，不要再次开启。
   - `daily_time`：例如 `08:00`；`timezone` 默认 `Asia/Singapore`（与北京时间均为 UTC+8）。
6. 将你的 QQ 账号设置为 **AstrBot 管理员**，在目标 QQ 群发送 `/nano会话`。把返回的完整 `unified_msg_origin` 填入 `targets` 列表，不能只填群号。常见 OneBot/NapCat 适配器可主动推送；AstrBot 官方说明 QQ 官方 API 适配器不支持这里使用的 `Context.send_message`，不保证适用于 QQ 官方机器人。
7. 先开启 `enabled`，发送 `/nano签到`，到 Nano 原客户端核对当日状态和奖励，再检查群推送。**首次启用会在到达计划时间后自动尝试；需要手动先验时可先将时间设到当日稍后。**
8. 验证成功后保存正式计划时间。配置更改后重新加载插件以确保面板监听等设置生效。该插件仅运行一个账号；同一数据目录只运行一个 AstrBot 实例。

## 可视化面板

AstrBot 自带的插件配置页面用于设置开关、API、时间、时区和群会话。另有独立状态面板：

1. 设置 `NANO_PANEL_TOKEN` 后，打开 `panel_enabled`。
2. Docker 容器内将 `panel_host` 改为 `0.0.0.0`，`panel_port` 默认为 `6198`；给现有容器增加 NAS 端口到容器端口 `6198` 的映射。`127.0.0.1` 默认仅容器自身可访问。
3. 使用群晖 HTTPS 反向代理指向映射后的面板端口，再用你配置的 NAS 域名访问。不要直接把未加 TLS 的端口开放到公网。
4. 输入独立面板令牌连接。令牌仅存在当前页面内存，不保存在 localStorage，不放在 URL 中。

页面中“任务完成”包括服务端返回 `can_checkin=false` 的情况，此时表示本插件不会再提交，不等于已核实获得签到奖励。首次必须在 Nano 客户端核实。

## 指令

仅 AstrBot 管理员可用：`/nano会话`、`/nano签到`、`/nano状态`。

## 接口约定和限制

当前适配器尝试 `GET /user/checkinStatus`，要求 `data.can_checkin` 为布尔值；可以签到时调用 `POST /user/checkin`。明确的 `data=true`、`success=true`、`status=true` 才直接报告成功，否则复查状态。未知结构会提示协议不兼容，不凭 HTTP 200 判定成功。

APK 公共配置在分析时指向 `https://account.alibaba.alicdn.men/api/v1`；这是从样本读到的第三方地址，不代表 Alibaba 官方服务，也不构成可信背书。无凭据只读请求返回空响应，未证实有效 JSON 或登录契约。请先自行核对当前服务地址；本插件不会使用远程配置自动替换你填写的地址。

若出现“未识别到 data.can_checkin”，需要脱敏后的成功/失败响应样本或原始 Flutter 源码进一步适配。不要绕过验证码、签名校验或服务端限制。

令牌过期必须重新登录更新容器环境变量；本版不实现密码登录和令牌刷新。网络在服务端提交成功后中断时，下一次会先查询状态，但服务器本身仍需正确实现防重。消息发送后进程在落盘前崩溃，可能导致极少数重复推送；不承诺端到端 exactly-once。

## 测试

```sh
python3 -m pip install -r requirements.txt
python3 -m unittest discover -s tests -v
```

测试使用本地模拟服务和虚拟令牌，不调用真实签到，不发送 QQ 消息。真实服务、QQ 和群晖端到端验证仍需用户环境。

已通过 14 项自动测试，另通过 AstrBot 4.29.3 实际 SDK 加载测试及真实 Chromium 桌面/手机宽度面板交互测试。可在安装了 AstrBot 的同一 Python 环境执行 `python3 tests/sdk_smoke.py` 重现 SDK 检查。
