// Virtual board: the real RTL under Verilator with its UART exposed as a pty,
// so the orchestrator talks to real hardware logic with no Arty attached.
//
//   sim/obj_dir/Vtop_edge_node --node 0 --clk 2000000 --baud 200000
//
// Prints "PTY /dev/ttysNNN" on stdout once the pty exists, then runs until
// killed. The pty has no physical baud; --baud must equal the BAUD parameter
// the RTL was built with (-GBAUD=...) because this file bit-bangs the UART
// pins at CLK_HZ/BAUD cycles per bit. Use a high sim baud (10 Mbaud) or a
// 56 KB push takes minutes; the RTL does not care.
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fcntl.h>
#include <termios.h>
#include <unistd.h>
#include <time.h>
#include <deque>
#include "Vtop_edge_node.h"
#include "verilated.h"


int main(int argc, char** argv) {
    Verilated::commandArgs(argc, argv);
    int node = 0; long baud = 200000; long CLK_HZ = 2000000;   // must equal the -G build values (sim/Makefile)
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--node") && i + 1 < argc) node = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--baud") && i + 1 < argc) baud = atol(argv[++i]);
        else if (!strcmp(argv[i], "--clk") && i + 1 < argc) CLK_HZ = atol(argv[++i]);
    }
    const long cyc_per_bit = CLK_HZ / baud;

    int master = posix_openpt(O_RDWR | O_NOCTTY);
    if (master < 0 || grantpt(master) || unlockpt(master)) { perror("pty"); return 1; }
    termios t; tcgetattr(master, &t); cfmakeraw(&t); tcsetattr(master, TCSANOW, &t);
    fcntl(master, F_SETFL, fcntl(master, F_GETFL) | O_NONBLOCK);
    printf("PTY %s\n", ptsname(master)); fflush(stdout);

    Vtop_edge_node* top = new Vtop_edge_node;
    top->clk = 0; top->sw = node & 3; top->uart_txd_in = 1;
    top->eval();

    std::deque<unsigned char> txq;          // host -> node bytes waiting to be bit-banged
    long rx_bit_cyc = 0; int rx_bit = -1; unsigned rx_shift = 0;   // host->node shifter
    long tx_n = -1; unsigned tx_shift = 0;   // node->host sampler: cycles since start edge, -1 = idle
    unsigned char buf[512];
    unsigned long long cycle = 0;

    while (!Verilated::gotFinish()) {
        // clock: one full period per loop iteration
        top->clk = 1; top->eval();
        top->clk = 0; top->eval();
        cycle++;
        if ((cycle & ((1ull << 25) - 1)) == 0) {          // ~every 33.5M cycles: report achieved sim rate on stderr
            static double last = 0; timespec ts; clock_gettime(CLOCK_MONOTONIC, &ts);
            double now = ts.tv_sec + ts.tv_nsec * 1e-9;
            if (last > 0) fprintf(stderr, "sim: %.1f Mcycles/s (sim-time %.2f s)\n", (1ull << 25) / (now - last) / 1e6, (double)cycle / CLK_HZ);
            last = now;
        }

        // ---- host -> node: drive uart_txd_in one bit per cyc_per_bit ----
        if (rx_bit < 0) {
            if (txq.empty() && (cycle & 1023) == 0) {        // poll the pty occasionally, not every cycle
                ssize_t n = read(master, buf, sizeof buf);
                for (ssize_t i = 0; i < n; i++) txq.push_back(buf[i]);
            }
            if (!txq.empty()) {
                rx_shift = (0x100u | txq.front()) << 1;      // start(0) + 8 data (LSB first) + stop(1)
                txq.pop_front();
                rx_bit = 0; rx_bit_cyc = 0;
            }
        }
        if (rx_bit >= 0) {
            if (rx_bit_cyc == 0) top->uart_txd_in = (rx_shift >> rx_bit) & 1;
            if (++rx_bit_cyc >= cyc_per_bit) { rx_bit_cyc = 0; if (++rx_bit > 9) { rx_bit = -1; top->uart_txd_in = 1; } }
        }

        // ---- node -> host: sample uart_rxd_out at the centre of each bit ----
        // Data bit i (LSB first) is centred (i+1.5) bit-times after the start edge;
        // the stop bit at 9.5. Sampling at 0.5 would read the start bit as data.
        if (tx_n < 0) {
            if (!top->uart_rxd_out) { tx_n = 0; tx_shift = 0; }
        } else {
            tx_n++;
            long mid = tx_n - cyc_per_bit / 2;
            if (mid >= 0 && mid % cyc_per_bit == 0) {
                long bit = mid / cyc_per_bit;            // 0 = start, 1..8 = data, 9 = stop
                if (bit >= 1 && bit <= 8) tx_shift |= (unsigned)top->uart_rxd_out << (bit - 1);
                else if (bit == 9) {
                    if (top->uart_rxd_out) { unsigned char b = tx_shift & 0xFF; if (write(master, &b, 1) < 0) {} }
                    tx_n = -1;                            // framing error: drop silently, resync on next start edge
                }
            }
        }
    }
    delete top;
    return 0;
}
