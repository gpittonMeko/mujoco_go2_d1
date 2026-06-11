#include <unitree/robot/channel/channel_subscriber.hpp>
#include <unitree/common/time/time_tool.hpp>

#include "msg/ArmString_.hpp"
#include "msg/PubServoInfo_.hpp"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdlib>
#include <iostream>
#include <thread>

using namespace unitree::robot;

static std::atomic<int> servo_count{0};
static std::atomic<int> feedback_count{0};
static std::atomic<bool> servo_got_sample{false};

void ServoHandler(const void* msg) {
    const auto* pm = static_cast<const unitree_arm::msg::dds_::PubServoInfo_*>(msg);
    ++servo_count;
    servo_got_sample.store(true);
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

    // Il terzo argomento è timeout massimo. Uscita anticipata dopo il primo ``servo_angles`` +
    // piccolo settle (default 80 ms): evitare di bloccare la dashboard per secondi interi senza
    // pubblicare su rt/arm_Command (rischio perdita coppia / collasso braccio tra un comando e l'altro).
    int settle_ms = 80;
    if (const char* env_ms = std::getenv("D1_FEEDBACK_HELPER_SETTLE_MS")) {
        const int v = std::atoi(env_ms);
        settle_ms = std::max(0, std::min(800, v));
    }
    using clock = std::chrono::steady_clock;
    const auto deadline = clock::now() + std::chrono::seconds(std::max(1, seconds));
    while (clock::now() < deadline) {
        if (servo_got_sample.load()) {
            if (settle_ms > 0) {
                std::this_thread::sleep_for(std::chrono::milliseconds(settle_ms));
            }
            break;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(4));
    }
    if (!servo_got_sample.load()) {
        std::this_thread::sleep_until(deadline);
    }

    std::cout << "servo_count=" << servo_count.load() << " feedback_count=" << feedback_count.load() << std::endl;
    return servo_count.load() > 0 || feedback_count.load() > 0 ? 0 : 1;
}
