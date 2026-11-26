# 普通摄像头如何模拟 OpenXR 手部追踪

**Meta XR Simulator Windows v207.1 · `webcam_tracking_server.exe` 静态解剖**

一台 RGB 摄像头就能让 OpenXR 应用收到双手 26 个关节和头部转向。摄像头提供图像，MediaPipe 预测手形，模拟器负责把 21 个点变成一个米制的、带朝向的、不抖的手。

本文的依据是 Meta 的公开文档，以及对 v207.1 安装包里这个摄像头服务的静态分析。公开文档确认了「21 点到 26 关节」「掌宽估深」「头部只取旋转」这些设计；字节码进一步回答了文档没写的那部分——**全手投影拟合优先，掌宽估深只是回退**，以及拟合失败时判据是什么。

这篇里没有精度和延迟的测量。凡是没测的，下文都会说明。

---

## 0. 七条结论

| 问题 | 答案 | 代码位置 |
|---|---|---|
| 用什么检测 | MediaPipe 0.10.33 的 `HandLandmarker` + `FaceLandmarker`，都是 `LIVE_STREAM` | `mediapipe_detector.py:167`、`head_pose_detector.py:268` |
| 手的位置怎么来 | 21 点针孔投影最小二乘拟合；失败才用掌宽估深 | `positioning.py:331`、`:206`、`:148` |
| 26 个关节怎么补 | 21 个直拷，5 个掌骨位取中点 | `retargeting.py:281` |
| 朝向怎么定 | 骨段方向 + 上一关节的屈伸轴构造 frame | `retargeting.py:346` |
| 怎么减抖 | 1€ 滤波，掌部参考点与关节相对位移分开滤 | `one_euro_filter.py:217` |
| 捏合怎么算 | 2D 与 3D 间距各按掌长归一化，取更保守的那个 | `pinch.py:130` |
| 头部为什么不动 | 平移三个分量写死 0.0，只发旋转 | `hand_tracking_service.py:184` |

每一条都有可执行断言，`tools/verify.py` 跑完打印 `19/19 claims verified`。

---

## 1. 它是什么：一个 12 MB 的 sidecar

`webcam_tracking_server.exe` 不是你链接的库，是 MetaXRSimulator 拉起来的独立进程。`SIMULATOR.dll` 通过 gRPC 向它要帧，服务端只有一个 RPC：`StreamFrames`。

包体事实：

```
12,407,739 B   webcam_tracking_server.exe
PyInstaller    onefile（CArchive + PYZ-00.pyz）
1,310          模块，全部 CPython 3.12 字节码
14             server 包里的业务模块
```

默认运行参数能直接从 `main.py` 的字符串常量里读出来，它们本身就说明了这个进程的定位：

| 参数 | 默认 | 说明 |
|---|---|---|
| `--port` | 33797 | gRPC 监听端口 |
| `--camera_width/height` | 640 × 480 | 请求的采集分辨率 |
| `--camera_fourcc` | MJPG | 请求的像素格式 |
| `--camera_backend` | dshow | Windows 上避开 Media Foundation，后者常常开得慢或打不开 |
| `--camera_fps` | 30 | 采集与推理节奏 |
| `--stream_fps` | 60 | 只发**新**结果的轮询频率 |
| `--head_fps` | 15 | 人脸推理独立限频 |
| `--process_priority` | high（Windows） | 和 Unity 抢 CPU |

两个细节值得留意：采集 30 Hz、人脸 15 Hz、推流 60 Hz 是三个独立节奏，说明流是「有新样本就发」而不是「定时重发」；`--process_priority high` 说明作者把端到端延迟当成硬指标——这个进程和渲染线程在同一台机器上抢核心。

模块 docstring 全都还在（PyInstaller 不会剥注释性字符串），这句话就写在 `positioning.py` 开头：

> We deliberately do NOT use the wrist landmark's image-space `z` for depth — in MediaPipe `hand_landmarks` the wrist (index 0) is the depth origin, so its `z` is always ~0 and never reflects forward/back motion.

