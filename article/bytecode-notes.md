# 附录：逐条对到字节码

Meta XR Simulator v207.1 · `webcam_tracking_server.exe` · CPython 3.12 字节码

正文见 [README](../README.md)。这一篇是它的证据附录：每条结论都指到具体的文件、行号，并给出该行上的指令原文；代码块是**根据字节码写的等价形态**（不是 Meta 的原文，行号注释对应汇编行）。文中「断言 `xxx`」指的是 [`claims/claims.json`](../claims/claims.json) 里的同名条目，由 `tools/verify.py` 跑。指令原文快照在 [`evidence/disassembly/`](../evidence/disassembly/)。

---

## 0. 拆的是什么

`webcam_tracking_server.exe`（12.4 MB，SHA-256 `d59aa218…530f`）是 MetaXRSimulator 启动的 sidecar：`SIMULATOR.dll` 通过 gRPC 向它要手部/头部的追踪帧。它是一个 PyInstaller 单文件包，尾部挂着 CArchive，里面是 `PYZ-00.pyz`——1310 个模块，全部是 CPython 3.12 字节码。业务代码在 `server` 包里，14 个模块：

```
server/main.py                  进程入口：参数、摄像头源、gRPC 服务、预览 HTTP
server/hand_tracking_service.py gRPC servicer + 每帧组装
server/mediapipe_detector.py    HandLandmarker 封装
server/head_pose_detector.py    FaceLandmarker 封装
server/positioning.py           图像 → stage 空间的腕点位置
server/retargeting.py           21 点 → 26 关节
server/one_euro_filter.py       1€ 滤波
server/pinch.py                 捏合强度
server/webcam_capture.py        采集线程
server/hand_identity_tracker.py 左右手连续性
server/synthetic_source.py      --synthetic 的假手
server/selftest.py              --selftest 的自检
server/session_recorder.py      录制
server/pipeline_telemetry.py    延迟统计
```

模块 docstring 都还在（PyInstaller 不会剥注释性字符串）。它们本身就说明了设计意图，比如 `positioning.py` 开头写着「深度故意不用 wrist 的 image-space z，因为 wrist 是 MediaPipe 的深度原点，z 恒为 0」——这句话解释了下文为什么需要「掌宽」这个单目深度线索。

---

## 1. 手部和头部都用 MediaPipe

两处 `start()` 的结构完全一样，只是类换成了 Face 版：

```python
# server/mediapipe_detector.py:167  MediaPipeDetector.start
options = mp.tasks.vision.HandLandmarkerOptions(
    base_options=mp.tasks.BaseOptions(model_asset_path=self._model_path),
    running_mode=mp.tasks.vision.RunningMode.LIVE_STREAM,      # :178
    num_hands=self._num_hands,
    min_hand_detection_confidence=..., min_hand_presence_confidence=...,
    min_tracking_confidence=...,
    result_callback=self._on_result,                            # :183
)
self._landmarker = mp.tasks.vision.HandLandmarker.create_from_options(options)   # :185
```

```python
# server/head_pose_detector.py:268  HeadPoseDetector.start
options = mp.tasks.vision.FaceLandmarkerOptions(
    ...
    running_mode=mp.tasks.vision.RunningMode.LIVE_STREAM,       # :279
    num_faces=1,                                                # :280
    output_facial_transformation_matrixes=True,                 # :284
    output_face_blendshapes=True,                               # :287（暂时没用到）
)
```

`output_facial_transformation_matrixes=True` 是关键：头部姿态不是从 468 个面部关键点反算的，而是直接用 MediaPipe 给的 4×4 变换分解成平移 + 四元数（`_decompose_transform`，`head_pose_detector.py:158`）。docstring 里说得很清楚：头部位置只是「相对启用时刻的 delta」。

模型文件本身也没改过。随包的两个 `.task` 与 Google 公开 CDN 上的模型**逐字节相同**：

```
fbc2a300…cde1  hand_landmarker.task  7,819,105 B   float16/1
64184e22…c9ff  face_landmarker.task  3,758,596 B   float16/1
```

`server/model_download.py` 里也写着同一个 `hand_landmarker` URL，作为首次运行缺失时的下载源。

---

## 2. 21 点投影拟合优先，掌宽估深回退

`estimate_hand_placement`（`positioning.py:331`）是位置计算的唯一入口，`hand_tracking_service.py:235` 每帧调它。它的形状是一棵树：

