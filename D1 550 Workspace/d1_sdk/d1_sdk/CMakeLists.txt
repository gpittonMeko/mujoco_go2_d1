project(marm_code)
cmake_minimum_required(VERSION 3.5)

SET(CMAKE_CXX_STANDARD 17)

include_directories(/usr/local/include/ddscxx /usr/local/include/iceoryx/v2.0.2)
if(EXISTS "/usr/local/lib/libunitree_go2_sdk.a" OR EXISTS "/usr/local/lib/libunitree_go2_sdk.so")
  link_libraries(unitree_go2_sdk unitree_go2_idl_cpp unitree_ros2_idl_cpp ddsc ddscxx rt pthread)
else()
  link_libraries(unitree_sdk2 ddsc ddscxx rt pthread)
endif()

add_executable(joint_angle_control src/joint_angle_control.cpp src/msg/ArmString_.cpp)
add_executable(multiple_joint_angle_control src/multiple_joint_angle_control.cpp src/msg/ArmString_.cpp)
add_executable(joint_enable_control src/joint_enable_control.cpp src/msg/ArmString_.cpp)
add_executable(arm_zero_control src/arm_zero_control.cpp src/msg/ArmString_.cpp)
add_executable(get_arm_joint_angle src/get_arm_joint_angle.cpp src/msg/ArmString_.cpp src/msg/PubServoInfo_.cpp)
add_executable(d1_command_stdin src/d1_command_stdin.cpp src/msg/ArmString_.cpp)
add_executable(d1_feedback_snapshot src/d1_feedback_snapshot.cpp src/msg/ArmString_.cpp src/msg/PubServoInfo_.cpp)