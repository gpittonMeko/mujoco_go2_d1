#!/usr/bin/env bash
# Compila gli helper DDS braccio D1 nella cartella bin/ (Jetson / Linux con Unitree SDK2).
# Sorgenti in ``D1 550 Workspace/OLD/scripts`` + ``OLD/msg``; su NX il deploy li mette in ``scripts/`` + ``msg/`` flat.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WRK_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
if [ -f "${WRK_ROOT}/diagnostics_dashboard.py" ]; then
  REPO_ROOT="${WRK_ROOT}"
elif [ -f "${WRK_ROOT}/../../diagnostics_dashboard.py" ]; then
  REPO_ROOT="$(cd "${WRK_ROOT}/../.." && pwd)"
else
  REPO_ROOT="${WRK_ROOT}"
fi
MSG_ROOT="${WRK_ROOT}"
mkdir -p "${REPO_ROOT}/bin"
rm -f "${REPO_ROOT}/bin/d1_arm_command" "${REPO_ROOT}/bin/d1_arm_feedback_helper"

UNITREE_SDK2="${UNITREE_SDK2:-/usr/local}"
UNITREE_INCLUDE="${UNITREE_INCLUDE:-${UNITREE_SDK2}/include}"
UNITREE_LIB="${UNITREE_LIB:-${UNITREE_SDK2}/lib}"
ICEORYX_CPP_INC="${ICEORYX_CPP_INC:-/usr/local/include/iceoryx/v2.0.2}"

if [ ! -e "${UNITREE_LIB}/libunitree_sdk2.so" ] && [ ! -e "${UNITREE_LIB}/libunitree_sdk2.a" ]; then
  for d in "${UNITREE_SDK2}/lib64" /usr/local/lib /usr/local/lib/aarch64-linux-gnu /opt/unitree/lib /opt/unitree_robotics/lib; do
    if [ -e "${d}/libunitree_sdk2.so" ] || [ -e "${d}/libunitree_sdk2.a" ]; then
      UNITREE_LIB="$d"
      break
    fi
  done
fi
if [ ! -e "${UNITREE_LIB}/libunitree_sdk2.so" ] && [ ! -e "${UNITREE_LIB}/libunitree_sdk2.a" ]; then
  _found=$(find /opt /usr/local "${HOME}" -maxdepth 6 \( -name 'libunitree_sdk2.so' -o -name 'libunitree_sdk2.so.*' \) -type f -print -quit 2>/dev/null || true)
  if [ -n "${_found}" ]; then
    UNITREE_LIB="$(dirname "${_found}")"
  fi
fi
UNITREE_LINK=""
if [ -e "${UNITREE_LIB}/libunitree_go2_sdk.so" ] || [ -e "${UNITREE_LIB}/libunitree_go2_sdk.a" ]; then
  UNITREE_LINK="-lunitree_go2_sdk"
elif [ -e "${UNITREE_LIB}/libunitree_sdk2.so" ] || [ -e "${UNITREE_LIB}/libunitree_sdk2.a" ]; then
  UNITREE_LINK="-lunitree_sdk2"
else
  echo "ERROR: nessuna libreria Unitree SDK in ${UNITREE_LIB} (atteso libunitree_go2_sdk o libunitree_sdk2)." >&2
  exit 2
fi
echo "Using UNITREE_LIB=${UNITREE_LIB} UNITREE_INCLUDE=${UNITREE_INCLUDE} LINK=${UNITREE_LINK} REPO_ROOT=${REPO_ROOT}"

CXXFLAGS=(
  -std=c++17 -O2 -Wall
  "-I${UNITREE_INCLUDE}"
  "-I${ICEORYX_CPP_INC}"
)
if [ -d "${MSG_ROOT}/msg" ]; then
  CXXFLAGS+=("-I${MSG_ROOT}")
fi
USE_USR_LOCAL_DDSCXX=0
if [ -f /usr/local/include/ddscxx/dds/dds.hpp ]; then
  USE_USR_LOCAL_DDSCXX=1
  CXXFLAGS+=("-I/usr/local/include/ddscxx")
fi
if [ "${USE_USR_LOCAL_DDSCXX}" = 0 ] && [ -n "${CYCLONEDDS_HOME:-}" ]; then
  for idir in include include/ddscxx; do
    if [ -d "${CYCLONEDDS_HOME}/${idir}" ]; then
      CXXFLAGS+=("-I${CYCLONEDDS_HOME}/${idir}")
    fi
  done
fi

LIBS=(
  "-L${UNITREE_LIB}"
  ${UNITREE_LINK}
  -lunitree_go2_idl_cpp -lunitree_ros2_idl_cpp
  -lcycloneddsidlcxx -lcycloneddsidl
  -lddscxx -lddsc -lpthread -lssl -lcrypto
)
if [ "${USE_USR_LOCAL_DDSCXX}" = 0 ] && [ -n "${CYCLONEDDS_HOME:-}" ] && [ -d "${CYCLONEDDS_HOME}/lib" ]; then
  LIBS=("-L${CYCLONEDDS_HOME}/lib" "${LIBS[@]}")
fi
for _iox in iceoryx_posh iceoryx_platform iceoryx_hoofs; do
  if compgen -G "/usr/local/lib/lib${_iox}.so*" > /dev/null 2>&1 \
    || compgen -G "${UNITREE_LIB}/lib${_iox}.so*" > /dev/null 2>&1; then
    LIBS+=("-l${_iox}")
  fi
done
RPATH_FLAGS=("-Wl,-rpath,${UNITREE_LIB}")
if [ -d /usr/local/lib ]; then RPATH_FLAGS+=("-Wl,-rpath,/usr/local/lib"); fi
if [ -n "${CYCLONEDDS_HOME:-}" ] && [ -d "${CYCLONEDDS_HOME}/lib" ]; then
  RPATH_FLAGS+=("-Wl,-rpath,${CYCLONEDDS_HOME}/lib")
fi
echo "Building d1_arm_command -> bin/d1_arm_command"
g++ "${CXXFLAGS[@]}" "${RPATH_FLAGS[@]}" -o "${REPO_ROOT}/bin/d1_arm_command" \
  "${SCRIPT_DIR}/d1_arm_dds_helper.cpp" \
  "${MSG_ROOT}/msg/ArmString_.cpp" \
  "${LIBS[@]}"

echo "Building d1_arm_feedback_helper -> bin/d1_arm_feedback_helper"
g++ "${CXXFLAGS[@]}" "${RPATH_FLAGS[@]}" -o "${REPO_ROOT}/bin/d1_arm_feedback_helper" \
  "${SCRIPT_DIR}/d1_arm_feedback_helper.cpp" \
  "${MSG_ROOT}/msg/ArmString_.cpp" \
  "${MSG_ROOT}/msg/PubServoInfo_.cpp" \
  "${LIBS[@]}"

echo "Done."
