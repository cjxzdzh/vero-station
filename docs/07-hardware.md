# 硬件

## 设备

| 项 | 值 | 证据 |
|---|---|---|
| 厂商 | Amorlink 爱刻（北京缀梦科技） | — |
| App 包名 | `cn.zhuimeng.amorlink` | — |
| 分析版本 | 1.1.5 (57) 与 1.2.3 | — |
| 型号 | **Vero** | `normalizeSn("UMVx…")` → `"VX"` → `DeviceKind.VERO` |
| SN | `UMVXEXAMPLE01` | 实测 |
| MAC | `E7:97:7D:XX:XX:XX` | 实测 |
| BLE 固件 | `1.0.11` | caps 帧 |
| 行程 | 8 cm | App 设备表 |
| 行程编码 | `STROKE_MAX = 10000` | 协议 |

### BlueZ 别名 ≠ 广播名

`bluetoothctl` 里显示 `Amorlinkvex`，但那是**BlueZ 本地别名**，广播协议里
不存在。设备真正广播的名字是 **`UMVXEXAMPLE01`**。

早期实现按 `Amorlinkvex` 找设备，在 Windows 上永远找不到。发现方式是在
`discovery.py` 里同时用广播名和厂商服务 UUID 做双通道识别。

---

## 芯片：Telink LE501x

**固件字符串表直接给出**：

```
.\..\SRC\soc\arm_cm\le501x\integration\ls_soc_gpio.c
.\..\SRC\hal_driver\src\ls_hal_adc.c
.\..\SRC\module\tinyfs\tinyfs.c
```

> 注意：`FIRMWARE-REPORT.md` 开头一度写成「nRF52 系列」，第 503 行自己推翻了
> 那一行。**以字符串表为准。** 早期 Ghidra 语言 ID 也写错过
> （`ARM:LE:32:Thumb` 应为 `ARM:LE:32:Cortex`）。

---

## 固件内置的串口控制台

【smali/反汇编】Ghidra 完整反编译出一个**从未被 BLE 路径使用**的调试口：

```c
void FUN_0000a3dc(char *s) {
    if (*s == '=') { FUN_000080dc(s[1]); }   // 设置模式
    else if (*s == '?') { FUN_00009f50(..., 1); }
}
```

模式表（固件自带字符串）：

| 值 | 含义 |
|---|---|
| 1 | offline mode |
| `0x52` | online mode |
| `0x53` | online wait connect mode |
| **`0x54`** | **online Active mode**（+4000 ms 延时） |
| `0x55` | online Passage mode |
| `0x56` | online interactive mode |
| `0x57` | OTA ONLINE MODE |

**这是独立于 App 的第三方证据**，两条关键结论互相印证：

1. `0x54` = **online Active mode** —— 正是我修复后用来开遥测的字节
2. `0x52` = online mode —— 正是连接握手时发的那个

预设解析器 `FUN_00009d2c` 只在模式 ∈ {`0x52`, `0x55`, `0x56`} 时动作。

---

## 供电

| 项 | 值 |
|---|---|
| caps 报 `dev.power` | 14.5 – 15.8 V【实测】 |
| 电池 | 4 节串联 → 单节 3.6 – 3.95 V |
| 升压后电机供电 | 15.7 – 16.3 V |

换算见 [03-battery.md](03-battery.md)。

---

## 主机侧适配器

【实测 2026-10-20】本机当前只有 **一个** HCI 控制器：

| 设备 | 详情 |
|---|---|
| `hci0` | `33fa:0012` **UGREEN BT6.0 Adapter**（USB 3-8 口），`UP RUNNING` |
| 芯片 | `Barrot Technology Limited (2279)`，`HCI Version (0xe) Revision 0x204` |
| 驱动 | Linux `btusb` 0.8，`/sys/module/btusb/version` |
| BD Address | `04:7F:0E:XX:XX:XX`，ACL MTU `679:9`，SCO MTU `255:4` |
| nRF52840 嗅探器 | `/dev/ttyACM0`，1000000 baud（被动抓包工具，不是 HCI 控制器） |

实测命令与原始输出：

