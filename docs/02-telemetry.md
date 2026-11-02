# 遥测通道

## 这是位置，不是压力

设备把行程分成 **5 段**，按下时从最深的一段向外扫描，第一个被触发的段就是
当前位置。段内的原始读数表示该段被压得多实，**不是力的大小**。

用户最初也判断成「压力值」，是错的。修正过程记录在
[06-reverse-method.md](06-reverse-method.md#被证伪的假设)。

---

## 值怎么读

【smali】`v1.smali` 的 `e(String)`：

```java
String substring = str.substring(i, Math.min(i + 4, length));  // 4 hex = 2 字节
byte[] bArr = new byte[2];
bArr[i12] = (byte) Integer.parseInt(substring2, 16);
arrayList.add(ByteBuffer.wrap(bArr)
    .order(ByteOrder.LITTLE_ENDIAN)
    .getShort() & 4095);                       // & 0x0FFF
```

**每 4 个 hex 字符一组，16 位小端，再掩码到 12 位。量程 0..4095。**

### 逐字节读会错多少

载荷里的固定常量 `0x0A` / `0x0D` 会落到高位字节上：

| 读法 | 第1个值 | 第2个值 | 第3个值 |
|---|---|---|---|
| 单字节（错） | 10 | 11 | 12 |
| 16 位 LE（对） | 2570 | 3339 | 12 |
| 差 | **+2560** | **+3328** | 0 |

我曾长期用单字节读法，因为**槽位总数巧合也是 15**，代码看起来完全合理。

---

## 载荷布局

【源码】`n1.java:186-198` 用 stride 5 去交织：

```java
String substring2 = str5.substring(18, str5.length() - 4);  // byte 9 起，到 CRC 前
ArrayList e4 = v1.e(substring2);
int size4 = e4.size() / 5;
...
arrayList5.set(i31, e4.get((i29 * i30) + i31) + arrayList5.get(i31));
```

**30 字节载荷 = 15 个值 = 5 个 slot × 3 次子采样。**

载荷切片范围是 `byte 9` 到帧尾前 2 字节（CRC），**不依赖固定帧长**。

---

## 位置量化

【smali】`v1.smali` 的 `d(ArrayList)`，四个常量逐字核对：

```smali
const/16 v2, 0x320           // 800  阈值（Vero）
const/16 v2, 0xc8            // 200  阈值（非 Vero）
mul-int/lit16 v2, v0, 0x7d0  // 2000 每 slot 权重
const/16 v0, 0x2710          // 10000 上限
```

```java
threshold = isVero ? 800 : 200;
for (i = size-1; i >= 0; i--)          // 从最深一段向外扫
    if (values[i] > threshold) { found = i; break; }
return clamp(found * 2000, 0, 10000);
```

阈值选择依赖 SN 归一化后的设备类型（`of.i.a(r1.o, "Vx")`）；本设备
`UMVx` → `VX` → Vero → **800**。

### 位置表

| 输入 | 位置 | 触发段 |
|---|---|---|
| `[50,40,30,20,10]` | 0% | 未触发 |
| `[900,40,30,20,10]` | 0% | 第 1 段 |
| `[900,900,900,20,10]` | 40% | 第 3 段 |
| `[900,900,900,900,10]` | 60% | 第 4 段 |
| `[9999]×5` | **80%** | 第 5 段 |

**5 段封顶在 80%，永远到不了 100%。** 5 × 2000 = 10000 是另一个路径的上限。

比较是严格的 `>`：值恰好等于 800 不算命中。

---

## 自动标定

【smali】`n1.smali:2480-2487`：

```smali
div-int/2addr v6, v7
const/16 v7, 0xc8      # 200
add-int/2addr v6, v7
```

连接后设备自动收 7 帧 → 逐 slot 求均值 → **+200 死区**，写入基线。

**全自动，App 里没有校准按钮。** 所以静息值不是 0 而是约 200。

> 刚连上、标定完成前读数会接近 0 —— 一个「按压峰值 46」的读数就是在这个
> 阶段测到的。
> `arm_grace_seconds = 2.7` 正是为了让标定有时间完成。

---

## 采样率

**33 帧/秒，实测。**

结论：固件以 **100 Hz** 采样，每 **3** 个采样打成 1 帧 → 100/3 = 33.33。
　【反推】理由是 33.33 × 3 = 100 为整数，且 5-slot × 3-子采样结构要求每帧
装 3 个采样点。

主机侧无法提高：

| 手段 | 可用性 |
|---|---|
| 命令帧速率字段 | 不存在 —— `A5 5A 08 B0 3D 36` 里 `3D` 后就是命令码本身 |
| `requestMtu` | 全 APK 零命中，MTU 停在默认 23 |
| `requestConnectionPriority` | 零命中 |
| 提高轮询频率 | 无关 —— 设备主动推 |

要更高只能改固件的打包比（3 → 2 得 50 Hz，3 → 1 得 100 Hz）。

### 一条被推翻的解释

我曾断言「33 Hz = BLE 30 ms 连接间隔的上限」。**该解释错误**：

```
50 ms 间隔 → 20 事件/秒 → 每事件 1 帧 = 最多 20 帧/秒
实测 33 帧/秒 → 超出 65%
```

btmon 实测本机连接间隔是 **50 ms**【实测】，若帧被连接间隔切出则不可能达到 33 Hz。
30 ms 的吻合是巧合。

---

## 诊断字段

面板和 API 暴露三个数字，用于区分「设备没说话」与「格式认错了」：

```json
"notify": {"total": 834, "telemetry": 834, "other": 0,
           "other_signatures": []}
```

| 显示 | 含义 |
|---|---|
| `total = 0` | 设备完全没发通知 → 握手命令有问题 |
| `other > 0` | 在说话但格式不认 → 看 `other_signatures` 的字节指纹 |
| `telemetry > 0` | 正常 |

`other_signatures` 形如 `41B:A55A29A0 ×834`（长度 + 前 4 字节）。这个字段曾直接
定位过一次问题：设备在发 `A55A 7D` 帧被我的解码器当「其他」丢弃。

---

## 相关实现

| 文件 | 职责 |
|---|---|
| `vero/pressure.py` | `decode_frame()` —— 帧校验 + 16 位解码 + slot 分组 |
| `vero/monitor.py` | `locate()` 位置量化、滚动窗口、到达间隔测量 |
| `vero/simulator.py` | 按 5-slot 结构造帧 |

`PressureAccumulator` 是旧单字节模型的遗留，**仅供离线自测**，控制路径不走它。
