# Nano_v8a.apk 静态分析

样本 SHA-256：`b425456130fd76220dae3f571bb23c5aae40da85b8ceefb747c70d7b891bf61c`

## 已确认的证据

- 包名 `com.nanocloud`，应用 Nano，版本 `1.0.10`，minSdk 24，targetSdk 35。
- Flutter 应用，APK 包含 `lib/arm64-v8a/libapp.so`、`libflutter.so`，以及 sing-box 相关 `libbox.so`。当前样本只有 ARM64 Android 原生库。
- `libapp.so` 字符串中包含 `/api/v1`、`/passport/auth/login`、`auth_data`、`Authorization`、`Bearer`、`/user/checkin`、`/user/checkinStatus`、`can_checkin`、`last_checkin_at`、`checkin_days`。
- 这些字符串能证明接口路径和字段存在，不能独立证明 HTTP 方法、鉴权格式及完整响应结构。本次插件将常见协议作为明确标注的待验证假设。
- APK 中存在公共配置 URL `https://nanoapi.oss-ap-northeast-1.aliyuncs.com/u.json`。只读获取时其中 `url` 指向 `https://account.alibaba.alicdn.men/api/v1`。配置可变，不能当作永久地址或域名归属背书。
- 无凭据请求上述 `/user/checkinStatus` 返回 HTTP 200 但响应为空，不能判为接口可用或签到成功。本次未进行任何真实账号登录或签到请求。

## 自动签到与保活的判断

AndroidManifest 已声明前台服务、开机广播、电池优化豁免请求等权限，并注册 VPNService / ProxyService。声明权限不等于用户已授权，也不等于开机行为已实现。

`com.phantom.bg.ProxyService` 和 `VPNService` 的 `onStartCommand` 都返回 `0x2`，即 `START_NOT_STICKY`。但把它替换为 `START_STICKY` 不能直接实现每日签到，也不能保证 Flutter 引擎、VPN 状态及账户会话能在空 Intent 重启时正确恢复。

签到业务位于 Flutter AOT 原生代码，普通 smali 反编译不等于恢复 Dart 源码。本次没有制作或发布“自动签到+永久保活 APK”，也没有进行 Android 真机后台、Doze、进程重启或重签名兼容性测试。完成 Android 修改版需要 Nano 的可构建 Flutter 源码，或继续完成可靠的业务逻辑恢复、重建和真机验证；不能把仅增加权限或修改服务返回值的包当作完成品。

即便有源码，Android 也不保证永久后台运行。合理实现应使用系统支持的周期任务，在用户可见且授权的情况下使用必要的前台服务；强制停止、撤销授权、厂商省电规则会影响执行。

## 群晖 / AstrBot 可行性

如果签到是普通 HTTPS API，便不依赖 Android 的 ARM64 原生库，可独立编写 Python AstrBot 插件，运行在支持 AstrBot 的 x86_64 或 ARM64 群晖设备上。

本次已实现独立插件：每日调度、持久化防重、失败重试、指定会话推送、独立鉴权面板和 AstrBot 配置面板。插件默认关闭，令牌经 NAS 容器环境变量注入。

尚未证实的条件：真实签到是否需要额外签名/验证码/设备信息；实际响应字段；登录令牌格式及有效期；目标 AstrBot / QQ 适配器版本和群会话；NAS 上的运行与推送。任何一项不兼容都需要进一步适配。

分析依据：本地 Apktool 解码输出、样本原生库字符串，以及 AstrBot 4.29.3 官方源码参考提交 `abf683071952375ec36baa4cad6b6f3d9c9bb03f`。已通过 14 项本地自动测试，另通过真实 AstrBot SDK 的配置解析、导入、生命周期、面板鉴权和消息链构造测试；未进行真实 QQ 发送和账号签到。
