# 电量

## 设备只报电压

caps JSON 里的 `dev.power` 是**电池包总电压**【实测】：14.5–15.8 V，4 节串联。
设备不报百分比。

【实测】实测范围 14.5–15.8 V（充电时可达 15.82 V）。

---

## App 的换算不是阈值分段，是查表

【源码】`utils/h.java` 的 `case 0`：

```java
double doubleValue = ((Double) obj).doubleValue();
List<BatteryData> list = eVar.f6064a;        // 放电曲线
double d4 = Double.MAX_VALUE;
for (BatteryData b : list) {
    double abs = Math.abs(b.getV() - doubleValue);   // 最近邻
    if (abs < d4) { batteryData = b; d4 = abs; }
}
i10 = Math.max((int) Math.round(batteryData.getPercent() * 100.0d), 0);
return Integer.valueOf(BatteryVoltageNormalizer.normalizeMappedPercent(i10));
```

曲线来自 APK 的 **`assets/battery_data.json`**（不是硬编码）：

```python
# e.java 构造函数
this.f6064a = new sa.m().b(
    open(context.getAssets().open("battery_data.json"))...
```

**122 个点，单节 3.0000–4.1530 V。**【源码】文件已随包放在
`vero/data/battery_data.json`。

### 三步换算

【smali】`n1.java:306`：

```java
dev.power → BatteryVoltageNormalizer.legacyPackVoltageToCell(...) → 包电压 ÷ 4.0
```

【源码】`BatteryVoltageNormalizer`：

```java
private static final double LEGACY_CELL_COUNT = 4.0d;

public static final int normalizeMappedPercent(int i) {
    if (i > 95) i = 100;
    return e.h(i, 0, 100);
}
```

1. 包电压 **÷ 4** → 单节电压
2. 在 122 点曲线里找**最近邻**（不是插值）
3. `> 95 → 100`，钳制到 0..100

---

## 参考点

| 电量 | 单节电压 | 包电压 |
|---|---|---|
| **100%** | **≥ 4.0609 V** | **≥ 16.24 V** |
| 95% | 4.0438 V | 16.18 V |
| 60% | 3.7500 V | 15.00 V |
| 35% | 3.6375 V | 14.55 V |
| 17% | 3.5000 V | 14.00 V |
| **0%** | ≤ 3.2719 V | ≤ 13.09 V |

【实测】14.55 V → 35%。

### 注意顶部的平台期

曲线算出 96/97/98/99/100 的那 **5 行**，经过 `>95 → 100` 之后**全部显示 100%**。
所以从包电压 **16.24 V** 起就是满电，App 永远不会显示 96%–99%。

> 这条我一开始答错过：报了「100% ≥ 16.61 V」，那是**曲线最高点**而不是显示
> 门槛，差了 0.37 V。代码一直是对的，错的是引用的数字。

---

## 为什么用最近邻而非插值

曲线两端很陡，插值会造出测量不支持的精度。用最近邻则：

- 与厂商 App 显示的数字一致
- 可被 `battery_regression.py` 逐点验证（122 个点全部自洽）

---

## 1.2.3 的平滑层

1.1.5 直接换算每帧，显示会随噪声抖动。1.2.3 在外面包了一层滤波：

【smali】`DeviceBatteryEstimator.Config` 默认值（从合成构造器的位掩码读出）：

| 参数 | 值 |
|---|---|
| `windowDurationMs` | 10000 |
| `maxWindowSamples` | 512 |
| `minimumInitialSamples` | 3 |
| `initialStabilityDelayMs` | 1000 |
| `evaluationIntervalMs` | 2000 |
| `minimumCellVoltageExclusive` | 0.0 |
| `maximumCellVoltageExclusive` | 10.0 |
| `decreaseConfirmationEvaluations` | 2 |

行为：

- 10 秒窗口内取**中位数**，不是最新帧
- 拒绝 0.0–10.0 V/节 之外的样本
- 需 3 个样本 + 1 秒稳定期才出首个读数
- 最多每 2 秒重评一次
- **上涨永不发布**（防充电回弹导致电量跳回）
- **下跌需连续 2 次确认**

【实测】实测行为：

```
t=0.6s  samples=3   shown=None  settling
t=1.2s  samples=5   shown=35    initial reading
t=1.5s  samples=6   shown=35    not due yet
```

14.55 V 充到 15.2 V，显示**纹丝不动** —— 上涨被抑制。

---

## API 返回两个值

```json
{
  "percent": 60,          "← 此刻电压换算
  "percent_shown": 35,    "← 滤波后，App 显示值
  "samples": 6,
  "filter_reason": "not due yet",
  "pack_volts": 14.55, "cell_volts": 3.6375,
  "table_cell_volts": 3.6393, "table_percent": 35,
  "in_range": true, "cell_count": 4
}
```

`in_range` 说明电压是否落在实测曲线内 —— 超范围的读数是被钳制的，
不是测量值。

### 一个 Python 陷阱

`BatteryEstimator.evaluate()` 里写过：

```python
now - (self.first_sample_at or now)     # 错
```

时间戳恰好为 `0.0` 时，`or` 会替换成 `now`，差值恒为 0 → **滤波器永不发布**，
表现为「电量死活不出来」。生产环境 `time.monotonic()` 不会是 0 所以没暴露，
但这是真 bug。已改显式 `is not None`，并有专门测试。

---

## 相关实现

| 文件 | 职责 |
|---|---|
| `vero/battery.py` | 曲线查表 + `BatteryEstimator` 滤波 |
| `vero/data/battery_data.json` | 曲线原文件 |
| `battery_regression.py` | 122 点自洽、8 个实测电压、滤波行为 |
