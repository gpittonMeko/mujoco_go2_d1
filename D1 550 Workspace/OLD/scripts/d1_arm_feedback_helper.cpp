#include <unitree/robot/channel/channel_subscriber.hpp>
#include <unitree/common/time/time_tool.hpp>

#include "msg/ArmString_.hpp"
#include "msg/PubServoInfo_.hpp"

#include <atomic>
#include <chrono>
#include <iostream>
#include <thread>

using namespace unitree::robot;

static std::atomic<int> servo_count{0};
static std::atomic<int> feedback_count{0};

void ServoHandler(const void* msg) {
    const auto* pm = static_cast<const unitree_arm::msg::dds_::PubServoInfo_*>(msg);
    ++servo_count;
    std::cout << "servo_angles "
              << pm->servo0_data_() << " "
              << pm->servo1_data_() << " "
              << pm->servo2_data_() << " "
              << pm->servo3_data_() << " "
              << pm->servo4_data_() << " "
              << pm->servo5_data_() << " "
              << pm->servo6_data_() << std::endl;
}

void ArmFeedbackHandler(const void* msg) {
    const auto* pm = static_cast<const unitree_arm::msg::dds_::ArmString_*>(msg);
    ++feedback_count;
    std::cout << "arm_feedback " << pm->data_() << std::endl;
}

int main(int argc, char** argv) {
    const int domain = argc >= 2 ? std::atoi(argv[1]) : 0;
    const int seconds = argc >= 3 ? std::atoi(argv[2]) : 5;

    ChannelFactory::Instance()->Init(domain);
    ChannelSubscriber<unitree_arm::msg::dds_::PubServoInfo_> servo_sub("current_servo_angle");
    servo_sub.InitChannel(ServoHandler);
    ChannelSubscriber<unitree_arm::msg::dds_::ArmString_> feedback_sub("arm_Feedback");
    feedback_sub.InitChannel(ArmFeedbackHandler);

    std::this_thread::sleep_for(std::chrono::seconds(seconds));
    std::cout << "servo_count=" << servo_count.load() << " feedback_count=" << feedback_count.load() << std::endl;
    return servo_count.load() > 0 || feedback_count.load() > 0 ? 0 : 1;
}