这一句解释了下文为什么需要「掌宽」这个单目深度线索：MediaPipe 的 21 点里，**腕点的 z 恒等于 0**，它不能告诉你手伸多远。

## 2. 一帧的旅程

```
[采集线程]  cv2.read() → BGR/RGB → 时间戳(ms) → submit 到 landmarker
     │
     ▼
[MediaPipe LIVE_STREAM 回调]  30 Hz 手 / 15 Hz 脸（两条独立节奏）
     21 图像点 + 21 世界点 + 左右手分类；人脸给 4×4 变换
     │
     ▼
[每帧处理]
     1. 左右手身份关联（只用历史，不看未来）
     2. estimate_hand_placement → 腕点 + 刚性形状
     3. retarget 21 → 26 关节 + 朝向
     4. 1€ 滤波（先掌部参考点，再相对位移）
     5. pinch_strengths（2D 与 3D 间距）
     │
     ▼
[gRPC 线程]  每 1/60 s 轮询，有新样本才组帧 → StreamFrames 发出
```

第 1 步容易被忽略但很关键：MediaPipe 的 `handedness` 是**逐帧分类**，会抖。所以 `hand_identity_tracker` 用过去几帧的腕点位置和速度做预测，把这一帧的两只手和上一帧的 Left/Right 配对，代价函数是

```
cost(label, hand) = |预测腕点 − 观测腕点| + 0.05 × handedness 分数（标签不符时）
```

两只手靠得很近（图像距离 < 0.05）且两种配对的代价差小于阈值时，它宁可返回 `None`（这一帧不重标），也不猜。这是一个纯因果的关联器——不用未来帧，因为流式场景里没有未来。

第 2 步开始进入本文的主体。

## 3. 定位：把 21 个点拟合进针孔模型

### 3.1 未知量只有三个

MediaPipe 给了两组点：`image`（归一化图像坐标，21×2 有效）和 `world`（以手为中心的米制 3D，21×3）。刚性形状 `P[i]` 已知，要解的是相机坐标系里的平移 `t`（3 个未知数）。

给定相机焦距 `f`（以图像宽度为单位，默认 `0.741`，对应水平视场角 2·atan(0.5/0.741) ≈ 68°），把归一化图像点变成视线方向：

```
xn = (u − 0.5) / f
yn = (v − 0.5) × aspect / f
```

投影关系是 `X + tx = xn·(Z + tz)`、`Y + ty = yn·(Z + tz)`，整理成对 `t` 线性的形式：

```
tx − xn·tz = xn·Z − X
ty − yn·tz = yn·Z − Y
```

21 个点给出 42 条方程、3 个未知数。超定，用最小二乘。这就是 `_solve_translation` 里那个 `np.linalg.lstsq`——它不是拟合姿态，姿态是给定的，它只解平移。

### 3.2 两遍最小二乘，然后是一串否决

第一遍是普通最小二乘。第二遍按深度重新加权：

```
w[i] = 1 / max(depth[i], 0.02)        # 远处的点定位噪声大，权重低
lstsq(A·w, b·w)
```

求解前后有三道否决，任一不过就 `return None`：

- 深度必须全部有限、中位数为正（手不能在相机后面）；
- 解本身必须有限；
- 腕点深度落在 `(0, 3.0]` 米。

还有一个特殊情况不否决而是**修复**：如果腕点解出来比 8 cm 还近，先检查所有点是否仍在相机前方、重投影残差是否小于观测跨度的 25%；都通过就把整只手沿视线推回去，让腕点正好落在 8 cm。手贴到镜头前是真实场景，不该整只手掉回掌宽模型。

### 3.3 旋转只做两轮

`world` 坐标不是相机坐标：MediaPipe 是从**手部裁剪框**预测的，轴向跟着那条视线走。手在画面边缘时，这套轴和相机光轴差一个角度，直接拟合会把横向运动压缩、把深度估偏。所以外面套了两轮：

```python
R = identity
for _ in range(2):                     # _RAY_ITERATIONS = 2
    t = solve_translation(R @ P, rays)
    if t is invalid: break
    centroid = R @ mean(P) + t
    R = shortest_rotation(optical_axis, direction(centroid))
    t = solve_translation(R @ P, rays)
```

