# 三个平台的准备清单

本文是 `install.py` 的依据，也是 Mac 和 Windows 上测试时要对着看的东西。

**每一条都标注了来源和是否实测过。** 没在真机上跑过的（macOS 和 Windows）明确标出来 ——
那些是从上游 bleak 3.0.2 的文档和源码读来的，不是猜的，但**没有在这台机器上执行过**。

上游依据：bleak 3.0.2（本项目 `requirements.txt` 锁的版本），
<https://bleak.readthedocs.io/en/latest/> ，以及 PyPI JSON API 给出的
条件依赖清单。

---

## 共同的部分

| 事实 | 来源 | 实测 |
|---|---|---|
| bleak 需要 Python 3.10+ | bleak README | 是 |
| bleak 按平台选后端：Windows→WinRT，Linux→BlueZ，macOS→CoreBluetooth | bleak README | 是 |
| bleak 3.x 要求 **Windows 11 版本 22000 以上** | bleak README（v3.0.2 tag）。旧版本 README 里写的 "Windows 10, 16299" 是**过时的** | 文档 |
| bleak 3.x 要求 **BlueZ 5.55 以上** | bleak README | 文档 |
| bleak 3.x 要求 **macOS 10.15 以上** | bleak 文档 | 文档 |

---

## Linux

走 BlueZ。真正会出问题的地方：

1. **`bluetooth` 服务必须跑** —— 它持有 `org.bluez` 这个 D-Bus 名字。
   `sudo systemctl enable --now bluetooth`
2. **D-Bus 系统总线必须跑** —— bleak 的 Linux 后端是**通过 D-Bus 跟 BlueZ 说话**的，
   dbus-fast 连的是 `unix:path=/run/dbus/system_bus_socket`。
   BlueZ 在跑但 D-Bus 没跑，一样扫不到设备，而且报错是 bleak 内部抛的
   `BleakBluetoothNotAvailableError`，看起来像"库坏了"。
   `sudo systemctl enable --now dbus`
3. **适配器必须在，而且已上电** —— `bluetoothctl list` 看得到，
   `bluetoothctl show` 里 `Powered: yes`。
4. **适配器必须有 `central` 角色** —— GATT 客户端**就是** central。
   只会 advertise（peripheral）的蓝牙棒扫不到东西，而原因藏在
   `bluetoothctl show` 的 `Roles:` 那一行里。
5. **BlueZ 版本 ≥ 5.55** —— 低了**扫不到任何东西**，看起来像"设备没开机"。

### 不需要做的事（都实测过了）

- **不需要 `usermod -aG bluetooth`。** 实测：一个**没有任何附加组**的用户
  （`uid=1003(nogp) groups=1003(nogp)`）照样扫到 3 个设备，
  `bluetoothctl list` 也正常。原因是 BlueZ 自带的
  `/usr/share/dbus-1/system.d/bluetooth.conf` 里
  `<policy context="default">` 允许每个用户 `send_destination="org.bluez"`。
  加组这一步是 cargo cult。
- **不需要 root。**
- **不需要 `Experimental = true`** —— bleak 只用标准 D-Bus 接口。

### 包名（Debian/Ubuntu）

- `bluez` 提供 bluetoothd、bluetoothctl、hciconfig、btmgmt、btmon（`dpkg -S` 实测）
- `rfkill` 是**单独的包**，想检查/解除软屏蔽才需要
- `python3-venv` —— 建 venv 要它

---

## macOS

CoreBluetooth 在进程里，所以**没有适配器要配、没有守护进程要起**。
唯一的门槛是**权限**，而且这个权限是给**终端程序**的，不是给 Python 的。

> macOS 11 (Big Sur) 起，用 CoreBluetooth 的命令行程序，
> 它的**宿主应用**（Terminal.app / iTerm2 / VS Code / PyCharm）
> 必须在「系统设置 → 隐私与安全性 → 蓝牙」里被允许。
> Python 脚本从终端跑，继承的是**终端**的授权。

三个来源互相印证（bleak 的 macOS 后端文档、bleak 的 troubleshooting 文档、
以及 btleplug 这个 Rust BLE 栈的 README）—— 置信度高，但**没在这台机器上执行过**，
在 Mac 上第一次用的时候请先确认这一条。

