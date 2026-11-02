# Vero 控制 API 速查

服务地址：`http://<lan-ip>:8800`　（`<lan-ip>` = 部署 Vero 服务那台机器的内网 IP）

> 打包说明：这是给"在这台机器之外写脚本"的人看的速查。完整细节在 `SKILL.md`。
> 本文件里的每条命令都在真实运行的服务上跑过，响应是实际返回的。

本文件里每一条命令都在**真实运行的服务**上跑过，响应是实际返回的。

> ⚠️ **8800 连着真设备。** `GET /api/state` 实测返回 `"simulate": false`、
> `"connected": true`、`"serial": "UMVX"`。**对 8800 的写操作会真的动设备。**
> 要试写操作，起一个模拟器实例：
> ```bash
> cd <install-dir>
> .venv/bin/python3 start_web.py --simulate --host 127.0.0.1 --port 8807
> ```
> 本文件里所有写操作的实测结果都来自这个模拟器实例（8807），不是 8800。

---

## 0. 两套路由，先选对

| | `/api/v1/...` | `/api/...` |
|---|---|---|
| 用途 | **对外公开契约**，脚本请用这套 | UI 自己的内部状态 |
| 响应 | 每条都带 `ok` 字段 | 不带 `ok` |
| 稳定性 | 有契约保证 | 字段名为 UI 服务，无保证 |

**线上实测规模**（`GET /api/v1/openapi.json`，45886 B）：**50 条 path / 52 条路由**
（`/api/v1/stroke` 和 `/api/tick` 各有两个方法）。按前缀分：

- `/api/v1/` — 18 条 path
- `/api/`（UI 家族）— 27 条 path
- 页面自身（`/`、`/index.html`、`/js/{name}`、`/vendor/{name}`、`/icon/{name}`）— 5 条

拿到全部接口清单：

```bash
curl -s http://<lan-ip>:8800/api/v1/openapi.json \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); [print(m.upper(),p) for p,v in sorted(d["paths"].items()) for m in v if m in ("get","post","put","delete","patch")]'
```

---

## 1. 完整控制流程（十步，全部实测）

**关键顺序：`arm` 必须在 `connect` 之后**，且没有任何一步能替代它。

```bash
U=http://<lan-ip>:8800
B=$U/api/v1
```

### ① 连接设备

```bash
curl -s -X POST $B/connection/connect
```
```json
{"ok":true,"connected":true,"serial":"UMVX"}
```
连第二次是安全的（幂等）：
```json
{"ok":true,"connected":true,"already":true}
```

> 连指定设备（UI 家族，需要先 `GET /api/devices` 拿到地址）：
> ```bash
> curl -s -X POST -H 'Content-Type: application/json' \
>      -d '{"address":"E7:97:7D:XX:XX:XX"}' $U/api/connect
> ```

### ② 激活传感器（**UI 家族，遥测必需**）

```bash
curl -s -X POST $U/api/arm
```
```json
{"armed":true,"frames":53,"latest":[2896,2520,1849,1500,1794],"object":{...}}
```

- **`/api/v1` 里没有 `arm`**。遥测数据必须走这条。
- **不要重复发遥测指令**：设备会把第二条相同指令当成模式切换，反而打断数据流。用 `POST /api/arm`，不要裸写。
- 刚连上就 arm。已经流了一会儿的连接再 arm 会返回 `arm_accepted:false`、`frames:0`，那是上个会话的残留，不是设备坏了。

### ③ 读身份和计数器

```bash
curl -s $B/connection
```
```json
{"ok":true,"connected":true,"link_ok":true,"reconnects":0,"connected_seconds":0.0,
 "frames_sent":14306,"write_errors":0,"device":{"serial":"UMVX","firmware":"1.0.11",
 "power":15.300328254699709,"battery_percent":72,"battery_cell_volts":3.8251,
 "rssi":-31,"mtu":255},"platform":"linux","simulated":false,"error":""}
```

> `frames_sent` 数的是**写入次数**，不是收到的帧数。刚连上时 `frames_sent:1` 而遥测已经报 `frames:521`，两者不是一回事。

### ④ 保持一个位置（`vibration` 省略 = 保持当前档位）

```bash
curl -s -X POST -H 'Content-Type: application/json' \
     -d '{"percent":45,"vibration":90}' $B/stroke
```
```json
{"ok":true,"percent":45.0,"vibration":90,"held":true}
```

