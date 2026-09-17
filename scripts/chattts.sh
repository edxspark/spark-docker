#!/usr/bin/env bash
# 启动本地 ChatTTS 语音合成服务（供「系统配置 → 语音合成 → ChatTTS」调用）。
#
#   ./scripts/chattts.sh            # 已就绪则直接启动服务
#   ./scripts/chattts.sh --install  # 首次安装：建 venv、装依赖、拉模型权重
#
# 端口固定 9966（与系统配置里的默认地址一致），可用 CHATTTS_PORT 覆盖。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=./_common.sh
. "$ROOT/scripts/_common.sh"

CHATTTS_DIR="${CHATTTS_DIR:-$HOME/.cache/spark-chattts}"
VENV_DIR="$CHATTTS_DIR/venv"
MODEL_DIR="$CHATTTS_DIR/models/ChatTTS"
PORT="${CHATTTS_PORT:-9966}"
# 国内直连 huggingface.co 常失败，默认走镜像
export HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"
# Xet 传输在国内镜像上会 401，强制走普通 HTTP 下载
export HF_HUB_DISABLE_XET="${HF_HUB_DISABLE_XET:-1}"
PIP_INDEX="${PIP_INDEX:-https://pypi.tuna.tsinghua.edu.cn/simple}"

find_python() {
  local candidate
  # 候选顺序：常见命令名 → uv 托管版本 → pyenv → Homebrew 前缀。
  # torch 目前只对 3.10~3.13 提供稳定轮子；若命中 python3（可能是 brew 装的 3.14），
  # pip 会「装成功」但实际没有 torch 轮子，装完才 import 失败——所以按版本白名单挑。
  local candidates=(
    python3.12 python3.11 python3.13 python3.10 python3
    "$HOME/.local/share/uv/python"/*/bin/python3.12
    "$HOME/.local/share/uv/python"/*/bin/python3.11
    "$HOME/.local/share/uv/python"/*/bin/python3.13
    "$HOME/.pyenv/versions"/3.12*/bin/python
    "$HOME/.pyenv/versions"/3.11*/bin/python
    /opt/homebrew/opt/python@3.12/bin/python3.12
    /opt/homebrew/opt/python@3.11/bin/python3.11
    /usr/local/opt/python@3.12/bin/python3.12
  )
  for candidate in "${candidates[@]}"; do
    [ -x "$candidate" ] || command -v "$candidate" >/dev/null 2>&1 || continue
    if "$candidate" -c 'import sys; raise SystemExit(0 if (3, 10) <= sys.version_info[:2] < (3, 14) else 1)' 2>/dev/null; then
      echo "$candidate"
      return 0
    fi
  done
  return 1
}

verify_env() {
  # 装完必须验证：torch 缺轮子时 pip 依旧返回 0，只有真正 import 才能发现
  if ! "$VENV_DIR/bin/python" -c 'import torch, ChatTTS, flask, numpy; print("torch", torch.__version__)' 2>/tmp/chattts-verify.err; then
    echo "✗ 运行环境校验失败（ChatTTS 依赖导不进来）："
    tail -3 /tmp/chattts-verify.err
    echo "  常见原因是 Python 版本过新、torch 没有对应轮子。"
    echo "  处理：brew install python@3.12，删除 $VENV_DIR 后重新执行 --install"
    exit 1
  fi
}

install_all() {
  mkdir -p "$CHATTTS_DIR"
  local py
  if ! py="$(find_python)"; then
    echo "✗ 需要 Python 3.10~3.13（torch 暂无更新版本的稳定轮子）。安装：brew install python@3.12"
    exit 1
  fi
  echo "==> 使用 $("$py" -V 2>&1) 创建虚拟环境 $VENV_DIR"
  # 版本不对时重建，避免沿用旧 venv 里装不上的依赖
  if [ -x "$VENV_DIR/bin/python" ] && ! "$VENV_DIR/bin/python" -c 'import torch' >/dev/null 2>&1; then
    echo "    检测到已有环境缺少 torch，重建虚拟环境"
    rm -rf "$VENV_DIR"
  fi
  [ -x "$VENV_DIR/bin/python" ] || "$py" -m venv "$VENV_DIR"
  "$VENV_DIR/bin/python" -m pip install -q --upgrade pip -i "$PIP_INDEX"
  echo "==> 安装依赖（torch / ChatTTS / flask，约 1GB，视网速需要几分钟）"
  "$VENV_DIR/bin/python" -m pip install -i "$PIP_INDEX" torch torchaudio ChatTTS flask requests
  verify_env
  echo "==> 下载模型权重到 $MODEL_DIR（约 2.2GB）"
  "$VENV_DIR/bin/python" - <<PY
from huggingface_hub import snapshot_download
path = snapshot_download("2Noise/ChatTTS",
                         allow_patterns=["*.pt", "*.yaml", "*.json", "asset/*"],
                         local_dir="$MODEL_DIR")
print("模型就绪:", path)
PY
}