被拒绝的后果：bleak 每次访问蓝牙都抛 `BleakBluetoothNotAvailableError`，
而且**权限不能靠程序重新申请** —— 只能去系统设置里打开。

### 其它 macOS 特有的坑

- **没有配对 API**：`bleak.BleakClient.pair()` 抛 `NotImplementedError`，
  `pair=True` 被静默忽略。CoreBluetooth 会在**第一次访问需要加密的特征时**
  自动配对 —— 也就是说一次 GATT 读写**可能阻塞在等用户点确认**。
  所以客户端的超时必须给得宽。
- **设备标识是 CoreBluetooth 生成的 UUID，不是 MAC 地址**，
  而且这个 UUID **只在做过那次扫描的机器上有效**。
  Linux 上抓到的 MAC 在 Mac 上**不能**当 `--target` 用。
  （本项目的匹配走**广播名前缀**和**服务 UUID**，两者都来自广播、
  跟地址无关，所以不受影响。）
- **macOS 12.0–12.2**：扫描**什么都不返回**，除非给 `BleakScanner`
  传一个非空的 `service_uuids` 列表。12.3 修好了。
- **`scanning_mode='passive'` 在 macOS 不支持**，会抛 `BleakError`。
  （本项目没有用它。）

---

## Windows

走 WinRT（`winrt-*` 那一组包，bleak 会自己装）。
不用装驱动、不用配适配器。

1. **蓝牙支持服务**（`bthserv`）要跑
2. **无线电要开着** —— bleak 自己的 WinRT 扫描器会检查：
   `radio.state != RadioState.ON` 就抛
   `BleakBluetoothNotAvailableError(..., POWERED_OFF)`。
   开关在「设置 → 蓝牙和设备 → 蓝牙」。
3. **要有一个被系统枚举到的 BLE 适配器** ——
   本项目 `vero/diagnose.py` 里的
   `Get-PnpDevice -Class Bluetooth | Select-Object -ExpandProperty FriendlyName`
   就是不需要管理员权限的检查。

### 不需要做的事

- **不需要管理员权限**，也不需要 UWP 的 "bluetooth" 应用能力 ——
  那个要求是给打包/UWP 应用的，bleak 从普通 CPython 走 `winrt` 包，
  没有 manifest。（已查 bleak 3.0.2 的 WinRT 后端：只用 `winrt.*` 和标准库
  `ctypes`，不依赖 pywin32/pythoncom。）

### Windows 最大的坑：STA 线程

bleak 的 Windows 后端会调 `assert_mta()`。
如果线程是 Single-Threaded Apartment 且没有消息循环，
WinRT 的异步回调**永远不会完成**，bleak 就**永久挂住**。
任何 import 了 `pythoncom` 的包（pywin32 那些）都会把线程变成 STA。

报错是 `BleakError('Thread is configured for Windows GUI but callbacks are not working.')`。

修法：`sys.coinit_flags = 0`（在 import pythoncom 之前），
或者早期调 `bleak.backends.winrt.util.uninitialize_sta()`。

**对本项目已核实安全**：`grep -rn -E "import (win32|pythoncom|pywintypes)"
--include="*.py"` 在整个仓库返回**零结果**，所以本项目不会拖进 pywin32，
不会踩这个坑。

---

## 依赖从哪下

依赖来自 PyPI，国内直连很慢甚至下不动。

**软件源永远不会被悄悄改掉**，规则只有一条：**你自己配的优先**。

| 你的设置 | 用哪个源 |
|---|---|
| `PIP_INDEX_URL` | 你的（pip 自己认这个变量） |
| `VERO_PIP_INDEX` | 你的（没有上面那个时） |
| pip 自己的配置文件 | 你的（上面两个都没有时） |
| 都没有 | 官方源 `https://pypi.org/simple` |

pip 自己的优先级（实测）：命令行 `--index-url` > `PIP_INDEX_URL` 环境变量 >
pip 配置文件 > 默认。

**代理不会被改动。** 环境里的 `http_proxy` / `https_proxy` 是你的事，
pip 自己会读，安装程序只读、只报告、**绝不写**。

`no_proxy` 要把**文件服务器**也写进去，不只是索引服务器：