```
$ lsusb | grep -i -E "33fa|blue"
Bus 003 Device 006: ID 33fa:0012  UGREEN BT6.0 Adapter
Bus 003 Device 009: ID 1915:522a Nordic Semiconductor ASA nRF Sniffer for Bluetooth LE

$ hciconfig
hci0:	Type: Primary  Bus: USB
	BD Address: 04:7F:0E:XX:XX:XX  ACL MTU: 679:9  SCO MTU: 255:4
	UP RUNNING
	RX bytes:88701 acl:1307 sco:0 events:947 errors:0
	TX bytes:12219 acl:77 sco:0 commands:459 errors:0

$ hciconfig -a | grep -E "Manufacturer|HCI Version"
	HCI Version:  (0xe)  Revision: 0x204
	Manufacturer: Barrot Technology Limited (2279)
```

### 两个已作废的旧结论

1. **`33fa:0001`（Barrot BT 5.3）已不在 USB 枚举中。** 2026-10-20 实测，
   整个 `idVendor=33fa` 的设备只剩一个：

   ```
   $ for d in /sys/bus/usb/devices/*/; do
       [ "$(cat $d/idVendor 2>/dev/null)" = "33fa" ] && \
         echo "$d $(cat $d/idVendor):$(cat $d/idProduct) $(cat $d/product)"
     done
   /sys/bus/usb/devices/3-8/  33fa:0012  UGREEN BT6.0 Adapter
   ```

   （本内核的 USB 设备按端口路径命名，`/sys/bus/usb/devices/33fa:0012` 这个
   路径**不存在**，要按 `idVendor` 扫。）

   该旧适配器的 BlueZ 缓存目录 `/var/lib/bluetooth/60:6E:41:XX:XX:XX/` 仍在
   （含设备 `E7:97:7D:XX:XX:XX` 的 cache），但设备本身已不在总线上。
2. **"`hci1` 固件无响应"不再适用。** 旧记录里 `hci1` = `33fa:0012`
   UGREEN BT6.0 固件无响应、实际用 `hci0`；现在只剩一个适配器，它就是
   `hci0`，`UP RUNNING`，真机连接与遥测全部正常。

> **踩坑记录**：`lsusb | grep -i blue` 只打印 nRF 嗅探器 —— "blue" 不在
> UGREEN 适配器的厂商/产品字符串里。查适配器要按 USB id（`33fa`）过滤或直接
> 看完整 `lsusb` 输出，否则会得出"本机没有蓝牙适配器"的错误结论。

### Linux 侧的链路参数：内核默认值，不是 btusb 硬编码

2026-10-20 在本机 btmon 抓包复核（`btmon -i hci0 -w /tmp/btm.cap` + `btmon -r`），
并用自写的 btsnoop 解析器从原始字节 `/tmp/btm-new.cap`（MONITOR 格式，16022 条记录，
0 字节残留）复核。**原记录的三条归因都不成立**（结论与 [04-link.md](04-link.md) 一致）：
```
      LE Enhanced Connection Complete (0x0a)
        Connection interval: 50.00 msec (0x0028)
        Connection latency: 0 (0x0000)
        Supervision timeout: 420 msec (0x002a)
      LE Remote Connection Parameter Request (0x06)
        Min/Max connection interval: 7.50 msec (0x0006)
        Connection latency: 0 (0x0000)
        Supervision timeout: 4000 msec (0x0190)
< HCI Command: LE Remote Connectio.. (0x08|0x0021) plen 3
        Reason: Unknown Connection Identifier (0x02)
> HCI Event: Command Complete (0x0e) plen 6
        Status: Invalid HCI Command Parameters (0x12)
```

