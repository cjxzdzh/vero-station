# 协议

设备：Amorlink **Vero**（厂商：北京缀梦科技，包名 `cn.zhuimeng.amorlink`）
芯片：**Telink LE501x**　【实测】

SN 形如 `UMVXEXAMPLE01`，MAC `E7:97:7D:XX:XX:XX`，BLE 固件 `1.0.11`
　【实测】

---

## 帧结构

```
byte 0-1   A5 5A        固定头
byte 2     totalLen     整帧长度
byte 3     subId        子命令（A0 = 数据帧）
byte 4     dataId       数据类型
byte 5..   payload
last 2     CRC（文本 CRC，LE 存放）
```

**byte 2 是整帧长度。**【实测】遥测帧 41 字节，byte 2 = 0x29 = 41；
caps JSON 回帧 181/182 字节，byte 2 = 0xB5/0xB6 = 181/182
（`analysis/evidence/hw_retest.json` log[0] reply[0]）。

**遥测帧的完整布局**【实测，真机抓包
`analysis/scripts/find_pressure_byte.py`】：

```
byte 0-1   A5 5A
byte 2     0x29        整帧长度 = 41
byte 3     0xA0        子命令
byte 4     0xB0        dataId
byte 5     vibration   u8
byte 6     0xA0
byte 7     0x03        组数
byte 8     0x01
byte 9-38  15 个 u16le（5 slot × 3 时间子采样），每值 & 0x0FFF
byte 39-40 CRC16
```

> ⚠️ **这条曾经写反过，而且害出了真缺陷。** 早前这里写的是「byte 2 是 cmdId，
> 不是长度」——【smali】`r1.smali:12344-12420` 用 `%02X` 格式化它，App 侧
> `n1.java:142` 只校验 `startsWith("A55A")` 和 `byte 3 == "A0"`，**从不校验
> byte 2**。
>
> **App 不校验 ≠ 设备不写长度。** 设备写的就是自己的长度。上一轮照着错误结论
> 做了「重组器按标称 37 字节装配」，于是每帧都在 37 处被截断、CRC 永远不过
> （CRC 覆盖全部 41 字节）、尾巴 4 字节被丢。线上计数器当时明明白白写着
> `crc_fail 4880` / `reassembly_dropped 4906` —— **每一帧、每一条通知**。
>
> 教训：**App 不校验某个字段，只能说明 App 不用它，不能说明设备不写它。**
> 判据要来自真机抓包（`analysis/scripts/find_pressure_byte.py`），不是从
> 「App 没读它」反推。

CRC 为 **CRC-16/CCITT-FALSE**，算在**解码后的字节**上（`frame[0:len-2]`），
结果**小端**附在尾部。【源码】`protocol.crc16_ccitt_false` / `protocol.build_text_frame`

> **固件是权威。** 五个扫描器都是同一个信封：
>
> ```text
> crc = FUN_0000ad44(0xFFFF, frame, payload_len + 4);   // over frame[0:len-2]
> frame[len-2] = crc & 0xff; frame[len-1] = crc >> 8;  // LITTLE-ENDIAN
> ```
>
> （【固件】`analysis/firmware/fw-strings/A55A_SURVEY.md` —— 该文件的
> 「EVERY A5 5A SCANNER」与「CRC 复算」两节，证据等级最高。
> 那份文件的分节是纯文本编号，本仓库的引用检查只认 markdown 标题，
> 所以这里引文件而不引节号 —— 引一个查不到的东西比不引更糟。）
>
> **对设备真正回过话的帧直接复算，全部吻合字节规则**：
>
> | 帧 | 线上尾部 | 复算 | |
> |---|---|---|---|
> | caps `A5 5A 07 00 01` | `1E 90` | `0x901E` | ✅ |
> | mode `A5 5A 08 B0 3D 03` | `33 C7` | `0xC733` | ✅ |
> | intensity `A5 5A 0B D0 0A 00 00 14` | `EC 23` | `0x23EC` | ✅ |
> | mode `A5 5A 08 B0 3D 52` | `E7 8D` | `0x8DE7` | ✅ |
>
> **一句话**：尾部是**小端**存放的，**被哈希的内容是字节** —— 这是两件事，别混。
>
> ⚠️ `analysis/evidence/hw_retest.json` 里那些帧的尾部看起来像「文本 CRC」，
> 但它们是**分析工具自己构造**的，尾部只反映了那个工具当时的实现；
> 设备对它**没有回过话**（`status: ok` 只说明写成功）。
> 唯一被设备回应的帧（caps `1E 90`）用的是**字节规则**算出的固定 CRC。

