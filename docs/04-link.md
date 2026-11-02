# 链路与断连

## 判决实验：问题在 Linux 蓝牙栈

同一台设备、同一份代码、同一套 bleak API：

| 主机 | 时长 | 帧数 | 错误 | 重连 |
|---|---|---|---|---|
| **Windows** | 117.0s | 3839 | 0 | **0** |
| Linux | ~40.4s | — | — | 每 40.4s 一次 |

【实测】Windows 端数据：

```
[ 26.9s] frames=20    errs=0 reconnects=0 link=True
[ 41.9s] frames=660   errs=0 reconnects=0 link=True   ← Linux 已断过两次
[ 71.9s] frames=1930  errs=0 reconnects=0 link=True
[117.0s] frames=3839  errs=0 reconnects=0 link=True   ← 近 Linux 的 3 倍
帧率 ≈ 42 帧/秒 ≈ 24 ms/帧
```

**设备固件、协议、帧格式、本软件全部排除。**

---

## 根因

【实测】btmon 抓包（原始字节 `/tmp/btm-new.cap`，btsnoop MONITOR，16022 条记录）：

```
< HCI Command: LE Remote Connectio.. (0x08|0x0021) plen 3
        Reason: Unknown Connection Identifier (0x02)
> HCI Event: Command Complete (0x0e) plen 6
        Status: Invalid HCI Command Parameters (0x12)
t=+40.4s: LE Connection Update Complete → Connection Timeout (0x08)
```

同一份抓包里，控制器给的是 **interval 50 ms / supervision timeout 420 ms**，
设备要的是 **7.5 ms / 4000 ms**（完整片段见 [07-hardware.md](07-hardware.md)）。

**原记录错了三处**（证据、行号与抓包解码见 [07-hardware.md](07-hardware.md)）：**① 50 ms / 420 ms 是内核默认值，不是 `btusb` 写死的**；**② 拒绝是句柄还没进 `BT_CONNECTED`（命令的 Reason 字段是 `0x02`），不是「参数非法」**；**③ 「420 ms 撑了 40.4 秒」把寿命和静默混成了一个数**。

**② 的机制是时序竞态。** 内核 `hci_event.c:6629` 先查句柄，句柄查不到、或状态不是 `BT_CONNECTED` 就回 `0x02`；参数检查在 `:6634`/`:6638` **之后**。源码链：`conn->state = BT_CONFIG` 在 `:5758`，`= BT_CONNECTED` 在 `:6527`（`LE Read Remote Features Complete` 的回调，以 `if (conn->state == BT_CONFIG)` 为前提），而 `LE Read Remote Features` 命令在 `:5789` 就发出去了 —— 状态翻转必须等控制器回话，**不可能早于参数请求。这是状态机必然，不是竞态。**

【实测】**29 次参数请求全部早于对应的 `LE Read Remote Features Complete` 事件**（该事件在这份抓包里只出现 5 次，5 次都比对应的参数请求晚 **199–200 ms**），建链后 **3.9–63.9 ms**（均值 30.5 ms）到达。

**恒定的量是连接龄，不是末包时距。** 三次 `0x08` 死链的**连接龄**是 **40.380165 s / 40.366051 s / 40.374077 s**（**spread 14 ms**），而「死亡时刻 − 设备末个 ACL」是 3.9990 s / 2.7977 s / 40.374 s（**spread 1.20 s，非常量**）—— 监督超时若真在生效，这个差值必须恒定。**所以 40.4 s 是连接龄函数，不是末包时距函数。**


**这份抓包里没有空口 PDU**（mon_op 直方图只有 `ACL_TX=4658` / `ACL_RX=74`，全是 L2CAP/ATT 数据），所以「设备在死前 N 秒停止发射」在这份数据里**不可观测**；设备那 5 秒一次的「心跳」是它发的 **ACL 响应**，不是空口包。三种静默（宿主侧 / 设备侧 / 整条空闲）的口径见 [07-hardware.md](07-hardware.md)。

**设备要的 7.5 ms / 4000 ms 在内核判据下合法。** `hci_check_conn_params`（本机内核头 `the Linux kernel's include/net/bluetooth/hci_core.h:2254`）里两个 `3200` 单位不同：`max > 3200` 是**连接间隔**上限（1.25 ms → 4000 ms），`to_multiplier > 3200` 是**监督超时**上限（10 ms → **32000 ms**）。

---

## 修复已生效（2026-10-21 晚，设备上线后实测）

**上一版文档里「改这些能否解决断链，设备不在空中，没验证过」这一句已经过时。**
2026-10-21 晚 Vero 整机（`E7:97:7D:XX:XX:XX`，名字 `UMVXEXAMPLE01`）上电在线，
本机 `/etc/bluetooth/main.conf` 的 `[LE]` 四键已生效，
**用 Vero 设备本身**（不再是别的设备）完成了建链协商。

现网配置（`[LE]` 段，行 251-254；`hci0` 在改配置**之后**才重建，bluetoothd 于
14:46:51 启动，配置 mtime 14:46:47）：

