#!/usr/bin/env bash
# Build official d1_sdk helpers into repo bin/ (NX or Linux with Unitree SDK2).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
BIN_DIR="${REPO_ROOT}/bin"

SDK_SRC=""
for cand in \
  "${REPO_ROOT}/D1 550 Workspace/d1_sdk/d1_sdk" \
  "${REPO_ROOT}/D1_550_Workspace/d1_sdk/d1_sdk"; do
  if [ -f "${cand}/CMakeLists.txt" ]; then
    SDK_SRC="${cand}"
    break
  fi
done
BUILD_DIR="${SDK_SRC}/build"

if [ -z "${SDK_SRC}" ] || [ ! -f "${SDK_SRC}/CMakeLists.txt" ]; then
  echo "ERROR: d1_sdk not found under ${REPO_ROOT} (D1 550 Workspace or D1_550_Workspace)" >&2
  exit 2
fi

UNITREE_SDK2="${UNITREE_SDK2:-/usr/local}"
UNITREE_LIB="${UNITREE_LIB:-${UNITREE_SDK2}/lib}"
if [ ! -e "${UNITREE_LIB}/libunitree_sdk2.a" ] && [ ! -e "${UNITREE_LIB}/libunitree_sdk2.so" ]; then
  for d in "${UNITREE_SDK2}/lib64" /usr/local/lib; do
    if [ -e "${d}/libunitree_sdk2.a" ] || [ -e "${d}/libunitree_sdk2.so" ] || [ -e "${d}/libunitree_go2_sdk.a" ]; then
      UNITREE_LIB="$d"
      break
    fi
  done
fi

mkdir -p "${BUILD_DIR}" "${BIN_DIR}"
cd "${BUILD_DIR}"

CMAKE_EXTRA=()
if [ -e "${UNITREE_LIB}/libunitree_go2_sdk.a" ] || [ -e "${UNITREE_LIB}/libunitree_go2_sdk.so" ]; then
  CMAKE_EXTRA+=(-DUNITREE_SDK_LIB=unitree_go2_sdk)
fi

cmake .. "${CMAKE_EXTRA[@]}"
make -j"$(nproc 2>/dev/null || echo 2)" d1_command_stdin d1_feedback_snapshot get_arm_joint_angle

install -m 755 d1_command_stdin "${BIN_DIR}/d1_sdk_command"
install -m 755 d1_feedback_snapshot "${BIN_DIR}/d1_sdk_feedback"
install -m 755 get_arm_joint_angle "${BIN_DIR}/d1_sdk_get_angles"

echo "OK: ${BIN_DIR}/d1_sdk_command ${BIN_DIR}/d1_sdk_feedback ${BIN_DIR}/d1_sdk_get_angles"