| 原记录 | 实测 | 证据 |
|---|---|---|
| `btusb` 硬编码 50 ms / 420 ms | **内核默认值** | 内核 `hci_core.c:2489-2492`（`hci_alloc_dev_priv()` 里的 `le_conn_min_interval`/`le_conn_max_interval`/`le_conn_latency`/`le_supv_timeout`），再由 `hci_conn_params_add()` `hci_core.c:2290-2293` 写进每设备参数；`btusb` 里碰 `le_conn_*` 的符号 **0 个**（`readelf -s`；BTF 里那些名字是 `struct hci_dev` 的成员名，不是驱动在写它）。**活体 debugfs 实测**：`conn_min_interval` 24、`conn_max_interval` 40、`conn_latency` 0、`supervision_timeout` 42 —— 逐项吻合 |
| 设备请求被判非法（0x12） | **时序竞态：句柄还没进 `BT_CONNECTED`** | 内核 `hci_event.c:6629` **先查句柄**（句柄查不到、或状态不是 `BT_CONNECTED` 就回 `0x02`），参数检查在 `:6634`/`:6638` **之后**。命令的 **Reason 字段 `0x02`** 与 Command Complete 的 **status 字段 `0x12`** 是**两个不同包的字段**。状态链：`conn->state = BT_CONFIG` 在 `:5758`，`= BT_CONNECTED` 在 `:6527`（`LE Read Remote Features Complete` 回调，且以 `if (conn->state == BT_CONFIG)` 为前提），而 `LE Read Remote Features` 命令在 `:5789` 就发出去了 —— 翻转必须等控制器回话 |
| 设备的 7.5 ms / 4000 ms 越界 | **在内核判据下合法** | 本机内核头 `the Linux kernel's include/net/bluetooth/hci_core.h:2254`（`static inline`，不在 .c 文件里）：`max > 3200` 是**连接间隔**上限（1.25 ms 单位 → 4000 ms），`to_multiplier > 3200` 是**监督超时**上限（10 ms 单位 → **32000 ms**）。按源码逐条转写后实测 `hci_check_conn_params(6, 6, 0, 400)` 返回 **0** |
| 420 ms 撑了 40.4 秒说明固件没执行寄存器 | **寿命和静默是两个数** | 原始 btsnoop 抓包：寿命 **40.379249 s** / 40.365065 s，**宿主侧**静默（最后一个主机→控制器包到死亡）**0.315170 s** / 0.316850 s。**「固件未执行该寄存器」这份抓包不支持，不写。** |
| `[LE]` 四键无效（内核层） | **被读取且生效，且已实测消除断链** | BlueZ 5.83 `src/main.c:126-129`（键名）与 `:656-675`（取值与范围）；内核 `mgmt_config.c:293-302` 写进 `hdev->le_supv_timeout` 等字段。**活体实测** `le_supv_timeout` **42 → 2000**（可写可读，已复原）。**2026-10-21 晚用 Vero 设备本身实测**：`[LE]` 六/六/零/四百（`/etc/bluetooth/main.conf` 行 251-254）生效，内核 debugfs 读数 `conn_min_interval` 6 / `conn_max_interval` 6 / `conn_latency` 0 / `supervision_timeout` 400，btmon 实测协商 **7.50 ms / 4000 ms**，连接龄 **142.550 s** 未断，且**没有** `Connection Timeout (0x08)`。既不写「无效」也不写「已解决」的旧措辞**已过时** |
| `[LE]` 四键改完就一定生效？ | **只对之后新建的连接生效** | `conn->le_conn_min_interval` / `le_conn_max_interval` 在 `hci_event.c:5628-5629` 从 `hdev->` 拷贝，**在连接建立的那一刻**。所以改 `main.conf` 并重启 bluetoothd 后，**存量链路不变**。`hci_conn_params_add()`（`hci_conn.c:1449`）走的是自动连接的每设备参数表，也不是活动链路 |

### 修复后的建链协商：对端就是 Vero 设备本身（2026-10-21 晚）

前面那张表记录的是**修复前**的状态（主机给 50.00 ms / 420 ms，设备发参数请求被拒）。
设备上线后重测，**协商结果变了**，而且这次**建链对象就是 Vero 设备本身**：

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

对照上一段的修复前抓包（50.00 ms / 420 ms + `LE Remote Connection Parameter Request`
被以 `0x02` 拒绝），**修复后那份抓包里该参数请求事件计数 0 次** ——
`LE Features` 里的 `Connection Parameter Request Procedure` 只是 feature bit，
不是参数请求事件本身。主机直接用设备要的参数建链，设备因此不必再发参数请求。

**三次建链、三次同样的协商结果，零次超时：**

| # | 建链 | 断开 | 连接龄 | 断开原因 |
|---|---|---|---|---|
| A | 11.998477 s | 16.880372 s | 4.882 s | 本地主机主动断开 |
| B | 60.456589 s | 171.882121 s | 111.426 s | 本地主机主动断开 |
| C | 944.258849 s | 抓包结束仍在 | **≥ 142.550 s** | — |

`Disconnect Complete` 只有 2 次，两次 Reason 都是
`Connection Terminated By Local Host (0x16)`；
`Status: Connection Timeout (0x08)` 计数 **0** 次。

第 C 条链路活到抓包最后一个事件（连接龄 **142.550 s**）：主机 TX 12723 包、
设备 RX 4678 包，**设备侧通知间隔中位数 0.030 s、最大 1.333 s，没有一次超过 5 s**；
主机 TX 有一次 25.921 s 的停顿，期间设备仍在按 30 ms 节拍发包。