**实测：省略 `vibration` 不会把它清零。** 上一步是 90，再发 `{"percent":50}`：
```json
{"ok":true,"percent":50.0,"vibration":90,"held":true}
```
之后 `GET $B/stroke` 仍然是 `"vibration": 90`。只调位置不会悄悄关掉之前的振动。

**实测：这个位置会被「保持」，不会自己缩回去。** 发
`POST /api/v1/stroke {"percent":100}`，4 秒后设备仍在 100%，一分钟
后仍在 100%。

以前不是这样。HTTP 调用方没有网页，也就没有心跳，服务端的空闲守卫
把它当成「客户端不在」，2.5 秒后就把位置收了回去——**接口回的是
`200`，设备却自己缩了**。而这条路由自己的文档写着 "Applies
immediately and holds until changed"，**它当时并没有保持**。

**什么会结束这个保持**（每一条都是显式动作）：

| 动作 | 路由 |
|---|---|
| 松手 | `POST /api/v1/stroke/release` |
| 换个位置 | `POST /api/v1/stroke {"percent":30}`（重新保持） |
| 自动扫动接管 | `POST /api/v1/auto/start` |
| 步态接管 | `POST /api/v1/gait/start` |
| 停止 | `POST /api/v1/auto/stop`、`POST /api/v1/gait/stop`、`POST /api/stop` |
| 断开连接 | `POST /api/v1/connection/disconnect` |

**`held` 告诉你你的指令是否还在生效**，POST 的返回值和 `GET` 的
body 里都有。

**`hold: false` 是退出选项**，恢复旧行为（调用方安静下来就自动松开，
跟网页的安全网一样），并且会**清掉之前那条命令留下的保持**：

```bash
curl -s -X POST -H 'Content-Type: application/json' \
     -d '{"percent":45,"hold":false}' $B/stroke
```
```json
{"ok":true,"percent":45.0,"vibration":90,"held":false}
```

**`percent: 0` 是松手，不是保持**——设备回到零位并停在那儿，不占保持。

**网页的安全网没有被削弱。** 浏览器标签页关掉后仍在 2.5 秒内收回
位置，因为网页从不占这个保持——只有 API 的 stroke 路由占。两者是
分开的：网页的保持靠心跳，这个保持靠命令。

**最近的那条指令拥有这个位置。** 如果网页随后又动了行程，网页的
指令接管，调用方的保持结束——网页的安全网重新对网页的位置生效。

这是**真机上发现的一个 bug**：agent 下了 100%，网页把行程拖到 30%，
标签页关掉，位置却留在外面——因为 agent 的保持还生效着，网页自己
的安全网根本没机会跑。

所以：**要把设备交出去，就得先松手**，否则下一个来自别的界面的
指令会继承调用方的保持。松手就是交接：

```bash
curl -s -X POST $B/stroke/release
```

### ⑤ 自动扫动

```bash
curl -s -X POST -H 'Content-Type: application/json' \
     -d '{"min_percent":25,"max_percent":75,"traverse_seconds":0.6}' $B/auto/start
```
```json
{"ok":true,"running":true,"min_percent":25.0,"max_percent":75.0,"traverse_seconds":0.6}
```

### ⑥ 读实时触摸遥测（需要第 ② 步）

```bash
curl -s $B/pressure
```
```json
{"ok":true,"position":0,"position_pct":0,"active_slot":null,"touch_raw":0,
 "touch_span_pct":0,"resting_level":0,"press_delta":400,"pressure":0,"raw_pct":0,
 "peak":2783,"pressed":false,"press_events":359,"frames":12674,"streaming":true,
 "age_s":0.03,"sample_hz":33.3,"interval_ms":30.03}
```

- **`pressure` 不是力。** 设备把行程量化成几档，报的是最深的一档。`touch_raw` 是 12 位 ADC，读数偏离静止位多少说明**摸到哪**。`position_pct` 才是有用的那个数。
- **`/api/v1/pressure` 没有 `armed` 字段**。`armed` 在 UI 家族的 `/api/pressure` 上。用 `streaming` 和 `frames` 判断。

### ⑦ 电量（用厂商自己的曲线换算）

```bash
curl -s $B/battery
```
```json
{"ok":true,"percent":72,"percent_shown":72,"samples":2,"median_cell_volts":3.8245,
 "filter_reason":"waiting for 3 samples (2 so far)","pack_volts":15.3,"cell_volts":3.8251,
 "table_cell_volts":3.8278,"table_percent":72,"in_range":true,"cell_count":4,
 "connected":true,"note":null}
```

