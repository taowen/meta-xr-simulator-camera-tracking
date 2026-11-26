# NOTICE

## 这是什么

本仓库是对 **Meta XR Simulator v207.1 for Windows** 中随附的 `webcam_tracking_server.exe` 的独立技术分析。它不隶属于 Meta，也未获 Meta 授权或认可。分析对象是用户在自己机器上安装的软件副本。

## 不包含什么

- **不含** `webcam_tracking_server.exe`、`SIMULATOR.dll` 或任何其他 Meta 二进制文件。
- **不含** `hand_landmarker.task` / `face_landmarker.task` 模型文件。

`tools/extract.py` 只是从**你本地的安装目录**读取你自己的副本，解包到 `build/`（已 gitignore）。要复现，你必须自己持有该授权副本。

## `src/` 里有什么

`src/` 是 `server` 包的**机器反编译结果**，由 [tools/decompile.py](tools/decompile.py) 从解包出的字节码生成（流程与已知缺陷见 [src/README.md](src/README.md)）。它不是 Meta 的原始源码，也不保证语义等价：15 个模块里 5 个能通过 Python 语法解析，其余在推导式、`with`、`try` 嵌套等处有明确标注的残缺。

保留它的理由是**可核查性**：文中每条结论都要能落回具体代码，而读函数体比读指令表更直接。同样内容的指令级证据在 `evidence/`，两者可以互相校验。

如果这种尺度不合适，见文末的移除请求。

## 包含什么，以及为什么

`evidence/` 下是**函数级的字节码节选**，仅覆盖文中每条结论所需的那些行，用途是评论与说明（commentary / criticism）——一个可核查的断言必须能追溯到证据，否则它只是猜测。`article/` 里的 Python 片段是**根据字节码转写的等价形态**，不是 Meta 的原文：变量名、排版、注释均为转写产物。

`evidence/inventory/pyz-modules.txt` 是模块名清单，`evidence/strings/` 是字符串常量，属事实性元数据。

这些节选**不能**用来重建可用实现：`dis` 输出已被线性化，缺少源码结构，也没有任何模块能从中拼装出来。

## 归属

- `webcam_tracking_server.exe`、`SIMULATOR.dll`、`.task` 模型及其中的代码与文本，版权归 **Meta Platforms, Inc.** 所有。
- MediaPipe 模型来自 Google，见 [MediaPipe Models](https://developers.google.com/mediapipe/solutions) 的许可条款；本仓库只比对哈希，不分发文件。
- `tools/` 下的脚本是本仓库作者的原创代码，按 [MIT](LICENSE) 授权。
- 解包使用的 [pyinstxtractor](https://github.com/extremecoders-re/pyinstxtractor) 不在本仓库内，运行时按固定 commit 下载，遵循其自身许可。

## 许可边界

`LICENSE` 中的 MIT 授权**仅适用于 `tools/` 与本仓库作者的原创文字**。`evidence/` 中源自 Meta 二进制的节选不在 MIT 覆盖范围内，其使用受限于上述评论/说明用途。

## 移除请求

如果你是权利持有人并认为某段节选超出了合理引用范围，请在该仓库开 issue 注明文件与行，我会移除或改写对应内容。