**这条结论的边界**：只覆盖**这一台主机、这一台设备、这一组参数**。
别的 BlueZ 主机没有实测过，不作断言。

**键名写错过。** `[LE]` 段的键名是 `MinConnectionInterval` / `MaxConnectionInterval` /
`ConnectionLatency` / `ConnectionSupervisionTimeout`（`main.c:126-129`），**不是**
`MinInterval` / `MaxInterval` / `Timeout` —— 后者是 **per-device info 文件**
（`adapter.c:4216-4225` 的 `ConnectionParameters` 段）里的键，管的是另一套东西。
**「四键无效」的结论是错的：键名本身就写错了。**

**三种静默的数不能混。** 上面表里的 0.315170 s 是**宿主侧**静默（最后一个主机→控制器
包到死亡）；而**设备侧**静默（设备最后一次发射到死亡）是 **3.999 s / 2.798 s /
40.374 s** —— 第三个 0x08 死亡的链路内**一个 ACL 包都没有**（那条链路整条是闲的，
40 秒里主机没发过一帧控制、设备没回过一个包）。三者模式不一致，**「设备自己按量程
掉线」解释不了全部三次**，**「设备侧静默」也没有单一值可写**。

**恒定的量是连接龄，不是末包时距。** 三次 `0x08` 死链的**连接龄**是
**40.380165 s / 40.366051 s / 40.374077 s**（**spread 14 ms**），而「死亡时刻 − 设备
末个 ACL」是 3.9990 s / 2.7977 s / 40.374 s（**spread 1.20 s，非常量**）。监督超时的
定义就是「从最后一个包起算固定时长」，若真在生效这个差值必须恒定 —— 它不恒定，
所以 **40.4 s 是连接龄函数，不是末包时距函数**；触发点在连接建立后的固定时刻发生，
与丢包无关。

**这份抓包里没有空口 PDU，所以「设备停止发射」不可观测。** mon_op 直方图只有
`ACL_TX=4658` / `ACL_RX=74`（全是 L2CAP/ATT 数据），没有任何嗅探到的空口 PDU。
设备那 5 秒一次的「心跳」是它发的 **ACL 响应**，不是空口包 —— 所以「设备在死前
N 秒停止发射」在这份数据里**测不出来**，要测它得用 nRF52840 嗅探器抓空口。

（以上静默数都按**死亡瞬间 = `LE Connection Update Complete` 且 status 为 0x08**
那一帧算；按后面的 `Disconnect Complete` 帧算，末位会差 0.9 ms。寿命同理。）

**这是主机侧问题，Windows 上不存在。**

---

## 服务与特征 UUID

【源码】`f.java:10-25`：

| | UUID | 用途 |
|---|---|---|
| 服务 | `6e400001-b5a3-f393-e0a9-e50e24dcca9e` | Nordic UART |
| TX（写） | `6e400002-…` | `r1.i` |
| RX（notify） | `6e400003-…` | `r1.j` —— caps 与遥测共用 |

`discovery.py` 用 `6e400001` 与 `6f430001` 两个厂商服务做识别。

> **刻意不用 HID `0x1812`** —— 测试发现 AirPods、手柄、LYWSD02 温湿度计
> 都广播 HID，会造成误匹配。

---

## 另一型号（B 代，本设备不适用）

| 型号 | SN 前缀 | 规格 |
|---|---|---|
| Vero | `ALVX` `UMVX` | variable-speed device, 22 cm, 8 cm travel, 300 strokes/min |
| Mira | `ALMI` `UMMI` | vibrating device, 25 cm, 6 cm travel, **6 contacts** |

Mira 有 6 个触点，与 Vero 的 5-slot 位置传感器不同 —— 若换设备，遥测解码需重做。

---

## 工具环境

| 工具 | 位置 |
|---|---|
| jadx | `analysis/decompile/jadx-123/` |
| Ghidra | headless，`ARM:LE:32:Cortex` |
| btmon | BlueZ 自带 |
| nRF52840 嗅探器 | `/dev/ttyACM0`（1000000 baud） |
| Python 环境 | `vera-station/analysis/venv/`（bleak 3.0.2） |

> **nRF52840 嗅探器的官方工具 `/opt/nrfsniffer/sniffer_v4.zip` 是 9 字节占位
> 空文件**，不可用。

---

## 一次只能有一个客户端

【协议限制】BLE central 是独占的。两个程序同时连会互相踢 —— 这解释了为什么
测试脚本需要严格避让真机连接。