- `percent` 是**当前**换算值；`percent_shown` 是过了 10 秒中值滤波后才有的值（要连续两次确认下降才抑制上升），样本不够时是 `null`。要即时值就读 `percent`。
- `filter_reason` 说明为什么 `percent_shown` 还是 `null`。

### ⑧ 时间序列

```bash
curl -s "$B/pressure/series?seconds=10"
```
```json
{"ok":true,"channel":0,"values":[2280,2197,2115,2034,1955,...],
 "unit":"raw 12-bit ADC (touch position, not force)","position":0,"position_pct":0,
 "count":191,"requested_seconds":10.0,"window_seconds":26.8,"sample_hz":19.1,...}
```

**参数边界实测（越界是普通字符串 detail，不是 Pydantic 列表）：**
```
GET /api/v1/pressure/series?seconds=15   -> 422 {"detail":"seconds must be one of 10, 30, 60"}
GET /api/v1/pressure/series?channel=2    -> 422 {"detail":"channel must be 0 or 1"}
```

**`window_seconds` 和 `requested_seconds` 不可比，也永远不可比。** 缓冲比你要的窗口老，所以 `window_seconds` 会一路涨过 10，这不是 bug。要正好 10 秒，自己按 `t`（相对）或 `unix`（绝对）切片。

### ⑨ 停止扫动、松手

```bash
curl -s -X POST $B/auto/stop      # {"ok":true,"running":false}
curl -s -X POST $B/stroke/release # {"ok":true,"percent":0.0}
```

### ⑩ 断开连接

```bash
curl -s -X POST $B/connection/disconnect
```
```json
{"ok":true,"connected":false}
```

**断开连接会先停掉正在跑的扫动**，不会让设备停在某个位置。

---

## 2. 完整路由表

### 连接（4 条）

| 方法 | 路径 | 前置 | 实测 |
|---|---|---|---|
| GET | `/api/v1/connection` | 无 | 200，链接状态+身份+计数器 |
| POST | `/api/v1/connection/connect` | 无 | 200 `{"ok":true,"connected":true,"serial":"..."}` |
| POST | `/api/v1/connection/connect`（再连一次） | 已连接 | 200 `{"ok":true,"connected":true,"already":true}` 幂等 |
| POST | `/api/v1/connection/disconnect` | 无 | 200 `{"ok":true,"connected":false}` |

### 行程（4 条）

| 方法 | 路径 | 前置 | 实测 |
|---|---|---|---|
| GET | `/api/v1/stroke` | **需要连接** | 200 `{"ok":true,"percent":0.0,"active_percent":0.0,"reported_percent":null,"vibration":0,"streaming":false,"auto_running":false,"held":false}` |
| POST | `/api/v1/stroke` | 需要连接 | 200 `{"ok":true,"percent":45.0,"vibration":90,"held":true}` |
| POST | `/api/v1/stroke/release` | 需要连接 | 200 `{"ok":true,"percent":0.0}` |
| POST | `/api/v1/intensity` | 需要连接 | 200 `{"ok":true,"level":2}` — 设备原生三档 |

**`percent` 是你下的指令，`active_percent` 是发送循环现在发的值**——扫动时两者会不同。`reported_percent` 在设备自己的位置遥测到达前是 `null`。

**`/api/v1/intensity` 的 `level` 是序数，不是百分比**：`0`=低 `1`=中 `2`=高，帧是写死的，没有第四档，也无法缩放。要百分比请用 `vibration`（0–255）。
```
POST /api/v1/intensity {"level":5} -> 422 "Input should be less than or equal to 2"
```

### 自动扫动（4 条）

| 方法 | 路径 | 前置 | 实测 |
|---|---|---|---|
| GET | `/api/v1/auto` | 无 | 200 `{"ok":true,"running":false,"cycles":0,"current_percent":0.0,"min_percent":10.0,"max_percent":70.0,"traverse_seconds":0.5,"vibration":0,"dwell_seconds":0.0,"error":""}` |
| POST | `/api/v1/auto/params` | **无** | 200 — 断连也能用 |
| POST | `/api/v1/auto/start` | 需要连接 | 200 `{"ok":true,"running":true,"min_percent":25.0,"max_percent":75.0,"traverse_seconds":0.6}` |
| POST | `/api/v1/auto/stop` | 无 | 200 `{"ok":true,"running":false}` |

