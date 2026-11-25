#!/bin/bash
# Prepare the local iCourse app. Run from any directory with bash.
set -euo pipefail
umask 077
cd "$(dirname "$0")/.."
root=$PWD
bundle=sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17
release=https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models
mode=${1:-all}
usage() { echo '用法：bash scripts/setup_local.sh [--deps-only | --models-only]'; }
case "$mode" in
    --help|-h) usage; exit 0 ;;
    all|--deps-only|--models-only) ;;
    *) usage >&2; exit 2 ;;
esac
[[ $# -le 1 ]] || { usage >&2; exit 2; }

prepare_python() {
    if ! command -v ffmpeg >/dev/null; then
        echo '找不到 ffmpeg。请先安装并确认终端可以运行 ffmpeg（Homebrew 用户可运行 brew install ffmpeg）。' >&2
        exit 1
    fi

    if [[ ! -x .venv/bin/python ]]; then
        if [[ -n "${PYTHON_BIN:-}" ]]; then
            "$PYTHON_BIN" -m venv .venv
        elif command -v python3.12 >/dev/null; then
            python3.12 -m venv .venv
        elif command -v python3.11 >/dev/null; then
            python3.11 -m venv .venv
        elif command -v uv >/dev/null; then
            UV_PYTHON_INSTALL_DIR="$root/.local-python" uv venv --python 3.12 --seed .venv
        else
            echo '请准备 Python 3.12 或 3.11，或安装 uv 后重试。也可用 PYTHON_BIN 指定解释器路径。' >&2
            exit 1
        fi
    fi

    if ! .venv/bin/python -m pip --version >/dev/null 2>&1; then
        .venv/bin/python -m ensurepip
    fi
    .venv/bin/python -m pip install -r requirements.txt
}

download() {
    curl --fail --location --show-error --retry 3 --output "$2" "$1"
}

# Stage downloads before publishing them so a failed download cannot look ready.
prepare_models() {
    work=$(mktemp -d "$root/.icourse-setup.XXXXXX")
    trap 'rm -rf "$work"' EXIT
    if [[ ! -s "$bundle/tokens.txt" || ( ! -s "$bundle/model.int8.onnx" && ! -s "$bundle/model.onnx" ) ]]; then
        archive="$root/$bundle.tar.bz2"
        if [[ ! -s "$archive" ]]; then
            archive="$work/$bundle.tar.bz2"
            download "$release/$bundle.tar.bz2" "$archive"
        fi
        tar -xjf "$archive" -C "$work" "$bundle/tokens.txt" "$bundle/model.int8.onnx"
        [[ -s "$work/$bundle/tokens.txt" && -s "$work/$bundle/model.int8.onnx" ]]
        mkdir -p "$bundle"
        mv "$work/$bundle/tokens.txt" "$bundle/tokens.txt"
        mv "$work/$bundle/model.int8.onnx" "$bundle/model.int8.onnx"
    fi
    if [[ ! -s silero_vad.onnx ]]; then
        download "$release/silero_vad.onnx" "$work/silero_vad.onnx"
        [[ -s "$work/silero_vad.onnx" ]]
        mv "$work/silero_vad.onnx" silero_vad.onnx
    fi
}

if [[ "$mode" != --models-only ]]; then prepare_python; fi
if [[ "$mode" != --deps-only ]]; then prepare_models; fi
if [[ ! -f .env ]]; then
    cp -n .env.example .env || [[ -f .env ]]
fi
case "$mode" in
    all) echo '准备完成。填写项目中的 .env 后，运行 .venv/bin/python local_app.py 打开本地课程库。' ;;
    --deps-only) echo 'Python 环境已准备。' ;;
    --models-only) echo '语音模型已准备。' ;;
esac