---

## 命令表

以下全部由 `protocol.py` 构造，数值已与实际输出核对。

| 用途 | 线上帧 | 说明 |
|---|---|---|
| 归零 0% | `A5 5A 0D A0 B0 00 A0 01 0F 00 00 D3 56` | drive=0 |
| 定位 20% | `A5 5A 0D A0 B0 00 A0 01 0F D0 07 13 33` | drive=2000 |
| 定位 50% | `A5 5A 0D A0 B0 00 A0 01 0F 88 13 B0 E6` | drive=5000 |
| 定位 80% | `A5 5A 0D A0 B0 00 A0 01 0F 40 1F C1 B8` | drive=8000 |
| 定位 100% | `A5 5A 0D A0 B0 00 A0 01 0F 10 27 25 01` | drive=10000 |
| **遥测启动** | `A5 5A 08 B0 3D **54** 21 ED` | 见下节 |
| 运动模式（连接时） | `A5 5A 08 B0 3D **52** E7 8D` | `POST_CONNECT_MODE = 52` |
| 设备信息查询 | `A5 5A 07 00 01 1E 90` | 回帧走 notify，含 182 字节 JSON |

### 定位帧结构【源码】

```
A5 5A 0D A0 | B0 | vib | A0 | 01 | 0F | drive(u16 LE) | CRC16 LE（算在字节上）
```

**精确偏移**（以 `app_a0_control(5000)` 为例）：

| 偏移 | 内容 | 值 |
|---|---|---|
| 0-1 | 头 | `A5 5A` |
| 2 | 长度 | `0D`（13） |
| 3 | subId | `A0` |
| 4 | dataId | `B0` |
| 5 | vibration | |
| 6 | 常量 | `A0` |
| 7 | 常量 | `01` |
| 8 | 常量 | `0F` |
| **9-10** | **drive，小端** | `88 13` = 5000 |
| 11-12 | CRC16 LE（算在字节上） | |

| 位置 | drive |
|---|---|
| 0% | 0 |
| 20% | 2000 |
| 50% | 5000 |
| 80% | 8000 |
| 100% | 10000 (`0x2710`) |

行程上限 `STROKE_MAX = 10000`。

> 偏移 7 是常量 `01`（smali `const/4 v5, 0x1`），**不是第二个振动字段**。
> 早期抄错过这一处，但因为帧长仍是 13 字节，长度与 CRC 校验都通过，
> 错误一直没被发现。

---

## 遥测命令

### 这是最容易搞错的一处

App 的 A 代模式命令构造在 `r1.java:2032`：

```java
G0(this, "B0", 8, ze.k.Q("3D", String.valueOf(i)), ...)
```

`K0()` 内部（`r1.smali:26101`）：

```smali
const-string v2, "3D"
invoke-static {p1}, Ljava/lang/String;->valueOf(I)Ljava/lang/String;
```

`String.valueOf(int)` 是**十进制**。而 `r0.smali:322` 传入的是整数 `0x36`：

```smali
const/16 v3, 0x36
```

于是：

```
String.valueOf(0x36)  →  "54"  →  线上字节 0x54
```

**线上是 `0x54`，不是 `0x36`。**

> 两个分析组对此给过相反答案：一方把 smali 常量 `0x36` 当成线上字节，另一方
> 正确。判定方法是查 CRC —— 两者各自的 CRC 都自洽，因为它们说的是两个不同的
> 命令。最终以 smali 的格式化指令为准。

对应帧：`A5 5A 08 B0 3D 54 39 6C`

### 连接时序【smali】`m.java:41-48`

```java
int p10 = r1Var.p(54);          // Vero 返回 2000
r1Var.T0(54, POST_CONNECT, ...);// = K0(54) + n0(p(54))
postDelayed(..., r1.F(0, p10) + 700);   // 2000 + 700 = 2700 ms
```

