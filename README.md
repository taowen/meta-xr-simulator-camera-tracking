# 普通摄像头如何模拟 OpenXR 手部追踪

以 **Meta XR Simulator Windows v207.1** 为例。一台 RGB 摄像头就能让 OpenXR 应用收到双手关节和头部转向。摄像头提供图像，MediaPipe 预测局部手形，模拟器负责估算整手位置、补关节和减少抖动。

本文依据 Meta 的公开文档，以及对 v207.1 安装包中摄像头服务的静态分析。公开文档确认 21 点到 26 关节的转换、掌宽估深和头部旋转。字节码进一步显示，**全手投影拟合优先，掌宽估深用于回退**。本文没有测量摄像头追踪的精度和延迟。 [Meta 摄像头追踪文档](https://developers.meta.com/vr/documentation/unity/xrsim-camera-tracking/)

## 一帧图像里有什么

摄像头服务调用 MediaPipe 0.10.33 的 `HandLandmarker` 和 `FaceLandmarker`，两者都使用 `LIVE_STREAM`。随包的 `hand_landmarker.task` 和 `face_landmarker.task` 分别与 Google 发布的 float16/1 模型逐字节一致。手模型报告每只手的 21 个图像点、21 个以手为中心的局部 3D 点和左右手分类。后续算法同时使用两组点。 [Google Hand Landmarker 输出说明](https://ai.google.dev/edge/mediapipe/solutions/vision/hand_landmarker/python#handle_and_display_results)

## 1. 用 21 对关键点估计整只手的位置

令 `P[i]` 为局部 3D 点，`uv[i]` 为对应的归一化图像点。用约 68° 的默认水平视场角和图像宽高比生成针孔相机的视线坐标 `(x, y)`。给定旋转 `R`，将 `(X, Y, Z) = R × P[i]` 投影到这条视线。每个点给未知平移 `t` 提供两条方程。

```text
tx - x * tz = x * Z - X
ty - y * tz = y * Z - Y
```

```text
place_hand(P, uv, image_size):
    rays = camera_rays(uv, image_size, horizontal_fov=68°)
    R = identity
    t = fit_translation(R * P, rays)

    repeat 2 times:
        if t is invalid: break
        centroid = R * mean(P) + t
        R = shortest_rotation(optical_axis, direction(centroid))
        t = fit_translation(R * P, rays)

    if t is valid:
        wrist_stage = camera_to_stage(R * P[WRIST] + t)
        return lateral_reach_gain(wrist_stage), R * P

    span = distance_2d(uv[INDEX_MCP], uv[LITTLE_MCP])
    dz = clamp(-depth_range * (span / reference_span - 1),
               -depth_range, +depth_range)
    return map_image_wrist_to_stage(uv[WRIST], dz), P
```

平移求解使用最小二乘，随后按初步估计的深度重新加权，并检查解是否合理。`R` 只处理手偏离画面中心时的视线旋转。限制旋转自由度可以减少平面手形拟合时的朝向歧义。回退时，手掌在画面里变宽，虚拟手腕就往前移动。

## 2. 把 21 点转换成 26 个 OpenXR 关节

```text
retarget(P, wrist_stage, handedness):
    joints = copy_21_positions_into_openxr_slots(P)
    joints[PALM] = midpoint(P[WRIST], P[MIDDLE_MCP])
    for finger in [INDEX, MIDDLE, RING, LITTLE]:
        joints[finger.METACARPAL] = midpoint(P[WRIST], P[finger.MCP])

    joints = remap_axes_and_place_wrist(joints, wrist_stage)
    palm_normal = orient_for_hand_side(normal_from_palm(joints), handedness)
    set_wrist_and_palm_orientations(joints, palm_normal)
    for finger in all_fingers:
        use_palm_or_thumb_reference_for_first_joint(finger)
        for joint in remaining_joints(finger):
            joint.rotation = frame_from(
                minus_z = direction_toward_fingertip(joint),
                flexion_axis = previous_joint.local_x
            )
    return joints_with_default_radii
```

沿手指继承屈伸轴，可以减少握拳时的 180° 滚转。这个运算只生成朝向。关节中心和指骨长度仍随 MediaPipe 的预测变化，摄像头服务在这段路径中没有做固定骨长、关节角度限制或 IK。

## 3. 保持左右手身份、减抖和计算捏合

```text
on_hand_result(result, timestamp):
    tracks = associate_by_wrist_position_velocity_and_handedness(result)
    for track in tracks:
        if briefly_missing(track):
            hold_recent_pose(track)
            continue
        if tracking_expired(track):
            mark_inactive_and_reset_filters(track)
            continue

        wrist, shape = place_hand(track.world_points, track.image_points,
                                  result.image_size)
        joints = retarget(shape, wrist, track.handedness)
        palm_root = average_of_selected_palm_joints(joints)
        root = one_euro_root(palm_root, timestamp)
        for joint in joints:
            local = joint.position - palm_root
            joint.position = root + one_euro_local(joint, local, timestamp)

        for finger in [INDEX, MIDDLE, RING, LITTLE]:
            gap_2d = image_thumb_to_tip(finger) / image_palm_length
            gap_3d = spatial_thumb_to_tip(finger) / spatial_palm_length
            gap = max(gap_2d, gap_3d - 0.06)
            pinch[finger] = clamp((0.75 - gap) / (0.75 - 0.22), 0, 1)

        publish_hand(joints, pinch)
```

此处省略了短时预测和测量无效的分支。捏合公式展示 2D、3D 间距都有效时的路径。两根手指可能在图像里重合，结合 3D 间距可以减少这类误判。3D 间距扣除 `0.06` 个掌长，是对模型可能把相触的指尖估得偏远留出的余量。

## 4. 头部和 OpenXR 输出

```text
on_face_result(face):
    if face_transform_is_valid(face):
        orientation = quaternion_from(face.transform.rotation)
        orientation = slerp(previous_orientation, orientation)
        publish_head(remap_orientation(orientation), translation=(0, 0, 0))
    else:
        mark_camera_head_inactive()
```

摄像头服务通过 gRPC 发送追踪状态。OpenXR 应用用 `xrLocateHandJointsEXT` 读取手部关节，头部转向则进入常规视图和空间位姿。人在摄像头前探身或蹲下时，虚拟头显位置仍由模拟器的移动控制决定。 [Meta OpenXR 接入说明](https://developers.meta.com/vr/documentation/unity/xrsim-camera-tracking/#what-your-application-receives)

## 为什么还需要深度参考

这套算法解出了整手的平移，但 `Z(食指尖) − Z(掌根)` 相减时，共同平移会抵消。这个差值主要来自 MediaPipe 的局部手形和视线旋转校正。平滑可以减少跳动，也会保留稳定的估计偏差。要检验它是否把指尖的前后关系估反，可以用一台能同步输出 RGB 和深度的手机，保存同一帧的数据，再比较两个点的深度差。

## 复核依据

用 CPython 3.12 运行 [检查脚本](inspect_camera_tracking.py)。它读取本机安装包中的 PyInstaller `PYZ-00.pyz`，打印模型哈希、模块名、原始行号和指定函数的字节码。它不执行包内模块。仓库提供该检查脚本和算法伪代码。

```powershell
py -3.12 .\inspect_camera_tracking.py
py -3.12 .\inspect_camera_tracking.py --disassemble positioning:estimate_hand_placement
py -3.12 .\inspect_camera_tracking.py --disassemble positioning:_solve_translation
py -3.12 .\inspect_camera_tracking.py --verify-public-models
```

将安装目录作为第一个参数可检查其他位置的同一版本。默认检查仅使用本机文件。`--verify-public-models` 额外下载 Google 的公开模型到内存进行哈希比对。

除交互式检查外，仓库把下表的每一行拆成了可执行断言。[claims/claims.json](claims/claims.json) 为每条结论记录「哪个文件的哪个函数、哪几行、必须出现哪条指令」，[tools/verify.py](tools/verify.py) 一次跑完并按需比对模型哈希，输出 `19/19 claims verified`；命中的指令原文快照在 [evidence/disassembly/](evidence/disassembly/)，逐条对照见 [article/bytecode-notes.md](article/bytecode-notes.md)。

```powershell
py -3.12 .\tools\extract.py --install "C:\Program Files\MetaXRSimulator\v207.1" --out build
py -3.12 .\tools\verify.py --models "C:\Program Files\MetaXRSimulator\v207.1\webcam_tracking_server\_internal"
```

两套脚本都只用标准库：`marshal` 读代码对象，`dis` 反汇编，全程不导入包内模块。`evidence/` 是快照、`claims/` 是断言，都由 [tools/snapshot.py](tools/snapshot.py) 从本机副本重新生成；`build/` 已忽略。行号与指令只对本次样本成立，换版本重跑，失败清单就是差异清单。

| 结论 | 安装包内的函数 |
|---|---|
| MediaPipe 手部和头部模型 | `mediapipe_detector.py:167`、`head_pose_detector.py:268` |
| 21 点拟合优先，掌宽回退 | `positioning.py:206`、`:283`、`:331`，回退深度位于 `:148` |
| 补关节和沿手指定朝向 | `retargeting.py:281`、`:346`、`:362` |
| 左右手跨帧关联，整手与局部关节滤波 | `hand_identity_tracker.py:221`、`one_euro_filter.py:217`、`hand_tracking_service.py:218` |
| 2D 与 3D 捏合间距 | `pinch.py:101`、`:130` |
| 头部平移为零 | `hand_tracking_service.py:184` |

本次检查的 `webcam_tracking_server.exe` SHA-256 为 `d59aa21863beeaac5e2a70277aa5f40347dced6ad2625dab3fc63a55e529530f`。原始行号由打包字节码记录。伪代码省略了线程、时序和异常处理，适用于这个 Windows 样本。

仓库不含 Meta 的二进制与模型文件。反编译结果在 [src/](src/)：`server` 包的 15 个模块，由 [tools/decompile.py](tools/decompile.py) 从字节码生成——生成前要先修掉一处厂商改动（`IMPORT_NAME` 被换成了 `CACHE`），再补上 Decompyle++ 缺失的 3.12 语义，补丁在 [tools/pycdc-3.12.patch](tools/pycdc-3.12.patch)。**这不是原始源码**：5 个模块能通过语法解析，其余在推导式（PEP 709）、`with`、`try` 嵌套处有标注的残缺，每个文件的 docstring 与 [src/README.md](src/README.md) 都写明了状态。逐行证据仍在 [evidence/](evidence/)，[article/](article/) 是按字节码转写的等价形态。来源与授权说明见 [NOTICE.md](NOTICE.md)。