只允许绕「光轴 → 手的方向」这一个旋转，自由度是 2。这是刻意的：平面手形本身有朝向歧义，放开三轴会让拟合在镜面解之间跳。

### 3.4 相机空间 → stage 空间

解出来的 `t` 是相机空间的米。转 stage 是固定映射 `(−x, −y, z) + camera_position`（默认相机在 `(0, 0, −0.84)`），再把横纵放大 `reach_gain = (1.5, 2.0)`——深度 1:1 保留，左右上下放大，让手能够扫过整个视野。深度不动是有道理的：深度是唯一有物理意义的量，放大它只会让手穿模。

### 3.5 回退：掌宽估深

拟合被拒时，整只手退化成启发式（`estimate_stage_position`）：

```
size   = |uv[5] − uv[17]|                       # 食指根到小指根的图像距离
rel    = size / 0.15                            # 参考掌宽（归一化）
offset = −0.4 × (rel − 1)                       # 手越大越靠前
z      = center.z + clamp(offset, ±0.4)
```

`uv[5]` 是食指 MCP，`uv[17]` 是小指 MCP。选这一对而不是包围盒，是因为**握拳时指节间距不变**——包围盒会随着手指蜷起缩小，把"捏拳"误判成"手远了"。docstring 写得很直白：knuckle span is a stable scale cue。

注意这是**互斥**的两条路，不是两套估计做融合：拟合过了就是米制位姿，被拒就整只手退回启发式。而且回退会自己播报，`positioning.py:381` 只打一次 warning：

```
Pinhole placement rejected a hand; using the palm-width model (logged once)
```

grep 这一句，就知道实际跑起来有多少比例走了回退。

## 4. 21 → 26：补点与朝向

### 4.1 五个补出来的位置

OpenXR 要 26 个关节，MediaPipe 给 21 个。缺的正好是 5 个掌骨位。`_map_positions` 先建一张 21 项的直拷表（腕 + 每指 4 点），再补：

```python
positions[PALM]              = (lm[0] + lm[9])  / 2    #   0
positions[INDEX_METACARPAL]  = (lm[0] + lm[5])  / 2    #   6
positions[MIDDLE_METACARPAL] = (lm[0] + lm[9])  / 2    #  11
positions[RING_METACARPAL]   = (lm[0] + lm[13]) / 2    #  16
positions[LITTLE_METACARPAL] = (lm[0] + lm[17]) / 2    #  21
```

这里有一个只有对着字节码才看得见的巧合：**`:324` 和 `:326` 是逐字节相同的表达式**。`PALM`（0）和 `MIDDLE_METACARPAL`（11）算出的是同一个点，所以 26 个关节实际只有 25 个独立位置。

这不是 bug——两者本来都在掌心中线上，下游也不会因此出问题——但它说明这 5 个点是**几何补的**，不是解剖学量出来的：掌骨长度、掌骨角度都没有估计，直接取中点。

### 4.2 朝向是构造的，不是解出来的

OpenXR 的关节朝向约定是「骨段沿局部 −Z 指向远端」。用朴素 look-at 把骨段对到 +Z，每个手指关节会整体转 180°，蒙皮网格直接里外翻转，看上去就是一坨不透明的灰块。所以代码里专门有一段注释解释为什么是 `-bone_dir`：

```python
def _joint_frame(bone_dir, up_hint):
    return _look_rotation(-bone_dir, up_hint)      # -Z 指向指尖
```

第一层关节的 roll 由 up-hint 决定：四指用掌面法线，拇指用它自己的局部 hint（拇指不在掌平面里）。往下每个关节**继承父关节的 x 轴**（屈伸轴），只把 z 重设为当前骨段方向，x 正交化：

```python
z_axis = normalize(-bone_dir)
hinge  = parent_frame[:, 0]                       # 父关节的屈伸轴
x_axis = normalize(hinge - dot(hinge, z_axis) * z_axis)
if not z_axis.any() or not x_axis.any():
    return parent_frame                           # 退化时保持父朝向
return column_stack([x_axis, cross(z_axis, x_axis), z_axis])
```

