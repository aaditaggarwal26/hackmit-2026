// node_ctrl: the edge node's brain — protocol.md §3 (busy drops), §4 (message
// field layouts) and §5 (ingest, scoring, queue, grant, config, bench, timers) as
// one job-priority state machine that owns the TX framer. It mirrors
// orbit/golden/node.py::ScoringNode.handle/tick.
//
// Jobs, highest priority first, picked whenever the FSM is idle:
//   1 bench finished: BENCH_DONE, state -> IDLE
//   2 a received message (1-deep latch from framer_rx)
//   3 heartbeat due: HEARTBEAT then STATUS_REPLY
//   4 power sample ready: POWER
// Scoring (row 127 of FRAME_INGEST) runs kernel -> composite -> queue insert ->
// FRAME_SCORED without returning to idle, so a STATUS can never show the queue
// before the ground has seen the insert; likewise GRANT runs TX_FRAME -> pop ->
// TX_DONE in one job. Every TX goes through one "wait for tx_ready, pulse
// tx_start" step, so FIFO back-pressure only delays, never drops.
//
// Little-endian wire fields fall out of plain concatenation: byte 0 of the
// payload is bits [7:0], so a u16 at offset 2 is bits [31:16].

`include "orbit_params.vh"

module node_ctrl #(
    parameter PPC             = `ORBIT_PIXELS_PER_CYCLE,
    parameter FRAME_W         = `ORBIT_FRAME_W,
    parameter FRAME_H         = `ORBIT_FRAME_H,
    parameter FRAME_BYTES     = `ORBIT_FRAME_BYTES,
    parameter QUEUE_DEPTH     = `ORBIT_QUEUE_DEPTH,
    parameter ADDR_W          = 11,
    parameter CW              = 6,
    parameter HEARTBEAT_MS    = `ORBIT_HEARTBEAT_MS,
    parameter LINK_TIMEOUT_MS = `ORBIT_LINK_TIMEOUT_MS,
    parameter POWER_PERIOD_MS = `ORBIT_POWER_PERIOD_MS,
    parameter RX_MAX_PAYLOAD  = 131,
    parameter TX_MAX_PAYLOAD  = 30
) (
    input  wire         clk,
    input  wire         rst_n,
    input  wire [7:0]   node_id,
    input  wire         ms_tick,
    // framer_rx
    input  wire         rx_msg_valid,
    input  wire [7:0]   rx_msg_type,
    input  wire [8*RX_MAX_PAYLOAD-1:0] rx_msg_payload,
    input  wire [15:0]  crc_errors,
    input  wire [15:0]  len_errors,
    input  wire [15:0]  unknown_type,
    input  wire [15:0]  rx_overflow,
    // framer_tx
    output reg          tx_start,
    output reg  [7:0]   tx_type,
    output reg  [5:0]   tx_len,
    output reg  [8*TX_MAX_PAYLOAD-1:0] tx_payload,
    input  wire         tx_ready,
    input  wire         tx_fifo_hwm,                // TX byte FIFO above its high-water mark (sticky TX_STALLED flag)
    // frame / reference stores (write side)
    output reg          fs_wr_en,
    output reg          fs_wr_ref,                  // 1: reference store, 0: frame store
    output reg  [ADDR_W-1:0] fs_wr_addr,
    output reg  [8*PPC-1:0]  fs_wr_data,
    // kernel
    output reg          k_start,
    output reg  [31:0]  k_iterations,
    /* verilator lint_off UNUSEDSIGNAL */
    input  wire         k_busy,                     // informational; the FSM sequences the kernel itself
    /* verilator lint_on UNUSEDSIGNAL */
    input  wire         k_done,
    input  wire [31:0]  k_cycles,
    output reg  [7:0]   cfg_cloud_thr,
    output reg  [7:0]   cfg_change_thr,
    // composite
    output reg          c_start,
    input  wire         c_done,
    input  wire [15:0]  c_clear,
    input  wire [15:0]  c_sharp,
    input  wire [15:0]  c_change,
    input  wire [15:0]  c_score,
    output reg  [15:0]  cfg_w_clear,
    output reg  [15:0]  cfg_w_sharp,
    output reg  [15:0]  cfg_w_change,
    output reg  [4:0]   cfg_sharp_shift,
    // queue
    output reg          q_insert,
    output reg  [15:0]  q_in_score,
    output reg  [15:0]  q_in_id,
    output reg          q_pop,
    output reg          q_set_limit,
    output reg  [CW-1:0] q_new_limit,
    input  wire [CW-1:0] q_count,
    input  wire         q_has_data,
    input  wire [15:0]  q_top_score,
    input  wire [15:0]  q_top_id,
    input  wire [15:0]  q_evicted,
    input  wire [15:0]  q_lost_id,
    // INA219
    output reg          power_trigger,
    input  wire         power_sample_valid,
    input  wire [15:0]  power_bus_raw,
    input  wire [15:0]  power_shunt_raw,
    input  wire         power_ok,
    // status for LEDs
    output reg  [1:0]   state,
    output reg          link_ok,
    output wire         busy,
    output reg          hb_blink
);
    localparam ST_IDLE = 2'd0, ST_SCORING = 2'd1, ST_BENCH = 2'd2;
    localparam [7:0] T_FRAME_INGEST = 8'h01, T_CONFIG_SET = 8'h02, T_REF_FRAME_SET = 8'h03, T_BENCH_RUN = 8'h04,
                     T_STATUS_QUERY = 8'h10, T_STATUS_REPLY = 8'h11, T_FRAME_SCORED = 8'h12, T_BENCH_DONE = 8'h13,
                     T_GRANT = 8'h20, T_TX_FRAME = 8'h21, T_TX_DONE = 8'h22, T_HEARTBEAT = 8'h40, T_POWER = 8'h41;
    localparam WPR = FRAME_W / PPC;
    localparam WW  = 8 * PPC;
    localparam KW  = (WPR > 1) ? $clog2(WPR) : 1;
    /* verilator lint_off WIDTHTRUNC */
    localparam [15:0]   HB_LAST   = HEARTBEAT_MS - 1;
    localparam [15:0]   PWR_LAST  = POWER_PERIOD_MS - 1;
    localparam [KW-1:0] W_LAST    = WPR - 1;
    localparam [CW-1:0] DEPTH_CW  = QUEUE_DEPTH;
    localparam [7:0]    ROW_LAST  = FRAME_H - 1;
    localparam [7:0]    SHIFT_MAX = `ORBIT_SHARP_SHIFT_MAX;
    /* verilator lint_on WIDTHTRUNC */

    assign busy = (state != ST_IDLE);

    // ---------------------------------------------------------------- node state
    reg         config_valid, ref_loaded, tx_stalled, ina219_present;
    reg [15:0]  frame_id, frames_scored, frames_sent, rows_rx, busy_drops;
    reg [31:0]  cycles_last, bench_n;
    reg         bench_done_due;
    reg [31:0]  uptime_ms, link_ms;
    reg [15:0]  hb_seq, hb_ms, power_ms;
    reg         hb_seen, hb_due, power_due;
    reg [15:0]  power_bus_q, power_shunt_q;
    reg         power_ok_q;
    reg [15:0]  slot_id, tx_score, tx_id;
    reg [31:0]  tx_bytes;

    wire link_timeout = hb_seen && (link_ms > LINK_TIMEOUT_MS);

    // ---------------------------------------------------------------- rx latch
    reg         rx_pending;
    reg [7:0]   rx_type;
    reg [8*RX_MAX_PAYLOAD-1:0] rx_pl;
    reg [15:0]  rx_dropped;                      // latch full: debug only, not a protocol counter

    // decoded fields (valid while rx_pending)
    wire [15:0] f_frame_id  = rx_pl[15:0];
    wire [7:0]  f_row       = rx_pl[23:16];
    wire [15:0] f_w_clear   = rx_pl[15:0];
    wire [15:0] f_w_sharp   = rx_pl[31:16];
    wire [15:0] f_w_change  = rx_pl[47:32];
    wire [7:0]  f_cloud_thr = rx_pl[55:48];
    wire [7:0]  f_change_thr= rx_pl[63:56];
    wire [7:0]  f_shift     = rx_pl[71:64];
    wire [7:0]  f_qlimit    = rx_pl[79:72];
    wire [31:0] f_iters     = rx_pl[31:0];
    wire [15:0] f_slot      = rx_pl[15:0];
    wire [31:0] f_budget    = rx_pl[47:16];
    wire cfg_ok = (f_qlimit >= 8'd1) && (f_qlimit <= QUEUE_DEPTH[7:0]) && (f_shift <= SHIFT_MAX);

    // ---------------------------------------------------------------- job FSM
    localparam F_IDLE = 4'd0, F_RX = 4'd1, F_RX_STATUS = 4'd2, F_ROW = 4'd3, F_SCORE_WAIT = 4'd4,
               F_COMP_WAIT = 4'd5, F_INSERTED = 4'd6, F_SCORED = 4'd7, F_TXFRAME = 4'd8, F_TXFRAME_SENT = 4'd9,
               F_TXDONE = 4'd10, F_BENCHDONE = 4'd11, F_HB = 4'd12, F_HB_SENT = 4'd13, F_HB_STATUS = 4'd14,
               F_POWER = 4'd15;
    reg [3:0]   fsm;
    reg [KW-1:0] w;                              // row copy word counter
    reg         row_is_ref, row_last;
    reg [$clog2(FRAME_H)-1:0] row_q;

    // ---------------------------------------------------------------- TX message builder
    reg [7:0] tx_sel;
    wire [7:0] status_flags = {1'b0, ina219_present, tx_stalled, busy, ref_loaded, config_valid, link_ok, q_has_data};
    always @(*) begin
        tx_type = tx_sel;
        tx_len = 6'd0;
        tx_payload = {(8*TX_MAX_PAYLOAD){1'b0}};
        case (tx_sel)
            T_HEARTBEAT: begin
                tx_len = 6'd7;
                tx_payload[55:0] = {uptime_ms, hb_seq, node_id};
            end
            T_STATUS_REPLY: begin
                tx_len = 6'd30;
                tx_payload[239:0] = {cycles_last, rx_overflow, unknown_type, len_errors, crc_errors, busy_drops, rows_rx,
                                     frames_sent, q_evicted, frames_scored, q_top_id, q_top_score,
                                     {{(8-CW){1'b0}}, q_count}, status_flags, {6'b0, state}, node_id};
            end
            T_FRAME_SCORED: begin
                tx_len = 6'd14;
                tx_payload[111:0] = {{{(8-CW){1'b0}}, q_count}, q_lost_id, c_score, c_change, c_sharp, c_clear,
                                     frame_id, node_id};
            end
            T_BENCH_DONE: begin
                tx_len = 6'd9;
                tx_payload[71:0] = {cycles_last, bench_n, node_id};
            end
            T_TX_FRAME: begin
                tx_len = 6'd11;
                tx_payload[87:0] = {tx_bytes, tx_score, tx_id, slot_id, node_id};
            end
            T_TX_DONE: begin
                tx_len = 6'd10;
                tx_payload[79:0] = {{7'b0, q_has_data}, q_top_score, tx_bytes, slot_id, node_id};
            end
            T_POWER: begin
                tx_len = 6'd10;
                tx_payload[79:0] = {uptime_ms, power_shunt_q, power_bus_q, {7'b0, power_ok_q}, node_id};
            end
            default: begin end
        endcase
    end

    // ---------------------------------------------------------------- main
    always @(posedge clk) begin
        if (!rst_n) begin
            state <= ST_IDLE; config_valid <= 1'b1; ref_loaded <= 1'b0; tx_stalled <= 1'b0; ina219_present <= 1'b0;
            link_ok <= 1'b0; hb_blink <= 1'b0;
            cfg_w_clear <= `ORBIT_W_CLEAR; cfg_w_sharp <= `ORBIT_W_SHARP; cfg_w_change <= `ORBIT_W_CHANGE;
            cfg_cloud_thr <= `ORBIT_CLOUD_THRESHOLD; cfg_change_thr <= `ORBIT_CHANGE_THRESHOLD;
            cfg_sharp_shift <= `ORBIT_SHARP_SHIFT;
            frame_id <= 16'd0; frames_scored <= 16'd0; frames_sent <= 16'd0; rows_rx <= 16'd0; busy_drops <= 16'd0;
            cycles_last <= 32'd0; bench_n <= 32'd0; bench_done_due <= 1'b0;
            uptime_ms <= 32'd0; link_ms <= 32'd0; hb_seq <= 16'd0; hb_ms <= 16'd0; power_ms <= 16'd0;
            hb_seen <= 1'b0; hb_due <= 1'b0; power_due <= 1'b0;
            power_bus_q <= 16'd0; power_shunt_q <= 16'd0; power_ok_q <= 1'b0;
            slot_id <= 16'd0; tx_score <= 16'd0; tx_id <= 16'd0; tx_bytes <= 32'd0;
            rx_pending <= 1'b0; rx_type <= 8'd0; rx_pl <= {(8*RX_MAX_PAYLOAD){1'b0}}; rx_dropped <= 16'd0;
            fsm <= F_IDLE; w <= {KW{1'b0}}; row_is_ref <= 1'b0; row_last <= 1'b0; row_q <= {$clog2(FRAME_H){1'b0}}; tx_sel <= 8'd0;
            tx_start <= 1'b0; fs_wr_en <= 1'b0; fs_wr_ref <= 1'b0; fs_wr_addr <= {ADDR_W{1'b0}}; fs_wr_data <= {WW{1'b0}};
            k_start <= 1'b0; k_iterations <= 32'd1; c_start <= 1'b0;
            q_insert <= 1'b0; q_in_score <= 16'd0; q_in_id <= 16'd0; q_pop <= 1'b0; q_set_limit <= 1'b0;
            q_new_limit <= DEPTH_CW; power_trigger <= 1'b0;
        end else begin
            // one-cycle pulses
            tx_start <= 1'b0; fs_wr_en <= 1'b0; k_start <= 1'b0; c_start <= 1'b0;
            q_insert <= 1'b0; q_pop <= 1'b0; q_set_limit <= 1'b0; power_trigger <= 1'b0;

            // ---- timers
            if (ms_tick) begin
                uptime_ms <= uptime_ms + 1'b1;
                if (link_ms != 32'hFFFFFFFF) link_ms <= link_ms + 1'b1;
                if (hb_ms == HB_LAST) begin
                    hb_ms <= 16'd0;
                    hb_due <= 1'b1;
                    hb_blink <= ~hb_blink;
                end else begin
                    hb_ms <= hb_ms + 1'b1;
                end
                if (power_ms == PWR_LAST) begin
                    power_ms <= 16'd0;
                    power_trigger <= 1'b1;
                end else begin
                    power_ms <= power_ms + 1'b1;
                end
            end
            if (link_timeout) link_ok <= 1'b0;
            if (tx_fifo_hwm) tx_stalled <= 1'b1;
            if (power_sample_valid) begin
                power_bus_q <= power_bus_raw;
                power_shunt_q <= power_shunt_raw;
                power_ok_q <= power_ok;
                ina219_present <= power_ok;
                power_due <= 1'b1;
            end
            if (k_done && state == ST_BENCH) begin
                bench_done_due <= 1'b1;
                cycles_last <= k_cycles;
            end

            // ---- rx latch
            if (rx_msg_valid) begin
                if (rx_pending) rx_dropped <= rx_dropped + 1'b1;
                else begin
                    rx_pending <= 1'b1;
                    rx_type <= rx_msg_type;
                    rx_pl <= rx_msg_payload;
                end
            end

            // ---- job FSM
            case (fsm)
                F_IDLE: begin
                    if (bench_done_due) begin
                        tx_sel <= T_BENCH_DONE;
                        fsm <= F_BENCHDONE;
                    end else if (rx_pending) begin
                        fsm <= F_RX;
                    end else if (hb_due) begin
                        tx_sel <= T_HEARTBEAT;
                        fsm <= F_HB;
                    end else if (power_due) begin
                        tx_sel <= T_POWER;
                        fsm <= F_POWER;
                    end
                end
                // -- received message (ScoringNode.handle)
                F_RX: begin
                    rx_pending <= 1'b0;
                    fsm <= F_IDLE;
                    if (rx_type == T_HEARTBEAT) begin
                        link_ok <= 1'b1;
                        link_ms <= 32'd0;
                        hb_seen <= 1'b1;
                    end else if (rx_type == T_STATUS_QUERY) begin
                        tx_sel <= T_STATUS_REPLY;
                        fsm <= F_RX_STATUS;
                    end else if (rx_type == T_CONFIG_SET) begin
                        if (cfg_ok) begin
                            cfg_w_clear <= f_w_clear; cfg_w_sharp <= f_w_sharp; cfg_w_change <= f_w_change;
                            cfg_cloud_thr <= f_cloud_thr; cfg_change_thr <= f_change_thr;
                            cfg_sharp_shift <= f_shift[4:0];
                            q_new_limit <= f_qlimit[CW-1:0];
                            q_set_limit <= 1'b1;
                            config_valid <= 1'b1;
                        end else begin
                            config_valid <= 1'b0;
                            busy_drops <= busy_drops + 1'b1;
                        end
                        tx_sel <= T_STATUS_REPLY;
                        fsm <= F_RX_STATUS;
                    end else if (busy) begin                    // §3: rows, grants, bench while BUSY
                        if (rx_type == T_FRAME_INGEST || rx_type == T_REF_FRAME_SET || rx_type == T_GRANT ||
                            rx_type == T_BENCH_RUN)
                            busy_drops <= busy_drops + 1'b1;
                    end else if (rx_type == T_FRAME_INGEST || rx_type == T_REF_FRAME_SET) begin
                        if (f_row > ROW_LAST) begin
                            busy_drops <= busy_drops + 1'b1;
                        end else begin
                            rows_rx <= rows_rx + 1'b1;
                            row_is_ref <= (rx_type == T_REF_FRAME_SET);
                            row_last <= (f_row == ROW_LAST);
                            row_q <= f_row[$clog2(FRAME_H)-1:0];
                            if (rx_type == T_FRAME_INGEST) frame_id <= f_frame_id;
                            w <= {KW{1'b0}};
                            fsm <= F_ROW;
                        end
                    end else if (rx_type == T_GRANT) begin
                        slot_id <= f_slot;
                        if (q_has_data && f_budget >= FRAME_BYTES) begin
                            tx_score <= q_top_score;
                            tx_id <= q_top_id;
                            tx_bytes <= FRAME_BYTES;
                            tx_sel <= T_TX_FRAME;
                            fsm <= F_TXFRAME;
                        end else begin
                            tx_bytes <= 32'd0;
                            tx_sel <= T_TX_DONE;
                            fsm <= F_TXDONE;
                        end
                    end else if (rx_type == T_BENCH_RUN) begin
                        bench_n <= f_iters;
                        if (f_iters == 32'd0) begin
                            cycles_last <= 32'd0;
                            tx_sel <= T_BENCH_DONE;
                            fsm <= F_BENCHDONE;
                        end else begin
                            k_iterations <= f_iters;
                            k_start <= 1'b1;
                            state <= ST_BENCH;
                        end
                    end
                    // any other (N->O) type: ignored, like the golden model
                end
                F_RX_STATUS: begin                          // registers above have settled
                    if (tx_ready) begin tx_start <= 1'b1; fsm <= F_IDLE; end
                end
                // -- row copy: WPR words from the latched payload into the selected store
                F_ROW: begin
                    fs_wr_en <= 1'b1;
                    fs_wr_ref <= row_is_ref;
                    fs_wr_addr <= {row_q, w};                            // row * WPR + w
                    fs_wr_data <= rx_pl[24 + WW*w +: WW];
                    if (w == W_LAST) begin
                        if (row_last && !row_is_ref) begin
                            k_iterations <= 32'd1;
                            k_start <= 1'b1;
                            state <= ST_SCORING;
                            fsm <= F_SCORE_WAIT;
                        end else begin
                            if (row_last) ref_loaded <= 1'b1;
                            fsm <= F_IDLE;
                        end
                    end else begin
                        w <= w + 1'b1;
                    end
                end
                // -- scoring: kernel -> composite -> insert -> FRAME_SCORED (no idle in between)
                F_SCORE_WAIT: begin
                    if (k_done) begin
                        cycles_last <= k_cycles;
                        c_start <= 1'b1;
                        fsm <= F_COMP_WAIT;
                    end
                end
                F_COMP_WAIT: begin
                    if (c_done) begin
                        q_in_score <= c_score;
                        q_in_id <= frame_id;
                        q_insert <= 1'b1;
                        frames_scored <= frames_scored + 1'b1;
                        fsm <= F_INSERTED;
                    end
                end
                F_INSERTED: begin                           // queue outputs (count, lost_id) settle this cycle
                    tx_sel <= T_FRAME_SCORED;
                    fsm <= F_SCORED;
                end
                F_SCORED: begin
                    if (tx_ready) begin
                        tx_start <= 1'b1;
                        state <= ST_IDLE;
                        fsm <= F_IDLE;
                    end
                end
                // -- grant: TX_FRAME, pop, TX_DONE
                F_TXFRAME: begin
                    if (tx_ready) begin tx_start <= 1'b1; fsm <= F_TXFRAME_SENT; end
                end
                F_TXFRAME_SENT: begin
                    q_pop <= 1'b1;
                    frames_sent <= frames_sent + 1'b1;
                    tx_sel <= T_TX_DONE;
                    fsm <= F_TXDONE;
                end
                F_TXDONE: begin                             // pop has settled (bounce state)
                    if (tx_ready) begin tx_start <= 1'b1; fsm <= F_IDLE; end
                end
                F_BENCHDONE: begin
                    if (tx_ready) begin
                        tx_start <= 1'b1;
                        bench_done_due <= 1'b0;
                        state <= ST_IDLE;
                        fsm <= F_IDLE;
                    end
                end
                // -- periodic
                F_HB: begin
                    if (tx_ready) begin tx_start <= 1'b1; hb_due <= 1'b0; fsm <= F_HB_SENT; end
                end
                F_HB_SENT: begin
                    hb_seq <= hb_seq + 1'b1;
                    tx_sel <= T_STATUS_REPLY;
                    fsm <= F_HB_STATUS;
                end
                F_HB_STATUS: begin
                    if (tx_ready) begin tx_start <= 1'b1; fsm <= F_IDLE; end
                end
                F_POWER: begin
                    if (tx_ready) begin tx_start <= 1'b1; power_due <= 1'b0; fsm <= F_IDLE; end
                end
                default: fsm <= F_IDLE;
            endcase
        end
    end
endmodule