```python
def estimate_hand_placement(image_landmarks, config=None, world_landmarks=None, image_size=None):
    if config is None:
        config = DEFAULT_WORKSPACE
    if world_landmarks:                                       # :360 无世界坐标直接回退
        if image_size and image_size[0] > 0 and image_size[1] > 0:
            aspect = image_size[1] / image_size[0]
            if aspect != config.image_aspect:
                config = replace(config, image_aspect=aspect)  # :364 真实宽高比覆盖配置
        pose = solve_camera_pose(world_landmarks, image_landmarks, config)   # :365
        if pose is not None:                                  # :366
            rotation, translation = pose
            points = np.asarray(world_landmarks, dtype=np.float64)[:, :3]
            wrist = rotation @ points[_MP_WRIST] + translation
            cx, cy, cz = config.camera_position
            stage = (cx - float(wrist[0]), cy - float(wrist[1]), cz + float(wrist[2]))
            rotated = points @ rotation.T
            return (apply_reach_gain(stage, config),           # :375
                    tuple((float(x), float(y), float(z)) for x, y, z in rotated))
        if not _fallback_logged:
            _fallback_logged = True
            logger.warning("Pinhole placement rejected a hand; "     # :381
                           "using the palm-width model (logged once)")
    return (estimate_stage_position(image_landmarks, config), None)   # :385
```

函数签名里 `config` 在第一位、`world_landmarks` 在后面，但字节码里 `world_landmarks`/`image_size` 是关键字传进去的（`hand_tracking_service.py:235` 的 `KW_NAMES(('world_landmarks', 'image_size'))`），第一个位置参数是 `image_landmarks`。

`_solve_translation`（`positioning.py:206`）把「世界坐标刚性形状 + 图像观测」变成线性最小二乘。对每个点写出针孔投影的比值形式 `X + tx = xn · (Z + tz)`、`Y + ty = yn · (Z + tz)`，排成 `A·t = b`：

```python
xn = (image[:, 0] - 0.5) / config.focal_length                       # :224
yn = (image[:, 1] - 0.5) * config.image_aspect / config.focal_length # :225
a = np.zeros((2 * count, 3)); b = np.empty(2 * count)
a[0::2, 0] = 1.0;  a[0::2, 2] = -xn
b[0::2] = xn * points[:, 2] - points[:, 0]                           # :230
a[1::2, 1] = 1.0;  a[1::2, 2] = -yn
b[1::2] = yn * points[:, 2] - points[:, 1]                           # :233
translation = np.linalg.lstsq(a, b, rcond=None)[0]                   # :234
```

然后是一串闸门，任一不过就 `return None`，上层随即落到掌宽模型：深度中位数必须为正（`:238`）、腕点深度落在 `(_MIN, _MAX_CAMERA_DEPTH_M]`（`:246`）、所有点变换后仍在相机前方（`:250`）、重投影误差不超过 `_MAX_NEAR_REPROJECTION_ERROR`（`:256`）。

过了闸门还有第二遍最小二乘（`:242`）：以 `1 / max(depth, _MIN_POINT_DEPTH_M)` 作权重重解，归一化残差——远处的点定位噪声大，权重低。

回退路径的深度完全来自「手在画面里有多大」。`estimate_stage_position`（`:170`）把腕点图像坐标线性映射到 stage 的 x/y，z 用 `center[2] + _estimate_depth_offset(...)`；偏移量（`:148`）是

```python
size = _palm_width(image_landmarks)              # :157
rel  = size / config.reference_hand_size         # :163  相对参考掌宽
offset = -config.depth_range * (rel - 1.0)       # :166  手越大越靠前（-z）
return max(-config.depth_range, min(config.depth_range, offset))  # :167
```

而 `_palm_width`（`:135`）就是 MediaPipe 索引 5 到 17 的**图像平面**欧氏距离：

```python
ax, ay, _ = image_landmarks[_MP_INDEX_MCP]   # 5   食指根  :143
bx, by, _ = image_landmarks[_MP_PINKY_MCP]   # 17  小指根  :144
return math.hypot(ax - bx, ay - by)          # :145
```

这两个索引常量在模块级定死：`_MP_WRIST = 0`、`_MP_INDEX_MCP = 5`、`_MP_PINKY_MCP = 17`（`:130`–`:132`）。

所以「优先/回退」不是两套并行的估计再融合，而是**互斥**的：拟合通过就用米制位姿，被拒就整只手退化成掌宽启发式，且日志只抱怨一次。

---

## 3. 26 个关节怎么补、朝向怎么定

MediaPipe 给 21 个位置，OpenXR 要 26 个 6DoF 关节。`_map_positions`（`retargeting.py:281`）先建一张 21 项的直拷表（`:296`–`:317`，腕 + 每指 4 点），再补 5 个：