为什么不每个关节都用掌面法线重算 roll？注释给了答案：**一根指骨蜷过垂直于掌面的位置时（握拳，或者指人的那个手势），掌面法线导出的 roll 会翻 180°**，蒙皮跟着拧。继承父关节的铰链轴就没有这个问题——铰链轴在屈伸过程中是连续的。

这段路径里没有 IK、没有骨长约束、没有关节角度限制。26 个位置全部来自 21 个观测点及其算术组合；朝向是纯几何构造。这一点后面会再提一次，因为它决定了"减抖"能做什么、不能做什么。

## 5. 滤波：1€ 与两级分解

### 5.1 一个会自我调节的低通

用的是 Casiez 等人的 1€ filter（CHI 2012）。一阶低通的系数由截止频率和帧间隔决定：

```
alpha(cutoff, dt) = 1 / (1 + tau/dt),   tau = 1/(2π·cutoff)
cutoff = min_cutoff + beta · |dx/dt|
```

手静止时速度接近 0，截止频率等于 `min_cutoff`，重滤波把 MediaPipe 的抖动压掉；手快速移动时速度项把截止频率抬上去，滤波变轻，避免拖尾。每个轴独立一个标量滤波器，3D 位置就是三个实例。

默认参数（模块级常量；`min_cutoff`、`beta` 和预测参数可用 `XRSIM_HAND_*` 覆盖）：

| 参数 | 值 | 含义 |
|---|---|---|
| `_DEFAULT_MIN_CUTOFF` | 4 Hz | 静止时的基线截止 |
| `_DEFAULT_BETA` | 16 | 速度对截止的斜率 |
| `_DEPTH_MIN_CUTOFF` / `_DEPTH_BETA` | 2 / 4 | 深度轴单独一套，更钝 |
| `_ROOT_D_CUTOFF` | 6 | 参考点速度估计的截止 |

深度轴单独调参是个信号：单目估出来的 z 噪声本来就比 x/y 大，用同一套参数会让手在前后方向上抖。

### 5.2 为什么要把参考点和相对位移分开滤

如果对 26 个关节各自独立滤波，手的整体平移和手指的局部运动会被同一组截止频率处理，慢速平移时整只手会"粘"在空间里。所以 `_filter_with_root` 分两步：

```python
raw_root = mean(positions[j] for j in PALM_JOINTS)     # 掌部 5 个关节的均值
root     = filter_root(raw_root)                       # 先滤整体
out[j]   = root + filter_local(positions[j] - raw_root) # 再滤相对位移
```

整体平移用一套（更平滑的）参数，手指局部用另一套。位移不会被重复计入，因为第二级滤的是**差值**。

参考点还带速度自适应预测：速度在 0.1–0.4 m/s 之间线性启用（低于 0.1 不预测，高于 0.4 全量），预测位移有 0.05 m 上限；单关节的门槛更松（0.15 m/s），上限更小（0.02 m），加速度夹在 30 m/s² 以内。这是为了补上滤波本身引入的相位延迟，同时不让预测在快速反向时冲出去——速度方向与上一帧相反时直接不预测。

### 5.3 为什么这不会破坏骨长

`_hand_state_from_detection` 把滤波结果写回关节位置，然后直接组装输出：

```python
positions = [(jp.position.x, jp.position.y, jp.position.z) for jp in joints]
smoothed  = filters[handedness].filter_positions(positions, t_s, ...)
for jp, (sx, sy, sz) in zip(joints, smoothed):
    jp.position.x, jp.position.y, jp.position.z = sx, sy, sz
return _joints_to_hand_state(joints, ...)
```

整个函数里没有 `lstsq`、没有 `solve_`、没有 `bone_length`、没有 `angle`（断言 `streaming-path-has-no-ik-solve` 对整个函数做否定扫描）。朝向沿用 retargeting 构造的那份，滤波只碰位置。

所以"减抖不破坏骨长一致性"这个说法要反过来理解：**从来没有约束过骨长**，所以也就无所谓破坏。手指长短会随着 MediaPipe 的预测逐帧微微变化，这套管线接受这一点。

