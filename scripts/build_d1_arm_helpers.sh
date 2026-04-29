#!/usr/bin/env bash
# Compila gli helper DDS braccio D1 nella cartella bin/ (Jetson / Linux con Unitree SDK2).
# Imposta prima le variabili se i path sul tuo sistema differiscono:
#   export UNITREE_SDK2="${UNITREE_SDK2:-/usr/local}"
#   export UNITREE_INCLUDE="${UNITREE_INCLUDE:-$UNITREE_SDK2/include}"
#   export UNITREE_LIB="${UNITREE_LIB:-$UNITREE_SDK2/lib}"
#   export ICEORYX_CPP_INC="${ICEORYX_CPP_INC:-/usr/local/include/iceoryx/v2.0.2}"

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "${ROOT}/bin"

UNITREE_SDK2="${UNITREE_SDK2:-/usr/local}"
UNITREE_INCLUDE="${UNITREE_INCLUDE:-${UNITREE_SDK2}/include}"
UNITREE_LIB="${UNITREE_LIB:-${UNITREE_SDK2}/lib}"
ICEORYX_CPP_INC="${ICEORYX_CPP_INC:-/usr/local/include/iceoryx/v2.0.2}"

CXXFLAGS=(
  -std=c++17 -O2 -Wall
  "-I${UNITREE_INCLUDE}"
  "-I${UNITREE_INCLUDE}/dds"
  "-I${ICEORYX_CPP_INC}"
)

LIBS=(
  "-L${UNITREE_LIB}"
  -lunitree_sdk2 -lddsc -lddscxx -lpthread
)

echo "Building d1_arm_command -> bin/d1_arm_command"
g++ "${CXXFLAGS[@]}" -o "${ROOT}/bin/d1_arm_command" "${ROOT}/scripts/d1_arm_dds_helper.cpp" "${LIBS[@]}"

echo "Building d1_arm_feedback_helper -> bin/d1_arm_feedback_helper"
g++ "${CXXFLAGS[@]}" -o "${ROOT}/bin/d1_arm_feedback_helper" "${ROOT}/scripts/d1_arm_feedback_helper.cpp" "${LIBS[@]}"

echo "Done."