```python
positions[PALM]               = (lm[MP_WRIST] + lm[MP_MIDDLE_MCP]) / 2.0   # :324
positions[INDEX_METACARPAL]   = (lm[MP_WRIST] + lm[MP_INDEX_MCP])  / 2.0   # :325
positions[MIDDLE_METACARPAL]  = (lm[MP_WRIST] + lm[MP_MIDDLE_MCP]) / 2.0   # :326
positions[RING_METACARPAL]    = (lm[MP_WRIST] + lm[MP_RING_MCP])   / 2.0   # :327
positions[LITTLE_METACARPAL]  = (lm[MP_WRIST] + lm[MP_PINKY_MCP])  / 2.0   # :328
```

补出来的下标是 1 / 6 / 11 / 16 / 21，正好是 OpenXR 手部关节表里缺的那 5 个掌骨位（`XR_HAND_JOINT_COUNT = 26`，`:101`）。**注意 `:324` 和 `:326` 是同一个表达式**——编号 1 的 `palm` 与编号 11 的 `middle_metacarpal` 重合，26 个关节实际只有 25 个独立位置。这不影响下游（两者本来就都在掌心中线上），但它说明这 5 个点是几何补的，不是解剖学量出来的。

朝向不是解出来的，是构造出来的。第一层用两段骨定义 frame，第二层（远节指骨）继承父关节的屈伸轴：

```python
# :346 _child_joint_frame(parent_frame, bone_dir)
z_axis = _normalize(-bone_dir)                          # :354 当前骨段方向取反
hinge  = parent_frame[:, 0]                             # :355 上一关节的 x 轴 = 屈伸轴
x_axis = _normalize(hinge - np.dot(hinge, z_axis) * z_axis)   # :356 正交化
if not z_axis.any() or not x_axis.any():
    return parent_frame                                 # :358 退化时保持父朝向
return np.column_stack([x_axis, np.cross(z_axis, x_axis), z_axis])   # :359
```

也就是说：`z` 是指骨指向，`x` 是父关节留下的铰链轴，`y = z × x` 补完右手系。整手没有 IK、没有骨长约束、没有角度求解——26 个位置直接来自 21 个观测点及其算术组合。

---

## 4. 整手和局部关节分别减抖

`HandJointPositionFilter._filter_with_root`（`one_euro_filter.py:217`）分两步滤：

```python
raw_root = [sum(p[axis] for p in positions[i] for i in _PALM_JOINTS) / len(_PALM_JOINTS)
            for axis in range(3)]                       # :223-:225 掌部参考点（三轴各一个滤波器）
root = [f(raw_root[a], t_s) for a, f in enumerate(self._root)]   # :227
...
out.append((root[0] + fx(position[0] - raw_root[0], t_s),         # :248
            root[1] + fy(position[1] - raw_root[1], t_s),         # :249
            root[2] + fz(position[2] - raw_root[2], t_s)))        # :250
```

参考点是**掌部一组关节的均值**，先滤它；每个关节再滤「相对参考点的偏移」，最后相加还原。这样手的整体平移抖动和手指的局部抖动各用一套截止频率，位移不会被重复计入。`:228`–`:242` 还有一层速度自适应预测：参考点速度超过 `_ROOT_PREDICTION_SPEED` 才启动，位移按比例放大并夹到 `_ROOT_PREDICTION_MAX_DISTANCE`。

关键在下一步：`hand_tracking_service.py:218` 的 `_hand_state_from_detection` 把滤波结果**写回关节位置**，然后直接生成输出：

```python
joints = retarget(landmarks, stage_offset=stage_offset, handedness=handedness)   # :243
positions = [(jp.position.x, jp.position.y, jp.position.z) for jp in joints]     # :246
smoothed = filters[handedness].filter_positions(positions, t_s, prediction_s=...)  # :248/:254
for jp, (sx, sy, sz) in zip(joints, smoothed):                                   # :255
    jp.position.x, jp.position.y, jp.position.z = sx, sy, sz                     # :256
return _joints_to_hand_state(joints, is_active=True, image_points=image_points)  # :264
```

这个函数里没有 `lstsq`、没有 `solve_`、没有骨长或角度相关的名字（断言 `streaming-path-has-no-ik-solve` 对整个函数做否定扫描）。朝向沿用 retargeting 构造的那份，滤波只作用于位置。所以「减抖」不会破坏骨长一致性——因为从来就没有约束过骨长。

---

## 5. 捏合同时参考 2D 和 3D 间距

`pinch_strengths`（`pinch.py:130`）：

