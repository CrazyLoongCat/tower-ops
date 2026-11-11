#!/usr/bin/env bash
# =============================================================================
# 生成 tower-ops 最小源码包 tower-ops-min-src.tar.gz（DEC-112）
# =============================================================================
# 用法（任意目录执行均可；脚本自己定位 tower-ops）：
#     bash scripts/make_min_src.sh                 # 默认：含 vendor（可在构建机直接打包）
#     NO_VENDOR=1 bash scripts/make_min_src.sh     # 不含 vendor（只是源码，构建机需另配 SDK）
#     OUT=/tmp/tower-ops-min-src.tar.gz bash scripts/make_min_src.sh
#     VERIFY=1 bash scripts/make_min_src.sh        # 额外解包到临时目录跑 check_packaging --linux
#
# 包含：main.py、pyproject.toml、conftest.py、README.md、application/、configs/、
#       packaging/、scripts/、edge_service/、（默认）vendor/
# 排除：runtime/（记录库与帧，可能几百 MB）、tests/、__pycache__、*.pyc、*.bak、
#       build*/dist*、.pytest_cache、.git
#
# 产物：默认 ../tower-ops-min-src.tar.gz 与同名 .sha256；
#       压缩包内顶层目录为 tower-ops/，解包即得到完整工程目录（tar -xzf 即可构建）。
# =============================================================================
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"                                  # .../tower-ops
NAME="$(basename "$ROOT")"                     # tower-ops（目录被改名时随实际名字）
PARENT="$(dirname "$ROOT")"
OUT="${OUT:-$PARENT/tower-ops-min-src.tar.gz}"
NO_VENDOR="${NO_VENDOR:-0}"
VERIFY="${VERIFY:-0}"

fail() { echo "错误：$*" >&2; exit 1; }

echo "== 源目录: $ROOT =="
for f in main.py pyproject.toml application configs packaging \
         scripts/check_packaging.py scripts/build_linux.sh scripts/make_min_src.sh; do
  [[ -e "$f" ]] || fail "缺少 $f（请在完整 tower-ops 目录里运行）"
done
if [[ "$NO_VENDOR" == "1" ]]; then
  echo "  模式: 不含 vendor（构建机需另行提供 SDK/模型，否则 check_packaging --linux 会报缺件）"
else
  for d in vendor/robot/Robotic_Arm vendor/camera/scepter_sdk vendor/circle38/detect_circle; do
    [[ -d "$d" ]] || echo "  提示：未找到 $d —— 对应能力在包里不可用"
  done
  echo "  模式: 含 vendor（可直接在构建机 pyinstaller 打包）"
fi

echo "== 打包 =="
rm -f "$OUT" "$OUT.sha256"
EXCLUDES=(
  --exclude="$NAME/runtime"
  --exclude="$NAME/tests"
  --exclude="$NAME/.pytest_cache"
  --exclude="$NAME/.git"
  --exclude="$NAME/.venv"
  --exclude="$NAME/build"
  --exclude="$NAME/build_host"
  --exclude="$NAME/build_linux"
  --exclude="$NAME/build_linux_host"
  --exclude="$NAME/dist"
  --exclude="$NAME/dist_host"
  --exclude="$NAME/dist_linux"
  --exclude="*/__pycache__"
  --exclude="*.pyc"
  --exclude="*.bak"
)
if [[ "$NO_VENDOR" == "1" ]]; then
  EXCLUDES+=(--exclude="$NAME/vendor")
fi
tar -czf "$OUT" "${EXCLUDES[@]}" -C "$PARENT" "$NAME"

echo "== 校验内容 =="
LIST="$(mktemp)"
tar -tzf "$OUT" > "$LIST"
for rel in main.py pyproject.toml application/presentation/app.py \
           configs/devices/devices.yaml packaging/tower-ops.spec \
           packaging/circle38_host.spec packaging/tower-ops-linux.spec \
           scripts/check_packaging.py scripts/build_linux.sh scripts/build_windows.ps1; do
  grep -qx "$NAME/$rel" "$LIST" || { rm -f "$LIST"; fail "压缩包缺少 $rel"; }
done
if grep -q "^$NAME/runtime/" "$LIST"; then rm -f "$LIST"; fail "压缩包混入了 runtime/"; fi
if grep -q "^$NAME/tests/" "$LIST"; then rm -f "$LIST"; fail "压缩包混入了 tests/"; fi
if grep -q '\.pyc$' "$LIST"; then rm -f "$LIST"; fail "压缩包混入了 .pyc"; fi
n_files=$(grep -cv '/$' "$LIST" || true)
if [[ "$NO_VENDOR" == "1" ]]; then
  if grep -q "^$NAME/vendor/" "$LIST"; then rm -f "$LIST"; fail "NO_VENDOR=1 但包里仍有 vendor/"; fi
fi
rm -f "$LIST"

SIZE="$(du -h "$OUT" | awk '{print $1}')"
SHA="$(sha256sum "$OUT" | awk '{print $1}')"
printf '%s  %s\n' "$SHA" "$(basename "$OUT")" > "$OUT.sha256"
echo "  文件数: $n_files"
echo "  大小:   $SIZE"
echo "  sha256: $SHA"
echo "  已写:   $OUT.sha256"

if [[ "$VERIFY" == "1" ]]; then
  echo "== 解包验证（临时目录 + check_packaging --linux）=="
  TMP="$(mktemp -d)"
  trap 'rm -rf "$TMP"' EXIT
  tar -xzf "$OUT" -C "$TMP"
  if cmp -s "$TMP/$NAME/main.py" main.py; then
    echo "  解包 main.py 与源文件一致"
  else
    fail "解包内容与源不一致"
  fi
  if command -v python3 >/dev/null 2>&1; then
    ( cd "$TMP/$NAME" && python3 scripts/check_packaging.py --linux )
  else
    echo "  提示：本机没有 python3，跳过 check_packaging --linux"
  fi
fi

echo "== 完成 =="
echo "  压缩包: $OUT"
echo "  使用:   tar -xzf $(basename "$OUT") && cd $NAME && bash scripts/build_linux.sh"