**运动模板 `preset`**（UI 家族 `/api/auto/start`、`/api/auto/params` 才认）：

| `preset` | 形状 | 峰值速度 vs sine | 感觉 |
|---|---|---|---|
| `sine`（默认） | `sin(x·π/2)` | 1.00x | 基准，两端静止 |
| `surge` | `sin(x·π/2)·(1+0.3·cos(x·π/2)²)` | 1.30x | 中段更活跃 |
| `sag` | `sin(x·π/2)·(1−0.3·cos(x·π/2)²)` | 0.82x | 中段更柔，两端占更多周期 |

- **`/api/v1` 家族根本没有 `preset` 字段**，`auto/start` 只能跑默认的 `sine`。
- **`GET /api/v1/auto` 不报 `preset`**——它用白名单字典构建响应，模板不在里面。别去轮询找这个字段。
- **v1 调用既不会丢掉你设的模板，也不会清掉正在跑的模板**（这个键根本到不了生成器）。实测：`/api/auto/start` 设了 `surge`，再用 v1 的 `auto/params` 只改 `max_percent`，模板仍是 `surge`。
- 未知模板被 **422 拒绝**，不是悄悄跑默认：
  ```json
  {"detail":[{"type":"value_error","loc":["body","preset"],
   "msg":"unknown motion template 'triangle'; expected one of sine, surge, sag"}]}
  ```
- `GET /api/auto`（UI 家族）在 `params.preset` 下报模板。

### 自动运动参数：记下来（1 条）

| POST | 路径 | 前置 | 回答 |
|---|---|---|---|
| POST | `/api/auto/remember` | 需要连接 | 200 — 记录参数，**不启动**自动模式 |

**为什么需要它。** 页面左边「自动运动」的滑块，**只在自动模式正在跑的时候**才推给服务端。所以操作员拖了滑块、然后直接按实体设备的「开始自动模式」键时，服务端根本没收到过这些数字——按键启动的是**默认**的 10–70%、0.5 秒周期。页面显示的是操作员设的，设备跑的是另一套。

这条路由把参数记下来（不启动任何东西），按键启动时用的就是它。

- 前置条件跟 `/api/auto/params` 一样：**需要连接**（409）、`max_percent >= min_percent`（422）、振动要这个型号允许（422）。
- **不会覆盖正在跑的自动模式的参数**——正在跑的时候参数本来就是最新的。
- **`preset` 和 `tick_ms` 是继承的，不是重置**：没指定就沿用当前值。
- 实测：拖滑块设成 25–85%、1.2 秒 → 按键 → 自动模式跑的就是 25–85%、1.2 秒。

### 步态 / 按键（6 条）

| 方法 | 路径 | 前置 | 实测 |
|---|---|---|---|
| GET | `/api/v1/gait` | 无 | 200 `{"ok":true,"running":false,"step":0,"laps":0,"steps":[],"repeat":true,"elapsed":0.0,"error":""}` |
| POST | `/api/v1/gait/start` | 需要连接 | 200 — `{"steps":"30:3,70,20:1.5"}` 走一串位置，每段停自己的时间 |
| POST | `/api/v1/gait/stop` | 无 | 200 — 停步态并松手 |
| GET | `/api/v1/buttons` | 无 | 200 — 绑定表 + 动作菜单 + 最近按键 |
| POST | `/api/buttons/bind` | 无 | 200 `{"button":"mode","action":"travel:0","bindings":{...},"version":2}` |
| POST | `/api/buttons/execute` | 无 | 200 `{"action":"travel:30","kind":"travel","arg":30,"ok":true,"error":""}` |

**动作目录由服务器给。** `actions` 里每项带 `action`（服务器认的字符串，如 `"travel:60"`）、`kind`、`arg` 和 `label`。**别自己拼动作字符串**，从列表里取，因为列表和校验器是同一张表。

`version` 每次绑定变化都加一，轮询时能分清"操作员改了"和"我这份过期了"。

### 电量与触摸（5 条）