## 6. 捏合：两个间距，取更保守的

```python
scale = |joints[WRIST] − joints[MIDDLE_PROXIMAL]|          # 3D 掌长
gaps  = [|joints[THUMB_TIP] − joints[tip]| / scale for tip in TIPS]
image = [|uv[4] − uv[tip]| / |uv[0] − uv[9]| for tip in TIPS]   # 2D，图像平面

gap = max(image[i], gaps[i] − 0.06)
strength = clamp((0.75 − gap) / (0.75 − 0.22), 0, 1)
```

三件事同时发生：

1. **两个尺度不一样**。2D 那条用图像平面的「腕 → 中指根」像素距离归一化，3D 那条用关节空间的「腕 → 中指近节」距离。两者都叫"掌长"，但量的是不同空间里的东西——所以不能混用，代码里也确实各算各的。
2. **3D 先扣 0.06 掌长的容差**。`max(2D, 3D − 0.06)` 的语义是「两种测量都算捏住才算」：3D 少扣一点，再取两者中更保守（间距更大 → 强度更低）的那个。
3. **为什么非要 2D**。docstring 里给了一个具体例子：面向相机的左手捏合，MediaPipe 的世界坐标会读成 0.29 掌长，和「C 形抬手」没区别。图像平面在这种情况下反而更靠谱。

强度是线性斜坡：0.22 掌长以内算捏合（1.0），0.75 以外算张开（0.0），中间线性。输出的四个字段直接对齐 `XrHandTrackingAimStateFB`，runtime 不需要再缩放。

## 7. 头部：只要旋转

FaceLandmarker 开了 `output_facial_transformation_matrixes`，所以头部姿态不是从面部关键点反算的，而是直接把 4×4 矩阵分解成平移 + 四元数。然后：

```python
frame.head_pose.pose.position.x = 0.0        # :202
frame.head_pose.pose.position.y = 0.0        # :203
frame.head_pose.pose.position.z = 0.0        # :204

mx, my, mz, mw = _mirror_head_orientation((ox, oy, oz, ow))
frame.head_pose.pose.orientation = (mx, my, mz, mw)
```

平移三个分量写死 0.0，紧接着三个 `STORE_ATTR`。**分解出来的平移被明确丢弃了**，不是算错、不是没算。

原因在 `head_pose_detector.py` 的 docstring 里：运行时把摄像头的头姿当作「相对启用时刻的 delta」叠加到 WASD / mouse-look 上。单目摄像头估出来的绝对平移会漂，一旦叠加进去就会把用户的视角慢慢拽走——我往前探身，虚拟相机也跟着往前飘，这在 VR 里是晕动症的来源。旋转是可靠的（人脸朝向），平移不是。

四元数还做了一次镜像（`_mirror_head_orientation`），让摄像头里的「向右转头」在 stage 空间也是 +X。人脸镜像和左右手标签是这类系统里最容易搞反的两处。

旋转本身也做了 slerp 平滑，权重是「最新样本占多少」，默认 `0.8`（`XRSIM_HEAD_SMOOTHING` 可覆盖，夹在 `(0.05, 1.0]`——0.05 保证姿态不会冻住，1.0 等于不平滑）。注意这里只滤四元数，平移既然一直是 0，也就没有可滤的东西。

## 8. 三个取舍

把上面的机制放在一起看，会发现它们共享同一种取舍：**少解一点，换稳一点、快一点。**

**一、不做 IK，接受骨长会变。** 21 个点直接算术补齐，没有骨长约束、没有关节限位、没有角度求解。好处是路径极短——一次最小二乘，加几次中点——坏处是手形完全由 MediaPipe 决定：模型把手指估短了，虚拟手就短一帧。不少手部追踪实现会用骨长做约束求解，代价是迭代和延迟。这里选了前者，和「每帧都要出结果、不能排队」的定位一致。

**二、只解平移，旋转只做视线修正。** 未知数从 6 个降到 3 个，平面手形就不容易在镜面解之间跳。代价是手腕的绝对朝向仍然来自 MediaPipe 的局部形状——如果模型把掌心估反，这套算法不会纠正它。头部处理是同一思路的延伸。

