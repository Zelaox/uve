#include "core/state/app_state_store.hpp"

#include <filesystem>
#include <chrono>
#include <iostream>
#include <string>
#include <thread>

int main(int argc, char** argv) {
    if (argc != 5) return 2;
    const std::filesystem::path state_path = argv[1];
    const std::string operation = argv[2];
    const std::string mode = argv[3];
    if (operation.find_first_not_of("0123456789abcdef") != std::string::npos || operation.empty()) return 2;

    emulator::state::AppStateStore store(state_path);
    if (mode == "save" || mode == "save_then_wait") {
        emulator::state::ApplicationState state;
        state.selected_project_id = operation;
        state.preferences.dock_gap = std::stoi(argv[4]);
        const bool saved = store.save(state);
        std::cout << "{\"operation_id\":\"" << operation << "\",\"saved\":" << std::boolalpha << saved << "}" << std::endl;
        if (saved && mode == "save_then_wait") {
            // This fixture waits for UVE's owned process handle. UVE supplies the deadline.
            while (true) std::this_thread::sleep_until(std::chrono::steady_clock::time_point::max());
        }
        return 0;
    }
    if (mode != "load") return 2;
    const auto loaded = store.load();
    if (!loaded.loaded || loaded.state.selected_project_id != operation) return 3;
    std::cout << "{\"operation_id\":\"" << operation << "\",\"loaded\":true,\"recovered\":"
              << std::boolalpha << loaded.recovered_from_backup << ",\"revision\":"
              << loaded.state.preferences.dock_gap << "}\n";
}
