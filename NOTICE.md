# NOTICE

本文件说明本仓库的授权范围与第三方权利归属。**它不是许可证的组成部分**，
许可证正文见 `LICENSE`（MIT）。

## 1. 本仓库的授权范围

`LICENSE` 中的 MIT 许可**仅覆盖**本仓库原有的文档、脚本与 Skill 结构与组织方式，
即：`SKILL.md`、`README.md`、`README_EN.md`、`ROADMAP.md`、`NOTICE.md`、
`references/`、`scripts/`、`tests/`、`docs/` 下的原创内容。

## 2. 不在授权范围内的部分

下列内容随本仓库被**引用或调用**，但**不属于**本仓库，也未在此授予任何权利：

- 第三方模型权重（例如 OpenAI Whisper `large-v3` / `large-v3-turbo`、
  SenseVoiceSmall、Paraformer）——权重许可以各自模型卡为准。
- 第三方推理引擎与运行时（例如 faster-whisper、CTranslate2、FunASR、
  llama.cpp / GGUF 运行时、ONNX Runtime）。
- 上述项目自带的二进制、示例、测试数据与文档。

**代码许可与模型权重许可是两件事**，必须分别核对。逐项目的维护方、地址、
许可证与用途，见 `references/09-attribution.md`；商用前请以当次官方仓库与模型卡为准
（归属会迁移，核实日期见该文件）。

## 3. 本仓库不包含的内容

- 不包含任何模型权重文件（`*.bin` / `*.gguf` / `*.onnx` 等）。
- 不包含任何音频素材或转写产物。
- 不包含任何用户数据、账号或凭证。

## 4. 引用与署名

若转载或再分发本仓库内容，请保留 `LICENSE` 与 `references/09-attribution.md`，
并保留其中对第三方项目的署名与许可说明。