**三、头部只要旋转。** 单目绝对平移不可靠，而在 VR 里错误的位置比没有位置更糟：它会持续把用户视角带偏。扔掉平移，换来「转头永远对、位置永不漂」。反过来说，摄像头追踪不能替代 6DoF 头显定位，它只补朝向——这也是为什么它更适合做模拟器的输入，而不是做成产品级的追踪方案。

三个取舍的副作用是一致的：出了问题不会表现为「算错」，而是表现为「跟着 MediaPipe 错」。所以真要评估质量，得先有一份独立真值，而不是盯着虚拟手看。

## 9. 一个"要上生产"的 sidecar 还会管什么

算法讲完了。但这个包里有一半的代码在处理和算法无关的事，而正是这些代码说明它是产品的一部分，不是 demo。这部分值得单独列出来，因为它解释了为什么一个"摄像头追踪服务"要 550 行。

**父进程死了要自己退。** 前端崩了不会调 teardown，sidecar 会攥着摄像头不放。所以启动时挂一个存活性监视：Unix 上用前端给的 socket 路径（`XRSIM_PARENT_SOCK_PATH`），blocking `recv()`，对端关闭就是 EOF，毫秒级自毁；Windows 上没有 Unix socket，改成等父进程句柄（`XRSIM_PARENT_PID`，`WaitForSingleObject`），父进程一退立刻返回。没有轮询、没有超时。

**端口被占要自己抢回来。** 上一次的 sidecar 变成孤儿时还占着 33797，下一次开关就报 "failed to connect"。启动时用 `taskkill` 清掉同镜像名、PID 不是自己的进程，日志打一行 "Reclaimed gRPC port from stale sidecar(s)"。这是典型的现场故障反推出来的代码。

**要给 UI 一个不用 gRPC 的预览。** 本地 HTTP 服务提供 `/preview.jpg`（`Cache-Control: no-store`，JPEG 编码最新一帧），xrsim 的界面直接 `<Image>` 渲染。客户端轮询会频繁断连，所以专门重写了 `handle_error`，把断连降级成一行日志，免得真错误被 traceback 埋掉。

**录制有端点。** `/recording/start`、`/stop`、`/mark`、`/status`，可以只记录数据，也可以 `record_video: true` 同时存视频；`/mark` 打时间戳标记，方便回放时定位。默认空闲，要显式 POST 才开始。

**延迟是可观测的。** `--pipeline_telemetry` 打开后，11 条延迟序列（采集读、色彩转换、submit、回调年龄、处理、组帧、发出时的样本年龄……）各自一个**预分配的定长环**，写入不加锁、不分配内存，快照允许撕裂（docstring 明说 percentiles are approximate），每 10 秒打一次 p50/p95 汇总。除了延迟还有十几个计数器：序列缺口、身份歧义帧数、滤波重置次数、handedness 分歧、空轮询次数。这套东西的存在说明作者在真机上追过延迟。

**没有摄像头也能跑。** `--synthetic` 用程序生成的手驱动整条管线（只需要 grpcio/protobuf/numpy，不需要 mediapipe/opencv）；`--selftest` 走真实的 OpenCV + MediaPipe 启动路径、真实 gRPC servicer，输出一段 JSON 摘要，可选生成 MP4 作为证据。CI 就是拿它跑打包后的 sidecar 的。

这些细节不改变算法结论，但它们解释了为什么这份字节码值得读：它是有人为延迟、为崩溃、为现场故障改过的代码，不是教科书示例。

## 10. 这些结论是怎么读出来的

### 10.1 拆包

`webcam_tracking_server.exe` 是 PyInstaller onefile，尾部挂 CArchive，里面是 `PYZ-00.pyz`。用 `pyinstxtractor`（固定 commit）展开，得到 1310 个 `.pyc`，业务代码在 `server` 包里。

然后只用标准库：`marshal` 读代码对象，`dis` 反汇编。**不导入包内模块**——这些代码会开摄像头、连 gRPC，不该被执行。

