// edge_node_core: the whole node behind plain single-ended ports (no
// tristates, no reset generator) so it can be simulated and tested as is;
// top_edge_node adds the board-level wrapper. Clock-rate constants derive
// from CLK_HZ/BAUD so a testbench or the pty harness can run the UART at
// 10 Mbaud (-GBAUD=10000000) and the timers at any speed (-GCLK_HZ).
//
// Data path: UART -> framer_rx -> node_ctrl (row copy) -> frame/ref stores
// -> score_kernel (PPC px/clk) -> score_composite -> priority_queue -> node_ctrl
// -> framer_tx -> byte FIFO -> UART.

`include "orbit_params.vh"

module edge_node_core #(
    parameter PPC             = `ORBIT_PIXELS_PER_CYCLE,
    parameter CLK_HZ          = `ORBIT_CLK_HZ,
    parameter BAUD            = `ORBIT_BAUD,
    parameter HEARTBEAT_MS    = `ORBIT_HEARTBEAT_MS,
    parameter LINK_TIMEOUT_MS = `ORBIT_LINK_TIMEOUT_MS,
    parameter POWER_PERIOD_MS = `ORBIT_POWER_PERIOD_MS,
    parameter I2C_HZ          = 100000,
    parameter TX_FIFO_ADDR_W  = 12               // 4096 B >= TX_FIFO_FRAMES x 35 B wire frames
) (
    input  wire       clk,
    input  wire       rst_n,
    input  wire [7:0] node_id,
    input  wire       uart_rxd,
    output wire       uart_txd,
    output wire       scl_oe,
    output wire       sda_oe,
    input  wire       sda_i,
    output wire [1:0] state,
    output wire       link_ok,
    output wire       has_data,
    output wire       busy,
    output wire       hb_blink
);
    localparam FRAME_W = `ORBIT_FRAME_W, FRAME_H = `ORBIT_FRAME_H, FRAME_BYTES = `ORBIT_FRAME_BYTES;
    localparam QUEUE_DEPTH = `ORBIT_QUEUE_DEPTH;
    localparam WW     = 8 * PPC;
    localparam WORDS  = FRAME_BYTES / PPC;
    localparam ADDR_W = $clog2(WORDS);
    localparam CW     = $clog2(QUEUE_DEPTH + 1);
    localparam CLKS_PER_BIT = CLK_HZ / BAUD;
    localparam MS_DIV = CLK_HZ / 1000;
    localparam I2C_QUARTER = CLK_HZ / I2C_HZ / 4;
    localparam RX_MAX = 131, TX_MAX = 30;

    // ---------------------------------------------------------------- 1 ms tick
    localparam MS_W = $clog2(MS_DIV);
    reg [MS_W-1:0] ms_cnt;
    /* verilator lint_off WIDTHTRUNC */
    localparam [MS_W-1:0] MS_LAST = MS_DIV - 1;    // MS_DIV - 1 < 2**MS_W by construction
    /* verilator lint_on WIDTHTRUNC */
    reg ms_tick;
    always @(posedge clk) begin
        if (!rst_n) begin
            ms_cnt <= {MS_W{1'b0}};
            ms_tick <= 1'b0;
        end else if (ms_cnt == MS_LAST) begin
            ms_cnt <= {MS_W{1'b0}};
            ms_tick <= 1'b1;
        end else begin
            ms_cnt <= ms_cnt + 1'b1;
            ms_tick <= 1'b0;
        end
    end

    // ---------------------------------------------------------------- UART + framers
    wire       rx_valid, rx_frame_err_unused;
    wire [7:0] rx_data;
    uart_rx #(.CLKS_PER_BIT(CLKS_PER_BIT)) urx (
        .clk(clk), .rst_n(rst_n), .rxd(uart_rxd), .valid(rx_valid), .data(rx_data), .frame_err(rx_frame_err_unused));

    wire        msg_valid;
    wire [7:0]  msg_type;
    wire [8*RX_MAX-1:0] msg_payload;
    wire [15:0] crc_errors, len_errors, unknown_type, rx_overflow;
    framer_rx #(.MAX_PAYLOAD(RX_MAX)) frx (
        .clk(clk), .rst_n(rst_n), .rx_valid(rx_valid), .rx_data(rx_data),
        .msg_valid(msg_valid), .msg_type(msg_type), .msg_payload(msg_payload),
        .crc_errors(crc_errors), .len_errors(len_errors), .unknown_type(unknown_type), .rx_overflow(rx_overflow));

    wire        tx_start, tx_ready, fifo_wr;
    wire [7:0]  tx_type, fifo_wdata, fifo_rdata;
    wire [5:0]  tx_len;
    wire [8*TX_MAX-1:0] tx_payload;
    wire [TX_FIFO_ADDR_W:0] fifo_count;
    wire        fifo_empty, fifo_full_unused;
    framer_tx #(.MAX_PAYLOAD(TX_MAX), .FIFO_ADDR_W(TX_FIFO_ADDR_W)) ftx (
        .clk(clk), .rst_n(rst_n), .msg_start(tx_start), .msg_type(tx_type), .msg_len(tx_len),
        .msg_payload(tx_payload), .ready(tx_ready), .fifo_wr(fifo_wr), .fifo_wdata(fifo_wdata), .fifo_count(fifo_count));

    // TX_STALLED diagnostic: the FIFO came within one worst-case burst of full (nothing is ever
    // dropped — the framer waits — but a full FIFO means the ground is not draining us)
    wire tx_fifo_hwm = fifo_count > ((1 << TX_FIFO_ADDR_W) - 256);

    // FIFO -> UART: fetch a byte (registered read) whenever the transmitter is free
    reg fifo_rd, tx_go;
    wire tx_busy;
    byte_fifo #(.ADDR_W(TX_FIFO_ADDR_W)) fifo (
        .clk(clk), .rst_n(rst_n), .wr_en(fifo_wr), .wr_data(fifo_wdata), .rd_en(fifo_rd), .rd_data(fifo_rdata),
        .empty(fifo_empty), .full(fifo_full_unused), .count(fifo_count));
    always @(posedge clk) begin
        if (!rst_n) begin
            fifo_rd <= 1'b0;
            tx_go <= 1'b0;
        end else begin
            fifo_rd <= 1'b0;
            tx_go <= fifo_rd;                        // data lands one cycle after rd_en
            if (!fifo_empty && !tx_busy && !fifo_rd && !tx_go) fifo_rd <= 1'b1;
        end
    end
    uart_tx #(.CLKS_PER_BIT(CLKS_PER_BIT)) utx (
        .clk(clk), .rst_n(rst_n), .start(tx_go), .data(fifo_rdata), .txd(uart_txd), .busy(tx_busy));

    // ---------------------------------------------------------------- frame + reference stores
    wire              fs_wr_en, fs_wr_ref;
    wire [ADDR_W-1:0] fs_wr_addr, frame_rd_addr, ref_rd_addr;
    wire [WW-1:0]     fs_wr_data, frame_rd_data, ref_rd_data;
    frame_store #(.WORD_W(WW), .WORDS(WORDS), .ADDR_W(ADDR_W)) frame_mem (
        .clk(clk), .wr_en(fs_wr_en && !fs_wr_ref), .wr_addr(fs_wr_addr), .wr_data(fs_wr_data),
        .rd_addr(frame_rd_addr), .rd_data(frame_rd_data));
    frame_store #(.WORD_W(WW), .WORDS(WORDS), .ADDR_W(ADDR_W)) ref_mem (
        .clk(clk), .wr_en(fs_wr_en && fs_wr_ref), .wr_addr(fs_wr_addr), .wr_data(fs_wr_data),
        .rd_addr(ref_rd_addr), .rd_data(ref_rd_data));

    // ---------------------------------------------------------------- kernel + composite
    wire        k_start, k_busy, k_done, c_start, c_done;
    wire [31:0] k_iterations, k_cycles;
    wire [7:0]  cfg_cloud_thr, cfg_change_thr;
    wire [14:0] cloud_px, changed_px;
    wire [24:0] sobel_sum;
    wire [15:0] cfg_w_clear, cfg_w_sharp, cfg_w_change, c_clear, c_sharp, c_change, c_score;
    wire [4:0]  cfg_sharp_shift;
    score_kernel #(.PPC(PPC), .FRAME_W(FRAME_W), .FRAME_H(FRAME_H), .ADDR_W(ADDR_W)) kernel (
        .clk(clk), .rst_n(rst_n), .start(k_start), .iterations(k_iterations), .busy(k_busy), .done(k_done),
        .cycles(k_cycles), .cloud_thr(cfg_cloud_thr), .change_thr(cfg_change_thr),
        .frame_rd_addr(frame_rd_addr), .frame_rd_data(frame_rd_data), .ref_rd_addr(ref_rd_addr), .ref_rd_data(ref_rd_data),
        .cloud_px(cloud_px), .changed_px(changed_px), .sobel_sum(sobel_sum));
    score_composite #(.FRAME_BYTES(FRAME_BYTES)) comp (
        .clk(clk), .rst_n(rst_n), .start(c_start), .done(c_done),
        .cloud_px(cloud_px), .changed_px(changed_px), .sobel_sum(sobel_sum),
        .w_clear(cfg_w_clear), .w_sharp(cfg_w_sharp), .w_change(cfg_w_change), .sharp_shift(cfg_sharp_shift),
        .clear(c_clear), .sharp(c_sharp), .change(c_change), .score(c_score));

    // ---------------------------------------------------------------- queue
    wire          q_insert, q_pop, q_set_limit;
    wire [15:0]   q_in_score, q_in_id, q_top_score, q_top_id, q_evicted, q_lost_id;
    wire [CW-1:0] q_new_limit, q_limit_unused, q_count;
    priority_queue #(.DEPTH(QUEUE_DEPTH), .CW(CW)) queue (
        .clk(clk), .rst_n(rst_n), .insert(q_insert), .in_score(q_in_score), .in_id(q_in_id), .pop(q_pop),
        .set_limit(q_set_limit), .new_limit(q_new_limit), .limit(q_limit_unused), .count(q_count),
        .has_data(has_data), .top_score(q_top_score), .top_id(q_top_id), .evicted(q_evicted), .lost_id(q_lost_id));

    // ---------------------------------------------------------------- INA219
    wire        power_trigger, power_sample_valid, power_ok;
    wire [15:0] power_bus_raw, power_shunt_raw;
    ina219_reader #(.I2C_ADDR(`ORBIT_INA219_ADDR), .CLKS_PER_QUARTER(I2C_QUARTER)) ina (
        .clk(clk), .rst_n(rst_n), .trigger(power_trigger), .sample_valid(power_sample_valid),
        .bus_raw(power_bus_raw), .shunt_raw(power_shunt_raw), .ok(power_ok),
        .scl_oe(scl_oe), .sda_oe(sda_oe), .sda_i(sda_i));

    // ---------------------------------------------------------------- control
    node_ctrl #(.PPC(PPC), .FRAME_W(FRAME_W), .FRAME_H(FRAME_H), .FRAME_BYTES(FRAME_BYTES), .QUEUE_DEPTH(QUEUE_DEPTH),
                .ADDR_W(ADDR_W), .CW(CW), .HEARTBEAT_MS(HEARTBEAT_MS), .LINK_TIMEOUT_MS(LINK_TIMEOUT_MS),
                .POWER_PERIOD_MS(POWER_PERIOD_MS), .RX_MAX_PAYLOAD(RX_MAX), .TX_MAX_PAYLOAD(TX_MAX)) ctl (
        .clk(clk), .rst_n(rst_n), .node_id(node_id), .ms_tick(ms_tick),
        .rx_msg_valid(msg_valid), .rx_msg_type(msg_type), .rx_msg_payload(msg_payload),
        .crc_errors(crc_errors), .len_errors(len_errors), .unknown_type(unknown_type), .rx_overflow(rx_overflow),
        .tx_start(tx_start), .tx_type(tx_type), .tx_len(tx_len), .tx_payload(tx_payload), .tx_ready(tx_ready),
        .tx_fifo_hwm(tx_fifo_hwm),
        .fs_wr_en(fs_wr_en), .fs_wr_ref(fs_wr_ref), .fs_wr_addr(fs_wr_addr), .fs_wr_data(fs_wr_data),
        .k_start(k_start), .k_iterations(k_iterations), .k_busy(k_busy), .k_done(k_done), .k_cycles(k_cycles),
        .cfg_cloud_thr(cfg_cloud_thr), .cfg_change_thr(cfg_change_thr),
        .c_start(c_start), .c_done(c_done), .c_clear(c_clear), .c_sharp(c_sharp), .c_change(c_change), .c_score(c_score),
        .cfg_w_clear(cfg_w_clear), .cfg_w_sharp(cfg_w_sharp), .cfg_w_change(cfg_w_change), .cfg_sharp_shift(cfg_sharp_shift),
        .q_insert(q_insert), .q_in_score(q_in_score), .q_in_id(q_in_id), .q_pop(q_pop), .q_set_limit(q_set_limit),
        .q_new_limit(q_new_limit), .q_count(q_count), .q_has_data(has_data), .q_top_score(q_top_score), .q_top_id(q_top_id),
        .q_evicted(q_evicted), .q_lost_id(q_lost_id),
        .power_trigger(power_trigger), .power_sample_valid(power_sample_valid), .power_bus_raw(power_bus_raw),
        .power_shunt_raw(power_shunt_raw), .power_ok(power_ok),
        .state(state), .link_ok(link_ok), .busy(busy), .hb_blink(hb_blink));
endmodule