write_server() {
  cat > "$CHATTTS_DIR/server.py" <<'PY'
"""最小 ChatTTS HTTP 服务：接口与官方 examples/web/webui/app.py 的 /tts 完全一致。

只依赖 flask + ChatTTS，不需要 gradio；参数名、返回体与上游一致，
因此 Spark 里的 ChatTTS 提供者可以直接对接。
"""
import io
import os
import time
import wave

import numpy as np
import torch
import ChatTTS
from flask import Flask, request, jsonify, send_file

MODEL_DIR = os.environ.get("CHATTTS_MODEL_DIR", "")
DEFAULT_SEED = int(os.environ.get("CHATTTS_VOICE_SEED", "2222"))

chat = ChatTTS.Chat()
print("加载 ChatTTS 模型…", flush=True)
started = time.time()
if not chat.load(compile=False, source="local", custom_path=MODEL_DIR):
    raise SystemExit("模型加载失败，请用 --install 重新拉取权重")
print(f"模型就绪，耗时 {time.time() - started:.1f}s", flush=True)

# 预采样一个说话人作为默认音色（与服务端默认行为一致）
torch.manual_seed(DEFAULT_SEED)
DEFAULT_SPK = chat.sample_random_speaker()
print("默认说话人已就绪（seed=%d）" % DEFAULT_SEED, flush=True)

app = Flask(__name__)


@app.route("/")
def index():
    return "ChatTTS api is running. POST /tts with form field text."


@app.route("/tts", methods=["GET", "POST"])
def tts():
    text = (request.args.get("text") or request.form.get("text") or "").strip()
    if not text:
        return jsonify({"code": 400, "msg": "text 不能为空"}), 400

    prompt = request.args.get("prompt") or request.form.get("prompt") or ""
    custom_voice = int(request.args.get("custom_voice") or request.form.get("custom_voice") or 0)
    voice = str(custom_voice) if custom_voice > 0 else (
        request.args.get("voice") or request.form.get("voice") or str(DEFAULT_SEED)
    )
    temperature = float(request.args.get("temperature") or request.form.get("temperature") or 0.3)
    top_p = float(request.args.get("top_p") or request.form.get("top_p") or 0.7)
    top_k = int(request.args.get("top_k") or request.form.get("top_k") or 20)
    speed = int(request.args.get("speed") or request.form.get("speed") or 5)

    # 说话人：数字即种子（上游语义），固定种子保证同一条任务音色一致
    if voice.isdigit() and int(voice) != DEFAULT_SEED:
        torch.manual_seed(int(voice))
        spk = chat.sample_random_speaker()
    else:
        spk = DEFAULT_SPK

    if "[speed_" not in prompt:
        prompt = f"{prompt}[speed_{speed}]" if prompt else f"[speed_{speed}]"

    started = time.time()
    wavs = chat.infer(
        [text],
        params_infer_code=ChatTTS.Chat.InferCodeParams(
            spk_emb=spk, prompt=prompt, temperature=temperature, top_P=top_p, top_K=top_k
        ),
    )
    samples = np.asarray(wavs[0], dtype=np.float32).reshape(-1)
    pcm = (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(24000)
        handle.writeframes(pcm)
    buffer.seek(0)
    print(f"[tts] {len(text)} 字 / {time.time() - started:.1f}s / speed={speed}", flush=True)
    return send_file(buffer, mimetype="audio/x-wav")


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "9966")), threaded=True)
PY
}

main() {
  if [ "${1:-}" = "--install" ]; then
    install_all
    write_server
    echo "✓ 安装完成：$CHATTTS_DIR"
  fi

  if [ ! -x "$VENV_DIR/bin/python" ]; then
    echo "✗ 未安装 ChatTTS 运行环境。先执行：./scripts/chattts.sh --install"
    exit 1
  fi
  [ -f "$CHATTTS_DIR/server.py" ] || write_server

  if port_in_use "$PORT"; then
    echo "✗ 端口 $PORT 已被占用：可能服务已在运行。"
    echo "  验证：curl -s http://127.0.0.1:$PORT/"
    exit 1
  fi

  echo "==> 启动 ChatTTS 服务 http://127.0.0.1:$PORT （Ctrl+C 停止）"
  echo "    系统配置 → 语音合成 → 服务地址填 http://127.0.0.1:$PORT"
  echo
  CHATTTS_MODEL_DIR="$MODEL_DIR" PORT="$PORT" \
    exec "$VENV_DIR/bin/python" "$CHATTTS_DIR/server.py"
}

main "$@"