行号来自代码对象的行表（`dis` 的 `starts_line`），就是 Meta 构包时源文件的真实行号。所有断言都绑在「某一行上出现了某条指令」，而不是绑在函数名或文本匹配上：

```json
{
  "id": "palm-equals-middle-metacarpal",
  "file": "server/retargeting.py",
  "function": "_map_positions",
  "checks": [{ "line": 324, "contains": ["MP_WRIST", "MP_MIDDLE_MCP", "STORE_SUBSCR"] },
             { "line": 326, "contains": ["MP_WRIST", "MP_MIDDLE_MCP", "STORE_SUBSCR"] }]
}
```

19 条这样的断言，`tools/verify.py` 一次跑完。

### 10.2 两个坑

**坑一：厂商动了字节码。** 每个模块里 `IMPORT_NAME`（108）都被替换成了 `CACHE`（0），操作数（`co_names` 的下标）留着。CPython 不会执行那个字节——PyInstaller 打包时已经把 import 图解析完了——但任何字节码工具都会在模块第一条 `import` 上死掉。修复是原地换一个字节，偏移、行表、异常表全都不动。

**坑二：Decompyle++ 的 3.12 支持只做了一半。** 它的 3.12 操作码表是全的，但 AST 层还按 3.11 的语义在跑。补丁 `tools/pycdc-3.12.patch`（+172/−13，只动 `ASTree.cpp` 和 `FastStack.h`）主要几处：

| 修的东西 | 不修的后果 |
|---|---|
| `MAKE_FUNCTION` 的操作数是标志位，不是数量 | 带默认值的签名全变成 `def f(self=None, a=('x','int',...))` |
| 3.12 把单参调用塞进 `CALL` 的 self 槽 | `@property` / `@dataclass` 变成 `velocity = (lambda ...)()` |
| `LOAD_BUILD_CLASS` 前的 `PUSH_NULL` 属于那次调用 | 装饰过的类打成 `(class)()` |
| 相对跳转是相对"下一条指令" | 每个块边界差两个字节，表现为莫名其妙的 `y = None` |
| `RETURN_VALUE` 不该吞掉下一条指令 | 那个 hack 是为了吃 3.11 的 `POP_BLOCK`，3.12 没有，于是吃掉分支后第一条语句 |

### 10.3 反编译产物

`src/` 下是 server 包 15 个文件的反编译结果，`tools/decompile.py` 把修字节 + 调反编译器串起来。**5 个能通过 `compile()`**（`positioning`、`retargeting`、`pinch`、`pipeline_telemetry`、`__init__`），其余有标注的残缺，每个文件头写明状态，[src/README.md](src/README.md) 有覆盖表。

残缺全部是**反编译器的**，不是二进制的：PEP 709 内联推导式没有独立代码对象可认，生成器表达式变成 `(lambda .0: pass)`，`BEFORE_WITH` 是空操作所以 `with` 丢了主语。

最需要警惕的是条件：**融进条件跳转的 `not` 会偶尔丢失**，链式比较会被拍平。`_solve_translation` 里真实代码是

```python
if not 0 < wrist[2] <= _MAX_CAMERA_DEPTH_M:
    return None
```

反编译出来是 `if not 0 < wrist[2] or wrist[2] <= ...`——按字面读会以为所有解都被拒绝。所以**读 `if` 要对着 `evidence/disassembly/`**，那里的指令原文不会骗人。

## 11. 不知道的部分

- **没有测精度和延迟。** 这篇讲的是"怎么算"，不是"算得准不准"。要回答后者需要同步的真值：一台能同时输出 RGB 和深度的手机，同一帧保存下来，比较指尖与掌根的深度差，就能验证 `Z(食指尖) − Z(掌根)` 有没有估反。
- **只覆盖 sidecar。** `SIMULATOR.dll` 是 C++，它怎么消费这些帧、怎么把头部 delta 叠进相机、怎么和移动控制合成，不在本文范围。
- **版本敏感。** 行号和指令只对 v207.1（`d59aa218…530f`）成立。换版本重跑 `verify.py`，失败清单就是差异清单——这比重新人肉读一遍靠谱。
- **伪代码不是原文。** 文中所有 Python 片段都是按字节码转写的等价形态，变量名和排版是转写产物。真实标识符（`_solve_translation`、`_MIN_CAMERA_DEPTH_M`）来自 `co_names`/`co_varnames`，是准的。