```python
scale = palm_length(joints)                                     # :151 3D 掌长（腕→中指近节）
thumb = joints[THUMB_TIP]                                       # :154
gaps  = [_distance(thumb, joints[tip]) / scale for tip in _FINGER_TIPS]   # :155 3D，按掌长归一化
image = _image_gaps(image_points)                               # :157 2D 间距
if image is not None:
    for seen, gap in zip(image, gaps):
        if math.isfinite(gap) and math.isfinite(seen):           # :163
            gaps = max(seen, gap - _SKELETON_SLACK_PALM_FRACTION)   # :162
        else:
            gaps = gap
values = [_strength(gap) for gap in gaps]                        # :168
return PinchStrengths(index=..., middle=..., ring=..., little=...)   # :169
```

`max(2D, 3D − 0.06)`（`_SKELETON_SLACK_PALM_FRACTION = 0.06`，`:67`）的含义是「两种测量都算捏住才算」：3D 少扣 0.06 掌长的容差，然后取两者中更保守（间距更大 → 强度更低）的那个。两个间距各自用自己的掌长归一化——2D 那条在 `_image_gaps` 里用图像平面的「腕→中指根」像素距离（`:123`），3D 那条用关节的腕→中指近节距离（`palm_length`，`:95`）。docstring 解释了为什么需要 2D：MediaPipe 的世界坐标在某些姿态下指尖间距不可靠——「面向相机的左手捏合会读成 0.29 掌长，和 C 形抬手一样」。

强度本身是线性斜坡（`:101`）：

```python
span = _APART_PALM_FRACTION - _TOUCHING_PALM_FRACTION       # 0.75 - 0.22 = 0.53  :105
return min(1.0, max(0.0, (_APART_PALM_FRACTION - gap) / span))   # :106
```

0.22 掌长以内算捏合（1.0），0.75 掌长以外算张开（0.0），中间线性。输出结构直接对齐 `XrHandTrackingAimStateFB`，runtime 不需要再缩放。

---

## 6. 头部平移为零

```python
# server/hand_tracking_service.py:184  _apply_head_pose
if head_detection is not None and head_detection.is_active:
    ox, oy, oz, ow = head_detection.orientation             # :196
    frame.head_pose.is_active = True
    frame.head_pose.confidence = head_detection.confidence
    frame.head_pose.pose.position.x = 0.0                   # :202
    frame.head_pose.pose.position.y = 0.0                   # :203
    frame.head_pose.pose.position.z = 0.0                   # :204
    mx, my, mz, mw = _mirror_head_orientation((ox, oy, oz, ow))   # :209
    frame.head_pose.pose.orientation.x = mx                 # :210
    frame.head_pose.pose.orientation.y = my                 # :211
    frame.head_pose.pose.orientation.z = mz                 # :212
    frame.head_pose.pose.orientation.w = mw                 # :213
else:
    frame.head_pose.is_active = False                       # :215
```

位置三个常数 `0.0`，紧接着三个 `STORE_ATTR` 到 `pose.position.x/y/z`。FaceLandmarker 明明算出了平移（`_decompose_transform` 拆的就是 4×4 里的平移列），这里选择丢掉，只保留旋转——`head_pose_detector.py` 的 docstring 说明了原因：运行时把摄像头的头姿当作「相对启用时刻的 delta」叠加到 WASD/mouse-look 上，单目摄像头估出来的绝对平移会漂，反而会把用户视角拽走。

四元数还做了一次镜像（`_mirror_head_orientation`，`:168`），让摄像头里的「向右转头」在 stage 空间也是 +X。

---

## 7. 方法学与局限

- **行号可信度**：`dis` 的 `starts_line` 直接来自代码对象的行表，就是 Meta 构包时源文件的真实行号。所有断言都绑在「某行上出现了某条指令」上，而不是绑在函数名或文本匹配上。
- **字节码 ≠ 源码**：`dis` 把控制流线性化了，`for`/`try` 会被拆成 `FOR_ITER`/异常表，等价形态只能表达语义，不能表达原文的排版与命名。文中所有 Python 片段都标注为「根据字节码写的等价形态」。
- **只覆盖 sidecar**：`SIMULATOR.dll` 是 C++，它怎么消费这些帧、怎么把头部 delta 叠进相机，不在本仓库范围。
- **版本敏感**：行号与指令只对 v207.1（`d59aa218…530f`）成立。换版本请重跑 `verify.py`，失败清单就是差异清单。

---

## 附：复现三行命令

```bash
python tools/extract.py --install "C:\Program Files\MetaXRSimulator\v207.1" --out build
python tools/verify.py  --models "C:\Program Files\MetaXRSimulator\v207.1\webcam_tracking_server\_internal"
python tools/snapshot.py --install "C:\Program Files\MetaXRSimulator\v207.1"
```
