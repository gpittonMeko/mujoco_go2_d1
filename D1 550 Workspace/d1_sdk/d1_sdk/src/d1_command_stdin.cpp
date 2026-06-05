#include <chrono>
#include <cstdlib>
#include <iostream>
#include <string>
#include <thread>

#include <unitree/robot/channel/channel_publisher.hpp>
#include "msg/ArmString_.hpp"

#define TOPIC "rt/arm_Command"

using namespace unitree::robot;

int main(int argc, char** argv) {
    const int domain = argc >= 2 ? std::atoi(argv[1]) : 0;
    const int delay_ms = argc >= 3 ? std::atoi(argv[2]) : 40;

    ChannelFactory::Instance()->Init(domain);
    ChannelPublisher<unitree_arm::msg::dds_::ArmString_> publisher(TOPIC);
    publisher.InitChannel();
    std::this_thread::sleep_for(std::chrono::milliseconds(150));

    int sent = 0;
    std::string line;
    while (std::getline(std::cin, line)) {
        if (line.empty() || line[0] == '#') {
            continue;
        }
        unitree_arm::msg::dds_::ArmString_ msg{};
        msg.data_() = line;
        publisher.Write(msg);
        std::cout << line << std::endl;
        ++sent;
        if (delay_ms > 0) {
            std::this_thread::sleep_for(std::chrono::milliseconds(delay_ms));
        }
    }

    std::cout << "sent_stages=" << sent << std::endl;
    return sent > 0 ? 0 : 4;
}