## 12. 自己复核

交互式检查（只用标准库，不执行包内模块）：

```powershell
py -3.12 .\inspect_camera_tracking.py
py -3.12 .\inspect_camera_tracking.py --disassemble positioning:estimate_hand_placement
py -3.12 .\inspect_camera_tracking.py --disassemble positioning:_solve_translation
py -3.12 .\inspect_camera_tracking.py --verify-public-models
```

跑全部断言：

```powershell
py -3.12 .\tools\extract.py --install "C:\Program Files\MetaXRSimulator\v207.1" --out build
py -3.12 .\tools\verify.py --models "C:\Program Files\MetaXRSimulator\v207.1\webcam_tracking_server\_internal"
```

输出：

```
19/19 claims verified
  hand_landmarker.task: fbc2a300…cde1 identical to .../float16/1/hand_landmarker.task
  face_landmarker.task: 64184e22…c9ff identical to .../float16/1/face_landmarker.task
```

重新生成证据快照：

```powershell
py -3.12 .\tools\snapshot.py --install "C:\Program Files\MetaXRSimulator\v207.1"
```

### 结论对照表

| 结论 | 安装包内的函数 |
|---|---|
| MediaPipe 手部和头部模型 | `mediapipe_detector.py:167`、`head_pose_detector.py:268` |
| 21 点拟合优先，掌宽回退 | `positioning.py:206`、`:283`、`:331`，回退深度位于 `:148` |
| 补关节和沿手指定朝向 | `retargeting.py:281`、`:346`、`:362` |
| 左右手跨帧关联，整手与局部关节滤波 | `hand_identity_tracker.py:221`、`one_euro_filter.py:217`、`hand_tracking_service.py:218` |
| 2D 与 3D 捏合间距 | `pinch.py:101`、`:130` |
| 头部平移为零 | `hand_tracking_service.py:184` |

### 样本指纹

```
webcam_tracking_server.exe  d59aa21863beeaac5e2a70277aa5f40347dced6ad2625dab3fc63a55e529530f
hand_landmarker.task        fbc2a300…cde1   7,819,105 B   float16/1
face_landmarker.task        64184e22…c9ff   3,758,596 B   float16/1
```

两个 `.task` 与 Google 公开 CDN 上的模型逐字节一致，说明 Meta 没有微调模型，改的都在后处理。

## 仓库结构

```
inspect_camera_tracking.py  交互式检查脚本（文章原有）
claims/claims.json          19 条断言：文件、函数、行号、该行必须出现的指令
tools/extract.py            解包 sidecar（CArchive + PYZ）
tools/pycdis.py             按函数反汇编，保留 CPython 原始行号
tools/verify.py             跑断言 + 模型哈希比对
tools/snapshot.py           从解包结果重建 evidence/
tools/decompile.py          修字节 + 调反编译器，产出 src/
tools/pycdc-3.12.patch      Decompyle++ 的 3.12 补丁（+172/−13）
src/                        server 包 15 个文件的反编译结果 + 状态表
evidence/disassembly/       18 份按行号压缩的指令表 + dis 全文
evidence/inventory/         PYZ 全部 1310 个模块名
evidence/strings/           main.py / model_download.py 的字符串常量
evidence/models.txt         两个 .task 与 Google 上游的哈希对照
article/bytecode-notes.md   证据附录：逐条指到行号 + 等价伪代码
```

`evidence/` 是快照、`claims/` 是断言，都由 `snapshot.py` 从本机副本重新生成；`build/` 已忽略。

仓库不含 Meta 的二进制和模型文件。`src/` 是反编译产物，不是原始源码——5/15 能通过语法解析，其余在推导式、`with`、`try` 嵌套处有标注的残缺。来源与授权说明见 [NOTICE.md](NOTICE.md)，那里也写明了移除请求的方式。