`p(54)` 对 Vero 返回 **2000**（其他设备 7000）。这是 `n0()` 的目标位置参数
（0..10000 域），**不是采样率**；但 `m.java` 用它算出 2.7 秒的等待。

本实现 `arm_grace_seconds = 2.7` 与之一致 —— 早期用 0.5 秒会导致误报「未激活」。

### 模式命令只有两处【源码】

`j.java:78` 和 `j.java:122`，都是 `K0(52)`。全 APK 没有第三处模式命令。

`n1.java:161` 的 `if (N().f6498d.size() == 7) T0(52, …)` 所在的分支，其进入条件是
`n1.java:154` 的 `if (f6353a0 && x())`。`f6353a0` 是**基线就绪标志**——
`r1.java:2337` 在采满样本后置 `f6353a0 = true` 并打日志
`"v13 sensor baseline ready"`。**它的作用是阻止在基线建立之前反复重发
command 52**，而不是「让该分支永久失效」。

因此 command 52 的补发条件是**基线就绪**，且每次都附带 `size()==7` 这个帧数
判断，**不是周期续流**。真机语义：0x52 只在基线阶段需要重发。

【订正】本节早期版本把 `f6353a0 = true` 误读为「令该分支永久失效」，据此推出
「command 52 全程只补发一次」。源码不支持该推断——该标志是前置条件，不是
禁用开关。**「全程只补发一次」这个结论不成立**；下面对实现语义的记录仍成立，
因为它来自真机观测而非该推断。

> 我曾实现成「每 7 帧重发一次」，在 20 Hz 下等于每 350 ms 重置设备流，遥测
> 直接消失。这个反直觉的语义写在 `monitor.batch_due()` 的注释里。
> **注意**:`size()==7` 是「每满 7 帧才检查一次是否需要补发」，不是
> 「每 7 帧无条件重发」；是否重发还取决于 `f6353a0 && x()` 是否成立。

---

## notify 通道

**设备有两条 notify 特征，两个都要订阅。**【实测，固件 GATT 布局
`analysis/FIRMWARE-REPORT.md` 第三节】：

```
service 6e400001-…   NUS 兼容
  6e400002  [write]    主 TX（控制帧写这里）
  6e400003  [notify]
service 6f430001-…   厂商私有
  6f430002  [write]
  6f430003  [notify]
```

App 两个都绑：【源码】`BleManagerHandler$4.java:570`（`r1Var2.j` = 6e400003）
与 `:601`（`r1Var5.f6368l` = 6f430003）。本项目的抓包脚本也是两个都订
（`analysis/scripts/hw_retest.py:55`）。

> ⚠️ **本项目曾经只订了一条。** 设备发在另一条上的帧**根本没到过** —— 没被
> 看见、没被计数、没被解码。于是设备自己的按键帧（`0B02` 加 / `0B03` 减 /
> `0B04` 模式，dataId 在 byte 4-5）从未出现过，页面显示「0 个按键」。
>
> 教训：**「没收到」有两种完全不同的原因** —— 设备没发，和我们没在听。
> 只看「计数为 0」分不开它们；要看**订阅了哪几条特征**。

【实测】caps JSON（182 字节）与 0x29 遥测帧走**同一通道**。判据：caps 能收到
就证明 notify 订阅正常，此时遥测为 0 只能归因于协议层而非链路层。

caps JSON 结构（`n1.java:290-306` 解析）：

```json
{"dev":{"sn":"...","power":14.55,"ble_1_fw":"1.0.11",
        "mcu1_fw":"...","mcu2_fw":"..."},
 "ble":{"MTU":23,"RSSI":-49}}
```

**caps 只能通过 notify 推送，不能等 write-response。** 【实测】设备不用
write-response 回 caps，同步等待必然超时 —— 这是早期身份字段一直为空的根因。

---

## 按键帧（设备的实体按键）

**按键帧走 A1 分支，不是 A0。**【固件源码，
`analysis/firmware/fw-strings/frame_handlers.txt:1632-1650`】—— 固件里那个
日志字面量就叫 `sent Key Value` 的函数：