| 方法 | 路径 | 前置 | 实测 |
|---|---|---|---|
| GET | `/api/v1/battery` | 无 | 200，见第 ⑦ 步 |
| GET | `/api/v1/battery/curve` | 无 | 200，8865 B，`cell_count=4`，`full_at=95`，`points` **122** 项 |
| GET | `/api/v1/pressure` | **需 arm** | 200，见第 ⑥ 步 |
| GET | `/api/v1/pressure/series` | **需 arm** | 200，未 arm 时 `count=0` |
| GET | `/api/v1/pressure/series?seconds=N` | 需 arm | `seconds` 只收 **10、30、60** |
| GET | `/api/v1/pressure/series?channel=N` | 需 arm | 只收 **0**（主）或 **1**（次）；两个通道都在 `channel0`/`channel1` 里返回 |

### UI 家族控制指令（8 条）

这些返回整个内部快照（**不带 `ok`**），从里面读你需要的字段。

| 方法 | 路径 | 请求体 | 实测 |
|---|---|---|---|
| POST | `/api/target` | `{"percent":40}` | 200 — 设目标位置 |
| POST | `/api/stop` | — | 200 — 松手：`target_percent` 归 `0.0`，**链接不断** |
| POST | `/api/keepalive` | — | 200 — 保活 |
| POST | `/api/connect` | `{"address":"..."}`（可选） | 200 — 连接或切设备 |
| POST | `/api/disconnect` | — | 200 — 断链，**先停扫动** |
| GET | `/api/frame/{percent}` | — | 200 `{"percent":50,"drive":5000,"vibration":0,"hex":"A5 5A 0D A0 B0 00 A0 01 0F 88 13 B0 E6"}` |
| GET | `/api/state` | — | 200 — 整个内部快照 |
| GET | `/api/pressure` | — | 200 — **这条才有 `armed`**，`object` 块带每档数据 |

**`GET /api/frame/{percent}` 不会动任何东西**——它把某个位置会编码成的帧渲染出来给你看字节，是看清"一个位置在链路上到底占多少"的最便宜办法。

**`POST /api/stop` 是松手，不是断连。** 链接还在。

### 设备选择与节奏（4 条）

| 方法 | 路径 | 实测 |
|---|---|---|
| GET | `/api/devices` | 200 `{"devices":[],"count":0,"hidden_count":0,"hidden":[],"scan_seconds":8.0}` |
| POST | `/api/tick` | 200 `{"tick_ms":20,"tick_hz":50.0,"frame_interval_ms":20,"frame_hz":50.0,"min_tick_ms":8,"max_tick_ms":30,"nominal_tick_ms":15,"running":false,"source":"operator"}` |
| GET | `/api/tick` | 200 同上 |
| GET | `/api/events` | **SSE 流**，见下 |

**`GET /api/devices` 有三个计数，第三个才是关键：**

```json
{"devices": [...], "count": N, "hidden_count": M, "hidden": [...], "scan_seconds": 8.0}
```

- `devices` / `count` — **本程序能控制的**设备
- `hidden` / `hidden_count` — 扫到了但**不给连**的，每条带 `reason`

隐藏的那些是**计数上报，不是静默丢弃**。"外面什么都没有"和"外面有但不能控制"是两回事，操作员必须能分辨——一个在广播却不提供的设备，在被点名之前看起来就像设备不见了。

每行字段：`address`、`name`、`rssi`、`product`、`identified`、`has_vibration`、`has_position_sensors`、`evidence`、`controllable`、`note`、`address_masked`。

- **`controllable` 的意思是"本程序能驱动它"，不等于"它是本厂的"**。Nexo 是本厂的，但发送路径没说它的传输协议，所以不提供；而一个广播了厂商服务 UUID 但名字读不出来的设备是"本厂 + 型号未知"——它可能是 Vero，能驱动——所以会提供，`note` 里说明型号没读出来。
- **`has_vibration: null` 是"型号没认出来"**，跟 `false`（型号已知但没有振动）不是一回事。
- **`address` 是真地址**（连接要用），**`address_masked` 是打码版**（OUI + 掩码）。要打印就打后者。
- **BLE 同时只允许一个 central。** 设备连着手机就不会广播，也不会出现在列表里。"不在列表里"经常意味着"你手机占着"，不是坏了。
- 扫描要 **8 秒**，且全局加锁，不能两个扫描同时跑。

**`POST /api/tick`** 带 `{"tick_ms": N}` 立即生效，不用重启。范围 **8–30 ms**（厂商 App 自己的范围），越界是 **422**，不是悄悄夹紧：
```json
{"detail":[{"type":"value_error","loc":["body","tick_ms"],
 "msg":"tick_ms must be between 8 and 30 ms (the vendor app's own range: GeneratedMotionPolicy.MIN_TICK_MS=8, MAX_TICK_MS=30). Got 5, which was REFUSED, not clamped."}]}
```