```
[LE]
MinConnectionInterval=6          # 6 x 1.25 ms = 7.5 ms
MaxConnectionInterval=6          # 6 x 1.25 ms = 7.5 ms
ConnectionLatency=0
ConnectionSupervisionTimeout=400 # 400 x 10 ms = 4000 ms
```

内核活体读数（`/sys/kernel/debug/bluetooth/hci0/`）：`conn_min_interval` 6、
`conn_max_interval` 6、`conn_latency` 0、`supervision_timeout` 400 ——
与上面四行逐项吻合。

【实测】btmon 抓到的建链协商结果（**对端就是 Vero 设备本身**）：

```
> HCI Event: LE Meta Event (0x3e) plen 31
      LE Enhanced Connection Complete (0x0a)
        Status: Success (0x00)
        Handle: 65
        Peer address: E7:97:7D:XX:XX:XX (OUI E7-97-7D)
        Connection interval: 7.50 msec (0x0006)
        Connection latency: 0 (0x0000)
        Supervision timeout: 4000 msec (0x0190)
@ MGMT Event: Device Connected (0x000b) plen 41
        Name (short): UMVXEXAMPLE01
```

**修复前**（同一台主机的历史抓包）控制器给的是 **50.00 ms / 420 ms**，
修复后主机实际下发 **7.5 ms / latency 0 / 4000 ms** —— 正是设备请求的参数。

**这次建链里没有出现 `LE Remote Connection Parameter Request` 事件**（整份抓包
该事件计数 **0** 次）。`LE Features` 里的 `Connection Parameter Request Procedure`
只是一个 feature bit，不是参数请求事件本身。这与修复的机制一致：主机直接用设备
要的参数建链，设备**不需要**再发参数请求，也就绕开了「句柄还没进 `BT_CONNECTED`
→ 内核以 `0x02` 拒绝 → 控制器侧连接更新过程无人应答」这条失败路径。

### 40.4 秒断链：已消除（实测，不是推断）

**这一条现在有实测支撑，不再是「未验证」。** 同一份 btmon 抓包里，
Vero 设备这一轮一共建链 **3 次**，三次的协商结果**都是** 7.5 ms / 0 / 4000 ms：

| # | 建链时刻 | 断开时刻 | 连接龄 | 断开原因 |
|---|---|---|---|---|
| A | 11.998477 s | 16.880372 s | 4.882 s | 本地主机主动断开（`0x16` / `0x13`） |
| B | 60.456589 s | 171.882121 s | **111.426 s** | 本地主机主动断开（`0x16` / `0x13`） |
| C | 944.258849 s | 抓包结束时仍在 | **≥ 142.550 s** | — |

**三次都没有 `Connection Timeout (0x08)`。** 整份抓包里
`LE Connection Update Complete` 事件计数 **0** 次、
`Status: Connection Timeout` 计数 **0** 次，
`Disconnect Complete` 只有 **2** 次、两次的 Reason 都是**本地主机主动断开**
（`Connection Terminated By Local Host (0x16)`），不是链路超时。

**第 C 条链路活到抓包最后一个事件，连接龄 142.550 s**，
期间 TX 12723 个 ACL 包、RX 4678 个 ACL 包。
**设备侧（RX，即设备的 ATT notification）间隔中位数 0.030 s、最大 1.333 s，
一次都没超过 5 s**；主机侧 TX 间隔中位数 0.009 s、均值 0.011 s，
其中有 **1 次 25.921 s 的 TX 停顿**（974.080 s 起，1000.001 s 恢复）——
**停顿期间设备仍在按 30 ms 节拍发 notification**，所以那是主机应用层停发，
不是链路静默。**142.550 s 已经越过修复前 40.4 秒死链点三次以上，链路没有断。**

**空口也确认了设备当时在广播。** nRF52840 嗅探器抓的
`/tmp/le-40s-evidence/air_final_hold120.bin`（4612 帧）里，
目标设备地址 `E7:97:7D:XX:XX:XX` 出现 **594 帧**，
名字 `UMVXEXAMPLE01` 出现 **590 帧** —— 设备**在空口**，
不是「那轮不在空中」。

**结论：修复前 40.4 秒的连接龄函数断链，在修复后没有再出现。**
这条结论的适用范围是**这一台主机、这一台设备、这一组参数**，
不是对所有 BlueZ 主机的普适断言。

---

## 用户空间无法根治

**第 1 行的理由已被推翻**（`[LE]` 四键被读取且生效），**而且第 1 行现在也已被推翻**：
四键不只是「被读取且生效」，它**确实消除了 40.4 秒断链**（见上一节，实测）。
其余各行未重测。

| 尝试 | 结果 |
|---|---|
| `/etc/bluetooth/main.conf [LE]` 四个键 | **被读取且生效**（活体实测：内核 `le_supv_timeout` 42 → 2000），**且已实测消除 40.4 秒断链**（2026-10-21 晚，Vero 设备本身建链，连续 142.550 s 未断） |
| `btmgmt hci-cmd 0x2013` × 7 次 | 无效 |
| 换 USB 口 | 无效 |
| 停 Home Assistant | 无效 |
| USB 隔离 | 无效 |