```c
void FUN_0000a288(void) {
  local_110 = 0xff;
  local_114 = 0x0b;              // payload 第一字节
  local_113 = FUN_00006bbc();    // 按键值
  FUN_00009ebc(0xa1, &local_114, 2, auStack_10c);
  ...
}
```

它调用的信封构造器【`analysis/firmware/fw-strings/control_path.txt:174-190`】：

```c
*param_4 = 0xa5;  param_4[1] = 0x5a;
param_4[2] = (char)(param_3 + 6);   // 2 + 6 = 8
param_4[3] = param_1;               // 0xA1
FUN_00000d88(param_4 + 4, param_2, param_3, param_4, param_4);  // 拷贝 2 字节 payload
uVar1 = FUN_0000ad44(DAT_00009ef4, param_4, param_3 + 4);        // CRC over frame[0..5]
param_4[param_3 + 4] = crc & 0xff;  param_4[param_3 + 5] = crc >> 8;   // LE
```

所以设备发出来的按键帧是：

```
byte 0-1   A5 5A
byte 2     0x08        整帧长度 = 8
byte 3     0xA1        子命令 ← 关键
byte 4     0x0B        dataId 高字节
byte 5     0x02/03/04 按键值（加 / 减 / 模式）
byte 6-7   CRC16 LE
```

**App 读到的就是 bytes 4-5。** `n1.java:308-336` 取的是十六进制串的
`substring(8,12)`，也就是 **byte 4 和 byte 5**，得到 `0B02` / `0B03` / `0B04`
—— 正好是它比较的三个 id。而 App 的按键分发**并不以 byte 3 == "A0" 为条件**：
`n1.java:142` 只校验 `startsWith("A55A")` 且长度 ≥ 12，按键分支与遥测分支
是**兄弟**关系，不是父子。

> ⚠️ **这条曾经写反过，而且害出了真缺陷。** 早前解码器要求 byte 3 == "A0"，
> 于是设备的每一帧按键都被丢掉，页面永远显示「0 个按键」。
>
> 当时的证据链是「App 的按键分发在 A0 分支里」—— 而那是从**反编译的 App
> 源码**推出来的。设备**固件**才是权威：固件自己用 `0xA1` 造这一帧。
>
> 当时线上计数器其实已经写着了：`8B:A55A08A1 × 7` —— **A1 就在面板上**，
> 8 字节，正好是按键帧的长度，而按键卡显示「0」。信号一直在，没人读。
>
> 教训（与 byte 2 那次同源，但更隐蔽）：**App 源码能证明「App 怎么读」，
> 不能证明「设备怎么发」。** 帧的构造方在固件里。判据要取自
> `analysis/firmware/fw-strings/`（固件反编译产物），不是从 App 的读取逻辑
> 反推。
>
> 另一个同源的坑：**错误的规格会进测试夹具。** `button_binding_regression.py`
> 里曾有一条断言写的是「A1 分支不是按键帧」—— 它把错误结论钉成了回归保护。
> 夹具钉错规格时，**改夹具**，不要为了让测试变绿而回退产品代码。

### 按键值是谁写进去的（固件侧）

按键帧的**载荷**（byte 5 那个键值）不是设备自己按 GPIO 产生的，而是**收到
一帧 F0 之后写进 RAM 的**。【固件反汇编，OTA 包里的
`BIN_FA004_SRSN_ALMiA_V1.0.2_2506026_Beta.bin`（注意：`fw-strings/assets/`
里那份 100435 字节的文件是**损坏**的，正确的 65536 字节那份在
`analysis/firmware/ota/assets/`）】

RAM 里有一个三字节的结构，地址 **0x1095**：

| 地址 | 含义 | 谁写 | 谁读 |
|---|---|---|---|
| `0x1095[0]` | **按键值** | `FUN_00006be0` | `FUN_00006bbc` |
| `0x1095[1]` | 另一个值 | `FUN_00006bc8` | `FUN_00006bec` |
| `0x1095[2]` | 另一个值 | `FUN_00006bd4` | `FUN_00006bec` |

三个都是 6 字节的读写器，形状一致：

```asm
FUN_00006be0:  ldr r1, [pc, #4] ; -> 0x1095
               strb r0, [r1]        ; 0x1095[0] = r0
               bx  lr
FUN_00006bbc:  ldr r0, [pc, #4] ; -> 0x1095
               ldrb r0, [r0]       ; return 0x1095[0]
               bx  lr
```