## 3. 错误怎么读

### 409 — 没连接

**实测，逐条路由**——**消息并不全一样**：

| 路由 | 请求体 | 状态 | `detail` |
|---|---|---|---|
| `GET /api/v1/stroke` | — | 409 | `not connected; POST /api/v1/connection/connect first` |
| `POST /api/v1/stroke` | `{"percent":50}` | 409 | 同上 |
| `POST /api/v1/stroke/release` | — | 409 | 同上 |
| `POST /api/v1/auto/start` | `{"min_percent":10,"max_percent":70}` | 409 | 同上 |
| `POST /api/v1/intensity` | `{"level":1}` | 409 | 同上 |
| **`POST /api/v1/gait/start`** | `{"steps":"30:2"}` | **409** | **`not connected`** —— 没有后面的尾巴 |
| `POST /api/v1/auto/stop` | — | **200** | 不需要连接（`{"ok":true,"running":false}`） |

**⚠️ 重要：请求体校验跑在连接检查之前。** 这决定了 422 和 409 谁先出现。实测（断连时）：

```
POST /api/v1/stroke  （无 body）      -> 422  {"type":"missing","loc":["body"],"msg":"Field required"}
POST /api/v1/stroke  {"percent":150} -> 422  "Input should be less than or equal to 100"
POST /api/v1/stroke  {"vibration":10} -> 422  "Field required"（缺 percent）
POST /api/v1/stroke  {"percent":50}   -> 409  not connected; POST ... first
```

也就是说：**body 不合法 → 422，跟连没连无关；body 合法但没连 → 409。** 你在断连状态下写脚本，会先撞到 422 而不是 409——**别以为拿到 422 就说明连上了**。

`GET /api/v1/stroke` 是**读**，断连时也 409。其余八条 v1 读（`connection`、`gait`、`auto`、`battery`、`battery/curve`、`pressure`、`pressure/series`、`buttons`）断连时都是 200。

**按状态码分支，不要匹配消息文本。**

### 422 — 四种形状

| 触发 | 响应体 | 含义 |
|---|---|---|
| `{"percent":150}` | `{"detail":[{"type":"less_than_equal","loc":["body","percent"],"msg":"Input should be less than or equal to 100","input":150,"ctx":{"le":100.0}}]}` | `percent` 范围 0–100 |
| `{"percent":50,"vibration":999}` | 同形状，`loc` 指向 `vibration` | `vibration` 范围 0–255 |
| `{"vibration":10}`（缺 `percent`） | `{"detail":[{"type":"missing","loc":["body","percent"],"msg":"Field required",...}]}` | `percent` 必填 |
| `{"min_percent":90,"max_percent":10}` → `/auto/params` | `{"detail":"max_percent (10.0) must be >= min_percent (90.0)"}` | 区间反了 |
| `{"max_percent":20}`（当前 min=25） | `{"detail":"max_percent (20.0) must be >= min_percent (25.0)"}` | 合并后仍反 |

**区间反了是故意拒绝，不是夹紧。** `auto/params` 是**部分更新**：只应用你给的字段，服务端把你的字段合并到当前参数上再校验。底层本来会悄悄把 `{min:80, max:20}` 换成 `{min:20, max:80}`——那对 UI 滑块是对的，对 API 调用者是错的，因为把错误藏在一个 `200` 里。所以 API 返回 `422`，**并且把两个数都写出来**，你不用再读一次状态才知道它比的是什么。

改之前先 `GET /api/v1/auto` 看当前范围，或者上下限一起发。

### 503 — 服务没接上

只有路由挂载时缺依赖才会到，正常部署不会。`{"detail":"service not wired"}`。

`GET /api/devices` 在**本机没有蓝牙适配器**时也是 503，带可读的 `detail`，不是 500。

---

## 4. 几个会咬人的字段语义