- PyPI：`no_proxy=pypi.org,files.pythonhosted.org`（wheel 是从后者下的）
- 镜像站：只要它自己那一个（镜像的索引和文件同源）

### 国内源

| 源 | 地址 | 实测 |
|---|---|---|
| 清华 | `https://pypi.tuna.tsinghua.edu.cn/simple` | ✅ 装成功，wheel 的 sha256 和 PyPI 一致 |
| 阿里云 | `https://mirrors.aliyun.com/pypi/simple/` | ✅ 同上 |
| 腾讯云 | `https://mirrors.cloud.tencent.com/pypi/simple` | ✅ 同上 |
| 华为云 | `https://repo.huaweicloud.com/repository/pypi/simple` | ⚠️ **慢一拍**：fastapi 索引比 PyPI 少一个版本（0.161.1 vs 0.162.0） |
| 中科大 | `https://mirrors.ustc.edu.cn/pypi/simple` | ✅ |
| 北外 | `https://mirrors.bfsu.edu.cn/pypi/web/simple` | ✅ |
| 南大 | `https://mirror.nju.edu.cn/pypi/web/simple` | ✅ |

**索引地址必须以 `/simple` 结尾** —— 实测：
`-i https://pypi.tuna.tsinghua.edu.cn/` 报「找不到可用版本」，
`-i https://pypi.tuna.tsinghua.edu.cn/simple` 正常。结尾的斜杠可有可无。

**别用这两个**：

- `pypi.douban.com` —— **已死**。TLS 握手能完成，然后每个请求都失败，
  报错和断网一模一样。`pypi.doubanio.com` 只是 302 跳到腾讯云。
- `mirrors.szu.edu.cn` —— 从这里连不上（TCP 都连不上）。

### `--trusted-host` 什么时候才需要

**几乎从不需要 —— 而且加错会有安全后果。**

`--trusted-host` 不是"这个源我信"，它是给那个主机**关掉 TLS 证书校验**
（pip 挂上 `InsecureHTTPAdapter`，它的 `cert_verify()` 调的是
`super().cert_verify(conn, url, verify=False)`）。

上面那些源**全部是 HTTPS 且证书受信任**（GlobalSign / Let's Encrypt /
ZeroSSL），pip 直接就能装，**不需要**这个参数。实测 wheel 的 sha256 在
pypi.org、清华、阿里、腾讯上完全一致。

只有两种情况需要：

1. 源是 `http://` 的 —— pip 会**直接忽略**这个源并报「找不到可用版本」，
   和缺包长得一模一样。加了 `--trusted-host` 才会去看它。
2. 机器的证书库验不过这个源 —— 公司代理拦截、旧 Windows 的 CA 库。
   pip 会报 `[SSL: CERTIFICATE_VERIFY_FAILED]`，然后同样是一句
   「找不到可用版本」。

### 一个假错误：跨平台解析

`pip download --platform win_amd64 --python-version 3.10 bleak` 会报
`ResolutionImpossible`，而错误信息说 **`dbus-fast` 找不到匹配版本**。

**这是假象**：pip 在跨平台解析时用**宿主**的 `sys_platform` 判断条件依赖，
所以它把 Linux 专用的 `dbus-fast` 也算进去了。Windows 上真的装
`bleak` 没有任何问题。（实测：同一个目标平台，Windows 专用的
`winrt-windows-devices-bluetooth` 等 9 个包全部正常解析。）

**别追这个** —— 它不是真的依赖问题。

### Python 3.10 还是 3.11？

bleak 声明 `Requires-Python: >=3.10`，但 Linux 的依赖 `dbus-fast`
的版本下限动过：

| dbus-fast | 要求 |
|---|---|
| 1.83.0 | >=3.7 |
| 5.0.26 | >=3.10 |
| 5.1.0 / 5.2.0 | **>=3.11** |

所以在 Python 3.10 上 pip 会**回溯**到 dbus-fast 5.0.x。
实测这个回溯只多花 **0.3 秒**（3.10 解析 1.0 秒 vs 3.11 解析 0.7 秒），
**不值得**因此把门槛提到 3.11 —— Ubuntu 22.04 装的就是 3.10。

**结论：保持 3.10。**