**「420 ms 硬编码在内核态」已推翻**（那是内核默认值）。用户换到 Windows 后 117 秒零重连，问题消失。
**「用户空间无法根治」这一节的标题也已过时** —— `[LE]` 四键就是根治手段，
它把建链参数换成了设备要求的值，设备因此不再发参数请求。
真正没被根治的是**别的 Linux 主机**上是否也这样：本仓库的结论只覆盖本机实测过的那一台。

---

## 补偿层
软件层做的是自愈，不是预防：
| 参数 | 值 | 作用 |
|---|---|---|
| `RECONNECT_INTERVAL` | 0.25s | 固定间隔重试 |
| `RECONNECT_JITTER` | 0.15s | 抖动，避免退避错过广播窗口 |
| `REASSERT_FRAMES` | 60 | 重连后立刻补发位置 |
| `FRAME_INTERVAL` | 0.020s | 20 ms 发送泵 |
| `CLIENT_GONE_SECONDS` | 2.5 | 客户端消失判定 |



【实测】Linux 上验收 300 秒：7 次断链，每次恢复约 **2.9 秒**，补发 60 帧，
写错误 0。

**代价**：每次断链有约 2.9 秒的控制真空期。Windows 上不发生。

---

## 被我误判过的观察

**「100% 行程能一直保持不掉线」** —— 这条观察发生在 **Windows** 上，而
Windows 本来就不断。它不构成任何 Linux 侧的证据。

我当时把它当作 Linux 端的可能线索引用了一次，已撤回。同类错误还有：

- 「设备一直在和某个蓝牙适配器保持自动重连」→ 之前的观察都不被动
- 「按住压力控件 70 秒读数无变化」→ 早期实验用的是旧解码模型，结论无效
- 「`btusb` 硬编码 50 ms / 420 ms」→ 那是**内核默认值**（`hci_core.c:2489-2492`，再由 `hci_conn_params_add()` `:2290-2293` 写进每设备参数），`btusb` 里碰 `le_conn_*` 的符号 **0 个**
- 「设备请求被判非法（0x12）」→ 命令的 **Reason 字段是 `0x02`**（句柄还没进 `BT_CONNECTED`，内核 `hci_event.c:6629` 先查句柄、`:6634`/`:6638` 再判参数），`0x12` 是另一个包的 status 字段；设备的请求在内核判据下合法，监督超时上限 **32000 ms**
- 「420 ms 撑了 40.4 秒，说明固件没执行那个寄存器」→ 寿命 **40.379249 s** / 40.365065 s，但**宿主侧静默只有 0.315170 s / 0.316850 s**（最后一个主机→控制器包到死亡），链路不是「安静地活了 40.4 秒」。**设备侧**静默却是 **3.999 s / 2.798 s / 40.374 s**（第三个 0x08 死亡的链路内**一个 ACL 包都没有**），**模式不一致，「设备自己按量程掉线」解释不了全部三次**。**「固件未执行该寄存器」这份抓包不支持，撤回。**
- 「`[LE]` 四键无效（内核层）」→ **被读取且生效**（活体实测：内核 `le_supv_timeout` 42 → 2000，已复原），**且已实测消除 40.4 秒断链**（2026-10-21 晚，Vero 设备本身建链，连接龄 142.550 s 未断）

---

## 链路诊断字段

WebUI「链路诊断」面板与 `/api/state`：

| 字段 | 含义 |
|---|---|
| `link_count` | 本次会话累计建链次数 |
| `link_lifetimes` | 最近 12 次连接寿命（秒） |
| `median_link_lifetime` | 中位数 |
| `reconnects` | 重连次数 |
| `recovery_seconds` | 累计恢复耗时 |
| `reassert_frames` | 补发帧数 |
| `connected_seconds` | 累计在线 |

### 连接时长曾显示负数

【已修】早期实现混用了单位：`performance.now()`（毫秒）减 `performance.now()/1000`（秒），
结果恒等于 `-0.999 × 页面打开秒数`；用户截图 `-27745.8s` 反推页面开了约 7.7 小时 —— 数字完全吻合。

---

## 其他主机层参数

| 项 | 值 | 来源 |
|---|---|---|
| 连接间隔 | 50 ms → **7.5 ms** | 内核默认值【实测】；改 `[LE]` 后实测协商为 7.5 ms |
| 监督超时 | 420 ms → **4000 ms** | 内核默认值【实测】；改 `[LE]` 后实测协商为 4000 ms |
| MTU | 23（ATT 默认） | App 不请求【源码】 |
| RSSI | 实测 -16 ~ -49 dBm | — |
| 驱动 | `btusb` 0.8 | 驱动版本本身没问题（本文的更正都与它无关） |

---

## 相关实现

| 文件 | 职责 |
|---|---|
| `vero/session.py` | 建链、20 ms 帧流、断链检测与恢复 |
| `vero/diagnose.py` | 链路寿命测量与报告 |
| `run_diagnose.py` | 诊断入口 |