- **`pressure` 不是力。** 设备把行程量化成几档，报最深的一档。`touch_raw` 是 12 位 ADC，偏离静止位的量说明**摸到哪**。两个数都不表示"多用力"。`position_pct` 才是有用的。
- **`window_seconds` vs `requested_seconds` 永不可比。** 见第 ⑧ 步。
- **`percent_shown` 有滤波，`percent` 没有。** 见第 ⑦ 步。
- **`frames_sent` 数写入，不数收到的帧。**
- **连两次安全。** 第二次返回 `{"ok":true,"connected":true,"already":true}`。
- **断连会停扫动。** 不会让设备停在某个位置。
- **模拟器的身份是固定的**：`serial:"sim-device"`、`firmware:"1.0.11"`、`power:15.0`、`rssi:-40`、`mtu:255`。**这些是模拟器的值，别当设备测量值。**

---

## 5. `GET /api/events` 是流，不是轮询

**实测：**
```
HTTP/1.1 200 OK
content-type: text/event-stream; charset=utf-8
Transfer-Encoding: chunked

data: {"frames_sent": 641, "write_errors": 0, "target_percent": 0.0, ...}
```

每个 `data:` 载荷就是 `GET /api/state` 的那份快照。

**别用普通 JSON 客户端配短超时指望它返回一次。** 实测：40 秒读超时，客户端一直等到超时才放弃，一次都没返回。要用流式客户端，并且记得关掉。

想要实时状态有两条诚实的路：读流并解析每个 `data:` 行，或者轮询 `GET /api/state`。

---

## 6. 部署与安全

**8800 跑的不是本仓库的工作树。** 它跑的是 `<install-dir>`（systemd 单元 `vera-preview.service`）：

```
WorkingDirectory=<install-dir>
ExecStart=<install-dir>/.venv/bin/python -u start_web.py --host 0.0.0.0 --port 8800
```

副本和源码仓库共享 git 历史，一个部署脚本负责推过去并重启。**改完源码不跑部署，8800 上还是旧的。**

**录制文件在部署副本自己的目录里。** 实测 `GET /api/records`：
```json
{"records":[{"name":"<timestamp>.jsonl",
  "path":"<deploy-dir>/vera-station-preview/vero/data/records/<timestamp>.jsonl"}], ...}
```
是**部署树**的录制，不是你的工作目录的。

**⚠️ `current` 是"正在写"的文件，断开连接后就是 `null`。** 实测断开后返回 `"current": null, "recording": false`，而 `records` 里仍有已完成的文件。**想知道录制文件在哪，读 `records[].path`，别读 `current`。**

**实测：8800 的访问令牌门是关的。** 不带任何 token，`GET /api/state`、`/api/records`、`/api/v1/connection` 全部 200。也就是说**局域网里任何人都能访问所有路由**，包括设备身份和录制数据。这是出厂默认（面板是操作员自己在自己网络上的），不是被打开的洞——但写脚本的人要知道，操作员不想这样就设 `VERO_ACCESS_TOKEN` 重启：

```bash
VERO_ACCESS_TOKEN=<你的密钥> .venv/bin/python3 start_web.py --host 0.0.0.0 --port 8800
```

之后每个请求都要带 token（`X-Vero-Token: <密钥>` 或 `?token=<密钥>`）。页面自身需要的资源（`/`、`/js/*`、`/vendor/*`、`/icon/*`）豁免。

**API KEY 永远不会被回显。** 公开配置只报 `has_api_key` 和 `api_key_length`，key 存在仓库之外的一个 `600` 权限文件里。

---

## 7. 一句话速记

```
连上 → arm → 动它 → 读数 → 松手 → 断连
```

- 要对外写脚本，用 `/api/v1/...`（带 `ok`，有契约）
- 遥测必须先 `POST /api/arm`（UI 家族，v1 里没有）
- 错误按**状态码**分支，别匹配消息文本
- 区间反了是 422 拒绝，不会悄悄夹紧
- 省略 `vibration` = 保持当前档位，不是清零
- 8800 连着真设备，写操作先去 8807 模拟器上试

---

## 附：本文档的验证方式

- **只读调用**（GET）在 `http://<lan-ip>:8800` 实测
- **写调用**在本地模拟器 `http://127.0.0.1:8807` 实测（`--simulate`），因为 8800 连着真设备
- 路由清单来自 `GET /api/v1/openapi.json`，50 path / 52 route
- 校验脚本：`scripts/verify_api_doc.py`

  ```bash
  VERO_LIVE_HOST=<lan-ip> .venv/bin/python3 scripts/verify_api_doc.py
  # 期望：62 passed, 0 failed
  ```

  脚本自带模拟器实例，**不会向 8800 发写操作**。

- 完整细节版：`SKILL.md`（skill 名 `vera-station-http-api`）