**写按键值的是收到 F0 帧的时候**【0x4d2e】：

```asm
0x04D2E: ldrb r0, [r0]        ; 帧的 byte 0
0x04D34: cmp  r0, #0xf0
0x04D36: bne  0x4d3e
0x04D38: ldrb r0, [r4, #1]     ; 帧的 byte 1
0x04D3A: bl   0x6be0          ; -> 0x1095[0] = byte 1
```

**读按键值并分发的是 `FUN_00006bec`**【0x6bec】—— 它比较的是 **1 / 2 / 3 / 4**
四个值，不是 2/3/4：

```asm
0x06C08: ldrb r1, [r5]        ; r5 = 0x1095
0x06C0A: cmp  r1, #1
0x06C0C: beq  0x6c3a
0x06C10: cmp  r1, #2
0x06C12: beq  0x6ca2
0x06C14: cmp  r1, #3
0x06C16: beq  0x6ccc
0x06C18: cmp  r1, #4
0x06C1A: bne  0x6c38           ; 其它值：什么都不做
```

> ⚠️ **这里有一处对不上，必须说清楚，不能含糊过去。**
>
> - **App 侧**（`n1.java:308-336`）把 `X.substring(8,12)` 也就是 **bytes 4-5**
>   和 `0B04` / `0B03` / `0B02` 比 —— 也就是 **低字节是 2/3/4**。
> - **固件侧**分发的是 **1 / 2 / 3 / 4** 四个值 —— **多了一个 1**。
>
> 也就是说：**键值 1 在固件里是一个独立的分支，而 App 只会把 2/3/4 当成
> 三个实体按键。** 固件的 1 分支做什么，要看 `0x6c3a` 那一段才能下结论；
> 本仓库**不猜**。
>
> 另一个仍然开放的事实：**F0 帧是谁发的。** 固件是从**收到的** F0 帧里取
> 按键值的，而 App 的反编译源码里**没有** F0 帧（`grep '"F0"'` 在
> `com/app/aike/` 下 0 命中）。可能的来源有三个，本仓库**没有证据区分**：
> (a) 另一个控制器（遥控器 / 第二个 BLE 设备）发来的；
> (b) 设备自己的某个内部模块构造后走环形缓冲回来的；
> (c) 一条本仓库还没找到的路径。
>
> **所以「设备上的实体按键」这个前提本身还没有被证实。** 本轮修好的是
> **解码器**（它此前把每一帧真按键都丢了）；键值的**语义**（哪个值对应哪个
> 实体按键）以及**按键帧的来源**，需要真机抓一次带 F0 的流量才能定论。
>
> 判据：真机按下实体按键时，`other_signatures` 里应出现
> `8B:A55A08A1`（本轮已实测到过 7 次，8 字节，与固件的构造完全吻合），
> 且 `/api/buttons` 的 `events` 里有对应条目。**这两件事同时成立**才叫
> 「按键能用」。

---

## MTU 与连接间隔

| 项 | 状态 |
|---|---|
| `requestMtu` | **全 APK 零命中**，App 从不协商 MTU |
| `requestConnectionPriority` | 零命中 |
| 实际 MTU | 23（ATT 默认），41 字节帧需分 3 段 |

`caps` 里的 `MTU` 字段是**设备上报的当前值**，不是主机请求的结果。

---

## B 代协议（本设备不适用）

APK 里存在原生轨迹帧（`5A A5 55 00` + 6 个 `(ts:u32, pos:u32)` 点，设备自行
插值执行）。【源码】`DeviceResolver.java:22` 的
`forcedBGenerationModelCodes = {"ALNX","UMNX"}` 不含 `UMVx`，且
`normalizeSn("UMVx...")` → `"VX"` → `DeviceKind.VERO`。

本设备 `modelCode` 取前 4 字符 = `UMVx`，**不在名单内** → `isBGeneration()` 为
false → App 对 Vero 也走 20 ms 逐帧下发。

连贯性只能靠「发什么序列」来实现，见 [05-app-behaviour.md](05-app-behaviour.md)。
