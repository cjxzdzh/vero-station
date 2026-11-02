# App 内部算法

复刻这些行为时的依据。多数来自 decompiled 代码而非猜测。

---

## 运动轨迹用正弦

【源码】`r1.java:2996` 附近的 `n0()`，配合 `k1.toString()` 的决定性证据：

```
Trajectory(phaseRadiansAtProduced=…, speed=…, minUiPosition=…, maxUiPosition=…)
```

**字段名是 `phaseRadians`（相位弧度）** —— App 用的是正弦相位驱动，不是三角波。

### 正弦 vs 三角波

| | 端点速度 `d/dphase` | 加速度 |
|---|---|---|
| 正弦 | 0.0006 | 有限 |
| 三角波 | 1.0000 | **无穷**（瞬时反向） |
| 差 | **1600 倍** | — |

端点速度差 1600 倍：对有惯性的机械件，三角波的瞬时反向就是抽搐的来源。

### 曾试过缓动曲线，已删除

raised-cosine（ease）实测中段速度是直线的 **3.14 倍** —— 对惯性机构是灾难。
直接移除，不是调参问题。

### 首 8 步对比

```
sine   0.01 0.04 0.07 0.10 0.13 0.16 0.19 0.22   （渐入）
linear 0.60 0.60 0.60 0.60 0.60 0.60 0.60 0.60   （恒速）
```

### 相位必须掉头，不能回绕

【已修】这是抽搐的**硬 bug**：

```python
phase += dphase
if phase >= 1.0:
    phase -= 2.0        # ← 75% → 15%，一个 tick 内砸到底
```

相位只单向推进并回绕 → **返程从未真正发生过**。实测每周期：慢速伸到 80%
→ 一 tick 跳回 20% → 再慢速伸出。

修复后相位是真正的三角波 `-1→0→1→0→-1`：

```python
if direction > 0:
    if phase >= 1.0: phase = 1.0; direction = -1.0
else:
    if phase <= -1.0: phase = -1.0; direction = 1.0
phase += direction * (4.0 / steps)     # 完整往返是 4.0 相位
```

轨迹实测对称：

```
t=0.00 20.00 → t=0.97 79.76   上行
t=1.13 79.06 → t=2.09 20.00   下行（真的走了）
```

最坏单帧跳变从 **59.99%** 降到 **0.63%**（降 95 倍）。

---

## tick 必须自适应

固定 40 ms 在两个方向都错：

- 0.1 秒档只有 5 步 —— 台阶不是运动
- 15 秒档有 750 步 —— 纯浪费

`tick_seconds(traverse)` 两个方向钳制：

| traverse | tick | 步/周期 | 峰值步长 |
|---|---|---|---|
| 0.1 s | 20.0 ms | 10 | 18.85% |
| 0.2 s | 40.0 ms | 10 | — |
| 0.3 s | 40.0 ms | 15 | 12.57% |
| 0.5 s | 40.0 ms | 25 | 7.54% |
| 1.0 s | 40.0 ms | 50 | 3.77% |
| 1.5 s | 40.0 ms | 75 | 2.51% |

`MIN_STEPS_PER_CYCLE = 10`、`MAX_STEPS_PER_CYCLE = 400`、`MIN_TICK_SECONDS = 0.020`。

**0.1 秒是物理下限**：20 ms 发送泵，0.2 秒往返 = 10 帧。再快没有帧可发，
所以 API 拒绝更小的值（422）。

---

## 绝对时间调度

```python
deadline += tick          # 而不是 time.sleep(tick)
```

用相对 sleep 时，每个 tick 自身的耗时会累积，实测 0.1 秒 traverse 被拉长到
0.3 秒。

---

## 反馈最终回退为 pass-through

| 版本 | 问题 |
|---|---|
| `actual + drift` | 展开就是 `commanded`（恒等式，反馈无效但不致害） |
| 改成比例控制 | 因「信号天生滞后」产生 **60% 跳变** |

平滑只能来自命令路径本身。反馈仅用于显示。

---

## 参数实时生效的两个坑

**坑 1：`p = self.state.params` 只在循环开始取一次**

运行中 `update()` 换掉 `state.params`，循环仍在用**旧对象** —— 滑杆看着能改，
实际不生效。修法：每轮循环开头重读。

**坑 2：前端用轮询差分算帧率**

```js
// 错：测的是浏览器轮询率，不是设备帧率
const hz = (s.frames - prev.f) / (now - prev.t);
```

而且 `api()` 默认 `method="POST"`，`api("/api/auto")` 发的是 POST → **405**，
被空 `catch` 吞掉 —— 表现为「滑块动了但没反应」。修法：`api("/api/auto", "GET")`
+ 空 catch 改成显示警告。

---

## 空 catch 是反复出现的元凶

```js
} catch(e){}          // ← 吞掉一切，表现与「功能坏了」完全相同
```

这条至少掩盖了三个真 bug：字段名不匹配、`drawSeries` 里的 `ReferenceError`、
POST/GET 不匹配。现在所有 catch 都会显示可见信息。

---

## 一个 JavaScript 陷阱

```js
for(const [t,a,b] of series) if(a>maxv) maxv=a; if(b>maxv) maxv=b;
```

两个 `if` 写在同一行无花括号，**只有第一个是循环体**。第二个在循环外，
`b` 从未绑定 → `ReferenceError`。被空 catch 吞掉后，表现为「服务端有数据但
面板空白」。

---

## 相关实现

| 文件 | 职责 |
|---|---|
| `vero/automotion.py` | 正弦相位、tick 自适应、绝对调度 |
| `vero/web/index.html` | 实时参数推送（120 ms 防抖） |
| `motion_regression.py` | 有惯性的运动质量验证 |
| `fast_traverse_regression.py` | 0.1 s 单程仍是插值 |
